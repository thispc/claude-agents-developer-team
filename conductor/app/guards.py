"""Who may touch what — the ownership gates every router leans on.

These lived at the top of routes.py when the whole HTTP surface was one file.
Now that surface is a package with sibling routers (lifeworld, self-repair,
logs), and the gates belong to none of those domains: they are the app's
authorization model in seven functions. A session cookie names a user; a user
owns projects and every row hanging off them; root sees all because root owns
the server. Missing and forbidden both answer 404, so a guessed id learns
nothing — not even that the row exists.
"""

from fastapi import HTTPException, Request

from . import auth, config, db


def current_user(request: Request) -> dict:
    u = auth.user_for_token(request.cookies.get("devteam_session"))
    if not u:
        raise HTTPException(401, "not signed in")
    return u


def can_see(project: dict, user: dict) -> bool:
    """Projects are private to their owner. The root/operator account can see all
    (it owns the server); legacy projects with no owner belong to root."""
    if user["is_root"]:
        return True
    return project.get("owner_id") == user["id"]


def owned_task(task_id: int, request: Request) -> dict:
    """Fetch a task only if the caller may see the project it belongs to.

    Task ids are global, so without this any signed-in user could read another
    user's agent transcripts or steer their work just by guessing a number.
    """
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, "no such task")
    owned_project(t["project_id"], request)
    return t


def owned_project(project_id: int, request: Request) -> dict:
    """Fetch a project only if the signed-in user is allowed to see it."""
    u = current_user(request)
    p = db.get_project(project_id)
    if not p:
        raise HTTPException(404, "no such project")
    if not can_see(p, u):
        raise HTTPException(404, "no such project")     # don't leak existence
    return p


def _owned(request: Request, getter, obj_id: int, label: str, *, allow_root: bool = False) -> dict:
    """Fetch a row by id and 404 unless it belongs to the caller — the one
    fetch-or-404-and-check-ownership shape every scoped resource (round table,
    Studio agent, scene, artifact def) needs."""
    u = current_user(request)
    row = getter(obj_id)
    if not row or (row["owner_id"] != u["id"] and not (allow_root and u["is_root"])):
        raise HTTPException(404, f"no such {label}")
    return row


def owned_table(table_id: int, request: Request) -> dict:
    return _owned(request, db.get_table, table_id, "round table", allow_root=True)


def _root(request: Request) -> dict:
    """Who may point the team at this platform's own codebase.

    Root always may. Beyond that it is an operator decision, not a user one —
    self-repair writes to the repo this server runs from, so it is granted in the
    server's own environment (SELFREPAIR_USERS) rather than from inside the app,
    where a compromised account could grant it to itself.
    """
    u = current_user(request)
    if not config.may_self_repair(u["username"], bool(u["is_root"])):
        raise HTTPException(403, "you are not allowed to work on the platform itself")
    return u
