"""usage.py — the conductor's one door to the shared quota meter.

Every Claude-spending path on this box reports through here — repair scout/build
sessions, the project manager's session, worker task sessions, studio seats — so
the self-repair crew can answer the only two questions it really has: is anyone
else using the subscription right now, and how much of this window is gone.

Since P2 the meter itself is a SERVICE — services/usage, its own process on 8882,
its own `data/usage.db`, its own committed contract — and this file keeps every
public name the in-process module ever had (note / note_result / rows / snapshot /
verdict / attributed / current_source / backfill_repair / window_hours /
budget_tokens, plus health) so nothing above it had to learn that the meter moved.

WHY IT MOVED. The old meter was one kv blob rewritten whole on every note: read
the list, append, write it back, under a thread lock. A thread lock is not a
process lock, and the moment the platform became a fleet the lost update stopped
being theoretical — on the very number that decides whether the crew may spend
the owner's quota. The service replaces the blob with a real `usage_rows` table
and one INSERT per call. That is the whole point of the extraction; the process
boundary is just what stops anyone reaching back in.

DUAL-MODE, for the strangler window between commit A and commit B:

  USAGE_URL set    → the HTTP client below. gen_fleet writes the URL into
                     data/env/conductor.env from services.yaml, so a fleet boot
                     (./run-local.sh) is in this mode by construction.
  USAGE_URL unset  → the old in-process body, vendored unchanged in
                     _usage_legacy.py, INSTALLED WHOLESALE: this module replaces
                     itself in sys.modules with the legacy module, so fallback is
                     byte-identical to pre-P2 — same functions, same kv blob,
                     same introspectable source. That is the rollback between the
                     two commits; commit B deletes it.

The mode is decided at import: a process boots into one world and stays there —
flipping the env var mid-flight would half-migrate in-memory state.

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
    backfill_repair → 0 WITHOUT setting the done-marker, so the import is retried
                next boot instead of silently lost.
"""

from __future__ import annotations

import os
import sys

_URL = (os.environ.get("USAGE_URL") or "").strip().rstrip("/")

if not _URL:
    # Fallback mode: BE the legacy module. The import system re-reads sys.modules
    # after executing this file, so `from . import usage` everywhere yields the
    # legacy module itself — its functions, constants and source, unchanged.
    from . import _usage_legacy as _legacy
    sys.modules[__name__] = _legacy

