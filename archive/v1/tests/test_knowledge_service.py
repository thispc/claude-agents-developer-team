"""The knowledge client: the conductor's side of the P1 extraction.

The store is a service now and this file tests the DOOR to it — the shim in
conductor/app/knowledge.py. The service itself is mounted in-process by
tests/conftest.py (real app, temp database, httpx ASGI transport), so these
exercise the real client path against the real store: no sockets, no fleet, no
mock that can drift from the thing it stands for.

What the store DOES with what it is given lives in services/knowledge/tests.
"""

import json
import re
import sys
from pathlib import Path

import httpx
import pytest

from conftest import KNOWLEDGE_TEST_TOKEN, knowledge_service

REPO = Path(__file__).resolve().parent.parent


class _DeadTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """The service being down, as a transport: every request refuses."""

    def handle_request(self, request):
        raise httpx.ConnectError("connection refused (drill)")

    async def handle_async_request(self, request):
        raise httpx.ConnectError("connection refused (drill)")


@pytest.fixture()
def shim(fresh_db):
    """The conductor's knowledge door, wired by conftest to the mounted service
    and emptied by fresh_db."""
    from app import knowledge
    return knowledge


@pytest.fixture()
def dead_shim(fresh_db, monkeypatch):
    """The same door with the service unreachable — the degraded world."""
    from app import knowledge
    monkeypatch.setattr(knowledge, "_TRANSPORT", _DeadTransport())
    monkeypatch.setattr(knowledge, "_sync_client",
                        lambda: httpx.Client(base_url="http://knowledge.test",
                                             transport=_DeadTransport()))
    return knowledge


# --- the client against the real service -------------------------------------

async def test_the_shim_round_trips_remember_and_recall(shim):
    rid = await shim.remember("a1", "the build failed with ImportError: no module named app",
                              "an ImportError here means the venv symlink, not the code",
                              sig="error:ImportError", good=2)
    assert rid > 0
    hits = await shim.recall("a1", "ImportError building the app", k=3)
    assert hits and "venv symlink" in hits[0]["says"]
    assert set(hits[0]["why"]) >= {"similarity", "shared_terms", "relevance", "matched"}


async def test_the_shim_speaks_the_whole_verb_surface(shim):
    rid = await shim.remember("a1", "HTTP 505 from the billing host", "the staging env",
                              good=3)
    before = (await shim.recall("a1", "505 from billing", k=1))[0]["confidence"]
    shim.reinforce(rid, "bad")
    after = (await shim.recall("a1", "505 from billing", k=1))[0]["confidence"]
    assert after < before
    st = shim.stats("a1")
    assert st["total"] == 1 and "degraded" not in st
    assert shim._tokens("the build is on the host") == ["build", "host"]
    assert shim.forget("a1") == 1
    assert shim.stats("a1")["total"] == 0


async def test_the_shim_clamps_k_like_the_in_process_body_did(shim):
    """The service's contract bounds k to 1..25. Callers kept their old habits,
    so the clamp lives here — an out-of-range k must not become a 422 that reads
    like an outage and silently empties a briefing."""
    await shim.remember("a1", "one situation", "one lesson")
    assert await shim.recall("a1", "one situation", k=0) != []
    assert await shim.recall("a1", "one situation", k=999) != []


async def test_an_empty_query_never_reaches_the_wire(shim, monkeypatch):
    def _boom():
        raise AssertionError("an empty query must not cost a round-trip")
    monkeypatch.setattr(shim, "_client", _boom)
    assert await shim.recall("a1", "   ") == []


async def test_the_settings_ride_the_call_so_no_key_lives_in_the_service(shim):
    """The embedding key is per-request data, never service env — the conductor
    stays the only holder of model credentials."""
    import inspect
    src = inspect.getsource(shim.remember) + inspect.getsource(shim.recall)
    assert '"settings": settings or {}' in src
    svc_env = (REPO / "services" / "knowledge" / "app.py").read_text()
    assert "OPENAI_API_KEY" not in svc_env and "ANTHROPIC_API_KEY" not in svc_env


# --- the cutover: no fallback left, and the legacy table goes ----------------

def test_without_the_url_init_refuses_and_says_where_to_look(fresh_db, monkeypatch):
    """There is no in-process store any more, so a conductor with no service
    configured must fail at the door — loudly, naming the boot script — instead
    of running with a memory that silently answers nothing."""
    from app import knowledge
    monkeypatch.setattr(knowledge, "_URL", "")
    with pytest.raises(RuntimeError) as e:
        knowledge.init()
    msg = str(e.value)
    assert "KNOWLEDGE_URL" in msg and "run-local.sh" in msg
    assert "services.yaml" in msg


async def test_the_verbs_still_degrade_when_the_url_is_missing(fresh_db, monkeypatch):
    """init() is the loud door; the verbs stay soft. A door that also killed
    every later call would turn one misconfigured process into a crash loop."""
    from app import knowledge
    monkeypatch.setattr(knowledge, "_URL", "")
    monkeypatch.setattr(knowledge, "_TRANSPORT", None)
    monkeypatch.setattr(knowledge, "_sync_client",
                        lambda: httpx.Client(base_url="", transport=_DeadTransport()))
    assert await knowledge.recall("a1", "anything") == []
    assert await knowledge.remember("a1", "cue", "says") == 0
    assert knowledge.stats()["degraded"] is True


