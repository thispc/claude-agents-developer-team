"""The parts of the algorithm the plan calls the heart: what runs first, who
reads finished work, and what does not get paid for twice.
"""

import json

import pytest

from conftest import make_project, make_task
from app import db, review, scheduler, team, tuning


def _deps(task_id, deps):
    db.update_task(task_id, deps=json.dumps(deps))


# --- what goes first ------------------------------------------------------

def test_a_task_holding_up_others_is_dispatched_before_one_that_is_not(fresh_db):
    """Creation order is fine when capacity exceeds the ready set and actively
    harmful when it does not — the last worker slot going to a leaf while three
    people wait behind a blocker costs the whole project the difference."""
    pid = make_project(name="o1")
    leaf = make_task(pid, role="backend", title="nobody waits on this")
    trunk = make_task(pid, role="backend", title="three people wait on this")
    for _ in range(3):
        t = make_task(pid, role="frontend", title="waiting")
        _deps(t, [trunk])

    tasks = db.list_tasks(pid)
    ready = [t for t in tasks if t["id"] in (leaf, trunk)]
    ordered = scheduler.order_by_impact(ready, tasks)
    assert [t["id"] for t in ordered] == [trunk, leaf]


def test_impact_counts_the_whole_chain_not_just_direct_dependents(fresh_db):
    """One task with a long tail behind it outranks one with two immediate
    dependents and nothing after them."""
    pid = make_project(name="o2")
    chain = make_task(pid, role="backend", title="start of a chain")
    prev = chain
    for i in range(4):
        nxt = make_task(pid, role="backend", title=f"link {i}")
        _deps(nxt, [prev])
        prev = nxt

    wide = make_task(pid, role="backend", title="two dependents, no tail")
    for _ in range(2):
        t = make_task(pid, role="frontend", title="leafy")
        _deps(t, [wide])

    tasks = db.list_tasks(pid)
    impact = scheduler.downstream(tasks)
    assert impact[chain] == 4
    assert impact[wide] == 2


def test_ordering_is_stable_so_two_runs_stay_comparable(fresh_db):
    """An unstable order would make two runs of the same plan incomparable, which
    defeats the point of recording per-run metrics at all."""
    pid = make_project(name="o3")
    ids = [make_task(pid, role="backend", title=f"t{i}") for i in range(5)]
    tasks = db.list_tasks(pid)
    first = [t["id"] for t in scheduler.order_by_impact(tasks, tasks)]
    second = [t["id"] for t in scheduler.order_by_impact(list(reversed(tasks)), tasks)]
    assert first == second == sorted(ids)


def test_a_cycle_does_not_hang_the_impact_calculation(fresh_db):
    """The fixpoint has to terminate even on input the DAG check would reject —
    this runs on every scheduler tick and must never be the thing that wedges."""
    pid = make_project(name="o4")
    a = make_task(pid, role="backend", title="a")
    b = make_task(pid, role="backend", title="b")
    _deps(a, [b])
    _deps(b, [a])
    impact = scheduler.downstream(db.list_tasks(pid))
    assert impact[a] == 1 and impact[b] == 1     # each other, not themselves


# --- idle capacity --------------------------------------------------------

def test_idle_capacity_names_who_is_sitting_out(fresh_db):
    pid = make_project(name="o5")
    team.hire(pid, [{"role": "backend", "count": 2}])
    cap = scheduler.idle_capacity(pid, db.get_project(pid))
    assert cap["free_slots"] == 3
    assert {a["role"] for a in cap["idle"]} == {"backend"}


def test_a_busy_teammate_is_not_reported_as_idle(fresh_db):
    pid = make_project(name="o6")
    team.hire(pid, [{"role": "backend", "count": 2}])
    tid = make_task(pid, role="backend", title="x")
    team.claim(db.get_task(tid))
    cap = scheduler.idle_capacity(pid, db.get_project(pid))
    assert len(cap["idle"]) == 1


# --- the review panel -----------------------------------------------------

