"""Faults the platform notices about itself, and the daily pass that reads them.

Everything these tests exercise was already being recorded before and none of it
was being read: a fault that happened forty times over a weekend looked exactly
like one that happened once, because neither was ever counted.
"""

import time

import pytest

from conftest import make_project
from app import db, findings, tuning, upkeep


@pytest.fixture()
def clean(fresh_db):
    findings.init()
    db._execute("DELETE FROM findings")
    db.kv_delete("findings_scanned_to")
    db.kv_delete(upkeep.LAST_RUN_KEY)
    return fresh_db


# --- counting the same fault as the same fault ----------------------------

def test_the_same_fault_is_counted_not_re_filed(clean):
    for _ in range(5):
        findings.record("dispatch_error", "could not launch worker: connection refused")
    rows = db._rows("SELECT * FROM findings", ())
    assert len(rows) == 1
    assert rows[0]["count"] == 5


def test_line_numbers_and_ids_do_not_make_two_faults_different(clean):
    """Traces differ by task id, line number and port between otherwise identical
    failures. A fingerprint that kept those would file a fresh finding for every
    occurrence — exactly the counting problem this exists to solve."""
    findings.record("dispatch_error", "task 41 failed on port 8000 at line 220")
    findings.record("dispatch_error", "task 97 failed on port 9123 at line 4")
    assert len(db._rows("SELECT * FROM findings", ())) == 1


def test_genuinely_different_faults_stay_separate(clean):
    findings.record("dispatch_error", "connection refused")
    findings.record("worker_stalled", "no activity for 30 min")
    assert len(db._rows("SELECT * FROM findings", ())) == 2


def test_an_unwatched_event_kind_is_ignored(clean):
    """A kind that fires in normal operation would bury everything real, and a
    scanner nobody trusts gets ignored — which is worse than not having one."""
    assert findings.record("dispatched", "task 3 started") is None
    assert db._rows("SELECT * FROM findings", ()) == []


def test_rate_limits_are_not_treated_as_defects(clean):
    """They are frequent, they are not defects, and the platform already handles
    them. Listing them would put them permanently at the top."""
    assert "rate_limited" not in findings.WATCHED


# --- ranking ---------------------------------------------------------------

def test_a_rare_critical_outranks_a_noisy_warning(clean):
    """Severity sets the band and frequency ranks within it. Frequency alone
    would put a chatty warning above a data-loss bug."""
    findings.record("dispatch_error", "critical thing")             # critical, seen once
    for _ in range(50):
        findings.record("client_error", "noisy warning")            # warning, seen often

    top = findings.ranked()
    assert top[0]["kind"] == "dispatch_error"


def test_frequency_ranks_within_a_severity_band(clean):
    findings.record("client_error", "seen once")
    for _ in range(20):
        findings.record("http_error", "seen many times")

    top = findings.ranked()
    assert top[0]["detail"].startswith("seen many")


def test_an_old_fault_sinks_below_a_current_one(clean):
    """Something that stopped happening a week ago was probably fixed by
    something. Without decay the top of the list is a museum."""
    old = findings.record("dispatch_error", "ancient problem")
    for _ in range(30):
        findings.record("dispatch_error", "ancient problem")
    db._execute("UPDATE findings SET last_seen=? WHERE id=?",
                (time.time() - 30 * 86400, old["id"]))
    findings.record("dispatch_error", "happening right now")

    top = findings.ranked()
    assert top[0]["detail"] == "happening right now"


def test_one_runaway_loop_cannot_own_the_list_forever(clean):
    """Logarithmic in count: 1 vs 10 occurrences matters far more than 100 vs
    1000, and linear scaling lets a single crash loop drown everything."""
    a = findings.record("client_error", "a")
    b = findings.record("dispatch_error", "b")
    db._execute("UPDATE findings SET count=100000 WHERE id=?", (a["id"],))
    assert findings.ranked()[0]["id"] == b["id"]


# --- lifecycle -------------------------------------------------------------

def test_a_recurrence_after_a_fix_reopens_it_loudly(clean):
    """A repair that did not hold is more interesting than a fresh fault, and
    quietly re-filing it as new would hide that."""
    f = findings.record("dispatch_error", "boom")
    findings.set_status(f["id"], "fixed")

    again = findings.record("dispatch_error", "boom")
    assert again["status"] == "open"
    assert again["count"] == 2


