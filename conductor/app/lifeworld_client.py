"""lifeworld_client.py — the conductor's one door to the lifeworld substrate.

Since P4 the Lifeworld is a SERVICE: services/lifeworld, its own process on 8885,
its own `data/lifeworld.db`, its own committed contract — the whole 26-file
package, the 35 `/api/lw/*` handler bodies and the `lw_worlds` blob. This file is
the client that reaches it.

The P4 cutover finished here: the in-process package (`conductor/app/lifeworld/`),
the `_local_*` half of every function below and the dual-mode route class are gone,
and `LIFEWORLD_URL` is now REQUIRED. It is written into data/env/conductor.env by
tools/gen_fleet.py from services.yaml, so every supported boot path —
`./run-local.sh` and `./run-local.sh --legacy` alike — has it. A conductor started
by hand without it fails loudly in init() rather than serving a Studio with no
world behind it.

WHY init() AND NOT IMPORT. Importing this module must stay free: tools, tests and
the module graph import it without ever reaching the substrate. Boot does call
init(), and that is the honest place to refuse — one clear message naming
run-local.sh, before the first canvas discovers there is nothing behind it.

WHAT CROSSES THE WIRE, AND WHAT NEVER DOES. Every call carries the CALLER, as five
stamp headers the service treats as vouched-for (see services/lifeworld's
caller.py): who the owner is, whether they are root, which principal the model door
should bill, and whether they may spend on authoring. What never crosses is a
credential: `settings_ref` is a signed reference (auth.mint_settings_ref) and only
the conductor can resolve one.

DEGRADED MODES (service down — every shape chosen so nothing lies):
    /api/lw/*         → 503 with a readable reason. The Studio canvas says the
                        substrate is unavailable instead of rendering an empty
                        world that the next save would then persist.
    seat_crew         → None, so repair.ensure_team returns None and the sprint
                        tick logs + sleeps with the reason "lifeworld down".
                        PAUSING IS THE HONEST BEHAVIOUR: a crew that kept
                        sprinting without its specialists would still be spending,
                        just anonymously. The sleep is bounded at 60s, so it wakes
                        on recovery without a restart and without losing a sprint.
    context/consult   → None / a declined consult, and the build carries on
    /review             anonymously and unreviewed — both already fail open.
    decision/outcome  → no-op, and the association simply is not recorded. A
                        learning system that blocks the work to learn is worse
                        than one that misses a lesson.
    room_members      → None → the Atlas room panel reads "unavailable" and the
    /rooms/usage        assignment pool says so rather than showing an empty team.
    health            → False, which is what turns the module graph card red.
The verbs degrade for a MISSING url too, by the same path: init() is the loud
door, and a door that also killed every later call would turn one misconfigured
process into a crash loop.

LATENCY BUDGET: one localhost round trip per verb, ~1-3ms; timeout 5s for the
canvas verbs and 300s for the ones with a real model call behind them. A six-agent
deliberation measured 3m46s live — minutes of provider time, not milliseconds of
ours — so the slow timeout is sized for the work, and the fast one is sized so a
wedged service can never hold the canvas.
"""

from __future__ import annotations

import os

import httpx
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse, Response

from . import config, db, pool

_URL = (os.environ.get("LIFEWORLD_URL") or "").strip().rstrip("/")

_TIMEOUT = 5.0            # the canvas verbs: a localhost round trip and a blob
_SLOW_TIMEOUT = 300.0     # deliberate / consult / review / chat: a model behind it

# Tests inject an httpx transport here (ASGITransport onto the mounted service,
# or one that raises) — the client code path stays identical.
_TRANSPORT: httpx.AsyncBaseTransport | None = None
_TOKEN = ""

DOWN = ("the lifeworld service is not answering — the Studio canvas and the "
        "self-repair crew both live in it. Check the fleet "
        "(data/logs/fleet.log), or start it with ./run-local.sh.")

_NO_URL = (
    "LIFEWORLD_URL is not set — the Studio's substrate and the self-repair crew's "
    "world are a service since P4 and the conductor has no in-process fallback any "
    "more. Boot with ./run-local.sh (or ./run-local.sh --legacy), which generates "
    "data/env/conductor.env from services.yaml and starts services/lifeworld on 8885."
)


def _token() -> str:
    """The service's own token, read from where gen_fleet minted it — the same
    resolution the /svc gateway uses (routes/svc.py)."""
    global _TOKEN
    if not _TOKEN:
        try:
            _TOKEN = (config.ROOT / "data" / "tokens" / "lifeworld.token") \
                .read_text().strip()
        except OSError:
            _TOKEN = ""
    return _TOKEN


