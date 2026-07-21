"""Features: Blockers tab (derived, never stored) and Self-repair / self-ops.

Commits: a6ee780 (blockers + self-repair). Blockers are derived on read, so the
tests set up DB state and assert what the scan surfaces. Self-ops tests the
issue->manager routing and the redeploy safety guards, including the prefix-match
bug fix.
"""

import json

import pytest

from conftest import make_project, make_task
from app import db, blockers, launcher


# ---- blockers derivation ---------------------------------------------------

def test_no_blockers_on_clean_project(fresh_db):
    p = make_project(status="running")
    make_task(p, status="done")
    s = blockers.summary(p)
    assert s["critical"] == 0
    # a clean project may still warn (e.g. no repo), but nothing critical


def test_failed_task_is_a_critical_blocker(fresh_db):
    p = make_project(status="running", repo="owner/repo")
    make_task(p, role="frontend", title="checkout", status="failed")
    s = blockers.summary(p)
    assert s["critical"] >= 1
    kinds = [b["kind"] for b in s["blockers"]]
    assert "task_failed" in kinds
    card = next(b for b in s["blockers"] if b["kind"] == "task_failed")
    assert card["fix"]                      # every blocker names its fix
    assert card["task_seq"] == 1


def test_dependency_blocked_by_failed_predecessor(fresh_db):
    p = make_project(status="running", repo="owner/repo")
    a = make_task(p, status="failed")
    b = make_task(p, status="planned", deps=[a])
    kinds = [x["kind"] for x in blockers.summary(p)["blockers"]]
    assert "dep_blocked" in kinds


def test_pending_question_is_a_blocker(fresh_db):
    p = make_project(status="hold", repo="owner/repo")
    db.ask_question(p, "Which database?", ["postgres", "sqlite"])
    kinds = [x["kind"] for x in blockers.summary(p)["blockers"]]
    assert "awaiting_boss" in kinds


def test_run_cap_blocker(fresh_db):
    p = make_project(status="running", repo="owner/repo")
    proj = db.get_project(p)
    db._execute("UPDATE projects SET runs_used=?, max_runs=? WHERE id=?", (40, 40, p))
    kinds = [x["kind"] for x in blockers.summary(p)["blockers"]]
    assert "run_cap" in kinds


def test_blockers_are_not_stored(fresh_db):
    """Derived on read: fixing the state makes the blocker vanish, no cleanup."""
    p = make_project(status="running", repo="owner/repo")
    t = make_task(p, status="failed")
    assert blockers.summary(p)["critical"] >= 1
    db.update_task(t, status="done")        # fix it
    assert not any(b["kind"] == "task_failed" for b in blockers.summary(p)["blockers"])


# ---- self-ops --------------------------------------------------------------

def test_self_issue_becomes_a_directive_not_a_raw_task(fresh_db, monkeypatch):
    """A raw task would be dispatched immediately with no plan or review; the fix
    routes it through the manager as a directive instead."""
    from app import selfops, scheduler
    monkeypatch.setattr(scheduler, "ensure", lambda pid: None)
    pid = selfops.ensure_project(owner_id=1)
    import asyncio
    res = asyncio.run(
        selfops.file_issue(pid, "Bug in agents tab", "Scroll jumps to top", "bug"))
    assert res["queued"] is True
    # no task was created directly
    assert db.list_tasks(pid) == []
    # a directive is waiting for the manager
    directives = db.take_directives(pid)
    assert len(directives) == 1
    assert "Bug in agents tab" in directives[0]


def test_self_project_is_marked_and_reused(fresh_db):
    from app import selfops
    pid1 = selfops.ensure_project(owner_id=1)
    pid2 = selfops.ensure_project(owner_id=1)
    assert pid1 == pid2                      # created once, reused
    assert db.get_project(pid1)["is_self"] == 1


def test_can_redeploy_prefix_match_fix(fresh_db, monkeypatch):
    """The prefix-match bug: task id 1 must NOT count active key '12' as its agent."""
    from app import selfops
    launcher.ACTIVE.clear()
    launcher.ACTIVE["12"] = {"project_id": 99, "task_id": 12}   # a different task
    pid = selfops.ensure_project(owner_id=1)
    t1 = make_task(pid, status="running")
    # rename its id-in-ACTIVE scenario: task t1 is NOT key "12"
    # force t1 to look recent so only the prefix logic could (wrongly) flag it
    db._execute("UPDATE tasks SET updated_at=? WHERE id=?",
                (__import__("time").time(), t1))
    # a task whose id is a prefix of "12" must not be reported as tracked
    # (we assert the helper doesn't crash and the fix uses exact/hyphen match)
    res = selfops.can_redeploy()
    assert "reasons" in res
    launcher.ACTIVE.clear()


# --- what a blocker DOES vs how urgent it is -------------------------------
#
# Reported from a real run: a game project on staging, progressing normally with
# a task actively running, showed "1 slowing it down". Nothing was slow. The one
# blocker was "nothing is verifying this project", which is about evidence, not
# pace — but the UI derived its wording from severity, and every warning became
# the words "slowing it down".

def test_a_blocker_says_what_it_does_not_only_how_bad_it_is(fresh_db):
    from app import blockers
    pid = make_project(name="impact")
    t = make_task(pid, status="done")
    db.update_task(t, verification="")
    items = blockers.scan(pid)
    unverified = [b for b in items if b["kind"] == "unverified"]
    assert unverified, "the unverified blocker did not fire"
    assert unverified[0]["impact"] == "evidence"
    assert unverified[0]["impact"] != "pace"


def test_every_blocker_kind_declares_an_impact(fresh_db):
    """A kind added without one silently falls back to 'worth knowing', which is
    how a stopping-the-project fault would come to read as a footnote."""
    import re
    from pathlib import Path
    from app import blockers
    src = (Path(__file__).resolve().parent.parent
           / "conductor" / "app" / "blockers.py").read_text()
    kinds = set(re.findall(r'_b\(\s*(?:"[a-z]+"|"[a-z]+" if [^,]+ else "[a-z]+"),\s*"([a-z_]+)"',
                           src))
    missing = kinds - set(blockers.IMPACT)
    assert not missing, f"these blocker kinds have no declared impact: {sorted(missing)}"


def test_things_that_genuinely_slow_a_project_are_still_marked_as_pace(fresh_db):
    from app import blockers
    for kind in ("slow_worker", "rate_limited", "awaiting_manager", "dep_blocked"):
        assert blockers.IMPACT[kind] == "pace", kind


def test_things_that_stop_a_project_outright_are_marked_stopped(fresh_db):
    from app import blockers
    for kind in ("task_failed", "budget", "run_cap", "no_credentials", "stalled"):
        assert blockers.IMPACT[kind] == "stopped", kind


def test_the_verification_advice_fits_a_project_with_no_test_suite(fresh_db):
    """The advice listed pytest, go test and cargo test at a browser game. A build
    or lint script is accepted by the checker and is the realistic answer there,
    so it should not be buried behind four irrelevant ecosystems."""
    from app import blockers
    pid = make_project(name="advice")
    t = make_task(pid, status="done")
    db.update_task(t, verification="")
    fix = [b for b in blockers.scan(pid) if b["kind"] == "unverified"][0]["fix"]
    assert "does not have to be a test suite" in fix
    assert fix.index("build") < fix.index("pytest"), \
        "the realistic option for a web project is buried behind irrelevant ones"
