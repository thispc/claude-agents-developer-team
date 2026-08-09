"""The watch client: the conductor's side of the P3 extraction.

The log ring and the monitor are a service now, and this file tests the DOORS to it — the
batching shim in conductor/app/logs.py and the composing shim in conductor/app/monitor.py.
The service itself is mounted in-process by tests/conftest.py (real app, temp database, a
TestClient standing in for the socket), so these exercise the real client path against the
real service: no sockets, no fleet, no mock that can drift from the thing it stands for.

What the SERVICE does with what it is given lives in services/watch/tests.

The drill that matters most is the first one. There are 64 fire-and-forget log call sites,
some inside exception handlers and some in a loop that ticks every twenty seconds; a logger
that got slower would degrade the very thing it exists to observe. So the budget is timed
here, with the service UNREACHABLE, which is when a naive client would be slowest.
"""

import ast
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx
import pytest

from conftest import WATCH_TEST_TOKEN, watch_service

REPO = Path(__file__).resolve().parent.parent


class _DeadTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """The service being down, as a transport: every request refuses."""

    def handle_request(self, request):
        raise httpx.ConnectError("connection refused (drill)")

    async def handle_async_request(self, request):
        raise httpx.ConnectError("connection refused (drill)")


def _dead_client():
    return httpx.Client(base_url="http://watch.test", transport=_DeadTransport())


@pytest.fixture()
def no_spend(monkeypatch):
    """No approval drill may reach a provider. The repair suite has the same
    guard; a fixture is not shared across test files, and an accidental live call
    from a test about logging would be an expensive way to find that out."""
    async def boom(*a, **k):
        raise AssertionError("an offline watch test touched a provider")
    from app import providers, repair_builder
    monkeypatch.setattr(providers, "complete", boom)
    monkeypatch.setattr(repair_builder, "_run_sdk", boom)
    return boom


@pytest.fixture()
def shim(fresh_db, monkeypatch):
    """The conductor's log door, wired by conftest to the mounted service and
    emptied by fresh_db. Echo off: these tests assert on the ring, and 3000
    printed lines in a timing drill measure the terminal, not the pipeline."""
    from app import logs
    monkeypatch.setattr(logs, "ECHO", False)
    return logs


@pytest.fixture()
def dead_shim(shim, monkeypatch):
    """The same door with the ring unreachable — the degraded world."""
    monkeypatch.setattr(shim, "_client", _dead_client)
    from app import monitor
    monkeypatch.setattr(monitor, "_client", _dead_client)
    return shim


# --- the promise: a log call never waits ---------------------------------------

def test_a_thousand_log_calls_stay_inside_the_budget_with_the_service_down(dead_shim):
    """THE drill for this extraction. LOG_CALL_BUDGET_S is the contract: one
    call, wall clock, with nothing listening.

    A per-call round-trip would be milliseconds each even on localhost, and the
    engine's 20s tick plus a build's chatter would pay it hundreds of times a
    minute. Timed with the service DOWN because that is the case a naive client
    handles worst — a connect that refuses, or worse, one that hangs.
    """
    n = 1000
    started = time.perf_counter()
    for i in range(n):
        dead_shim.info("sprint", "tick", "the crew is thinking", i=i)
    elapsed = time.perf_counter() - started
    budget = n * dead_shim.LOG_CALL_BUDGET_S
    assert elapsed < budget, (
        f"{n} calls took {elapsed:.3f}s against a {budget:.3f}s budget "
        f"({elapsed / n * 1000:.3f}ms per call)")


def test_the_same_budget_holds_when_the_service_is_up(shim):
    """The healthy path must not be slower than the broken one: with AUTOFLUSH
    off nothing is posted from the call itself, so 1000 calls cost 1000 dict
    builds and 1000 appends either way."""
    n = 1000
    started = time.perf_counter()
    for i in range(n):
        shim.info("sprint", "tick", "the crew is thinking", i=i)
    elapsed = time.perf_counter() - started
    assert elapsed < n * shim.LOG_CALL_BUDGET_S


def test_no_exception_ever_escapes_a_log_call(dead_shim):
    """It is called from inside exception handlers. A logger that raises there
    replaces the failure you were trying to record with one of its own."""
    class Hostile:
        def __str__(self):
            raise RuntimeError("even str() fails")

    dead_shim.log(None, None, None)                       # type: ignore[arg-type]
    dead_shim.error("nonsense-category", "e" * 500, "m" * 5000)
    dead_shim.warn("git", "e", "m", obj=object(), nested={"a": [1, 2]}, none=None)
    dead_shim.info("git", "e", "m", hostile=Hostile())
    dead_shim.debug("git", "e", dedupe_s=60)
    # No raise IS the assertion; the return is still the row the caller expects.
    assert dead_shim.info("git", "landed", "a fix")["event"] == "landed"


