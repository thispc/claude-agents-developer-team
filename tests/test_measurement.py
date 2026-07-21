"""The measurement harness, the knobs it exists to evaluate, and the state that
used to evaporate on restart.

These three go together on purpose. A tunable you cannot measure and a
measurement with nothing to tune are each useless alone, and both are worthless
if the numbers reset every time the pod restarts.
"""

import time

import pytest

from conftest import make_project, make_task
from app import auth, db, metrics, notify, tuning


# --- runs: the unit of measurement ---------------------------------------

def test_a_dispatch_is_not_a_delivery_and_a_delivery_is_not_an_acceptance(fresh_db):
    """The three states have to stay distinct or first-pass acceptance becomes
    unmeasurable — which is the one number that cannot be bought with more spend."""
    pid = make_project(name="m1")
    tid = make_task(pid, role="backend", title="t")

    run = db.start_run(pid, tid, role="backend", model="claude-haiku-4-5", attempt=1)
    assert metrics.project(pid)["overall"]["running"] == 1
    assert metrics.project(pid)["overall"]["runs"] == 0    # nothing has concluded

    db.finish_run(run, "delivered", cost_usd=0.4)
    over = metrics.project(pid)["overall"]
    assert over["runs"] == 1
    assert over["tasks_delivered"] == 0     # delivered is not accepted
    assert over["first_pass"] == 0.0

    db.judge_run(tid, "accepted")
    over = metrics.project(pid)["overall"]
    assert over["tasks_delivered"] == 1
    assert over["first_pass"] == 1.0
    assert over["cost_per_task"] == 0.4


def test_rework_shows_up_as_rework_not_as_a_cheaper_success(fresh_db):
    """Two dispatches for one delivered task must read as 2.0 runs per task. If
    rework were invisible, an algorithm that redoes everything twice would look
    identical to one that gets it right first time."""
    pid = make_project(name="m2")
    tid = make_task(pid, role="backend", title="t")

    r1 = db.start_run(pid, tid, role="backend", attempt=1)
    db.finish_run(r1, "delivered", cost_usd=0.5)
    db.judge_run(tid, "rework", "tests do not cover the new branch")

    r2 = db.start_run(pid, tid, role="backend", attempt=2)
    db.finish_run(r2, "delivered", cost_usd=0.5)
    db.judge_run(tid, "accepted")

    over = metrics.project(pid)["overall"]
    assert over["runs_per_task"] == 2.0
    assert over["cost_per_task"] == 1.0          # the rejected attempt still cost money
    assert over["first_pass"] == 0.0             # accepted, but not on the first try
    assert over["rework_rate"] == 0.5
    assert over["escalation_rate"] == 0.5        # one of two runs was a retry


def test_failures_do_not_make_the_average_look_cheap(fresh_db):
    """Cost per task divides by DELIVERED tasks. Dividing by attempted ones lets a
    configuration that fails constantly report a flattering average."""
    pid = make_project(name="m3")
    good = make_task(pid, role="backend", title="good")
    bad = make_task(pid, role="backend", title="bad")

    r = db.start_run(pid, good, role="backend", attempt=1)
    db.finish_run(r, "delivered", cost_usd=1.0)
    db.judge_run(good, "accepted")

    r = db.start_run(pid, bad, role="backend", attempt=1)
    db.finish_run(r, "failed", cost_usd=3.0)

    over = metrics.project(pid)["overall"]
    assert over["tasks_attempted"] == 2
    assert over["tasks_delivered"] == 1
    assert over["cost_per_task"] == 4.0     # all $4 spent, one task to show for it
    assert over["failure_rate"] == 0.5


def test_a_run_orphaned_by_a_restart_is_swept_not_left_open(fresh_db):
    """A run stuck on 'running' is neither success nor failure, and it silently
    shrinks every denominator computed after it."""
    pid = make_project(name="m4")
    tid = make_task(pid, role="backend", title="t")
    rid = db.start_run(pid, tid, role="backend", attempt=1)
    db._execute("UPDATE runs SET started_at=? WHERE id=?", (time.time() - 99999, rid))

    assert metrics.reconcile(pid) == 1
    assert db.get_agent(0) is None                       # unrelated, but harmless
    assert metrics.project(pid)["overall"]["running"] == 0
    assert db.list_runs(pid)[0]["outcome"] == "abandoned"


def test_a_recent_run_is_not_swept(fresh_db):
    """Sweeping too eagerly would abandon work that is simply taking a while."""
    pid = make_project(name="m5")
    tid = make_task(pid, role="backend", title="t")
    db.start_run(pid, tid, role="backend", attempt=1)
    assert metrics.reconcile(pid) == 0


def test_profiles_are_compared_but_small_samples_are_not_called_results(fresh_db):
    """Two configurations measured over three tasks each is noise. Reporting it as
    a finding is how you tune towards randomness."""
    pid = make_project(name="m6")
    for i in range(3):
        tid = make_task(pid, role="backend", title=f"t{i}")
        r = db.start_run(pid, tid, role="backend", attempt=1, profile="experiment")
        db.finish_run(r, "delivered", cost_usd=0.1)
        db.judge_run(tid, "accepted")

    rows = {p["profile"]: p for p in metrics.compare()["profiles"]}
    assert rows["experiment"]["tasks_delivered"] == 3
    assert rows["experiment"]["confident"] is False


