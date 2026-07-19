import asyncio

from fastapi import APIRouter, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import bus, config, db, lead

router = APIRouter()
_lead_tasks: dict[int, asyncio.Task] = {}


class NewProject(BaseModel):
    name: str
    brief: str
    repo: str = ""
    budget_usd: float = 0
    max_workers: int = 0


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


@router.get("/api/health")
def health() -> dict:
    return {"ok": True, "launcher": config.LAUNCHER,
            "anthropic_key": bool(config.ANTHROPIC_API_KEY),
            "github": bool(config.GITHUB_TOKEN)}


@router.post("/api/projects")
async def create_project(body: NewProject) -> dict:
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(400, "ANTHROPIC_API_KEY is not set on the conductor")
    repo = body.repo or config.GITHUB_REPO
    project_id = db.create_project(
        body.name, body.brief, repo,
        body.budget_usd or config.PROJECT_BUDGET_USD,
        body.max_workers or config.MAX_CONCURRENT_WORKERS,
    )
    bus.emit(project_id, None, "system", "project_created", {"name": body.name})
    _lead_tasks[project_id] = asyncio.get_event_loop().create_task(lead.run_lead(project_id))
    return {"id": project_id}


@router.get("/api/projects")
def list_projects() -> list[dict]:
    return db.list_projects()


@router.get("/api/projects/{project_id}")
def get_project(project_id: int) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "no such project")
    project["tasks"] = db.list_tasks(project_id)
    return project


@router.post("/api/projects/{project_id}/restart")
async def restart_project(project_id: int) -> dict:
    """Re-run the lead session on a failed/review/cancelled project (tasks are kept)."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "no such project")
    if project["status"] not in ("failed", "review", "cancelled"):
        raise HTTPException(400, f"cannot restart a project in status '{project['status']}'")
    existing = _lead_tasks.get(project_id)
    if existing and not existing.done():
        raise HTTPException(400, "lead session is still running")
    db.set_project_status(project_id, "planning")
    bus.emit(project_id, None, "system", "project_restarted", {})
    _lead_tasks[project_id] = asyncio.get_event_loop().create_task(lead.run_lead(project_id))
    return {"ok": True}


@router.post("/api/projects/{project_id}/cancel")
def cancel_project(project_id: int) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "no such project")
    db.set_project_status(project_id, "cancelled")
    bus.emit(project_id, None, "system", "project_cancelled", {})
    return {"ok": True}


@router.get("/api/projects/{project_id}/events")
def get_events(project_id: int, after: int = 0) -> list[dict]:
    return db.list_events(project_id, after_id=after)


# --- internal endpoints used by workers ---

def _check_token(token: str | None) -> None:
    if token != config.WORKER_TOKEN:
        raise HTTPException(401, "bad worker token")


@router.post("/internal/events")
def worker_event(body: WorkerEvent, x_worker_token: str | None = Header(None)) -> dict:
    _check_token(x_worker_token)
    bus.emit(body.project_id, body.task_id, body.source, body.kind, body.payload)
    if body.kind == "agent_status" and body.payload == "running":
        db.update_task(body.task_id, status="running")
    return {"ok": True}


@router.post("/internal/report")
def worker_report(body: WorkerReport, x_worker_token: str | None = Header(None)) -> dict:
    _check_token(x_worker_token)
    status = "pushed" if body.status == "pushed" else "failed"
    task = db.get_task(body.task_id)
    db.update_task(body.task_id, status=status, report=body.report,
                   cost_usd=(task["cost_usd"] if task else 0) + body.cost_usd)
    db.add_project_cost(body.project_id, body.cost_usd)
    bus.emit(body.project_id, body.task_id, f"worker:{task['role'] if task else '?'}",
             "report", {"status": status, "cost_usd": body.cost_usd,
                        "summary": body.report[:2000]})
    return {"ok": True}


# --- websocket live feed ---

@router.websocket("/ws")
async def ws_feed(ws: WebSocket) -> None:
    await ws.accept()
    q = bus.subscribe()
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(q)
