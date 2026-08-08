"""Shared fixtures for the devteam e2e suite.

The tests exercise the platform's real code paths — routes, auth, orchestration
logic — against an isolated temp database, WITHOUT spawning real agent sessions
(no token spend). The manager and worker launch paths are monkeypatched to
no-ops so route side effects that would start an agent are safe to trigger.
"""

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
           # not reach a real 8881/8882/8883 (or fail because nothing is
           # listening there).
           "KNOWLEDGE_URL", "USAGE_URL", "NOTIFY_URL"):
    os.environ.pop(_k, None)

# --- the fleet services, mounted in-process -----------------------------------
#
# Since P1 (knowledge) and P2 (usage, notify) the conductor has no in-process
# store, meter or notifier: recall, note and report_error are HTTP calls. So the
# suite MOUNTS each service — the real app, its own temp database — and points
# the shim's httpx client at it. No sockets, no fleet, nothing to start: the
# offline invariant holds, and every conductor test that recalls, meters or
# reports exercises the real client path against the real service instead of a
# mock that can drift from it.
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
_KNOWLEDGE_URL = "http://knowledge.test"
_USAGE_URL = "http://usage.test"
_NOTIFY_URL = "http://notify.test"
os.environ["KNOWLEDGE_URL"] = _KNOWLEDGE_URL
os.environ["USAGE_URL"] = _USAGE_URL
os.environ["NOTIFY_URL"] = _NOTIFY_URL


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
    """
    service_dir = REPO / "services" / name
    keys = ("DB_PATH", "SERVICE_TOKEN", "SERVICE_NAME", "LEGACY_DB_PATH",
            "CONDUCTOR_URL", "GITHUB_TOKEN", "NOTIFY_GITHUB", "NOTIFY_MAX_PER_HOUR")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["DB_PATH"] = str(Path(_TMP) / f"{name}-service.db")
    os.environ["SERVICE_TOKEN"] = token
    os.environ["SERVICE_NAME"] = name
    # no legacy db to copy from: each first-boot backfill is that service's own
    # test's subject, not a side effect every conductor test pays for
    os.environ["LEGACY_DB_PATH"] = str(Path(_TMP) / "absent-legacy.db")
    for k, v in extra_env.items():
        os.environ[k] = v
    prior_helpers = sys.modules.pop("helpers", None)
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
        sys.modules.pop("helpers", None)
        if prior_helpers is not None:
            sys.modules["helpers"] = prior_helpers
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

from app import auth, db  # noqa: E402
from app import knowledge as _knowledge  # noqa: E402
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
    # The mounted services outlive any one test's database, so empty their stores
    # here too — a test that recalls must see only what it remembered, and a test
    # that meters must not inherit the previous test's spend.
    for svc, tables in ((knowledge_service, ("knowledge",)),
                        (usage_service, ("usage_rows",)),
                        (notify_service, ("notify_seen", "notify_sent"))):
        for table in tables:
            svc.helpers.db().execute(f"DELETE FROM {table}")
        svc.helpers.db().commit()
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
