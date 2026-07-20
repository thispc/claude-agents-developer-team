"""Features: model escalation/fallback, contests, agent kill/sweep, done-guard,
per-project task numbers, role normalisation.

Commits: db96e20 (escalation), e71906e (rate-limit cooldown), 4b51c3f (contests),
9332713 (kill/sweep), 077e73b (done-guard + seq), 8f1d56c (seq), plus the
canon_role fix.
"""

import time

import pytest

from conftest import make_project, make_task
from app import db, launcher, scheduler


# ---- per-project task numbers (seq) ---------------------------------------

def test_seq_starts_at_one_per_project(fresh_db):
    p1 = make_project(name="p1")
    p2 = make_project(name="p2")
    a = make_task(p1); b = make_task(p1)
    c = make_task(p2)
    assert db.get_task(a)["seq"] == 1
    assert db.get_task(b)["seq"] == 2
    assert db.get_task(c)["seq"] == 1          # second project restarts at 1
    # global ids remain unique
    assert len({a, b, c}) == 3


def test_resolve_task_prefers_seq(fresh_db):
    p = make_project()
    t = make_task(p)                            # seq 1, some global id
    assert db.resolve_task(p, 1)["id"] == t     # by seq
    assert db.resolve_task(p, t)["id"] == t     # falls back to global id


# ---- model selection: escalation + fallback -------------------------------

def test_pinned_model_wins(fresh_db):
    p = make_project()
    t = make_task(p, role="backend")
    db.update_task(t, pinned_model="claude-opus-4-8", attempts=5)
    assert launcher.pick_model(db.get_task(t), db.get_project(p)) == "claude-opus-4-8"


def test_escalates_after_two_attempts(fresh_db):
    from app import config
    p = make_project()
    t = make_task(p, role="backend")
    db.update_task(t, attempts=2)
    assert launcher.pick_model(db.get_task(t), db.get_project(p)) == config.ESCALATION_MODEL


def test_recruited_role_model_applies_despite_spacing(fresh_db):
    """The canon_role fix: a roster role with a space/underscore still matches."""
    team = [{"role": "Propulsion Engineer", "count": 1, "model": "claude-opus-4-8"}]
    p = make_project(team=team)
    t = make_task(p, role="propulsion-engineer")   # manager-normalised form
    got = launcher.pick_model(db.get_task(t), db.get_project(p))
    assert got == "claude-opus-4-8", f"recruited model lost: {got}"


def test_weak_rate_limit_marker_needs_context(fresh_db):
    # A tester report merely mentioning a 429 is NOT a rate limit…
    assert launcher.looks_rate_limited("the endpoint returned 429 to the user") is False
    # …but a 429 in an error context is.
    assert launcher.looks_rate_limited("Error: HTTP 429 rate_limit exceeded") is True
    assert launcher.looks_rate_limited("overloaded, please retry") is True


# ---- contests --------------------------------------------------------------

def test_contender_lifecycle(fresh_db):
    p = make_project()
    t = make_task(p, status="running")
    c1 = db.create_contender(t, 1, "task/x-c1", "claude-haiku-4-5")
    c2 = db.create_contender(t, 2, "task/x-c2", "claude-sonnet-5")
    assert len(db.list_contenders(t)) == 2
    db.update_contender(c1, status="pushed", report="A")
    db.update_contender(c2, status="pushed", report="B")
    running = db.list_running_contenders(p)
    assert running == []          # both finished
    db.clear_contenders(t)
    assert db.list_contenders(t) == []


# ---- kill + sweep ----------------------------------------------------------

def test_sweep_orphans_fails_stuck_tasks(fresh_db):
    p = make_project(status="running")
    running = make_task(p, status="running")
    queued = make_task(p, status="queued")
    planned = make_task(p, status="planned")
    n = launcher.sweep_orphans()
    assert n == 2                                  # running + queued, not planned
    assert db.get_task(running)["status"] == "failed"
    assert db.get_task(queued)["status"] == "failed"
    assert db.get_task(planned)["status"] == "planned"


def test_kill_task_marks_failed_and_clears_registry(fresh_db):
    p = make_project()
    t = make_task(p, status="running")
    launcher.ACTIVE[str(t)] = {"kind": "process", "pid": None, "proc": None,
                               "project_id": p, "task_id": t}
    launcher.kill_task(t, "test kill")
    assert db.get_task(t)["status"] == "failed"
    assert str(t) not in launcher.ACTIVE


