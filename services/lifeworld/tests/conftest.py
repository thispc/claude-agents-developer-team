"""Test env for the lifeworld service, set BEFORE app/helpers latch their config.

Offline by construction: a temp database, a known token, no legacy db, no sockets
and no fleet — the suite drives the ASGI app in-process, and every outward door
(the model door, the tuning knob, the agent register, the knowledge store) is
answered by a MockTransport that behaves like the real thing without one.

THREE departures from the template's conftest, all because this suite must also
run INSIDE the full repo run (pytest.ini's testpaths include services/):

  1. The service is loaded under UNIQUE module names via importlib, because the
     conductor's `app` package is already in sys.modules when tests/ collected
     first — a plain `from app import app` would hit the wrong one.

  2. Every env var this file touches is SAVED and RESTORED after the load,
     because helpers/app read the environment at import time and leaking DB_PATH
     across suites is how one suite's fixture deletes another suite's database.

  3. The service's OWN modules are evicted from sys.modules around the load. This
     service is the only one with internal modules (`store`, `crew`, `manifest`,
     `caller`), and the conductor's suite mounts the same service under its own
     harness — without the eviction the second load would silently reuse the
     first's `store`, which is bound to the first's `helpers` and therefore to
     the first's DATABASE. The two suites would share a store and the failure
     would look like a flaky test rather than what it is.

     `substrate` is deliberately NOT evicted. It holds no state (it is the
     engine), its modules import each other lazily by relative name, and a
     half-evicted package would fail those imports three frames from the cause.
     The one thing in it that IS mutable is `ports`' client config — so both
     suites set that in a FIXTURE, per test, rather than at import: whichever
     conftest was imported last would otherwise decide where the other one's
     model door points.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

SERVICE_DIR = Path(__file__).resolve().parent.parent
_TMP = tempfile.mkdtemp(prefix="lifeworld-svc-test-")

TOKEN = "test-service-token"
# What the conductor's model door would resolve; here the reference is opaque and
# the mock answers whatever it is handed, which is exactly the point — nothing in
# this process can turn one into a key.
SETTINGS_REF = "user:1.testsignature"

_OWNED = ("store", "crew", "manifest", "caller", "app", "helpers")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _evict() -> dict:
    """Drop this service's own modules so the load below is genuinely fresh.
    Returns them, to be restored afterwards. `substrate` stays — see the header."""
    return {name: sys.modules.pop(name) for name in list(sys.modules) if name in _OWNED}


_ENV = {k: os.environ.get(k)
        for k in ("DB_PATH", "SERVICE_TOKEN", "SERVICE_NAME", "LEGACY_DB_PATH",
                  "CONDUCTOR_URL", "KNOWLEDGE_URL", "KNOWLEDGE_TOKEN")}
os.environ["DB_PATH"] = str(Path(_TMP) / "lifeworld-test.db")
os.environ["SERVICE_TOKEN"] = TOKEN
os.environ["SERVICE_NAME"] = "lifeworld"
os.environ["LEGACY_DB_PATH"] = str(Path(_TMP) / "absent-legacy.db")   # no first-boot copy
os.environ["CONDUCTOR_URL"] = "http://conductor.test"
os.environ["KNOWLEDGE_URL"] = "http://knowledge.test"
os.environ["KNOWLEDGE_TOKEN"] = "test-knowledge-token"

_SAVED_MODULES = _evict()
# app.py puts its own directory first on sys.path (so `uvicorn app:app` works from
# any cwd); left there it would shadow the conductor's `app` package for whatever
# runs next in a whole-repo pytest run.
_SAVED_PATH = list(sys.path)
try:
    helpers = _load("lifeworld_service_helpers", SERVICE_DIR / "helpers.py")
    sys.modules["helpers"] = helpers        # app.py's own `import helpers`
    sys.path.insert(0, str(SERVICE_DIR))
    svc = _load("lifeworld_service_app", SERVICE_DIR / "app.py")
    ports = svc.ports
finally:
    sys.path[:] = _SAVED_PATH
    for _n in _OWNED:
        sys.modules.pop(_n, None)
    sys.modules.update(_SAVED_MODULES)
    for _k, _v in _ENV.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v


# --- the four outward doors, answered without a socket -----------------------

MODEL_CALLS: list[dict] = []
REGISTER: dict[str, dict] = {}


async def _model_door(request: httpx.Request) -> httpx.Response:
    """The conductor's POST /internal/complete. It records what it was sent, which
    is how the smoke test proves a settings REFERENCE crossed and a key did not."""
    body = json.loads(request.content or b"{}")
    MODEL_CALLS.append(body)
    if not body.get("settings_ref"):
        return httpx.Response(403, json={"detail": "no settings reference"})
    return httpx.Response(200, json={"text": "1. Be concise.\n2. Cite the file."})


def _sync_door(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/internal/tuning":
        name = request.url.params.get("name", "")
        return httpx.Response(200, json={"name": name, "value": {
            "scene_default_model": "claude-haiku-4-5",
            "scene_utterance_max_tokens": 200,
            "agent_session_cap": 30,
            "agent_session_window_s": 18000}.get(name)})
    if path == "/internal/agents/note":
        body = json.loads(request.content or b"{}")
        REGISTER[body["key"]] = body
        return httpx.Response(200, json={"state": body.get("state", "idle")})
    if path.startswith("/internal/agents/"):
        key = path[len("/internal/agents/"):]
        row = REGISTER.get(key) or {}
        return httpx.Response(200, json={"state": row.get("state", "idle"),
                                         "busy": row.get("state") not in (None, "idle"),
                                         "what": row.get("what", ""), "for_s": 0})
    return httpx.Response(404, json={"detail": f"no such door: {path}"})


def _knowledge_door(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/tokens":
        body = json.loads(request.content or b"{}")
        return httpx.Response(200, json={"tokens": str(body.get("text", "")).lower().split()})
    return httpx.Response(200, json={"hits": [], "id": 0})


def wire_doors() -> None:
    """Point the substrate's four outward clients at this suite's mocks.

    Called per test, not at import: `substrate.ports` is shared with whatever else
    mounted this service in the same interpreter (the conductor's suite does), and
    a module attribute set at import time is decided by collection order rather
    than by which test is running.
    """
    ports.CONDUCTOR_URL = "http://conductor.test"
    ports.KNOWLEDGE_URL = "http://knowledge.test"
    ports.KNOWLEDGE_TOKEN = "test-knowledge-token"
    ports.SERVICE_TOKEN = TOKEN
    ports.TRANSPORT = httpx.MockTransport(_model_door)
    ports.SYNC_TRANSPORT = httpx.MockTransport(_sync_door)
    ports.KNOWLEDGE_TRANSPORT = httpx.MockTransport(_knowledge_door)
    ports.SYNC_KNOWLEDGE_TRANSPORT = httpx.MockTransport(_knowledge_door)
    # The service's real 30s cache. Left at 0 (read-through) every `Human.usage()`
    # on every room view becomes a door round trip, and the contract fuzzer's few
    # hundred requests turn into tens of thousands — a gate slow enough that nobody
    # runs it is a gate that does not exist.
    ports.KNOB_TTL = 30.0
    ports._KNOBS.clear()


wire_doors()


@pytest.fixture(autouse=True)
def _doors():
    wire_doors()
    yield


@pytest.fixture()
def clean_store():
    """An empty worlds table for one test — cheaper than a fresh file, and the
    schema ran once at load, exactly like a real boot."""
    svc.helpers.db().execute("DELETE FROM lw_worlds")
    svc.helpers.db().commit()
    MODEL_CALLS.clear()
    REGISTER.clear()
    yield svc
