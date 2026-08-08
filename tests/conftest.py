"""Shared fixtures for the devteam e2e suite.

The tests exercise the platform's real code paths — routes, auth, orchestration
logic — against an isolated temp database, WITHOUT spawning real agent sessions
(no token spend). The manager and worker launch paths are monkeypatched to
no-ops so route side effects that would start an agent are safe to trigger.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "conductor"))

# A fresh temp DB per test session, set BEFORE importing app config, so the real
# devteam.db is never touched.
_TMP = tempfile.mkdtemp(prefix="devteam-test-")
os.environ["DB_PATH"] = str(Path(_TMP) / "test.db")
os.environ["ROOT_USERNAME"] = "root"
os.environ["ROOT_PASSWORD"] = "testpass"
os.environ["WORKER_TOKEN"] = "test-worker-token"
os.environ["WORKSPACES_DIR"] = str(Path(_TMP) / "workspaces")
# Blank EVERY credential, not just the Anthropic key. Sourcing .env into the
# shell before running pytest made the suite behave differently — triage would
# reach a real provider and classify a vague test fixture as "substantial",
# failing an unrelated assertion. A test whose outcome depends on what happens to
# be exported is not a test.
#
# The second group is deployment IDENTITY rather than credentials, and it caught
# us the same way. Running this suite inside the staging pod failed five sprint
# tests and a branch-naming test that pass on a laptop — not because the code
# differed, but because staging sets BRANCH_PREFIX and a developer machine has a
# `claude` CLI login. Both made tests pass or fail for reasons the tests were not
# about. Anything below that changes behaviour has to be pinned here, or the
# suite means something different in every environment that runs it.
for _k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY",
           "GEMINI_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "DOCR_READ_TOKEN",
           "DOCR_REGISTRY", "DIGITALOCEAN_API_TOKEN", "AUTO_UPDATE", "DEMO_MODE",
           "BRANCH_PREFIX", "ALLOW_MERGE", "DEVTEAM_ENV", "SELF_REPO",
           "PROTECTED_REPOS",
           "GITHUB_REPO", "REQUIRE_STAGING", "CUSTOM_MODEL_ENDPOINTS",
           # Pinned below to MOUNTED services, never to whatever a shell that
           # sourced data/env/conductor.env happens to point at — the suite must
           # not reach a real 8881/8882/8883/8884 (or fail because nothing is
           # listening there).
           "KNOWLEDGE_URL", "USAGE_URL", "NOTIFY_URL", "WATCH_URL",
           "LIFEWORLD_URL", "MODGRAPH_URL"):
    os.environ.pop(_k, None)

# --- the fleet services, mounted in-process -----------------------------------
#
# Since P1 (knowledge), P2 (usage, notify) and P3 (watch) the conductor has no
# in-process store, meter, notifier or log ring: recall, note, report_error and
# every log row are HTTP calls. So the suite MOUNTS each service — the real app,
# its own temp database — and points the shim's httpx client at it. No sockets,
# no fleet, nothing to start: the offline invariant holds, and every conductor
# test that recalls, meters, reports or LOGS exercises the real client path
# against the real service instead of a mock that can drift from it.
#
# Mounting matters more than it sounds. Leaving a *_URL set with nothing
# listening would not fail the suite — every verb would degrade, silently, and
# the tests would pass against a platform that had lost its memory and its meter.
# The services are loaded under unique module names because the conductor's own
# `app` package owns that name, and their env is saved/restored around each load
# so no harness inherits another's DB_PATH.

KNOWLEDGE_TEST_TOKEN = "conductor-suite-knowledge-token"
USAGE_TEST_TOKEN = "conductor-suite-usage-token"
NOTIFY_TEST_TOKEN = "conductor-suite-notify-token"
WATCH_TEST_TOKEN = "conductor-suite-watch-token"
LIFEWORLD_TEST_TOKEN = "conductor-suite-lifeworld-token"
MODGRAPH_TEST_TOKEN = "conductor-suite-modgraph-token"
_KNOWLEDGE_URL = "http://knowledge.test"
_LIFEWORLD_URL = "http://lifeworld.test"
_USAGE_URL = "http://usage.test"
_NOTIFY_URL = "http://notify.test"
_WATCH_URL = "http://watch.test"
_MODGRAPH_URL = "http://modgraph.test"
os.environ["KNOWLEDGE_URL"] = _KNOWLEDGE_URL
os.environ["USAGE_URL"] = _USAGE_URL
os.environ["NOTIFY_URL"] = _NOTIFY_URL
os.environ["MODGRAPH_URL"] = _MODGRAPH_URL
# Set BEFORE `from app import ...` below: the logs and monitor shims decide their
# mode at import (URL set = client, unset = the vendored pre-P3 body), and a
# process boots into one world and stays there.
os.environ["WATCH_URL"] = _WATCH_URL
os.environ["LIFEWORLD_URL"] = _LIFEWORLD_URL


def _load_module(name: str, path: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mount_service(name: str, token: str, **extra_env):
    """Load one service's app with a temp store and a known token.

    Every env var touched is saved and restored: helpers.py and app.py read the
    environment at import, and leaking DB_PATH out of here is how one harness
    deletes another harness's database.

    THE SAME GOES FOR MODULE NAMES. A service is launched as `uvicorn app:app
    --app-dir services/<name>`, so its own files are TOP-LEVEL modules — `store`,
    `derive`, `seed`, `helpers`. Two multi-file services in one interpreter
    therefore collide on any name they share, and the collision is silent: the
    lifeworld mounts first, so `import store` inside the modgraph service's app.py
    handed it the LIFEWORLD's store and every table it wanted was missing. So each
    load owns those names for the length of the load and gives them back after —
    exactly what was already being done for `helpers`, generalised to every file
    in the service directory. The loaded modules keep direct references to each
    other, which is why the service's own code imports at module level and never
    inside a function.
    """
    service_dir = REPO / "services" / name
    keys = ("DB_PATH", "SERVICE_TOKEN", "SERVICE_NAME", "LEGACY_DB_PATH",
            "CONDUCTOR_URL", "GITHUB_TOKEN", "NOTIFY_GITHUB", "NOTIFY_MAX_PER_HOUR",
            "REPO_ROOT")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["DB_PATH"] = str(Path(_TMP) / f"{name}-service.db")
    os.environ["SERVICE_TOKEN"] = token
    os.environ["SERVICE_NAME"] = name
    # no legacy db to copy from: each first-boot backfill is that service's own
    # test's subject, not a side effect every conductor test pays for
    os.environ["LEGACY_DB_PATH"] = str(Path(_TMP) / "absent-legacy.db")
    for k, v in extra_env.items():
        os.environ[k] = v
    own = sorted(p.stem for p in service_dir.glob("*.py"))
    prior_mods = {m: sys.modules.pop(m, None) for m in own}
    # app.py puts its own directory FIRST on sys.path so `uvicorn app:app` works
    # from any cwd. Left there, `from app import auth` in this very file would
    # resolve to the SERVICE's app.py instead of the conductor package — so the
    # path is restored the moment the load is done.
    prior_path = list(sys.path)
    try:
        helpers = _load_module(f"{name}_svc_helpers", service_dir / "helpers.py")
        sys.modules["helpers"] = helpers        # app.py's own `import helpers`
        return _load_module(f"{name}_svc_app", service_dir / "app.py")
    finally:
        sys.path[:] = prior_path
        for _m in own:
            sys.modules.pop(_m, None)
        for _m, _mod in prior_mods.items():
            if _mod is not None:
                sys.modules[_m] = _mod
        for _k, _v in saved.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v


knowledge_service = _mount_service("knowledge", KNOWLEDGE_TEST_TOKEN)
usage_service = _mount_service("usage", USAGE_TEST_TOKEN)
# A token that makes gh_enabled() true, so the drills reach the (monkeypatched)
# GitHub call instead of stopping at "no repo configured" — the real credential
# is blanked above with every other one.
notify_service = _mount_service("notify", NOTIFY_TEST_TOKEN,
                                GITHUB_TOKEN="test-github-token-not-real")
watch_service = _mount_service("watch", WATCH_TEST_TOKEN)
# Since the P4 cutover the conductor has no substrate at all: every /api/lw path is
# a proxy, and the self-repair crew's seating, deliberations, consults and reviews
# are HTTP calls. So the suite mounts this one like the rest — and unlike the rest it
# also needs its OUTWARD doors answered (a model, two knobs, the agent register, the
# knowledge store), because a lifeworld that cannot reach a model is not the thing
# any of these tests are about. Those are wired below.
lifeworld_service = _mount_service("lifeworld", LIFEWORLD_TEST_TOKEN)
# The module graph's six tables (P5). REPO_ROOT is pinned at the real checkout on
# purpose: the seed's whole job is to describe the tree that is actually there, and
# services/modgraph's own suite holds every claim in it against those files. The
# conductor mounts it because since the cutover /api/graph/* has nothing else to
# read — the payload, the verify runner and the engine's build hooks are all
# clients of this app.
modgraph_service = _mount_service("modgraph", MODGRAPH_TEST_TOKEN, REPO_ROOT=str(REPO))

from app import auth, db  # noqa: E402
from app import knowledge as _knowledge  # noqa: E402
from app import lifeworld_client as _lifeworld  # noqa: E402
from app import logs as _logs  # noqa: E402
from app import modgraph as _modgraph  # noqa: E402
from app import monitor as _monitor  # noqa: E402
from app import notify as _notify  # noqa: E402
from app import usage as _usage  # noqa: E402

import httpx  # noqa: E402
from starlette.testclient import TestClient as _TestClient  # noqa: E402


def _svc_client(service, base_url, token):
    """A fresh client per call, matching each shim's own client-per-call shape —
    one shared instance would be closed by the first `with` block."""
    c = _TestClient(service.app, base_url=base_url)
    c.headers["X-Service-Token"] = token
    return c


_knowledge._TRANSPORT = httpx.ASGITransport(app=knowledge_service.app)
_knowledge._TOKEN = KNOWLEDGE_TEST_TOKEN
_knowledge._sync_client = lambda: _svc_client(knowledge_service, _KNOWLEDGE_URL,
                                              KNOWLEDGE_TEST_TOKEN)

_usage._TOKEN = USAGE_TEST_TOKEN
_usage._client = lambda: _svc_client(usage_service, _USAGE_URL, USAGE_TEST_TOKEN)

_notify._TRANSPORT = httpx.ASGITransport(app=notify_service.app)
_notify._TOKEN = NOTIFY_TEST_TOKEN
_notify._sync_client = lambda: _svc_client(notify_service, _NOTIFY_URL, NOTIFY_TEST_TOKEN)

_lifeworld._TOKEN = LIFEWORLD_TEST_TOKEN
_lifeworld._TRANSPORT = httpx.ASGITransport(app=lifeworld_service.app)
# The sync half (repair.ensure_team, note_decision, team_usage, the module graph's
# assignment pool) — a factory, not one instance, matching the shim's client-per-call
# shape. `*a` because production passes a timeout.
_lifeworld._sync_client = lambda *_a, **_k: _svc_client(
    lifeworld_service, _LIFEWORLD_URL, LIFEWORLD_TEST_TOKEN)

_modgraph._TOKEN = MODGRAPH_TEST_TOKEN
# The shim is sync all the way down (every caller is a plain function), so one
# factory covers it. `*a` because production passes a timeout.
_modgraph._client = lambda *_a, **_k: _svc_client(modgraph_service, _MODGRAPH_URL,
                                                  MODGRAPH_TEST_TOKEN)


def graph_rows(sql: str, params: tuple = ()) -> list[dict]:
    """Rows straight out of the modgraph SERVICE's own database.

    A handful of conductor-side drills assert about the ROWS rather than about the
    answer — that a verify's verdict really landed, that an advisory result touched
    nothing else. Since the cutover those rows are another process's, so the drills
    read them where they are; asking through the client would only prove the client
    agrees with itself."""
    return [dict(r) for r in
            modgraph_service.helpers.db().execute(sql, params).fetchall()]

_logs._TOKEN = WATCH_TEST_TOKEN
_logs._client = lambda: _svc_client(watch_service, _WATCH_URL, WATCH_TEST_TOKEN)
_monitor._TOKEN = WATCH_TEST_TOKEN
_monitor._client = lambda: _svc_client(watch_service, _WATCH_URL, WATCH_TEST_TOKEN)
# The log shim batches rows onto a queue that a DAEMON THREAD posts. Here the
# thread never starts, so every row is posted on the calling test's own thread
# inside logs.flush() — which each read verb calls first anyway, so a test still
# reads its own writes. A background thread racing `fresh_db` as it empties the
# service's tables is a flake, not a test; the thread, its 500ms timer and its
# 100-row trigger are drilled deliberately in tests/test_watch_service.py.
_logs.AUTOFLUSH = False


# The usage service reads the owner's dials through the conductor's
# GET /internal/tuning. Here that hop is answered IN-PROCESS by the conductor's
# own tuning module: the service still runs its real client code — request,
# JSON, cache, stale-value fallback — only the socket is replaced. Driving the
# conductor's ASGI app from inside a service handler that is itself being driven
# by the conductor's TestClient would mean two nested portals on one call stack,
# and a deadlock is not a fixture. The REAL door (auth, the door allowlist, the
# knob allowlist) is drilled against the running conductor app in
# tests/test_usage_service.py, which is where it belongs.
def _tuning_answer(request: httpx.Request) -> httpx.Response:
    from app import tuning
    name = request.url.params.get("name", "")
    return httpx.Response(200, json={"name": name, "value": tuning.get(name)})


usage_service.TUNING_TRANSPORT = httpx.MockTransport(_tuning_answer)
# 0: a test that sets a knob and immediately asks for a snapshot must see it. The
# service's real 30s cache is exercised in services/usage/tests.
usage_service.KNOB_TTL = 0.0


# --- the lifeworld service's four doors, answered in-process ------------------
#
# The lifeworld reaches UP for more than any other service: a model (the door
# that keeps credentials in the conductor), two of the owner's knobs, the agent
# activity register, and the knowledge store. Each hop is answered here by the
# conductor's own module, so the service runs its REAL client code — request,
# JSON, cache, degraded fallback — with only the socket replaced. Driving the
# conductor's ASGI app from inside a service handler that is itself being driven
# by the conductor's TestClient would mean two nested portals on one call stack,
# and a deadlock is not a fixture. The REAL doors (auth, the door allowlist, the
# knob allowlist, a forged settings reference) are drilled against the running
# conductor app in tests/test_lifeworld_service.py, which is where they belong.

def _lw_sync_door(request: httpx.Request) -> httpx.Response:
    """The conductor's SYNC doors: one tuning knob, and the activity register."""
    path = request.url.path
    if path == "/internal/tuning":
        from app import tuning
        name = request.url.params.get("name", "")
        return httpx.Response(200, json={"name": name, "value": tuning.get(name)})
    if path == "/internal/agents/note":
        from app import agents as reg
        body = json.loads(request.content or b"{}")
        return httpx.Response(200, json=reg.note(body["key"], body.get("state", "idle"),
                                                 body.get("what", ""),
                                                 **(body.get("fields") or {})))
    if path.startswith("/internal/agents/"):
        from app import agents as reg
        return httpx.Response(200, json=reg.get(path[len("/internal/agents/"):]))
    return httpx.Response(404, json={"detail": f"no such door: {path}"})


