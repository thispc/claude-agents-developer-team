"""knowledge.py — what an agent has learned, stored so it can be found again. Since P1
the store itself is the knowledge SERVICE (services/knowledge, its own process on 8881,
its own data/knowledge.db); this file is the conductor's one door to it, same public
names as ever: remember / recall / reinforce / forget / stats / init /
backfill_from_sprints, plus _tokens for lifeworld/ports.

DUAL-MODE, for the strangler window between commit A and commit B:

  KNOWLEDGE_URL set    → the HTTP client below. gen_fleet writes the URL into
                         data/env/conductor.env from services.yaml, so a fleet boot
                         (./run-local.sh) is in this mode by construction.
  KNOWLEDGE_URL unset  → the old in-process body, vendored unchanged in
                         _knowledge_legacy.py, INSTALLED WHOLESALE: this module
                         replaces itself in sys.modules with the legacy module, so
                         fallback mode is byte-identical to pre-P1 — same functions,
                         same table in devteam.db, same introspectable source.
                         (`./run-local.sh --legacy` and the offline test suite run
                         here.) Commit B deletes the legacy module and this branch.

The mode is decided at import: a process boots into one world and stays there —
flipping the env var mid-flight would otherwise half-migrate in-memory state.

LATENCY BUDGET (URL mode): one localhost HTTP round-trip per verb, ~1-3ms in
practice, 5ms p50 budgeted; hard timeout 2s so a wedged service can never hold a
sprint hostage. A client is built per call — at the platform's call rates the
~0.1ms construction cost buys freedom from event-loop lifetime bugs.

DEGRADED MODES (URL mode, service down — every shape chosen so a sprint never
blocks and never lies):
    recall   → []                      with one deduped warn (not 180 an hour)
    remember → 0 (no row id, no-op)    same warn
    reinforce→ no-op                   same warn
    forget   → 0                       same warn
    stats    → {"total": 0, "rows": [], "backends": [], "degraded": True}
    _tokens  → []                      same warn (leak-checks go blind, honestly)
    backfill_from_sprints → 0 WITHOUT setting the done-marker, so the seed is
                            retried next boot instead of silently lost.

MIGRATION, handled in init() rather than db.py's migration tuple because the
knowledge schema never lived there (it was knowledge.py's own SCHEMA, created by
its own init — the same precedent modgraph follows): on a URL-mode boot, an
existing `knowledge` table is RENAMED to `knowledge_legacy` — renamed, not
dropped, because (a) the service's first-boot backfill copies the rows out of it
over a read-only ATTACH, and (b) rollback (unset the URL) must find the data
again — _knowledge_legacy.init() renames it back. Commit B drops it for good.
"""

from __future__ import annotations

import os
import sys

_URL = (os.environ.get("KNOWLEDGE_URL") or "").strip().rstrip("/")

if not _URL:
    # Fallback mode: BE the legacy module. The import system re-reads sys.modules
    # after executing this file, so `from . import knowledge` everywhere yields
    # the legacy module itself — its functions, constants and source, unchanged.
    from . import _knowledge_legacy as _legacy
    sys.modules[__name__] = _legacy

