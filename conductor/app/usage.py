"""usage.py — one rolling meter of every model call this box makes, whoever makes it.

The self-repair crew and the owner draw on the SAME subscription quota. A crew that meters
only its own sessions is measuring the wrong thing: six scout runs are nothing on a quiet
Sunday and are theft on a weekday afternoon when the owner is mid-project. So the sleep
decision needs a number the crew cannot see from inside itself — how much of the window's
quota is actually being consumed, and by whom.

Every Claude-spending path on the box reports here: repair scout/build sessions, the project
manager's session, worker task sessions, and studio/lifeworld seat calls. Each row carries a
SOURCE, so the meter can answer the only two questions the manager really has:

    "is anyone else using the quota right now?"   → contention (yield to the human)
    "how much of this window is already gone?"    → utilization (don't hit the wall)

`usd` is the Agent SDK's `total_cost_usd`. On a subscription that is not a bill — it is the
API-equivalent price of the tokens consumed, which is the best available proxy for how much
of an opaque quota a call ate. Token counts are recorded too when the SDK reports them, so
the proxy can be checked against the real thing later.

kv-only (no new tables), rolling, capped. Every read tolerates a missing/garbage ledger:
a meter that raises would take the platform down to protect a number.
"""

from __future__ import annotations

import contextvars
import threading
import time

from . import db, tuning

LEDGER_KEY = "usage:ledger"
MAX_ROWS = 2000                 # ~a week of busy days; the window queries only look back hours

# Sources that are the OWNER's work — the crew yields to these. Anything tagged "repair" is
# the crew's own spend and must not make the crew think the box is busy (it would then yield
# to itself and never wake).
OWNER_SOURCES = ("manager", "worker", "studio")

_LOCK = threading.Lock()

# Who to bill when the spending code cannot know. The crew's own deliberation runs through
# `providers.complete` exactly like a Studio seat does — and if that spend were filed as the
# owner's, the crew would see the box as busy, yield, and put ITSELF to sleep forever.
# A contextvar (not a global) because it has to follow one asyncio task and not leak into
# whatever else the event loop is running at the same moment.
_SOURCE = contextvars.ContextVar("usage_source", default="")


class attributed:
    """`with usage.attributed("repair"):` — bill everything spent inside to that source."""

    def __init__(self, source: str):
        self.source, self._token = source, None

    def __enter__(self):
        self._token = _SOURCE.set(self.source)
        return self

    def __exit__(self, *exc):
        if self._token is not None:
            _SOURCE.reset(self._token)
        return False


def current_source(default: str = "") -> str:
    """The source in force, for callers that are generic about who is spending."""
    return _SOURCE.get() or default


def note(source: str, model: str = "", usd: float = 0.0, tokens: int = 0,
         calls: int = 1, ts: float | None = None) -> None:
    """Record one model call (or one session standing for `calls` of them)."""
    row = {"ts": float(ts or time.time()), "source": str(source or "?")[:24],
           "model": str(model or "")[:60], "usd": round(float(usd or 0), 4),
           "tokens": max(0, int(tokens or 0)), "calls": max(1, int(calls or 1))}
    try:
        with _LOCK:
            rows = db.kv_get(LEDGER_KEY) or []
            if not isinstance(rows, list):
                rows = []
            rows.append(row)
            db.kv_set(LEDGER_KEY, rows[-MAX_ROWS:])
    except Exception:
        pass                    # metering must never break the thing being metered


def note_result(source: str, model: str, message) -> float:
    """Record an Agent SDK ResultMessage. Returns the usd so callers can keep using it."""
    usd = float(getattr(message, "total_cost_usd", 0) or 0)
    u = getattr(message, "usage", None) or {}
    tok = 0
    if isinstance(u, dict):
        tok = sum(int(u.get(k) or 0) for k in
                  ("input_tokens", "output_tokens",
                   "cache_read_input_tokens", "cache_creation_input_tokens"))
    note(source, model, usd, tok)
    return usd


def rows(since: float = 0.0) -> list[dict]:
    try:
        out = db.kv_get(LEDGER_KEY) or []
        return [r for r in out if isinstance(r, dict) and float(r.get("ts", 0)) >= since]
    except Exception:
        return []


def window_hours() -> float:
    try:
        return max(0.25, float(tuning.get("usage_window_h")))
    except Exception:
        return 5.0


def totals(since: float, sources: tuple[str, ...] | None = None) -> dict:
    sel = [r for r in rows(since) if sources is None or r.get("source") in sources]
    return {"usd": round(sum(float(r.get("usd") or 0) for r in sel), 4),
            "tokens": sum(int(r.get("tokens") or 0) for r in sel),
            "calls": sum(int(r.get("calls") or 1) for r in sel),
            "rows": len(sel)}


