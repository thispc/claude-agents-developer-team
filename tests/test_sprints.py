"""Feature: sprints — the project runs N delivery cycles without the human.

Each sprint the manager decides its own requirements, ships, and rolls straight
into the next one. The point is that asking for 6 sprints up front means not
being asked 6 times.

Also covers the run-cost fixes the mars-rover logs exposed: a session limit is
capacity, not a quality failure, and a retry must not start cold.
"""

import json

import pytest

from conftest import make_project, make_task
from app import config, db, launcher, manager, scheduler


def _project(sprints=3, **kw):
    pid = db.create_project("p", "brief", "", 5.0, 3, max_runs=40,
                            owner_id=1, sprints=sprints, **kw)
    db.set_project_status(pid, "running")
    return pid


# ---- the sprint counter ----------------------------------------------------

def test_a_project_defaults_to_a_single_sprint(fresh_db):
    p = make_project()
    assert db.get_project(p)["sprints"] == 1
    assert db.get_project(p)["sprint"] == 1


def test_sprints_are_stored_and_advance(fresh_db):
    p = _project(sprints=4)
    assert db.get_project(p)["sprints"] == 4
    assert db.advance_sprint(p) == 2
    assert db.advance_sprint(p) == 3


def test_sprint_count_is_clamped_to_at_least_one(fresh_db):
    p = db.create_project("p", "b", "", 5.0, 3, owner_id=1, sprints=0)
    assert db.get_project(p)["sprints"] == 1


def test_tasks_are_stamped_with_the_sprint_that_created_them(fresh_db):
    p = _project(sprints=3)
    a = make_task(p)
    db.advance_sprint(p)
    b = make_task(p)
    assert db.get_task(a)["sprint"] == 1
    assert db.get_task(b)["sprint"] == 2


# ---- the gate: a finished sprint is not a finished product -----------------

def test_finishing_a_sprint_rolls_into_the_next_one(fresh_db):
    p = _project(sprints=3)
    t = make_task(p, status="done")
    db.update_task(t, title="login page")
    out = manager.sprint_gate(p, "shipped the login page")
    assert out, "the gate should have kept the project going"
    assert db.get_project(p)["sprint"] == 2
    assert db.get_project(p)["status"] == "running"
    # the manager is told what it just delivered, so sprint 2 isn't a repeat
    assert "login page" in out
    assert "do NOT finish" in out
    assert "SPRINT 2" in out


def test_the_last_sprint_really_does_finish(fresh_db):
    p = _project(sprints=2)
    db.advance_sprint(p)                    # now on the final sprint
    assert manager.sprint_gate(p) == ""
    assert db.get_project(p)["sprint"] == 2


def test_a_single_sprint_project_finishes_immediately(fresh_db):
    assert manager.sprint_gate(_project(sprints=1)) == ""


@pytest.mark.asyncio
async def test_finish_refuses_to_end_a_project_with_sprints_left(fresh_db):
    p = _project(sprints=2)
    make_task(p, status="done")
    manager.build_team_server(p)
    finish = manager.HANDLERS[p]["handlers"]["finish"]
    out = str(await finish({"status": "done", "summary": "v1 shipped"}))
    assert "SPRINT 1 OF 2 IS COMPLETE" in out
    assert db.get_project(p)["status"] == "running"
    assert db.get_project(p)["status"] != "done"


@pytest.mark.asyncio
async def test_finish_still_works_on_the_final_sprint(fresh_db):
    p = _project(sprints=2)
    db.advance_sprint(p)
    make_task(p, status="done")
    manager.build_team_server(p)
    finish = manager.HANDLERS[p]["handlers"]["finish"]
    await finish({"status": "done", "summary": "all done"})
    assert db.get_project(p)["status"] == "done"


@pytest.mark.asyncio
async def test_the_outstanding_work_guard_still_wins(fresh_db):
    """Sprints must not become a way to skip past unfinished work."""
    p = _project(sprints=3)
    make_task(p, status="failed")
    manager.build_team_server(p)
    finish = manager.HANDLERS[p]["handlers"]["finish"]
    out = str(await finish({"status": "done", "summary": "x"}))
    assert "REFUSED" in out
    assert db.get_project(p)["sprint"] == 1, "a refused finish must not burn a sprint"


def test_the_gate_emits_a_sprint_boundary_event(fresh_db):
    p = _project(sprints=3)
    make_task(p, status="done")
    manager.sprint_gate(p, "summary")
    ev = [e for e in db.list_events(p) if e["kind"] == "sprint_finished"]
    assert ev and json.loads(ev[-1]["payload"])["of"] == 3


# ---- the manager is briefed on which sprint it is in -----------------------

def test_sprint_briefing_tells_the_manager_not_to_rebuild(fresh_db, monkeypatch):
    p = _project(sprints=3)
    t = make_task(p, status="done")
    db.update_task(t, title="auth service")
    db.advance_sprint(p)
    captured = {}

    async def fake_query(prompt, options):
        captured["prompt"] = prompt
        if False:
            yield None
    monkeypatch.setattr(manager, "query", fake_query)
    import asyncio
    asyncio.run(manager.run_manager(p))
    assert "SPRINT 2" in captured["prompt"]
    assert "auth service" in captured["prompt"]
    assert "Do NOT rebuild" in captured["prompt"]


def test_a_one_sprint_project_gets_no_sprint_noise(fresh_db, monkeypatch):
    p = _project(sprints=1)
    captured = {}

    async def fake_query(prompt, options):
        captured["prompt"] = prompt
        if False:
            yield None
    monkeypatch.setattr(manager, "query", fake_query)
    import asyncio
    asyncio.run(manager.run_manager(p))
    assert "DELIVERY MODEL" not in captured["prompt"]


