"""lifeworld_client.py — the conductor's one door to the lifeworld substrate.

Since P4 the Lifeworld is a SERVICE: services/lifeworld, its own process on 8885,
its own `data/lifeworld.db`, its own committed contract. This file is the client
that reaches it, and — for the duration of the P4-A/P4-B strangler — the switch
that chooses between the service and the in-process package still sitting in
`conductor/app/lifeworld/`.

DUAL MODE, and how it ends. `LIFEWORLD_URL` set (every supported boot path since
tools/gen_fleet.py learned the registry entry) → every call below goes over
httpx. Unset → the vendored package runs in-process exactly as it did before,
which is the rollback: `unset LIFEWORLD_URL` and the conductor is the monolith
again, world blobs and all. Commit B deletes `conductor/app/lifeworld/`, the
`_local_*` half of every function here, and this paragraph; what is left is a
pure client.

    Rollback between A and B = unset the URL.   After B = git revert B; the
    service's own database survives either way, because it was copied out, never
    moved.

THE LEGACY TABLE IS NOT RENAMED, and that is a deliberate departure from P1/P2/P3.
Those phases renamed the conductor's table aside on first boot in URL mode. Here
the rollback is the PACKAGE, not a vendored copy, and the package reads
`lw_worlds` — so renaming it would mean a rollback found an empty table, `db.init`
recreated it, and the crew re-seated itself with new human ids, orphaning every
knowledge row keyed to the old ones. That is the one failure this phase spends the
most care avoiding. So the rows stay where they are, the service copies them out
once (ids preserved), and a rollback reads them again — stale by whatever the
service did since, which is the honest and much smaller cost. Commit B drops the
table, by then genuinely unread.

WHY A PACKAGE AND NOT A VENDORED FILE. P1/P2/P3 each vendored one legacy module
(`_knowledge_legacy.py` and friends). The lifeworld is twenty-six files and
nearly five thousand lines; a second copy inside the conductor would be a
maintenance trap for the days between two commits, and the package is already
sitting there untouched. So the fallback IS the package, in place, and the only
thing that is duplicated is the ~80 lines of crew seating below — which stage B
deletes along with it.

WHAT CROSSES THE WIRE, AND WHAT NEVER DOES. Every call carries the CALLER, as
five stamp headers the service treats as vouched-for (see services/lifeworld's
caller.py): who the owner is, whether they are root, which principal the model
door should bill, and whether they may spend on authoring. What never crosses is
a credential: `settings_ref` is a signed reference (auth.mint_settings_ref) and
only the conductor can resolve one.

DEGRADED MODES (service down — every shape chosen so nothing lies):
    /api/lw/*         → 503 with a readable reason. The Studio canvas says the
                        substrate is unavailable instead of rendering an empty
                        world that the next save would then persist.
    seat_crew         → None, so repair.ensure_team returns None and the sprint
                        tick logs + sleeps with the reason "lifeworld down".
                        PAUSING IS THE HONEST BEHAVIOUR: a crew that kept
                        sprinting without its specialists would still be
                        spending, just anonymously. The sleep is bounded, so it
                        wakes on recovery without a restart.
    context/consult   → None / a declined consult, and the build carries on
    /review             anonymously and unreviewed — both already fail open.
    decision/outcome  → no-op, and the association simply is not recorded. A
                        learning system that blocks the work to learn is worse
                        than one that misses a lesson.
    room_members      → None → the Atlas room panel reads "unavailable" and the
    /rooms/usage        assignment pool falls back to empty.

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

from . import config

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


def enabled() -> bool:
    """Client mode. Checked at CALL time, not at import: the P4-A rollback is
    `unset LIFEWORLD_URL`, and a mode latched at import would need a restart to
    honour it — which is exactly what a rollback cannot afford."""
    return bool(_URL) or _TRANSPORT is not None


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


def _client(timeout: float = _TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=_URL or "http://lifeworld.invalid",
                             timeout=timeout, transport=_TRANSPORT,
                             headers={"X-Service-Token": _token()})


def _sync_client(timeout: float = _TIMEOUT) -> httpx.Client:
    """The SYNC half. `repair.ensure_team`, `note_decision`, `team_usage` and the
    module graph's assignment pool are plain functions on paths that have never
    been awaitable; making them async would ripple through twenty call sites for
    no gain a localhost round trip can measure. Tests swap this factory for a
    TestClient on the mounted service — the same seam usage.py and knowledge.py
    use."""
    return httpx.Client(base_url=_URL or "http://lifeworld.invalid", timeout=timeout,
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
        async with _client(_SLOW_TIMEOUT) as c:
            r = await c.request(request.method, target, params=request.query_params,
                                content=body or None, headers=headers)
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
        async with _client() as c:
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

async def _get(path: str, *, headers: dict, params: dict | None = None,
               timeout: float = _TIMEOUT):
    async with _client(timeout) as c:
        r = await c.get(path, params=params or {}, headers=headers)
        r.raise_for_status()
        return r.json()


async def _post(path: str, payload: dict, *, headers: dict,
                params: dict | None = None, timeout: float = _TIMEOUT):
    async with _client(timeout) as c:
        r = await c.post(path, json=payload, params=params or {}, headers=headers)
        r.raise_for_status()
        return r.json()


def health() -> bool:
    """Is the substrate actually answering? Its own /health — the same endpoint
    process-compose probes — asked through this door rather than around it, so
    the module graph's heartbeat and every verb agree on what "up" means."""
    if not enabled():
        return True                    # in-process: it is up iff this process is
    try:
        with _sync_client(2.0) as c:
            r = c.get("/health")
            return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False


