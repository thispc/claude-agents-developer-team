"""Self-repair: triggered only when asked, and only through a good ticket.

The platform working on itself is the one project where "it started on its own"
is genuinely dangerous — the repo it edits is the one this process runs from.
"""

import json
from pathlib import Path

import pytest

from conftest import make_project, make_task
from app import config, db, selfops

DASH = Path(__file__).resolve().parent.parent / "dashboard"


# ---- it must never start itself -------------------------------------------

def test_the_self_project_starts_idle_not_planning(fresh_db):
    """A new project defaults to 'planning', and startup resumes planning/running/
    hold — so opening the tab once meant a manager began changing the running
    platform on every restart."""
    pid = selfops.ensure_project(1)
    assert db.get_project(pid)["status"] == "idle"


def test_startup_never_resumes_the_self_project(fresh_db):
    src = (Path(__file__).resolve().parent.parent / "conductor" / "app" / "main.py").read_text()
    resume = src.split("for p in db.list_projects():", 1)[1][:600]
    assert 'p["is_self"]' in resume and "continue" in resume


def test_ensure_project_is_idempotent(fresh_db):
    assert selfops.ensure_project(1) == selfops.ensure_project(1)


# ---- it is not one of "your projects" -------------------------------------

def test_the_self_row_is_hidden_from_the_projects_list(root_client, fresh_db):
    make_project(owner_id=1, name="normal")
    selfops.ensure_project(1)
    names = [p["name"] for p in root_client.get("/api/projects").json()]
    assert "normal" in names
    assert selfops.SELF_PROJECT_NAME not in names


def test_but_it_is_still_reachable_directly(root_client, fresh_db):
    pid = selfops.ensure_project(1)
    assert root_client.get(f"/api/projects/{pid}").status_code == 200


def test_self_repair_is_a_page_not_a_tab_on_someone_elses_project():
    """As a tab it lingered after switching project, showing the platform's data
    under another project's name. It is now its own page off the landing tile."""
    html = (DASH / "index.html").read_text()
    js = (DASH / "app.js").read_text()
    assert 'id="selfTab"' not in html, "the tab chip is back"
    assert 'data-v="self"' not in html
    assert 'id="selfPage"' in html
    assert '#/improve' in js, "the page has no route of its own"


def test_opening_a_project_hides_the_self_repair_page():
    js = (DASH / "app.js").read_text()
    block = js.split("function openProject(", 1)[1][:300]
    assert '$("#selfPage")' in block and "hidden = true" in block


# ---- the ticket is refined before anyone works on it ----------------------

@pytest.mark.asyncio
async def test_refine_expands_a_one_liner_into_a_ticket(monkeypatch):
    async def fake(provider, model, system, prompt, settings, max_tokens=2000):
        return json.dumps({
            "title": "Blockers tab shows no background",
            "body": "## Now\nThe panel renders unstyled.\n## Expected\nStyled.",
            "severity": "bug",
            "acceptance": ["the panel has a background", "text is readable"]})
    monkeypatch.setattr("app.providers.complete", fake)
    got = await selfops.refine_issue("blockers tab looks broken",
                                     {"claude_oauth_token": "x"})
    assert got["refined"] is True
    assert got["title"] == "Blockers tab shows no background"
    assert "Acceptance criteria" in got["body"]
    assert "- [ ] the panel has a background" in got["body"]


@pytest.mark.asyncio
async def test_refine_falls_back_to_the_users_own_words(monkeypatch):
    """A provider outage must not stop someone filing a bug."""
    async def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr("app.providers.complete", boom)
    got = await selfops.refine_issue("the bell count is wrong",
                                     {"claude_oauth_token": "x"})
    assert got["refined"] is False
    assert got["body"] == "the bell count is wrong"


@pytest.mark.asyncio
async def test_refine_without_credentials_still_returns_a_ticket():
    got = await selfops.refine_issue("something is broken", {})
    assert got["refined"] is False and got["title"]


