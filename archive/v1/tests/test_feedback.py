"""Feature: notes that stay attached to the thing they are about.

A directive is a message with no subject. These tests pin the parts that make a
note different from one: it names its target, it reaches the manager with the
state of that target attached, it says how old the work is, and it is never
quietly dropped because nobody happened to be listening when it was written.
"""

import pytest

from conftest import make_project, make_task
from app import db, feedback


def _directives(pid):
    return [r["text"] for r in db._rows(
        "SELECT text FROM inbox WHERE project_id=? AND kind='directive' ORDER BY id",
        (pid,))]


# ---- a note has to point at something --------------------------------------

def test_a_note_on_a_task_from_another_project_is_refused(fresh_db):
    """Task ids are global; a note attached across projects would show up in the
    artifact view of a project its author cannot see."""
    mine = make_project(name="mine")
    theirs = make_project(name="theirs")
    other_task = make_task(theirs)
    with pytest.raises(ValueError):
        feedback.record(mine, "task", other_task, "looks wrong")


def test_a_note_on_a_sprint_the_project_has_not_reached_is_refused(fresh_db):
    pid = make_project(name="p")
    with pytest.raises(ValueError):
        feedback.record(pid, "sprint", 9, "not there yet")


def test_an_empty_note_is_refused(fresh_db):
    pid = make_project(name="p")
    with pytest.raises(ValueError):
        feedback.record(pid, "project", 0, "   ")


def test_an_unknown_target_is_refused(fresh_db):
    pid = make_project(name="p")
    with pytest.raises(ValueError):
        feedback.record(pid, "pull_request", 3, "hmm")


# ---- what the manager actually receives ------------------------------------

def test_a_note_on_a_task_carries_that_tasks_state_to_the_manager(fresh_db):
    """The whole point: the manager should not have to go and find out what is
    being discussed."""
    pid = make_project(name="p", status="running")
    tid = make_task(pid, role="frontend", title="build the signup form",
                    status="review")
    db.update_task(tid, pr_number=12)
    feedback.record(pid, "task", tid, "the error copy is unhelpful", "root")
    feedback.deliver(pid)

    sent = _directives(pid)[0]
    assert "build the signup form" in sent
    assert "frontend" in sent and "review" in sent
    assert "PR #12" in sent
    assert "the error copy is unhelpful" in sent
    assert "root" in sent


def test_an_unverified_task_is_described_as_unverified(fresh_db):
    pid = make_project(name="p", status="running")
    tid = make_task(pid, title="ship it", status="done")
    feedback.record(pid, "task", tid, "did anyone check this?")
    feedback.deliver(pid)
    assert "nothing verified it" in _directives(pid)[0]


def test_a_note_on_a_sprint_carries_that_sprints_counts(fresh_db):
    pid = make_project(name="p", status="running")
    make_task(pid, title="a", status="done")
    make_task(pid, title="b", status="failed")
    feedback.record(pid, "sprint", 1, "too little shipped")
    feedback.deliver(pid)
    sent = _directives(pid)[0]
    assert "sprint 1" in sent and "1 delivered" in sent and "1 failed" in sent


def test_each_note_becomes_its_own_directive(fresh_db):
    """Batched into one message, no note could record which directive carried it,
    so 'the manager has been told about this one' becomes unanswerable."""
    pid = make_project(name="p", status="running")
    feedback.record(pid, "project", 0, "first thought")
    feedback.record(pid, "project", 0, "second thought")
    feedback.deliver(pid)
    assert len(_directives(pid)) == 2
    assert all(n["directive_id"] for n in db.list_feedback(pid))


# ---- late feedback: delivered, but labelled --------------------------------

def test_a_note_on_an_old_sprint_is_delivered_with_its_age(fresh_db):
    """Feedback that arrives late is still the boss's opinion. Delivering it
    unlabelled invites the manager to rewrite a frozen cycle in place."""
    pid = make_project(name="p", status="running")
    db.advance_sprint(pid)
    db.advance_sprint(pid)          # the project is now in sprint 3
    feedback.record(pid, "sprint", 1, "the first release notes overclaimed")
    feedback.deliver(pid)

    sent = _directives(pid)[0]
    assert "about sprint 1" in sent
    assert "2 sprint(s) ago" in sent
    assert "do not rewrite the old one" in sent


def test_a_note_on_the_current_sprint_carries_no_age_warning(fresh_db):
    pid = make_project(name="p", status="running")
    feedback.record(pid, "sprint", 1, "looks fine")
    feedback.deliver(pid)
    assert "sprint(s) ago" not in _directives(pid)[0]


def test_a_note_on_a_task_from_an_earlier_sprint_is_aged_too(fresh_db):
    pid = make_project(name="p", status="running")
    tid = make_task(pid, title="the old thing")
    db.advance_sprint(pid)
    feedback.record(pid, "task", tid, "still bothers me")
    feedback.deliver(pid)
    assert "about sprint 1" in _directives(pid)[0]


# ---- nobody listening ------------------------------------------------------