def test_a_dismissed_fault_that_comes_back_is_reopened(clean):
    f = findings.record("http_error", "nope")
    findings.set_status(f["id"], "dismissed")
    assert findings.record("http_error", "nope")["status"] == "open"


# --- the scan --------------------------------------------------------------

def test_the_scan_reads_recorded_events_rather_than_hooking_them(clean):
    """Working off stored rows also picks up whatever happened while the scanner
    was not running — a hook would miss all of it."""
    pid = make_project(name="sc")
    for i in range(3):
        db.add_event(pid, None, "scheduler", "dispatch_error", "spawn failed: no route")
    db.add_event(pid, None, "scheduler", "dispatched", "task 1 started")

    out = findings.scan()
    assert out["events_read"] == 3
    rows = db._rows("SELECT * FROM findings", ())
    assert len(rows) == 1 and rows[0]["count"] == 3


def test_the_scan_does_not_re_read_what_it_already_read(clean):
    pid = make_project(name="sc2")
    db.add_event(pid, None, "scheduler", "dispatch_error", "once")
    findings.scan()
    assert findings.scan()["events_read"] == 0
    assert db._rows("SELECT * FROM findings", ())[0]["count"] == 1


def test_a_malformed_payload_does_not_stop_the_scan(clean):
    pid = make_project(name="sc3")
    db.add_event(pid, None, "scheduler", "dispatch_error", {"detail": "structured"})
    db.add_event(pid, None, "scheduler", "worker_stalled", ["unexpected", "shape"])
    assert findings.scan()["events_read"] == 2


# --- the daily pass --------------------------------------------------------

def test_a_pass_is_not_due_immediately_after_one_ran(clean):
    """A restart must not re-run a pass that already happened."""
    db.kv_set(upkeep.LAST_RUN_KEY, time.time())
    assert upkeep.due() is False


def test_a_pass_is_due_once_the_interval_has_elapsed(clean):
    db.kv_set(upkeep.LAST_RUN_KEY, time.time() - 25 * 3600)
    assert upkeep.due() is True


def test_a_machine_that_was_off_all_night_runs_once_not_eight_times(clean):
    """The last run is stored rather than counted, so missed windows do not
    accumulate into a burst."""
    db.kv_set(upkeep.LAST_RUN_KEY, time.time() - 8 * 24 * 3600)
    assert upkeep.due() is True
    import asyncio
    asyncio.run(upkeep.run_once())
    assert upkeep.due() is False


def test_the_whole_thing_can_be_switched_off(clean):
    db.kv_set(upkeep.LAST_RUN_KEY, 0)
    tuning.set("upkeep_enabled", False)
    try:
        assert upkeep.due() is False
    finally:
        tuning.reset("upkeep_enabled")


def test_a_quiet_period_produces_no_ticket(clean):
    import asyncio
    out = asyncio.run(upkeep.run_once())
    assert out["actionable"] == 0
    assert out["filed"] is None


def test_only_findings_above_the_bar_are_actionable(clean):
    """The threshold is a knob because the right value depends on how noisy a
    given deployment is, which nobody can know in advance."""
    findings.record("client_error", "one small warning")
    assert findings.worth_a_sprint() == []

    for _ in range(20):
        findings.record("dispatch_error", "workers will not start")
    assert len(findings.worth_a_sprint()) == 1


def test_filing_can_be_switched_off_without_switching_off_looking(clean):
    """Someone who wants to know what the platform thinks is wrong should not
    have to let it act to find out."""
    import asyncio
    for _ in range(20):
        findings.record("dispatch_error", "workers will not start")
    tuning.set("upkeep_files_tickets", False)
    try:
        out = asyncio.run(upkeep.run_once())
    finally:
        tuning.reset("upkeep_files_tickets")

    assert out["actionable"] == 1
    assert out["filed"] is None       # found and ranked, not filed


def test_the_loop_survives_a_failing_pass(clean, monkeypatch):
    """A crash here silently disables self-monitoring, which is the failure mode
    where you find out months later that nothing has been watching."""
    import asyncio

    async def explode(*a, **k):
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(upkeep, "run_once", explode)
    monkeypatch.setattr(upkeep, "due", lambda now=None: True)
    monkeypatch.setattr(upkeep, "TICK_SECONDS", 0.01)

    async def briefly():
        task = asyncio.get_event_loop().create_task(upkeep.loop())
        await asyncio.sleep(0.05)
        assert not task.done(), "the upkeep loop died on an error"
        task.cancel()

    asyncio.run(briefly())