def test_refine_route_needs_a_real_complaint(root_client, fresh_db):
    assert root_client.post("/api/self/refine", json={"rough": "bad"}).status_code == 400


def test_refine_route_is_not_open_to_everyone(client, make_user, fresh_db, monkeypatch):
    monkeypatch.setattr(config, "SELFREPAIR_USERS", [])
    _uid, c2 = make_user("mallory")
    assert c2.post("/api/self/refine", json={"rough": "the thing is broken"}).status_code == 403


# ---- raising an issue runs it autonomously, over sprints ------------------

def test_filing_an_issue_makes_the_run_autonomous_and_scales_the_cap(root_client, fresh_db,
                                                                     monkeypatch):
    async def fake_file(pid, title, body, severity):
        return {"issue": 1}
    monkeypatch.setattr(selfops, "file_issue", fake_file)
    monkeypatch.setattr("app.routes.manager.run_manager", lambda pid: _noop())
    r = root_client.post("/api/self/issue", json={
        "title": "t", "body": "b", "severity": "bug", "sprints": 3})
    assert r.status_code == 200, r.text
    p = db.get_project(r.json()["project_id"])
    assert p["autonomy"] == "autonomous", "self-repair should not stop to ask"
    assert p["sprints"] == 3
    assert p["max_runs"] == config.MAX_AGENT_RUNS * 3


async def _noop():
    return None


def test_a_single_sprint_issue_leaves_the_cap_alone(root_client, fresh_db, monkeypatch):
    async def fake_file(pid, title, body, severity):
        return {"issue": 1}
    monkeypatch.setattr(selfops, "file_issue", fake_file)
    monkeypatch.setattr("app.routes.manager.run_manager", lambda pid: _noop())
    r = root_client.post("/api/self/issue", json={"title": "t", "body": "b"})
    assert db.get_project(r.json()["project_id"])["max_runs"] == config.MAX_AGENT_RUNS


# ---- the draft step has to exist in the UI, not just the API --------------

def test_the_self_panel_offers_a_draft_step():
    js = (DASH / "app.js").read_text()
    assert 'id="roughIssue"' in js and 'id="refineBtn"' in js
    assert "/api/self/refine" in js


def test_the_ui_admits_when_the_draft_is_just_your_own_words():
    """A silent fallback reads as 'the AI thought it was already perfect'."""
    js = (DASH / "app.js").read_text()
    block = js.split('id="refineBtn"', 1)[1]
    assert "d.refined" in block


def test_the_issue_form_collects_sprints():
    js = (DASH / "app.js").read_text()
    assert 'name="sprints"' in js and "sprints: Number(f.get(\"sprints\"))" in js


# ---- pruning must never delete a live agent's workspace ------------------

def test_pruning_keeps_workspaces_of_running_tasks(fresh_db, tmp_path, monkeypatch):
    from app import config as cfg, launcher
    monkeypatch.setattr(cfg, "WORKSPACES_DIR", tmp_path)
    p = make_project()
    live = make_task(p, status="running")
    old = make_task(p, status="done")
    for tid in (live, old):
        (tmp_path / f"task-{tid}-a1" / "repo").mkdir(parents=True)
    # plus plenty of finished clones so the keep-window is exceeded
    for i in range(12):
        (tmp_path / f"task-{old}-old{i}").mkdir()
    launcher.prune_workspaces(keep=2)
    assert (tmp_path / f"task-{live}-a1").exists(), "deleted a running agent's checkout"


def test_pruning_deletes_finished_clones(fresh_db, tmp_path, monkeypatch):
    from app import config as cfg, launcher
    monkeypatch.setattr(cfg, "WORKSPACES_DIR", tmp_path)
    p = make_project()
    done = make_task(p, status="done")
    for i in range(10):
        (tmp_path / f"task-{done}-a{i}").mkdir()
    removed = launcher.prune_workspaces(keep=2)
    assert removed == 8
    assert len(list(tmp_path.iterdir())) == 2


