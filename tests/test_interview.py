"""The conversation before planning, and changing the manager mid-project."""

import asyncio
import json

import pytest

from conftest import make_project, make_task
from app import db, interview, manager, process, tuning

from conftest import dashboard_js  # the split dashboard JS, concatenated in load order


def _tool(pid, name):
    manager.build_team_server(pid)
    return manager.HANDLERS[pid]["handlers"][name]


# --- what it asks ----------------------------------------------------------

def test_the_questions_are_shaped_by_how_the_work_will_be_split(fresh_db):
    """Agile needs the first useful slice settled; waterfall needs the shape,
    because a contract that changes later invalidates finished work. Asking the
    same questions either way would waste the one interruption you get."""
    assert "FIRST usable" in interview.FOCUS["agile"]
    assert "invalidates finished work" in interview.FOCUS["waterfall"]
    assert set(interview.FOCUS) == set(process.MODELS)


def test_the_drafter_is_told_not_to_ask_what_it_should_decide(fresh_db):
    """A manager that asks permission for its own job is worse than one that
    guesses — it spends the interruption and still has to decide."""
    assert "Never ask what you can decide yourself" in interview.SYSTEM
    assert "would fit any project is a wasted question" in interview.SYSTEM


def test_a_clear_brief_is_allowed_to_produce_no_questions(fresh_db):
    """Forcing three questions out of an unambiguous brief trains people to
    ignore the whole mechanism."""
    assert "return no questions" in interview.SYSTEM
    assert interview._parse('{"questions": []}', 3) == []


def test_questions_are_capped_at_the_knob(fresh_db):
    raw = json.dumps({"questions": [{"ask": f"q{i}"} for i in range(9)]})
    assert len(interview._parse(raw, 3)) == 3


def test_prose_instead_of_json_degrades_to_no_interview(fresh_db):
    """The failure mode has to be 'plan straight from the brief' — the behaviour
    before this existed — not a crash on a project that was fine yesterday."""
    assert interview._parse("Sure! Here are some questions:", 3) == []
    assert interview._parse("", 3) == []


def test_a_fenced_json_block_is_still_read(fresh_db):
    raw = '```json\n{"questions": [{"ask": "Which platform?", "options": ["web"]}]}\n```'
    got = interview._parse(raw, 3)
    assert got[0]["ask"] == "Which platform?"
    assert got[0]["options"] == ["web"]


def test_each_question_says_what_it_would_change(fresh_db):
    """A question with a visible consequence gets a considered answer; one
    without it reads as a form to fill in."""
    msg = interview.as_message([
        {"ask": "Backtracking, or just the combat feel?",
         "why": "it decides whether level design is one task or five",
         "options": ["Full backtracking", "Combat feel only"]}])
    assert "Backtracking" in msg
    assert "one task or five" in msg
    assert "ignore the rest" in msg


# --- what it does with the answer -----------------------------------------

def test_an_answer_outranks_the_managers_own_reading(fresh_db):
    pid = make_project(name="i1")
    block = interview.record(pid, [{"ask": "Which platform?"}], "web only, no mobile")
    assert "web only" in block
    assert "outrank" in block


def test_no_answer_means_plan_anyway_and_say_what_you_assumed(fresh_db):
    """Silence must not stop the project, and it must not be silent either —
    an unstated assumption is the expensive kind."""
    pid = make_project(name="i2")
    block = interview.record(pid, [{"ask": "Which platform?"}], "")
    assert "did not answer" in block
    assert "state the assumptions" in block


def test_the_exchange_is_kept(fresh_db):
    pid = make_project(name="i3")
    interview.record(pid, [{"ask": "Scope?"}], "small")
    assert interview.stored(pid)["answer"] == "small"


@pytest.mark.asyncio
async def test_the_interview_happens_once(fresh_db, monkeypatch):
    """Asking again on a restarted session would re-interrupt someone who has
    already answered."""
    pid = make_project(name="i4")
    interview.record(pid, [{"ask": "asked already"}], "answered already")
    out = str(await _tool(pid, "interview_boss")({}))
    assert "already" in out.lower()


@pytest.mark.asyncio
async def test_an_autonomous_project_does_not_wait(fresh_db, monkeypatch):
    """They asked for unattended. Blocking the start on a question defeats it."""
    async def one_question(project, settings):
        return [{"ask": "Which platform?", "why": "", "options": []}]
    monkeypatch.setattr(interview, "draft", one_question)
    tuning.set("interview_wait_seconds", 600)
    try:
        pid = make_project(name="i5", autonomy="autonomous")
        out = str(await asyncio.wait_for(_tool(pid, "interview_boss")({}), timeout=10))
    finally:
        tuning.reset("interview_wait_seconds")
    assert "did not answer" in out


