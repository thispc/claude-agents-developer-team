import asyncio

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import auth, bus, config, db, github_client, manager, planner, preview, scheduler

router = APIRouter()
_manager_tasks: dict[int, asyncio.Task] = {}


class TeamMember(BaseModel):
    role: str
    count: int = 1
    model: str = "worker"


class NewProject(BaseModel):
    name: str
    brief: str
    repo: str = ""
    budget_usd: float = 0
    max_workers: int = 0
    max_runs: int = 0
    team: list[TeamMember] = []
    autonomy: str = "supervised"
    manager_model: str = ""
    manager_persona: str = ""


class BriefOnly(BaseModel):
    brief: str


class Login(BaseModel):
    username: str
    password: str


class Settings(BaseModel):
    github_token: str | None = None
    anthropic_api_key: str | None = None


class NewRepo(BaseModel):
    name: str
    private: bool = True


class NewTask(BaseModel):
    role: str
    title: str
    description: str
    depends_on: list[int] = []


class EditTask(BaseModel):
    title: str | None = None
    description: str | None = None
    depends_on: list[int] | None = None


class Directive(BaseModel):
    text: str


class Answer(BaseModel):
    answer: str


class Budget(BaseModel):
    budget_usd: float


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


# --- auth & per-user settings ------------------------------------------------

def current_user(request: Request) -> dict:
    u = auth.user_for_token(request.cookies.get("devteam_session"))
    if not u:
        raise HTTPException(401, "not signed in")
    return u


@router.post("/api/login")
def login(body: Login, response: Response) -> dict:
    u = auth.verify(body.username, body.password)
    if not u:
        raise HTTPException(401, "wrong username or password")
    token = auth.start_session(u["id"])
    response.set_cookie("devteam_session", token, httponly=True, samesite="lax", max_age=30 * 86400)
    return {"username": u["username"], "is_root": bool(u["is_root"])}


@router.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    auth.end_session(request.cookies.get("devteam_session"))
    response.delete_cookie("devteam_session")
    return {"ok": True}


@router.get("/api/me")
def me(request: Request) -> dict:
    u = auth.user_for_token(request.cookies.get("devteam_session"))
    if not u:
        return {"signed_in": False}
    s = auth.get_settings(u)
    return {"signed_in": True, "username": u["username"], "is_root": bool(u["is_root"]),
            "settings": auth.redacted(s)}


@router.post("/api/settings")
def save_settings(body: Settings, request: Request) -> dict:
    u = current_user(request)
    auth.save_settings(u["id"], body.model_dump(exclude_none=True))
    return auth.redacted(auth.get_settings(auth.get_user(u["id"])))


@router.get("/api/github/repos")
async def list_my_repos(request: Request) -> dict:
    """The signed-in user's repos, for the project picker."""
    u = current_user(request)
    token = auth.get_settings(u).get("github_token", "")
    if not token:
        raise HTTPException(400, "no GitHub token set — add one in Settings")
    return {"repos": await github_client.list_user_repos(token)}


@router.post("/api/github/repos")
async def create_my_repo(body: NewRepo, request: Request) -> dict:
    u = current_user(request)
    token = auth.get_settings(u).get("github_token", "")
    if not token:
        raise HTTPException(400, "no GitHub token set — add one in Settings")
    ok, result = await github_client.create_user_repo(token, body.name, body.private)
    if not ok:
        raise HTTPException(400, result)
    return {"repo": result}


@router.get("/api/health")
def health() -> dict:
    return {"ok": True, "launcher": config.LAUNCHER, "auth": config.auth_mode(),
            "github": bool(config.GITHUB_TOKEN)}


@router.post("/api/suggest-team")
async def suggest_team(body: BriefOnly) -> dict:
    """Recruiting: propose a starting team from the brief for the boss to tweak."""
    return {"team": await planner.suggest_team(body.brief),
            "known_roles": [r["name"] for r in config.load_roles()]}