def _client() -> httpx.AsyncClient:
    """The SHARED async client, one per running event loop.

    THE TIMEOUT MOVED TO THE CALL. It used to be a constructor argument, which
    forced a client per distinct timeout (the 60s proxy, the 2s health) and
    therefore a client per call. httpx takes `timeout=` on the request itself, so
    one pooled client serves every deadline and the slow proxy no longer builds a
    connection nobody reuses. Never close what this returns."""
    return pool.async_client("lifeworld", base_url=_URL or "http://lifeworld.invalid",
                             timeout=_TIMEOUT, transport=_TRANSPORT,
                             headers={"X-Service-Token": _token()})


def _sync_client() -> httpx.Client:
    """The SYNC half. `repair.ensure_team`, `note_decision`, `team_usage` and the
    module graph's assignment pool are plain functions on paths that have never
    been awaitable; making them async would ripple through twenty call sites for
    no gain a localhost round trip can measure. Tests swap this factory for a
    TestClient on the mounted service — the same seam usage.py and knowledge.py
    use. Shared, and per-call deadlines ride the request (see _client)."""
    return pool.sync_client("lifeworld:sync",
                            base_url=_URL or "http://lifeworld.invalid",
                            timeout=_TIMEOUT,
                            headers={"X-Service-Token": _token()})


def _degraded(verb: str, err: Exception) -> None:
    """One deduped warn per window, never a raise — the degraded shapes are the
    contract; the log line is how a 3am operator learns which one fired."""
    try:
        from . import logs
        logs.log("lifecycle", "lifeworld_degraded",
                 f"lifeworld service unreachable — {verb} degraded "
                 f"({type(err).__name__}: {str(err)[:120]})",
                 level="warn", dedupe_s=300, verb=verb)
    except Exception:
        pass


# --- the caller stamp --------------------------------------------------------

def stamp(user: dict | None, *, source: str = "studio", author: bool | None = None) -> dict:
    """The five headers that say who this request is for.

    Authentication already happened here — this is the conductor vouching, on a
    request it authenticated, for an identity the service has no way to check on
    its own. `settings_ref` is SIGNED, so a service cannot name a principal the
    conductor did not.
    """
    from . import auth
    if not user:
        return {}
    can_author = auth.has_own_ai_credentials(user) if author is None else bool(author)
    return {"X-Lw-Owner": str(user["id"]),
            "X-Lw-Root": "1" if user.get("is_root") else "0",
            "X-Lw-Settings": auth.mint_settings_ref(user),
            "X-Lw-Source": source,
            "X-Lw-Author": "1" if can_author else "0"}


def root_stamp(source: str = "repair") -> dict:
    """The crew's own stamp: the root account, billed to the engine that spends.

    The crew works on the platform itself, so it runs on the operator's
    credentials — the same resolution `repair._root_settings()` always did, only
    now it is a reference instead of the keys themselves.
    """
    from . import auth
    u = auth.get_user_by_name(auth.ROOT_USERNAME)
    if not u:
        return {}
    return stamp(u, source=source, author=True)


# --- the /api/lw proxy -------------------------------------------------------

async def proxy(request: Request, path: str) -> Response:
    """Forward one already-authenticated /api/lw request to the service.

    Path rewrite only: `/api/lw/<rest>` → `/worlds/<rest>` and `/api/lw` →
    `/worlds`. The dashboard hardcodes the `/api/lw/*` paths in fifty-odd places
    and they MUST NOT MOVE; the service's own prefix exists only so `/health` and
    `/openapi.json` have somewhere to live that a world id cannot collide with.

    Streamed? No — buffered, on purpose. Every one of these bodies is a JSON world
    view measured in kilobytes, and buffering is what lets the 503 below be a
    readable JSON object instead of a half-written stream the canvas would have to
    guess about.

    The session cookie is deliberately NOT forwarded — the service authenticates
    by token and identifies the caller by the stamp, and handing it user cookies
    would tempt exactly the coupling the contract forbids.
    """
    from .guards import current_user
    user = current_user(request)
    target = f"/worlds/{path}" if path else "/worlds"
    body = await request.body()
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    headers.update(stamp(user))
    try:
        c = _client()
        r = await c.request(request.method, target, params=request.query_params,
                            content=body or None, headers=headers, timeout=_SLOW_TIMEOUT)
    except Exception as e:
        _degraded(f"proxy {request.method} {target}", e)
        return JSONResponse({"detail": DOWN, "degraded": True}, status_code=503)
    return _passthrough(r)


