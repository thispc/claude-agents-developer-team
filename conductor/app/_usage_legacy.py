"""The pre-P2 in-process usage body, vendored for the strangler window.

This file exists only between commit A (extract) and commit B (cutover) of the
usage extraction: conductor/app/usage.py is the dual-mode shim, and when
USAGE_URL is unset it hands the whole surface to this module — byte-for-byte the
old behaviour, including the `usage:ledger` kv blob in devteam.db. Commit B
deletes this file and the fallback branch with it. Nothing imports this module
except the shim's fallback branch; the moved-out body now lives, adapted to a
real `usage_rows` table, in services/usage/app.py.

The one thing to know while reading it: `note()` below is a read-modify-write of
one kv blob under a THREAD lock. That lock stops two threads from losing an
update and does nothing at all about two processes — which is why the meter left
the conductor.

--- the original module docstring follows ---

usage.py — one rolling meter of every model call this box makes, whoever makes it.

The self-repair crew and the owner draw on the SAME subscription. A crew that meters only its
own sessions is measuring the wrong thing: six scout runs are nothing on a quiet Sunday and
are theft on a weekday afternoon when the owner is mid-project. So the sleep decision needs a
number the crew cannot see from inside itself — how much of the window is actually being
consumed, and by whom.

Every Claude-spending path reports here: repair scout/build sessions, the project manager's
session, worker task sessions, and studio/lifeworld seat calls. Each row carries a SOURCE, so
the meter can answer the only two questions the manager really has:

    "is anyone else using it right now?"   → contention (yield to the human)
    "how much of this window is gone?"     → utilization (don't hit the wall)

MEASURED IN TOKENS, not dollars. On a Max subscription there is no bill, so a dollar figure is
a number nobody can act on — "$3.67" answers no question the owner has. Tokens are what the
limit is actually made of, and the SDK reports them per session. Two counts are kept apart on
purpose:

    tok    input + output — the real work, and what the meter runs on
    cache  cache reads/writes — enormous next to the rest (one build reads ~3M) and far
           cheaper; folded into one number it makes every build look catastrophic

`usd` is still recorded, because someone running on API keys rather than a subscription will
want it, but nothing in the sleep decision or the Improve screen reads it.

kv-only (no new tables), rolling, capped. Every read tolerates a missing/garbage ledger:
a meter that raises would take the platform down to protect a number.
"""

from __future__ import annotations

import contextvars
import threading
import time

from . import db, logs, tuning

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


def note(source: str, model: str = "", tok: int = 0, cache: int = 0,
         usd: float = 0.0, calls: int = 1, ts: float | None = None) -> None:
    """Record one model call (or one session standing for `calls` of them)."""
    row = {"ts": float(ts or time.time()), "source": str(source or "?")[:24],
           "model": str(model or "")[:60], "tok": max(0, int(tok or 0)),
           "cache": max(0, int(cache or 0)), "usd": round(float(usd or 0), 4),
           "calls": max(1, int(calls or 1))}
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
    tok = cache = 0
    if isinstance(u, dict):
        tok = int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
        cache = (int(u.get("cache_read_input_tokens") or 0)
                 + int(u.get("cache_creation_input_tokens") or 0))
    note(source, model, tok=tok, cache=cache, usd=usd)
    # Logged HERE rather than at each call site, so a new spender cannot forget to: this is
    # the one function every Agent SDK session's result passes through.
    logs.debug("spend", "session_metered", f"{source} used {tok} tokens on {model}",
               source=source, model=model, tok=tok, cache=cache)
    return usd


def _tok(r: dict) -> int:
    """Work tokens for a row.

    Rows written before work and cache were separated carry a `tokens` field that summed all
    four kinds — one build's 3M cache reads inside it — so reading them as work tokens pinned
    the meter at 100% and put the crew to sleep on a fiction. They count as zero: they still
    appear as CALLS, which is exactly what the session-count backstop is for, and an honest
    zero beats a number we know is wrong."""
    return int(r.get("tok") or 0)


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