def test_kill_project_stops_all_its_agents(fresh_db):
    p = make_project()
    t1 = make_task(p, status="running")
    t2 = make_task(p, status="queued")
    for t in (t1, t2):
        launcher.ACTIVE[str(t)] = {"kind": "process", "pid": None, "proc": None,
                                   "project_id": p, "task_id": t}
    launcher.kill_project(p, "cancelled")
    assert db.get_task(t1)["status"] == "failed"
    assert db.get_task(t2)["status"] == "failed"
    assert not [k for k, v in launcher.ACTIVE.items() if v["project_id"] == p]


# ---- done-guard / reconcile ------------------------------------------------

def test_project_cannot_be_done_with_a_failed_task(fresh_db):
    p = make_project(status="done")
    make_task(p, status="done")
    make_task(p, status="failed", title="cart & checkout")
    changed = scheduler.reconcile_status(p)
    assert changed is True
    proj = db.get_project(p)
    assert proj["status"] == "review"
    assert "Cannot be done" in proj["summary"]


def test_done_project_with_unfinished_work_reopens(fresh_db):
    p = make_project(status="done")
    make_task(p, status="running")     # still working
    scheduler.reconcile_status(p)
    assert db.get_project(p)["status"] == "running"


def test_clean_done_project_stays_done(fresh_db):
    p = make_project(status="done")
    make_task(p, status="done")
    make_task(p, status="done")
    assert scheduler.reconcile_status(p) is False
    assert db.get_project(p)["status"] == "done"


# ---- DAG cycle detection ---------------------------------------------------

def test_cycle_detection(fresh_db):
    p = make_project()
    a = make_task(p)
    b = make_task(p)
    db.update_task(a, deps=__import__("json").dumps([b]))
    db.update_task(b, deps=__import__("json").dumps([a]))
    assert scheduler.has_cycle(p)          # non-empty list = cycle found


def test_no_cycle_on_linear_dag(fresh_db):
    p = make_project()
    a = make_task(p)
    b = make_task(p)
    db.update_task(b, deps=__import__("json").dumps([a]))
    assert scheduler.has_cycle(p) == []


# ---- PR opening is idempotent (worker may open its own) --------------------

def test_auto_open_pr_adopts_a_pr_the_worker_already_opened(fresh_db, monkeypatch):
    """A worker with Bash can open its own PR. The scheduler must adopt its number,
    not fail — otherwise the PR exists but merge_pr has nothing to merge."""
    import asyncio
    from app import github_client, scheduler

    p = make_project(repo="owner/repo")
    t = make_task(p, status="pushed")
    db.update_task(t, branch="task/1")

    monkeypatch.setattr(github_client, "enabled", lambda repo: True)
    monkeypatch.setattr(github_client, "find_pr_for_branch",
                        _async(lambda repo, branch: 42))
    # create_pr must NOT be called when one already exists
    called = {"create": False}

    async def _boom(*a, **k):
        called["create"] = True
        raise AssertionError("create_pr should not run when a PR already exists")
    monkeypatch.setattr(github_client, "create_pr", _boom)

    asyncio.run(scheduler._auto_open_pr(db.get_project(p), db.get_task(t)))
    fresh = db.get_task(t)
    assert fresh["pr_number"] == 42, "existing PR was not adopted"
    assert fresh["status"] == "review"
    assert called["create"] is False


def test_auto_open_pr_adopts_after_losing_a_race(fresh_db, monkeypatch):
    """create_pr fails with 'already exists' -> re-check and adopt, not fail."""
    import asyncio
    from app import github_client, scheduler

    p = make_project(repo="owner/repo")
    t = make_task(p, status="pushed")
    db.update_task(t, branch="task/2")

    lookups = {"n": 0}

    async def _find(repo, branch):
        lookups["n"] += 1
        return None if lookups["n"] == 1 else 77   # appears only on the re-check

    async def _create(*a, **k):
        raise RuntimeError("422 A pull request already exists for owner:task/2")

    monkeypatch.setattr(github_client, "enabled", lambda repo: True)
    monkeypatch.setattr(github_client, "find_pr_for_branch", _find)
    monkeypatch.setattr(github_client, "create_pr", _create)
    monkeypatch.setattr(github_client, "default_branch", _async(lambda repo: "main"))

    asyncio.run(scheduler._auto_open_pr(db.get_project(p), db.get_task(t)))
    fresh = db.get_task(t)
    assert fresh["pr_number"] == 77, "raced PR was not adopted"
    assert fresh["status"] == "review"


def _async(fn):
    async def _inner(*a, **k):
        return fn(*a, **k)
    return _inner
