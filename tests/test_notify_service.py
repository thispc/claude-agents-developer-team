"""The notify client, and the bus door it announces through.

The notifier is a service now and this file tests the DOOR to it — the shim in
conductor/app/notify.py — plus the conductor's new POST /internal/bus, which is
how an extracted service puts an event on the platform bus without ever opening
the events table. The service itself is mounted in-process by tests/conftest.py.

What the notifier DOES with what it is given — dedup, the hourly ceiling, the
fingerprint's coarseness — lives in services/notify/tests.
"""

import json
import re
import sys
from pathlib import Path

import httpx
import pytest

from conftest import NOTIFY_TEST_TOKEN, notify_service

REPO = Path(__file__).resolve().parent.parent


class _DeadTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """The service being down, as a transport: every request refuses."""

    def handle_request(self, request):
        raise httpx.ConnectError("connection refused (drill)")

    async def handle_async_request(self, request):
        raise httpx.ConnectError("connection refused (drill)")


def _wipe():
    con = notify_service.helpers.db()
    con.execute("DELETE FROM notify_seen")
    con.execute("DELETE FROM notify_sent")
    con.commit()


@pytest.fixture()
def shim(fresh_db, monkeypatch):
    """The conductor's notify door, wired by conftest to the mounted service."""
    from app import notify
    _wipe()
    monkeypatch.setattr(notify, "_repo", lambda: "o/r")
    return notify


@pytest.fixture()
def dead_shim(fresh_db, monkeypatch):
    """The same door with the notifier unreachable — the degraded world."""
    from app import notify
    monkeypatch.setattr(notify, "_TRANSPORT", _DeadTransport())
    monkeypatch.setattr(notify, "_sync_client",
                        lambda: httpx.Client(base_url="http://notify.test",
                                             transport=_DeadTransport()))
    monkeypatch.setattr(notify, "_repo", lambda: "o/r")
    return notify


@pytest.fixture()
def filed(monkeypatch):
    """The GitHub call, captured on the SERVICE — the side that holds the token."""
    out = []

    async def fake_issue(repo, title, body):
        out.append({"repo": repo, "title": title, "body": body})
        return 100 + len(out)

    monkeypatch.setattr(notify_service, "create_issue", fake_issue)
    return out


# --- the client against the real service --------------------------------------

async def test_a_fault_goes_through_the_door_and_comes_back_as_an_issue(shim, filed):
    r = await shim.report_error("manager crashed", "RuntimeError: boom",
                                {"project": "shop"})
    assert r == {"sent": True, "issue": 101}
    assert filed[0]["repo"] == "o/r"
    assert "manager crashed" in filed[0]["title"]
    assert "shop" in filed[0]["body"], "the context must survive the wire"


async def test_the_second_occurrence_is_counted_not_filed(shim, filed):
    await shim.report_error("k", "same thing")
    again = await shim.report_error("k", "same thing")
    assert again["sent"] is False and again["count"] == 2 and again["issue"] == 101
    assert len(filed) == 1


def test_status_composes_the_services_answer_with_the_conductors_repo(shim, filed):
    st = shim.status()
    assert st["repo"] == "o/r", "the repo is conductor knowledge, added here"
    assert st["enabled"] is True and "distinct_faults" in st
    assert "degraded" not in st


async def test_forget_reaches_the_services_memory(shim, filed):
    await shim.report_error("k", "boom")
    assert shim.forget() >= 1
    assert shim.status()["distinct_faults"] == []


def test_health_is_true_against_the_running_service_and_false_without_it(shim, dead_shim):
    assert dead_shim.health() is False
    from app import notify
    from conftest import _svc_client
    notify._sync_client = lambda: _svc_client(notify_service, "http://notify.test",
                                              NOTIFY_TEST_TOKEN)
    assert notify.health() is True


# --- degraded: silence is this module's designed failure mode -----------------

async def test_report_error_says_the_notifier_is_down_and_never_raises(dead_shim):
    assert await dead_shim.report_error("k", "something") == \
        {"sent": False, "reason": "notify service down"}


async def test_a_digest_with_the_notifier_down_never_blocks_a_sprint(dead_shim):
    from app import db
    p = db.create_project("x", "b", "o/r", 5.0, 3, owner_id=1, sprints=2)
    assert await dead_shim.sprint_digest(p, 1) == \
        {"sent": False, "reason": "notify service down"}


