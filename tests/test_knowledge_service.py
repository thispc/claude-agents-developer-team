"""P1 drills: the knowledge shim in URL mode, its degraded shapes, and the fallback.

The service app is mounted IN-PROCESS (httpx.ASGITransport / starlette TestClient
over ASGI) — no sockets, offline like everything else here. The suite's baseline
env has KNOWLEDGE_URL unset (conftest pops it), so the fallback swap is the
default world; URL mode is entered per-test by re-importing app.knowledge with
the env set, and always restored.
"""

import importlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO / "services" / "knowledge"
SVC_TOKEN = "drill-service-token"

# --- load the service app under drill-unique names ---------------------------
# (services/knowledge/tests loads its own instance under other names with its
# own temp db; two harnesses must never share a latched environment)

_TMP = tempfile.mkdtemp(prefix="knowledge-drill-")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ENV = {k: os.environ.get(k)
        for k in ("DB_PATH", "SERVICE_TOKEN", "SERVICE_NAME", "LEGACY_DB_PATH")}
os.environ["DB_PATH"] = str(Path(_TMP) / "knowledge-drill.db")
os.environ["SERVICE_TOKEN"] = SVC_TOKEN
os.environ["SERVICE_NAME"] = "knowledge"
os.environ["LEGACY_DB_PATH"] = str(Path(_TMP) / "absent.db")
_SAVED_HELPERS = sys.modules.pop("helpers", None)
try:
    _helpers = _load("knowledge_drill_helpers", SERVICE_DIR / "helpers.py")
    sys.modules["helpers"] = _helpers
    svc = _load("knowledge_drill_app", SERVICE_DIR / "app.py")
finally:
    sys.modules.pop("helpers", None)
    if _SAVED_HELPERS is not None:
        sys.modules["helpers"] = _SAVED_HELPERS
    for _k, _v in _ENV.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


class _DeadTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """The service being down, as a transport: every request refuses."""

    def handle_request(self, request):
        raise httpx.ConnectError("connection refused (drill)")

    async def handle_async_request(self, request):
        raise httpx.ConnectError("connection refused (drill)")


def _reimport_knowledge():
    """Re-execute conductor/app/knowledge.py so it re-reads KNOWLEDGE_URL.
    The module swaps itself for the legacy body when the URL is unset, so a
    plain importlib.reload would reload the wrong module."""
    sys.modules.pop("app.knowledge", None)
    return importlib.import_module("app.knowledge")


def _live_sync_client():
    """A fresh TestClient per call, matching the shim's client-per-call shape —
    a single instance would be closed by the first `with` and unusable after."""
    t = TestClient(svc.app, base_url="http://knowledge.drill")
    t.headers["X-Service-Token"] = SVC_TOKEN
    return t


@pytest.fixture()
def shim(fresh_db, monkeypatch):
    """The conductor shim in URL mode, wired to the in-process service app."""
    monkeypatch.setenv("KNOWLEDGE_URL", "http://knowledge.drill")
    mod = _reimport_knowledge()
    assert mod.__name__ == "app.knowledge" and not hasattr(mod, "embed")
    monkeypatch.setattr(mod, "_TRANSPORT", httpx.ASGITransport(app=svc.app))
    monkeypatch.setattr(mod, "_TOKEN", SVC_TOKEN)
    monkeypatch.setattr(mod, "_sync_client", _live_sync_client)
    svc.helpers.db().execute("DELETE FROM knowledge")
    svc.helpers.db().commit()
    yield mod
    monkeypatch.delenv("KNOWLEDGE_URL", raising=False)
    restored = _reimport_knowledge()
    assert hasattr(restored, "embed"), "the fallback swap did not come back"


@pytest.fixture()
def dead_shim(fresh_db, monkeypatch):
    """URL mode with the service unreachable — the degraded world."""
    monkeypatch.setenv("KNOWLEDGE_URL", "http://knowledge.drill")
    mod = _reimport_knowledge()
    monkeypatch.setattr(mod, "_TRANSPORT", _DeadTransport())
    monkeypatch.setattr(mod, "_sync_client",
                        lambda: httpx.Client(base_url="http://knowledge.drill",
                                             transport=_DeadTransport()))
    yield mod
    monkeypatch.delenv("KNOWLEDGE_URL", raising=False)
    _reimport_knowledge()


# --- URL mode: the shim against the real service, in-process -----------------

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


async def test_the_shim_clamps_k_like_the_legacy_body_did(shim):
    await shim.remember("a1", "one situation", "one lesson")
    # the service contract bounds k to 1..25; the shim owes callers the old
    # clamping semantics, not a fresh 422 → degraded-[] surprise
    assert await shim.recall("a1", "one situation", k=0) != []
    assert await shim.recall("a1", "one situation", k=999) != []