def test_pruning_deletes_nothing_when_it_cannot_tell(fresh_db, tmp_path, monkeypatch):
    """Failing closed matters here — the alternative is deleting live work."""
    from app import config as cfg, launcher
    monkeypatch.setattr(cfg, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(launcher, "_rows_of_live_tasks",
                        lambda: (_ for _ in ()).throw(RuntimeError("db gone")))
    (tmp_path / "task-1-a1").mkdir()
    assert launcher.prune_workspaces(keep=0) == 0
    assert (tmp_path / "task-1-a1").exists()


# ---- pruning must also keep the volume from filling ----------------------

def _clone(base, name, kb=1):
    d = base / name
    d.mkdir(parents=True)
    (d / "blob").write_bytes(b"x" * (kb * 1024))
    return d


def test_pruning_keeps_the_volume_under_budget_not_just_the_count(fresh_db, tmp_path,
                                                                  monkeypatch):
    """Repo size is unbounded, so a count is not a bound: a few large clones fill
    the volume as thoroughly as many small ones, and a full volume stops every
    agent — none of them has anywhere to clone."""
    from app import config as cfg, launcher
    monkeypatch.setattr(cfg, "WORKSPACES_DIR", tmp_path)
    done = make_task(make_project(), status="done")
    for i in range(6):
        _clone(tmp_path, f"task-{done}-a{i}", kb=10)
    # Well inside the keep-window (6 clones, keep 8) and far over the byte budget.
    removed = launcher.prune_workspaces(keep=8, budget=25 * 1024)
    assert removed == 4
    assert sum(f.stat().st_size for f in tmp_path.rglob("blob")) <= 25 * 1024


def test_the_budget_still_never_touches_a_live_clone(fresh_db, tmp_path, monkeypatch):
    """Reclaiming space is worth less than the work in flight, and that was a
    deliberate fix — a budget is not a licence to delete a running agent's tree."""
    from app import config as cfg, launcher
    monkeypatch.setattr(cfg, "WORKSPACES_DIR", tmp_path)
    p = make_project()
    live = make_task(p, status="running")
    _clone(tmp_path, f"task-{live}-a1", kb=50)
    launcher.prune_workspaces(keep=0, budget=1)
    assert (tmp_path / f"task-{live}-a1").exists(), "deleted a running agent's checkout"


def test_being_over_budget_with_nothing_left_to_prune_is_announced(fresh_db, tmp_path,
                                                                   monkeypatch):
    """The old failure mode was silence until the volume filled, at which point the
    platform stops and nothing ever said why."""
    from app import config as cfg, launcher
    monkeypatch.setattr(cfg, "WORKSPACES_DIR", tmp_path)
    said = []
    monkeypatch.setattr(launcher.bus, "emit",
                        lambda *a, **k: said.append((a[3], a[4])) or {})
    p = make_project()
    live = make_task(p, status="running")
    _clone(tmp_path, f"task-{live}-a1", kb=50)
    launcher.prune_workspaces(keep=0, budget=1024)
    assert said and said[0][0] == "workspaces_over_budget"
    assert said[0][1]["live_clones"] == 1


def test_a_run_that_fits_says_nothing(fresh_db, tmp_path, monkeypatch):
    """An alarm that fires when everything is fine is an alarm nobody reads."""
    from app import config as cfg, launcher
    monkeypatch.setattr(cfg, "WORKSPACES_DIR", tmp_path)
    said = []
    monkeypatch.setattr(launcher.bus, "emit", lambda *a, **k: said.append(a) or {})
    done = make_task(make_project(), status="done")
    _clone(tmp_path, f"task-{done}-a1", kb=1)
    launcher.prune_workspaces(keep=8, budget=1024 * 1024)
    assert said == []
