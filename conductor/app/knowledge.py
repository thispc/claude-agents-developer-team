"""knowledge.py — the conductor's one door to what an agent has learned, stored so
it can be found again. Since P1 the store itself is a SERVICE — services/knowledge,
its own process on 8881, its own data/knowledge.db, its own committed contract —
and this file is the pure client that reaches it, keeping every public name the
in-process module ever had (remember / recall / reinforce / forget / stats / init /
backfill_from_sprints, plus health and _tokens) so nothing above it had to learn
that the store moved.

The P1 cutover finished here: the vendored in-process body (_knowledge_legacy.py)
and the dual-mode branch are gone, and `KNOWLEDGE_URL` is now REQUIRED. It is
written into data/env/conductor.env by tools/gen_fleet.py from services.yaml, so
every supported boot path — `./run-local.sh` and `./run-local.sh --legacy` alike
— has it. A conductor started by hand without it fails loudly in init() rather
than pretending to have a memory.

WHY init() AND NOT IMPORT. Importing this module must stay free: tools, tests and
the module graph import it without ever calling the store. Boot does call init(),
and that is the honest place to refuse — one clear message naming run-local.sh,
before the first sprint discovers its memory is gone.

LATENCY BUDGET: one localhost HTTP round-trip per verb, ~1-3ms in practice, 5ms
p50 budgeted; hard timeout 2s so a wedged service can never hold a sprint
hostage. A client is built per call — at the platform's call rates the ~0.1ms
construction cost buys freedom from event-loop lifetime bugs.

DEGRADED MODES (service down — every shape chosen so a sprint never blocks and
never lies):
    recall   → []                      with one deduped warn (not 180 an hour)
    remember → 0 (no row id, no-op)    same warn
    reinforce→ no-op                   same warn
    forget   → 0                       same warn
    stats    → {"total": 0, "rows": [], "backends": [], "degraded": True}
    _tokens  → []                      same warn (leak-checks go blind, honestly)
    backfill_from_sprints → 0 WITHOUT setting the done-marker, so the seed is
                            retried next boot instead of silently lost.
The verbs degrade for a MISSING url too, by the same path: init() is the loud
door, and a door that also killed every later call would turn one misconfigured
process into a crash loop.
"""

from __future__ import annotations

import os

import httpx

from . import config, db

_URL = (os.environ.get("KNOWLEDGE_URL") or "").strip().rstrip("/")

DIM = 256
LOCAL = f"hash-{DIM}"          # logs_routes names the free backend in its answers

_TIMEOUT = 2.0
# Tests inject an httpx transport here (ASGITransport onto the service app, or a
# transport that raises) — the client code path stays identical.
_TRANSPORT: httpx.AsyncBaseTransport | None = None
_TOKEN = ""

_NO_URL = (
    "KNOWLEDGE_URL is not set — the knowledge store is a service since P1 and the "
    "conductor has no in-process fallback any more. Boot with ./run-local.sh "
    "(or ./run-local.sh --legacy), which generates data/env/conductor.env from "
    "services.yaml and starts services/knowledge on 8881."
)


def _token() -> str:
    """The service's own token, read from where gen_fleet minted it — the same
    resolution the /svc gateway uses (routes/svc.py)."""
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
    # reinforce/forget/stats/_tokens keep their historical sync signatures; a
    # blocked event loop is bounded by the 2s timeout and, in practice, by the
    # localhost round-trip. Tests swap this factory for a TestClient.
    return httpx.Client(base_url=_URL, timeout=_TIMEOUT,
                        headers={"X-Service-Token": _token()})


def _degraded(verb: str, err: Exception) -> None:
    """One deduped warn per window, never a raise — the degraded shapes are the
    contract; the log line is how a 3am operator learns which one fired."""
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


# --- the four verbs, over the wire -------------------------------------------

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
        return []                      # nothing to ask: no wire call
    try:
        async with _client() as c:
            r = await c.post("/recall", json={
                "owner": owner, "query": query,
                # the service's contract bounds k to 1..25; clamp here rather
                # than earn a 422 that would read as an outage
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


def health() -> bool:
    """Is the store actually answering? The service's own /health — the same
    endpoint process-compose probes — asked through this door rather than around
    it, so the module graph's heartbeat and every verb agree on what "up" means
    and there is exactly one place that knows the URL.

    Unauthenticated by contract, but the token costs nothing to send.
    """
    try:
        with _sync_client() as c:
            r = c.get("/health")
            return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception as e:
        _degraded("health", e)
        return False


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


# --- lifecycle ---------------------------------------------------------------

def init() -> None:
    """Refuse to boot without the service, and finish the strangler.

    The conductor declares no knowledge table any more. `knowledge_legacy` is the
    table commit A renamed aside so the service could copy the rows out over a
    read-only ATTACH and so a rollback (unset the URL) could find them again;
    with the fallback deleted, keeping it would only be a second, staler copy of
    data another process now owns. Dropping it here — in the code that used to
    create it — is the last step of the extraction. IF EXISTS because most boots
    (a fresh box, every boot after the first) never had it.
    """
    if not _URL:
        raise RuntimeError(_NO_URL)
    db._execute("DROP TABLE IF EXISTS knowledge_legacy")


async def backfill_from_sprints(settings: dict | None = None) -> int:
    """Seed the store from sprints that already happened — conductor-side,
    because the sprint record is conductor kv; each lesson goes through
    remember() above.

    A knowledge base is useless on the day you build it and useful on the day it
    has been running a month, which makes the first month the hard part. This
    platform has already run 30-odd sprints and recorded every one, so that
    material is imported once. Only OUTCOMES, never intentions: a task nobody
    finished taught nothing, and filling a store with hopes is how it starts
    confidently answering questions nobody asked.
    """
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