# --- the crew's verbs --------------------------------------------------------
#
# Each one is a WHOLE BEHAVIOUR, because each is a read-modify-write on a world
# blob and `store.lock_for` can only be held on the service's side of the wire.
# The `_local_*` twin under each is the P4-A fallback and dies with commit B.
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
    if not enabled():
        return _local_seat_crew(world_id, factors, manager=manager, protocol=protocol,
                                scene_name=scene_name, current_room_id=current_room_id,
                                world_name=world_name)
    try:
        with _sync_client(30.0) as c:
            r = c.post(f"/worlds/{int(world_id)}/crew-seating", json={
                "factors": factors, "manager": manager, "protocol": protocol,
                "scene_name": scene_name, "world_name": world_name,
                "current_room_id": int(current_room_id or 0)}, headers=root_stamp())
            r.raise_for_status()
            return r.json()
    except Exception as e:
        _degraded("seat_crew", e)
        return None


def crew_decision(world_id: int, human_id: int, saw: str, understood: str,
                  chose: str, because: dict) -> dict | None:
    if not enabled():
        return _local_crew_decision(world_id, human_id, saw, understood, chose, because)
    try:
        with _sync_client() as c:
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
    if not enabled():
        return _local_crew_decision_node(world_id, human_id, decision_id)
    try:
        with _sync_client() as c:
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
    if not enabled():
        return _local_crew_usage(world_id, room_id)
    try:
        with _sync_client() as c:
            r = c.get(f"/worlds/{int(world_id)}/crew-usage",
                      params={"room_id": int(room_id)}, headers=root_stamp())
            r.raise_for_status()
            return list(r.json().get("agents") or [])
    except Exception as e:
        _degraded("crew_usage", e)
        return None


def crew_chat_note(world_id: int, room_id: int, thread_id: int, text: str,
                   role: str = "manager") -> bool:
    if not enabled():
        return _local_crew_chat_note(world_id, room_id, thread_id, text, role)
    try:
        with _sync_client() as c:
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
    if not enabled():
        return _local_room_alive(world_id, room_id)
    try:
        with _sync_client() as c:
            return c.get(f"/worlds/{int(world_id)}/room/{int(room_id)}",
                         headers=root_stamp()).status_code == 200
    except Exception:
        return False