async def studio_get(user: dict, path: str, params: dict | None = None) -> dict:
    """One Studio GET, parsed — for the two conductor routes that COMPOSE rather
    than forward (the world list, which hides the crew's own world, and the agent
    detail panel, which decorates root's copy with the watch service's log rows).
    Everything else is a byte-for-byte proxy."""
    try:
        c = _client()
        r = await c.get(f"/worlds{path}", params=params or {}, headers=stamp(user))
    except Exception as e:
        _degraded(f"GET {path}", e)
        raise HTTPException(503, DOWN)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:
            detail = None
        raise HTTPException(r.status_code, detail or "the lifeworld service refused that")
    return r.json()


def _passthrough(r: httpx.Response) -> Response:
    """The service's answer, unchanged. Content-type is copied rather than assumed
    so a 422 body from FastAPI's own validation reaches the browser intact."""
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))


# --- calls the conductor makes on its own behalf ----------------------------

def _deadline(timeout: float | None) -> dict:
    """A per-request timeout, or nothing at all — the shared client already carries
    `_TIMEOUT`, and only the SLOW verbs (the Studio proxy, the crew's seating and
    deliberations) name their own. See modgraph._deadline for why it is **kwargs."""
    return {} if timeout is None else {"timeout": timeout}


async def _get(path: str, *, headers: dict, params: dict | None = None,
               timeout: float | None = None):
    c = _client()
    r = await c.get(path, params=params or {}, headers=headers, **_deadline(timeout))
    r.raise_for_status()
    return r.json()


async def _post(path: str, payload: dict, *, headers: dict,
                params: dict | None = None, timeout: float | None = None):
    c = _client()
    r = await c.post(path, json=payload, params=params or {}, headers=headers,
                     **_deadline(timeout))
    r.raise_for_status()
    return r.json()


def health() -> bool:
    """Is the substrate actually answering? Its own /health — the same endpoint
    process-compose probes — asked through this door rather than around it, so
    the module graph's heartbeat and every verb agree on what "up" means."""
    try:
        c = _sync_client()
        r = c.get("/health", timeout=2.0)
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False


def init() -> None:
    """Refuse to boot without the substrate, and finish the strangler.

    The conductor declares no `lw_worlds` table any more. It is dropped here — in
    the module that replaced the code which used to read it — but ONLY once the
    service says it has finished with it, and that condition is the whole point of
    this function rather than a formality.

    NOTHING ORDERS THE TWO PROCESSES. process-compose starts the conductor and the
    lifeworld service together with no `depends_on` between them, so on the first
    boot after the cutover this can easily run before the service has attached and
    copied. Dropping `lw_worlds` at that moment does not lose data anyone can shrug
    at, and it is not the P3 decisions storm either — it is EVERY WORLD ON THE BOX:
    the operator's Studio teams, and the self-repair crew's own world with every
    association each specialist ever proved hanging off human ids that would never
    exist again. `GET /health` answers `backfilled` for exactly this; it means "the
    first-boot decision has been made, either way", including the boring cases
    where there was nothing to copy. Until it is true the table stays and the next
    boot tries again — a dead table costs a few kilobytes, and nothing in the
    conductor reads it ever again.

    P4-A ALSO DID NOT RENAME IT. The earlier phases renamed their legacy table
    aside; here the rollback was the package itself, which read `lw_worlds` by
    name, so renaming would have made a rollback find an empty table and re-seat
    the crew with new ids. The rows stayed put and the service copied them out with
    the ROWIDS PRESERVED, because every world id on the platform is a pointer at
    one.
    """
    if not _URL:
        raise RuntimeError(_NO_URL)
    try:
        c = _sync_client()
        r = c.get("/health", timeout=2.0)
        r.raise_for_status()
        settled = bool(r.json().get("backfilled"))
    except Exception as e:
        # Deliberately not fatal. A conductor that refused to boot because a PEER
        # was still starting is how a fleet takes itself down in a ring, and the
        # only thing waiting on this answer is a table nobody reads.
        _degraded("init", e)
        return
    if settled:
        db._execute("DROP TABLE IF EXISTS lw_worlds")


# --- the crew's verbs --------------------------------------------------------
#
# Each one is a WHOLE BEHAVIOUR, because each is a read-modify-write on a world
# blob and `store.lock_for` can only be held on the service's side of the wire.
#
# SYNC OR ASYNC follows the CALLER, not taste. `repair.ensure_team`,
# `note_decision`, `team_usage` and the module graph's pool are plain functions
# on paths that have never been awaitable, and turning them async would ripple
# through twenty call sites and every test that drives them. A blocked event loop
# is bounded by the timeout and, in practice, by the localhost round trip — the
# same trade usage.py documents for the meter.