def budget_tokens() -> int:
    try:
        return max(1000, int(tuning.get("usage_budget_tokens")))
    except Exception:
        return 1_000_000


def snapshot(now: float | None = None) -> dict:
    """The whole utilization picture, in one dict the engine and the UI both read.

    `allowance_tok` is the heart of it: what the crew may spend in this window is what the
    OWNER left unused, capped by `repair_idle_share`. Nobody has to tune a crew budget — a
    quiet box hands the crew room automatically, and a busy one takes it back.
    """
    now = float(now or time.time())
    win = window_hours() * 3600
    budget = budget_tokens()
    share = min(1.0, max(0.0, float(tuning.get("repair_idle_share"))))
    quiet_need = max(0, int(tuning.get("repair_yield_quiet_s")))

    all_rows = rows(now - win)
    owner = [r for r in all_rows if r.get("source") in OWNER_SOURCES]
    mine = [r for r in all_rows if r.get("source") == "repair"]
    owner_tok = sum(_tok(r) for r in owner)
    my_tok = sum(_tok(r) for r in mine)
    total_tok = sum(_tok(r) for r in all_rows)

    last_owner = max((float(r["ts"]) for r in owner), default=0.0)
    quiet_s = int(now - last_owner) if last_owner else 10 ** 9   # never seen = fully quiet

    by_source: dict[str, dict] = {}
    for r in all_rows:
        b = by_source.setdefault(str(r.get("source")), {"tok": 0, "cache": 0, "calls": 0})
        b["tok"] += _tok(r)
        b["cache"] += int(r.get("cache") or 0)
        b["calls"] += int(r.get("calls") or 1)
    oldest = min((float(r["ts"]) for r in all_rows), default=0.0)
    return {
        "window_h": round(win / 3600, 2), "budget_tok": budget,
        "used_tok": total_tok, "owner_tok": owner_tok, "repair_tok": my_tok,
        "cache_tok": sum(int(r.get("cache") or 0) for r in all_rows),
        "frac": round(min(1.0, total_tok / budget), 3),
        "owner_frac": round(min(1.0, owner_tok / budget), 3),
        "idle_frac": round(max(0.0, 1.0 - owner_tok / budget), 3),
        "allowance_tok": max(0, int(budget * share) - owner_tok),
        "quiet_s": min(quiet_s, 10 ** 9), "quiet_need_s": quiet_need,
        "contended": bool(last_owner and quiet_s < quiet_need),
        "calls": sum(int(r.get("calls") or 1) for r in all_rows),
        "usd": round(sum(float(r.get("usd") or 0) for r in all_rows), 2),
        "resets_at": (oldest + win) if oldest else 0.0,
        "by_source": by_source,
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
    if u["repair_tok"] >= u["allowance_tok"] > 0:
        return (False, "self-repair has used its share of this window",
                u["resets_at"] or now + 1800)
    if u["allowance_tok"] <= 0 and u["owner_tok"] > 0:
        return (False, "your own work has claimed the window's quota",
                u["resets_at"] or now + 1800)
    return True, "", 0.0


def backfill_repair() -> int:
    """One-shot: import the crew's own historical sessions from `repair:ledger`.

    The crew kept its own ledger long before this shared meter existed, and until its rows
    show up here the meter reads "nothing used" on a box that has been working all week —
    which then lets the crude call-counter fallback keep deciding. Runs once (guarded by a kv
    flag). Those rows predate per-call token reporting and carry only a cost, so they are
    imported as CALLS with no tokens: an honest zero beats an invented number.
    """
    try:
        if db.kv_get("usage:backfilled"):
            return 0
        moved = 0
        for r in db.kv_get("repair:ledger") or []:
            if isinstance(r, dict) and float(r.get("usd") or 0) > 0:
                note("repair", str(r.get("model") or ""), usd=float(r["usd"]),
                     calls=int(r.get("n") or 1), ts=float(r.get("ts") or 0) or None)
                moved += 1
        db.kv_set("usage:backfilled", True)
        return moved
    except Exception:
        return 0
