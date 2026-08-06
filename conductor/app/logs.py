"""logs.py — one pipeline for everything the backend has to say about itself.

Before this there were three channels and no discipline. `print()` went to whatever terminal
the server happened to be started from and was gone the moment it scrolled — 56 of them, and
they were the only record of most failures. `bus.emit` is a NARRATIVE channel: it exists to
tell a human what their team is doing, and using it for operational detail buries the story
under plumbing. And a handful of failures were written to one kv key that only ever held the
most recent one, so the second error erased the first.

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

Storage is a capped kv ring — no new table, survives restarts, and bounded so a chatty loop
cannot fill the disk. Every write also goes to stdout in one readable line, because when
something is wrong at 3am the terminal is what you have.
"""

from __future__ import annotations

import json
import sys
import threading
import time

from . import db

RING_KEY = "logs:ring"
MAX_ROWS = 3000                # a few days of ordinary operation; errors are also kept apart
ERR_KEY = "logs:errors"        # a longer memory for the rows anyone actually goes looking for
MAX_ERRORS = 300

LEVELS = ("debug", "info", "warn", "error")
_RANK = {name: i for i, name in enumerate(LEVELS)}

# The vocabulary. Adding a category is a deliberate act: it is the axis someone filters on
# when they are trying to find out what broke, so it has to answer "what should I look at",
# not "which file was running".
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

_LOCK = threading.Lock()

# Set false in tests that assert on stdout, or when a caller wants the ring only.
ECHO = True


def _print(row: dict) -> None:
    when = time.strftime("%H:%M:%S", time.localtime(row["ts"]))
    extra = " ".join(f"{k}={v}" for k, v in row.items()
                     if k not in ("ts", "level", "cat", "event", "msg"))
    line = f"{when} {row['level'].upper():<5} {row['cat']}/{row['event']}  {row['msg']}"
    print(f"{line}  {extra}" if extra else line,
          file=sys.stderr if row["level"] in ("warn", "error") else sys.stdout, flush=True)


_LAST: dict[tuple, float] = {}


def log(cat: str, event: str, msg: str = "", level: str = "info",
        dedupe_s: int = 0, **fields) -> dict:
    """Record one fact. Never raises: a logger that can take the process down with it is
    worse than no logger, and this one is called from inside exception handlers.

    `dedupe_s` is for the never-die loops. A loop that ticks every 20 seconds and finds the
    same thing wrong writes the same line 180 times an hour, and the record it produces is
    unreadable exactly when someone needs it. With it set, a repeat inside the window bumps
    a counter on the row already there instead of adding another — the fact stays visible,
    and so does everything around it.
    """
    row = {"ts": time.time(), "level": level if level in LEVELS else "info",
           "cat": cat if cat in CATEGORIES else "lifecycle",
           "event": str(event)[:60], "msg": str(msg)[:600]}
    for k, v in (fields or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            row[str(k)[:24]] = v if not isinstance(v, str) else v[:300]
        else:
            row[str(k)[:24]] = str(v)[:300]
    if dedupe_s > 0:
        key = (row["cat"], row["event"], row["msg"])
        if row["ts"] - _LAST.get(key, 0) < dedupe_s:
            try:
                with _LOCK:
                    ring = db.kv_get(RING_KEY) or []
                    for r in reversed(ring[-40:]):
                        if (r.get("cat"), r.get("event"), r.get("msg")) == key:
                            r["repeats"] = int(r.get("repeats") or 1) + 1
                            r["ts"] = row["ts"]
                            db.kv_set(RING_KEY, ring)
                            break
            except Exception:
                pass
            return row
        _LAST[key] = row["ts"]
    try:
        if ECHO:
            _print(row)
    except Exception:
        pass
    try:
        with _LOCK:
            ring = db.kv_get(RING_KEY) or []
            if not isinstance(ring, list):
                ring = []
            ring.append(row)
            db.kv_set(RING_KEY, ring[-MAX_ROWS:])
            if row["level"] == "error":
                # Errors also go somewhere that a busy hour cannot push them out of.
                errs = db.kv_get(ERR_KEY) or []
                if not isinstance(errs, list):
                    errs = []
                errs.append(row)
                db.kv_set(ERR_KEY, errs[-MAX_ERRORS:])
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


def rows(errors_only: bool = False) -> list[dict]:
    try:
        out = db.kv_get(ERR_KEY if errors_only else RING_KEY) or []
        return [r for r in out if isinstance(r, dict)]
    except Exception:
        return []


def recent(level: str = "", cat: str = "", event: str = "", q: str = "",
           since: float = 0.0, limit: int = 200) -> list[dict]:
    """The tail, filtered. `level` is a FLOOR (warn returns warn and error), because the
    question is always "show me things at least this bad", never "only warnings"."""
    floor = _RANK.get(level, 0)
    ql = (q or "").lower()
    out = []
    for r in rows():
        if _RANK.get(r.get("level", "info"), 1) < floor:
            continue
        if cat and r.get("cat") != cat:
            continue
        if event and r.get("event") != event:
            continue
        if since and float(r.get("ts", 0)) < since:
            continue
        if ql and ql not in json.dumps(r).lower():
            continue
        out.append(r)
    return out[-max(1, min(int(limit), 1000)):]


def stats(window_s: int = 3600, now: float | None = None) -> dict:
    """What monitoring reads: how much of each kind, how bad, and what broke most recently.

    The point of a category vocabulary is that this is answerable at all — "14 errors" is a
    number, "14 errors, all sandbox" is a diagnosis.
    """
    now = float(now or time.time())
    since = now - max(60, int(window_s))
    by_cat: dict[str, int] = {}
    by_level = {name: 0 for name in LEVELS}
    worst: dict | None = None
    for r in rows():
        if float(r.get("ts", 0)) < since:
            continue
        by_cat[r.get("cat", "?")] = by_cat.get(r.get("cat", "?"), 0) + 1
        lvl = r.get("level", "info")
        if lvl in by_level:
            by_level[lvl] += 1
        if lvl == "error":
            worst = r
    return {"window_s": int(now - since), "total": sum(by_cat.values()),
            "by_cat": dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
            "by_level": by_level, "errors": by_level["error"] + by_level["warn"],
            "last_error": worst,
            "categories": CATEGORIES}