def seat_crew(world_id: int, factors: list[dict], *, manager: dict, protocol: dict,
              scene_name: str, current_room_id: int,
              world_name: str = "devteam IT crew") -> dict | None:
    """Seat the crew — adopting a surviving room before rebuilding one.

    None when the substrate is unreachable, and that is what makes the sprint tick
    sleep with an honest reason instead of running a crew that is not there.
    """
    try:
        c = _sync_client()
        r = c.post(f"/worlds/{int(world_id)}/crew-seating", json={
            "factors": factors, "manager": manager, "protocol": protocol,
            "scene_name": scene_name, "world_name": world_name,
            "current_room_id": int(current_room_id or 0)}, headers=root_stamp(), timeout=30.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _degraded("seat_crew", e)
        return None


def crew_decision(world_id: int, human_id: int, saw: str, understood: str,
                  chose: str, because: dict) -> dict | None:
    try:
        c = _sync_client()
        r = c.post(f"/worlds/{int(world_id)}/crew-decision",
                   json={"human_id": int(human_id), "saw": saw,
                         "understood": understood, "chose": chose,
                         "because": because}, headers=root_stamp())
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _degraded("crew_decision", e)
        return None


def crew_decision_node(world_id: int, human_id: int, decision_id: int) -> dict | None:
    """One recorded decision, read back. Not on any hot path — the engine writes and
    stamps, never reads — but the drills assert that an outcome really landed on the
    specialist, and a claim only checkable by opening the service's database would not
    be a claim about the boundary at all."""
    try:
        c = _sync_client()
        r = c.post(f"/worlds/{int(world_id)}/crew-decision-get",
                   json={"human_id": int(human_id), "decision_id": int(decision_id),
                         "ok": True, "says": ""}, headers=root_stamp())
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        _degraded("crew_decision_node", e)
        return None


def crew_usage(world_id: int, room_id: int) -> list[dict] | None:
    try:
        c = _sync_client()
        r = c.get(f"/worlds/{int(world_id)}/crew-usage",
                  params={"room_id": int(room_id)}, headers=root_stamp())
        r.raise_for_status()
        return list(r.json().get("agents") or [])
    except Exception as e:
        _degraded("crew_usage", e)
        return None


def crew_chat_note(world_id: int, room_id: int, thread_id: int, text: str,
                   role: str = "manager") -> bool:
    try:
        c = _sync_client()
        r = c.post(f"/worlds/{int(world_id)}/crew-chat-note",
                   json={"room_id": int(room_id), "thread_id": int(thread_id),
                         "role": role, "text": text}, headers=root_stamp())
        r.raise_for_status()
        return bool(r.json().get("ok"))
    except Exception as e:
        _degraded("crew_chat_note", e)
        return False


def room_alive(world_id: int, room_id: int) -> bool:
    """Is the room the crew's kv record points at actually there?

    The freshness check used to trust the pointer if the WORLD loaded; a deleted or
    never-persisted room then 404'd the canvas and silently un-staffed every build
    (the factor→agent ids resolve to nobody) while the check kept saying fine.
    """
    try:
        c = _sync_client()
        return c.get(f"/worlds/{int(world_id)}/room/{int(room_id)}",
                     headers=root_stamp()).status_code == 200
    except Exception:
        return False


def room_view(world_id: int, room_id: int, user: dict | None = None) -> dict | None:
    """One room exactly as the canvas sees it — cast, props, threads and the last of
    the room log. The same answer `/api/lw/{wid}/room/{rid}` proxies to a browser,
    asked from inside the conductor for the crew's own room."""
    from . import repair
    try:
        c = _sync_client()
        r = c.get(f"/worlds/{int(world_id)}/room/{int(room_id)}",
                  headers=stamp(user or repair._root_user() or {}))
        if r.status_code != 200:
            return None
        return r.json().get("room")
    except Exception as e:
        _degraded("room_view", e)
        return None


def room_members(world_id: int, room_id: int, user: dict) -> dict | None:
    """One room as a staffing pool, or None — which the module graph reads as
    "this room is gone" and answers with an honest empty pool."""
    try:
        c = _sync_client()
        r = c.get(f"/worlds/{int(world_id)}/room/{int(room_id)}/members",
                  headers=stamp(user))
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        _degraded("room_members", e)
        return None


def rooms(user: dict, extra_world_ids: list[int]) -> list[dict] | None:
    try:
        c = _sync_client()
        r = c.get("/rooms", headers=stamp(user),
                  params={"extra": ",".join(str(int(i)) for i in extra_world_ids)})
        r.raise_for_status()
        return list(r.json().get("rooms") or [])
    except Exception as e:
        _degraded("rooms", e)
        return None


async def crew_context(world_id: int, room_id: int, thread_id: int, human_id: int,
                       cue: str) -> dict | None:
    """Who is building and what it has PROVEN — the lifeworld half of a specialist
    briefing. The knowledge half stays with the conductor, which holds the key."""
    try:
        return await _post(f"/worlds/{int(world_id)}/crew-context",
                           {"room_id": int(room_id), "thread_id": int(thread_id),
                            "human_id": int(human_id), "cue": cue},
                           headers=root_stamp())
    except Exception as e:
        _degraded("crew_context", e)
        return None


async def crew_outcome(world_id: int, human_id: int, decision_id: int, ok: bool,
                       says: str) -> dict | None:
    try:
        return await _post(f"/worlds/{int(world_id)}/crew-outcome",
                           {"human_id": int(human_id), "decision_id": int(decision_id),
                            "ok": bool(ok), "says": says}, headers=root_stamp())
    except Exception as e:
        _degraded("crew_outcome", e)
        return None


async def crew_consult(world_id: int, room_id: int, thread_id: int, human_id: int,
                       question: str, who: str = "", live: bool = True) -> dict | None:
    try:
        return await _post(f"/worlds/{int(world_id)}/crew-consult",
                           {"room_id": int(room_id), "thread_id": int(thread_id),
                            "human_id": int(human_id), "question": question,
                            "who": who},
                           headers=root_stamp() if live else root_stamp_free(),
                           timeout=_SLOW_TIMEOUT)
    except Exception as e:
        _degraded("crew_consult", e)
        return None


async def crew_review(world_id: int, room_id: int, thread_id: int, human_id: int,
                      question: str, cue: str = "") -> dict | None:
    try:
        return await _post(f"/worlds/{int(world_id)}/crew-review",
                           {"room_id": int(room_id), "thread_id": int(thread_id),
                            "human_id": int(human_id), "question": question,
                            "cue": cue}, headers=root_stamp(), timeout=_SLOW_TIMEOUT)
    except Exception as e:
        _degraded("crew_review", e)
        return None


async def crew_deliberate(world_id: int, room_id: int, thread_id: int, *, topic: str,
                          rulebook: str, rounds: int, live: bool) -> dict | None:
    try:
        return await _post(f"/worlds/{int(world_id)}/crew-deliberate",
                           {"room_id": int(room_id), "thread_id": int(thread_id),
                            "topic": topic, "rulebook": rulebook,
                            "rounds": int(rounds)},
                           headers=root_stamp() if live else root_stamp_free(),
                           timeout=_SLOW_TIMEOUT)
    except Exception as e:
        _degraded("crew_deliberate", e)
        return None


async def crew_chat(world_id: int, room_id: int, thread_id: int, text: str,
                    live: bool) -> dict:
    """The operator talking to the crew's hidden manager. The same thread-chat
    endpoint the Studio uses; only the stamp differs — the crew's world belongs to
    root and its spend is billed to the engine.

    This one RAISES rather than degrading: it answers a click, and a chat box that
    silently returns nothing is worse than one that says the substrate is down.
    """
    try:
        return await _post(f"/worlds/{int(world_id)}/room/{int(room_id)}"
                           f"/thread/{int(thread_id)}/chat",
                           {"to": "manager", "text": text},
                           headers=root_stamp() if live else root_stamp_free(),
                           params={"live": 1 if live else 0}, timeout=_SLOW_TIMEOUT)
    except HTTPException:
        raise
    except Exception as e:
        _degraded("crew_chat", e)
        raise HTTPException(503, DOWN)


async def create_world(user: dict, name: str) -> int:
    out = await _post("/worlds", {"name": name}, headers=stamp(user))
    return int((out.get("world") or {}).get("id") or 0)


async def apply_manifest(user: dict, world_id: int, body: dict) -> dict:
    """A whole team as one spec, applied to a world. Used by the project wizard's
    "build me a team" path; the Studio's own route goes through the proxy."""
    return await _post(f"/worlds/{int(world_id)}/manifest", body, headers=stamp(user),
                       timeout=_SLOW_TIMEOUT)


def root_stamp_free() -> dict:
    """The crew's stamp with NO settings reference — the free deterministic
    substrate. `_live()` is false offline, and a world loaded live with nothing to
    resolve would spend a round trip per agent to learn that."""
    s = dict(root_stamp())
    if s:
        s["X-Lw-Settings"] = ""
        s["X-Lw-Author"] = "0"
    return s
