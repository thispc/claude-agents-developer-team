"""The sprint boundary as a conversation with the boss, not a receipt after it.

Two separate ideas here, and keeping them separate is the point: the manager may
correct the sprint COUNT once it understands the work, and the boss may steer the
CONTENT at each boundary. Neither is allowed to stall an unattended run.
"""

import pytest

from conftest import make_project, make_task
from app import db, manager, tuning


def _project(sprints=3, autonomy="supervised"):
    pid = make_project(name="sp", autonomy=autonomy)
    db.set_sprints(pid, sprints)
    return pid


def _tool(pid, name):
    manager.build_team_server(pid)
    return manager.HANDLERS[pid]["handlers"][name]


# --- the manager corrects the count ---------------------------------------

@pytest.mark.asyncio
async def test_the_manager_can_replan_the_sprint_count(fresh_db):
    """The boss picks a number before a single line has been read, which is the
    worst-informed moment in the project's life to be making that call."""
    p = _project(sprints=2)
    out = str(await _tool(p, "plan_sprints")({"sprints": 5, "reason": "bigger than it looked"}))
    assert db.get_project(p)["sprints"] == 5
    assert "2 to 5" in out


@pytest.mark.asyncio
async def test_it_cannot_replan_below_the_sprint_already_running(fresh_db):
    """Otherwise a manager could retroactively decide the sprint it is in never
    happened, and the record of what was delivered when stops meaning anything."""
    p = _project(sprints=5)
    db.advance_sprint(p)
    db.advance_sprint(p)                      # now in sprint 3
    out = str(await _tool(p, "plan_sprints")({"sprints": 1, "reason": "shrinking"}))
    assert "error" in out.lower()
    assert db.get_project(p)["sprints"] == 5


@pytest.mark.asyncio
async def test_replanning_to_the_current_sprint_makes_it_the_last(fresh_db):
    p = _project(sprints=5)
    db.advance_sprint(p)
    await _tool(p, "plan_sprints")({"sprints": 2, "reason": "enough scope"})
    assert db.get_project(p)["sprints"] == 2


@pytest.mark.asyncio
async def test_replanning_is_clamped_to_something_survivable(fresh_db):
    p = _project(sprints=1)
    await _tool(p, "plan_sprints")({"sprints": 900, "reason": "ambitious"})
    assert db.get_project(p)["sprints"] == 20


@pytest.mark.asyncio
async def test_replanning_to_the_same_number_changes_nothing(fresh_db):
    p = _project(sprints=3)
    out = str(await _tool(p, "plan_sprints")({"sprints": 3, "reason": "no change"}))
    assert "nothing changed" in out


# --- the boss steers the content ------------------------------------------

@pytest.mark.asyncio
async def test_a_sprint_boundary_asks_the_boss_something(fresh_db):
    """A digest tells the boss what happened. It does not ask them anything, so
    six sprints was six unilateral decisions with a receipt after each one."""
    p = _project(sprints=2)
    make_task(p, status="done")
    await _tool(p, "finish")({"status": "done", "summary": "v1"})

    q = db.pending_question(p)
    assert q is not None
    assert "next sprint" in q["text"].lower()


@pytest.mark.asyncio
async def test_the_boundary_does_not_stall_an_unattended_run(fresh_db):
    """A courtesy that blocks an overnight run for an hour per sprint has
    destroyed the thing the boss actually asked for. The question is posted; the
    manager carries on."""
    p = _project(sprints=2)
    make_task(p, status="done")
    assert tuning.get("sprint_checkin_seconds") == 0

    out = str(await _tool(p, "finish")({"status": "done", "summary": "v1"}))
    assert "SPRINT 1 OF 2 IS COMPLETE" in out
    assert db.get_project(p)["sprint"] == 2      # it moved on without an answer


@pytest.mark.asyncio
async def test_an_answer_that_arrived_in_time_steers_the_next_sprint(fresh_db):
    p = _project(sprints=2)
    make_task(p, status="done")

    # Stand in for a boss who was watching: answer the moment the question appears.
    real_ask = db.ask_question

    def answer_immediately(project_id, text, options):
        qid = real_ask(project_id, text, options)
        db.answer_question(qid, "drop the export feature and fix the login bug")
        return qid

    db.ask_question = answer_immediately
    tuning.set("sprint_checkin_seconds", 5)
    try:
        out = str(await _tool(p, "finish")({"status": "done", "summary": "v1"}))
    finally:
        db.ask_question = real_ask
        tuning.reset("sprint_checkin_seconds")

    assert "login bug" in out
    assert "outrank your own judgement" in out


@pytest.mark.asyncio
async def test_the_boss_can_end_the_run_early_at_a_boundary(fresh_db):
    """The cheapest moment to stop is before the next sprint is planned."""
    p = _project(sprints=6)
    make_task(p, status="done")

    real_ask = db.ask_question

    def answer_stop(project_id, text, options):
        qid = real_ask(project_id, text, options)
        db.answer_question(qid, "Stop here, this is enough")
        return qid

    db.ask_question = answer_stop
    tuning.set("sprint_checkin_seconds", 5)
    try:
        await _tool(p, "finish")({"status": "done", "summary": "v1"})
    finally:
        db.ask_question = real_ask
        tuning.reset("sprint_checkin_seconds")

    assert db.get_project(p)["sprints"] == 1


@pytest.mark.asyncio
async def test_carry_on_adds_no_instructions(fresh_db):
    """'You decide' must not be fed back to the manager as though it were guidance."""
    p = _project(sprints=2)
    make_task(p, status="done")

    real_ask = db.ask_question

    def answer_carry_on(project_id, text, options):
        qid = real_ask(project_id, text, options)
        db.answer_question(qid, "Carry on — you decide the next sprint")
        return qid

    db.ask_question = answer_carry_on
    tuning.set("sprint_checkin_seconds", 5)
    try:
        out = str(await _tool(p, "finish")({"status": "done", "summary": "v1"}))
    finally:
        db.ask_question = real_ask
        tuning.reset("sprint_checkin_seconds")

    assert "outrank" not in out


def test_the_question_says_what_actually_shipped(fresh_db):
    """A check-in that does not say what was delivered is asking the boss to
    approve something they cannot see."""
    p = _project(sprints=2)
    t = make_task(p, status="done", title="password reset")
    db.update_task(t, status="done")
    make_task(p, status="failed", title="email digest")

    text, options = manager.sprint_review_prompt(p, 1)
    assert "password reset" in text
    assert "1 task(s) failed" in text
    assert any("stop" in o.lower() for o in options)
