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
           "GITHUB_REPO", "REQUIRE_STAGING", "CUSTOM_MODEL_ENDPOINTS"):
    os.environ.pop(_k, None)

from app import auth, db  # noqa: E402


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