def room_view(world_id: int, room_id: int, user: dict | None = None) -> dict | None:
    """One room exactly as the canvas sees it — cast, props, threads and the last of
    the room log. The same answer `/api/lw/{wid}/room/{rid}` proxies to a browser,
    asked from inside the conductor for the crew's own room."""
    if not enabled():
        from .lifeworld import store
        w = store.load(int(world_id))
        s = w.scene(int(room_id)) if w else None
        return s.view() if s is not None else None
    from . import repair
    try:
        with _sync_client() as c:
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
    if not enabled():
        return _local_room_members(world_id, room_id)
    try:
        with _sync_client() as c:
            r = c.get(f"/worlds/{int(world_id)}/room/{int(room_id)}/members",
                      headers=stamp(user))
            if r.status_code != 200:
                return None
            return r.json()
    except Exception as e:
        _degraded("room_members", e)
        return None


def rooms(user: dict, extra_world_ids: list[int]) -> list[dict] | None:
    if not enabled():
        return _local_rooms(user, extra_world_ids)
    try:
        with _sync_client() as c:
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
    if not enabled():
        return _local_crew_context(world_id, room_id, thread_id, human_id, cue)
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
    if not enabled():
        return _local_crew_outcome(world_id, human_id, decision_id, ok, says)
    try:
        return await _post(f"/worlds/{int(world_id)}/crew-outcome",
                           {"human_id": int(human_id), "decision_id": int(decision_id),
                            "ok": bool(ok), "says": says}, headers=root_stamp())
    except Exception as e:
        _degraded("crew_outcome", e)
        return None


async def crew_consult(world_id: int, room_id: int, thread_id: int, human_id: int,
                       question: str, who: str = "", live: bool = True) -> dict | None:
    if not enabled():
        return await _local_crew_consult(world_id, room_id, thread_id, human_id,
                                         question, who, live)
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
    if not enabled():
        return await _local_crew_review(world_id, room_id, thread_id, human_id,
                                        question, cue)
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
    if not enabled():
        return await _local_crew_deliberate(world_id, room_id, thread_id, topic=topic,
                                            rulebook=rulebook, rounds=rounds, live=live)
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
    if not enabled():
        return await _local_crew_chat(world_id, room_id, thread_id, text, live)
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
    if not enabled():
        from .lifeworld import store
        return store.create(user["id"], name).id
    out = await _post("/worlds", {"name": name}, headers=stamp(user))
    return int((out.get("world") or {}).get("id") or 0)


async def apply_manifest(user: dict, world_id: int, body: dict) -> dict:
    """A whole team as one spec, applied to a world. Used by the project wizard's
    "build me a team" path; the Studio's own route goes through the proxy."""
    if not enabled():
        return _local_apply_manifest(user, world_id, body)
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


# =============================================================================
# THE P4-A FALLBACK: the in-process package, unchanged, behind the same names.
# Every function below is deleted by commit B along with conductor/app/lifeworld/.
# =============================================================================

def _local_settings(live: bool) -> dict | None:
    from . import repair
    return repair._root_settings() if live else None


def _local_seat_crew(world_id: int, factors: list[dict], *, manager: dict,
                     protocol: dict, scene_name: str, current_room_id: int,
                     world_name: str) -> dict | None:
    from . import auth
    from .lifeworld import store
    from .lifeworld_routes import ManifestAgent, ManifestBody, materialise_manifest
    u = auth.get_user_by_name(auth.ROOT_USERNAME)
    if not u or not factors:
        return None
    w = store.load(world_id) if world_id else None
    if w is None:
        w = store.create(u["id"], world_name)
    names = [f["name"] for f in factors]
    adopted = _local_adopt(w, factors, names)
    if adopted:
        return adopted
    keep_agents, keep_thread = {}, {}
    old = w.scene(current_room_id) if current_room_id else None
    if old is not None:
        for h in old.players():
            keep_agents[h.name] = {"memory": h.memory.to_dict(),
                                   "decisions": h.decisions.to_dict(),
                                   "skills": h.skills.to_dict(), "tau": h.tau}
        t0 = old.threads[0] if old.threads else None
        if t0:
            keep_thread = {"chats": t0.get("chats") or {}, "results": t0.get("results") or []}
    s = materialise_manifest(w, ManifestBody(
        name=scene_name,
        agents=[ManifestAgent(name=f["name"], brief=f.get("brief", ""),
                              dials=f.get("dials") or {}, drives=f.get("drives") or {})
                for f in factors],
        edges=[[names[i], names[(i + 1) % len(names)]] for i in range(len(names))]
              if len(names) > 1 else [],
        rules="", manager=manager, protocol=protocol))
    from .lifeworld.decisions import DecisionLog
    from .lifeworld.memory import Memory
    from .lifeworld.skills import Skills
    for h in s.players():
        prior = keep_agents.get(h.name)
        if not prior:
            continue
        h.memory = Memory.from_dict(prior.get("memory"))
        h.decisions = DecisionLog.from_dict(prior.get("decisions"))
        h.skills = Skills.from_dict(prior.get("skills"))
        h.tau = int(prior.get("tau") or 0)
    if keep_thread and s.threads:
        s.threads[0]["chats"] = keep_thread.get("chats") or {}
        s.threads[0]["results"] = keep_thread.get("results") or []
    dropped = _local_tidy(w, s.id)
    store.save(w)
    return {"world_id": w.id, "room_id": s.id,
            "thread_id": s.threads[0]["id"] if s.threads else 0,
            "agents": {f["id"]: hid for f, hid in zip(factors, s.seats)},
            "outcome": "rebuilt", "dropped_rooms": dropped}


