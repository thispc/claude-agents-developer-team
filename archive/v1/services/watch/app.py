"""The watch service — the platform's memory of what it did, and what it noticed.

Two things that were always one idea, and are now one process: the LOG PIPELINE
(every fact the backend has to say about itself) and the MONITOR (what those
facts add up to). Nobody reads 3000 log rows; the value of keeping them is that
something else can turn them into a short list of NOTICES a person would actually
want to know, each carrying the evidence behind it.

    row      {ts, level, cat, event, msg, ...fields} — what the system did, and
             whether it worked. `cat` is a SEMANTIC category ("git", "quota"),
             never a module name; `event` is a stable machine-readable slug;
             `msg` is one sentence for a human, free to change.
    notice   derived on read from the rows in the window, so one that no longer
             applies simply stops being reported. Nothing to clean up, and the
             list can never show a problem that has already gone away.
    decision what the human said about a notice. THAT is the part worth
             persisting: dismissing silences a fingerprint, approving records
             that its action ran. Otherwise the same question is asked every
             thirty seconds forever.

WHAT THIS SERVICE IS NOT ALLOWED TO DO (the P3 split, and the reason it is safe
to run the detection somewhere else). This service owns FACTS and DETECTION. It
owns no lever. Every action a notice can propose targets machinery that lives in
the conductor — pausing the self-repair crew, aborting the task in flight,
nudging a tuning knob, filing a bug on the crew's backlog — so the JUDGMENT half
(`monitor.ACTIONS`, `approve()`, `sweep()`, `AUTO_SAFE`) stayed there. A notice
here is a sentence and a proposal; it is never a switch. The conductor resolves
the notice, runs the action against its own machinery, and only then POSTs the
decision back to `POST /notices/{fp}/decide`.

The two rules that read the conductor's own repair state rather than log rows —
"changes waiting for you" (the review queue) and "stuck in a phase" (the engine's
own clock) — stayed conductor-side as LOCAL RULES for the same reason, and that
is what erased the last cross-owner kv read in the platform: the monitor used to
reach into `repair:queue`, a key another module owns.

WHY IT IS TABLES AND NOT A BLOB (the same lesson as the quota meter). The ring
was ONE kv value in the conductor's database, rewritten whole on every single
log line: read the list, append, write the list back, under a thread lock. A
thread lock is not a process lock. The moment the platform became a fleet, two
processes logging at the same instant meant one of them silently vanished — on
the record you go to when you are trying to find out what happened. Here the ring
is a REAL capped table and a log line is one INSERT.

DEDUPE LIVES HERE, because the ring does. A never-die loop that ticks every 20
seconds and finds the same thing wrong writes the same line 180 times an hour,
and the record it produces is unreadable exactly when someone needs it. A repeat
inside `dedupe_s` bumps a counter on the row already there — checked against the
STORED row, so two processes reporting the same fault collapse into one line
instead of two.

FIRST BOOT copies the conductor's four kv keys (`logs:ring`, `logs:errors`,
`monitor:decisions`, `monitor:auto`) over a read-only ATTACH, once. The decisions
are the reason it is not optional: without them the extraction itself would be a
notice storm, every already-dismissed notice looking new again on the first boot
after the cutover.
"""

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


class StrictBody(BaseModel):
    """Strict validation: the contract says integer, so "5" is a 422, not a
    quiet coercion — lax mode is how a spec and a service drift apart."""
    model_config = ConfigDict(strict=True)