@router.post("/api/projects")
async def create_project(body: NewProject, request: Request) -> dict:
    owner = auth.user_for_token(request.cookies.get("devteam_session"))
    if not config.AUTH_CONFIGURED:
        raise HTTPException(400, "Set ANTHROPIC_API_KEY (API billing) or "
                                 "CLAUDE_CODE_OAUTH_TOKEN (Pro/Max subscription) on the conductor")
    if config.LAUNCHER == "k8s" and config.CLI_LOGIN:
        raise HTTPException(400, "k8s workers cannot inherit local CLI credentials — "
                                 "set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) "
                                 "or ANTHROPIC_API_KEY")
    repo = body.repo or config.GITHUB_REPO
    team = [m.model_dump() for m in body.team]
    autonomy = "autonomous" if body.autonomy == "autonomous" else "supervised"
    project_id = db.create_project(
        body.name, body.brief, repo,
        body.budget_usd or config.PROJECT_BUDGET_USD,
        body.max_workers or config.MAX_CONCURRENT_WORKERS,
        body.max_runs or config.MAX_AGENT_RUNS,
        team=team, autonomy=autonomy,
        manager_model=body.manager_model.strip(),
        manager_persona=body.manager_persona.strip(),
        owner_id=(owner["id"] if owner else 0),
    )
    bus.emit(project_id, None, "system", "project_created", {"name": body.name})
    # Make sure the target repo exists before the team tries to clone it. This is
    # why manual repo creation is unnecessary — the token creates a private repo.
    if repo and github_client.enabled(repo):
        ok, note = await github_client.ensure_repo(repo)
        bus.emit(project_id, None, "system", "repo_ready" if ok else "repo_error", note)
    _manager_tasks[project_id] = asyncio.get_event_loop().create_task(manager.run_manager(project_id))
    return {"id": project_id}


@router.get("/api/projects")
def list_projects() -> list[dict]:
    projects = db.list_projects()
    for p in projects:
        p["task_count"] = len(db.list_tasks(p["id"]))
    return projects


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
    existing = _manager_tasks.get(project_id)
    if existing and not existing.done():
        raise HTTPException(400, "manager session is still running")
    db.set_project_status(project_id, "planning")
    bus.emit(project_id, None, "system", "project_restarted", {})
    _manager_tasks[project_id] = asyncio.get_event_loop().create_task(manager.run_manager(project_id))
    return {"ok": True}


@router.post("/api/projects/{project_id}/cancel")
async def cancel_project(project_id: int) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "no such project")
    db.set_project_status(project_id, "cancelled")
    db.abandon_questions(project_id)
    scheduler.stop(project_id)
    t = _manager_tasks.get(project_id)
    if t and not t.done():
        t.cancel()  # aborts the manager session immediately instead of at its next wait
    # Close this project's still-open GitHub issues so they don't linger as orphans.
    repo = project["repo"]
    if github_client.enabled(repo):
        for task in db.list_tasks(project_id):
            if task["issue_number"] and task["status"] not in ("done",):
                try:
                    await github_client.close_issue(repo, task["issue_number"])
                except Exception:
                    pass
    bus.emit(project_id, None, "system", "project_cancelled", {})
    return {"ok": True}


@router.get("/api/projects/{project_id}/events")
def get_events(project_id: int, after: int = 0) -> list[dict]:
    return db.list_events(project_id, after_id=after)


@router.get("/api/projects/{project_id}/artifacts")
async def get_artifacts(project_id: int) -> dict:
    """Everything the project produced: repo, branches, PRs, and the public site URL."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "no such project")
    repo = project["repo"]
    out = {"repo": repo, "repo_url": f"https://github.com/{repo}" if repo else None,
           "prs": [], "branches": [], "pages_url": None}
    if github_client.enabled(repo):
        try:
            out["prs"] = await github_client.list_prs(repo)
            out["branches"] = await github_client.list_branches(repo)
            out["pages_url"] = await github_client.get_pages_url(repo)
        except Exception as e:
            out["error"] = str(e)[:200]
    return out


@router.post("/api/projects/{project_id}/preview")
async def build_preview(project_id: int) -> dict:
    """Sync the project's static site into our own preview host → viewable at /preview/{id}/."""
    if not db.get_project(project_id):
        raise HTTPException(404, "no such project")
    ok, note = await preview.sync(project_id)
    if not ok:
        raise HTTPException(400, note)
    bus.emit(project_id, None, "boss", "preview_ready", {"url": f"/preview/{project_id}/"})
    return {"url": f"/preview/{project_id}/"}


@router.get("/preview/{project_id}/{path:path}")
def serve_preview(project_id: int, path: str) -> Response:
    """Serve the built static site read-only. Sandboxed: static files only, and a CSP
    keeps the previewed app from touching the control-plane API."""
    root = preview.preview_root(project_id)
    if root is None:
        raise HTTPException(404, "not previewed yet — click Preview in Artifacts first")
    target = (root / (path or "index.html")).resolve()
    if not str(target).startswith(str(root.resolve())):  # path-traversal guard
        raise HTTPException(403, "forbidden")
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        target = root / "index.html"  # SPA fallback
    # Sandbox the previewed app: it may run its own JS, but it can't call our API
    # (same-origin fetch to /api is blocked) and can't be embedded elsewhere.
    csp = "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: https:; " \
          "connect-src https: http:; frame-ancestors 'self'"
    return FileResponse(target, headers={"Content-Security-Policy": csp,
                                         "X-Devteam-Preview": str(project_id)})