def test_degraded_status_says_so_instead_of_lying_that_nothing_broke(dead_shim):
    st = dead_shim.status()
    assert st["degraded"] is True and st["distinct_faults"] == []
    assert st["enabled"] is False, "an unreachable notifier is not an enabled one"


def test_degraded_forget_is_zero(dead_shim):
    assert dead_shim.forget() == 0


async def test_upkeep_completes_silently_with_the_notifier_down(dead_shim, monkeypatch):
    """The plan's drill: `pc stop notify` → the daily self-check still finishes.
    A notifier that can break the thing it reports on is worse than none."""
    from app import findings, upkeep
    f = findings.record("crash", "a finding worth filing", severity="high",
                        title="something is wrong")
    res = await upkeep._file(f)
    assert res["sent"] is False and res["reason"] == "notify service down"
    assert res["finding"] == f["id"], "the check still finished and named the finding"


async def test_the_outage_is_logged_once_per_window_not_once_per_call(dead_shim):
    from app import logs
    logs._LAST.clear()
    for _ in range(5):
        await dead_shim.report_error("k", "the same fault")
    rows = [r for r in logs.recent(event="notify_degraded", limit=50)
            if r.get("verb") == "report_error"]
    assert len(rows) == 1 and rows[0].get("repeats") == 5
    assert rows[0]["level"] == "warn"


# --- the bus door: POST /internal/bus -----------------------------------------

@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    """A generated fleet, in a temp directory: tokens on disk and a topology that
    says which doors each service is allowed."""
    from app.routes import svc
    (tmp_path / "tokens").mkdir(parents=True)
    (tmp_path / "tokens" / "notify.token").write_text("notify-token-456")
    (tmp_path / "tokens" / "usage.token").write_text("usage-token-123")
    (tmp_path / "fleet_topology.json").write_text(json.dumps({"services": {
        "usage": {"kind": "service", "managed": True, "url": "http://u",
                  "doors": ["tuning"], "knobs": ["usage_window_h"]},
        "notify": {"kind": "service", "managed": True, "url": "http://n",
                   "doors": ["bus"], "knobs": []},
    }}))
    monkeypatch.setattr(svc, "_DATA", tmp_path)
    return tmp_path


def test_an_allowlisted_service_can_emit(client, fleet):
    from app import db
    r = client.post("/internal/bus", headers={"X-Service-Token": "notify-token-456"},
                    json={"kind": "notified", "payload": {"issue": 7, "kind": "crash"}})
    assert r.status_code == 200 and r.json()["ok"] is True
    (ev,) = [e for e in db.list_events(0) if e["kind"] == "notified"]
    assert ev["source"] == "notify", "a blank source is stamped with the caller's name"
    assert json.loads(ev["payload"])["issue"] == 7


def test_a_service_without_the_bus_door_is_refused(client, fleet):
    """The meter has no business emitting events, and being inside the fleet is
    not a permission."""
    r = client.post("/internal/bus", headers={"X-Service-Token": "usage-token-123"},
                    json={"kind": "anything", "payload": {}})
    assert r.status_code == 403 and "bus door" in r.text


def test_a_wrong_or_missing_token_cannot_emit(client, fleet):
    assert client.post("/internal/bus", json={"kind": "k"}).status_code == 401
    assert client.post("/internal/bus", headers={"X-Service-Token": "nope"},
                       json={"kind": "k"}).status_code == 401


def test_the_worker_token_is_not_a_fleet_credential(client, fleet):
    """Two populations, two checks. /internal/events is the worker's door and
    stays exactly as it was; this one is not reachable with its token."""
    r = client.post("/internal/bus", headers={"X-Service-Token": "test-worker-token"},
                    json={"kind": "k"})
    assert r.status_code == 401


def test_an_emitted_event_reaches_the_live_feed(root_client, fleet):
    """The whole reason the door exists: an event from a service is an ordinary
    event — durable, and on the dashboard's websocket like every other one."""
    with root_client.websocket_connect("/ws") as ws:
        root_client.post("/internal/bus",
                         headers={"X-Service-Token": "notify-token-456"},
                         json={"kind": "notified", "payload": {"issue": 7}})
        for _ in range(10):
            ev = ws.receive_json()
            if ev["kind"] == "notified":
                break
        assert ev["kind"] == "notified" and ev["source"] == "notify"
        assert json.loads(ev["payload"])["issue"] == 7


