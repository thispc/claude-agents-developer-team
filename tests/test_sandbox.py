"""The sandbox: a candidate build of this platform, running beside the live one.

The whole point is that it CANNOT do anything real. Most of these tests are
about that guarantee, not about the happy path — a sandbox that quietly spends a
run or pushes to GitHub would be worse than having no sandbox at all.
"""

import json
import os
from pathlib import Path

import pytest

from conftest import make_project, make_task
from app import config, db, demo, sandbox

REPO = Path(__file__).resolve().parent.parent


# ---- isolation: the guarantees that make it safe --------------------------

def test_every_secret_is_blanked_not_omitted(monkeypatch):
    """The child inherits os.environ, so an *unset* variable is the operator's
    real one. That distinction already caused one credential leak here."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_real")
    monkeypatch.setenv("GEMINI_KEY", "AIza-real")
    env = sandbox._child_env(8701)
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN",
              "GEMINI_API_KEY", "GEMINI_KEY", "OPENAI_API_KEY"):
        assert env[k] == "", f"{k} leaked into the sandbox"


def test_the_sandbox_gets_its_own_database(monkeypatch):
    env = sandbox._child_env(8701)
    assert env["DB_PATH"] == str(sandbox.DB)
    assert env["DB_PATH"] != config.DB_PATH, "sandbox would share the live database"


def test_the_sandbox_runs_in_demo_mode():
    assert sandbox._child_env(8701)["DEMO_MODE"] == "1"


def test_the_sandbox_cannot_reach_the_operators_cli_login():
    env = sandbox._child_env(8701)
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(sandbox.SANDBOX_DIR))
    assert env["WORKSPACES_DIR"].startswith(str(sandbox.SANDBOX_DIR))


def test_a_hostile_ref_is_refused():
    for bad in ("main; rm -rf /", "--upload-pack=evil", "$(whoami)", "a b"):
        assert sandbox.start(bad)["ok"] is False


def test_stop_is_safe_when_nothing_is_running(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "PID_FILE", tmp_path / "none.json")
    monkeypatch.setattr(sandbox, "TREE", tmp_path / "no-tree")
    assert sandbox.stop()["ok"] is True


def test_status_reports_a_dead_sandbox_rather_than_claiming_it_runs(monkeypatch, tmp_path):
    pf = tmp_path / "s.json"
    pf.write_text(json.dumps({"pid": 999999, "port": 8701, "ref": "x"}))
    monkeypatch.setattr(sandbox, "PID_FILE", pf)
    st = sandbox.status()
    assert st["running"] is False and st["died"] is True


def test_it_never_switches_the_live_tree(monkeypatch):
    """A checkout would leave the running platform on another commit if anything
    went wrong mid-way; a worktree cannot."""
    src = (REPO / "conductor" / "app" / "sandbox.py").read_text()
    assert "worktree" in src
    assert '"git", "checkout"' not in src


# ---- the mock engine: a sandbox must not be able to spend anything --------

def test_demo_mode_is_off_by_default():
    assert config.DEMO_MODE is False, "the live conductor must never simulate"


@pytest.mark.asyncio
async def test_dispatch_is_intercepted_before_any_agent_starts(fresh_db, monkeypatch):
    from app import launcher
    monkeypatch.setattr(config, "DEMO_MODE", True)

    def _boom(*a, **k):
        raise AssertionError("a real worker was launched inside the sandbox")
    monkeypatch.setattr(launcher, "owner_credentials", _boom)
    monkeypatch.setattr(launcher, "_worker_env", _boom)

    p = make_project(owner_id=1)
    t = make_task(p)
    out = await launcher.dispatch_task(t)
    assert "simulated" in out
    assert db.get_task(t)["status"] == "running"


@pytest.mark.asyncio
async def test_the_manager_session_never_starts_in_a_sandbox(fresh_db, monkeypatch):
    from app import manager
    monkeypatch.setattr(config, "DEMO_MODE", True)

    async def _boom(*a, **k):
        raise AssertionError("a real manager session started in the sandbox")
        yield
    monkeypatch.setattr(manager, "query", _boom)
    p = make_project(owner_id=1)
    await manager.run_manager(p)          # must simply return
    kinds = [e["kind"] for e in db.list_events(p)]
    assert "agent_status" in kinds


def test_a_run_is_not_charged_for_a_simulated_task(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    import asyncio
    from app import launcher
    p = make_project(owner_id=1)
    before = db.get_project(p)["runs_used"]
    t = make_task(p)
    asyncio.run(launcher.dispatch_task(t))
    assert db.get_project(p)["runs_used"] == before


# ---- the seed: an empty sandbox has no screens worth checking -------------

def test_seeding_does_nothing_unless_demo_mode_is_on(fresh_db):
    assert demo.seed() is None
    assert demo.is_seeded() is False


def test_the_seed_covers_the_states_the_ui_has_to_render(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    pid = demo.seed()
    tasks = db.list_tasks(pid)
    states = {t["status"] for t in tasks}
    assert {"done", "running", "planned", "failed"} <= states, states
    p = db.get_project(pid)
    assert p["sprints"] > 1 and p["sprint"] > 1, "sprint archive would be empty"
    # a failed task with real failure detail, so the evidence block renders
    failed = [t for t in tasks if t["status"] == "failed"][0]
    v = json.loads(failed["verification"])
    assert v["ran"] and not v["ok"] and v["failures"]


def test_seeding_twice_does_not_duplicate(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    assert demo.seed() == demo.seed()
    assert len([p for p in db.list_projects() if p["name"] == demo.DEMO_PROJECT]) == 1


def test_the_seeded_project_has_dependencies_to_draw(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    pid = demo.seed()
    assert any(json.loads(t["deps"] or "[]") for t in db.list_tasks(pid))


# ---- routes ---------------------------------------------------------------

def test_sandbox_routes_are_not_open_to_everyone(client, make_user, fresh_db, monkeypatch):
    monkeypatch.setattr(config, "SELFREPAIR_USERS", [])
    _uid, c2 = make_user("mallory")
    assert c2.get("/api/self/sandbox").status_code == 403
    assert c2.post("/api/self/sandbox", json={"ref": "main"}).status_code == 403
    assert c2.delete("/api/self/sandbox").status_code == 403


def test_sandbox_status_lists_branches_to_try(root_client, fresh_db):
    d = root_client.get("/api/self/sandbox").json()
    assert "running" in d and isinstance(d["branches"], list)


def test_a_failed_boot_surfaces_the_reason(root_client, fresh_db, monkeypatch):
    monkeypatch.setattr(sandbox, "start",
                        lambda ref: {"ok": False, "error": "could not check out"})
    r = root_client.post("/api/self/sandbox", json={"ref": "nope"})
    assert r.status_code == 400 and "could not check out" in r.json()["detail"]


def test_home_is_redirected_so_an_old_branch_cannot_find_the_keychain():
    """The candidate may be a commit that predates DEMO_MODE and honours none of
    the in-app guards, so isolation has to hold from the parent's side alone.
    config._has_cli_login() probes ~/.claude and the macOS keychain via HOME."""
    env = sandbox._child_env(8701)
    assert env["HOME"].startswith(str(sandbox.SANDBOX_DIR))
    assert env["HOME"] != os.path.expanduser("~")


def test_isolation_does_not_rely_on_the_candidates_own_code():
    """Every guarantee must be enforced by the environment we hand the child, not
    by code inside it — that code is exactly what is under test."""
    env = sandbox._child_env(8701)
    parent_enforced = ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN",
                       "HOME", "DB_PATH", "WORKSPACES_DIR"]
    for k in parent_enforced:
        assert k in env, f"{k} left to the candidate to get right"