def test_a_dropped_row_is_still_on_stdout_and_says_so_once(dead_shim, monkeypatch, capsys):
    """Beyond MAX_QUEUE the newest rows are dropped. That is the right trade —
    they are already on this process's stdout, and unbounded memory in the one
    module that must never be the thing that fails is not — but it has to be
    said, and said once."""
    monkeypatch.setattr(dead_shim, "ECHO", True)
    monkeypatch.setattr(dead_shim, "MAX_QUEUE", 20)
    monkeypatch.setattr(dead_shim, "_last_note", 0.0)
    for i in range(60):
        dead_shim.info("sprint", "noise", f"row {i}")
    err = capsys.readouterr()
    assert len(dead_shim._QUEUE) == 20, "the queue is bounded"
    assert err.out.count("sprint/noise") == 60, "every row still reached stdout"
    assert err.err.count("[logs]") == 1, "one note per window, not one per drop"
    assert "dropped" in err.err


# --- batching ------------------------------------------------------------------

def _recording_client(posts):
    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content)["rows"])
        return httpx.Response(200, json={"stored": len(posts[-1]), "deduped": 0})
    return lambda: httpx.Client(base_url="http://watch.test",
                                transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("n", [1, 99, 100, 101, 250])
def test_n_rows_cost_ceil_n_over_100_round_trips(shim, monkeypatch, n):
    """One POST per BATCH_ROWS. 250 lines then cost three round-trips instead of
    250 — which is the entire reason the ring could move out of the process at
    all."""
    posts: list[list] = []
    monkeypatch.setattr(shim, "_client", _recording_client(posts))
    for i in range(n):
        shim.info("sprint", "tick", f"row {i}")
    assert posts == [], "nothing may be sent from the call itself"
    shim.flush()
    assert len(posts) == -(-n // shim.BATCH_ROWS)
    assert sum(len(p) for p in posts) == n
    assert all(len(p) <= shim.BATCH_ROWS for p in posts)


def test_the_daemon_flushes_on_its_timer_without_anyone_reading(shim, monkeypatch):
    """The other half of the trigger: a quiet process that logs one line must not
    hold it until somebody happens to open the log view. FLUSH_INTERVAL_S is the
    longest a row waits."""
    posts: list[list] = []
    monkeypatch.setattr(shim, "_client", _recording_client(posts))
    monkeypatch.setattr(shim, "AUTOFLUSH", True)
    assert shim._THREAD is None, "the suite runs with the daemon off"
    try:
        shim.info("sprint", "tick", "one lonely row")
        assert shim._THREAD is not None, "the first row starts the flusher"
        deadline = time.time() + 10 * shim.FLUSH_INTERVAL_S
        while not posts and time.time() < deadline:
            time.sleep(0.05)
        assert posts, f"nothing was flushed within {10 * shim.FLUSH_INTERVAL_S}s"
        assert posts[0][0]["msg"] == "one lonely row"
    finally:
        monkeypatch.setattr(shim, "AUTOFLUSH", False)
        shim.stop()
    assert shim._THREAD is None, "stop() has to leave no thread behind"


def test_a_full_batch_does_not_wait_out_the_timer(shim, monkeypatch):
    """BATCH_ROWS queued is the other trigger: a build that logs a hundred lines
    in a burst should not sit on them for half a second."""
    posts: list[list] = []
    monkeypatch.setattr(shim, "_client", _recording_client(posts))
    monkeypatch.setattr(shim, "AUTOFLUSH", True)
    monkeypatch.setattr(shim, "FLUSH_INTERVAL_S", 30.0)     # the timer must not be the reason
    try:
        for i in range(shim.BATCH_ROWS):
            shim.info("sprint", "tick", f"row {i}")
        deadline = time.time() + 5
        while not posts and time.time() < deadline:
            time.sleep(0.02)
        assert posts, "a full batch waited for the timer"
    finally:
        monkeypatch.setattr(shim, "AUTOFLUSH", False)
        shim.stop()


def test_an_outage_holds_the_rows_and_recovery_sends_them(shim, monkeypatch):
    """pc stop watch → pc start watch, without restarting the conductor. The rows
    written while it was down are still queued and go out on the next flush."""
    monkeypatch.setattr(shim, "_client", _dead_client)
    for i in range(5):
        shim.info("git", "landed", f"a fix {i}")
    assert shim.flush() == 0 and len(shim._QUEUE) == 5, "nothing may be silently discarded"
    assert shim.recent() == []
    from conftest import _svc_client, _WATCH_URL
    monkeypatch.setattr(shim, "_client",
                        lambda: _svc_client(watch_service, _WATCH_URL, WATCH_TEST_TOKEN))
    assert shim.flush() == 5
    assert [r["msg"] for r in shim.recent()] == [f"a fix {i}" for i in range(5)]


def test_a_read_flushes_first_so_you_see_your_own_writes(shim):
    """Batching would otherwise mean a test — or the notice scan — asking for
    rows that are still sitting in this process."""
    shim.error("sandbox", "escape", "wrote into the live checkout", files="x.py")
    assert len(shim._QUEUE) == 1
    assert [r["event"] for r in shim.recent()] == ["escape"]
    assert len(shim._QUEUE) == 0


# --- the ring, through the door ------------------------------------------------

def test_the_shim_round_trips_a_row_with_its_own_fields(shim):
    shim.warn("quota", "rate_limited", "sonnet is cooling down", model="claude-sonnet-5")
    (row,) = shim.recent()
    assert row["level"] == "warn" and row["cat"] == "quota"
    assert row["model"] == "claude-sonnet-5"


def test_the_vocabulary_is_the_same_on_both_sides_of_the_wire(shim):
    """Coercion happens in the shim (the row is built and returned before
    anything is sent) and again in the service (a contract cannot trust its
    callers). Two copies that drift are a filter that quietly stops matching."""
    assert shim.CATEGORIES == watch_service.CATEGORIES
    assert tuple(shim.LEVELS) == tuple(watch_service.LEVELS)


def test_dedupe_is_the_services_and_the_echo_is_the_shims(shim, monkeypatch, capsys):
    """Two halves of one idea, split where each belongs. The RING's half moved
    with the ring, so two processes hitting the same fault collapse into one
    counted line. The ECHO's half stayed, so the 3am terminal does not repeat
    itself."""
    monkeypatch.setattr(shim, "ECHO", True)
    shim._LAST.clear()
    for _ in range(5):
        shim.log("lifecycle", "lease_held_elsewhere", "standing down",
                 level="warn", dedupe_s=600)
    out = capsys.readouterr()
    assert out.err.count("lease_held_elsewhere") == 1, "the terminal said it once"
    rows = [r for r in shim.recent() if r["event"] == "lease_held_elsewhere"]
    assert len(rows) == 1 and rows[0]["repeats"] == 5, "and the ring counted all five"


def test_a_second_process_needs_no_shared_memory_to_dedupe(shim):
    """The reason the window check had to move: `_LAST` is per-process, so before
    P3 a fleet produced one line per process for a single fault."""
    row = dict(cat="lifecycle", event="lease_held_elsewhere", msg="standing down",
               level="warn", dedupe_s=600)
    shim.log(**row)
    shim._LAST.clear()                  # "a different process", with no memory of the first
    shim.log(**row)
    rows = [r for r in shim.recent() if r["event"] == "lease_held_elsewhere"]
    assert len(rows) == 1 and rows[0]["repeats"] == 2


def test_the_filters_travel_to_the_service_and_come_back_applied(shim):
    """The floor, the category and the free-text search are the SERVICE's
    arithmetic now, so the conductor-side claim is narrower and sharper: every
    filter the caller asked for actually reached it. A client that quietly
    dropped `level` would answer every question with "no warnings"."""
    shim.debug("git", "a"); shim.info("git", "b")
    shim.warn("git", "c"); shim.error("sandbox", "d", "wrote outside", sha="abc1234")
    assert [r["event"] for r in shim.recent(level="warn")] == ["c", "d"]
    assert [r["event"] for r in shim.recent(cat="git", event="b")] == ["b"]
    assert [r["event"] for r in shim.recent(q="abc1234")] == ["d"]
    assert [r["event"] for r in shim.recent(errors_only=True)] == ["d"]
    assert [r["event"] for r in shim.recent(limit=1)] == ["d"]


def test_stats_and_errors_come_back_through_the_door(shim):
    shim.error("sandbox", "escape", "wrote outside")
    shim.info("git", "landed", "a fix")
    st = shim.stats(3600)
    assert st["by_level"]["error"] == 1 and st["by_cat"]["sandbox"] == 1
    assert "degraded" not in st
    assert [r["event"] for r in shim.rows(errors_only=True)] == ["escape"]


# --- the notices composition ---------------------------------------------------

def _stale_queue(db_mod, n=1):
    db_mod.kv_set("repair:queue", [{"slug": f"q{i}", "title": f"a finished change {i}",
                                    "branch": f"b{i}", "created_at": time.time() - 7200}
                                   for i in range(n)])


def test_the_notice_list_is_one_list_from_two_sources(shim):
    """The dashboard asks one question and gets one answer. Half of it was
    derived from log rows in another process; half of it was derived here from
    state that never left. Nothing on the screen says which."""
    from app import db, monitor
    shim.error("sandbox", "escape", "wrote outside", files="x.py")     # a WATCH notice
    _stale_queue(db)                                                   # a LOCAL notice
    kinds = [n["kind"] for n in monitor.scan()]
    assert "sandbox" in kinds and "queue" in kinds


def test_the_composed_list_has_one_sort_order_worst_first(shim):
    from app import db, monitor
    shim.error("sandbox", "escape", "wrote outside", files="x.py")     # critical
    shim.log("quota", "rate_limited", "cooling", level="warn", model="m")   # info
    _stale_queue(db)                                                   # warning
    ranks = [monitor.SEVERITY_RANK[n["severity"]] for n in monitor.scan()]
    assert ranks == sorted(ranks), f"the two lists were concatenated, not merged: {ranks}"


def test_a_fingerprint_cannot_appear_twice(shim, monkeypatch):
    """The kinds are disjoint today, so a collision would mean both sides
    independently derived the same notice — in which case showing it twice is the
    bug, not showing it once."""
    from app import monitor
    shim.error("sandbox", "escape", "wrote outside", files="x.py")
    twin = dict(monitor.scan()[0])
    monkeypatch.setattr(monitor, "LOCAL_RULES", [lambda: [twin]])
    fps = [n["fp"] for n in monitor.scan()]
    assert len(fps) == len(set(fps)), f"duplicated: {fps}"


def test_a_dismissal_reaches_both_halves(shim):
    """One decisions store, one fingerprint space. A local notice is filtered
    against the same map the service filters its own by, which is why that map
    rides back on the notices call."""
    from app import db, monitor
    _stale_queue(db)
    n = next(x for x in monitor.scan() if x["kind"] == "queue")
    monitor.decide(n["fp"], "dismissed")
    assert not [x for x in monitor.scan() if x["kind"] == "queue"]
    assert [x for x in monitor.scan(include_decided=True) if x["kind"] == "queue"]
    assert watch_service.decisions()[n["fp"]]["state"] == "dismissed", \
        "the decision has to be in the store, not in this process"


def test_the_summary_counts_the_composed_list_not_half_of_it(shim):
    from app import db, monitor
    shim.error("sandbox", "escape", "wrote outside", files="x.py")
    _stale_queue(db)
    s = monitor.summary()
    assert s["total"] == len(monitor.scan()) == 2
    assert s["critical"] == 1 and s["warning"] == 1


def test_the_screens_payload_kept_every_key_it_had(root_client, shim):
    """The response shape is the contract with the dashboard. rpNotices reads
    `notices` and `auto`; the HQ panel renders the same objects; `actions` and
    `auto_safe` are what the Approve button's copy is built from."""
    shim.error("sandbox", "escape", "wrote outside", files="x.py")
    body = root_client.get("/api/logs/notices").json()
    assert set(body) >= {"notices", "summary", "actions", "auto", "auto_safe"}
    assert body["actions"] == ["abort_task", "file_task", "pause_repair", "set_knob"]
    assert body["auto_safe"] == ["file_task", "set_knob"]
    n = body["notices"][0]
    assert set(n) >= {"fp", "kind", "severity", "title", "detail", "evidence",
                      "proposal", "action", "params", "count", "since", "decision"}


# --- approve: act here, decide there -------------------------------------------

def test_approving_a_watch_notice_acts_locally_then_decides_remotely(shim, no_spend):
    """Both halves, asserted separately. The action ran against machinery that
    never left this process; the record of it is in a store that did."""
    import asyncio
    from app import monitor, repair
    repair.toggle(True)
    shim.error("sandbox", "escape", "wrote outside", files="x.py")
    n = next(x for x in monitor.scan() if x["kind"] == "sandbox")
    out = asyncio.run(monitor.approve(n["fp"]))
    assert out["ok"] and repair.enabled() is False, "the action must actually run"
    assert watch_service.decisions()[n["fp"]]["state"] == "approved", \
        "and the decision must be in the service's store"


def test_the_actions_all_target_machinery_that_stayed_here(shim):
    """The reason the levers did not move. Reading ACTIONS still tells you the
    whole blast radius of pressing Approve — and every entry reaches into a
    module in this process, one of which (save_backlog) has no HTTP endpoint at
    all and needs none."""
    from app import monitor
    assert set(monitor.ACTIONS) == {"pause_repair", "set_knob", "abort_task", "file_task"}
    body = "".join(re.sub(r'""".*?"""', "", __import__("inspect").getsource(fn), flags=re.S)
                   for fn in monitor.ACTIONS.values())
    assert "repair.toggle" in body and "tuning.set" in body
    assert "repair.abort" in body and "repair.save_backlog" in body
    assert "httpx" not in body, "an action that needed the wire would be an action that moved"


def test_a_local_notice_can_still_be_approved_with_the_service_down(dead_shim, no_spend):
    """The outage that would otherwise be worst: the inbox is half-blind AND the
    one notice it can still raise is the one that stops runaway work. The action
    runs; only the decision is lost, so the notice comes back — which is the
    honest failure, not a silent one."""
    import asyncio
    from app import db, monitor, repair
    repair.toggle(True)
    # Written straight into kv: set_state() stamps updated_at itself, which is
    # exactly the field this rule reads, so it cannot be used to age a phase.
    db.kv_set("repair:state", {**repair.state(), "phase": "build",
                               "updated_at": time.time() - 7200})
    ns = monitor.scan()
    assert [n["kind"] for n in ns] == ["stuck"], "the log-derived half is gone, the local half is not"
    out = asyncio.run(monitor.approve(ns[0]["fp"]))
    assert out["ok"] and out["did"] == "the task in flight was aborted"


def test_nothing_runs_unattended_on_a_permission_nobody_could_read(dead_shim):
    """`auto` lives in the store. When the store cannot be asked the answer is
    no — the failure mode of an unattended approver has to be "did not"."""
    import asyncio
    from app import monitor
    assert monitor.auto_on() is False
    assert asyncio.run(monitor.sweep()) == []


def test_the_standing_decision_round_trips_through_the_store(shim):
    from app import monitor
    assert monitor.auto_on() is False
    assert monitor.set_auto(True) is True
    assert monitor.auto_on() is True
    assert watch_service.auto_on() is True, "it has to be in the store, not in this process"
    assert monitor.set_auto(False) is False


# --- the split, pinned ---------------------------------------------------------

def _code(path: Path) -> str:
    """Source with docstrings stripped — these pins say "the code does not do
    this", and both modules explain at length what they deliberately do NOT do."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and ast.get_docstring(node):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_the_monitor_no_longer_reads_another_owners_kv_key_across_a_process():
    """The cross-owner read the split erased. `repair:queue` is written by the
    repair engine; the monitor used to open it directly, and had the monitor gone
    to a service that would have become a second process opening another's
    database. The rule stayed instead — same key, same process, same owner."""
    watch = _code(REPO / "services" / "watch" / "app.py")
    assert not re.search(r"""['"]repair:""", watch), \
        "the watch service opens one of the repair engine's kv keys"
    # The ONLY line that may name the conductor's database is the one-shot
    # first-boot copy, which is read-only, marked, and never runs again.
    conductor_db = [ln for ln in watch.splitlines() if "devteam.db" in ln]
    assert conductor_db == ["LEGACY_DB_PATH = Path(os.environ.get('LEGACY_DB_PATH', 'devteam.db'))"], \
        f"a second reach into the conductor's store: {conductor_db}"
    monitor = _code(REPO / "conductor" / "app" / "monitor.py")
    assert "repair:queue" in monitor, "the read belongs here, next to its owner"


def test_the_service_imports_nothing_from_the_conductor():
    src = (REPO / "services" / "watch" / "app.py").read_text()
    assert not re.search(r"^\s*(from|import)\s+(app|conductor)\b", src, re.M)
    assert not re.search(r"from\s+\.", src, re.M)


def test_the_log_rules_moved_whole_and_the_state_rules_did_not():
    """The criterion was mechanical: a rule that reads only log rows moved, and
    one that reads the conductor's own objects did not."""
    from app import monitor
    assert [r.__name__ for r in monitor.LOCAL_RULES] == ["_rule_queue", "_rule_stuck"]
    assert [r.__name__ for r in watch_service.RULES] == [
        "_rule_sandbox", "_rule_zombie", "_rule_dashboard", "_rule_build_deaths",
        "_rule_red_streak", "_rule_errors", "_rule_quota"]


# --- every degraded shape -------------------------------------------------------

def test_stdout_survives_the_service_the_3am_terminal_is_what_you_have(dead_shim,
                                                                       monkeypatch, capsys):
    monkeypatch.setattr(dead_shim, "ECHO", True)
    dead_shim.error("sandbox", "escape", "wrote into the live checkout", files="x.py")
    dead_shim.info("git", "landed", "a fix", sha="abc1234")
    out = capsys.readouterr()
    assert "sandbox/escape" in out.err and "files=x.py" in out.err
    assert "git/landed" in out.out and "sha=abc1234" in out.out


def test_degraded_reads_are_empty_and_admit_it(dead_shim):
    assert dead_shim.recent() == []
    assert dead_shim.rows() == [] and dead_shim.rows(errors_only=True) == []
    assert dead_shim.degraded() is True
    st = dead_shim.stats(3600)
    assert st["degraded"] is True
    for k in ("total", "errors"):
        assert st[k] == 0, f"{k} was invented"
    assert st["last_error"] is None and st["by_cat"] == {}


def test_a_degraded_stats_has_every_key_the_callers_read(shim, monkeypatch):
    """A missing key is a KeyError three frames away from the outage that caused
    it. The zero shape is compared against the real one, field for field."""
    real = set(shim.stats(3600))
    monkeypatch.setattr(shim, "_client", _dead_client)
    degraded = set(shim.stats(3600))
    assert real - degraded == set(), f"the degraded stats is missing {real - degraded}"


def test_the_notice_list_falls_back_to_the_local_rules_with_a_banner(dead_shim):
    from app import db, monitor
    _stale_queue(db)
    got = monitor.compose()
    assert [n["kind"] for n in got["notices"]] == ["queue"]
    assert got["degraded"] is True
    assert got["banner"] and "watch service" in got["banner"]


def test_an_empty_inbox_during_an_outage_cannot_be_read_as_all_is_well(root_client,
                                                                       dead_shim):
    """The one lie this screen could tell. `notices: []` and "nothing needs you"
    are the same picture, and only one of them is true when the detector is
    down."""
    body = root_client.get("/api/logs/notices").json()
    assert body["notices"] == [] and body["degraded"] is True
    assert body["banner"]
    logs_body = root_client.get("/api/logs").json()
    assert logs_body["logs"] == [] and logs_body["degraded"] is True and logs_body["banner"]
    assert logs_body["categories"], "the filter vocabulary is a constant, not a lookup"


def test_the_screen_shows_the_banner_it_is_given():
    """A field nothing renders is a field that does not exist."""
    from conftest import dashboard_js
    js = dashboard_js()
    assert "function rpBanner" in js and 'id="rpBanner"' in js
    assert "r.degraded ? r.banner" in js, "the sentence the server sent has to be the one shown"
    css = (REPO / "dashboard" / "style.css").read_text()
    assert ".rp-banner" in css, "an unstyled banner is a line of grey text nobody reads"


def test_the_outage_is_one_counted_line_not_one_per_call(shim, monkeypatch):
    """The notice panel polls. Five identical failures have to read as one fact
    with a count, or the record is unreadable exactly when someone needs it.

    Only the MONITOR's client is dead here — the ring itself is up, which is the
    honest shape of the drill: the module reporting the outage is a client of the
    same service, and the whole reason its warn still lands is that the log call
    does not depend on the notices call."""
    from app import monitor
    monkeypatch.setattr(monitor, "_client", _dead_client)
    shim._LAST.clear()
    for _ in range(5):
        monitor.compose()
    rows = [r for r in shim.recent(event="watch_degraded", limit=50)
            if r.get("verb") == "notices"]
    assert len(rows) == 1, f"{len(rows)} rows for five identical failures"
    assert rows[0].get("repeats") == 5 and rows[0]["level"] == "warn"


def test_the_degraded_note_never_goes_through_the_logger_itself(dead_shim):
    """A logger reporting its own outage through itself is a recursion, and the
    one moment it would fire is the one moment you cannot afford one."""
    body = _code(REPO / "conductor" / "app" / "logs.py")
    note = body.split("def _note(")[1].split("\ndef ")[0]
    assert "log(" not in note and "logs." not in note
    assert "sys.stderr" in note


def test_health_is_false_when_the_ring_is_down_and_true_when_it_is_not(shim, monkeypatch):
    from app import monitor
    assert shim.health() is True and monitor.health() is True
    monkeypatch.setattr(shim, "_client", _dead_client)
    monkeypatch.setattr(monitor, "_client", _dead_client)
    assert shim.health() is False and monitor.health() is False


# --- the wiring that makes URL mode the real mode -------------------------------

def test_gen_fleet_wires_the_url_and_asks_for_no_doors(tmp_path):
    """Proven against a temp root (never the real data/): the conductor's env
    names the watch peer — so every generated boot has WATCH_URL — and the
    service declares no doors at all, because detection reads log rows and
    nothing else."""
    import shutil
    sys.path.insert(0, str(REPO / "tools"))
    import gen_fleet
    shutil.copy(REPO / "services.yaml", tmp_path / "services.yaml")
    gen_fleet.generate(tmp_path, {})
    assert "WATCH_URL=http://127.0.0.1:8884" in \
        (tmp_path / "data/env/conductor.env").read_text()
    wenv = (tmp_path / "data/env/watch.env").read_text()
    assert "PORT=8884" in wenv and "DB_PATH=data/watch.db" in wenv
    assert (tmp_path / "data/tokens/watch.token").exists()
    topo = json.loads((tmp_path / "data/fleet_topology.json").read_text())["services"]
    assert topo["watch"]["port"] == 8884
    assert topo["watch"]["doors"] == [] and topo["watch"]["knobs"] == []


def test_the_committed_spec_is_what_the_service_serves():
    """oasdiff gates the diff between commits; this gates the diff between the
    file and the code that is supposed to implement it."""
    from fastapi.testclient import TestClient
    served = TestClient(watch_service.app).get("/openapi.json").json()
    assert served == json.loads((REPO / "services" / "watch" / "openapi.json").read_text())


# --- the cutover: no fallback left, and the four kv keys go ----------------------

def test_nothing_imports_the_deleted_fallbacks():
    """The files are gone; what matters is that no code still reaches for them.
    (Both shims name them in a docstring as history — a comment cannot import.)"""
    for legacy in ("_logs_legacy", "_monitor_legacy"):
        assert not (REPO / "conductor" / "app" / f"{legacy}.py").exists()
        importers = [str(f) for f in (REPO / "conductor").rglob("*.py")
                     if re.search(rf"^\s*(from|import).*{legacy}", f.read_text(), re.M)]
        assert importers == [], f"{legacy} still has importers: {importers}"


def test_neither_shim_can_fall_back_any_more():
    """One mode, decided nowhere: there is no branch left to take. A `sys.modules`
    swap surviving the cutover would mean a conductor that silently kept its own
    ring on a box where WATCH_URL happened to be unset."""
    for name in ("logs", "monitor"):
        code = _code(REPO / "conductor" / "app" / f"{name}.py")
        assert "sys.modules" not in code, f"{name}.py still installs a fallback"
        assert "_logs_legacy" not in code and "_monitor_legacy" not in code
        # ...and a missing URL can now do exactly one thing: refuse, once, at the
        # door. Never pick an implementation.
        assert code.count("if not _URL") <= 1, f"{name}.py branches on the URL more than once"
        if "if not _URL" in code:
            after = code.split("if not _URL", 1)[1][:80]
            assert "raise RuntimeError(_NO_URL)" in after, \
                f"{name}.py does something other than refuse when the URL is missing"


def test_without_the_url_init_refuses_and_says_where_to_look(fresh_db, monkeypatch):
    """There is no in-process ring any more, so a conductor with no service
    configured must fail at the door — loudly, naming the boot script — instead of
    running with no record of what it does."""
    from app import logs
    monkeypatch.setattr(logs, "_URL", "")
    with pytest.raises(RuntimeError) as e:
        logs.init()
    msg = str(e.value)
    assert "WATCH_URL" in msg and "run-local.sh" in msg and "services.yaml" in msg


def test_the_verbs_still_degrade_when_the_url_is_missing(fresh_db, monkeypatch):
    """init() is the loud door; the verbs stay soft. A door that also killed every
    later call would turn one misconfigured process into a crash loop — and would
    do it through the module every other module logs to."""
    from app import logs, monitor
    monkeypatch.setattr(logs, "_URL", "")
    monkeypatch.setattr(logs, "_client", _dead_client)
    monkeypatch.setattr(monitor, "_client", _dead_client)
    monkeypatch.setattr(logs, "ECHO", False)
    logs.error("sandbox", "escape", "wrote outside")      # no raise IS the assertion
    assert logs.recent() == [] and logs.rows() == []
    assert logs.stats(3600)["degraded"] is True
    assert monitor.compose()["degraded"] is True


def test_the_conductor_boots_all_four_extracted_stores_loudly():
    """Each init() is the door that refuses. A store whose init is never called
    would degrade silently forever instead of failing on the first boot."""
    main = (REPO / "conductor" / "app" / "main.py").read_text()
    for mod in ("knowledge", "usage", "notify", "logs"):
        assert f"{mod}.init()" in main, f"{mod}.init() is not called at boot"


def test_init_drops_the_four_migrated_keys(fresh_db):
    """They are a second, staler copy of state another process owns. Left behind,
    a reader would reasonably conclude the ring still lives here."""
    from app import db, logs
    db.kv_set("logs:ring", [{"ts": 1.0, "cat": "git", "event": "landed"}])
    db.kv_set("logs:errors", [{"ts": 1.0, "cat": "sandbox", "event": "escape"}])
    db.kv_set("monitor:decisions", {"abc": {"state": "dismissed", "ts": 1.0}})
    db.kv_set("monitor:auto", True)
    logs.init()
    for key in logs._MIGRATED_KEYS:
        assert db.kv_get(key) is None, f"{key} survived the cutover"
    logs.init()             # and a box that never had them boots fine


def test_the_drop_waits_until_the_service_has_copied_them(fresh_db, monkeypatch):
    """The scrutiny this key set actually needed. Nothing orders the two
    processes — process-compose starts them together with no depends_on — so on
    the first boot after the cutover this can run before the service has
    attached. Deleting `monitor:decisions` then does not lose data anyone can
    shrug at: it loses every answer the owner has ever given, and the next scan
    asks all of them again at once. That storm is the exact failure the copy
    exists to prevent.
    """
    from app import db, logs
    db.kv_set("monitor:decisions", {"abc": {"state": "dismissed", "ts": 1.0}})
    db.kv_set("monitor:auto", True)

    def _not_yet(request):
        return httpx.Response(200, json={"ok": True, "service": "watch",
                                         "db": "x", "checks": {}, "backfilled": False})
    monkeypatch.setattr(logs, "_client",
                        lambda: httpx.Client(base_url="http://watch.test",
                                             transport=httpx.MockTransport(_not_yet)))
    logs.init()
    assert db.kv_get("monitor:decisions"), "the owner's answers were dropped mid-copy"
    assert db.kv_get("monitor:auto") is True

    # ...and an unreachable service is the same answer, not a crash: a conductor
    # that refused to boot because a PEER was starting is how a fleet rings down.
    monkeypatch.setattr(logs, "_client", _dead_client)
    logs.init()
    assert db.kv_get("monitor:decisions"), "an outage was treated as permission to delete"

    # ...and once it says yes, they go.
    from conftest import _svc_client, _WATCH_URL
    monkeypatch.setattr(logs, "_client",
                        lambda: _svc_client(watch_service, _WATCH_URL, WATCH_TEST_TOKEN))
    logs.init()
    assert db.kv_get("monitor:decisions") is None and db.kv_get("monitor:auto") is None


def test_the_service_settles_its_marker_either_way(tmp_path, monkeypatch):
    """`backfilled` has to mean "the decision has been made", not "rows were
    copied" — otherwise a box that never had a legacy database would carry four
    dead keys forever, waiting for a copy that is never coming."""
    monkeypatch.setattr(watch_service, "LEGACY_DB_PATH", tmp_path / "nothing.db")
    monkeypatch.setattr(watch_service.helpers, "DB_PATH", tmp_path / "w.db")
    monkeypatch.setattr(watch_service.helpers, "_conn", None)
    watch_service.init_store()
    assert watch_service.settled() is False
    assert watch_service.backfill_from_legacy() == 0
    assert watch_service.settled() is True, "nothing to copy is still an answer"
    assert watch_service.health()["backfilled"] is True
    assert "no legacy database" in watch_service.helpers.kv_get("backfilled_from")


def test_a_transient_copy_failure_leaves_the_keys_alone(tmp_path, monkeypatch):
    """The one case that must NOT settle: a locked file or a conductor mid-write
    is worth retrying, and the keys have to still be there when it is."""
    legacy = tmp_path / "devteam.db"
    con = sqlite3.connect(legacy)
    con.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    con.execute("INSERT INTO kv VALUES (?,?)", ("monitor:auto", "true"))
    con.commit()
    con.close()
    monkeypatch.setattr(watch_service, "LEGACY_DB_PATH", legacy)
    monkeypatch.setattr(watch_service.helpers, "DB_PATH", tmp_path / "w2.db")
    monkeypatch.setattr(watch_service.helpers, "_conn", None)
    watch_service.init_store()

    def _boom(*a, **k):
        raise sqlite3.OperationalError("database is locked (drill)")
    monkeypatch.setattr(watch_service, "_legacy_value", _boom)
    assert watch_service.backfill_from_legacy() == 0
    assert watch_service.settled() is False, "a retryable failure must not settle"