async def test_the_service_announces_a_filed_issue_through_that_door(shim, filed,
                                                                    fleet, client,
                                                                    monkeypatch):
    """End to end: the shim calls the service, the service files the issue, and
    the service announces it back through the conductor rather than writing the
    events table itself."""
    from app import db, main as app_main
    # The service announces with ITS OWN token, so that is the token the fleet's
    # tokens/ directory has to hold — not a second one invented for the test.
    (fleet / "tokens" / "notify.token").write_text(NOTIFY_TEST_TOKEN)
    monkeypatch.setattr(notify_service, "CONDUCTOR_URL", "http://conductor.test")
    monkeypatch.setattr(notify_service, "BUS_TRANSPORT",
                        httpx.ASGITransport(app=app_main.app))
    r = await shim.report_error("manager crashed", "RuntimeError: boom")
    assert r["sent"] is True
    assert [e for e in db.list_events(0) if e["kind"] == "notified"], \
        "the service filed an issue and told nobody"


async def test_a_closed_bus_door_does_not_stop_the_notification(shim, filed, monkeypatch):
    """An issue that was filed but not announced is a notification that worked.
    Raising there would turn a cosmetic gap into the failure of the thing it was
    reporting on."""
    monkeypatch.setattr(notify_service, "BUS_TRANSPORT", _DeadTransport())
    monkeypatch.setattr(notify_service, "CONDUCTOR_URL", "http://conductor.test")
    assert (await shim.report_error("k", "boom"))["sent"] is True


def test_the_service_never_writes_the_events_table_itself():
    """SERVICE_CONTRACT rule 5, in two assertions: the service asks the conductor
    to emit, and it declares no events table of its own. The bus stays
    single-writer."""
    src = (REPO / "services" / "notify" / "app.py").read_text()
    assert "/internal/bus" in src
    assert "events" not in notify_service.N_SCHEMA.lower()
    assert "devteam.db" not in src.split("LEGACY_DB_PATH", 1)[1].split("def ", 1)[1], \
        "the only mention of the conductor's database is the one-time backfill"


# --- what stayed in the conductor ---------------------------------------------

async def test_the_digest_is_composed_here_and_only_the_text_crosses(shim, filed):
    """Every line of it is a JOIN over projects and tasks. Handing a service a
    reader on those would undo the isolation the extraction bought."""
    from app import db
    p = db.create_project("shop", "b", "o/r", 5.0, 3, owner_id=1, sprints=3)
    t = db.create_task(p, "backend", "checkout endpoint", "d")
    db.update_task(t, status="done", verification='{"ran": true, "ok": true}')
    r = await shim.sprint_digest(p, 1)
    assert r["sent"] is True
    assert "checkout endpoint" in filed[0]["body"]
    events = [e for e in db.list_events(p) if e["kind"] == "digest_filed"]
    assert events, "the digest's own bus event stays conductor-side"


def test_the_shim_holds_no_git_credential():
    """The GitHub call moved with the dedup and the ceiling; the conductor's
    github_client stays for the things that did not move (PRs, merges)."""
    import re
    src = (REPO / "conductor" / "app" / "notify.py").read_text()
    imports = [l for l in src.splitlines()
               if re.match(r"\s*(from|import)\b", l) and "github" in l]
    assert imports == [], f"the shim still imports a GitHub client: {imports}"
    from app import notify
    assert not hasattr(notify, "github_client")


# --- the wiring ---------------------------------------------------------------

def test_gen_fleet_wires_the_url_the_door_and_the_one_credential(tmp_path):
    import shutil
    sys.path.insert(0, str(REPO / "tools"))
    import gen_fleet
    shutil.copy(REPO / "services.yaml", tmp_path / "services.yaml")
    gen_fleet.generate(tmp_path, {"GITHUB_TOKEN": "ghp_from_the_shell"})
    assert "NOTIFY_URL=http://127.0.0.1:8883" in \
        (tmp_path / "data/env/conductor.env").read_text()
    nenv = (tmp_path / "data/env/notify.env").read_text()
    assert "PORT=8883" in nenv and "DB_PATH=data/notify.db" in nenv
    assert "GITHUB_TOKEN=ghp_from_the_shell" in nenv, \
        "the GitHub call moved here, so the token has to follow it"
    topo = json.loads((tmp_path / "data/fleet_topology.json").read_text())["services"]
    assert topo["notify"]["doors"] == ["bus"] and topo["notify"]["knobs"] == []