async def _lw_model_door(request: httpx.Request) -> httpx.Response:
    """THE MODEL DOOR, minus the socket. The settings REFERENCE is resolved with the
    real signature check, so a test that forges one gets the same 403 the door gives —
    and providers.complete is the same function every other spender calls, which is
    what lets a test monkeypatch it once and have the substrate see the fake."""
    from app import auth, providers
    body = json.loads(request.content or b"{}")
    settings = auth.resolve_settings_ref(body.get("settings_ref", ""))
    if settings is None:
        return httpx.Response(403, json={"detail": "the settings reference did not resolve"})
    try:
        text = await providers.complete(
            body.get("provider") or "anthropic", body.get("model", ""),
            body.get("system", ""), body.get("prompt", ""), settings,
            max_tokens=int(body.get("max_tokens") or 800),
            source=body.get("source") or "lifeworld")
    except Exception as e:
        return httpx.Response(502, json={"detail": str(e)[:400]})
    return httpx.Response(200, json={"text": text})


def _lw_knowledge_tokens(request: httpx.Request) -> httpx.Response:
    """knowledge's tokenizer, sync — the leak check runs inside the host's line pass."""
    body = json.loads(request.content or b"{}")
    return httpx.Response(200, json={"tokens": knowledge_service._tokens(body.get("text", ""))})


