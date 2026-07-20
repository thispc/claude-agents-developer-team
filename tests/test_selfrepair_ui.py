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


def test_leaving_the_self_project_closes_the_self_view():
    """Switching project left the tab on screen showing the platform's data under
    another project's name."""
    js = (DASH / "app.js").read_text()
    block = js.split('const st = $("#selfTab");', 1)[1][:400]
    assert "switchView(\"command\")" in block


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