def _local_adopt(w, factors: list[dict], names: list[str]) -> dict | None:
    want = set(names)
    for s in w.scenes.values():
        players = list(s.players())
        if {h.name for h in players} != want or not s.threads:
            continue
        by_name = {h.name: h.id for h in players}
        return {"world_id": w.id, "room_id": s.id,
                "thread_id": s.threads[0]["id"] if s.threads else 0,
                "agents": {f["id"]: by_name[f["name"]] for f in factors},
                "outcome": "adopted", "dropped_rooms": 0}
    return None


def _local_tidy(w, keep_room: int) -> int:
    from .lifeworld.human import Human
    dropped = 0
    for sid in [i for i in list(w.scenes) if i != keep_room]:
        w.scenes.pop(sid, None)
        dropped += 1
    seated = set(w.scene(keep_room).seats) if w.scene(keep_room) else set()
    for eid, ent in list(w.entities.items()):
        if isinstance(ent, Human) and eid not in seated:
            w.entities.pop(eid, None)
    return dropped


def _local_table(w, room_id: int, thread_id: int):
    s = w.scene(room_id) if w else None
    t = s.thread(thread_id) if s else None
    return s, t


def _local_neighbours(s, t, me) -> list:
    from .lifeworld.threads import members_of
    if s is None or t is None or me is None:
        return []
    return [o for o in (s.world.get(i) for i in members_of(t))
            if o is not None and o.id != me.id and s._hears(t, o.id, me.id)]


async def _local_best_informed(peers, question: str, world_id: int):
    from . import knowledge, repair
    best, score = None, -1.0
    for p_ in peers:
        try:
            hits = await knowledge.recall(repair.reg_key_for(world_id, p_.id), question,
                                          k=1, settings=repair._root_settings())
            s_ = hits[0]["score"] if hits else 0.0
        except Exception:
            s_ = 0.0
        if s_ > score:
            best, score = p_, s_
    return best


def _local_crew_context(world_id: int, room_id: int, thread_id: int, human_id: int,
                        cue: str) -> dict | None:
    from .lifeworld import store
    from .lifeworld.decisions import signature
    from .lifeworld.human import Human
    from .lifeworld.world import _persona
    w = store.load(world_id)
    s, t = _local_table(w, room_id, thread_id)
    h = w.get(human_id) if w else None
    if not isinstance(h, Human):
        return None
    p = _persona(h)
    exact = h.decisions.recall(signature(cue, kind="task")) if cue else None
    return {"name": h.name, "traits": p.get("traits") or {}, "wants": p.get("wants") or "",
            "exact": ({"says": exact.says[:200], "evidence": exact.evidence,
                       "confidence": exact.confidence} if exact is not None else None),
            "neighbours": [o.name for o in _local_neighbours(s, t, h)]}