def test_no_model_credential_follows_the_git_one(tmp_path):
    """The one documented exception must not become two."""
    import shutil
    sys.path.insert(0, str(REPO / "tools"))
    import gen_fleet
    shutil.copy(REPO / "services.yaml", tmp_path / "services.yaml")
    gen_fleet.generate(tmp_path, {"ANTHROPIC_API_KEY": "sk-ant-leaked",
                                  "OPENAI_API_KEY": "sk-leaked",
                                  "CLAUDE_CODE_OAUTH_TOKEN": "oauth-leaked"})
    for name in ("notify", "usage", "knowledge"):
        env = (tmp_path / f"data/env/{name}.env").read_text()
        for forbidden in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                          "CLAUDE_CODE_OAUTH_TOKEN"):
            assert forbidden not in env, f"{forbidden} reached {name}'s env"


# --- the cutover: no fallback left, and the kv keys go ------------------------

def test_without_the_url_init_refuses_and_says_where_to_look(fresh_db, monkeypatch):
    """There is no in-process notifier any more, so a conductor with none
    configured must fail at the door — loudly, naming the boot script — rather
    than run all night unable to tell anyone that something broke."""
    from app import notify
    monkeypatch.setattr(notify, "_URL", "")
    with pytest.raises(RuntimeError) as e:
        notify.init()
    msg = str(e.value)
    assert "NOTIFY_URL" in msg and "run-local.sh" in msg and "services.yaml" in msg


async def test_the_verbs_still_degrade_when_the_url_is_missing(fresh_db, monkeypatch):
    """init() is the loud door; the verbs stay soft. Silence is this module's
    designed failure mode, and a misconfigured URL must not become a crash loop."""
    from app import notify
    monkeypatch.setattr(notify, "_URL", "")
    monkeypatch.setattr(notify, "_TRANSPORT", _DeadTransport())
    monkeypatch.setattr(notify, "_sync_client",
                        lambda: httpx.Client(base_url="", transport=_DeadTransport()))
    monkeypatch.setattr(notify, "_repo", lambda: "o/r")
    assert (await notify.report_error("k", "x"))["reason"] == "notify service down"
    assert notify.status()["degraded"] is True
    assert notify.forget() == 0


def test_init_drops_the_migrated_kv_keys(fresh_db):
    """notify_seen:* was the dedup memory the service copied on first boot;
    notify_sent was the rolling hour it deliberately did not. Keeping either
    would let a reader conclude the dedup still happens here."""
    from app import db, notify
    db.kv_set("notify_sent", [1.0, 2.0])
    db.kv_set("notify_seen:abc123", {"count": 3, "first": 0, "last": 0, "issue": 12})
    db.kv_set("notify_seen:def456", {"count": 1, "first": 0, "last": 0, "issue": None})
    notify.init()
    assert db.kv_get("notify_sent") is None
    assert db.kv_prefix("notify_seen:") == {}
    notify.init()          # and a box that never had them boots fine


def test_nothing_imports_the_deleted_fallback():
    """The file is gone; what matters is that no code still reaches for it."""
    assert not (REPO / "conductor" / "app" / "_notify_legacy.py").exists()
    importers = [str(p) for p in (REPO / "conductor").rglob("*.py")
                 if re.search(r"^\s*(from|import).*_notify_legacy", p.read_text(), re.M)]
    assert importers == []


def test_the_first_line_crash_the_fuzzer_found_is_gone_from_the_conductor_too():
    """`"\r".strip().splitlines()` is an EMPTY list, so `[0]` raised — on code
    whose whole promise is that it never raises. The notify service's contract
    fuzzer found it in its copy; findings.py had the same two lines and is the
    copy that SURVIVED the deletions."""
    from app import findings
    assert findings._head("\r", 200) == ""
    assert findings._fingerprint("crash", "\r")          # no raise IS the assertion
    assert findings.record("crash", "\r", severity="warning") is not None
    offenders = []
    for path in (REPO / "conductor").rglob("*.py"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if ".strip().splitlines()[0]" not in line:
                continue
            if "`" in line:                     # prose ABOUT the bug, not the bug
                continue
            if "if rough.strip()" in line:      # selfops guards its own read
                continue
            offenders.append(f"{path.name}:{n}")
    assert offenders == [], f"the unguarded first-line read survives at {offenders}"
