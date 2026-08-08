"""The /svc gateway — every fleet service's API and UI, same-origin behind the conductor.

Path-based where preview_proxy is host-based, same httpx plumbing: a request for
`/svc/<name>/<path>` forwards to the managed service registered under `<name>`,
resolved from data/fleet_topology.json (tools/gen_fleet.py's output) with
services.yaml as the fallback when the generator has not run yet.

Auth is two layers, both conductor-side: the CALLER must be authorised (checked
before anything is forwarded), and the conductor then adds the service's own
X-Service-Token server-side — so service tokens never reach a browser and the
dashboard never changes origin. The session cookie is deliberately STRIPPED from
the forwarded request: a service authenticates callers by token, and handing it
user cookies would tempt exactly the coupling the contract forbids.

WHO the caller must be defaults to ROOT, and a service opts out by declaring
`public: true` in services.yaml. Fail closed, for the same reason the door
allowlist does: being signed in is not a permission. Every service in the fleet
today holds operator data — what agents have learned, what the subscription was
spent on, what broke — and the conductor's own routes for that data are all
root-gated. P3 made the point concrete: `/api/logs` is root-only because logs name
file paths, branch names, model errors and the shape of the operator's own work,
and a gateway that forwarded `/svc/watch/logs` to any signed-in user would have
handed the same rows to a normal account through a different door. A project's own
service that genuinely serves its users says so in one line, reviewed in the
registry, instead of every service leaking by omission.

Gate: only managed services (kind core/service) resolve. Unknown names, ephemeral
and external entries (worker-pool, sandbox, apps) 404 — those have their own doors.
The conductor itself is excluded too: it IS this origin, and proxying to yourself
is a loop wearing a trenchcoat.

Not handled (yet): WebSocket upgrades — the same honest gap preview_proxy has. The
first push-feed service (watch, P3) forces that decision; nothing needs it in P0.
"""

from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from .. import config, fleet
from ..guards import _root
from ..preview_proxy import _HOP          # one hop-by-hop list, not two drifting copies
from .base import current_user, router

_DATA = Path(config.ROOT) / "data"
MANAGED_KINDS = fleet.MANAGED_KINDS


def _services() -> dict:
    """The registry as the fleet sees it: fleet_topology.json first (what the
    generator actually wrote), services.yaml as the legacy-boot fallback.

    ONE READER, and since P6 it lives in `fleet.py` — this gateway, the fleet
    doors in routes/internal.py and the Atlas's own cards all resolve the same
    entries, so a generated topology and a hand-edited registry can never grant
    different permissions or draw a fleet nobody is running.
    """
    return fleet.services()


def resolve(name: str) -> dict | None:
    """The target a /svc name addresses: {url, token, public} — or None, which the
    route turns into a 404. None for the conductor itself and for anything
    unmanaged. `public` is the registry's opt-out from the root gate, and it
    defaults to False for a service that does not mention it."""
    if name == "conductor":
        return None
    svc = _services().get(name)
    if not svc or not svc.get("managed") or not svc.get("url"):
        return None
    token = ""
    tok = _DATA / "tokens" / f"{name}.token"
    if tok.exists():
        token = tok.read_text().strip()
    return {"url": svc["url"].rstrip("/"), "token": token,
            "public": bool(svc.get("public"))}


@router.api_route("/svc/{name}/{path:path}",
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def svc_proxy(name: str, path: str, request: Request):
    current_user(request)                   # the caller's auth, before ONE byte forwards
    target = resolve(name)
    if not target:
        raise HTTPException(404, f"no such service: {name}")
    if not target["public"]:
        # Resolved first so an unknown name is still a 404 rather than a 403 that
        # confirms it exists; then the root gate, still before a byte forwards.
        _root(request)

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP and k.lower() != "cookie"}
    headers["X-Service-Token"] = target["token"]
    client = httpx.AsyncClient(timeout=30, follow_redirects=False)
    req = client.build_request(
        request.method, f"{target['url']}/{path}", params=request.query_params,
        content=request.stream(),           # streamed through, never buffered whole
        headers=headers)
    try:
        upstream = await client.send(req, stream=True)
    except Exception as e:                  # down, still booting, mid-restart
        await client.aclose()
        return Response(
            f"The {name} service did not respond ({str(e)[:120]}).\n"
            "It may be starting or stopped — check the fleet (data/logs/fleet.log).",
            status_code=502, media_type="text/plain")

    async def _close() -> None:
        await upstream.aclose()
        await client.aclose()

    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP}
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                             headers=out_headers, background=BackgroundTask(_close))