def wire_lifeworld_ports() -> None:
    """Point the substrate's outward clients at this suite's answers.

    Called per test (below), not once at import: `substrate.ports` is module state
    shared with any other harness that mounted this service in the same
    interpreter — and services/lifeworld/tests/conftest.py is exactly that, imported
    during the SAME collection pass. Whichever conftest ran last would otherwise
    decide where the other one's model door points, and the symptom is a drill
    that fails only in a whole-repo run.
    """
    ports = lifeworld_service.ports
    ports.CONDUCTOR_URL = "http://conductor.test"
    ports.KNOWLEDGE_URL = _KNOWLEDGE_URL
    ports.KNOWLEDGE_TOKEN = KNOWLEDGE_TEST_TOKEN
    ports.SERVICE_TOKEN = LIFEWORLD_TEST_TOKEN
    ports.TRANSPORT = httpx.MockTransport(_lw_model_door)
    ports.SYNC_TRANSPORT = httpx.MockTransport(_lw_sync_door)
    ports.KNOWLEDGE_TRANSPORT = httpx.ASGITransport(app=knowledge_service.app)
    ports.SYNC_KNOWLEDGE_TRANSPORT = httpx.MockTransport(_lw_knowledge_tokens)
    # 0: a test that sets scene_default_model and immediately runs a round must see it.
    ports.KNOB_TTL = 0.0
    ports._KNOBS.clear()


