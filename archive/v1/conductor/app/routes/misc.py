"""The small standalone surfaces: tuning knobs, the dashboard's own error
reports, the notification bell, and /api/health.

None of these belongs to a project or to the self-repair area; each is a
one-or-three-endpoint family that would only dilute whichever module it was
bolted onto. Health stays unauthenticated on purpose — a monitor that needs a
session cannot tell you the login path is broken.
"""

from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .. import auth, config, db, metrics, notify, tuning
from .base import _root, can_see, current_user, router


@router.get("/api/tuning")
def get_tuning(request: Request) -> dict:
    """The orchestration knobs, with where each current value came from."""
    current_user(request)
    return {"knobs": tuning.describe()}


class TuningUpdate(BaseModel):
    name: str
    value: Any = None
    reset: bool = False


@router.post("/api/tuning")
def set_tuning(body: TuningUpdate, request: Request) -> dict:
    """Change a knob on the running instance.

    Root only. These values decide how much the platform spends and how hard it
    retries, so they sit with the operator rather than with anyone who can log in.
    """
    u = current_user(request)
    if not u["is_root"]:
        raise HTTPException(403, "only root may change orchestration settings")
    try:
        if body.reset:
            tuning.reset(body.name)
        else:
            tuning.set(body.name, body.value, who=u["username"])
    except KeyError:
        raise HTTPException(404, f"no knob called '{body.name}'")
    except (TypeError, ValueError):
        raise HTTPException(400, f"'{body.value}' is not a valid value for {body.name}")
    return {"knobs": tuning.describe()}


@router.get("/api/tuning/compare")
def compare_tuning(request: Request) -> dict:
    """Tuning profiles side by side — the reason runs are recorded at all."""
    current_user(request)
    return metrics.compare()


class ClientError(BaseModel):
    message: str
    stack: str = ""
    url: str = ""


@router.post("/api/client-error")
async def client_error(body: ClientError, request: Request) -> dict:
    """A JavaScript error in the dashboard, reported by the page itself.

    Console errors were invisible to everyone but whoever had DevTools open —
    which, on an unattended platform, is nobody. A broken button stayed broken
    until a human happened to click it.
    """
    u = auth.user_for_token(request.cookies.get("devteam_session"))
    from .. import logs
    logs.error("http", "dashboard_error", body.message[:300],
               page=body.url[:120], user=(u or {}).get("username", "anonymous"))
    return await notify.report_error(
        "dashboard error", f"{body.message}\n{body.stack[:1200]}",
        {"page": body.url[:200], "user": (u or {}).get("username", "anonymous")})


@router.get("/api/notify/status")
def notify_status(request: Request) -> dict:
    """So "no notifications" is provably "nothing broke", not "it is broken"."""
    _root(request)
    return notify.status()


@router.get("/api/notifications")
def notifications(request: Request) -> dict:
    """Everything waiting on the boss, across THEIR projects — for the bell menu."""
    u = current_user(request)
    items = []
    for p in db.list_projects():
        if not can_see(p, u) or p["status"] in ("cancelled",):
            continue
        q = db.pending_question(p["id"])
        if q:
            items.append({"project_id": p["id"], "project": p["name"],
                          "question_id": q["id"], "question": q["text"],
                          "options": db.json.loads(q["options"])})
    return {"count": len(items), "items": items}


@router.get("/api/health")
def health() -> dict:
    # The dashboard is static and served straight from disk, but the API is
    # whichever process is running. Edit both and the page silently runs ahead of
    # the server — which looks exactly like a broken feature (an empty dropdown,
    # a button that does nothing) rather than a conductor that needs restarting.
    stale_ui = False
    try:
        from ..main import STARTED_AT
        parts = list((config.DASHBOARD_DIR / "js").glob("*.js")) + list(config.DASHBOARD_DIR.glob("canvas2/*.js"))
        stale_ui = any(p.stat().st_mtime > STARTED_AT for p in parts)
    except Exception:
        pass
    # And whether this process can actually do its job, because an external
    # watchdog reads only the status code. Being alive is the least interesting
    # thing that can be true about a pod: one with a read-only volume answers
    # every static request perfectly while being unable to work, and that exact
    # failure has happened here. Unauthenticated for the same reason — a monitor
    # that needs a session cannot tell you the login path is broken.
    try:
        db._rows("SELECT id FROM projects LIMIT 1", ())
    except Exception as e:
        raise HTTPException(503, f"database unavailable: {str(e)[:200]}")
    return {"ok": True, "launcher": config.LAUNCHER, "auth": config.auth_mode(),
            "github": bool(config.GITHUB_TOKEN) or config.DEMO_MODE,
            "demo": config.DEMO_MODE, "stale_ui": stale_ui,
            "weak_password": auth.password_is_weak(auth.ROOT_PASSWORD),
            # So the dashboard can build git-host links (gitWebUrl in core.js)
            # from the same config github_client.py uses, instead of a
            # hardcoded github.com that 404s against a self-hosted host.
            "git_web": config.GIT_WEB, "git_provider": config.GIT_PROVIDER}