@router.post("/api/projects/{project_id}/publish")
async def publish_artifacts(project_id: int) -> dict:
    """Enable GitHub Pages → a public link named after the repo/project."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "no such project")
    if not github_client.enabled(project["repo"]):
        raise HTTPException(400, "GitHub not configured for this project")
    ok, result = await github_client.enable_pages(project["repo"])
    if not ok:
        raise HTTPException(400, result)
    bus.emit(project_id, None, "boss", "published", {"url": result})
    return {"url": result}


@router.get("/api/tasks/{task_id}/events")
def get_task_events(task_id: int) -> list[dict]:
    """Full start-to-end transcript for one task's agent (messages + tool calls)."""
    return db.list_task_events(task_id)


# --- boss controls -----------------------------------------------------------

@router.post("/api/projects/{project_id}/directive")
def send_directive(project_id: int, body: Directive) -> dict:
    """Boss -> manager message. Delivered at the manager's next decision point."""
    if not db.get_project(project_id):
        raise HTTPException(404, "no such project")
    db.add_directive(project_id, body.text)
    bus.emit(project_id, None, "boss", "directive", body.text)
    return {"ok": True}


@router.get("/api/projects/{project_id}/question")
def get_pending_question(project_id: int) -> dict:
    """The manager's open question for the boss, if any."""
    q = db.pending_question(project_id)
    if not q:
        return {"question": None}
    # NOTE: the key is "question" (not "text") — the dashboard keys off it to raise
    # the approval modal. Keep this name stable.
    return {"id": q["id"], "question": q["text"], "options": db.json.loads(q["options"])}


@router.post("/api/questions/{qid}/answer")
def answer(qid: int, body: Answer) -> dict:
    q = db.get_question(qid)
    if not q:
        raise HTTPException(404, "no such question")
    db.answer_question(qid, body.answer)
    bus.emit(q["project_id"], None, "boss", "answered",
             {"question": q["text"], "answer": body.answer})
    return {"ok": True}


@router.post("/api/projects/{project_id}/budget")
def set_budget(project_id: int, body: Budget) -> dict:
    if not db.get_project(project_id):
        raise HTTPException(404, "no such project")
    db._execute("UPDATE projects SET budget_usd=? WHERE id=?", (body.budget_usd, project_id))
    bus.emit(project_id, None, "boss", "budget_changed", {"budget_usd": body.budget_usd})
    return {"ok": True}


@router.post("/api/projects/{project_id}/tasks")
async def add_task(project_id: int, body: NewTask) -> dict:
    """Boss adds a task to the DAG directly (no manager needed)."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "no such project")
    valid = {t["id"] for t in db.list_tasks(project_id)}
    deps = [d for d in body.depends_on if d in valid]
    task_id = db.create_task(project_id, body.role, body.title, body.description,
                             deps=deps, origin="runtime")
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
def edit_task(task_id: int, body: EditTask) -> dict:
    """Boss edits a task's title/spec/dependencies live."""
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, "no such task")
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
def retry_task(task_id: int) -> dict:
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, "no such task")
    db.update_task(task_id, status="planned")
    scheduler.ensure(t["project_id"])
    bus.emit(t["project_id"], task_id, "boss", "retry_requested", {})
    return {"ok": True}


@router.post("/api/tasks/{task_id}/skip")
def skip_task(task_id: int) -> dict:
    """Boss marks a task done/skipped so dependents can proceed."""
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, "no such task")
    db.update_task(task_id, status="done")
    scheduler.ensure(t["project_id"])
    bus.emit(t["project_id"], task_id, "boss", "task_skipped", {})
    return {"ok": True}


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
    else:
        db.touch_task(body.task_id)  # keep the stall watchdog from firing on busy tasks
    return {"ok": True}


@router.post("/internal/report")
def worker_report(body: WorkerReport, x_worker_token: str | None = Header(None)) -> dict:
    _check_token(x_worker_token)
    status = "pushed" if body.status == "pushed" else "failed"
    task = db.get_task(body.task_id)
    from .launcher import looks_rate_limited
    if status == "failed" and looks_rate_limited(body.report):
        bus.emit(body.project_id, body.task_id, "system", "rate_limited",
                 {"model": task.get("model") if task else "", "detail": body.report[:300]})
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