def test_the_author_never_reviews_their_own_work(fresh_db):
    """Asking someone whether their own work is good is not a review, and it
    reliably returns yes."""
    pid = make_project(name="o7")
    team.hire(pid, [{"role": "backend", "count": 2}, {"role": "tester", "count": 1}])
    tid = make_task(pid, role="backend", title="x")
    author = team.claim(db.get_task(tid))

    chosen = review.reviewers(pid, db.get_task(tid), size=3)
    assert author["id"] not in [r["id"] for r in chosen]


def test_a_reviewer_from_another_role_is_preferred(fresh_db):
    """Someone who does the same job tends to share the same blind spot, and the
    point of a second reader is to not have the first one's."""
    pid = make_project(name="o8")
    team.hire(pid, [{"role": "backend", "count": 3}, {"role": "tester", "count": 1}])
    tid = make_task(pid, role="backend", title="x")
    team.claim(db.get_task(tid))

    first = review.reviewers(pid, db.get_task(tid), size=1)[0]
    assert first["role"] == "tester"


def test_a_failing_check_is_put_in_front_of_the_authors_prose(fresh_db):
    """An imperfect verifier caps how good the outcome can get regardless of
    budget, so the verifier's word goes first and the write-up is context for it."""
    pid = make_project(name="o9")
    tid = make_task(pid, role="backend", title="x")
    db.update_task(tid, verification=json.dumps(
        {"ran": True, "ok": False, "cmd": "pytest", "exit_code": 1,
         "output": "2 failed"}), report="Everything works great!")
    t = db.get_task(tid)

    ev = review._evidence(t)
    assert "FAILED" in ev and "pytest" in ev
    prompt = review._prompt(t, {"name": "R", "role": "tester", "notes": ""})
    assert prompt.index("The evidence") < prompt.index("What the author says")


def test_unverified_work_is_labelled_unverified_not_assumed_fine(fresh_db):
    pid = make_project(name="o10")
    tid = make_task(pid, role="backend", title="x")
    db.update_task(tid, verification=json.dumps({"ran": False, "reason": "no command"}))
    assert "NOTHING VERIFIED" in review._evidence(db.get_task(tid))


def test_an_unreadable_review_counts_as_rework_not_approval(fresh_db):
    """Silence is not consent. The opposite default means work waved through by a
    parsing bug."""
    assert review._parse("I have thoughts but no verdict line")["verdict"] == "rework"
    assert review._parse("VERDICT: accept\nWHY: it is fine")["verdict"] == "accept"
    assert review._parse("")["verdict"] == "rework"


def test_the_manager_is_shown_the_split_including_the_dissent(fresh_db):
    """A panel that returns 'accept' has thrown away the only part the manager
    could not have worked out alone."""
    text = review.summarise({
        "reviews": [{"name": "A", "role": "tester", "verdict": "accept", "text": "fine"},
                    {"name": "B", "role": "backend", "verdict": "rework", "text": "leaks"}],
        "accepts": 1, "voting": 2, "unanimous": False})
    assert "1 of 2" in text
    assert "disagree" in text
    assert "leaks" in text


def test_an_unreachable_reviewer_does_not_get_a_vote_invented_for_them(fresh_db):
    result = {"reviews": [{"name": "A", "role": "tester", "verdict": "unavailable",
                           "text": "could not be reached"}],
              "accepts": 0, "voting": 0, "unanimous": False}
    assert "No teammate could be reached" in review.summarise(result)


def test_a_project_with_nobody_else_says_so_rather_than_failing(fresh_db):
    pid = make_project(name="o11")
    tid = make_task(pid, role="backend", title="x")
    assert review.reviewers(pid, db.get_task(tid), size=3) == []


# --- reuse ----------------------------------------------------------------

def test_verification_reuse_can_be_switched_off(fresh_db):
    """It is a cost optimisation on a correctness-critical path, so it has a
    switch, and the switch has to actually reach the worker."""
    assert tuning.get("reuse_verification") is True
    tuning.set("reuse_verification", False)
    assert tuning.get("reuse_verification") is False
