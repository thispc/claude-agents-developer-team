"""The sandbox: a candidate build of this platform, running beside the live one.

The whole point is that it CANNOT do anything real. Most of these tests are
about that guarantee, not about the happy path — a sandbox that quietly spends a
run or pushes to GitHub would be worse than having no sandbox at all.
"""

import json
import os
from pathlib import Path

import pytest

# These read files from the repository (manifests, workflows) or drive host
# tooling like git and docker. Neither exists inside the shipped image, so they
# verify the REPO rather than the ARTIFACT — staging deselects them.
pytestmark = pytest.mark.hostonly

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
    for bad in ("ref:main; rm -rf /", "ref:--upload-pack=evil", "ref:$(whoami)",
                "ref:a b", "ref:../../etc", "workspace:../../etc", "nonsense:x"):
        assert sandbox.start(bad)["ok"] is False, bad


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
    """The simulated manager is a daemon like the real one, so it is created as a
    task, not awaited — but it must never reach the SDK."""
    import asyncio
    from app import manager
    monkeypatch.setattr(config, "DEMO_MODE", True)

    async def _boom(*a, **k):
        raise AssertionError("a real manager session started in the sandbox")
        yield
    monkeypatch.setattr(manager, "query", _boom)
    p = make_project(owner_id=1)
    task = asyncio.get_running_loop().create_task(manager.run_manager(p))
    await asyncio.sleep(0.15)
    task.cancel()
    assert "agent_status" in [e["kind"] for e in db.list_events(p)]


def test_the_simulated_manager_cannot_run_forever(fresh_db):
    """An unbounded loop in a process nobody is watching is how a sandbox outlives
    the review it was started for."""
    assert 0 < demo.MANAGER_MAX_LIFE <= 12 * 3600


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


def test_sandbox_status_lists_what_can_be_run(root_client, fresh_db):
    d = root_client.get("/api/self/sandbox").json()
    assert "running" in d and isinstance(d["sources"], list)
    # the working tree must always be offered: needing a commit first is the
    # limitation this replaced
    assert any(s["kind"] == "live" for s in d["sources"])


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


# ---- no commit required: the point of the whole thing ---------------------

def test_a_snapshot_captures_uncommitted_work(tmp_path):
    """A git worktree can only show you a commit, which is the wrong tool for
    "does this change work?" — the change you want to try is usually the one
    still sitting in an agent's workspace."""
    src, dest = tmp_path / "src", tmp_path / "dest"
    (src / "conductor" / "app").mkdir(parents=True)
    (src / "conductor" / "app" / "main.py").write_text("# never committed\n")
    ok, _ = sandbox.snapshot(src, dest)
    assert ok
    assert (dest / "conductor" / "app" / "main.py").read_text() == "# never committed\n"


def test_a_snapshot_leaves_out_weight_and_state(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    (src / "node_modules" / "dep").mkdir(parents=True)
    (src / ".venv" / "bin").mkdir(parents=True)
    (src / ".git").mkdir()
    (src / "workspaces").mkdir()
    (src / "app.py").write_text("keep me")
    (src / "devteam.db").write_text("real data")
    ok, _ = sandbox.snapshot(src, dest)
    assert ok and (dest / "app.py").exists()
    for gone in ("node_modules", ".venv", ".git", "workspaces", "devteam.db"):
        assert not (dest / gone).exists(), f"{gone} was copied into the sandbox"


def test_a_snapshot_of_a_missing_source_fails_cleanly(tmp_path):
    ok, note = sandbox.snapshot(tmp_path / "nope", tmp_path / "dest")
    assert ok is False and "does not exist" in note


def test_a_source_that_is_not_a_devteam_checkout_is_refused(tmp_path, monkeypatch):
    """Booting some unrelated folder as the conductor would fail confusingly."""
    monkeypatch.setattr(sandbox, "TREE", tmp_path / "tree")
    monkeypatch.setattr(sandbox, "SANDBOX_DIR", tmp_path)
    src = tmp_path / "notdevteam"
    (src).mkdir()
    (src / "readme.md").write_text("hi")
    monkeypatch.setattr(config, "WORKSPACES_DIR", tmp_path)
    (tmp_path / "thing" / "repo").mkdir(parents=True)
    (tmp_path / "thing" / "repo" / "x.txt").write_text("y")
    res = sandbox.start("workspace:thing")
    assert res["ok"] is False and "does not look like a devteam checkout" in res["error"]


# ---- deploying an agent's work without a commit --------------------------

@pytest.mark.asyncio
async def test_deploy_can_run_an_agents_checkout(fresh_db, tmp_path, monkeypatch):
    """Deploying only from main means the first time anyone runs a change is
    after it has already shipped."""
    from app import deploy
    monkeypatch.setattr(config, "WORKSPACES_DIR", tmp_path)
    repo = tmp_path / "task-9-a1" / "repo"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("# uncommitted work")
    p = make_project(owner_id=1)
    monkeypatch.setattr(deploy, "workdir", lambda pid: tmp_path / f"out-{pid}")
    ok, note = await deploy.sync_from_workspace(p, "task-9-a1")
    assert ok, note
    assert (tmp_path / f"out-{p}" / "app.py").read_text() == "# uncommitted work"


@pytest.mark.asyncio
async def test_deploy_refuses_a_traversing_workspace_name(fresh_db):
    from app import deploy
    ok, note = await deploy.sync_from_workspace(1, "../../etc")
    assert ok is False and "refusing" in note


# ---- page/server drift must announce itself ------------------------------

def test_health_reports_when_the_page_is_newer_than_the_server(root_client, fresh_db):
    """The dashboard is served from disk, the API is whichever process is running.
    Edit both and the page calls endpoints that do not exist yet — which looks
    like a broken feature, not a conductor that needs restarting."""
    d = root_client.get("/api/health").json()
    assert "stale_ui" in d


def test_the_dashboard_shows_a_banner_when_it_is_ahead():
    js = (REPO / "dashboard" / "app.js").read_text()
    assert "stale_ui" in js and "showStaleBanner" in js


def test_the_source_dropdown_degrades_instead_of_emptying():
    """It read d.sources against a server that still returned d.branches, so the
    dropdown was empty and the feature looked dead."""
    js = (REPO / "dashboard" / "app.js").read_text()
    assert "d.sources || d.branches" in js


# ---- a sandbox must not ask for keys it is designed not to have -----------

def test_a_sandbox_does_not_demand_credentials(fresh_db, monkeypatch):
    """Holding no credentials IS the guarantee. Asking for keys is backwards:
    nothing will authenticate because no agent will ever run, and pasting a real
    key into a throwaway build is the one thing to discourage."""
    from app import auth
    monkeypatch.setattr(config, "DEMO_MODE", True)
    u = auth.get_user(1)
    assert auth.has_own_ai_credentials(u) is True


def test_the_live_app_still_demands_them(fresh_db, make_user, monkeypatch):
    from app import auth
    monkeypatch.setattr(config, "DEMO_MODE", False)
    monkeypatch.setattr(config, "AUTH_CONFIGURED", False)
    uid, _c = make_user("zoe")
    assert auth.has_own_ai_credentials(auth.get_user(uid)) is False


def test_a_project_can_be_created_in_a_sandbox(root_client, fresh_db, monkeypatch):
    """The build you most want to click through must not be the only one you
    cannot use."""
    monkeypatch.setattr(config, "DEMO_MODE", True)
    monkeypatch.setattr(config, "AUTH_CONFIGURED", False)
    r = root_client.post("/api/projects", json={"name": "in sandbox", "brief": "try me"})
    assert r.status_code == 200, r.text


# ---- fidelity: a mock that differs visibly trains you to distrust it ------

def test_the_sandbox_reports_the_same_auth_shape_as_its_parent(monkeypatch):
    """The dashboard reads auth mode to decide whether to show agent RUNS or
    DOLLARS. A sandbox reporting "none" rendered "$0.00 / $5.00" where the live
    build shows "9/40 runs" — a difference in the thing under test."""
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "CLAUDE_CODE_OAUTH_TOKEN", "")
    monkeypatch.setattr(config, "CLI_LOGIN", False)
    monkeypatch.setattr(config, "DEMO_MODE", True)
    monkeypatch.setenv("DEMO_AUTH_MODE", "subscription")
    assert config.auth_mode() == "subscription"