wire_lifeworld_ports()


@pytest.fixture(autouse=True)
def _lifeworld_doors():
    wire_lifeworld_ports()
    yield


def spawn_lifeworld_service(tmp: str, conductor_url: str):
    """Start a REAL lifeworld service as a child, for the two suites that start a real
    conductor (the canvas end-to-end drills).

    Those suites drive a browser against a `uvicorn app.main:app` subprocess, and since
    the P4 cutover a conductor with no substrate behind it serves a Studio that 503s. The
    other four services are allowed to be absent there — `logs`, `usage` and friends all
    degrade to shapes a canvas test never looks at — but the canvas IS the lifeworld's
    face, so its one required peer starts with it.

    Returns (process, url). The caller must terminate it. Deliberate details:

      LEGACY_DB_PATH points at a file that does not exist, so a throwaway service can
      never copy the OPERATOR's real worlds out of devteam.db into a temp directory.
      CONDUCTOR_URL points at the spawned conductor, so the model door, the tuning knobs
      and the agent register resolve against the same process the browser is talking to.
      The token is the fleet's own minted one, because that is what the conductor's client
      will send — a service with a different one would answer 401 to every proxied call.
    """
    import socket
    import subprocess
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    token = ""
    tok = REPO / "data" / "tokens" / "lifeworld.token"
    if tok.exists():
        token = tok.read_text().strip()
    env = dict(os.environ)
    env.update({"PORT": str(port), "DB_PATH": str(Path(tmp) / "lifeworld.db"),
                "SERVICE_TOKEN": token, "SERVICE_NAME": "lifeworld",
                "LEGACY_DB_PATH": str(Path(tmp) / "absent-legacy.db"),
                "CONDUCTOR_URL": conductor_url})
    log = open(Path(tmp) / "lifeworld.log", "w")
    proc = subprocess.Popen(
        [str(REPO / ".venv/bin/uvicorn"), "app:app", "--app-dir",
         str(REPO / "services" / "lifeworld"), "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    import httpx as _httpx
    for _ in range(120):
        try:
            if _httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return proc, url
        except Exception:
            pass
        time.sleep(0.25)
    proc.terminate()
    raise AssertionError(f"the lifeworld service did not come up on {port}")


@pytest.fixture()
def fresh_db():
    """A clean database for one test. Recreates the schema and reseeds root."""
    dbfile = Path(os.environ["DB_PATH"])
    if db._conn is not None:
        try:
            db._conn.close()
        except Exception:
            pass
        db._conn = None
    if dbfile.exists():
        dbfile.unlink()
    db.init()
    auth.init()
    from app import findings, knowledge, notify, usage
    findings.init()
    # None of the three has a schema of its own any more — each init() drops what
    # its strangler left behind (knowledge's legacy table, usage's and notify's
    # migrated kv keys) and refuses when its service is not configured. Called
    # here so every test starts from the state a real boot produces.
    knowledge.init()
    usage.init()
    notify.init()
    # lifeworld.init() drops lw_worlds once the service says it has settled its
    # first-boot copy. Here there is no legacy db to copy from, so the service
    # settles immediately and the drop is a no-op — the point is that the real
    # conditional path runs, in the order a real boot runs it.
    from app import lifeworld_client, modgraph
    lifeworld_client.init()
    # modgraph.init() drops the six graph_* tables (and the layout kv keys) once
    # the service says it has settled its first-boot copy. Here there is no legacy
    # db to copy from, so the service settles immediately and the drop is a no-op
    # on a fresh database — the point is that the real conditional path runs, in
    # the order a real boot runs it.
    modgraph.init()
    # The mounted services outlive any one test's database, so empty their stores
    # here too — a test that recalls must see only what it remembered, and a test
    # that meters must not inherit the previous test's spend.
    # Anything still queued in the log shim belongs to the PREVIOUS test and would
    # flush into this one's clean ring on its first read. Dropped before the
    # tables are emptied, not after.
    _logs._QUEUE.clear()
    _logs._LAST.clear()
    _logs._degraded_read = False
    # ...and the graph client's, for the same reason: a test that drilled the
    # outage would otherwise leave the next test's payload flagged `degraded`.
    _modgraph._degraded_read = False
    for svc, tables in ((knowledge_service, ("knowledge",)),
                        (usage_service, ("usage_rows",)),
                        (notify_service, ("notify_seen", "notify_sent")),
                        (watch_service, ("log_rows", "error_rows", "decisions")),
                        # Worlds outlive a test's database too, and a stale one is
                        # worse here than anywhere else: the crew's kv record is a
                        # POINTER at a world id, so a leftover world with the right
                        # id would be silently adopted by the next test's crew.
                        (lifeworld_service, ("lw_worlds",)),
                        # A leftover plan is worse than a leftover row: `active_plan`
                        # returns the newest active one whatever database made it, so
                        # one test's seed would silently become the next test's graph.
                        (modgraph_service, ("graph_plans", "graph_nodes", "graph_edges",
                                            "graph_node_runs", "graph_node_tests",
                                            "graph_assign"))):
        for table in tables:
            svc.helpers.db().execute(f"DELETE FROM {table}")
        svc.helpers.db().commit()
    # ...and the layouts, which are kv and keyed by plan id, so a recycled id
    # would inherit somebody else's drag.
    modgraph_service.helpers.db().execute("DELETE FROM kv WHERE key LIKE 'graph:pos:%'")
    modgraph_service.helpers.db().commit()
    # ...and the standing decision, which is not a table row: a suite that
    # inherited `auto` from an earlier test would start approving by itself.
    watch_service.helpers.db().execute("DELETE FROM kv WHERE key = 'auto'")
    watch_service.helpers.db().commit()
    from app import bus
    bus._loop = None          # don't inherit a closed loop from a prior TestClient
    yield db


@pytest.fixture()
def client(fresh_db, monkeypatch):
    """A TestClient with agent-launching side effects stubbed out.

    We patch the manager session, the worker launcher and the deterministic
    scheduler to no-ops so that hitting a route which *would* start an agent
    neither spends tokens nor hangs. The DB/auth/route logic runs for real.
    """
    from starlette.testclient import TestClient

    from app import main as app_main
    from app import manager, scheduler
    from app import launcher as launcher_mod

    async def _noop_manager(project_id):
        return None

    def _noop_ensure(project_id):
        return None

    async def _noop_dispatch(task_id, source="scheduler"):
        return "stubbed: dispatch disabled in tests"

    monkeypatch.setattr(manager, "run_manager", _noop_manager)
    monkeypatch.setattr("app.routes.manager.run_manager", _noop_manager)
    monkeypatch.setattr(scheduler, "ensure", _noop_ensure)
    monkeypatch.setattr(scheduler, "_run", lambda pid: _noop_ensure(pid))
    monkeypatch.setattr(launcher_mod, "dispatch_task", _noop_dispatch)

    with TestClient(app_main.app) as c:
        yield c


def _signup(client, username, password="hunter2pw"):
    r = client.post("/api/signup", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


def login(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


@pytest.fixture()
def root_client(client):
    """A TestClient already logged in as root; carries the session cookie."""
    login(client, "root", "testpass")
    return client


@pytest.fixture()
def root_can_run_agents(fresh_db, monkeypatch):
    """Make root able to start a project, explicitly.

    There are two gates and both were being satisfied by accident. Project
    creation refuses a user with no credentials of their own, AND refuses when
    the conductor itself is unauthenticated. On a developer machine a `claude`
    CLI login satisfies both, so these tests were green for a reason that had
    nothing to do with what they assert — and went red the moment the same suite
    ran inside a container, where no such login exists.

    AUTH_CONFIGURED is a module constant computed at import, so it is patched
    rather than set: by the time a test runs, the environment has already been
    read.
    """
    from app import auth as auth_mod, config as config_mod
    monkeypatch.setattr(config_mod, "AUTH_CONFIGURED", True)
    auth_mod.save_settings(1, {"anthropic_api_key": "sk-ant-test-not-a-real-key"})
    return auth_mod.get_user(1)


@pytest.fixture()
def make_user(client):
    """Factory: create a normal (non-root) user and return their id + a helper
    that returns a *separate* cookie jar logged in as them."""
    from starlette.testclient import TestClient
    from app import main as app_main

    created = {}

    def _make(username):
        # sign up AND log in on a fresh client, so the shared client's session
        # cookie (e.g. root's) is never overwritten.
        c2 = TestClient(app_main.app)
        _signup(c2, username)
        u = auth.get_user_by_name(username)
        created[username] = u["id"]
        return u["id"], c2

    return _make


def make_project(owner_id=1, name="proj", repo="", status="running",
                 team=None, autonomy="supervised"):
    """Insert a project row directly, bypassing the manager start."""
    pid = db.create_project(name, f"brief for {name}", repo, 5.0, 3,
                            max_runs=40, team=team or [], autonomy=autonomy,
                            owner_id=owner_id)
    if status != "planning":
        db.set_project_status(pid, status)
    return pid


def make_task(project_id, role="backend", title="t", desc="d", status="planned",
              deps=None):
    tid = db.create_task(project_id, role, title, desc, deps=deps or [])
    if status != "planned":
        db.update_task(tid, status=status)
    return tid


def clear_logs():
    """Empty the ring the way a test used to empty the kv blob.

    Since P3 `logs.RING_KEY` does not exist: the rows live in the mounted watch
    service, and some of them are still on the shim's queue. Both have to go, in
    that order, or the next read flushes the rows a test just cleared.
    """
    _logs._QUEUE.clear()
    con = watch_service.helpers.db()
    con.execute("DELETE FROM log_rows")
    con.execute("DELETE FROM error_rows")
    con.commit()


def dashboard_js() -> str:
    """All dashboard JS as one string, concatenated in index.html's load order — the
    single source of truth since app.js was split into dashboard/js/*.js. Tests that
    grep 'the dashboard code' read this instead of the old monolith."""
    import re
    dash = Path(__file__).resolve().parent.parent / "dashboard"
    html = (dash / "index.html").read_text()
    srcs = re.findall(r'<script src="(js/[^"]+)"></script>', html)
    assert srcs, "index.html lists no dashboard/js scripts"
    return "\n".join((dash / s).read_text() for s in srcs)