# ---- run cost: what the mars-rover logs actually showed --------------------

def test_a_session_limit_is_capacity_not_a_quality_failure():
    """Anthropic's wording matched none of the markers, so two capacity deaths were
    classified as quality failures and escalated the model for no reason."""
    assert launcher.looks_rate_limited("You've hit your session limit · resets 3pm") is True


def test_escalation_moves_up_from_whatever_actually_failed(fresh_db):
    """Returning a fixed model made this a no-op when the roster had already
    assigned it — the run 'escalated' sonnet-5 to sonnet-5."""
    p = make_project()
    t = make_task(p)
    db.update_task(t, attempts=2, model="claude-sonnet-5")
    got = launcher.pick_model(db.get_task(t), db.get_project(p))
    assert got != "claude-sonnet-5", "escalated to the model that just failed"
    assert got == "claude-opus-4-8"


def test_escalation_stops_at_the_strongest_model(fresh_db):
    p = make_project()
    t = make_task(p)
    db.update_task(t, attempts=3, model="claude-opus-4-8")
    assert launcher.pick_model(db.get_task(t), db.get_project(p)) == "claude-opus-4-8"


def test_a_retry_carries_what_the_previous_attempt_achieved(fresh_db):
    """Six of eight runs on the mars-rover project were retries that started cold
    and re-derived work the branch already contained."""
    p = make_project()
    t = make_task(p)
    db.update_task(t, attempts=1,
                   report="Built sim/link_budget.py and validated it against DSN numbers.")
    ctx = launcher.prior_attempt(db.get_task(t))
    assert "link_budget" in ctx
    assert "CONTINUE" in ctx


def test_a_capacity_death_is_described_as_such_to_the_retry(fresh_db):
    p = make_project()
    t = make_task(p)
    db.update_task(t, attempts=1, report="Error: you've hit your session limit")
    assert "not by a mistake" in launcher.prior_attempt(db.get_task(t))


def test_a_first_attempt_gets_no_prior_context(fresh_db):
    p = make_project()
    t = make_task(p)
    assert launcher.prior_attempt(db.get_task(t)) == ""


# ---- the API and the UI must agree about sprints and modes ----------------

def test_create_project_accepts_and_clamps_sprints(root_client, fresh_db):
    r = root_client.post("/api/projects", json={
        "name": "six", "brief": "build a thing", "sprints": 6})
    assert r.status_code == 200, r.text
    assert db.get_project(r.json()["id"])["sprints"] == 6
    r2 = root_client.post("/api/projects", json={
        "name": "silly", "brief": "b", "sprints": 9999})
    assert db.get_project(r2.json()["id"])["sprints"] == 20      # clamped, not rejected


def test_project_payload_exposes_sprint_state(root_client, fresh_db):
    p = _project(sprints=3)
    got = root_client.get(f"/api/projects/{p}").json()
    assert got["sprints"] == 3 and got["sprint"] == 1


def test_the_wizard_collects_a_sprint_count():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "dashboard" / "index.html").read_text()
    assert 'name="sprints"' in html


# ---- self-repair access ----------------------------------------------------

def test_root_may_always_self_repair():
    assert config.may_self_repair("root", True) is True


def test_a_normal_user_may_not_by_default(monkeypatch):
    monkeypatch.setattr(config, "SELFREPAIR_USERS", [])
    assert config.may_self_repair("zoe", False) is False


def test_the_operator_can_grant_it_by_username(monkeypatch):
    monkeypatch.setattr(config, "SELFREPAIR_USERS", ["zoe"])
    assert config.may_self_repair("Zoe", False) is True
    assert config.may_self_repair("mallory", False) is False


def test_self_endpoint_refuses_an_ungranted_user(client, make_user, fresh_db, monkeypatch):
    monkeypatch.setattr(config, "SELFREPAIR_USERS", [])
    _uid, c2 = make_user("mallory")
    assert c2.get("/api/self").status_code == 403


def test_me_tells_the_ui_whether_to_offer_self_repair(root_client, fresh_db):
    assert root_client.get("/api/me").json()["may_self_repair"] is True


# ---- the run cap has to survive the sprints it was given -------------------

def test_run_cap_scales_with_sprints(root_client, fresh_db):
    """A cap sized for one pass stops mid-way through sprint 2 with the product
    half-built, which reads as the team giving up rather than a guard rail."""
    r = root_client.post("/api/projects", json={
        "name": "six", "brief": "b", "sprints": 6})
    p = db.get_project(r.json()["id"])
    assert p["sprints"] == 6
    assert p["max_runs"] == config.MAX_AGENT_RUNS * 6


def test_an_explicit_cap_is_a_decision_and_is_respected(root_client, fresh_db):
    r = root_client.post("/api/projects", json={
        "name": "tight", "brief": "b", "sprints": 4, "max_runs": 300})
    assert db.get_project(r.json()["id"])["max_runs"] == 300


def test_a_single_sprint_project_is_not_scaled(root_client, fresh_db):
    r = root_client.post("/api/projects", json={"name": "one", "brief": "b", "sprints": 1})
    assert db.get_project(r.json()["id"])["max_runs"] == config.MAX_AGENT_RUNS


def test_scaling_is_bounded(root_client, fresh_db):
    r = root_client.post("/api/projects", json={"name": "many", "brief": "b", "sprints": 20})
    assert db.get_project(r.json()["id"])["max_runs"] <= 400
