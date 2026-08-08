"""usage.py — the conductor's one door to the shared quota meter.

Every Claude-spending path on this box reports through here — repair scout/build
sessions, the project manager's session, worker task sessions, studio seats — so
the self-repair crew can answer the only two questions it really has: is anyone
else using the subscription right now, and how much of this window is gone.

Since P2 the meter itself is a SERVICE — services/usage, its own process on 8882,
its own `data/usage.db`, its own committed contract — and this file is the pure
client that reaches it, keeping every public name the in-process module ever had
(note / note_result / rows / snapshot / verdict / attributed / current_source /
window_hours / budget_tokens, plus health) so nothing above it had to learn that
the meter moved.

WHY IT MOVED. The old meter was one kv blob rewritten whole on every note: read
the list, append, write it back, under a thread lock. A thread lock is not a
process lock, and the moment the platform became a fleet the lost update stopped
being theoretical — on the very number that decides whether the crew may spend
the owner's quota. The service replaces the blob with a real `usage_rows` table
and one INSERT per call. That is the whole point of the extraction; the process
boundary is just what stops anyone reaching back in.

The P2 cutover finished here: the vendored in-process body (_usage_legacy.py) and
the dual-mode branch are gone, and `USAGE_URL` is now REQUIRED. It is written into
data/env/conductor.env by tools/gen_fleet.py from services.yaml, so every
supported boot path — `./run-local.sh` and `./run-local.sh --legacy` alike — has
it. A conductor started by hand without it fails loudly in init() rather than
pretending to know what the subscription is doing.

WHY init() AND NOT IMPORT. Importing this module must stay free: tools, tests and
the module graph import it without ever metering anything. Boot does call init(),
and that is the honest place to refuse — one clear message naming run-local.sh,
before the first sprint decides it may spend against a meter that is not there.

ATTRIBUTION. `attributed` and `current_source` stay HERE, conductor-side, because
they are a convenience for call sites that cannot know who is spending. They are
resolved BEFORE the wire (providers.complete does it at its top), and the request
body always carries an explicit `source` string. The service has no contextvar,
no default and no opinion about who spent — a meter that guesses can bill the
crew's own footsteps to the owner and put the crew to sleep forever.

note_result stays here too: it parses an Agent SDK ResultMessage, which is a
Python object the service will never see. Only the resulting numbers cross.

LATENCY BUDGET: one localhost HTTP round-trip per verb, ~1-3ms in practice, 5ms
p50 budgeted; hard timeout 2s so a wedged meter can never hold a sprint hostage.
A client is built per call — at the platform's call rates the ~0.1ms construction
cost buys freedom from event-loop lifetime bugs.

DEGRADED MODES (service down):
    note      → dropped, with one deduped warn. Metering must never break the
                thing being metered — that rule outranks the measurement.
    note_result → same, and still returns the usd it parsed
    rows      → []
    snapshot  → every number zero plus "degraded": True — never invented figures
    verdict   → FAIL-SAFE: (False, "usage meter unreachable", now + 300).
                The ONE verb that refuses instead of shrugging: with no meter
                there is no way to know whether the owner is mid-project, and
                spending blind against someone else's quota is the failure this
                whole module exists to prevent. The wake is BOUNDED at five
                minutes precisely so a flapping service cannot sleep the crew
                forever — it re-asks, and the moment the meter answers the crew
                resumes without a restart.
The verbs degrade for a MISSING url too, by the same path: init() is the loud
door, and a door that also killed every later call would turn one misconfigured
process into a crash loop.
"""

from __future__ import annotations

import contextvars
import os
import time

import httpx

from . import config, db, pool, tuning

_URL = (os.environ.get("USAGE_URL") or "").strip().rstrip("/")

# Sources that are the OWNER's work — the crew yields to these. Anything tagged
# "repair" is the crew's own spend and must not make the crew think the box is
# busy (it would then yield to itself and never wake). Duplicated in the service,
# which is the side that acts on it; kept here because the Improve screen and the
# tests read the tuple by name.
OWNER_SOURCES = ("manager", "worker", "studio")

_TIMEOUT = 2.0
# The bounded wake of the fail-safe verdict. Five minutes: long enough that a
# restarting meter is not hammered, short enough that a crew asleep on a flap is
# awake again before anyone notices.
FAILSAFE_WAKE_S = 300
# Tests inject an httpx transport here (one that refuses, for the outage drills)
# — the client code path stays identical. Sync, because every verb here is.
_TRANSPORT: httpx.BaseTransport | None = None
_TOKEN = ""

