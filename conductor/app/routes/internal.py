"""The worker door and the live feed: /internal/* plus the websocket.

Workers are not users — they authenticate with a shared token, checked in
constant time, and may only report on the task they were actually given,
because one leaked token must not let a caller forge outcomes for every
(project, task) pair in the system. The websocket lives here for the same
reason in reverse: the bus is global, so the feed filters every event down to
what the connected user is allowed to see.
"""

import hmac
import json

from fastapi import Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .. import auth, bus, config, db, team, usage
from .base import can_see, router


class WorkerEvent(BaseModel):
    project_id: int
    task_id: int
    source: str
    kind: str
    payload: str


class WorkerReport(BaseModel):
    project_id: int
    task_id: int
    status: str  # pushed | failed
    report: str
    cost_usd: float = 0
    # Tokens the worker's session actually consumed. Optional: an older worker binary
    # reports no tokens rather than failing, and its row then counts as calls only.
    tokens: int = 0
    cache_tokens: int = 0
    contender_id: int = 0
    verification: str = ""      # JSON, produced by the worker process not the model


def _check_token(token: str | None) -> None:
    # Constant-time: a plain != leaks the token one character at a time to anyone
    # who can measure the response.
    if not token or not hmac.compare_digest(token, config.WORKER_TOKEN):
        raise HTTPException(401, "bad worker token")


def _owns_task(project_id: int, task_id: int) -> None:
    """A worker may only report on the task it was actually given.

    Without this, one valid worker token lets any caller forge outcomes and costs
    for any (project, task) pair in the system.
    """
    t = db.get_task(task_id)
    if not t or t["project_id"] != project_id:
        raise HTTPException(400, "task does not belong to that project")


@router.post("/internal/events")
def worker_event(body: WorkerEvent, x_worker_token: str | None = Header(None)) -> dict:
    _check_token(x_worker_token)
    _owns_task(body.project_id, body.task_id)
    bus.emit(body.project_id, body.task_id, body.source, body.kind, body.payload)
    if body.kind == "agent_status" and body.payload == "running":
        db.update_task(body.task_id, status="running")
    else:
        db.touch_task(body.task_id)  # keep the stall watchdog from firing on busy tasks
    return {"ok": True}


def _close_run(task_id: int, status: str, cost_usd: float,
               contender_id: int | None = None) -> None:
    """Close the measurement row for a dispatch that just reported.

    'delivered' rather than 'accepted': the worker finished, which is not the same
    as the work being any good. The manager relabels it when it judges.
    """
    run = db.open_run_for(task_id, contender_id)
    if run:
        db.finish_run(run["id"], "delivered" if status == "pushed" else "failed",
                      cost_usd=cost_usd)


@router.get("/internal/teammate")
def teammate_context(project_id: int, role: str = "", name: str = "",
                     exclude_task: int = 0,
                     x_worker_token: str | None = Header(None)) -> dict:
    """Who a stuck worker is actually talking to, and what they have built.

    `ask_teammate` used to fire a one-shot query at a stronger model. That is a
    second opinion, but it is not a teammate: it had no idea what anyone on the
    project had already built, so its advice regularly contradicted decisions
    another agent had made an hour earlier — and the asker, having no way to know,
    followed it. Handing over the real teammate's persona and their delivered work
    is the difference between consulting a colleague and consulting a stranger who
    happens to be clever.
    """
    _check_token(x_worker_token)
    people = db.list_agents(project_id)
    if not people:
        return {"found": False}
    match = None
    if name:
        match = next((a for a in people if a["name"].lower() == name.lower()), None)
    if not match and role:
        match = next((a for a in people if a["role"].lower() == role.lower()), None)
    if not match:
        # Nobody named: the most experienced teammate is the best default, because
        # the point of asking is to reach someone who has seen more of this project.
        match = sorted(people, key=lambda a: -a["tasks_done"])[0]

    delivered = [t for t in db.list_tasks(project_id)
                 if t.get("agent_id") == match["id"] and t["status"] == "done"
                 and t["id"] != exclude_task]
    return {
        "found": True,
        "name": match["name"],
        "role": match["role"],
        "persona": match["persona"],
        "notes": match["notes"],
        "model": match["model"],
        "work": [{"title": t["title"], "report": (t["report"] or "")[:1500]}
                 for t in delivered[-3:]],
        "team": [{"name": a["name"], "role": a["role"]} for a in people],
    }


