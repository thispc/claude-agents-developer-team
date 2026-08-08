"""The usage client: the conductor's side of the P2 extraction.

The meter is a service now and this file tests the DOOR to it — the shim in
conductor/app/usage.py — plus the conductor door it reads its knobs through. The
service itself is mounted in-process by tests/conftest.py (real app, temp
database, a TestClient standing in for the socket), so these exercise the real
client path against the real meter: no sockets, no fleet, no mock that can drift
from the thing it stands for.

What the meter DOES with what it is given lives in services/usage/tests.
"""

import json
import re
import sys
import time
from pathlib import Path

import httpx
import pytest

from conftest import USAGE_TEST_TOKEN, usage_service

REPO = Path(__file__).resolve().parent.parent


class _DeadTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """The service being down, as a transport: every request refuses."""

    def handle_request(self, request):
        raise httpx.ConnectError("connection refused (drill)")

    async def handle_async_request(self, request):
        raise httpx.ConnectError("connection refused (drill)")


@pytest.fixture()
def shim(fresh_db):
    """The conductor's usage door, wired by conftest to the mounted service and
    emptied by fresh_db."""
    from app import usage
    return usage


@pytest.fixture()
def dead_shim(fresh_db, monkeypatch):
    """The same door with the meter unreachable — the degraded world."""
    from app import usage
    monkeypatch.setattr(usage, "_client",
                        lambda: httpx.Client(base_url="http://usage.test",
                                             transport=_DeadTransport()))
    return usage


# --- the client against the real service --------------------------------------

def test_the_shim_round_trips_a_note_into_the_picture(shim):
    now = time.time()
    shim.note("manager", "claude-opus-5", tok=4000, cache=900_000, usd=1.25, ts=now - 60)
    u = shim.snapshot(now)
    assert u["used_tok"] == 4000 and u["owner_tok"] == 4000
    assert u["cache_tok"] == 900_000 and u["usd"] == 1.25
    assert u["by_source"]["manager"]["calls"] == 1
    assert "degraded" not in u


def test_rows_come_back_through_the_door(shim):
    now = time.time()
    shim.note("repair", "m", tok=7, ts=now - 10)
    rows = shim.rows(now - 60)
    assert len(rows) == 1 and rows[0]["source"] == "repair" and rows[0]["tok"] == 7
    assert shim.rows(now + 60) == []