else:
    import httpx

    from . import config, db

    DIM = 256
    LOCAL = f"hash-{DIM}"          # logs_routes names the free backend in its answers

    _TIMEOUT = 2.0
    # Tests inject an httpx transport here (ASGITransport onto the service app,
    # or a MockTransport that raises) — the client code path stays identical.
    _TRANSPORT: httpx.AsyncBaseTransport | None = None
    _TOKEN = ""

    def _token() -> str:
        """The service's own token, read from where gen_fleet minted it — the
        same resolution the /svc gateway uses (routes/svc.py)."""
        global _TOKEN
        if not _TOKEN:
            try:
                _TOKEN = (config.ROOT / "data" / "tokens" / "knowledge.token") \
                    .read_text().strip()
            except OSError:
                _TOKEN = ""
        return _TOKEN

    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=_URL, timeout=_TIMEOUT, transport=_TRANSPORT,
                                 headers={"X-Service-Token": _token()})

    def _sync_client() -> httpx.Client:
        # reinforce/forget/stats/_tokens keep their historical sync signatures;
        # a blocked event loop is bounded by the 2s timeout and, in practice, by
        # the localhost round-trip. Tests swap this factory for a TestClient.
        return httpx.Client(base_url=_URL, timeout=_TIMEOUT,
                            headers={"X-Service-Token": _token()})

    def _degraded(verb: str, err: Exception) -> None:
        """One deduped warn per window, never a raise — the degraded shapes are
        the contract; the log line is how a 3am operator learns which one fired."""
        try:
            from . import logs
            # "lifecycle" is the vocabulary's own word for a process being up or
            # down — logs.log() coerces anything unknown, and a silently coerced
            # category is a filter that quietly stops working.
            logs.log("lifecycle", "knowledge_degraded",
                     f"knowledge service unreachable — {verb} degraded "
                     f"({type(err).__name__}: {str(err)[:120]})",
                     level="warn", dedupe_s=300, verb=verb)
        except Exception:
            pass

    # --- the four verbs, over the wire ---------------------------------------

    async def remember(owner: str, cue: str, says: str, *, kind: str = "belief",
                       sig: str = "", payload: dict | None = None, good: int = 0,
                       bad: int = 0, settings: dict | None = None) -> int:
        try:
            async with _client() as c:
                r = await c.post("/remember", json={
                    "owner": owner, "cue": str(cue or ""), "says": str(says or ""),
                    "kind": kind, "sig": sig, "payload": payload or {},
                    "good": int(good), "bad": int(bad), "settings": settings or {}})
                r.raise_for_status()
                return int(r.json().get("id") or 0)
        except Exception as e:
            _degraded("remember", e)
            return 0

    async def recall(owner: str, query: str, k: int = 5, *, kind: str = "",
                     settings: dict | None = None,
                     include_global: bool = True) -> list[dict]:
        if not str(query or "").strip():
            return []                      # the legacy fast-path, no wire call
        try:
            async with _client() as c:
                r = await c.post("/recall", json={
                    "owner": owner, "query": query,
                    # the service's contract bounds k to 1..25; clamp like the
                    # legacy body did instead of earning a 422
                    "k": max(1, min(int(k), 25)), "kind": kind,
                    "include_global": bool(include_global),
                    "settings": settings or {}})
                r.raise_for_status()
                return list(r.json().get("hits") or [])
        except Exception as e:
            _degraded("recall", e)
            return []

    def reinforce(row_id: int, outcome: str) -> None:
        if outcome not in ("good", "bad"):
            return
        try:
            with _sync_client() as c:
                c.post("/reinforce",
                       json={"id": int(row_id), "outcome": outcome}).raise_for_status()
        except Exception as e:
            _degraded("reinforce", e)

    def forget(owner: str, *, row_id: int = 0, sig: str = "") -> int:
        try:
            with _sync_client() as c:
                r = c.post("/forget", json={"owner": owner, "row_id": int(row_id),
                                            "sig": sig})
                r.raise_for_status()
                return int(r.json().get("removed") or 0)
        except Exception as e:
            _degraded("forget", e)
            return 0

    def stats(owner: str = "") -> dict:
        try:
            with _sync_client() as c:
                r = c.get("/stats", params={"owner": owner})
                r.raise_for_status()
                return r.json()
        except Exception as e:
            _degraded("stats", e)
            return {"total": 0, "rows": [], "backends": [], "degraded": True}

    def _tokens(text: str) -> list[str]:
        # lifeworld/ports.knowledge_tokens — the old private reach-in, contract now.
        try:
            with _sync_client() as c:
                r = c.post("/tokens", json={"text": str(text or "")})
                r.raise_for_status()
                return list(r.json().get("tokens") or [])
        except Exception as e:
            _degraded("tokens", e)
            return []

    # --- lifecycle -----------------------------------------------------------

    def init() -> None:
        """URL mode owns no table — this is the one-way door of commit A: rename
        the conductor's knowledge table aside (see the module docstring for why
        rename, not drop). Runs inside the conductor's normal init sequence, so
        it needs db.init() to have happened — same ordering main.py always had."""
        names = {r["name"] for r in db._rows(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name IN ('knowledge','knowledge_legacy')")}
        if "knowledge" in names and "knowledge_legacy" not in names:
            db._execute("ALTER TABLE knowledge RENAME TO knowledge_legacy")

    async def backfill_from_sprints(settings: dict | None = None) -> int:
        """Seed the store from sprints that already happened — conductor-side,
        because the sprint record is conductor kv; each lesson goes through
        remember() above. See _knowledge_legacy.backfill_from_sprints for the
        full reasoning; this is the same import loop over the wire."""
        if db.kv_get("knowledge:backfilled"):
            return 0
        # Preflight: if the service is unreachable, every remember() below would
        # quietly no-op and the marker would bury the seed forever. Skip WITHOUT
        # marking, and the next boot tries again.
        if stats().get("degraded"):
            _degraded("backfill_from_sprints", RuntimeError("preflight stats degraded"))
            return 0
        n = 0
        try:
            for rec in sorted((db.kv_prefix("repair:sprint:") or {}).values(),
                              key=lambda r: r.get("no", 0)):
                for t in rec.get("tasks", []) or []:
                    title = str(t.get("title") or "").strip()
                    if not title:
                        continue
                    v = t.get("verification") or {}
                    if t.get("status") == "landed":
                        cue, says, good, bad = title, f"this worked: {title}", 1, 0
                    elif t.get("status") == "failed":
                        why = str(t.get("error") or v.get("headline") or "").strip()
                        if not why:
                            continue
                        cue, says, good, bad = f"{title} — {why}", f"this failed: {why}", 0, 1
                    else:
                        continue                  # still open: it has taught nothing yet
                    await remember("global", cue=cue, says=says, kind="episode",
                                   payload={"sprint": rec.get("no"),
                                            "factor": t.get("factor", "")},
                                   good=good, bad=bad, settings=settings)
                    n += 1
            db.kv_set("knowledge:backfilled", True)
        except Exception:
            pass
        return n