def _local_crew_decision(world_id: int, human_id: int, saw: str, understood: str,
                         chose: str, because: dict) -> dict | None:
    from .lifeworld import store
    from .lifeworld.decisions import signature
    w = store.load(world_id)
    h = w.get(human_id) if w else None
    if h is None:
        return None
    d = h.decisions.record(tau=int(getattr(h, "tau", 0)), sig=signature(saw, kind="task"),
                           saw=saw, understood=understood, chose=chose, because=because)
    store.save(w)
    return {"decision_id": d.id, "sig": d.sig}


def _local_crew_outcome(world_id: int, human_id: int, decision_id: int, ok: bool,
                        says: str) -> dict | None:
    from .lifeworld import store
    w = store.load(world_id)
    h = w.get(human_id) if w else None
    if h is None:
        return None
    h.decisions.resolve(int(decision_id), "good" if ok else "bad", says=says[:200])
    store.save(w)
    node = h.decisions.get(int(decision_id))
    return {"ok": True, "name": h.name, "sig": node.sig if node else "",
            "saw": node.saw if node else ""}


def _local_crew_decision_node(world_id: int, human_id: int, decision_id: int) -> dict | None:
    from .lifeworld import store
    w = store.load(world_id)
    h = w.get(human_id) if w else None
    node = h.decisions.get(int(decision_id)) if h is not None else None
    return node.to_dict() if node is not None else None


async def _local_crew_consult(world_id: int, room_id: int, thread_id: int, human_id: int,
                              question: str, who: str, live: bool) -> dict | None:
    from .lifeworld import store
    from .lifeworld.decisions import signature
    from .lifeworld.threads import members_of
    from . import knowledge, repair
    async with store.lock_for(world_id):
        w = store.load(world_id, live=live, settings=_local_settings(live))
        s, t = _local_table(w, room_id, thread_id)
        me = w.get(human_id) if w else None
        if not (t is not None and me is not None):
            return {"ok": False, "reason": "no_table"}
        peers = _local_neighbours(s, t, me)
        if not peers:
            return {"ok": False, "reason": "no_peers"}
        nb = next((x for x in peers if x.name.lower() == who.strip().lower()), None) if who else None
        if who and nb is None:
            return {"ok": False, "reason": "not_a_neighbour", "peers": [x.name for x in peers]}
        if nb is None:
            nb = await _local_best_informed(peers, question, world_id) or peers[0]
        s._record("say", me.id, f"{me.name}: (consult) {question[:180]}", frm=me.id)
        await s._hear(me, nb, f"(consult) {question[:200]}")
        known = nb.decisions.recall(signature(question))
        recalled = None
        if known is not None:
            recalled = {"says": known.says, "confidence": known.confidence,
                        "seen": known.evidence}
        else:
            hits = await knowledge.recall(repair.reg_key_for(world_id, nb.id), question,
                                          k=1, settings=repair._root_settings())
            if hits and hits[0]["score"] >= 0.25:
                recalled = {"says": hits[0]["says"], "confidence": hits[0]["confidence"],
                            "seen": hits[0]["evidence"]}
        ring = [h for h in (w.get(i) for i in members_of(t)) if h is not None]
        answer = await w.agent_reply(
            nb, question, transcript=s._thread_transcript(ring, thread=t, for_agent=nb.id),
            recalled=recalled)
        if not answer:
            return {"ok": False, "reason": "unreachable", "who": nb.name}
        s._record("say", nb.id, f"{nb.name}: {answer[:200]}", frm=nb.id)
        await s._hear(nb, me, answer)
        store.save(w)
        return {"ok": True, "who": nb.name, "answer": answer, "model": w.model_for(nb)}


async def _local_crew_review(world_id: int, room_id: int, thread_id: int, human_id: int,
                             question: str, cue: str) -> dict | None:
    from .lifeworld import store
    async with store.lock_for(world_id):
        w = store.load(world_id, live=True, settings=_local_settings(True))
        s, t = _local_table(w, room_id, thread_id)
        me = w.get(human_id) if w else None
        if not (t is not None and me is not None):
            return {"ok": False, "reason": "no_table"}
        peers = _local_neighbours(s, t, me)
        if not peers:
            return {"ok": False, "reason": "no_peers"}
        reviewer = await _local_best_informed(peers, cue or question, world_id) or peers[0]
        answer = await w.agent_reply(reviewer, question)
        store.save(w)
        return {"ok": bool(answer), "who": reviewer.name, "answer": answer or "",
                "reason": "" if answer else "unreachable"}