def test_a_contest_records_one_run_per_rival(fresh_db):
    """Rivals run concurrently on the same task, so runs cannot be keyed by task
    alone — three rivals that all collapsed into one row would hide the real cost
    of running a contest, which is the whole thing worth knowing about contests."""
    pid = make_project(name="m7")
    tid = make_task(pid, role="backend", title="t")
    for i in (1, 2, 3):
        cid = db.create_contender(tid, i, f"branch-{i}", "claude-haiku-4-5")
        db.start_run(pid, tid, role="backend", attempt=1, kind="contender",
                     contender_id=cid)
        assert db.open_run_for(tid, cid) is not None

    assert len(db.open_runs(pid)) == 3
    # And the plain lookup must not pick up a rival's row by mistake.
    assert db.open_run_for(tid) is None


# --- tuning ---------------------------------------------------------------

def test_knobs_resolve_default_then_environment_then_stored(fresh_db, monkeypatch):
    assert tuning.get("escalate_after_attempts") == 2

    monkeypatch.setenv("ESCALATE_AFTER_ATTEMPTS", "5")
    assert tuning.get("escalate_after_attempts") == 5

    tuning.set("escalate_after_attempts", 1, who="root")
    assert tuning.get("escalate_after_attempts") == 1   # the table wins over env

    tuning.reset("escalate_after_attempts")
    assert tuning.get("escalate_after_attempts") == 5   # back to env


def test_a_typo_is_refused_rather_than_stored(fresh_db):
    """A misspelled knob that persists silently looks exactly like a knob that
    does not work."""
    with pytest.raises(KeyError):
        tuning.set("escalate_after_attemps", 3)
    with pytest.raises(KeyError):
        tuning.reset("nonsense")


def test_a_malformed_environment_value_does_not_break_boot(fresh_db, monkeypatch):
    monkeypatch.setenv("CONTEST_MAX_WIDTH", "three")
    assert tuning.get("contest_max_width") == 3        # falls back, does not raise


def test_every_knob_says_where_its_value_came_from(fresh_db, monkeypatch):
    monkeypatch.setenv("SLOW_SECONDS", "60")
    tuning.set("contest_max_width", 2, who="root")
    by_name = {k["name"]: k for k in tuning.describe()}

    assert by_name["slow_seconds"]["source"] == "environment"
    assert by_name["contest_max_width"]["source"] == "tuned"
    assert by_name["max_attempts"]["source"] == "default"
    # Every knob carries its rationale — a number with no reason gets tuned by vibes.
    assert all(k["why"] for k in by_name.values())


def test_booleans_survive_the_round_trip(fresh_db):
    """'false' from a form field is a non-empty string, and a naive bool() call
    turns every attempt to switch something off into switching it on."""
    assert tuning.get("rebalance_idle") is True
    tuning.set("rebalance_idle", False)
    assert tuning.get("rebalance_idle") is False


# --- state that used to die on restart ------------------------------------

def test_a_lockout_survives_a_restart(fresh_db):
    """The pod restarts on its own — rollouts, node drains, OOM kills. An attacker
    does not have to cause one to get their attempts back, only to wait."""
    for _ in range(auth.LOCKOUT_AFTER):
        assert auth.verify("root", "wrong") is None
    assert auth.locked_out("root") > 0

    db._conn.close()
    db._conn = None
    db.init()                       # a restart, as far as the process is concerned

    assert auth.locked_out("root") > 0
    assert auth.verify("root", "testpass") is None     # still locked, right password


def test_a_correct_password_clears_the_counter(fresh_db):
    for _ in range(auth.LOCKOUT_AFTER - 1):
        auth.verify("root", "wrong")
    assert auth.verify("root", "testpass") is not None
    assert auth.locked_out("root") == 0


def test_root_can_release_a_locked_out_account(fresh_db):
    """Locking yourself out is the common case by a wide margin, and the old
    remedy was restarting the server."""
    for _ in range(auth.LOCKOUT_AFTER):
        auth.verify("root", "wrong")
    assert auth.locked_out("root") > 0
    auth.clear_lockouts("root")
    assert auth.locked_out("root") == 0


def test_a_fault_stays_deduplicated_across_a_restart(fresh_db):
    """The fault most worth deduplicating is the one that kills the process. A
    crash loop restarts often enough that 'once per restart' is not once at all."""
    notify.forget()
    fp = notify._fingerprint("crash", "Traceback: boom")
    notify._remember(fp, {"count": 3, "first": time.time(), "last": time.time(),
                          "issue": 12})

    db._conn.close()
    db._conn = None
    db.init()

    assert notify._seen(fp)["count"] == 3
    assert notify._seen(fp)["issue"] == 12


def test_forgetting_a_fault_makes_a_recurrence_loud_again(fresh_db):
    notify.forget()
    fp = notify._fingerprint("crash", "boom")
    notify._remember(fp, {"count": 1, "first": 0, "last": 0, "issue": 7})
    assert notify._seen(fp) is not None
    notify.forget(fp)
    assert notify._seen(fp) is None