@router.post("/internal/report")
def worker_report(body: WorkerReport, x_worker_token: str | None = Header(None)) -> dict:
    _check_token(x_worker_token)
    _owns_task(body.project_id, body.task_id)
    status = "pushed" if body.status == "pushed" else "failed"
    task = db.get_task(body.task_id)
    # Worker sessions draw on the same subscription quota the self-repair crew watches, so
    # they are metered here — once, before the branchy paths below each bill the project.
    usage.note("worker", (task or {}).get("model") or "", tok=body.tokens,
               cache=body.cache_tokens, usd=body.cost_usd)

    # A rival attempt reports into its own row; the task only advances once every
    # rival is in, and then it goes to the manager to judge — not straight to a PR.
    if body.contender_id:
        db.update_contender(body.contender_id, status=status, report=body.report[:12000])
        db.add_project_cost(body.project_id, body.cost_usd)
        _close_run(body.task_id, status, body.cost_usd, contender_id=body.contender_id)
        rivals = db.list_contenders(body.task_id)
        c = db.get_contender(body.contender_id)
        bus.emit(body.project_id, body.task_id, f"rival {c['idx'] if c else '?'}",
                 "rival_finished", {"status": status, "model": c["model"] if c else "",
                                    "summary": body.report[:800]})
        if all(r["status"] in ("pushed", "failed") for r in rivals):
            ok = [r for r in rivals if r["status"] == "pushed"]
            if ok:
                # Write a digest onto the TASK as well. get_report reads task.report,
                # and a contest used to leave it empty — so a manager that called
                # get_report saw "(no report yet)", concluded nothing was delivered,
                # and sent perfectly good rival work back again and again.
                digest = (f"CONTEST: {len(ok)} of {len(rivals)} rivals delivered. "
                          f"Use compare_work to judge them, then pick_winner.\n\n" +
                          "\n\n".join(f"--- rival #{r['idx']} ({r['model']}) [{r['status']}] ---\n"
                                       f"{(r['report'] or '')[:1500]}" for r in rivals))
                db.update_task(body.task_id, status="review", report=digest)
                bus.emit(body.project_id, body.task_id, "system", "contest_ready",
                         {"rivals": len(rivals), "finished_ok": len(ok)})
            else:
                db.update_task(body.task_id, status="failed",
                               report="all rival attempts failed:\n\n" +
                                      "\n\n".join(f"[#{r['idx']} {r['model']}] {r['report'][:800]}"
                                                  for r in rivals))
        return {"ok": True}
    from ..launcher import looks_rate_limited, note_rate_limit, cooldown_left
    if status == "failed" and looks_rate_limited(body.report):
        model = (task.get("model") if task else "") or ""
        note_rate_limit(model, body.report)      # capture the real retry-after
        bus.emit(body.project_id, body.task_id, "system", "rate_limited",
                 {"model": model, "cooldown_s": cooldown_left(model),
                  "detail": body.report[:300]})
    # A report from a superseded worker must not drag the task backwards.
    #
    # On the mars-rover run two workers ended up on task #7 at once (a re-dispatch
    # while the first was still alive). The manager accepted the first one's work
    # and closed the task; forty seconds later the second reported, and the task
    # flipped from 'done' back to 'pushed' — permanently, because the project was
    # already finished and nothing was left running to move it on again.
    if task and task["status"] == "done":
        bus.emit(body.project_id, body.task_id, "system", "late_report_ignored",
                 {"status": status, "note": "this task was already accepted; a second "
                                            "worker reported afterwards"})
        db.add_project_cost(body.project_id, body.cost_usd)
        return {"ok": True, "ignored": "task already accepted"}

    db.update_task(body.task_id, status=status, report=body.report,
                   verification=body.verification or "",
                   cost_usd=(task["cost_usd"] if task else 0) + body.cost_usd)
    db.add_project_cost(body.project_id, body.cost_usd)
    _close_run(body.task_id, status, body.cost_usd)
    # Back to the pool. Not credited with the task yet — that happens when the
    # manager accepts, because finishing and being any good are different events.
    team.release(db.get_task(body.task_id) or {}, body.report)
    try:
        v = json.loads(body.verification or "{}")
    except Exception:
        v = {}
    if v.get("ran"):
        bus.emit(body.project_id, body.task_id, "system",
                 "verified" if v.get("ok") else "verification_failed",
                 {"cmd": v.get("cmd"), "exit_code": v.get("exit_code")})
    bus.emit(body.project_id, body.task_id, f"worker:{task['role'] if task else '?'}",
             "report", {"status": status, "cost_usd": body.cost_usd,
                        "summary": body.report[:2000]})
    return {"ok": True}


# --- websocket live feed ---

@router.websocket("/ws")
async def ws_feed(ws: WebSocket) -> None:
    """Live event feed, filtered to what this user is allowed to see.

    The bus is global: every project's events pass through it. Without the
    check below an anonymous socket received the live activity — briefs, agent
    messages, reports — of every project belonging to every user.
    """
    user = auth.user_for_token(ws.cookies.get("devteam_session"))
    if not user:
        await ws.close(code=1008)      # policy violation
        return
    await ws.accept()
    q = bus.subscribe()
    visible: dict[int, bool] = {}      # project_id -> allowed, resolved once each
    try:
        while True:
            event = await q.get()
            pid = event.get("project_id")
            if pid not in visible:
                if pid == 0:
                    # Project 0 is the platform itself: the crew's and the module
                    # graph's events. Same gate as the Improve tile — visible to
                    # whoever may self-repair, DROPPED for everyone else, so the
                    # graph screen gets a live feed without HQ-style polling.
                    visible[0] = config.may_self_repair(user["username"],
                                                       bool(user["is_root"]))
                else:
                    p = db.get_project(pid) if pid else None
                    visible[pid] = bool(p and can_see(p, user))
            if visible[pid]:
                await ws.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(q)