async def _local_crew_deliberate(world_id: int, room_id: int, thread_id: int, *,
                                 topic: str, rulebook: str, rounds: int,
                                 live: bool) -> dict | None:
    from .lifeworld import store
    from .lifeworld.threads import members_of, protocol_of
    async with store.lock_for(world_id):
        w = store.load(world_id, live=live, settings=_local_settings(live))
        s, t = _local_table(w, room_id, thread_id)
        if t is None:
            return {"ok": False, "reason": "no_table", "memo": None}
        if topic:
            t["topic"] = topic
        if rulebook:
            t["rulebook"] = rulebook[:2000]
        memo = await s.run_deliberation(t, rounds=max(1, int(rounds)))
        store.save(w)
        return {"ok": True, "memo": memo,
                "independent": protocol_of(t).get("init") == "independent",
                "ring": len(members_of(t))}


def _local_crew_chat_note(world_id: int, room_id: int, thread_id: int, text: str,
                          role: str) -> bool:
    import time
    from .lifeworld import store
    w = store.load(world_id)
    _s, t = _local_table(w, room_id, thread_id)
    if t is None:
        return False
    convo = t.setdefault("chats", {}).setdefault(role, [])
    convo.append({"role": role, "text": text, "ts": time.time()})
    t["chats"][role] = convo[-40:]
    store.save(w)
    return True


async def _local_crew_chat(world_id: int, room_id: int, thread_id: int, text: str,
                           live: bool) -> dict:
    from .lifeworld import store
    async with store.lock_for(world_id):
        w = store.load(world_id, live=live, settings=_local_settings(live))
        s = w.scene(room_id)
        t = s.thread(thread_id) if s else None
        if t is None:
            raise HTTPException(409, "the crew's table is missing — toggle repair off and on")
        res = await s.chat(t, "manager", text)
        store.save(w)
    return res


def _local_crew_usage(world_id: int, room_id: int) -> list[dict] | None:
    from .lifeworld import store
    w = store.load(world_id)
    if not w:
        return None
    s = w.scene(room_id) if room_id else None
    people = s.players() if s is not None else w.humans()
    return [{"agent_id": h.id, "name": h.name, "usage": h.usage()} for h in people]


def _local_room_alive(world_id: int, room_id: int) -> bool:
    try:
        from .lifeworld import store
        w = store.load(world_id)
        return bool(w and w.scene(room_id) is not None)
    except Exception:
        return False


def _local_room_members(world_id: int, room_id: int) -> dict | None:
    from .lifeworld import store
    try:
        w = store.load(int(world_id))
        s = w.scene(int(room_id)) if w else None
    except Exception:
        return None
    if s is None:
        return None
    return {"world_id": int(world_id), "room_id": int(room_id),
            "name": f"{w.name} · {s.name}" if s.name else w.name,
            "members": [{"agent_id": h.id, "name": h.name} for h in s.players()]}


def _local_rooms(user: dict, extra_world_ids: list[int]) -> list[dict]:
    from .lifeworld import store
    wids = [w["id"] for w in store.listing(user["id"])]
    for i in extra_world_ids:
        if int(i) not in wids:
            wids.append(int(i))
    out = []
    for wid in wids:
        try:
            w = store.load(wid)
        except Exception:
            continue
        if not w:
            continue
        for s in w.scenes.values():
            players = s.players()
            if not players:
                continue
            out.append({"world_id": w.id, "room_id": s.id,
                        "name": f"{w.name} · {s.name}" if s.name else w.name,
                        "agents": len(players)})
    return out


def _local_apply_manifest(user: dict, world_id: int, body: dict) -> dict:
    from .lifeworld import store
    from .lifeworld_routes import ManifestBody, materialise_manifest
    w = store.load(world_id)
    s = materialise_manifest(w, ManifestBody(**body))
    store.save(w)
    return {"room": s.view(), "agents": {(w.get(hid).name): hid for hid in s.seats},
            "thread_ids": [t["id"] for t in s.threads], "result": None}