def test_the_parent_passes_its_shape_not_its_secret():
    env = sandbox._child_env(8701)
    assert env["DEMO_AUTH_MODE"] in ("subscription", "api-key", "none")
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""


def test_github_reads_as_connected_in_a_sandbox(monkeypatch):
    """"GitHub isn't connected" is a lie about the CANDIDATE: it renders a blocker
    the real build would not show and hides the PR links a reviewer needs."""
    from app import github_client
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    monkeypatch.setattr(config, "DEMO_MODE", True)
    assert github_client.enabled("demo-org/thing") is True
    assert github_client.enabled("") is False


@pytest.mark.asyncio
async def test_no_github_call_can_escape_a_sandbox(monkeypatch):
    """Intercepted at the single choke point, so a future code path that forgets
    to check DEMO_MODE still cannot reach the network."""
    from app import github_client
    monkeypatch.setattr(config, "DEMO_MODE", True)

    class Boom:
        def __init__(self, *a, **k): raise AssertionError("the sandbox called GitHub")
    monkeypatch.setattr(github_client.httpx, "AsyncClient", Boom)
    n = await github_client.create_pr("o/r", "task/1", "main", "t", "b")
    assert isinstance(n, int)


@pytest.mark.asyncio
async def test_the_mock_github_is_stateful(monkeypatch):
    """A stub returning a random number each call gives a task a different PR on
    every refresh, and merges that never stick — the reviewer would be judging the
    mock's inconsistency instead of the candidate."""
    from app import github_client
    monkeypatch.setattr(config, "DEMO_MODE", True)
    demo._GH.update({"next": 101, "prs": {}, "issues": {}, "merged": set()})
    a = await github_client.create_pr("o/r", "task/9", "main", "t", "b")
    b = await github_client.create_pr("o/r", "task/9", "main", "t", "b")
    c = await github_client.create_pr("o/r", "task/10", "main", "t", "b")
    assert a == b, "the same branch produced two different PRs"
    assert c != a
    assert await github_client.merge_pr("o/r", a) is True


@pytest.mark.asyncio
async def test_a_simulated_worker_emits_tool_use_not_just_prose(fresh_db, monkeypatch):
    """That interleaving is most of what the Activity tab exists to show."""
    import asyncio
    monkeypatch.setattr(config, "DEMO_MODE", True)
    monkeypatch.setattr(demo.random, "uniform", lambda a, b: 0)
    p = make_project(owner_id=1)
    t = make_task(p)
    await demo.simulate(t)
    await asyncio.sleep(0.4)
    kinds = [e["kind"] for e in db.list_events(p)]
    assert "tool_use" in kinds and "message" in kinds


def test_the_settings_screen_is_not_a_wall_of_not_set(monkeypatch):
    from app import auth
    monkeypatch.setattr(config, "DEMO_MODE", True)
    r = auth.redacted({})
    assert r["anthropic_api_key_set"] and r["github_token_set"]