_NO_URL = (
    "USAGE_URL is not set — the shared quota meter is a service since P2 and the "
    "conductor has no in-process fallback any more. Boot with ./run-local.sh "
    "(or ./run-local.sh --legacy), which generates data/env/conductor.env from "
    "services.yaml and starts services/usage on 8882."
)

# Who to bill when the spending code cannot know. The crew's own deliberation runs
# through `providers.complete` exactly like a Studio seat does — and if that spend
# were filed as the owner's, the crew would see the box as busy, yield, and put
# ITSELF to sleep forever. A contextvar (not a global) because it has to follow
# one asyncio task and not leak into whatever else the event loop is running at
# the same moment.
#
# It never reaches the service: it is resolved at the call site, and the wire
# carries the resulting literal.
_SOURCE = contextvars.ContextVar("usage_source", default="")


class attributed:
    """`with usage.attributed("repair"):` — bill everything spent inside to that
    source."""

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
    """The source in force, for callers that are generic about who is spending.
    Read at the conductor's call sites — never on the wire."""
    return _SOURCE.get() or default


def _token() -> str:
    """The service's own token, read from where gen_fleet minted it — the same
    resolution the /svc gateway uses (routes/svc.py)."""
    global _TOKEN
    if not _TOKEN:
        try:
            _TOKEN = (config.ROOT / "data" / "tokens" / "usage.token") \
                .read_text().strip()
        except OSError:
            _TOKEN = ""
    return _TOKEN


def _client() -> httpx.Client:
    # Every verb here keeps its historical SYNC signature — repair.meters() and
    # repair.headroom() are plain functions on the sleep path. A blocked event
    # loop is bounded by the 2s timeout and, in practice, by the localhost
    # round-trip. Tests swap this factory for a TestClient (the mounted service),
    # or set _TRANSPORT to one that refuses (the outage) — and the pool rebuilds
    # on that swap, because _TRANSPORT is part of the cached client's identity.
    # SHARED, not per call: a fresh client per note() cost 5.6ms against 0.56ms
    # pooled, on a meter every spender touches. Never close what this returns.
    return pool.sync_client("usage", base_url=_URL, timeout=_TIMEOUT,
                            transport=_TRANSPORT,
                            headers={"X-Service-Token": _token()})


def _degraded(verb: str, err: Exception) -> None:
    """One deduped warn per window, never a raise — the degraded shapes are the
    contract; the log line is how a 3am operator learns which one fired."""
    try:
        from . import logs
        # "lifecycle" is the vocabulary's own word for a process being up or
        # down — logs.log() coerces anything unknown, and a silently coerced
        # category is a filter that quietly stops working.
        logs.log("lifecycle", "usage_degraded",
                 f"usage service unreachable — {verb} degraded "
                 f"({type(err).__name__}: {str(err)[:120]})",
                 level="warn", dedupe_s=300, verb=verb)
    except Exception:
        pass


# --- the verbs, over the wire -------------------------------------------------

def note(source: str, model: str = "", tok: int = 0, cache: int = 0,
         usd: float = 0.0, calls: int = 1, ts: float | None = None) -> None:
    """Record one model call (or one session standing for `calls` of them).

    `source` is always explicit on the wire. Callers that do not know theirs
    resolve it with current_source() BEFORE calling — the service is told, never
    left to guess.
    """
    try:
        c = _client()
        c.post("/note", json={
            "source": str(source or "?")[:24], "model": str(model or "")[:60],
            "tok": max(0, int(tok or 0)), "cache": max(0, int(cache or 0)),
            "usd": round(float(usd or 0), 4), "calls": max(1, int(calls or 1)),
            "ts": float(ts or 0)}).raise_for_status()
    except Exception as e:
        _degraded("note", e)        # metering must never break the thing being metered


def note_result(source: str, model: str, message) -> float:
    """Record an Agent SDK ResultMessage. Returns the usd so callers can keep
    using it.

    Stays conductor-side: it parses an SDK object, and only the numbers it pulls
    out of it cross the wire.
    """
    usd = float(getattr(message, "total_cost_usd", 0) or 0)
    u = getattr(message, "usage", None) or {}
    tok = cache = 0
    if isinstance(u, dict):
        tok = int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
        cache = (int(u.get("cache_read_input_tokens") or 0)
                 + int(u.get("cache_creation_input_tokens") or 0))
    note(source, model, tok=tok, cache=cache, usd=usd)
    # Logged HERE rather than at each call site, so a new spender cannot forget
    # to: this is the one function every Agent SDK session's result passes
    # through. It is also the record that survives the meter being down.
    try:
        from . import logs
        logs.debug("spend", "session_metered", f"{source} used {tok} tokens on {model}",
                   source=source, model=model, tok=tok, cache=cache)
    except Exception:
        pass
    return usd


