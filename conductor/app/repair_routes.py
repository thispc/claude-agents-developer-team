"""HTTP surface for self-repair v2 — its own router (like the Lifeworld's), every endpoint
behind the same _root gate the legacy self area uses: self-repair writes to the repo this
server runs from, so it is an operator power, not a user feature."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import db, repair
from . import repair_builder as rb
from .routes import _root

router = APIRouter(prefix="/api/repair", tags=["repair"])


class ToggleBody(BaseModel):
    on: bool


class FactorsBody(BaseModel):
    set: list[dict] = Field(default_factory=list)     # [{id, enabled}]
    add: dict | None = None                            # {name, brief}
    remove: str | None = None


class ChatText(BaseModel):
    text: str = ""


class BranchBody(BaseModel):
    branch: str


class ShaBody(BaseModel):
    sha: str


@router.get("/status")
def repair_status(request: Request) -> dict:
    """Everything the Improve screen renders, in one payload: the button, the phase, the
    token meters, the factors, the crew, the current sprint, the queue and the backlog."""
    _root(request)
    return repair.status()


@router.post("/toggle")
def repair_toggle(body: ToggleBody, request: Request) -> dict:
    """THE BUTTON. On means the crew sprints on this repository until told otherwise."""
    _root(request)
    return {"enabled": repair.toggle(body.on)}


@router.post("/factors")
def repair_factors(body: FactorsBody, request: Request) -> dict:
    """Turn a factor on or off, or add one of your own. A factor is a lens the crew looks
    through, and each enabled one is a seat at the sprint-planning table."""
    _root(request)
    return {"factors": repair.set_factors(body.set, body.add, body.remove)}


@router.get("/ledger")
def repair_ledger(request: Request) -> dict:
    """The crew's own call counter and the meters derived from it — the backstop used when
    nothing reports real token counts."""
    _root(request)
    return {"meters": repair.meters(), "entries": (db.kv_get("repair:ledger") or [])[-100:]}


@router.get("/sprints")
def repair_sprints(request: Request, limit: int = 20) -> dict:
    """Every sprint, oldest first: enough of each to list it. Open one for its full board."""
    _root(request)
    rows = sorted(db.kv_prefix("repair:sprint:").values(), key=lambda r: r.get("no", 0))
    return {"sprints": rows[-max(1, min(limit, 100)):]}


@router.get("/sprint/{no}")
def repair_sprint(no: int, request: Request) -> dict:
    """One sprint in full — its board. The list view can only say "2 landed, 1 failed"; the
    question that actually gets asked is *which* one failed and what the suite said about it,
    and that answer was only ever in the server log."""
    _root(request)
    rec = db.kv_get(f"repair:sprint:{int(no)}")
    if not rec:
        raise HTTPException(404, f"no sprint {no}")
    return {"sprint": rec}


@router.get("/activity")
def repair_activity(request: Request, after: int = 0, limit: int = 120) -> dict:
    """The crew's own event stream. Every phase transition already goes to the bus under
    project 0 — this is what makes it visible on the Improve screen instead of only in the
    terminal the server happens to be running in."""
    _root(request)
    rows = db.list_events(0, after_id=max(0, int(after)), limit=max(1, min(int(limit), 500)))
    return {"events": [r for r in rows if r.get("source") == "repair"]}


@router.post("/chat")
async def repair_chat(body: ChatText, request: Request) -> dict:
    """Say something to the crew's hidden manager, which answers with the graph in front of
    it — the same mediator that runs the deliberations."""
    _root(request)
    info = repair.ensure_team()
    if not info:
        raise HTTPException(409, "no crew yet — enable a factor first")
    from .lifeworld import store
    # The engine's sprint does load → await → save on the same world. Without this lock the
    # two overlap and the last writer wins, so a chat could erase a sprint's memo, or a
    # sprint could erase the conversation.
    async with store.lock_for(info["world_id"]):
        w = store.load(info["world_id"], live=repair._live(),
                       settings=repair._root_settings() if repair._live() else None)
        s = w.scene(info["room_id"])
        t = s.thread(info["thread_id"]) if s else None
        if t is None:
            raise HTTPException(409, "the crew's table is missing — toggle repair off and on")
        res = await s.chat(t, "manager", body.text)
        store.save(w)
    return res


@router.post("/queue/approve")
async def repair_approve(body: BranchBody, request: Request) -> dict:
    """Land a branch waiting in the review queue (supervised mode, or a build that finished
    while your own tree was dirty)."""
    _root(request)
    q = db.kv_get("repair:queue") or []
    item = next((x for x in q if x["branch"] == body.branch), None)
    if not item:
        raise HTTPException(404, "no such queued branch")
    out = rb.land(item["branch"], item["title"])
    if not out.get("ok"):
        raise HTTPException(409, out.get("reason", "could not land"))
    db.kv_set("repair:queue", [x for x in q if x["branch"] != body.branch])
    rb.discard(item["branch"], item.get("worktree"))
    rec = repair.sprint(item["sprint_no"])
    if rec:
        for t in rec["tasks"]:
            if t.get("branch") == body.branch:
                t["status"], t["landed_sha"] = "landed", out["sha"]
        rec["landed"] += 1
        rec["landed_files"] = sorted(set(rec.get("landed_files", []) + out.get("files", [])))
        repair.save_sprint(rec)
    files = out.get("files") or []
    return {"landed_sha": out["sha"], "files": files,
            # The UI's cue to offer the restart right away, without waiting for the poll.
            "needs_restart": any(f.startswith(("conductor/", "worker/")) for f in files)}


@router.post("/queue/rebuild")
async def repair_rebuild(body: BranchBody, request: Request) -> dict:
    """Rebase a stale queued branch onto today's main and re-run the suite on it.

    "main moved since this branch was cut" was a dead end: the work was finished and green,
    and the only offer on screen was to discard it. Re-verifying after the rebase is not
    optional — green against yesterday's main is not evidence about today's.
    """
    _root(request)
    from . import logs
    q = db.kv_get("repair:queue") or []
    item = next((x for x in q if x["branch"] == body.branch), None)
    if not item:
        raise HTTPException(404, "no such queued branch")
    out = rb.rebuild(item["branch"], item.get("worktree"))
    if not out.get("ok"):
        logs.warn("git", "rebuild_failed", f"{item['title']} — {out.get('reason')}",
                  branch=item["branch"])
        raise HTTPException(409, out.get("reason", "could not rebuild"))
    res = await rb.verify(out["worktree"])
    logs.log("verify", "suite_green" if res.get("ok") else "suite_red",
             f"after rebuild: {item['title']}", level="info" if res.get("ok") else "warn",
             branch=item["branch"])
    for x in q:
        if x["branch"] == body.branch:
            x["verification"] = res
            x["note"] = "" if res.get("ok") else f"still red after rebuild: {res.get('headline', '')}"
    db.kv_set("repair:queue", q)
    return {"ok": True, "verified": res.get("ok", False), "headline": res.get("headline", "")}


@router.post("/queue/discard")
def repair_discard(body: BranchBody, request: Request) -> dict:
    """Throw a queued branch away, worktree and all."""
    _root(request)
    q = db.kv_get("repair:queue") or []
    item = next((x for x in q if x["branch"] == body.branch), None)
    if not item:
        raise HTTPException(404, "no such queued branch")
    rb.discard(item["branch"], item.get("worktree"))
    db.kv_set("repair:queue", [x for x in q if x["branch"] != body.branch])
    return {"ok": True}


@router.post("/revert")
def repair_revert(body: ShaBody, request: Request) -> dict:
    """Undo a landed change. One task is one squashed commit, so this is one `git revert`."""
    _root(request)
    out = rb.revert(body.sha)
    if not out.get("ok"):
        raise HTTPException(409, out.get("reason", "could not revert"))
    return out


@router.post("/abort")
async def repair_abort(request: Request) -> dict:
    """Stop the task being built right now and clean up after it. The crew carries on."""
    _root(request)
    return {"ok": await repair.abort()}


@router.post("/restart")
async def repair_restart(request: Request) -> dict:
    """Restart the server on the code currently on disk.

    Edit the dashboard while the conductor is running and the page runs ahead of the API,
    which looks exactly like a broken feature — an empty panel, a button that does nothing.
    The banner that reports it used to end with a command for a port this setup no longer
    uses, so the one instruction on screen was wrong. Now it is a button. `restart_process`
    execs in place, keeping the pid and whatever supervises it.
    """
    _root(request)
    from . import logs, selfops
    logs.info("lifecycle", "restart_requested", "root asked for a restart from the dashboard")
    import asyncio
    asyncio.get_event_loop().call_later(0.3, selfops.restart_process)
    return {"ok": True, "restarting": True}