else:
    import contextvars
    import time

    import httpx

    from . import config, db, tuning

    # The kv key the legacy blob lives under. Nothing in URL mode reads or writes
    # it; it is named here because commit B is what deletes it, and because
    # backfill_repair's sibling marker (`usage:backfilled`) still guards a
    # one-shot import that runs conductor-side.
    LEDGER_KEY = "usage:ledger"
    MAX_ROWS = 2000                 # the legacy blob's cap; the service's table has none

    # Sources that are the OWNER's work — the crew yields to these. Anything
    # tagged "repair" is the crew's own spend and must not make the crew think
    # the box is busy (it would then yield to itself and never wake). Duplicated
    # in the service, which is the side that acts on it; kept here because the
    # Improve screen and the tests read the tuple by name.
    OWNER_SOURCES = ("manager", "worker", "studio")

    _TIMEOUT = 2.0
    # The bounded wake of the fail-safe verdict. Five minutes: long enough that a
    # restarting meter is not hammered, short enough that a crew asleep on a flap
    # is awake again before anyone notices.
    FAILSAFE_WAKE_S = 300
    # Tests inject an httpx transport here (one that refuses, for the outage
    # drills) — the client code path stays identical. Sync, because every verb
    # here is.
    _TRANSPORT: httpx.BaseTransport | None = None
    _TOKEN = ""

    # Who to bill when the spending code cannot know. The crew's own deliberation
    # runs through `providers.complete` exactly like a Studio seat does — and if
    # that spend were filed as the owner's, the crew would see the box as busy,
    # yield, and put ITSELF to sleep forever. A contextvar (not a global) because
    # it has to follow one asyncio task and not leak into whatever else the event
    # loop is running at the same moment.
    #
    # It never reaches the service: it is resolved at the call site, and the wire
    # carries the resulting literal.
    _SOURCE = contextvars.ContextVar("usage_source", default="")

    class attributed:
        """`with usage.attributed("repair"):` — bill everything spent inside to
        that source."""

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
        """The source in force, for callers that are generic about who is
        spending. Read at the conductor's call sites — never on the wire."""
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
        # Every verb here keeps its historical SYNC signature — repair.meters()
        # and repair.headroom() are plain functions on the sleep path. A blocked
        # event loop is bounded by the 2s timeout and, in practice, by the
        # localhost round-trip. Tests swap this factory for a TestClient (the
        # mounted service), or set _TRANSPORT to one that refuses (the outage).
        return httpx.Client(base_url=_URL, timeout=_TIMEOUT, transport=_TRANSPORT,
                            headers={"X-Service-Token": _token()})

    def _degraded(verb: str, err: Exception) -> None:
        """One deduped warn per window, never a raise — the degraded shapes are
        the contract; the log line is how a 3am operator learns which one fired."""
        try:
            from . import logs
            logs.log("lifecycle", "usage_degraded",
                     f"usage service unreachable — {verb} degraded "
                     f"({type(err).__name__}: {str(err)[:120]})",
                     level="warn", dedupe_s=300, verb=verb)
        except Exception:
            pass

    # --- the verbs, over the wire --------------------------------------------

    def note(source: str, model: str = "", tok: int = 0, cache: int = 0,
             usd: float = 0.0, calls: int = 1, ts: float | None = None) -> None:
        """Record one model call (or one session standing for `calls` of them).

        `source` is always explicit on the wire. Callers that do not know theirs
        resolve it with current_source() BEFORE calling — the service is told,
        never left to guess.
        """
        try:
            with _client() as c:
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

        Stays conductor-side: it parses an SDK object, and only the numbers it
        pulls out of it cross the wire.
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
            with _client() as c:
                r = c.get("/rows", params={"since": float(since or 0)})
                r.raise_for_status()
                return list(r.json().get("rows") or [])
        except Exception as e:
            _degraded("rows", e)
            return []

    def window_hours() -> float:
        """The rolling window, read from the conductor's own tuning — no wire
        call. The service reads the SAME knob through /internal/tuning; this is
        here so callers that only want the number do not pay a round-trip."""
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
        """What the meter says when it cannot say anything: zeros, and the flag
        that admits it. Every key the engine and the Improve screen read is
        present — a missing key is a KeyError three call frames away from the
        outage that caused it — and not one of them is invented."""
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
            with _client() as c:
                r = c.get("/snapshot", params={"now": now})
                r.raise_for_status()
                return r.json()
        except Exception as e:
            _degraded("snapshot", e)
            return _zero_snapshot(now)

    def verdict(now: float | None = None) -> tuple[bool, str, float]:
        """(quota_is_available, why_not, wake_ts) — the utilization half of the
        sleep decision.

        FAIL-SAFE when the meter is unreachable. Every other verb here shrugs;
        this one refuses, because "I cannot see the quota" and "the quota is
        free" are opposite answers and only one of them is safe to act on. The
        wake is bounded at FAILSAFE_WAKE_S so the refusal is a pause, not a
        shutdown: a flapping meter costs the crew five minutes, not a night.
        """
        now = float(now or time.time())
        try:
            with _client() as c:
                r = c.get("/verdict", params={"now": now})
                r.raise_for_status()
                v = r.json()
                return bool(v.get("ok")), str(v.get("why") or ""), float(v.get("wake") or 0)
        except Exception as e:
            _degraded("verdict", e)
            return False, "usage meter unreachable", now + FAILSAFE_WAKE_S

    def health() -> bool:
        """Is the meter actually answering? The service's own /health — the same
        endpoint process-compose probes — asked through this door rather than
        around it, so there is exactly one place that knows the URL."""
        try:
            with _client() as c:
                r = c.get("/health")
                return r.status_code == 200 and bool(r.json().get("ok"))
        except Exception as e:
            _degraded("health", e)
            return False

    # --- lifecycle ------------------------------------------------------------

    def backfill_repair() -> int:
        """One-shot: import the crew's own historical sessions from `repair:ledger`.

        Conductor-side, because `repair:ledger` is conductor kv — the same
        division knowledge's backfill_from_sprints keeps. Each row goes through
        note() above.

        The crew kept its own ledger long before this shared meter existed, and
        until its rows show up here the meter reads "nothing used" on a box that
        has been working all week — which then lets the crude call-counter
        fallback keep deciding. Those rows predate per-call token reporting and
        carry only a cost, so they are imported as CALLS with no tokens: an
        honest zero beats an invented number.
        """
        try:
            if db.kv_get("usage:backfilled"):
                return 0
            # Preflight: with the meter down every note() below would quietly drop
            # and the marker would bury the import forever. Skip WITHOUT marking,
            # and the next boot tries again.
            if snapshot().get("degraded"):
                _degraded("backfill_repair", RuntimeError("preflight snapshot degraded"))
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