def test_a_note_on_a_finished_project_is_held_not_queued(fresh_db):
    """A directive queued for a manager that will never run is swallowed with no
    trace, and the boss cannot tell that from being ignored."""
    pid = make_project(name="p", status="done")
    feedback.record(pid, "project", 0, "one more thing")
    assert feedback.deliver(pid) == []
    assert _directives(pid) == []
    assert db.list_feedback(pid)[0]["status"] == "open"


def test_the_view_says_out_loud_that_notes_are_waiting_on_a_dead_project(fresh_db):
    pid = make_project(name="p", status="cancelled")
    feedback.record(pid, "project", 0, "one more thing")
    view = feedback.summary(pid)
    assert view["held"] is True and view["open"] == 1
    assert "cancelled" in view["note"]


def test_restarting_a_project_delivers_everything_that_was_held(root_client, fresh_db):
    pid = make_project(name="p", status="failed")
    feedback.record(pid, "task", make_task(pid, title="the broken bit"),
                    "this never worked")
    r = root_client.post(f"/api/projects/{pid}/restart")
    assert r.status_code == 200, r.text
    assert r.json()["notes_delivered"] == 1
    assert "the broken bit" in _directives(pid)[0]
    assert db.list_feedback(pid)[0]["status"] == "delivered"


def test_delivering_twice_does_not_send_the_same_note_again(fresh_db):
    pid = make_project(name="p", status="running")
    feedback.record(pid, "project", 0, "say it once")
    feedback.deliver(pid)
    feedback.deliver(pid)
    assert len(_directives(pid)) == 1


# ---- through the API -------------------------------------------------------

def test_a_note_posted_on_a_live_project_is_delivered_immediately(root_client, fresh_db):
    pid = make_project(name="p", status="running")
    tid = make_task(pid, title="the login screen")
    r = root_client.post(f"/api/projects/{pid}/feedback",
                         json={"target": "task", "target_id": tid,
                               "text": "the copy is wrong"})
    assert r.status_code == 200, r.text
    assert r.json()["delivered"] is True
    assert "the login screen" in _directives(pid)[0]


def test_a_note_posted_on_a_stopped_project_says_why_it_is_waiting(root_client,
                                                                   fresh_db):
    pid = make_project(name="p", status="done")
    r = root_client.post(f"/api/projects/{pid}/feedback",
                         json={"text": "next time, more tests"})
    body = r.json()
    assert body["delivered"] is False
    assert "restarting the project" in body["held_reason"]


def test_notes_can_be_read_back_scoped_to_one_task(root_client, fresh_db):
    pid = make_project(name="p", status="running")
    a, b = make_task(pid, title="a"), make_task(pid, title="b")
    for tid, text in ((a, "about a"), (b, "about b")):
        root_client.post(f"/api/projects/{pid}/feedback",
                         json={"target": "task", "target_id": tid, "text": text})
    got = root_client.get(f"/api/projects/{pid}/feedback",
                          params={"target": "task", "target_id": a}).json()
    assert [n["text"] for n in got["notes"]] == ["about a"]


def test_a_sprints_artifacts_come_with_the_notes_written_on_it(root_client, fresh_db):
    pid = make_project(name="p", status="running")
    make_task(pid, title="a", status="done")
    root_client.post(f"/api/projects/{pid}/feedback",
                     json={"target": "sprint", "target_id": 1, "text": "thin sprint"})
    got = root_client.get(f"/api/projects/{pid}/sprints/1").json()
    assert [n["text"] for n in got["feedback"]["notes"]] == ["thin sprint"]


def test_the_boss_closes_their_own_note_not_the_manager(root_client, fresh_db):
    """The manager acting on a note is not the same as the boss being satisfied."""
    pid = make_project(name="p", status="running")
    note = root_client.post(f"/api/projects/{pid}/feedback",
                            json={"text": "tighten the error states"}).json()["note"]
    assert note["status"] == "delivered"
    r = root_client.post(f"/api/feedback/{note['id']}/resolve")
    assert r.json()["status"] == "resolved"


def test_another_user_cannot_read_or_resolve_your_notes(root_client, fresh_db,
                                                        make_user):
    pid = make_project(name="p", status="running")
    note = root_client.post(f"/api/projects/{pid}/feedback",
                            json={"text": "private thoughts"}).json()["note"]
    _, other = make_user("mallory")
    assert other.get(f"/api/projects/{pid}/feedback").status_code == 404
    assert other.post(f"/api/feedback/{note['id']}/resolve").status_code == 404


def test_deleting_a_project_takes_its_notes_with_it(fresh_db):
    pid = make_project(name="p", status="running")
    feedback.record(pid, "project", 0, "a note")
    db.delete_project(pid)
    assert db.list_feedback(pid) == []


def test_a_project_that_predates_feedback_simply_has_none(fresh_db):
    """Everything has to degrade for rows written before the table existed."""
    pid = make_project(name="old")
    view = feedback.summary(pid)
    assert view["notes"] == [] and view["held"] is False
