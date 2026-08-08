"""The /svc gateway — every fleet service's API and UI, same-origin behind the conductor.

Path-based where preview_proxy is host-based, same httpx plumbing: a request for
`/svc/<name>/<path>` forwards to the managed service registered under `<name>`,
resolved from data/fleet_topology.json (tools/gen_fleet.py's output) with
services.yaml as the fallback when the generator has not run yet.

Auth is two layers, both conductor-side: the CALLER must be a signed-in user
(session cookie, checked before anything is forwarded), and the conductor then adds
the service's own X-Service-Token server-side — so service tokens never reach a
browser and the dashboard never changes origin. The session cookie is deliberately
STRIPPED from the forwarded request: a service authenticates callers by token, and
handing it user cookies would tempt exactly the coupling the contract forbids.

Gate: only managed services (kind core/service) resolve. Unknown names, ephemeral
and external entries (worker-pool, sandbox, apps) 404 — those have their own doors.
The conductor itself is excluded too: it IS this origin, and proxying to yourself
is a loop wearing a trenchcoat.

Not handled (yet): WebSocket upgrades — the same honest gap preview_proxy has. The
first push-feed service (watch, P3) forces that decision; nothing needs it in P0.
"""

import json
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from .. import config
from ..preview_proxy import _HOP          # one hop-by-hop list, not two drifting copies
from .base import current_user, router

_DATA = Path(config.ROOT) / "data"
MANAGED_KINDS = ("core", "service")


def _services() -> dict:
    """The registry as the fleet sees it: fleet_topology.json first (what the
    generator actually wrote), services.yaml as the legacy-boot fallback.

    Read by the gateway below AND by the fleet doors in routes/internal.py, which
    need the same entries' `doors`/`knobs` allowlists — one reader, so a
    generated topology and a hand-edited registry can never grant different
    permissions.
    """
    topo = _DATA / "fleet_topology.json"
    if topo.exists():
        try:
            return json.loads(topo.read_text()).get("services", {})
        except Exception:
            pass
    try:
        import yaml
        reg = yaml.safe_load((config.ROOT / "services.yaml").read_text()) or {}
        host = (reg.get("defaults") or {}).get("host", "127.0.0.1")
        out = {}
        for n, s in (reg.get("services") or {}).items():
            managed = s.get("kind") in MANAGED_KINDS and s.get("port")
            out[n] = {"kind": s.get("kind"), "managed": bool(managed)}
            if managed:
                out[n]["url"] = f"http://{host}:{s['port']}"
                out[n]["doors"] = sorted(s.get("doors") or [])
                out[n]["knobs"] = sorted(s.get("knobs") or [])
        return out
    except Exception:
        return {}


def resolve(name: str) -> dict | None:
    """The target a /svc name addresses: {url, token} — or None, which the route
    turns into a 404. None for the conductor itself and for anything unmanaged."""
    if name == "conductor":
        return None
    svc = _services().get(name)
    if not svc or not svc.get("managed") or not svc.get("url"):
        return None
    token = ""
    tok = _DATA / "tokens" / f"{name}.token"
    if tok.exists():
        token = tok.read_text().strip()
    return {"url": svc["url"].rstrip("/"), "token": token}


@router.api_route("/svc/{name}/{path:path}",
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def svc_proxy(name: str, path: str, request: Request):
    current_user(request)                   # the caller's auth, before ONE byte forwards
    target = resolve(name)
    if not target:
        raise HTTPException(404, f"no such service: {name}")

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
