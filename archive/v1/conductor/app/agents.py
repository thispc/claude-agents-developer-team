"""Every agent this platform has spawned, and what it is doing right now.

There was no way to ask. An agent's activity was implied — by a timestamp in its usage
record, by a task row's status, by a line in a log — and each of those answers a different
question in a different place, so "is this one working or idle?" had no single answer at all.
Worse, the implied answers disagree: a worker whose process died still reads as `running`,
and an agent that spent thirty seconds ago looks identical to one spending right now.

This is the register. Anything that makes an agent do something says so here, and everything
that wants to know — the canvas glow, the speech bubble, the agent page, a monitor — reads
the same row.

Two properties it has to have, both learned the hard way elsewhere in this codebase:

    It expires.   An entry is a CLAIM that work is in flight, and claims made by processes
                  that then die must not glow forever. Every state carries its own patience;
                  past that, the row reads `idle` no matter what it last said. Nothing has to
                  remember to clean up after a crash, which is the only cleanup that works.

    It is cheap.  A dict in kv, written on transitions only — not per token, not per tick.
                  Reading it costs one kv get, which is what lets the canvas ask on every
                  repaint without anybody thinking about cost.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from . import db

KEY = "agents:activity"
MAX_ROWS = 400

# What an agent can be doing, and how long a claim of it stays believable without a refresh.
# A thought is short; a coding session is long. Past the horizon the row reads idle — a
# process that died mid-task cannot leave an agent glowing forever.
STATES: dict[str, tuple[str, int]] = {
    "idle":      ("not doing anything", 0),
    "thinking":  ("a model call is in flight for it", 180),
    "speaking":  ("composing its line in a round", 120),
    "building":  ("a coding session is open in its name", 3600),
    "verifying": ("its work is under test", 1800),
    "asleep":    ("out of model quota until its window rolls", 6 * 3600),
}

_LOCK = threading.Lock()


def _all() -> dict[str, dict]:
    try:
        d = db.kv_get(KEY) or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def key_for(kind: str, *parts: Any) -> str:
    """A stable identity across restarts. `lw:3:14` is world 3's agent 14, forever."""
    return ":".join([kind, *[str(p) for p in parts]])


def note(key: str, state: str, what: str = "", **fields) -> dict:
    """Record what this agent is doing. Called on transitions, never in a loop."""
    row = {"state": state if state in STATES else "idle", "what": str(what)[:160],
           "ts": time.time()}
    for k, v in (fields or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            row[str(k)[:20]] = v if not isinstance(v, str) else v[:120]
    try:
        with _LOCK:
            all_rows = _all()
            prev = all_rows.get(key) or {}
            # `since` is when this STATE began, not when it was last touched — so the UI can
            # say "building for 4 minutes" rather than "building as of a second ago".
            row["since"] = prev.get("since", row["ts"]) if prev.get("state") == row["state"] else row["ts"]
            for keep in ("name", "where", "kind"):
                if keep not in row and prev.get(keep):
                    row[keep] = prev[keep]
            all_rows[key] = row
            if len(all_rows) > MAX_ROWS:            # oldest-touched first
                for k in sorted(all_rows, key=lambda x: all_rows[x].get("ts", 0))[:len(all_rows) - MAX_ROWS]:
                    all_rows.pop(k, None)
            db.kv_set(KEY, all_rows)
    except Exception:
        pass                                        # a register that can break the work is worse than none
    return row


def done(key: str, what: str = "") -> dict:
    """Finished — back to idle. The counterpart of every `note`."""
    return note(key, "idle", what)


def _resolve(row: dict, now: float) -> dict:
    """A row as it should be READ: expired claims read idle, however confidently they were made."""
    state = row.get("state", "idle")
    patience = STATES.get(state, ("", 0))[1]
    age = now - float(row.get("ts", 0) or 0)
    stale = bool(patience and age > patience)
    out = dict(row)
    out["stale"] = stale
    out["state"] = "idle" if stale else state
    out["busy"] = out["state"] not in ("idle", "asleep")
    out["for_s"] = int(now - float(row.get("since", row.get("ts", now)) or now))
    out["means"] = STATES.get(out["state"], ("", 0))[0]
    return out


def get(key: str, now: float | None = None) -> dict:
    now = float(now or time.time())
    row = _all().get(key)
    return _resolve(row, now) if row else {"state": "idle", "busy": False, "what": "",
                                           "stale": False, "for_s": 0,
                                           "means": STATES["idle"][0]}


def roster(now: float | None = None) -> list[dict]:
    """Everyone, busiest first. This is the whole point: one list, one answer per agent."""
    now = float(now or time.time())
    out = []
    for key, row in _all().items():
        r = _resolve(row, now)
        r["key"] = key
        out.append(r)
    out.sort(key=lambda r: (not r["busy"], -float(r.get("ts", 0))))
    return out


def summary(now: float | None = None) -> dict:
    rows = roster(now)
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    return {"total": len(rows), "busy": sum(1 for r in rows if r["busy"]),
            "by_state": by_state, "states": {k: v[0] for k, v in STATES.items()}}


class working:
    """`with agents.working(key, "thinking", "answering you"):` — note, then always release.

    A context manager because the failure that matters is the one where the work raises: an
    agent that threw halfway through must not be left claiming to think. The expiry horizon
    is the backstop for a process that dies outright; this is the one for an exception.
    """

    def __init__(self, key: str, state: str, what: str = "", **fields):
        self.key, self.state, self.what, self.fields = key, state, what, fields

    def __enter__(self):
        note(self.key, self.state, self.what, **self.fields)
        return self

    def __exit__(self, *exc):
        done(self.key)
        return False
