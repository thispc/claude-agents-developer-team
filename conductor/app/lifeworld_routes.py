"""The `/api/lw/*` doorway — an authenticated thin proxy onto the lifeworld service.

A world is a container of registered AGENTS and ARTIFACTS and a set of ROOMS; a room is a
scene with a relatable type (home, office, casino, …) that sets its look and rules; agents
and artifacts are created once and placed into rooms. All of that — the substrate, the 35
handler bodies that used to live in this file, and the `lw_worlds` blob — is
`services/lifeworld` since P4. **The paths did not move**, because the dashboard hardcodes
them in fifty-odd places across `dashboard/js` and rewriting the Studio to chase a port
would be a change nobody asked for and everybody would feel.

So what is left here is a doorway, and its job is exactly three things:

  AUTHENTICATE   `current_user` resolves the session cookie, before one byte forwards.
                 The service never sees a cookie — the client strips it — and the
                 browser never sees a service token. Same origin, both directions.

  FORWARD        `/api/lw/<rest>` → the service's `/worlds/<rest>`, verbatim: method,
                 query, body, status and content-type. One catch-all rather than 35
                 declarations, because a proxy that enumerates its routes is a proxy
                 that silently 404s the next one somebody adds.

  COMPOSE, twice, where an answer genuinely belongs to two owners:
                 the world LIST hides the crew's own world — which world that is lives
                 in the repair engine's kv record, and the substrate has no business
                 knowing one of its rows is special; and the agent DETAIL panel adds
                 root's log rows, which belong to the WATCH service and are root-only.
                 Having the lifeworld fetch either would make it a second, undeclared
                 caller of somebody else's data with none of their gates.

AUTHORISATION is NOT here, and that is deliberate: the `owner_id` column moved into the
service, so the service checks it. The conductor says who the caller is; the row says
whether it is theirs. A check made against a copy of a table you no longer own is a check
against a stale copy — and after the cutover there is no copy at all.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from . import lifeworld_client
from .guards import current_user

router = APIRouter(prefix="/api/lw", tags=["lifeworld"])
# The two routes the conductor ANSWERS. On their own router so main.py can include
# them FIRST — the catch-all below would otherwise match them and forward blind.
compose_router = APIRouter(prefix="/api/lw", tags=["lifeworld"])

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def _devteam_world() -> int:
    """The self-repair crew's world, which the Studio deliberately does not list.

    It is not a team you assembled; it is the one that works on this platform, and it has
    its own door on the landing page with its own console attached. Mixing it into the same
    list as the teams you built invites someone to reorganise the crew that is mid-sprint.
    """
    try:
        from . import repair
        return int((repair.team() or {}).get("world_id") or 0)
    except Exception:
        return 0


@compose_router.get("")
async def worlds(request: Request, include_devteam: int = 0) -> dict:
    """The Studio's world list, minus the crew's own world."""
    u = current_user(request)
    rows = (await lifeworld_client.studio_get(u, "")).get("worlds") or []
    dev = _devteam_world()
    if not include_devteam and dev:
        rows = [w for w in rows if int(w.get("id")) != dev]
    return {"worlds": rows, "devteam_world": dev}


@compose_router.get("/{world_id}/human/{human_id}")
async def peek(world_id: int, human_id: int, request: Request) -> dict:
    """One agent's detail panel, plus — for root — the backend's own log rows about it.

    Root only, because logs name file paths, branch names and the shape of the operator's
    own work. The service answers what the world knows and hands back the agent's name
    purely as the key for this lookup; it is popped either way, so a non-root caller
    cannot learn it from a field that only exists to be consumed here.
    """
    u = current_user(request)
    out = await lifeworld_client.studio_get(u, f"/{int(world_id)}/human/{int(human_id)}")
    name = out.pop("name", "")
    if u.get("is_root"):
        from . import logs
        out["logs"] = logs.recent(q=name, limit=40) if name else []
    return out


@router.api_route("", methods=_METHODS)
async def lw_root(request: Request) -> Response:
    return await lifeworld_client.proxy(request, "")


@router.api_route("/{path:path}", methods=_METHODS)
async def lw_any(path: str, request: Request) -> Response:
    return await lifeworld_client.proxy(request, path)