async def test_url_mode_init_renames_the_conductor_table_aside(shim):
    from app import db
    names = {r["name"] for r in db._rows(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('knowledge','knowledge_legacy')")}
    assert names == {"knowledge"}, "fresh_db pre-creates the legacy table"
    shim.init()
    names = {r["name"] for r in db._rows(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('knowledge','knowledge_legacy')")}
    assert names == {"knowledge_legacy"}, "URL-mode init must rename, not drop"
    shim.init()                                        # idempotent on a second boot
    assert {r["name"] for r in db._rows(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('knowledge','knowledge_legacy')")} == {"knowledge_legacy"}


async def test_rolling_back_to_fallback_mode_renames_it_home(shim, monkeypatch):
    from app import db
    db._execute("INSERT INTO knowledge (owner, kind, sig, cue, says, backend, dim,"
                " vec, ts) VALUES ('a1','belief','','old cue','old lesson',"
                " 'hash-256', 256, x'00', 1.0)")
    shim.init()                                        # → knowledge_legacy
    monkeypatch.delenv("KNOWLEDGE_URL", raising=False)
    legacy = _reimport_knowledge()
    assert hasattr(legacy, "embed")
    legacy.init()                                      # rollback: rename back + schema
    rows = db._rows("SELECT says FROM knowledge WHERE owner='a1'")
    assert rows and rows[0]["says"] == "old lesson", \
        "unsetting the URL must return the old rows, not an empty store"


# --- URL mode, service down: the degraded shapes -----------------------------

async def test_degraded_recall_is_empty_and_never_raises(dead_shim):
    assert await dead_shim.recall("a1", "anything at all") == []


async def test_degraded_writes_are_noops_that_say_zero(dead_shim):
    assert await dead_shim.remember("a1", "a cue", "a lesson") == 0
    dead_shim.reinforce(7, "good")                     # no raise IS the assertion
    assert dead_shim.forget("a1") == 0


async def test_degraded_stats_says_so_instead_of_lying_zeros(dead_shim):
    st = dead_shim.stats()
    assert st == {"total": 0, "rows": [], "backends": [], "degraded": True}


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
    briefing_hits = await dead_shim.recall("lw:1:2", "the task cue", k=3)
    assert briefing_hits == []
    assert await dead_shim.remember("lw:1:2", "the task cue", "how it went",
                                    sig="task:x", good=1) == 0
    assert dead_shim.stats().get("degraded") is True


# --- recovery: the same shim reaches a healthy service again -----------------

async def test_recovery_after_the_outage_needs_no_restart(fresh_db, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_URL", "http://knowledge.drill")
    mod = _reimport_knowledge()
    monkeypatch.setattr(mod, "_TOKEN", SVC_TOKEN)
    monkeypatch.setattr(mod, "_TRANSPORT", _DeadTransport())
    assert await mod.recall("a1", "anything") == []
    # the service comes back (pc start): only the transport changes, no re-import
    monkeypatch.setattr(mod, "_TRANSPORT", httpx.ASGITransport(app=svc.app))
    svc.helpers.db().execute("DELETE FROM knowledge")
    svc.helpers.db().commit()
    assert await mod.remember("a1", "the port was taken", "pick another port") > 0
    assert (await mod.recall("a1", "port already taken", k=1)) != []
    monkeypatch.delenv("KNOWLEDGE_URL", raising=False)
    _reimport_knowledge()


# --- fallback mode: the default world stays byte-identical -------------------

def test_without_the_url_the_module_is_the_legacy_body(fresh_db):
    from app import knowledge
    assert knowledge.__name__ == "app._knowledge_legacy"
    assert callable(knowledge.embed) and hasattr(knowledge, "SCHEMA")
    # the introspection the old suite performs still lands on real source
    import inspect
    assert 'r["backend"] == backend' in inspect.getsource(knowledge.recall)


# --- the Atlas probe: honest about which world it is in ----------------------

def test_the_module_probe_asks_the_real_service_in_url_mode(fresh_db, monkeypatch):
    """The knowledge card's heartbeat used to be "a table exists here". With the
    store extracted that claim would be a lie — in URL mode the beat is the
    service's own /health, the same endpoint process-compose probes."""
    from app import modgraph_health as mh
    monkeypatch.setenv("KNOWLEDGE_URL", "http://knowledge.drill")

    calls = []

    def _fake_get(url, **kw):
        calls.append(url)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _fake_get)
    assert mh._probe_knowledge() is True
    assert calls == ["http://knowledge.drill/health"]

    def _unhealthy(url, **kw):
        return httpx.Response(200, json={"ok": False}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _unhealthy)
    assert mh._probe_knowledge() is False, "a service answering ok:false is not a beat"

    def _dead(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", _dead)
    assert mh._probe_knowledge() is False, "a raising probe is a failed beat, not a crash"


def test_the_module_probe_falls_back_to_the_table_without_the_url(fresh_db):
    """And in fallback mode it is the old claim, still true: the table is there
    and the embedder is callable."""
    from app import modgraph_health as mh
    assert not os.environ.get("KNOWLEDGE_URL")
    assert mh._probe_knowledge() is True


def test_gen_fleet_wires_url_mode_and_the_gateway_by_construction(tmp_path):
    """The wiring claim of commit A, proven against a temp root (never the real
    data/): the conductor's env names the knowledge peer — so a fleet boot is in
    URL mode by construction — and the topology registers knowledge for /svc."""
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
