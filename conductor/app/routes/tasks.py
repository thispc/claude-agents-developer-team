"""Direct task surgery by the boss: add, edit, retry, skip.

These bypass the manager on purpose — the DAG is the boss's to reshape — but
they never leave the machinery inconsistent: an edit that would make the graph
cyclic is rejected, a skip kills the agent still working the task, and a retry
on a parked project revives the manager, because a worker whose result nobody
judges hangs in review forever.
"""

import asyncio

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .. import bus, db, github_client, launcher, manager, scheduler
from .base import _manager_tasks, owned_project, owned_task, router


class NewTask(BaseModel):
    role: str
    title: str
    description: str
    depends_on: list[int] = []


class EditTask(BaseModel):
    title: str | None = None
    description: str | None = None
    depends_on: list[int] | None = None


@router.post("/api/projects/{project_id}/tasks")
async def add_task(project_id: int, body: NewTask, request: Request) -> dict:
    """Boss adds a task to the DAG directly (no manager needed)."""
    project = owned_project(project_id, request)
    valid = {t["id"] for t in db.list_tasks(project_id)}
    deps = [d for d in body.depends_on if d in valid]
    task_id = db.create_task(project_id, body.role, body.title, body.description,
                             deps=deps, origin="runtime")
    # New work on a finished project puts it back to work: reopen it and bring the
    # manager back so the task is planned, reviewed and shipped like any other.
    if project["status"] in ("done", "failed", "review", "cancelled"):
        db.set_project_status(project_id, "running")
        existing = _manager_tasks.get(project_id)
        if not existing or existing.done():
            _manager_tasks[project_id] = asyncio.get_event_loop().create_task(
                manager.run_manager(project_id))
        bus.emit(project_id, None, "system", "reopened",
                 {"reason": f"boss added a new {body.role} task"})
    if github_client.enabled(project["repo"]):
        try:
            n = await github_client.create_issue(
                project["repo"], f"[{body.role}] {body.title}",
                body.description + f"\n\n_devteam task {task_id} (added by boss)_")
            db.update_task(task_id, issue_number=n)
        except Exception:
            pass
    scheduler.ensure(project_id)
    bus.emit(project_id, task_id, "boss", "task_added", {"role": body.role, "title": body.title})
    return {"id": task_id}


@router.post("/api/tasks/{task_id}/edit")
def edit_task(task_id: int, body: EditTask, request: Request) -> dict:
    """Boss edits a task's title/spec/dependencies live."""
    t = owned_task(task_id, request)
    fields: dict = {}
    if body.title is not None:
        fields["title"] = body.title
    if body.description is not None:
        fields["description"] = body.description
    if body.depends_on is not None:
        valid = {x["id"] for x in db.list_tasks(t["project_id"]) if x["id"] != task_id}
        fields["deps"] = db.json.dumps([d for d in body.depends_on if d in valid])
    if fields:
        db.update_task(task_id, **fields)
        cycle = scheduler.has_cycle(t["project_id"])
        if cycle:  # reject the edit — a DAG must stay acyclic
            db.update_task(task_id, deps=t["deps"])  # revert dependency change
            raise HTTPException(400, f"that dependency would create a cycle: {cycle}")
    bus.emit(t["project_id"], task_id, "boss", "task_edited", {"fields": list(fields)})
    return {"ok": True}


@router.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: int, request: Request) -> dict:
    t = owned_task(task_id, request)
    pid = t["project_id"]
    db.update_task(task_id, status="planned")
    # Re-running work on a finished/parked project needs BOTH halves alive: the
    # scheduler to dispatch it, and a manager to judge the result. Without the
    # manager the worker pushes, the task lands in review, and nothing ever looks
    # at it — the boss sees it hang forever.
    revived = _revive(pid)
    scheduler.ensure(pid)
    bus.emit(pid, task_id, "boss", "retry_requested", {"manager_started": revived})
    return {"ok": True, "manager_started": revived}


def _revive(project_id: int) -> bool:
    """Make sure a parked project has a manager again. Returns True if one started."""
    p = db.get_project(project_id)
    if not p or p["status"] == "cancelled":
        return False
    if p["status"] in ("done", "review", "failed"):
        db.set_project_status(project_id, "running")
    existing = _manager_tasks.get(project_id)
    if existing and not existing.done():
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False        # called from a thread with no loop; nothing to schedule
    _manager_tasks[project_id] = loop.create_task(manager.run_manager(project_id))
    return True


@router.post("/api/tasks/{task_id}/skip")
def skip_task(task_id: int, request: Request) -> dict:
    """Boss marks a task done/skipped so dependents can proceed."""
    t = owned_task(task_id, request)
    # Skipping a task the boss no longer wants must also stop the agent doing it —
    # otherwise it keeps running and spending against a task already marked done.
    if t["status"] in ("queued", "running"):
        launcher.kill_task(task_id, "task was skipped by the boss")
    db.update_task(task_id, status="done")
    scheduler.ensure(t["project_id"])
    bus.emit(t["project_id"], task_id, "boss", "task_skipped", {})
    return {"ok": True}