def rows(since: float = 0.0) -> list[dict]:
    try:
        c = _client()
        r = c.get("/rows", params={"since": float(since or 0)})
        r.raise_for_status()
        return list(r.json().get("rows") or [])
    except Exception as e:
        _degraded("rows", e)
        return []


def window_hours() -> float:
    """The rolling window, read from the conductor's own tuning — no wire call.
    The service reads the SAME knob through /internal/tuning; this is here so
    callers that only want the number do not pay a round-trip."""
    try:
        return max(0.25, float(tuning.get("usage_window_h")))
    except Exception:
        return 5.0


def budget_tokens() -> int:
    try:
        return max(1000, int(tuning.get("usage_budget_tokens")))
    except Exception:
        return 1_000_000


def _zero_snapshot(now: float) -> dict:
    """What the meter says when it cannot say anything: zeros, and the flag that
    admits it. Every key the engine and the Improve screen read is present — a
    missing key is a KeyError three call frames away from the outage that caused
    it — and not one of them is invented."""
    return {"window_h": round(window_hours(), 2), "budget_tok": budget_tokens(),
            "used_tok": 0, "owner_tok": 0, "repair_tok": 0, "cache_tok": 0,
            "frac": 0.0, "owner_frac": 0.0, "idle_frac": 1.0,
            "allowance_tok": 0, "quiet_s": 0, "quiet_need_s": 0,
            "contended": False, "calls": 0, "usd": 0.0, "resets_at": 0.0,
            "by_source": {}, "degraded": True}


def snapshot(now: float | None = None) -> dict:
    """The whole utilization picture, in one dict the engine and the UI both
    read. Computed by the service against its table."""
    now = float(now or time.time())
    try:
        c = _client()
        r = c.get("/snapshot", params={"now": now})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _degraded("snapshot", e)
        return _zero_snapshot(now)


def verdict(now: float | None = None) -> tuple[bool, str, float]:
    """(quota_is_available, why_not, wake_ts) — the utilization half of the sleep
    decision.

    FAIL-SAFE when the meter is unreachable. Every other verb here shrugs; this
    one refuses, because "I cannot see the quota" and "the quota is free" are
    opposite answers and only one of them is safe to act on. The wake is bounded
    at FAILSAFE_WAKE_S so the refusal is a pause, not a shutdown: a flapping
    meter costs the crew five minutes, not a night.
    """
    now = float(now or time.time())
    try:
        c = _client()
        r = c.get("/verdict", params={"now": now})
        r.raise_for_status()
        v = r.json()
        return bool(v.get("ok")), str(v.get("why") or ""), float(v.get("wake") or 0)
    except Exception as e:
        _degraded("verdict", e)
        return False, "usage meter unreachable", now + FAILSAFE_WAKE_S


def health() -> bool:
    """Is the meter actually answering? The service's own /health — the same
    endpoint process-compose probes — asked through this door rather than around
    it, so there is exactly one place that knows the URL."""
    try:
        c = _client()
        r = c.get("/health")
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception as e:
        _degraded("health", e)
        return False


# --- lifecycle ----------------------------------------------------------------

def init() -> None:
    """Refuse to boot without the meter, and finish the strangler.

    The conductor keeps no meter state at all any more. Two kv keys are dropped
    here — in the module that used to write them — because both are now residue
    of a copy that has already happened:

      usage:ledger      the pre-P2 meter blob. The service copied it into
                        `usage_rows` on its own first boot (read-only ATTACH,
                        marked once). Commit A kept it so a rollback — unset the
                        URL — could find the data again; with the fallback
                        deleted it is only a second, staler copy of numbers
                        another process owns, and one that no longer grows.
      usage:backfilled  the one-shot guard on `backfill_repair()`, which imported
                        the crew's pre-meter `repair:ledger` into the blob above.
                        That job moved INTO the service's first-boot copy (same
                        marker, same ATTACH), so the guard guards nothing. Left
                        behind it would be a flag nobody reads, which is how a
                        reader concludes the import still runs here.

    `repair:ledger` itself STAYS: it is the crew's own call counter and still the
    backstop meter for boxes whose SDK reports no tokens. It was never usage's.
    """
    if not _URL:
        raise RuntimeError(_NO_URL)
    db.kv_delete("usage:ledger")
    db.kv_delete("usage:backfilled")
