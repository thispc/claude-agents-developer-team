"""logs.py — one pipeline for everything the backend has to say about itself.

Before this there were three channels and no discipline. `print()` went to whatever terminal
the server happened to be started from and was gone the moment it scrolled. `bus.emit` is a
NARRATIVE channel: it exists to tell a human what their team is doing, and using it for
operational detail buries the story under plumbing. And a handful of failures were written to
one kv key that only ever held the most recent one, so the second error erased the first.

The distinction this module draws:

    bus.emit   what happened, told as a story, per project, for the person watching
    logs.log   what the system did and whether it worked, for whoever is diagnosing it

A row is `{ts, level, cat, event, msg, ...fields}`. Three of those matter:

    cat    a SEMANTIC category — what KIND of fact this is, never which module said it.
           "git" and "quota" are answers to "what should I look at"; "repair_builder" is
           not, and a category named after a module goes stale the moment code moves.
    event  a stable slug you can filter and count on (`session_died`, `sandbox_escape`).
           Machine-readable, so monitoring can watch one without matching prose.
    msg    one sentence for a human. Prose changes freely; `event` does not.

Since P3 the ring itself is a SERVICE — services/watch, its own process on 8884, its own
`data/watch.db`, its own committed contract — and this file is the client that reaches it,
keeping every public name the in-process module ever had (log / debug / info / warn / error /
rows / recent / stats / CATEGORIES / LEVELS / ECHO) so nothing above it had to learn that the
ring moved.

WHY IT MOVED. Storage was one capped kv value rewritten whole on every single line: read the
list, append, write it back, under a thread lock. A thread lock is not a process lock, and the
moment the platform became a fleet the lost update stopped being theoretical — on the record
you go to precisely when you are trying to find out what happened. The service replaces the
blob with real capped tables and one INSERT per line.

DUAL-MODE, for the strangler window between commit A and commit B:

  WATCH_URL set    → the batching client below. gen_fleet writes the URL into
                     data/env/conductor.env from services.yaml, so a fleet boot
                     (./run-local.sh) is in this mode by construction.
  WATCH_URL unset  → the old in-process body, vendored unchanged in _logs_legacy.py,
                     INSTALLED WHOLESALE: this module replaces itself in sys.modules with
                     the legacy module, so the fallback is byte-identical to pre-P3 — same
                     functions, same kv blobs, same introspectable source. That is the
                     rollback between the two commits; commit B deletes it, and with it the
                     `logs:ring` and `logs:errors` keys in devteam.db.

The mode is decided at import: a process boots into one world and stays there.

--- THE HOT PATH, which is the whole design problem -------------------------------------

There are 64 fire-and-forget call sites. Some are inside exception handlers. Some are in a
loop that ticks every twenty seconds, forever. Not one of them can afford an HTTP round-trip,
and not one of them checks a return value — a logger that got slower would degrade the thing
it exists to observe, and a logger that raised would take down the code that was already
failing. So `log()` NEVER does I/O to the service:

    STDOUT ECHO STAYS LOCAL. Every line still goes to this process's own stdout, exactly as
    before. process-compose collects it into data/logs/fleet.log, and when something is wrong
    at 3am the terminal is what you have — it must not depend on another process being up.

    THE ROW GOES ON AN IN-MEMORY QUEUE. Append to a deque: no lock, no allocation beyond the
    row, no syscall.

    A DAEMON THREAD FLUSHES IT. Up to BATCH_ROWS rows per POST, or every FLUSH_INTERVAL_S,
    whichever comes first. 100 lines then cost one round-trip instead of 100.

    OVERFLOW DROPS, LOUDLY BUT ONCE. Beyond MAX_QUEUE rows the newest are dropped with a
    single stderr note per window — never through logs.* itself, which would recurse. Losing
    rows that are already on stdout is the correct trade against unbounded memory in a
    process whose logger cannot be allowed to be the thing that fails.

    A READ FLUSHES FIRST. rows/recent/stats — and the monitor's notice scan — drain the queue
    before asking, so you always read your own writes. Reads are rare (a poll from one screen)
    and writes are constant; paying on the rare side is the whole shape of this module.

`LOG_CALL_BUDGET_S` is the promise, drilled in tests/test_watch_service.py with the service
UNREACHABLE, which is when a naive client would be slowest.

DEDUPE. `dedupe_s` is for the never-die loops: a loop that ticks every 20 seconds and finds
the same thing wrong writes the same line 180 times an hour, and the record is unreadable
exactly when someone needs it. The RING's half of that now happens in the service, against the
stored row, so two processes hitting the same fault collapse into one line with a count of two
instead of two lines that each look like a single incident. What stays here is only the ECHO's
half: `_LAST` suppresses the repeated terminal line, so the 3am terminal stays as readable as
it was.

DEGRADED MODES (service down — the stdout half of this module keeps working throughout):
    log/debug/info/warn/error → unchanged. Echo to stdout, queue the row. When the queue
                fills, the newest rows are dropped with one stderr note; when the service
                comes back the queue flushes.
    rows      → []                       with one deduped stderr note
    recent    → []                       same
    stats     → zeros plus "degraded": True, with every key a caller reads present and not
                one of them invented
Never a raise, from any of them, ever. That rule outranks the record.
"""

