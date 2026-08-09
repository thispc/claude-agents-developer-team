"""Reverse-proxy for local-mode preview apps, so a hosted instance has an openable link.

A preview runs as a subprocess inside the conductor pod on localhost:PORT — reachable
only from the pod. On a hosted instance that is a dead link in the browser. This makes
it live: a request arriving on a preview host (`p<project>.<PREVIEW_HOST>`, resolved to
the ingress by nip.io, routed to the conductor) is forwarded to that project's local
app port. Because the browser sits on the preview host, EVERY request — including the
app's own absolute `/api/...` calls — comes back here and is proxied, which a subpath
proxy could never do.

Deliberately narrow: it only acts when the Host matches the preview pattern, so a normal
dashboard request passes straight through untouched. Off entirely unless PREVIEW_HOST is
set, so nothing changes for a dev/local instance.

Not handled (yet): WebSocket upgrades — fine for viewing a built app, a gap for a live
HMR dev server. Previews serve over http on their own host; opened in a new tab, that is
not mixed content.
"""

import re
from typing import Any

import httpx
from starlette.responses import Response

from . import config, deploy

# Hop-by-hop headers must not be forwarded across a proxy (RFC 7230 §6.1), plus the
# ones httpx/Starlette will recompute for the new connection.
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade", "content-length", "host"}

_HOST_RE = re.compile(r"^p(\d+)(?:-[a-z0-9-]+)?$")   # p<id> or p<id>-<branch-slug>


def project_for_host(host_header: str) -> int | None:
    """The project id a preview Host addresses, or None for any ordinary request.
    None whenever PREVIEW_HOST is unset, so this whole path is inert by default."""
    if not config.PREVIEW_HOST:
        return None
    host = (host_header or "").split(":")[0].lower()
    suffix = "." + config.PREVIEW_HOST.lower()
    if not host.endswith(suffix):
        return None
    m = _HOST_RE.match(host[: -len(suffix)])
    return int(m.group(1)) if m else None


async def proxy(request, project_id: int) -> Response:
    port = deploy.running_port(project_id)
    if not port:
        return Response(
            f"No preview is running for project {project_id}.\n\n"
            "Open the project's Deploy panel and press Build & deploy first.",
            status_code=503, media_type="text/plain")

    target = f"http://127.0.0.1:{port}{request.url.path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            upstream = await client.request(
                request.method, target, params=request.query_params,
                content=body, headers=headers)
    except Exception as e:                      # app crashed, still booting, wrong port
        return Response(
            f"The preview app for project {project_id} did not respond ({str(e)[:120]}).\n"
            "It may still be starting, or it may have stopped — try rebuilding it.",
            status_code=502, media_type="text/plain")

    out_headers: dict[str, Any] = {k: v for k, v in upstream.headers.items()
                                   if k.lower() not in _HOP}
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=out_headers)