def test_init_drops_the_stranglers_leftover_table(fresh_db):
    """knowledge_legacy was commit A's holding pen: renamed aside so the service
    could copy the rows out and so a rollback could find them. With the fallback
    deleted it is only a staler copy of data another process owns."""
    from app import db, knowledge
    db._execute("CREATE TABLE IF NOT EXISTS knowledge_legacy (id INTEGER PRIMARY KEY)")
    assert db._rows("SELECT name FROM sqlite_master WHERE name='knowledge_legacy'")
    knowledge.init()
    assert not db._rows("SELECT name FROM sqlite_master WHERE name='knowledge_legacy'")
    knowledge.init()          # and a box that never had one boots fine


def test_the_conductor_declares_no_knowledge_table_at_all(fresh_db):
    """The honest end of the accounting: after init the conductor's database has
    neither the old table nor the holding pen — the service owns that data."""
    from app import db
    names = {r["name"] for r in db._rows(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('knowledge','knowledge_legacy')")}
    assert names == set()


def test_nothing_imports_the_deleted_fallback():
    """The file is gone; what matters is that no code still reaches for it.
    (The shim's docstring names it as history — a comment cannot import.)"""
    assert not (REPO / "conductor" / "app" / "_knowledge_legacy.py").exists()
    importers = [str(p) for p in (REPO / "conductor").rglob("*.py")
                 if re.search(r"^\s*(from|import).*_knowledge_legacy", p.read_text(),
                              re.M)]
    assert importers == []


# --- the service down: the degraded shapes ------------------------------------

async def test_degraded_recall_is_empty_and_never_raises(dead_shim):
    assert await dead_shim.recall("a1", "anything at all") == []


async def test_degraded_writes_are_noops_that_say_zero(dead_shim):
    assert await dead_shim.remember("a1", "a cue", "a lesson") == 0
    dead_shim.reinforce(7, "good")                     # no raise IS the assertion
    assert dead_shim.forget("a1") == 0


async def test_degraded_stats_says_so_instead_of_lying_zeros(dead_shim):
    assert dead_shim.stats() == {"total": 0, "rows": [], "backends": [], "degraded": True}


async def test_degraded_tokens_are_empty_with_a_log_not_a_crash(dead_shim):
    assert dead_shim._tokens("some text to tokenise") == []


async def test_degraded_backfill_keeps_the_marker_unset_for_a_retry(dead_shim):
    from app import db
    db.kv_set("repair:sprint:1", {"no": 1, "tasks": [
        {"title": "fix the venv symlink", "status": "landed"}]})
    assert await dead_shim.backfill_from_sprints() == 0
    assert not db.kv_get("knowledge:backfilled"), \
        "a degraded backfill must not mark itself done — the seed would be lost forever"


async def test_a_sprint_shaped_flow_survives_the_outage(dead_shim):
    """The plan's drill in miniature: the calls a sprint makes around the store —
    recall for the briefing, remember for the outcome — all land in degraded
    shapes; nothing raises, nothing blocks."""
    assert await dead_shim.recall("lw:1:2", "the task cue", k=3) == []
    assert await dead_shim.remember("lw:1:2", "the task cue", "how it went",
                                    sig="task:x", good=1) == 0
    assert dead_shim.stats().get("degraded") is True


async def test_the_outage_is_logged_once_per_window_not_once_per_call(dead_shim):
    """A loop that recalls every tick would otherwise write the same warn 180
    times an hour, and the record is unreadable exactly when someone needs it.
    One row that counts itself up is the shape that stays readable."""
    from app import logs
    logs._LAST.clear()        # a process-level memo; fresh_db wiped the ring it points into
    for _ in range(5):
        await dead_shim.recall("a1", "the same situation as before")
    recalls = [r for r in logs.recent(event="knowledge_degraded", limit=50)
               if r.get("verb") == "recall"]
    assert len(recalls) == 1, f"{len(recalls)} rows for five identical failures"
    assert recalls[0].get("repeats") == 5, "the row must say how often it happened"
    assert recalls[0]["level"] == "warn"


# --- recovery -----------------------------------------------------------------

async def test_recovery_after_the_outage_needs_no_restart(fresh_db, monkeypatch):
    from app import knowledge
    monkeypatch.setattr(knowledge, "_TRANSPORT", _DeadTransport())
    assert await knowledge.recall("a1", "anything") == []
    # the service comes back (pc start): only the transport changes, no re-import
    monkeypatch.setattr(knowledge, "_TRANSPORT",
                        httpx.ASGITransport(app=knowledge_service.app))
    assert await knowledge.remember("a1", "the port was taken", "pick another port") > 0
    assert (await knowledge.recall("a1", "port already taken", k=1)) != []


# --- the Atlas probe: honest about the real process ---------------------------

def test_the_cards_beat_asks_the_real_service(fresh_db):
    """The knowledge card's heartbeat used to be "a table exists here". With the
    store extracted that claim would be a lie — the beat is the service itself
    answering /health, reached through the conductor's one knowledge door (the
    suite's mounted service), never through a private URL of the probe's own."""
    from app import fleet
    node = {"key": "knowledge", "node_type": "code"}
    assert fleet.beat_of("knowledge", node, None) == "ok"


def test_the_beat_goes_red_the_moment_the_service_does(fresh_db, monkeypatch):
    """A probe that reported green while every recall failed would be worse than
    no probe."""
    from app import fleet, knowledge
    monkeypatch.setattr(knowledge, "_sync_client",
                        lambda: httpx.Client(base_url="http://knowledge.test",
                                             transport=_DeadTransport()))
    assert knowledge.health() is False
    node = {"key": "knowledge", "node_type": "code"}
    assert fleet.beat_of("knowledge", node, None) == "fail", \
        "a raising check is a failed beat, not a crash"


def test_the_beat_is_red_when_the_service_answers_unhealthy(fresh_db, monkeypatch):
    """ok:false — its database will not open — is a running process, not a
    working store."""
    from app import fleet, knowledge

    def _unhealthy(request):
        return httpx.Response(200, json={"ok": False, "checks": {"db": False}})

    monkeypatch.setattr(knowledge, "_sync_client",
                        lambda: httpx.Client(base_url="http://knowledge.test",
                                             transport=httpx.MockTransport(_unhealthy)))
    node = {"key": "knowledge", "node_type": "code"}
    assert fleet.beat_of("knowledge", node, None) == "fail"
    # ...and process-compose reporting the PROCESS healthy does not save it: both
    # halves are required, which is the whole point of asking two questions.
    pc = {"knowledge": {"state": "Running", "ready": "Ready", "running": True,
                        "restarts": 0, "pid": 1, "uptime": "1m", "exit_code": 0}}
    assert fleet.beat_of("knowledge", node, pc) == "fail"


def test_the_beat_is_red_without_a_url(fresh_db, monkeypatch):
    """No service configured is not "healthy by default": the conductor cannot
    recall anything in that state, and the card must say so."""
    from app import fleet, knowledge
    monkeypatch.setattr(knowledge, "_URL", "")
    monkeypatch.setattr(knowledge, "_sync_client",
                        lambda: httpx.Client(base_url="", transport=_DeadTransport()))
    assert fleet.beat_of("knowledge", {"key": "knowledge", "node_type": "code"},
                         None) == "fail"


# --- the wiring that makes URL mode the only mode -----------------------------

def test_gen_fleet_wires_the_url_and_the_gateway_by_construction(tmp_path):
    """Proven against a temp root (never the real data/): the conductor's env
    names the knowledge peer — so every generated boot has KNOWLEDGE_URL — and
    the topology registers knowledge for the /svc gateway."""
    import shutil
    sys.path.insert(0, str(REPO / "tools"))
    import gen_fleet
    shutil.copy(REPO / "services.yaml", tmp_path / "services.yaml")
    gen_fleet.generate(tmp_path, {})
    env_file = (tmp_path / "data/env/conductor.env").read_text()
    assert "KNOWLEDGE_URL=http://127.0.0.1:8881" in env_file
    kenv = (tmp_path / "data/env/knowledge.env").read_text()
    assert "PORT=8881" in kenv and "DB_PATH=data/knowledge.db" in kenv
    assert (tmp_path / "data/tokens/knowledge.token").exists()
    topo = json.loads((tmp_path / "data/fleet_topology.json").read_text())
    k = topo["services"]["knowledge"]
    assert k["managed"] is True and k["port"] == 8881 and k["health"] == "/health"


@pytest.mark.hostonly
def test_both_boot_paths_start_every_service_before_the_conductor():
    """--legacy runs the conductor outside process-compose, but the stores are
    services either way: the script must start each one and hand over its URL on
    BOTH paths, or the legacy path boots into the RuntimeError above.

    Asserted against the loop rather than three literal export lines, because
    that is what the script actually has since P2 — one readiness wait, one list
    of names. Three copies of a wait is how one of them quietly stops waiting.
    """
    script = (REPO / "run-local.sh").read_text()
    conductor_exec = script.index("exec .venv/bin/uvicorn app.main:app")
    loop_at = script.find("for svc in ")
    assert loop_at >= 0, "run-local.sh no longer starts the fleet's services itself"
    names = script[loop_at:script.index(";", loop_at)]
    for svc in ("knowledge", "usage", "notify"):
        assert svc in names, f"the legacy path never starts {svc}"
    export_at = script.index("_URL=${svc_url}")
    assert loop_at < export_at < conductor_exec, \
        "the legacy path execs the conductor without the service URLs"
    # and it starts (or reuses) each service before claiming its URL is good
    assert script.index('--app-dir "services/${svc}"') < export_at
    assert "/health" in script[:export_at], "no readiness wait before the URL is exported"
    # the fleet path gets the URLs the other way: gen_fleet writes them into the
    # env file process-compose sources, so the conductor has them either way
    assert "data/env/conductor.env" in (REPO / "tools" / "gen_fleet.py").read_text() \
        or "data/env/" in script