def snapshot(now: float | None = None) -> dict:
    """The whole utilization picture, in one dict the engine and the UI both read.

    `allowance_usd` is the heart of it: what the crew may spend in this window is what the
    OWNER left unused, capped by `repair_idle_share`. Nobody has to tune a crew budget — a
    quiet box hands the crew room automatically, and a busy one takes it back.
    """
    now = float(now or time.time())
    win = window_hours() * 3600
    since = now - win
    budget = max(0.01, float(tuning.get("usage_budget_usd")))
    share = min(1.0, max(0.0, float(tuning.get("repair_idle_share"))))
    quiet_need = max(0, int(tuning.get("repair_yield_quiet_s")))

    all_rows = rows(since)
    owner = [r for r in all_rows if r.get("source") in OWNER_SOURCES]
    mine = [r for r in all_rows if r.get("source") == "repair"]
    owner_usd = round(sum(float(r.get("usd") or 0) for r in owner), 4)
    my_usd = round(sum(float(r.get("usd") or 0) for r in mine), 4)
    total_usd = round(sum(float(r.get("usd") or 0) for r in all_rows), 4)

    last_owner = max((float(r["ts"]) for r in owner), default=0.0)
    quiet_s = int(now - last_owner) if last_owner else 10 ** 9   # never seen = fully quiet

    allowance = max(0.0, round(budget * share - owner_usd, 4))
    oldest = min((float(r["ts"]) for r in all_rows), default=0.0)
    return {
        "window_h": round(win / 3600, 2), "budget_usd": round(budget, 2),
        "used_usd": total_usd, "owner_usd": owner_usd, "repair_usd": my_usd,
        "frac": round(min(1.0, total_usd / budget), 3),
        "owner_frac": round(min(1.0, owner_usd / budget), 3),
        "idle_frac": round(max(0.0, 1.0 - owner_usd / budget), 3),
        "allowance_usd": allowance,
        "quiet_s": min(quiet_s, 10 ** 9), "quiet_need_s": quiet_need,
        "contended": bool(last_owner and quiet_s < quiet_need),
        "tokens": sum(int(r.get("tokens") or 0) for r in all_rows),
        "calls": sum(int(r.get("calls") or 1) for r in all_rows),
        "resets_at": (oldest + win) if oldest else 0.0,
        "by_source": {s: round(sum(float(r.get("usd") or 0) for r in all_rows
                                   if r.get("source") == s), 4)
                      for s in sorted({str(r.get("source")) for r in all_rows})},
    }


def verdict(now: float | None = None) -> tuple[bool, str, float]:
    """(quota_is_available, why_not, wake_ts) — the utilization half of the sleep decision.

    Two distinct refusals, because they want different wake times: CONTENTION is transient
    (the owner is typing right now — check back in minutes), while an EXHAUSTED share only
    clears when the rolling window rolls.
    """
    now = float(now or time.time())
    u = snapshot(now)
    if u["contended"]:
        return (False, "yielding — your own work is using the quota",
                now + max(60, u["quiet_need_s"] - u["quiet_s"]))
    if u["budget_usd"] > 0 and u["repair_usd"] >= u["allowance_usd"] > 0:
        return (False, "self-repair has used its share of this window",
                u["resets_at"] or now + 1800)
    if u["allowance_usd"] <= 0 and u["owner_usd"] > 0:
        return (False, "your own work has claimed the window's quota",
                u["resets_at"] or now + 1800)
    return True, "", 0.0


def backfill_repair() -> int:
    """One-shot: import the crew's own historical spend from `repair:ledger`.

    The crew kept its own ledger long before this shared meter existed, and until its rows
    show up here the utilization picture reads "$0 used" on a box that has spent real money
    all week — which then lets the crude call-counter fallback keep deciding. Runs once
    (guarded by a kv flag) and only for rows with a cost, since those are exactly the ones
    that predate per-call reporting.
    """
    try:
        if db.kv_get("usage:backfilled"):
            return 0
        rows = db.kv_get("repair:ledger") or []
        moved = 0
        for r in rows:
            if isinstance(r, dict) and float(r.get("usd") or 0) > 0:
                note("repair", str(r.get("model") or ""), float(r["usd"]),
                     calls=int(r.get("n") or 1), ts=float(r.get("ts") or 0) or None)
                moved += 1
        db.kv_set("usage:backfilled", True)
        return moved
    except Exception:
        return 0
