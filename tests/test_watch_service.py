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
    note = body.split("def _note(")[1].split("\n    def ")[0]
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


# --- the rollback path still exists (deleted in commit B) ------------------------

def test_the_vendored_fallback_still_runs_in_process(fresh_db):
    """Between commit A and commit B, unsetting WATCH_URL must give back the
    pre-P3 behaviour exactly — that IS the rollback. Exercised here rather than
    assumed, because a rollback nobody has run is a hope."""
    from app import _logs_legacy as legacy, _monitor_legacy as mon_legacy, db
    legacy.ECHO = False
    # In real fallback mode logs.py replaces ITSELF in sys.modules with the legacy
    # module, so `from . import logs` inside _monitor_legacy yields exactly this
    # object. Here both modules are loaded in URL mode, so the binding is made by
    # hand — otherwise the legacy monitor would read the SERVICE's rows and the
    # drill would prove nothing about the rollback.
    mon_legacy.logs = legacy
    try:
        legacy.log("sandbox", "escape", "wrote outside", level="error", files="x.py")
        assert [r["event"] for r in legacy.rows()] == ["escape"]
        assert db.kv_get(legacy.RING_KEY), "the fallback still writes the old kv blob"
        assert any(n["kind"] == "sandbox" for n in mon_legacy.scan())
        assert mon_legacy.decisions() == {}
        mon_legacy.decide("abc", "dismissed")
        assert db.kv_get(mon_legacy.DECISIONS_KEY)["abc"]["state"] == "dismissed"
    finally:
        legacy.ECHO = True
        from app import logs as _url_mode_logs
        mon_legacy.logs = _url_mode_logs


def test_the_fallback_answers_the_same_route_surface(fresh_db):
    """logs_routes.py has ONE shape and both modes have to satisfy it. When the
    route gained `degraded`/`banner` and the one-call `compose`, the vendored
    bodies gained honest answers for them — otherwise unsetting WATCH_URL would
    500 the very screen the rollback exists to keep working. Drilled by name,
    because "the rollback boots" and "the rollback serves" are different claims.
    """
    from app import _logs_legacy as legacy, _monitor_legacy as mon_legacy
    from app import logs_routes
    src = (REPO / "conductor" / "app" / "logs_routes.py").read_text()
    for name in re.findall(r"\blogs\.([a-zA-Z_]+)", src):
        assert hasattr(legacy, name), f"the fallback has no logs.{name}"
    for name in re.findall(r"\bmonitor\.([a-zA-Z_]+)", src):
        assert hasattr(mon_legacy, name), f"the fallback has no monitor.{name}"
    assert legacy.degraded() is False and legacy.BANNER == "" and legacy.flush() == 0
    got = mon_legacy.compose()
    assert set(got) == {"notices", "summary", "degraded", "banner"}
    assert got["degraded"] is False, "the detector IS the process asking; it cannot be down"
    assert logs_routes.router.prefix == "/api/logs"


def test_both_shims_are_dual_mode_until_the_cutover():
    for name, legacy in (("logs", "_logs_legacy"), ("monitor", "_monitor_legacy")):
        src = (REPO / "conductor" / "app" / f"{name}.py").read_text()
        assert legacy in src and 'os.environ.get("WATCH_URL")' in src
        assert (REPO / "conductor" / "app" / f"{legacy}.py").exists()


def test_the_four_kv_keys_are_named_where_commit_b_will_drop_them():
    """They still exist in devteam.db between the commits, because that is what
    the rollback reads. Naming them here is the note to the next commit."""
    src = (REPO / "conductor" / "app" / "logs.py").read_text() \
        + (REPO / "conductor" / "app" / "monitor.py").read_text()
    for key in ("logs:ring", "logs:errors", "monitor:decisions", "monitor:auto"):
        assert key in src, f"{key} has no home and no note saying where it went"