def test_note_result_parses_the_sdk_object_here_and_sends_only_numbers(shim):
    """The SDK's ResultMessage is a Python object the service will never see —
    parsing it is the shim's job, and only what it pulls out crosses the wire."""
    class _Result:
        total_cost_usd = 0.42
        usage = {"input_tokens": 100, "output_tokens": 50,
                 "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 1000}

    now = time.time()
    assert shim.note_result("worker", "claude-haiku-4-5", _Result()) == 0.42
    (row,) = shim.rows(now - 60)
    assert row["tok"] == 150 and row["cache"] == 10_000 and row["usd"] == 0.42
    assert row["source"] == "worker"


# --- attribution: explicit on the wire, ambient only at the call site ---------

def test_the_wire_carries_the_literal_source(shim, monkeypatch):
    """A meter that guesses who spent can bill the crew's own footsteps to the
    owner and put the crew to sleep forever. So the source is a field somebody
    wrote down, sent on every request."""
    sent = {}
    real_note = usage_service.note

    def _spy(source, *a, **kw):
        sent["source"] = source
        return real_note(source, *a, **kw)

    monkeypatch.setattr(usage_service, "note", _spy)
    shim.note("worker", "m", tok=1)
    assert sent["source"] == "worker"


def test_the_contextvar_never_reaches_the_service(shim, monkeypatch):
    """`attributed` is a CONDUCTOR-side convenience. Inside it, a call site that
    knows its own source still wins: the ambient value is not allowed to
    overwrite an explicit one on its way out."""
    sent = []
    real_note = usage_service.note
    monkeypatch.setattr(usage_service, "note",
                        lambda source, *a, **kw: (sent.append(source),
                                                  real_note(source, *a, **kw))[1])
    with shim.attributed("repair"):
        shim.note("worker", "m", tok=1)
    assert sent == ["worker"]
    src = (REPO / "services" / "usage" / "app.py").read_text()
    assert "contextvars" not in src, "ambient attribution has no business in a meter"


def test_attributed_still_resolves_defaults_at_the_call_site(shim):
    """Kept working so nothing else had to churn — it is read HERE and turned
    into a literal before anything is sent."""
    assert shim.current_source("studio") == "studio"
    with shim.attributed("repair"):
        assert shim.current_source("studio") == "repair"
    assert shim.current_source("studio") == "studio"


def test_complete_resolves_the_source_before_the_call_not_inside_it(fresh_db):
    """providers.complete gained an explicit `source`, resolved at its top. That
    is what makes attribution survive a process boundary: after that line it is a
    string travelling as an argument, not a value the meter has to reach for."""
    import inspect
    from app import providers
    sig = inspect.signature(providers.complete)
    assert "source" in sig.parameters and sig.parameters["source"].default == ""
    body = inspect.getsource(providers.complete)
    assert "current_source" in body.split("ep = endpoint", 1)[0], \
        "the ambient default must be resolved before any provider work begins"
    assert "usage.note_result(source," in inspect.getsource(providers._anthropic)


def test_every_spender_names_itself(fresh_db):
    """The four paths that actually spend the subscription each pass a literal —
    a new spender that forgets shows up here rather than as a mystery row."""
    conductor = REPO / "conductor" / "app"
    assert 'usage.note_result("repair"' in (conductor / "repair_builder.py").read_text()
    assert 'usage.note_result("manager"' in (conductor / "manager.py").read_text()
    assert 'usage.note("worker"' in (conductor / "routes" / "internal.py").read_text()
    assert "usage.note_result(source," in (conductor / "providers.py").read_text()


# --- the fail-safe verdict ----------------------------------------------------

def test_a_healthy_meter_lets_an_idle_box_work(shim):
    ok, why, wake = shim.verdict(time.time())
    assert ok and why == "" and wake == 0.0


def test_the_verdict_fails_safe_when_the_meter_is_unreachable(dead_shim):
    """The ONE verb that refuses instead of shrugging. "I cannot see the quota"
    and "the quota is free" are opposite answers, and only one is safe to act on:
    spending blind against the owner's subscription is the failure this whole
    module exists to prevent."""
    now = time.time()
    ok, why, wake = dead_shim.verdict(now)
    assert ok is False
    assert "usage meter unreachable" in why


def test_the_fail_safe_wake_is_bounded_so_a_flap_cannot_sleep_the_crew_forever(dead_shim):
    now = time.time()
    _, _, wake = dead_shim.verdict(now)
    assert wake == now + dead_shim.FAILSAFE_WAKE_S
    assert 60 <= dead_shim.FAILSAFE_WAKE_S <= 900, \
        "a refusal this cheap to hit must be minutes, not hours"


def test_the_crews_sleep_decision_honours_the_fail_safe(dead_shim):
    """The drill that matters: not that the verb returns a tuple, but that
    headroom() — the thing that actually puts the crew to sleep — refuses with
    the meter's reason and the meter's bounded wake."""
    from app import repair
    now = time.time()
    ok, why, wake = repair.headroom(now)
    assert ok is False
    assert "usage meter unreachable" in why
    assert wake == now + dead_shim.FAILSAFE_WAKE_S


def test_the_crew_resumes_the_moment_the_meter_answers_again(fresh_db, monkeypatch):
    """pc stop usage → pc start usage, without restarting the conductor. Only the
    transport changes; nothing is re-imported and no state is rebuilt."""
    from app import repair, usage
    from conftest import _svc_client
    now = time.time()
    monkeypatch.setattr(usage, "_client",
                        lambda: httpx.Client(base_url="http://usage.test",
                                             transport=_DeadTransport()))
    assert repair.headroom(now)[0] is False
    monkeypatch.setattr(usage, "_client",
                        lambda: _svc_client(usage_service, "http://usage.test",
                                            USAGE_TEST_TOKEN))
    ok, why, _ = repair.headroom(now)
    assert ok, f"the crew stayed asleep after the meter came back: {why}"


# --- the other degraded shapes ------------------------------------------------

def test_a_dropped_note_never_breaks_the_thing_being_metered(dead_shim):
    """Metering must never break the thing being metered — that rule outranks the
    measurement. No raise IS the assertion."""
    dead_shim.note("repair", "m", tok=10)
    assert dead_shim.note_result("repair", "m", object()) == 0.0


def test_a_degraded_snapshot_is_zeros_that_admit_it(dead_shim):
    u = dead_shim.snapshot(time.time())
    assert u["degraded"] is True
    for k in ("used_tok", "owner_tok", "repair_tok", "cache_tok", "calls", "allowance_tok"):
        assert u[k] == 0, f"{k} was invented"
    assert u["contended"] is False and u["by_source"] == {}


def test_a_degraded_snapshot_has_every_key_the_engine_reads(dead_shim):
    """A missing key is a KeyError three frames away from the outage that caused
    it. The zero shape is compared against the real one, field for field."""
    from app import usage
    real = set(usage.snapshot(time.time()))
    degraded = set(dead_shim.snapshot(time.time()))
    assert real - degraded == set(), f"the degraded snapshot is missing {real - degraded}"


def test_degraded_rows_are_empty(dead_shim):
    assert dead_shim.rows(0) == []


def test_the_outage_is_logged_once_per_window_not_once_per_call(dead_shim):
    """A sleep loop that asks every tick would otherwise write the same warn 180
    times an hour, and the record is unreadable exactly when someone needs it."""
    from app import logs
    logs._LAST.clear()
    for _ in range(5):
        dead_shim.snapshot(time.time())
    rows = [r for r in logs.recent(event="usage_degraded", limit=50)
            if r.get("verb") == "snapshot"]
    assert len(rows) == 1, f"{len(rows)} rows for five identical failures"
    assert rows[0].get("repeats") == 5 and rows[0]["level"] == "warn"


def test_health_is_false_when_the_meter_is_down_and_true_when_it_is_not(shim, dead_shim):
    assert dead_shim.health() is False
    from app import usage
    from conftest import _svc_client
    usage._client = lambda: _svc_client(usage_service, "http://usage.test", USAGE_TEST_TOKEN)
    assert usage.health() is True


# --- the knob door: GET /internal/tuning --------------------------------------

@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    """A generated fleet, in a temp directory: tokens on disk and a topology that
    says which doors and knobs each service is allowed. The real thing, minus the
    ports."""
    from app.routes import svc
    (tmp_path / "tokens").mkdir(parents=True)
    (tmp_path / "tokens" / "usage.token").write_text("usage-token-123")
    (tmp_path / "tokens" / "notify.token").write_text("notify-token-456")
    (tmp_path / "tokens" / "knowledge.token").write_text("knowledge-token-789")
    (tmp_path / "tokens" / "conductor.token").write_text("conductor-token-000")
    (tmp_path / "fleet_topology.json").write_text(json.dumps({"services": {
        "conductor": {"kind": "core", "managed": True, "url": "http://c",
                      "doors": ["bus", "tuning"], "knobs": []},
        "usage": {"kind": "service", "managed": True, "url": "http://u",
                  "doors": ["tuning"],
                  "knobs": ["usage_window_h", "repair_idle_share"]},
        "notify": {"kind": "service", "managed": True, "url": "http://n",
                   "doors": ["bus"], "knobs": []},
        "knowledge": {"kind": "service", "managed": True, "url": "http://k",
                      "doors": [], "knobs": []},
        "sandbox": {"kind": "external", "managed": False},
    }}))
    monkeypatch.setattr(svc, "_DATA", tmp_path)
    return tmp_path


def test_the_tuning_door_answers_an_allowlisted_service(client, fleet):
    from app import tuning
    r = client.get("/internal/tuning", params={"name": "usage_window_h"},
                   headers={"X-Service-Token": "usage-token-123"})
    assert r.status_code == 200
    assert r.json() == {"name": "usage_window_h", "value": tuning.get("usage_window_h")}


def test_the_tuning_door_refuses_a_knob_the_service_did_not_declare(client, fleet):
    """Per-service knob allowlist, not just per-service door: nothing else in the
    fifty-odd knobs is the meter's business."""
    r = client.get("/internal/tuning", params={"name": "repair_builder_model"},
                   headers={"X-Service-Token": "usage-token-123"})
    assert r.status_code == 403 and "services.yaml" in r.text


def test_the_tuning_door_refuses_a_service_without_that_door(client, fleet):
    r = client.get("/internal/tuning", params={"name": "usage_window_h"},
                   headers={"X-Service-Token": "notify-token-456"})
    assert r.status_code == 403 and "tuning door" in r.text


def test_the_tuning_door_refuses_a_wrong_or_missing_token(client, fleet):
    assert client.get("/internal/tuning",
                      params={"name": "usage_window_h"}).status_code == 401
    assert client.get("/internal/tuning", params={"name": "usage_window_h"},
                      headers={"X-Service-Token": "nope"}).status_code == 401
    # ...including the conductor's own: a door to yourself is a loop wearing a
    # trenchcoat, and it must not be a usable credential either.
    assert client.get("/internal/tuning", params={"name": "usage_window_h"},
                      headers={"X-Service-Token": "conductor-token-000"}).status_code == 401


def test_the_worker_token_does_not_open_a_service_door(client, fleet):
    """Two populations, two checks. One leaked worker token must not become a
    fleet credential."""
    r = client.get("/internal/tuning", params={"name": "usage_window_h"},
                   headers={"X-Service-Token": "test-worker-token"})
    assert r.status_code == 401


def test_the_meters_fallback_defaults_match_the_conductors(fresh_db):
    """The service falls back to these only when it has NEVER reached the
    conductor. A silently diverged copy is a meter that disagrees with the
    Settings screen that set it."""
    from app import tuning
    for name, value in usage_service.KNOB_DEFAULTS.items():
        assert tuning.KNOBS[name][0] == value, f"{name} drifted from the conductor's default"


def test_the_meter_only_asks_for_knobs_it_is_allowed(fresh_db):
    """services.yaml is the allowlist; the service asking for anything outside it
    would 403 in production and read as an outage."""
    import yaml
    reg = yaml.safe_load((REPO / "services.yaml").read_text())
    allowed = set(reg["services"]["usage"]["knobs"])
    assert set(usage_service.KNOB_DEFAULTS) == allowed


# --- the wiring that makes URL mode the real mode -----------------------------

def test_gen_fleet_wires_the_url_the_doors_and_the_knobs(tmp_path):
    """Proven against a temp root (never the real data/): the conductor's env
    names the usage peer — so every generated boot has USAGE_URL — and the
    topology carries the allowlists the conductor's doors check."""
    import shutil
    sys.path.insert(0, str(REPO / "tools"))
    import gen_fleet
    shutil.copy(REPO / "services.yaml", tmp_path / "services.yaml")
    gen_fleet.generate(tmp_path, {})
    assert "USAGE_URL=http://127.0.0.1:8882" in \
        (tmp_path / "data/env/conductor.env").read_text()
    uenv = (tmp_path / "data/env/usage.env").read_text()
    assert "PORT=8882" in uenv and "DB_PATH=data/usage.db" in uenv
    assert "CONDUCTOR_URL=http://127.0.0.1:8787" in uenv, \
        "the meter cannot read the owner's dials without knowing where the conductor is"
    assert (tmp_path / "data/tokens/usage.token").exists()
    topo = json.loads((tmp_path / "data/fleet_topology.json").read_text())["services"]
    assert topo["usage"]["port"] == 8882 and topo["usage"]["doors"] == ["tuning"]
    assert "repair_idle_share" in topo["usage"]["knobs"]


def test_a_registry_that_asks_for_an_unknown_door_is_refused(tmp_path):
    """A typo would otherwise grant nothing at all and show up as a 403 at 3am
    instead of as a broken registry at generation time."""
    import yaml
    sys.path.insert(0, str(REPO / "tools"))
    import gen_fleet
    p = tmp_path / "services.yaml"
    p.write_text(yaml.safe_dump({"services": {
        "x": {"kind": "service", "port": 9, "cmd": "true", "doors": ["tunning"]}}}))
    with pytest.raises(ValueError) as e:
        gen_fleet.load_registry(p)
    assert "tunning" in str(e.value)


# --- the cutover: no fallback left, and the kv keys go ------------------------

def test_without_the_url_init_refuses_and_says_where_to_look(fresh_db, monkeypatch):
    """There is no in-process meter any more, so a conductor with no service
    configured must fail at the door — loudly, naming the boot script — instead
    of running blind against the owner's subscription."""
    from app import usage
    monkeypatch.setattr(usage, "_URL", "")
    with pytest.raises(RuntimeError) as e:
        usage.init()
    msg = str(e.value)
    assert "USAGE_URL" in msg and "run-local.sh" in msg and "services.yaml" in msg


def test_the_verbs_still_degrade_when_the_url_is_missing(fresh_db, monkeypatch):
    """init() is the loud door; the verbs stay soft. A door that also killed
    every later call would turn one misconfigured process into a crash loop —
    and the verdict still fails safe, which is the shape that matters."""
    from app import usage
    monkeypatch.setattr(usage, "_URL", "")
    monkeypatch.setattr(usage, "_client",
                        lambda: httpx.Client(base_url="", transport=_DeadTransport()))
    now = time.time()
    usage.note("repair", "m", tok=1)          # no raise IS the assertion
    assert usage.rows() == []
    assert usage.snapshot(now)["degraded"] is True
    ok, why, wake = usage.verdict(now)
    assert ok is False and wake == now + usage.FAILSAFE_WAKE_S


def test_init_drops_the_migrated_kv_keys(fresh_db):
    """usage:ledger was the pre-P2 meter and usage:backfilled the guard on the
    one-shot import that fed it. The service copied the first and inherited the
    second's job; keeping either would be a staler copy of numbers another
    process owns, and a flag nobody reads."""
    from app import db, usage
    db.kv_set("usage:ledger", [{"ts": 1.0, "source": "repair", "tok": 5}])
    db.kv_set("usage:backfilled", True)
    usage.init()
    assert db.kv_get("usage:ledger") is None
    assert db.kv_get("usage:backfilled") is None
    usage.init()          # and a box that never had them boots fine


def test_the_crews_own_counter_is_not_collateral(fresh_db):
    """repair:ledger is the crew's own call counter and still the backstop meter
    for boxes whose SDK reports no tokens. It was never usage's to drop."""
    from app import db, usage
    db.kv_set("repair:ledger", [{"ts": 1.0, "kind": "build", "model": "m", "usd": 1.0}])
    usage.init()
    assert db.kv_get("repair:ledger"), "the backstop meter was dropped with the blob"


def test_nothing_imports_the_deleted_fallback():
    """The file is gone; what matters is that no code still reaches for it.
    (The shim's docstring names it as history — a comment cannot import.)"""
    assert not (REPO / "conductor" / "app" / "_usage_legacy.py").exists()
    importers = [str(p) for p in (REPO / "conductor").rglob("*.py")
                 if re.search(r"^\s*(from|import).*_usage_legacy", p.read_text(), re.M)]
    assert importers == []


def test_the_engine_no_longer_runs_its_own_backfill():
    """backfill_repair() moved INTO the service's first-boot copy. Left here it
    would re-import the crew's ledger on every boot with its guard key dropped —
    which is exactly how a meter starts double-counting."""
    from app import usage
    assert not hasattr(usage, "backfill_repair")
    assert "backfill_repair" not in (REPO / "conductor" / "app" / "repair.py").read_text()


def test_the_conductor_boots_the_three_extracted_stores_loudly():
    """Each init() is the door that refuses. A store whose init is never called
    would degrade silently forever instead of failing on the first boot."""
    main = (REPO / "conductor" / "app" / "main.py").read_text()
    for mod in ("knowledge", "usage", "notify"):
        assert f"{mod}.init()" in main, f"{mod}.init() is not called at boot"