def _json_int(v):
    """JSON Schema's `integer` means "a number with zero fractional part" — 24.0
    qualifies — while Python's strict mode wants the int type itself. The
    contract is the spec, so meet ITS semantics exactly."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _json_float(v):
    """The mirror of the above: a JSON `number` accepts 3 as well as 3.0, and
    strict mode alone would 422 the integer."""
    if isinstance(v, int) and not isinstance(v, bool):
        return float(v)
    return v


JsonInt = Annotated[int, BeforeValidator(_json_int)]
JsonFloat = Annotated[float, BeforeValidator(_json_float)]

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))       # so `uvicorn app:app` works from any cwd
import helpers                      # vendored per service — never imported across services

SERVICE = os.environ.get("SERVICE_NAME", HERE.name)
SPEC = HERE / "openapi.json"
# The conductor's monolith store, for the one-time first-boot copy. Relative to
# the cwd process-compose runs from (the repo root) — same convention as DB_PATH.
LEGACY_DB_PATH = Path(os.environ.get("LEGACY_DB_PATH", "devteam.db"))
LEGACY_RING_KEY = "logs:ring"
LEGACY_ERR_KEY = "logs:errors"
LEGACY_DECISIONS_KEY = "monitor:decisions"
LEGACY_AUTO_KEY = "monitor:auto"

W_SCHEMA = """
CREATE TABLE IF NOT EXISTS log_rows (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    level   TEXT    NOT NULL DEFAULT 'info',
    cat     TEXT    NOT NULL DEFAULT 'lifecycle',
    event   TEXT    NOT NULL DEFAULT '',
    msg     TEXT    NOT NULL DEFAULT '',
    repeats INTEGER NOT NULL DEFAULT 1,     -- >1 only when a dedupe window collapsed repeats
    fields  TEXT    NOT NULL DEFAULT '{}'   -- the caller's own key/values, JSON
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON log_rows(ts);
CREATE INDEX IF NOT EXISTS idx_log_dedupe ON log_rows(cat, event, ts);
CREATE TABLE IF NOT EXISTS error_rows (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL    NOT NULL,
    level   TEXT    NOT NULL DEFAULT 'error',
    cat     TEXT    NOT NULL DEFAULT 'lifecycle',
    event   TEXT    NOT NULL DEFAULT '',
    msg     TEXT    NOT NULL DEFAULT '',
    repeats INTEGER NOT NULL DEFAULT 1,
    fields  TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_err_ts ON error_rows(ts);
CREATE TABLE IF NOT EXISTS decisions (
    fp    TEXT PRIMARY KEY,
    state TEXT NOT NULL,                    -- dismissed | approved | acknowledged
    ts    REAL NOT NULL,
    note  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
"""

LEVELS = ("debug", "info", "warn", "error")
_RANK = {name: i for i, name in enumerate(LEVELS)}

# The vocabulary. Adding a category is a deliberate act: it is the axis someone
# filters on when they are trying to find out what broke, so it has to answer
# "what should I look at", not "which file was running". Duplicated in the
# conductor's shim, which is where the coercion happens on the way in and where
# the dashboard's filter list comes from; a drill in tests/test_watch_service.py
# asserts the two copies are identical, because a silently diverged vocabulary is
# a filter that quietly stops matching.
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

MAX_ROWS = 3000        # a few days of ordinary operation
MAX_ERRORS = 300       # errors keep a longer memory of their own
# A decision older than this is about a notice whose window closed long ago. Kept
# generous: the cost of remembering a dismissal too long is one notice you have
# to dismiss again, and the cost of forgetting it too early is the storm this
# store exists to prevent.
DECISION_RETENTION_S = 90 * 86400


# --- this service's own store -------------------------------------------------

def _execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    con = helpers.db()
    cur = con.execute(sql, params)
    con.commit()
    return cur


def _query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in helpers.db().execute(sql, params).fetchall()]


def init_store() -> None:
    """Own schema, own file — WAL via helpers.db(), one owner per file."""
    con = helpers.db()          # helpers.db() already sets row_factory
    con.executescript(W_SCHEMA)
    con.commit()


def _expand(r: dict) -> dict:
    """One stored row as the flat dict every caller has always seen.

    `repeats` is omitted when it is 1, exactly as the kv ring omitted it: the
    dashboard reads `r.repeats > 1` and a row that never repeated should not
    carry a field claiming it did.
    """
    try:
        fields = json.loads(r.get("fields") or "{}")
    except Exception:
        fields = {}
    out = {"ts": float(r["ts"]), "level": r["level"], "cat": r["cat"],
           "event": r["event"], "msg": r["msg"]}
    if int(r.get("repeats") or 1) > 1:
        out["repeats"] = int(r["repeats"])
    if isinstance(fields, dict):
        out.update(fields)
    return out


def _split(row: dict) -> tuple[dict, dict]:
    """A wire row into (the five columns, everything else)."""
    known = ("ts", "level", "cat", "event", "msg", "repeats", "dedupe_s")
    core = {
        "ts": float(row.get("ts") or time.time()),
        "level": row.get("level") if row.get("level") in LEVELS else "info",
        "cat": row.get("cat") if row.get("cat") in CATEGORIES else "lifecycle",
        "event": str(row.get("event") or "")[:60],
        "msg": str(row.get("msg") or "")[:600],
    }
    fields = {str(k)[:24]: v for k, v in row.items() if k not in known}
    return core, fields


# --- writing ------------------------------------------------------------------

def _trim(table: str, cap: int) -> None:
    """Keep the ring bounded. AUTOINCREMENT ids are monotonic, so "everything
    older than the last `cap` ids" is one indexed delete — and trimming by
    INSERTION ORDER rather than by ts reproduces the kv ring exactly: a row
    whose ts was bumped by a dedupe keeps its place in the list."""
    top = helpers.db().execute(f"SELECT MAX(id) AS m FROM {table}").fetchone()["m"]
    if top is None:
        return
    _execute(f"DELETE FROM {table} WHERE id <= ?", (int(top) - cap,))


def _dedupe_bump(core: dict, dedupe_s: float) -> bool:
    """A repeat inside the window bumps the row already there. Returns whether
    it did.

    Checked against the STORED row rather than a per-process memory, which is the
    whole reason the ring moved: two processes hitting the same fault now collapse
    into one line with a count of two, instead of two lines that each look like a
    single incident. The kv ring could only afford to look at its last 40 entries
    before the scan cost more than the write; an index makes the honest question
    ("is there a row like this inside the window") the cheap one.
    """
    got = helpers.db().execute(
        "SELECT id, repeats FROM log_rows WHERE cat=? AND event=? AND msg=? AND ts > ?"
        " ORDER BY id DESC LIMIT 1",
        (core["cat"], core["event"], core["msg"], core["ts"] - dedupe_s)).fetchone()
    if not got:
        return False
    _execute("UPDATE log_rows SET repeats = repeats + 1, ts = ? WHERE id = ?",
             (core["ts"], int(got["id"])))
    return True


def store(rows: list[dict]) -> dict:
    """Record a batch. ONE transaction, one trim, whatever the batch size.

    Batching is the client's half of the promise that `logs.log()` never costs a
    caller a round-trip; this is the half that makes 100 rows cost about what one
    row used to.
    """
    con = helpers.db()
    stored = deduped = 0
    for row in rows:
        core, fields = _split(row if isinstance(row, dict) else {})
        dedupe_s = 0.0
        try:
            dedupe_s = max(0.0, float(row.get("dedupe_s") or 0))
        except Exception:
            dedupe_s = 0.0
        if dedupe_s > 0 and _dedupe_bump(core, dedupe_s):
            deduped += 1
            continue
        con.execute(
            "INSERT INTO log_rows (ts, level, cat, event, msg, repeats, fields)"
            " VALUES (?,?,?,?,?,1,?)",
            (core["ts"], core["level"], core["cat"], core["event"], core["msg"],
             json.dumps(fields)))
        if core["level"] == "error":
            # Errors also go somewhere a busy hour cannot push them out of.
            con.execute(
                "INSERT INTO error_rows (ts, level, cat, event, msg, repeats, fields)"
                " VALUES (?,?,?,?,?,1,?)",
                (core["ts"], core["level"], core["cat"], core["event"], core["msg"],
                 json.dumps(fields)))
        stored += 1
    con.commit()
    if stored:
        _trim("log_rows", MAX_ROWS)
        _trim("error_rows", MAX_ERRORS)
    return {"stored": stored, "deduped": deduped}


# --- reading ------------------------------------------------------------------

def rows(errors_only: bool = False, limit: int = 0) -> list[dict]:
    """The ring, oldest first — insertion order, like the list it replaced."""
    table = "error_rows" if errors_only else "log_rows"
    if limit > 0:
        got = _query(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (int(limit),))
        got.reverse()
    else:
        got = _query(f"SELECT * FROM {table} ORDER BY id ASC")
    return [_expand(r) for r in got]


def recent(level: str = "", cat: str = "", event: str = "", q: str = "",
           since: float = 0.0, limit: int = 200, errors_only: bool = False) -> list[dict]:
    """The tail, filtered. `level` is a FLOOR (warn returns warn AND error),
    because the question is always "show me things at least this bad", never
    "only warnings"."""
    table = "error_rows" if errors_only else "log_rows"
    where, params = [], []
    floor = _RANK.get(level, 0)
    if floor:
        where.append("level IN (%s)" % ",".join("?" * (len(LEVELS) - floor)))
        params.extend(LEVELS[floor:])
    if cat:
        where.append("cat = ?")
        params.append(cat)
    if event:
        where.append("event = ?")
        params.append(event)
    if since:
        where.append("ts >= ?")
        params.append(float(since))
    sql = f"SELECT * FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    out = [_expand(r) for r in _query(sql + " ORDER BY id ASC", tuple(params))]
    ql = (q or "").lower()
    if ql:
        # Substring over the whole row, fields included — the same thing the
        # in-process body did, and the reason searching a branch name or a sha
        # finds the row that mentions it.
        out = [r for r in out if ql in json.dumps(r).lower()]
    return out[-max(1, min(int(limit), 1000)):]


def stats(window_s: int = 3600, now: float | None = None) -> dict:
    """What monitoring reads: how much of each kind, how bad, and what broke most
    recently. The point of a category vocabulary is that this is answerable at
    all — "14 errors" is a number, "14 errors, all sandbox" is a diagnosis."""
    now = float(now or time.time())
    since = now - max(60, int(window_s))
    by_cat: dict[str, int] = {}
    by_level = {name: 0 for name in LEVELS}
    worst: dict | None = None
    for r in _query("SELECT * FROM log_rows WHERE ts >= ? ORDER BY id ASC", (since,)):
        row = _expand(r)
        by_cat[row.get("cat", "?")] = by_cat.get(row.get("cat", "?"), 0) + 1
        lvl = row.get("level", "info")
        if lvl in by_level:
            by_level[lvl] += 1
        if lvl == "error":
            worst = row
    return {"window_s": int(now - since), "total": sum(by_cat.values()),
            "by_cat": dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
            "by_level": by_level, "errors": by_level["error"] + by_level["warn"],
            "last_error": worst,
            "categories": CATEGORIES}


# --- decisions, and the standing one --------------------------------------------

def decisions() -> dict[str, dict]:
    return {r["fp"]: {"state": r["state"], "ts": r["ts"], "note": r["note"]}
            for r in _query("SELECT * FROM decisions ORDER BY ts DESC")}


def decide(fp: str, state: str, note: str = "") -> dict:
    """Remember what the human said. One row per fingerprint, replaced — a
    decision is the CURRENT answer, not a history."""
    rec = {"state": str(state)[:24], "ts": time.time(), "note": str(note or "")[:300]}
    _execute("INSERT INTO decisions (fp, state, ts, note) VALUES (?,?,?,?)"
             " ON CONFLICT(fp) DO UPDATE SET state=excluded.state, ts=excluded.ts,"
             " note=excluded.note",
             (str(fp)[:64], rec["state"], rec["ts"], rec["note"]))
    _execute("DELETE FROM decisions WHERE ts < ?", (time.time() - DECISION_RETENTION_S,))
    return rec


def auto_on() -> bool:
    """Whether the owner has asked for the additive proposals to run unattended.

    STORED here, next to the decisions, because it IS one: a standing decision
    about every future notice of a safe kind. ACTED ON in the conductor, where
    the actions live — this service never runs anything, and could not: it holds
    no lever on the crew.
    """
    return (helpers.kv_get("auto") or "") == "1"


def set_auto(on: bool) -> bool:
    helpers.kv_set("auto", "1" if on else "0")
    return bool(on)


# --- the rules ----------------------------------------------------------------
#
# Each takes the window's log rows and returns notices. They are deliberately
# dumb and readable: a rule nobody can check is a rule nobody trusts, and every
# one of these came from a failure that actually happened on this box.
#
# Everything here reads LOG ROWS AND NOTHING ELSE. That is the exact criterion
# that decided which rules moved: a rule that has to look at the conductor's own
# repair state (the review queue, the engine's phase clock) stayed there, as a
# local rule, and the composed list the dashboard reads is the two lists merged.

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

# How far back a rule looks. Long enough to see a pattern, short enough that a
# problem fixed an hour ago stops shouting.
WINDOW_S = 6 * 3600


def _fp(kind: str, *parts: Any) -> str:
    raw = "|".join([kind, *[str(p) for p in parts]])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _notice(kind: str, severity: str, title: str, detail: str, *, fp_parts: tuple = (),
            evidence: list | None = None, proposal: str = "", action: str = "",
            params: dict | None = None, count: int = 1, since: float = 0.0) -> dict:
    return {"fp": _fp(kind, *fp_parts), "kind": kind, "severity": severity,
            "title": title, "detail": detail,
            "evidence": [{"ts": r.get("ts"), "level": r.get("level"),
                          "cat": r.get("cat"), "event": r.get("event"),
                          "msg": r.get("msg", "")[:200]} for r in (evidence or [])[-5:]],
            "proposal": proposal, "action": action, "params": params or {},
            "count": count, "since": since}


def _rule_sandbox(rows_: list[dict]) -> list[dict]:
    hits = [r for r in rows_ if r.get("cat") == "sandbox"]
    if not hits:
        return []
    files = sorted({str(r.get("files") or r.get("path") or "")
                    for r in hits if r.get("files") or r.get("path")})
    return [_notice(
        "sandbox", "critical",
        f"{len(hits)} isolation breach{'es' if len(hits) > 1 else ''}",
        "A build session touched something outside its worktree, or a protected path. "
        "Nothing was reverted — that tree may hold your own work — so the files are still "
        "there to look at: " + (", ".join(files)[:300] or "see the evidence below"),
        fp_parts=("|".join(files),), evidence=hits, count=len(hits),
        since=min(float(r.get("ts", 0)) for r in hits),
        proposal="Stop self-repair until you have looked at those files.",
        action="pause_repair")]


# A live zombie heartbeats every tick; a dead one stops. Anything older than this
# is a problem that has already been solved, and reporting it is how a monitor
# loses its credibility — the whole promise of deriving on read is that what you
# see is still true.
ZOMBIE_FRESH_S = 300


def _rule_zombie(rows_: list[dict]) -> list[dict]:
    fresh = time.time() - ZOMBIE_FRESH_S
    hits = [r for r in rows_ if r.get("event") == "lease_held_elsewhere"
            and float(r.get("ts", 0)) >= fresh]
    if not hits:
        return []
    holder = hits[-1].get("holder")
    n = sum(int(r.get("repeats") or 1) for r in hits)
    return [_notice(
        "zombie", "critical", f"another process (pid {holder}) is driving the engine",
        f"A server that no longer answers requests is still ticking the self-repair engine "
        f"against this database — {n} times in the window. It is running whatever code it "
        f"started with. Kill it: `kill -9 {holder}`.",
        fp_parts=(holder,), evidence=hits, count=n,
        since=min(float(r.get("ts", 0)) for r in hits),
        proposal="", action="")]


def _rule_build_deaths(rows_: list[dict]) -> list[dict]:
    hits = [r for r in rows_ if r.get("event") == "build_died"]
    if len(hits) < 2:
        return []
    turns = [r for r in hits if "turn" in str(r.get("msg", "")).lower()]
    if len(turns) >= 2:
        return [_notice(
            "build_turns", "warning", f"{len(turns)} build sessions ran out of turns",
            "Tasks are being planned bigger than one session can finish, so the quota is "
            "spent and nothing lands.",
            fp_parts=(len(turns) // 3,), evidence=turns, count=len(turns),
            since=min(float(r.get("ts", 0)) for r in turns),
            # The pre-P3 prose named the current repair_max_turns value here. It
            # was decoration on a rule that otherwise reads only log rows, and
            # buying it would have meant giving this service the conductor's
            # tuning door — a knob read on the detection path, for a number in a
            # sentence. The sentence says the same thing without it.
            proposal="Ask the crew for smaller slices — drop repair_tasks_per_sprint to 1 "
                     "so each sprint plans less at once; the turn limit is untouched.",
            action="set_knob", params={"name": "repair_tasks_per_sprint", "value": 1})]
    return [_notice(
        "build_deaths", "warning", f"{len(hits)} build sessions died",
        "Sessions are failing before they produce anything. The last one said: "
        + str(hits[-1].get("msg", ""))[:200],
        fp_parts=(len(hits) // 3,), evidence=hits, count=len(hits),
        since=min(float(r.get("ts", 0)) for r in hits),
        proposal="Pause self-repair until the cause is understood — every dead session "
                 "still spends quota.",
        action="pause_repair")]


def _rule_red_streak(rows_: list[dict]) -> list[dict]:
    verdicts = [r for r in rows_ if r.get("cat") == "verify"
                and r.get("event") in ("suite_green", "suite_red")]
    tail: list[dict] = []
    for r in reversed(verdicts):
        if r.get("event") == "suite_green":
            break
        tail.append(r)
    if len(tail) < 3:
        return []
    return [_notice(
        "red_streak", "warning", f"the suite has been red {len(tail)} times running",
        "No task has verified green recently. Either the crew is picking work it cannot "
        "finish, or something on main is broken for everyone.",
        fp_parts=(len(tail) // 3,), evidence=list(reversed(tail)), count=len(tail),
        since=min(float(r.get("ts", 0)) for r in tail),
        proposal="Pause self-repair and run the suite yourself.",
        action="pause_repair")]


def _rule_dashboard(rows_: list[dict]) -> list[dict]:
    """A broken screen, reported by the screen itself.

    This is the loop the owner asked for and did not get: a JavaScript error sat
    visible in the UI for an hour without ever becoming a bug, because the code
    that caught it also hid it. Now it arrives here — and the obvious next move is
    not "look into it", it is "put it in front of the crew", so the proposal
    queues it as a P2 bug for the next sprint.
    """
    hits = [r for r in rows_ if r.get("event") in ("dashboard_error", "request_crashed")]
    if not hits:
        return []
    out = []
    by_msg: dict[str, list[dict]] = {}
    for r in hits:
        by_msg.setdefault(str(r.get("msg", ""))[:120], []).append(r)
    for msg, group in by_msg.items():
        where = str(group[-1].get("page") or group[-1].get("path") or "")
        out.append(_notice(
            "dashboard", "critical" if len(group) > 2 else "warning",
            f"the app is throwing: {msg[:80]}",
            f"Reported {len(group)}× by the page itself{f' on {where}' if where else ''}. "
            "It is a real defect in code that shipped, not a transient.",
            fp_parts=(msg,), evidence=group, count=len(group),
            since=min(float(r.get("ts", 0)) for r in group),
            proposal="Queue it as a bug for the crew's next sprint.",
            action="file_task",
            params={"title": f"Fix: {msg[:100]}", "type": "bug", "priority": "p2",
                    "factor": "correctness",
                    "brief": f"The dashboard reported this error {len(group)} times"
                             f"{f' on {where}' if where else ''}: {msg}. "
                             "Find the cause, fix it, and add the test that would have caught it."}))
    return out


def _rule_errors(rows_: list[dict]) -> list[dict]:
    """Anything logged at error level that no other rule has a better story for."""
    claimed = {"sandbox", "session"}
    claimed_events = {"dashboard_error", "request_crashed"}
    groups: dict[tuple, list[dict]] = {}
    for r in rows_:
        if r.get("level") != "error" or r.get("cat") in claimed \
                or r.get("event") in claimed_events:
            continue
        groups.setdefault((r.get("cat"), r.get("event")), []).append(r)
    out = []
    for (cat, event), hits in groups.items():
        out.append(_notice(
            "errors", "warning" if len(hits) < 3 else "critical",
            f"{len(hits)}× {cat}/{event}",
            str(hits[-1].get("msg", ""))[:300] or "see the evidence below",
            fp_parts=(cat, event, len(hits) // 3), evidence=hits, count=len(hits),
            since=min(float(r.get("ts", 0)) for r in hits),
            proposal="", action=""))
    return out


def _rule_quota(rows_: list[dict]) -> list[dict]:
    hits = [r for r in rows_ if r.get("event") == "rate_limited"]
    if not hits:
        return []
    models = sorted({str(r.get("model") or "?") for r in hits})
    return [_notice(
        "quota", "info", f"hit the limit on {', '.join(models)}",
        "The crew backed off and will wake when the provider says so — nothing to do, but "
        "it explains a quiet period.",
        fp_parts=(",".join(models), len(hits) // 3), evidence=hits, count=len(hits),
        since=min(float(r.get("ts", 0)) for r in hits),
        proposal="", action="")]


RULES: list[Callable[[list[dict]], list[dict]]] = [
    _rule_sandbox, _rule_zombie, _rule_dashboard, _rule_build_deaths, _rule_red_streak,
    _rule_errors, _rule_quota,
]


def scan(window_s: int = WINDOW_S, include_decided: bool = False) -> list[dict]:
    """Every notice the LOG ROWS currently support, worst first, dismissed ones
    filtered out. The conductor merges its two local rules into this list."""
    since = time.time() - max(300, int(window_s))
    window = [_expand(r) for r in
              _query("SELECT * FROM log_rows WHERE ts >= ? ORDER BY id ASC", (since,))]
    out: list[dict] = []
    for rule in RULES:
        try:
            out.extend(rule(window))
        except Exception as e:                 # a broken rule must not blind the rest
            store([{"ts": time.time(), "level": "error", "cat": "http",
                    "event": "monitor_rule_failed", "msg": f"{rule.__name__}: {e}"}])
    seen = decisions()
    for n in out:
        n["decision"] = seen.get(n["fp"], {})
    if not include_decided:
        out = [n for n in out if n.get("decision", {}).get("state") != "dismissed"]
    out.sort(key=lambda n: (SEVERITY_RANK.get(n["severity"], 9), -n.get("since", 0)))
    return out


def summary(window_s: int = WINDOW_S) -> dict:
    ns = scan(window_s)
    return {"total": len(ns),
            "critical": sum(1 for n in ns if n["severity"] == "critical"),
            "warning": sum(1 for n in ns if n["severity"] == "warning"),
            "needs_approval": sum(1 for n in ns if n.get("action")
                                  and not n.get("decision", {}).get("state"))}


# --- first boot ---------------------------------------------------------------

def _legacy_value(con: sqlite3.Connection, key: str) -> Any:
    got = con.execute("SELECT v FROM legacy.kv WHERE k=?", (key,)).fetchone()
    if not got:
        return None
    try:
        return json.loads(got[0])
    except Exception:
        return None


def settled() -> bool:
    """Has the first-boot decision been made, either way?

    The CONDUCTOR asks this — over /health — before it drops the four kv keys
    this service copies from. That is not politeness: the cutover made those keys
    dead weight in the conductor's database, but a conductor that deletes them
    while this service is still starting has destroyed the decisions store's
    history and the owner's standing `auto` setting, which is precisely the notice
    storm the copy exists to prevent. Nothing orders the two processes — the fleet
    starts them together — so the drop is conditional instead of hopeful.

    True means "this service will never look at those keys again", which includes
    the cases where it decided there was nothing to copy.
    """
    return bool(helpers.kv_get("backfilled_from"))


def _settle(reason: str, **extra) -> None:
    helpers.kv_set("backfilled_from", json.dumps(
        {"db": str(LEGACY_DB_PATH), "reason": reason,
         "keys": [LEGACY_RING_KEY, LEGACY_ERR_KEY, LEGACY_DECISIONS_KEY, LEGACY_AUTO_KEY],
         "ts": time.time(), **extra}))


def backfill_from_legacy() -> int:
    """First boot only: carry the conductor's four kv keys across the seam, once.

    The DECISIONS are the reason this is not optional. Without them the
    extraction itself becomes a notice storm: every notice the owner had already
    dismissed looks new again on the first boot after the cutover, which is
    precisely the failure the decisions store exists to prevent. The ring and the
    error ring come too, because a log view that starts empty on the morning of
    the cutover is a log view that cannot explain the night before — and because
    the notices are derived from those rows, so a breach two hours old must still
    be a notice two minutes after the move.

    THE MARKER IS SET EITHER WAY, and that is a deliberate change from the P1/P2
    services. It no longer means "rows were copied", it means "the first-boot
    decision has been made" — because the conductor reads it (through /health) to
    know when its own copies of those keys are safe to drop. "There was nothing
    to copy" has to be an answer, not silence, or a box that never had a legacy
    database would carry four dead keys forever waiting for a copy that will
    never happen.

    Only a genuine FAILURE (the conductor mid-write, a locked file) leaves it
    unset, so the next boot tries again — and the conductor keeps its keys until
    then. The copy is idempotent because it only ever runs against empty tables.
    """
    if settled():
        return 0
    if _query("SELECT COUNT(*) AS n FROM log_rows")[0]["n"]:
        # Rows already here and no marker: a previous boot copied and died before
        # marking. Not transient, and copying again would double the ring.
        _settle("tables were not empty")
        return 0
    if not LEGACY_DB_PATH.exists():
        _settle("no legacy database")
        return 0
    con = helpers.db()
    copied = 0
    try:
        con.execute("ATTACH DATABASE ? AS legacy", (f"file:{LEGACY_DB_PATH}?mode=ro",))
        try:
            for key, table, cap in ((LEGACY_RING_KEY, "log_rows", MAX_ROWS),
                                    (LEGACY_ERR_KEY, "error_rows", MAX_ERRORS)):
                blob = _legacy_value(con, key)
                if not isinstance(blob, list):
                    continue
                for r in blob[-cap:]:
                    if not isinstance(r, dict):
                        continue
                    core, fields = _split(r)
                    con.execute(
                        f"INSERT INTO {table} (ts, level, cat, event, msg, repeats, fields)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (core["ts"], core["level"], core["cat"], core["event"],
                         core["msg"], max(1, int(r.get("repeats") or 1)),
                         json.dumps(fields)))
                    copied += 1
            seen = _legacy_value(con, LEGACY_DECISIONS_KEY)
            if isinstance(seen, dict):
                for fp, rec in seen.items():
                    if not isinstance(rec, dict):
                        continue
                    con.execute(
                        "INSERT OR IGNORE INTO decisions (fp, state, ts, note)"
                        " VALUES (?,?,?,?)",
                        (str(fp)[:64], str(rec.get("state") or "")[:24],
                         float(rec.get("ts") or 0), str(rec.get("note") or "")[:300]))
                    copied += 1
            auto = _legacy_value(con, LEGACY_AUTO_KEY)
            con.commit()
            # Outside the ATTACH transaction's rows but inside the same boot: the
            # standing decision is a setting, and losing it silently would turn
            # unattended approval off without anyone deciding that.
            if auto is not None:
                helpers.kv_set("auto", "1" if auto else "0")
            _settle("copied", rows=copied, auto=auto)
        finally:
            con.execute("DETACH DATABASE legacy")
    except Exception:
        con.rollback()                  # try again next boot; the tables are still empty
        return 0
    return copied


init_store()
backfill_from_legacy()


# --- the HTTP surface ---------------------------------------------------------

# openapi_url=None: FastAPI must not serve a live-generated spec at the
# contract's address — the committed file is the contract, and drift between the
# two is a thing the contract tests exist to catch.
app = FastAPI(title=SERVICE, openapi_url=None)

# Bounds are contract, not decoration: a fuzzer will happily post ts=2**63 or a
# batch of a million rows to find out what happens.
_MAX_TS = 10 ** 11                  # 1970..~5138; excludes inf and nan
_MAX_BATCH = 500                    # the client flushes at 100; this is the ceiling
_MAX_WINDOW_S = 366 * 86400


class LogRow(StrictBody):
    """One fact.

    The six named fields are typed exactly as strictly as every other body in the
    fleet. Everything ELSE the caller sends rides along untyped — `sha`, `holder`,
    `files`, `n` — because that is the whole shape of a log row, and a logger that
    could only say the six things this schema names would be no logger at all.
    Those extras are coerced and truncated on the way into the table, so a caller
    inside an exception handler passing `n="3"` where an int was meant still gets
    its line on the record. Losing the evidence about a failure is worse than
    storing a string.

    `level` and `cat` are plain strings here rather than enums on purpose: an
    unknown one is CORRECTED to a known value (see `_split`), never refused. A
    422 on a typo'd category would drop the row, and the row is the point.
    """
    model_config = ConfigDict(extra="allow", strict=True)

    ts: JsonFloat = Field(0.0, ge=0, le=_MAX_TS)      # 0 = now
    level: str = Field("info", max_length=16)
    cat: str = Field("lifecycle", max_length=32)
    event: str = Field("", max_length=60)
    msg: str = Field("", max_length=600)
    dedupe_s: JsonFloat = Field(0.0, ge=0, le=86400)


class LogsBody(StrictBody):
    rows: list[LogRow] = Field(default_factory=list, max_length=_MAX_BATCH)


class DecideBody(StrictBody):
    state: str = Field(min_length=1, max_length=24)
    note: str = Field("", max_length=300)


class AutoBody(StrictBody):
    on: bool


def query_guard(*allowed: str) -> Callable:
    """Name every query parameter a route accepts, and refuse anything else.

    Two refusals, both found by the contract fuzzer's negative phase — both cases
    where the service quietly disagreed with its own committed spec:

      A NAME THE CONTRACT DOES NOT DEFINE. FastAPI ignores it and answers 200.
      Silently, `?levl=warn` returns every row and reads as "no warnings" — the
      most expensive kind of silence a log filter can produce, because the person
      asking is looking for trouble and is told there is none.

      A VALUE GIVEN TWICE. Starlette keeps the last one and FastAPI validates only
      that, so `?window_s=1&window_s=2` was a 200. A window is the span a notice
      is derived over, and "whichever of the two you sent last" is not an answer
      anyone can reason about at 3am; refusing is.
    """
    names = set(allowed)

    def _guard(request: Request) -> None:
        # FastAPI's own `HTTPValidationError` shape — a LIST of {loc, msg, type},
        # not a bare string. A hand-rolled refusal that answers the right status
        # with the wrong body is still a spec violation, and the fuzzer says so
        # on the very next run.
        for name in request.query_params:
            if name not in names:
                raise HTTPException(422, [{
                    "loc": ["query", name],
                    "msg": f"{name} is not a parameter of this endpoint; the "
                           f"contract defines {sorted(names)}",
                    "type": "value_error.unknown_parameter"}])
        for name in names:
            if len(request.query_params.getlist(name)) > 1:
                raise HTTPException(422, [{
                    "loc": ["query", name],
                    "msg": f"{name} was given more than once; the contract types "
                           f"it as a single value",
                    "type": "value_error.multiple_values"}])

    return _guard


@app.get("/health")
def health() -> dict:
    """Readiness, not liveness: ok only when the service could actually answer —
    its own database opens and the ring reads.

    `backfilled` rides alongside rather than inside `checks`, because it is not a
    readiness condition: a box with no legacy database to copy from is perfectly
    healthy. It is the conductor's signal that its own copies of the four
    migrated kv keys are finally safe to drop — see `settled()`.
    """
    def _table_ok() -> bool:
        try:
            helpers.db().execute("SELECT COUNT(*) FROM log_rows").fetchone()
            helpers.db().execute("SELECT COUNT(*) FROM decisions").fetchone()
            return True
        except Exception:
            return False
    checks = {"db": helpers.db_ok(), "table": _table_ok()}
    return {"ok": all(checks.values()), "service": SERVICE,
            "db": str(helpers.DB_PATH), "checks": checks, "backfilled": settled()}


@app.get("/openapi.json")
def openapi_spec() -> JSONResponse:
    """The committed contract. Regenerate after changing routes:
    `python app.py --spec > openapi.json` — and let oasdiff judge the diff."""
    return JSONResponse(json.loads(SPEC.read_text()))


@app.post("/logs", dependencies=[Depends(helpers.require_token)],
          responses={400: {"description": "Malformed JSON body"}})
def logs_route(body: LogsBody) -> dict:
    """Record a BATCH of rows. Batch, not row: the client is on the platform's
    hottest path (64 call sites, some inside exception handlers, some in a 20s
    tick) and it may never pay a round-trip per line."""
    return store([r.model_dump() for r in body.rows])


@app.get("/logs", dependencies=[
    Depends(helpers.require_token),
    Depends(query_guard("level", "cat", "event", "q", "since", "limit", "errors_only"))])
def list_logs(level: str = Query("", max_length=16), cat: str = Query("", max_length=32),
              event: str = Query("", max_length=60), q: str = Query("", max_length=200),
              since: float = Query(0, ge=0, le=_MAX_TS),
              limit: int = Query(200, ge=1, le=1000),
              errors_only: bool = False) -> dict:
    return {"logs": recent(level=level, cat=cat, event=event, q=q, since=since,
                           limit=limit, errors_only=errors_only),
            "categories": CATEGORIES, "levels": list(LEVELS)}


@app.get("/logs/stats", dependencies=[Depends(helpers.require_token),
                                      Depends(query_guard("window_s", "now"))])
def logs_stats(window_s: int = Query(3600, ge=0, le=_MAX_WINDOW_S),
               now: float = Query(0, ge=0, le=_MAX_TS)) -> dict:
    return stats(window_s, now or None)


@app.get("/notices", dependencies=[
    Depends(helpers.require_token),
    Depends(query_guard("window_s", "include_decided"))])
def list_notices(window_s: int = Query(0, ge=0, le=_MAX_WINDOW_S),
                 include_decided: bool = False) -> dict:
    """What the logs add up to — plus the two things the conductor needs to
    finish the list on its own side.

    `decisions` and `auto` ride along deliberately. The conductor merges two
    LOCAL rules into this list and has to know which of THOSE fingerprints the
    owner already dismissed; a second round-trip for that would double the cost
    of the panel's poll for one small map.
    """
    w = window_s or WINDOW_S
    return {"notices": scan(w, include_decided=include_decided),
            "summary": summary(w), "decisions": decisions(), "auto": auto_on()}


@app.post("/notices/{fp}/decide", dependencies=[Depends(helpers.require_token)],
          responses={400: {"description": "Malformed JSON body"}})
def decide_route(fp: str, body: DecideBody) -> dict:
    """Record what the human said. The ACTION, if the notice had one, already ran
    in the conductor — this service holds no lever and never did."""
    return {"fp": fp[:64], **decide(fp, body.state, body.note)}


@app.get("/auto", dependencies=[Depends(helpers.require_token)])
def get_auto() -> dict:
    """The standing decision, on its own, because the conductor's engine asks
    every tick and the answer is usually "no" — a full notice scan to learn that
    would be a scan a minute for nothing."""
    return {"auto": auto_on()}


@app.post("/auto", dependencies=[Depends(helpers.require_token)],
          responses={400: {"description": "Malformed JSON body"}})
def post_auto(body: AutoBody) -> dict:
    return {"auto": set_auto(body.on)}


@app.get("/summary", dependencies=[Depends(helpers.require_token),
                                   Depends(query_guard("window_s"))])
def summary_route(window_s: int = Query(0, ge=0, le=_MAX_WINDOW_S)) -> dict:
    """This service's own view, counted. The conductor's badge counts the COMPOSED
    list (these plus its two local rules) and computes that itself."""
    return summary(window_s or WINDOW_S)


if __name__ == "__main__":
    if "--spec" in sys.argv:
        # The one honest way to update the contract: print what the code actually
        # serves, commit it, and let the tests + oasdiff hold you to it.
        print(json.dumps(app.openapi(), indent=2))
    else:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8884")))