@pytest.mark.asyncio
async def test_a_brief_with_nothing_to_ask_records_that_and_moves_on(fresh_db, monkeypatch):
    async def nothing(project, settings):
        return []
    monkeypatch.setattr(interview, "draft", nothing)
    pid = make_project(name="i6")
    out = str(await _tool(pid, "interview_boss")({}))
    assert "clear enough" in out
    assert interview.stored(pid)["questions"] == []


def test_the_manager_is_told_to_interview_before_planning(fresh_db):
    """Ordering is the whole point: the plan's shape depends on the answers, so
    asking afterwards means redoing the plan or ignoring what was said."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "conductor" / "app" / "manager.py").read_text()
    assert "call interview_boss BEFORE create_tasks" in src


# --- the contest knob ------------------------------------------------------

def test_the_contest_width_knob_is_actually_read(fresh_db):
    """It was declared as tunable and then ignored — the manager hardcoded 3. A
    knob nothing reads is worse than no knob, because it says the value is
    adjustable and then quietly is not."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "conductor" / "app" / "manager.py").read_text()
    assert 'compete=min(compete, 3)' not in src
    assert 'tuning.get("contest_max_width")' in src


def test_contests_stay_off_unless_the_manager_asks(fresh_db):
    """Running every task three ways would triple the bill for a benefit the
    evidence says is small."""
    pid = make_project(name="c1")
    tid = make_task(pid, role="backend", title="x")
    assert (db.get_task(tid).get("compete") or 0) == 0


# --- the surfaces you actually see -----------------------------------------

def _dash(name):
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "dashboard" / name).read_text()


def test_a_question_carries_what_sort_of_moment_it_is(fresh_db):
    """Three different moments shared one heading. Being asked to settle an
    ambiguity before any work starts is not a problem to be unblocked, and
    "your manager needs a decision" made the first thing a new project did look
    like something had already gone wrong."""
    pid = make_project(name="t1")
    db.ask_question(pid, "before we start…", ["ok"], topic="interview")
    assert db.pending_question(pid)["topic"] == "interview"


def test_an_ordinary_question_still_defaults_to_a_decision(fresh_db):
    """Every question asked before this existed, and every caller that has not
    been updated, must keep working."""
    pid = make_project(name="t2")
    db.ask_question(pid, "merge this?", ["yes"])
    assert db.pending_question(pid)["topic"] == "decision"


def test_the_route_passes_the_topic_to_the_dashboard(fresh_db):
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "conductor" / "app" / "routes.py").read_text()
    assert '"topic": q.get("topic", "decision")' in src
    assert 'topic: q.topic || "decision"' in dashboard_js()


def test_the_interview_keeps_its_shape_on_screen():
    """It arrives as numbered questions with their consequences indented under
    them. Without preserved newlines the whole thing collapses into one run-on
    paragraph and the structure — the readable part — is gone."""
    css = _dash("style.css")
    block = css.split(".ask-card .qtext {")[1].split("}")[0]
    assert "pre-wrap" in block


def test_each_kind_of_ask_is_framed_as_what_it_is():
    js = dashboard_js()
    assert "Before your manager plans this" in js
    assert "Sprint finished" in js
    assert "Your manager needs a decision" in js


def test_the_manager_model_can_be_changed_from_the_screen_it_is_shown_on():
    js = dashboard_js()
    assert 'id="mgrModelSel"' in js
    assert "/manager-model" in js


def test_the_model_picker_admits_it_does_not_apply_immediately():
    """A model is bound when a session starts, so a running manager keeps the old
    one. Reporting plain success would leave someone waiting for a change that
    cannot happen until they restart it."""
    js = dashboard_js()
    assert "restart_needed" in js
    assert "Restart manager" in js


def test_every_way_of_being_asked_something_reaches_the_feed(fresh_db):
    """Only ask_boss ever emitted a feed event, so the two NEW ways of being asked
    — the interview before planning and the sprint check-in — were invisible in
    the one place people watch. On a live run the manager asked and nobody saw."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "conductor" / "app" / "manager.py").read_text()
    assert src.count('"boss_question"') >= 3, (
        "a question is posted somewhere without telling the activity feed")
    interview_block = src.split("async def interview_boss")[1].split("@tool")[0]
    assert '"boss_question"' in interview_block