from __future__ import annotations

import collections
import os
import sys
import threading
import time

_URL = (os.environ.get("WATCH_URL") or "").strip().rstrip("/")

if not _URL:
    # Fallback mode: BE the legacy module. The import system re-reads sys.modules
    # after executing this file, so `from . import logs` everywhere yields the
    # legacy module itself — its functions, constants and source, unchanged.
    from . import _logs_legacy as _legacy
    sys.modules[__name__] = _legacy

else:
    import atexit

    import httpx

    from . import config

    LEVELS = ("debug", "info", "warn", "error")
    _RANK = {name: i for i, name in enumerate(LEVELS)}

    # The vocabulary. Adding a category is a deliberate act: it is the axis someone filters on
    # when they are trying to find out what broke, so it has to answer "what should I look at",
    # not "which file was running".
    #
    # It lives on BOTH sides of the wire and must not drift: coercion happens here (a row is
    # built and returned to the caller before anything is sent), the dashboard's filter list is
    # served from here, and the service coerces again because a contract cannot trust its
    # callers. A drill in tests/test_watch_service.py asserts the two copies are identical — a
    # silently diverged vocabulary is a filter that quietly stops matching.
    CATEGORIES: dict[str, str] = {
        "lifecycle": "the process itself — startup, shutdown, restarts, which server owns the engine",
        "sprint":    "the self-repair crew's phase machine: what it decided to do next",
        "session":   "model sessions — started, finished, died, and how long they ran",
        "spend":     "what a session consumed, and against whose share of the window",
        "sleep":     "standing down, and the reason it will wake",
        "quota":     "rate limits and cooldowns, in the provider's own words",
        "git":       "worktrees, branches, merges, reverts",
        "verify":    "test-suite runs and their verdicts",
        "sandbox":   "isolation — protected paths, and anything that wrote where it should not",
        "http":      "failures on the API surface",
        "data":      "schema, migrations, storage",
        "auth":      "access decisions worth keeping a record of",
    }

    # Set false in tests that assert on stdout, or when a caller wants the queue only.
    ECHO = True

    # --- the promise ----------------------------------------------------------
    #
    # One log call, wall clock, with the service unreachable. A millisecond is
    # ~1000x what the work actually costs (build a dict, write a line, append to
    # a deque) and still small enough that the 20s engine tick could make a
    # thousand of them and lose a second — which it never does.
    LOG_CALL_BUDGET_S = 0.001

    BATCH_ROWS = 100            # rows per POST; also the "flush now" trigger
    FLUSH_INTERVAL_S = 0.5      # ...or this often, whichever comes first
    MAX_QUEUE = 1000            # beyond this the newest rows are dropped
    _TIMEOUT = 2.0              # a wedged ring must never hold the flusher

    # One stderr note per window for the whole degraded story. Deduped by time
    # because the failure that makes it fire is exactly the failure that repeats.
    DEGRADED_NOTE_S = 300

    # Tests set this False so posting happens on the calling thread inside
    # flush() — a background thread racing a fixture that empties the service's
    # tables is a flake, not a test. The thread itself is drilled explicitly.
    AUTOFLUSH = True

    # Tests inject an httpx transport here (one that refuses, for the outage
    # drills) — the client code path stays identical.
    _TRANSPORT: httpx.BaseTransport | None = None
    _TOKEN = ""

    _QUEUE: collections.deque = collections.deque()
    _WAKE = threading.Event()
    # Two locks, deliberately. _FLUSH_LOCK is held across a POST — up to _TIMEOUT
    # — and the write path must never wait on it, so starting the daemon takes a
    # different one. Sharing them would let the first log call after a read that
    # hit a hung service block for two seconds, which is exactly the promise this
    # module makes and would have broken.
    _FLUSH_LOCK = threading.Lock()
    _START_LOCK = threading.Lock()
    _STOP = threading.Event()
    _THREAD: threading.Thread | None = None
    _dropped = 0
    _last_note = 0.0
    # Whether the LAST read reached the service. An empty log view and a log view
    # that could not be read look identical on screen, and the difference is the
    # whole question ("nothing happened" vs "you cannot see what happened") — so
    # the routes carry this into the answer instead of leaving the screen to
    # guess. Set by the read verbs; costs no extra round-trip.
    _degraded_read = False

    # Echo dedupe only — see the module docstring. The ring's dedupe is the
    # service's, against the stored row.
    _LAST: dict[tuple, float] = {}

    def _print(row: dict) -> None:
        when = time.strftime("%H:%M:%S", time.localtime(row["ts"]))
        extra = " ".join(f"{k}={v}" for k, v in row.items()
                         if k not in ("ts", "level", "cat", "event", "msg"))
        line = f"{when} {row['level'].upper():<5} {row['cat']}/{row['event']}  {row['msg']}"
        print(f"{line}  {extra}" if extra else line,
              file=sys.stderr if row["level"] in ("warn", "error") else sys.stdout, flush=True)

    def _note(msg: str) -> None:
        """The degraded channel, on stderr and NEVER through logs.* — a logger
        reporting its own outage through itself is a recursion, and the one time
        it would fire is the one time you cannot afford one."""
        global _last_note
        now = time.time()
        if now - _last_note < DEGRADED_NOTE_S:
            return
        _last_note = now
        try:
            print(f"[logs] {msg}", file=sys.stderr, flush=True)
        except Exception:
            pass

    # --- the client -----------------------------------------------------------

    def _token() -> str:
        """The service's own token, read from where gen_fleet minted it — the same
        resolution the /svc gateway uses (routes/svc.py)."""
        global _TOKEN
        if not _TOKEN:
            try:
                _TOKEN = (config.ROOT / "data" / "tokens" / "watch.token") \
                    .read_text().strip()
            except OSError:
                _TOKEN = ""
        return _TOKEN

    def _client() -> httpx.Client:
        return httpx.Client(base_url=_URL, timeout=_TIMEOUT, transport=_TRANSPORT,
                            headers={"X-Service-Token": _token()})

    # --- the queue ------------------------------------------------------------

    def _enqueue(row: dict, dedupe_s: float) -> None:
        """Append, and nothing else. No lock: deque.append and len() are atomic
        under the GIL, and a lock on the hot path would let a slow flush block a
        caller inside an exception handler."""
        global _dropped
        if len(_QUEUE) >= MAX_QUEUE:
            _dropped += 1
            _note(f"the watch service is not taking rows — {_dropped} dropped "
                  f"since the queue filled (they are still on stdout above)")
            return
        if dedupe_s:
            row = {**row, "dedupe_s": float(dedupe_s)}
        _QUEUE.append(row)
        if AUTOFLUSH:
            if _THREAD is None:
                _start()
            if len(_QUEUE) >= BATCH_ROWS:
                _WAKE.set()             # a full batch does not wait out the timer

    def _start() -> None:
        global _THREAD
        with _START_LOCK:
            if _THREAD is not None:
                return
            t = threading.Thread(target=_run, name="logs-flush", daemon=True)
            _THREAD = t
            t.start()

    def _run() -> None:
        """Daemon: every FLUSH_INTERVAL_S, or the moment a batch is full."""
        while not _STOP.is_set():
            _WAKE.wait(FLUSH_INTERVAL_S)
            _WAKE.clear()
            try:
                flush()
            except Exception:           # a flusher that can die is a flusher that will
                pass

    def stop() -> int:
        """Stand the flusher down and send what it was holding. Returns the rows
        accepted by that last flush.

        Registered with atexit, because the thread is a daemon and nothing joins
        it: without this, up to FLUSH_INTERVAL_S of rows — including whatever the
        process said on its way down, which is the batch you most want — would go
        no further than stdout.
        """
        global _THREAD
        _STOP.set()
        _WAKE.set()
        t, _THREAD = _THREAD, None
        if t is not None:
            t.join(timeout=2)
        n = flush()
        _STOP.clear()               # a later _start() has to be able to work
        return n

    def _drain(limit: int) -> list[dict]:
        out = []
        for _ in range(limit):
            try:
                out.append(_QUEUE.popleft())
            except IndexError:
                break
        return out

    def _requeue(batch: list[dict]) -> None:
        """Put a failed batch back at the FRONT, so an outage costs order but not
        content: when the service comes back the queue flushes what it held.
        Bounded by the same ceiling — a service that stays down still cannot grow
        this process's memory without limit."""
        for row in reversed(batch):
            if len(_QUEUE) >= MAX_QUEUE:
                break
            _QUEUE.appendleft(row)

    def flush() -> int:
        """Send what is queued. Returns the number of rows accepted.

        Called by the daemon on its timer, by every read before it asks (so you
        read your own writes), and once by atexit. Held under one lock so a read
        and the daemon cannot interleave two half-batches; the WRITE path never
        touches this lock, which is what keeps the budget.
        """
        sent = 0
        with _FLUSH_LOCK:
            while True:
                batch = _drain(BATCH_ROWS)
                if not batch:
                    break
                try:
                    with _client() as c:
                        c.post("/logs", json={"rows": batch}).raise_for_status()
                    sent += len(batch)
                except Exception as e:
                    _requeue(batch)
                    _note(f"cannot reach the watch service — {len(_QUEUE)} rows waiting "
                          f"({type(e).__name__}: {str(e)[:120]})")
                    break               # do not spin against a service that is down
        return sent

    atexit.register(stop)

    # --- writing --------------------------------------------------------------

    def log(cat: str, event: str, msg: str = "", level: str = "info",
            dedupe_s: int = 0, **fields) -> dict:
        """Record one fact. Never raises: a logger that can take the process down
        with it is worse than no logger, and this one is called from inside
        exception handlers. Never blocks either — see LOG_CALL_BUDGET_S.
        """
        try:
            row = {"ts": time.time(), "level": level if level in LEVELS else "info",
                   "cat": cat if cat in CATEGORIES else "lifecycle",
                   "event": str(event)[:60], "msg": str(msg)[:600]}
            for k, v in (fields or {}).items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    row[str(k)[:24]] = v if not isinstance(v, str) else v[:300]
                else:
                    # Coerced HERE and not on the wire: the service's body is
                    # JSON, and one un-serialisable field would cost the whole
                    # batch rather than one value.
                    row[str(k)[:24]] = str(v)[:300]
        except Exception:
            return {"ts": time.time(), "level": "info", "cat": "lifecycle",
                    "event": str(event)[:60], "msg": ""}
        try:
            echo = True
            if dedupe_s > 0:
                key = (row["cat"], row["event"], row["msg"])
                if row["ts"] - _LAST.get(key, 0) < dedupe_s:
                    echo = False        # the terminal has already said this
                else:
                    _LAST[key] = row["ts"]
            if ECHO and echo:
                _print(row)
            _enqueue(row, dedupe_s)
        except Exception:
            pass
        return row

    def debug(cat: str, event: str, msg: str = "", dedupe_s: int = 0, **f) -> dict:
        return log(cat, event, msg, "debug", dedupe_s, **f)

    def info(cat: str, event: str, msg: str = "", dedupe_s: int = 0, **f) -> dict:
        return log(cat, event, msg, "info", dedupe_s, **f)

    def warn(cat: str, event: str, msg: str = "", dedupe_s: int = 0, **f) -> dict:
        return log(cat, event, msg, "warn", dedupe_s, **f)

    def error(cat: str, event: str, msg: str = "", dedupe_s: int = 0, **f) -> dict:
        return log(cat, event, msg, "error", dedupe_s, **f)

    # --- reading --------------------------------------------------------------

    # The service caps a page at 1000 rows; the ring holds 3000. Nobody reads
    # more than the tail — `rows()` exists for the error list and for tests, and
    # the screen asks for 300.
    _PAGE = 1000

    def rows(errors_only: bool = False) -> list[dict]:
        """The ring's tail, oldest first — the shape the in-process list had."""
        return recent(limit=_PAGE, errors_only=errors_only)

    def recent(level: str = "", cat: str = "", event: str = "", q: str = "",
               since: float = 0.0, limit: int = 200, errors_only: bool = False) -> list[dict]:
        """The tail, filtered. `level` is a FLOOR (warn returns warn and error),
        because the question is always "show me things at least this bad", never
        "only warnings". Filtered by the service, next to the rows."""
        global _degraded_read
        flush()
        try:
            with _client() as c:
                r = c.get("/logs", params={
                    "level": level, "cat": cat, "event": event, "q": q,
                    "since": float(since or 0), "limit": max(1, min(int(limit), _PAGE)),
                    "errors_only": bool(errors_only)})
                r.raise_for_status()
                _degraded_read = False
                return list(r.json().get("logs") or [])
        except Exception as e:
            _degraded_read = True
            _note(f"cannot read the ring — the log view is empty while the watch "
                  f"service is down ({type(e).__name__}: {str(e)[:120]})")
            return []

    def _zero_stats(window_s: int) -> dict:
        """What the ring says when it cannot say anything. Every key a caller
        reads is present — a missing key is a KeyError three frames away from the
        outage that caused it — and not one of them is invented."""
        return {"window_s": max(60, int(window_s)), "total": 0, "by_cat": {},
                "by_level": {name: 0 for name in LEVELS}, "errors": 0,
                "last_error": None, "categories": CATEGORIES, "degraded": True}

    def stats(window_s: int = 3600, now: float | None = None) -> dict:
        """What monitoring reads: how much of each kind, how bad, and what broke
        most recently. The point of a category vocabulary is that this is
        answerable at all — "14 errors" is a number, "14 errors, all sandbox" is
        a diagnosis."""
        global _degraded_read
        flush()
        try:
            with _client() as c:
                r = c.get("/logs/stats", params={"window_s": max(0, int(window_s)),
                                                 "now": float(now or 0)})
                r.raise_for_status()
                _degraded_read = False
                return r.json()
        except Exception as e:
            _degraded_read = True
            _note(f"cannot read the ring — log stats are zeros while the watch "
                  f"service is down ({type(e).__name__}: {str(e)[:120]})")
            return _zero_stats(window_s)

    def degraded() -> bool:
        """Did the last read reach the ring? The screen needs to tell "nothing
        happened" from "you cannot see what happened", and an empty list says
        both."""
        return bool(_degraded_read)

    # The one sentence the screen shows when it did not. Kept beside the flag so
    # the wording and the condition cannot drift apart.
    BANNER = ("The watch service is not answering, so there is nothing to show here — "
              "this is not the same as a quiet system. Rows are still going to this "
              "process's stdout, and will be sent when it comes back.")

    def health() -> bool:
        """Is the ring actually answering? The service's own /health — the same
        endpoint process-compose probes — asked through this door rather than
        around it, so there is exactly one place that knows the URL."""
        try:
            with _client() as c:
                r = c.get("/health")
                return r.status_code == 200 and bool(r.json().get("ok"))
        except Exception:
            return False
