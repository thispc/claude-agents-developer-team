import asyncio
import hmac
import json

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import (auth, blockers, bus, cloud, config, credcheck, db, deploy, notify,
               envs, github_client, launcher, manager, planner, preview, providers,
               roundtable, sandbox, scheduler, selfops, triage)

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
    sprints: int = 1


class BriefOnly(BaseModel):
    brief: str


class Login(BaseModel):
    username: str
    password: str


class Settings(BaseModel):
    github_token: str | None = None
    anthropic_api_key: str | None = None
    claude_oauth_token: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None


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


class Seat(BaseModel):
    name: str
    provider: str
    model: str
    persona: str = ""


class NewTable(BaseModel):
    brief: str
    title: str = ""
    mode: str = "debate"        # diverge | debate
    seats: list[Seat] = []
    mod_provider: str = ""
    mod_model: str = ""


class BuildFromBlueprint(BaseModel):
    name: str
    repo: str = ""
    autonomy: str = "supervised"
    manager_model: str = ""


class Autonomy(BaseModel):
    autonomy: str          # supervised | autonomous


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
    contender_id: int = 0
    verification: str = ""      # JSON, produced by the worker process not the model


# --- auth & per-user settings ------------------------------------------------

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


@router.post("/api/login")
def login(body: Login, response: Response) -> dict:
    wait = auth.locked_out(body.username)
    if wait:
        # Say it plainly. A locked account that reports "wrong password" sends the
        # owner hunting for a typo while an attacker learns nothing either way.
        raise HTTPException(429, f"too many failed attempts — try again in {wait}s")
    u = auth.verify(body.username, body.password)
    if not u:
        raise HTTPException(401, "wrong username or password")
    token = auth.start_session(u["id"])
    response.set_cookie("devteam_session", token, httponly=True, samesite="lax", max_age=30 * 86400)
    return {"username": u["username"], "is_root": bool(u["is_root"])}


@router.post("/api/signup")
def signup(body: Login, response: Response) -> dict:
    """Anyone can create an account, but they bring their own AI credentials —
    a new user never runs on the operator's subscription."""
    name = body.username.strip().lower()
    if len(name) < 3 or len(body.password) < 6:
        raise HTTPException(400, "username needs 3+ chars, password 6+")
    if auth.get_user_by_name(name):
        raise HTTPException(400, "that username is taken")
    uid = auth.create_user(name, body.password)
    token = auth.start_session(uid)
    response.set_cookie("devteam_session", token, httponly=True, samesite="lax", max_age=30 * 86400)
    return {"username": name, "is_root": False, "needs_credentials": True}


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
            "has_ai_credentials": auth.has_own_ai_credentials(u),
            "may_self_repair": config.may_self_repair(u["username"], bool(u["is_root"])),
            "settings": auth.redacted(s)}


@router.post("/api/settings")
def save_settings(body: Settings, request: Request) -> dict:
    u = current_user(request)
    auth.save_settings(u["id"], body.model_dump(exclude_none=True))
    return auth.redacted(auth.get_settings(auth.get_user(u["id"])))


class VerifyCred(BaseModel):
    kind: str
    value: str = ""      # blank = check the one already stored


@router.post("/api/settings/verify")
async def verify_credential(body: VerifyCred, request: Request) -> dict:
    """Prove a credential works before the user finds out by losing a project.

    Checks the value being typed if there is one, otherwise the stored value —
    so 'Check' is useful both while entering a key and long afterwards.
    """
    u = current_user(request)
    if body.kind not in credcheck.KINDS:
        raise HTTPException(400, f"cannot verify {body.kind!r}")
    value = body.value.strip() or auth.get_settings(u).get(body.kind, "")
    return await credcheck.check(body.kind, value)


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


@router.get("/api/agents")
def list_agents(request: Request, project_id: int | None = None) -> dict:
    """Live infrastructure: worker machines currently running, plus how work is being
    executed (local processes vs Kubernetes Jobs). Scoped to one project when given."""
    from . import launcher as lx
    import time as _t
    user = current_user(request)
    if project_id is not None:
        owned_project(project_id, request)
    agents = []
    for key, info in list(lx.ACTIVE.items()):
        if project_id is not None and info["project_id"] != project_id:
            continue
        owner = db.get_project(info["project_id"])
        if not owner or not can_see(owner, user):
            continue        # never surface another user's machines
        task_id = info.get("task_id", key)
        t = db.get_task(task_id)
        p = db.get_project(info["project_id"])
        agents.append({
            "task_id": task_id, "rival": info.get("rival", ""),
            "kind": info["kind"], "ref": info["ref"],
            "role": info["role"], "model": info["model"], "title": info.get("title", ""),
            "project_id": info["project_id"], "project": p["name"] if p else "",
            "uptime_s": int(_t.time() - info["started_at"]),
            "status": t["status"] if t else "gone",
            "location": info.get("workdir", ""),
        })
    live_ids = {a["task_id"] for a in agents}
    # Agents that already finished still matter — you want their logs afterwards, so
    # list recent ones too instead of letting them disappear when the machine stops.
    finished = []
    scope = ([db.get_project(project_id)] if project_id is not None
             else db.list_projects()[:6])
    for p in [x for x in scope if x]:
        for t in db.list_tasks(p["id"]):
            if t["id"] not in live_ids and t["model"] and t["attempts"] > 0:
                finished.append({
                    "task_id": t["id"], "kind": "finished", "ref": "—",
                    "role": t["role"], "model": t["model"], "title": t["title"],
                    "project_id": p["id"], "project": p["name"],
                    "uptime_s": 0, "status": t["status"], "location": "",
                })
    finished.sort(key=lambda a: -a["task_id"])
    return {"mode": config.LAUNCHER, "namespace": config.K8S_NAMESPACE,
            "max_parallel": config.MAX_CONCURRENT_WORKERS,
            "running": len(agents), "agents": agents, "finished": finished[:15]}


@router.get("/api/tasks/{task_id}/machine-logs")
def machine_logs(task_id: int, request: Request) -> dict:
    """Raw logs from the machine running this task (k8s pod logs when on a cluster)."""
    owned_task(task_id, request)
    if config.LAUNCHER == "k8s":
        try:
            from .launcher import get_launcher
            return {"source": "k8s pod", "logs": get_launcher().pod_logs(task_id)}
        except Exception as e:
            return {"source": "k8s pod", "logs": f"could not read pod logs: {e}"}
    evs = db.list_task_events(task_id)
    lines = [f"[{e['kind']}] {e['payload'][:400]}" for e in evs]
    return {"source": "local process (event stream)", "logs": "\n".join(lines) or "no output yet"}


def _api_key_quota(api_key: str) -> list[dict]:
    """Real remaining capacity, straight from Anthropic's rate-limit headers.

    Only possible with an API key: every response carries
    anthropic-ratelimit-{requests,tokens}-{limit,remaining,reset}. We use the free
    count_tokens endpoint so the probe costs nothing. Subscriptions have no
    equivalent endpoint, so this returns [] for them.
    """
    try:
        import anthropic
    except Exception:
        return []
    out = []
    try:
        client = anthropic.Anthropic(api_key=api_key)
        raw = client.messages.with_raw_response.count_tokens(
            model=config.WORKER_MODEL,
            messages=[{"role": "user", "content": "quota probe"}],
        )
        h = raw.headers
        def num(name):
            v = h.get(name)
            return int(v) if v and v.isdigit() else None
        out.append({
            "scope": "account",
            "requests_limit": num("anthropic-ratelimit-requests-limit"),
            "requests_remaining": num("anthropic-ratelimit-requests-remaining"),
            "requests_reset": h.get("anthropic-ratelimit-requests-reset"),
            "tokens_limit": num("anthropic-ratelimit-tokens-limit"),
            "tokens_remaining": num("anthropic-ratelimit-tokens-remaining"),
            "tokens_reset": h.get("anthropic-ratelimit-tokens-reset"),
        })
    except Exception:
        return []
    return [o for o in out if o.get("requests_limit") or o.get("tokens_limit")]


@router.get("/api/model-health")
def model_health(request: Request) -> dict:
    """Observed health per model, from OUR OWN signals — recent successes vs
    rate-limit/overload hits. Anthropic exposes no remaining-quota API, so this
    reports what we can actually measure, never a fabricated 'percent left'."""
    import time as _t
    user = current_user(request)
    window = _t.time() - 6 * 3600
    stats: dict[str, dict] = {}
    visible = [p for p in db.list_projects() if can_see(p, user)][:12]
    for p in visible:
        for t in db.list_tasks(p["id"]):
            m = t.get("model")
            if not m or t["updated_at"] < window:
                continue
            s = stats.setdefault(m, {"model": m, "runs": 0, "ok": 0, "throttled": 0})
            s["runs"] += 1
            if t["status"] in ("done", "pushed", "review"):
                s["ok"] += 1
            if launcher_looks_rate_limited(t.get("report", "")):
                s["throttled"] += 1
    out = []
    for s in stats.values():
        # Health = how much of this model's recent work got through untroubled.
        throttle_rate = s["throttled"] / max(s["runs"], 1)
        health = max(0, min(100, round((1 - throttle_rate) * 100)))
        s["health"] = health
        s["state"] = "throttled" if throttle_rate >= 0.5 else (
            "strained" if throttle_rate > 0 else "healthy")
        out.append(s)
    from .launcher import cooldown_left
    for s in out:
        s["cooldown_s"] = cooldown_left(s["model"])
        if s["cooldown_s"]:
            s["state"] = "cooling"
    out.sort(key=lambda x: x["model"])

    # Exact remaining capacity IS available on API keys (rate-limit headers).
    u = auth.user_for_token(request.cookies.get("devteam_session"))
    key = (auth.get_settings(u).get("anthropic_api_key") if u else "") or config.ANTHROPIC_API_KEY
    quota = _api_key_quota(key) if key else []

    note = ("Exact remaining requests/tokens read from Anthropic's rate-limit headers."
            if quota else
            "Subscription mode: Anthropic publishes no remaining-quota endpoint, so this "
            "shows observed throttling from this app's own runs plus the real retry-after "
            "window whenever a limit is actually hit.")
    return {"models": out, "window_hours": 6, "quota": quota, "note": note}


def launcher_looks_rate_limited(text: str) -> bool:
    from .launcher import looks_rate_limited
    return looks_rate_limited(text)


@router.get("/api/projects/{project_id}/deploy")
def deploy_status(project_id: int, request: Request) -> dict:
    owned_project(project_id, request)
    return deploy.status(project_id)


@router.post("/api/projects/{project_id}/deploy")
async def deploy_app(project_id: int, request: Request, mode: str = "",
                     workspace: str = "") -> dict:
    """Build and run the project's real app — backend included.

    `workspace` runs an agent's own checkout rather than the merged default
    branch, so a change can be exercised before it is committed.
    """
    owned_project(project_id, request)
    mode = mode or ("k8s" if config.LAUNCHER == "k8s" else "local")
    if mode == "k8s":
        return await deploy.deploy_k8s(project_id)
    return await deploy.deploy_local(project_id, workspace.strip())


@router.post("/api/projects/{project_id}/deploy/rollback")
async def rollback_app(project_id: int, request: Request) -> dict:
    """Put back the last version of this project's app that came up healthy."""
    owned_project(project_id, request)
    r = await deploy.rollback(project_id)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "rollback failed"))
    return r


@router.get("/api/projects/{project_id}/deploy/history")
def deploy_history(project_id: int, request: Request) -> dict:
    """Every deploy attempt of this app, healthy or not — a record of only the
    successes cannot tell you three attempts from one branch died the same way."""
    owned_project(project_id, request)
    return {"history": deploy.history(project_id)}


@router.delete("/api/projects/{project_id}/deploy")
def undeploy_app(project_id: int, request: Request) -> dict:
    owned_project(project_id, request)
    return {"status": deploy.stop(project_id)}


@router.post("/api/tasks/{task_id}/kill")
def kill_agent(task_id: int, request: Request) -> dict:
    """Stop the agent(s) working on one task, right now."""
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, "no such task")
    owned_project(t["project_id"], request)
    notes = launcher.kill_task(task_id, "stopped by the boss from the Agents tab")
    return {"ok": True, "stopped": len(notes), "detail": notes}


# --- plan mode: the round table -------------------------------------------

def owned_table(table_id: int, request: Request) -> dict:
    u = current_user(request)
    t = db.get_table(table_id)
    if not t:
        raise HTTPException(404, "no such round table")
    if not u["is_root"] and t["owner_id"] != u["id"]:
        raise HTTPException(404, "no such round table")
    return t


@router.get("/api/providers")
def list_providers(request: Request) -> dict:
    """Provider/model catalog plus which ones this user actually has keys for."""
    u = current_user(request)
    s = auth.get_settings(u)
    return {"providers": providers.catalog(), "available": providers.available(s)}


@router.get("/api/tables")
def list_tables(request: Request) -> list[dict]:
    u = current_user(request)
    rows = db.list_tables(None if u["is_root"] else u["id"])
    for r in rows:
        r["seat_count"] = len(db.list_seats(r["id"]))
    return rows


@router.post("/api/tables")
def create_table(body: NewTable, request: Request) -> dict:
    u = current_user(request)
    if not body.brief.strip():
        raise HTTPException(400, "describe the idea first")
    if len(body.seats) < roundtable.MIN_SEATS:
        raise HTTPException(400, f"a round table needs at least {roundtable.MIN_SEATS} seats")
    if len(body.seats) > roundtable.MAX_SEATS:
        raise HTTPException(400, f"at most {roundtable.MAX_SEATS} seats")
    have = providers.available(auth.get_settings(u))
    missing = sorted({s.provider for s in body.seats} - set(have))
    if missing:
        labels = ", ".join(providers.PROVIDERS.get(m, {}).get("label", m) for m in missing)
        raise HTTPException(400, f"no credentials for: {labels}. Add a key in Settings.")
    tid = db.create_table(u["id"], body.brief.strip(), body.title.strip(),
                          body.mod_provider, body.mod_model,
                          "diverge" if body.mode == "diverge" else "debate")
    for i, s in enumerate(body.seats):
        db.add_seat(tid, i, s.name.strip() or f"Seat {i+1}", s.provider, s.model,
                    s.persona.strip())
    seats = db.list_seats(tid)
    return {"id": tid, "warning": roundtable.homogeneity_warning(
        seats, "diverge" if body.mode == "diverge" else "debate")}


@router.get("/api/tables/{table_id}")
def get_table(table_id: int, request: Request) -> dict:
    t = dict(owned_table(table_id, request))
    t["seats"] = db.list_seats(table_id)
    t["turns"] = db.list_turns(table_id)
    t["blueprint"] = json.loads(t["blueprint"]) if t["blueprint"] else None
    t["warning"] = roundtable.homogeneity_warning(t["seats"], t.get("mode") or "debate")
    return t


@router.post("/api/tables/{table_id}/run")
async def run_table(table_id: int, request: Request) -> dict:
    """Start the deliberation. Runs in the background; watch the event feed."""
    t = owned_table(table_id, request)
    if t["status"] == "running":
        return {"ok": True, "already": True}

    async def _go():
        try:
            await roundtable.run_table(table_id)
        except Exception as e:
            db.update_table(table_id, status="failed")
            bus.emit(0, None, "roundtable", "table_failed",
                     {"table_id": table_id, "error": str(e)[:400]})

    asyncio.get_event_loop().create_task(_go())
    return {"ok": True}


@router.post("/api/tables/{table_id}/build")
async def build_from_blueprint(table_id: int, body: BuildFromBlueprint,
                               request: Request) -> dict:
    """Turn an agreed blueprint into a real project with the team it proposed."""
    u = current_user(request)
    t = owned_table(table_id, request)
    if not t["blueprint"]:
        raise HTTPException(400, "this table has not produced a blueprint yet")
    bp = json.loads(t["blueprint"])
    if not auth.has_own_ai_credentials(u):
        raise HTTPException(400, "add your own Anthropic key or Claude token in Settings")

    team = [{"role": str(m.get("role", "")).strip().lower().replace(" ", "_"),
             "count": max(1, min(int(m.get("count", 1) or 1), 4)),
             "model": "worker"}
            for m in (bp.get("team") or []) if m.get("role")]
    brief = _brief_from_blueprint(t["brief"], bp)
    repo = body.repo.strip() or config.GITHUB_REPO
    pid = db.create_project(
        body.name.strip() or (t["title"] or "planned project"), brief, repo,
        config.PROJECT_BUDGET_USD, config.MAX_CONCURRENT_WORKERS,
        max_runs=config.MAX_AGENT_RUNS, team=team, autonomy=body.autonomy,
        manager_model=body.manager_model, owner_id=u["id"])
    db.update_table(table_id, project_id=pid)
    if repo:
        try:
            await github_client.ensure_repo(repo)
        except Exception:
            pass
    bus.emit(pid, None, "boss", "built_from_blueprint",
             {"table_id": table_id, "seats": len(db.list_seats(table_id))})
    _manager_tasks[pid] = asyncio.get_event_loop().create_task(manager.run_manager(pid))
    return {"project_id": pid}


def _brief_from_blueprint(original: str, bp: dict) -> str:
    """The manager gets the blueprint as its brief — including the dissent, which
    is exactly the part a plan usually loses."""
    parts = [f"ORIGINAL IDEA:\n{original}", ""]
    if bp.get("restated_problem"):
        parts.append(f"WHAT IS ACTUALLY BEING BUILT:\n{bp['restated_problem']}")
    if bp.get("approach"):
        parts.append(f"\nAGREED APPROACH:\n{bp['approach']}")
    if bp.get("why"):
        parts.append(f"\nWHY THIS APPROACH:\n{bp['why']}")
    if bp.get("milestones"):
        parts.append("\nMILESTONES:\n" + "\n".join(f"- {m}" for m in bp["milestones"]))
    if bp.get("risks"):
        parts.append("\nRISKS:\n" + "\n".join(
            f"- {r.get('risk','')} -> {r.get('mitigation','')}" for r in bp["risks"]))
    if bp.get("strongest_objection"):
        parts.append(f"\nTHE STRONGEST OBJECTION RAISED IN PLANNING (do not lose "
                     f"sight of it):\n{bp['strongest_objection']}")
    if bp.get("open_questions"):
        parts.append("\nOPEN QUESTIONS — ask the boss if they block you:\n" +
                     "\n".join(f"- {q}" for q in bp["open_questions"]))
    return "\n".join(parts)


@router.get("/api/projects/{project_id}/blockers")
def get_blockers(project_id: int, request: Request) -> dict:
    """Everything currently standing in the way of this project."""
    owned_project(project_id, request)
    return blockers.summary(project_id)


# --- the platform working on itself (root only) ---

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


@router.get("/api/self")
def self_status(request: Request) -> dict:
    u = _root(request)
    pid = selfops.ensure_project(u["id"])
    st = selfops.can_redeploy()
    tasks = db.list_tasks(pid)
    repo = st["repo"]
    return {"project_id": pid, "repo": repo,
            "repo_missing": ("" if repo else
                             "SELF_REPO is not set, and a container cannot discover "
                             "its own repository — the image carries no .git. Set it "
                             "in the secret or self-repair has nothing to clone."),
            "head": st["head"],
            "can_redeploy": st["ok"], "blocked_reasons": st["reasons"],
            "last_deploy": st["last_deploy"],
            "open_issues": [
                {"seq": t["seq"], "id": t["id"], "title": t["title"], "status": t["status"],
                 "pr": t["pr_number"], "issue": t["issue_number"]}
                for t in tasks if t["status"] != "done"],
            "shipped": [
                {"seq": t["seq"], "title": t["title"], "pr": t["pr_number"]}
                for t in tasks if t["status"] == "done"][-10:]}


class SelfIssue(BaseModel):
    title: str
    body: str
    severity: str = "bug"
    sprints: int = 1


class RoughIssue(BaseModel):
    rough: str


@router.post("/api/self/refine")
async def self_refine(payload: RoughIssue, request: Request) -> dict:
    """Turn a one-line complaint into a ticket worth handing to an agent.

    A vague ticket is the cheapest way to waste a sprint — the team builds the
    wrong thing, competently. This is a draft for the human to edit, never
    submitted on their behalf.
    """
    u = _root(request)
    rough = payload.rough.strip()
    if len(rough) < 8:
        raise HTTPException(400, "say a little more about what's wrong")
    return await selfops.refine_issue(rough, auth.get_settings(u))


@router.post("/api/self/triage")
async def self_triage(payload: RoughIssue, request: Request) -> dict:
    """How much independence this issue would get, before any work starts.

    Shown before you file, so the answer is never a surprise afterwards.
    """
    u = _root(request)
    t = await triage.classify(payload.rough[:200], payload.rough,
                              auth.get_settings(u))
    return {**t, "policy": triage.policy(t["tier"])}


@router.post("/api/self/issue")
async def self_issue(payload: SelfIssue, request: Request) -> dict:
    u = _root(request)
    if not payload.title.strip() or not payload.body.strip():
        raise HTTPException(400, "describe the issue and give it a title")
    pid = selfops.ensure_project(u["id"])
    # Self-repair is meant to run start-to-finish on its own: fix, verify, ship.
    # Asking the operator to approve each step of a fix they already asked for is
    # the interruption this feature exists to remove.
    # Decide the independence BEFORE any work starts, so "it merged something it
    # should have asked about" cannot happen after the fact.
    t = await triage.classify(payload.title, payload.body, auth.get_settings(u))
    pol = triage.policy(t["tier"])
    if not pol["may_work"]:
        raise HTTPException(400, f"{pol['note']} ({'; '.join(t['why'])})")
    sprints = max(1, min(10, payload.sprints or 1))
    db.set_sprints(pid, sprints)
    # Only a routine fix runs unsupervised to completion. Anything substantial
    # does the work and stops at a pull request.
    db.set_project_autonomy(pid, "autonomous" if pol["may_merge"] else "supervised")
    if sprints > 1:
        db.set_max_runs(pid, min(400, config.MAX_AGENT_RUNS * sprints))
    res = await selfops.file_issue(pid, payload.title.strip(), payload.body.strip(),
                                   payload.severity)
    res["triage"] = {**t, "policy": pol}
    # The manager plans the fix. If one is already running it picks the issue up as
    # a directive on its next decision point, so we must not start a second session.
    if pid not in _manager_tasks or _manager_tasks[pid].done():
        _manager_tasks[pid] = asyncio.get_event_loop().create_task(manager.run_manager(pid))
        res["manager"] = "started"
    else:
        res["manager"] = "already running — the issue was handed to it"
    return {"project_id": pid, **res}


class EnvBuild(BaseModel):
    source: str
    note: str = ""


class EnvDeploy(BaseModel):
    tag: str
    env: str


class EnvTag(BaseModel):
    tag: str


@router.get("/api/self/envs")
def envs_overview(request: Request) -> dict:
    _root(request)
    return envs.overview()


@router.post("/api/self/envs/build")
def envs_build(body: EnvBuild, request: Request) -> dict:
    _root(request)
    r = envs.build(body.source.strip(), body.note.strip())
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "build failed"))
    return r


@router.post("/api/self/envs/deploy")
def envs_deploy(body: EnvDeploy, request: Request) -> dict:
    _root(request)
    r = envs.deploy_preview(body.tag.strip(), body.env.strip())
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "deploy failed"))
    return r


@router.post("/api/self/envs/promote")
def envs_promote(body: EnvTag, request: Request) -> dict:
    """Point production at an image that has already been previewed."""
    _root(request)
    r = envs.promote(body.tag.strip())
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "promotion failed"))
    bus.emit(0, None, "system", "promoted", {"tag": r["tag"], "from": r.get("previous")})
    return r


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
    return await notify.report_error(
        "dashboard error", f"{body.message}\n{body.stack[:1200]}",
        {"page": body.url[:200], "user": (u or {}).get("username", "anonymous")})


@router.get("/api/notify/status")
def notify_status(request: Request) -> dict:
    """So "no notifications" is provably "nothing broke", not "it is broken"."""
    _root(request)
    return notify.status()


@router.get("/api/self/healing")
def self_healing(request: Request) -> dict:
    """What the platform has fixed, tried and failed to fix, on its own.

    Exists so "you were not asked about it" does not mean "you cannot find out
    about it" — the whole bargain of routine autonomy is that it stays visible.
    """
    u = _root(request)
    pid = selfops.ensure_project(u["id"])
    kinds = {"self_healed": "healed", "canary_failed": "rejected a bad build",
             "auto_update": "took a new version", "self_update": "took a new version",
             "notified": "raised an issue", "digest_filed": "filed a sprint digest",
             "rolled_back": "rolled back"}
    items = []
    for e in reversed(db.list_events(pid, limit=300) + db.list_events(0, limit=200)):
        if e["kind"] not in kinds:
            continue
        try:
            payload = db.json.loads(e["payload"])
        except Exception:
            payload = {"note": str(e["payload"])[:200]}
        items.append({"kind": e["kind"], "what": kinds[e["kind"]],
                      "at": e["ts"], "detail": payload})
        if len(items) >= 40:
            break
    return {"items": items, "project_id": pid}


@router.get("/api/self/instance")
def self_instance(request: Request) -> dict:
    """What this instance is and whether it can ship a new version of itself."""
    _root(request)
    return cloud.describe()


@router.get("/api/self/images")
async def self_images(request: Request) -> dict:
    """What CI has published, and whether a newer one is waiting."""
    _root(request)
    imgs = await cloud.available_images()
    return {"images": imgs, "candidate": await cloud.newer_than_running(),
            "auto_update": cloud.AUTO_UPDATE, "busy": cloud.busy()}


class SelfImage(BaseModel):
    image: str
    force: bool = False


@router.post("/api/self/update")
def self_update(body: SelfImage, request: Request) -> dict:
    """Point our own Deployment at a new image. This pod is then replaced."""
    _root(request)
    r = cloud.self_update(body.image.strip(), body.force)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "self-update failed"))
    bus.emit(0, None, "system", "self_update", {"from": r.get("from"), "to": r["to"]})
    return r


class StagingReq(BaseModel):
    image: str


@router.post("/api/self/staging")
def staging_deploy(body: StagingReq, request: Request) -> dict:
    """Run an image in staging: real credentials, its own identity."""
    _root(request)
    r = cloud.staging_deploy(body.image.strip())
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "staging deploy failed"))
    bus.emit(0, None, "system", "staging_deployed", {"image": r["image"]})
    return r


@router.post("/api/self/staging/verify")
def staging_verify(body: StagingReq, request: Request) -> dict:
    """Run the test suite inside staging — the check a boot canary cannot make."""
    _root(request)
    r = cloud.staging_verify(body.image.strip())
    bus.emit(0, None, "system",
             "staging_passed" if r.get("ok") else "staging_failed",
             {"image": body.image, "output": (r.get("output") or "")[:600]})
    return r


@router.post("/api/self/staging/promote-check")
def staging_promote_check(body: StagingReq, request: Request) -> dict:
    """Deploy an image to staging and run its own tests against it, in one step.

    One call because the two halves are only meaningful together: a deployment
    nobody tested proves nothing, and a test run against a different build proves
    less than nothing.
    """
    _root(request)
    dep = cloud.staging_deploy(body.image.strip())
    if not dep.get("ok"):
        raise HTTPException(400, f"staging would not come up: {dep.get('error')}")
    ver = cloud.staging_verify(body.image.strip())
    bus.emit(0, None, "system",
             "staging_passed" if ver.get("ok") else "staging_failed",
             {"image": body.image, "output": (ver.get("output") or "")[:600]})
    return {"deployed": dep, "verified": ver,
            "may_promote": bool(ver.get("ok")),
            "note": ("production can now adopt this image" if ver.get("ok")
                     else "production will refuse this image while the gate is on")}


@router.delete("/api/self/staging")
def staging_teardown(request: Request) -> dict:
    _root(request)
    return cloud.staging_teardown()


@router.post("/api/self/update/rollback")
def self_update_rollback(request: Request) -> dict:
    _root(request)
    r = cloud.rollback()
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "rollback failed"))
    return r


@router.post("/api/self/envs/rollback")
def envs_rollback(request: Request) -> dict:
    """k8s keeps the previous ReplicaSet, so this needs no artifact and no guesswork."""
    _root(request)
    r = envs.rollback()
    if not r.get("ok"):
        raise HTTPException(400, r.get("detail") or r.get("error", "rollback failed"))
    bus.emit(0, None, "system", "rolled_back", {"detail": r.get("detail", "")})
    return r


@router.delete("/api/self/envs/{env}")
def envs_destroy(env: str, request: Request) -> dict:
    _root(request)
    return envs.destroy(env)


class SandboxReq(BaseModel):
    ref: str          # a source id from sandbox.sources(): live | workspace:… | ref:…


@router.get("/api/self/sandbox")
def sandbox_status(request: Request) -> dict:
    _root(request)
    return {**sandbox.status(), "sources": sandbox.sources()}


@router.post("/api/self/sandbox")
def sandbox_start(body: SandboxReq, request: Request) -> dict:
    """Boot the candidate build beside the live one so it can be clicked through.

    A diff says the code is plausible; only running it says the app still works.
    """
    _root(request)
    res = sandbox.start(body.ref.strip())
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "the sandbox would not start"))
    bus.emit(0, None, "system", "sandbox_started",
             {"ref": res["ref"], "commit": res["commit"], "port": res["port"]})
    return res


@router.delete("/api/self/sandbox")
def sandbox_stop(request: Request) -> dict:
    _root(request)
    return sandbox.stop()


@router.post("/api/self/redeploy")
async def self_redeploy(request: Request, force: bool = False) -> dict:
    _root(request)
    res = selfops.redeploy(force=force)
    if res.get("ok"):
        bus.emit(0, None, "system", "self_redeploy",
                 {"from": res["from"]["commit"], "to": res["to"]["commit"]})
        # Give the response time to reach the browser before the process is replaced.
        asyncio.get_event_loop().call_later(1.5, selfops.restart_process)
    return res


@router.post("/api/self/rollback")
async def self_rollback(request: Request) -> dict:
    _root(request)
    res = selfops.rollback()
    if res.get("ok"):
        asyncio.get_event_loop().call_later(1.5, selfops.restart_process)
    return res


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
        js = config.DASHBOARD_DIR / "app.js"
        from .main import STARTED_AT
        stale_ui = js.stat().st_mtime > STARTED_AT
    except Exception:
        pass
    return {"ok": True, "launcher": config.LAUNCHER, "auth": config.auth_mode(),
            "github": bool(config.GITHUB_TOKEN) or config.DEMO_MODE,
            "demo": config.DEMO_MODE, "stale_ui": stale_ui,
            "weak_password": auth.password_is_weak(auth.ROOT_PASSWORD)}


@router.post("/api/suggest-team")
async def suggest_team(body: BriefOnly, request: Request) -> dict:
    """Recruiting: propose a starting team from the brief for the boss to tweak."""
    u = current_user(request)   # spends tokens — never anonymous
    # Their own credentials, on their own provider. Without this the planner had
    # nothing to authenticate with and every user silently got the keyword heuristic.
    return {"team": await planner.suggest_team(body.brief, auth.get_settings(u)),
            "known_roles": [r["name"] for r in config.load_roles()]}


@router.post("/api/projects")
async def create_project(body: NewProject, request: Request) -> dict:
    owner = auth.user_for_token(request.cookies.get("devteam_session"))
    if not owner:
        raise HTTPException(401, "sign in to start a project")
    # Every user brings their own AI credentials — no borrowing the operator's.
    if not auth.has_own_ai_credentials(owner):
        raise HTTPException(400, "Add your own Anthropic API key or Claude subscription "
                                 "token in Settings (⚙) before starting a project — "
                                 "agents run on your account, not the server's.")
    if (not config.DEMO_MODE and not owner["is_root"]
            and not auth.get_settings(owner).get("github_token")):
        raise HTTPException(400, "Add your own GitHub token in Settings (⚙) — "
                                 "your team needs a repo it can push to.")
    # A sandbox has no credentials by design; gating on them would make the one
    # build you most want to click through the only one you cannot use.
    if not config.AUTH_CONFIGURED and not config.DEMO_MODE:
        raise HTTPException(400, "Set ANTHROPIC_API_KEY (API billing) or "
                                 "CLAUDE_CODE_OAUTH_TOKEN (Pro/Max subscription) on the conductor")
    if config.LAUNCHER == "k8s" and config.CLI_LOGIN:
        raise HTTPException(400, "k8s workers cannot inherit local CLI credentials — "
                                 "set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) "
                                 "or ANTHROPIC_API_KEY")
    repo = body.repo or config.GITHUB_REPO
    team = [m.model_dump() for m in body.team]
    autonomy = "autonomous" if body.autonomy == "autonomous" else "supervised"
    sprints = max(1, min(20, body.sprints or 1))
    # A run cap sized for one pass starves a multi-sprint run: it stops somewhere
    # inside sprint 2 with the product half-built, which looks like the team giving
    # up rather than hitting a guard rail. Scale it with the cycles asked for, but
    # only when the boss left the cap at its default — an explicit number is a
    # decision, not an oversight.
    runs = body.max_runs or config.MAX_AGENT_RUNS
    scaled = runs <= config.MAX_AGENT_RUNS and sprints > 1
    if scaled:
        runs = min(400, runs * sprints)
    project_id = db.create_project(
        body.name, body.brief, repo,
        body.budget_usd or config.PROJECT_BUDGET_USD,
        body.max_workers or config.MAX_CONCURRENT_WORKERS,
        runs,
        team=team, autonomy=autonomy,
        manager_model=body.manager_model.strip(),
        manager_persona=body.manager_persona.strip(),
        owner_id=(owner["id"] if owner else 0),
        sprints=sprints,
    )
    bus.emit(project_id, None, "system", "project_created", {"name": body.name})
    if scaled:
        bus.emit(project_id, None, "system", "run_cap_scaled",
                 {"runs": runs, "sprints": sprints})
    # Make sure the target repo exists before the team tries to clone it. This is
    # why manual repo creation is unnecessary — the token creates a private repo.
    if repo and github_client.enabled(repo):
        ok, note = await github_client.ensure_repo(repo)
        bus.emit(project_id, None, "system", "repo_ready" if ok else "repo_error", note)
    _manager_tasks[project_id] = asyncio.get_event_loop().create_task(manager.run_manager(project_id))
    return {"id": project_id}


@router.get("/api/projects")
def list_projects(request: Request) -> list[dict]:
    u = current_user(request)
    # The platform's own row is not one of "your projects" — it has its own way in
    # (the Improve tile) and listing it invited people to drive it like a normal one.
    projects = [p for p in db.list_projects() if can_see(p, u) and not p["is_self"]]
    for p in projects:
        p["task_count"] = len(db.list_tasks(p["id"]))
    return projects


@router.get("/api/projects/{project_id}")
def get_project(project_id: int, request: Request) -> dict:
    owned_project(project_id, request)
    if scheduler.reconcile_status(project_id):   # keeps 'done' honest
        project = db.get_project(project_id)
        if project and not (_manager_tasks.get(project_id) and not _manager_tasks[project_id].done()):
            _manager_tasks[project_id] = asyncio.get_event_loop().create_task(
                manager.run_manager(project_id))
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404, "no such project")
    tasks = db.list_tasks(project_id)
    for t in tasks:
        if t.get("compete"):
            t["rivals"] = db.list_contenders(t["id"])
    project["tasks"] = tasks
    return project


@router.post("/api/projects/{project_id}/restart")
async def restart_project(project_id: int, request: Request) -> dict:
    """Re-run the lead session on a failed/review/cancelled project (tasks are kept)."""
    project = owned_project(project_id, request)
    # 'hold' belongs here too. A project on hold is waiting for an answer, and if
    # its manager died while waiting, nothing is listening for one — answering
    # resumes a session that no longer exists. Without this the project is stuck
    # forever: not restartable, and not advanceable.
    if project["status"] not in ("failed", "review", "cancelled", "hold"):
        raise HTTPException(400, f"cannot restart a project in status '{project['status']}'")
    existing = _manager_tasks.get(project_id)
    if existing and not existing.done():
        raise HTTPException(400, "manager session is still running")
    db.set_project_status(project_id, "planning")
    bus.emit(project_id, None, "system", "project_restarted", {})
    _manager_tasks[project_id] = asyncio.get_event_loop().create_task(manager.run_manager(project_id))
    return {"ok": True}


@router.post("/api/projects/{project_id}/cancel")
async def cancel_project(project_id: int, request: Request) -> dict:
    project = owned_project(project_id, request)
    db.set_project_status(project_id, "cancelled")
    db.abandon_questions(project_id)
    scheduler.stop(project_id)
    t = _manager_tasks.get(project_id)
    if t and not t.done():
        t.cancel()  # aborts the manager session immediately instead of at its next wait
    # Stopping the scheduler only stops NEW work. The workers already running are
    # separate processes/Jobs: without this they keep going, keep spending tokens,
    # and their tasks stay 'running' forever because the only thing that clears
    # that status is a report from a worker nobody is listening to any more.
    killed = launcher.kill_project(project_id, "project was cancelled by the boss")
    # Close this project's still-open GitHub issues so they don't linger as orphans.
    repo = project["repo"]
    if github_client.enabled(repo):
        for task in db.list_tasks(project_id):
            if task["issue_number"] and task["status"] not in ("done",):
                try:
                    await github_client.close_issue(repo, task["issue_number"])
                except Exception:
                    pass
    bus.emit(project_id, None, "system", "project_cancelled", {"agents_stopped": len(killed)})
    return {"ok": True, "agents_stopped": len(killed), "detail": killed}


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: int, request: Request) -> dict:
    """Delete a project and everything under it. Irreversible."""
    p = owned_project(project_id, request)
    if p["is_self"]:
        raise HTTPException(400, "the platform's own project cannot be deleted")
    # Never orphan live agents: stop them before the rows they belong to vanish.
    killed = launcher.kill_project(project_id, "project was deleted by the boss")
    scheduler.stop(project_id)
    t = _manager_tasks.pop(project_id, None)
    if t and not t.done():
        t.cancel()
    deploy.stop(project_id)
    counts = db.delete_project(project_id)
    return {"ok": True, "agents_stopped": len(killed), **counts}


@router.delete("/api/tables/{table_id}")
def delete_table(table_id: int, request: Request) -> dict:
    owned_table(table_id, request)
    db.delete_table(table_id)
    return {"ok": True}


@router.post("/api/projects/{project_id}/autonomy")
def set_autonomy(project_id: int, body: Autonomy, request: Request) -> dict:
    """Change how much rope the manager has, mid-run.

    The manager's behavioural gates read this live, so a running session changes
    behaviour immediately. Its *system prompt* was fixed when the session started,
    though, so we also tell it in-band — otherwise its instructions would contradict
    how the platform is now treating it.
    """
    p = owned_project(project_id, request)
    mode = "autonomous" if body.autonomy == "autonomous" else "supervised"
    if p["autonomy"] == mode:
        return {"ok": True, "autonomy": mode, "changed": False}
    db.set_project_autonomy(project_id, mode)
    if mode == "autonomous":
        db.add_directive(project_id,
            "AUTONOMY CHANGED: you now have FULL AUTONOMY. Stop asking me to approve "
            "things — decide yourself, including merges and finishing. Only interrupt "
            "me if you are genuinely, unrecoverably blocked. If a question of yours is "
            "pending, answer it yourself with your best judgement and note the "
            "assumption you made.")
        db.abandon_questions(project_id)      # don't leave it blocked on a question
        if p["status"] == "hold":
            db.set_project_status(project_id, "running")
    else:
        db.add_directive(project_id,
            "AUTONOMY CHANGED: you are now SUPERVISED. Check with me before merging "
            "substantial work and before finishing, and whenever there is a real "
            "product or scope decision — give me 2-4 concrete options. Do not ask "
            "about routine mechanics.")
    bus.emit(project_id, None, "boss", "autonomy_changed", {"autonomy": mode})
    return {"ok": True, "autonomy": mode, "changed": True}


@router.get("/api/projects/{project_id}/events")
def get_events(project_id: int, request: Request, after: int = 0) -> list[dict]:
    owned_project(project_id, request)
    return db.list_events(project_id, after_id=after)


@router.get("/api/projects/{project_id}/files")
async def project_files(project_id: int, request: Request) -> dict:
    """What the team actually produced, as files you can open.

    The Artifacts tab listed pull requests and task rows — a record of ACTIVITY.
    The thing a person wants is the OUTPUT: the code, and the documents.
    """
    p = owned_project(project_id, request)
    if not p["repo"] or not github_client.enabled(p["repo"]):
        return {"files": [], "reason": "no GitHub repo attached to this project"}
    try:
        files = await github_client.list_tree(p["repo"])
    except Exception as e:
        return {"files": [], "reason": str(e)[:200]}
    # Group by what it IS, because "a README" and "a source file" are different
    # kinds of thing to a reader even though git treats them identically.
    def kind(path: str) -> str:
        low = path.lower()
        if low.endswith((".md", ".txt", ".rst")):
            return "doc"
        if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")):
            return "image"
        if "test" in low:
            return "test"
        return "code"
    return {"repo": p["repo"],
            "files": [{**f, "kind": kind(f["path"])} for f in files]}


@router.get("/api/projects/{project_id}/file")
async def project_file(project_id: int, path: str, request: Request) -> dict:
    p = owned_project(project_id, request)
    if not p["repo"] or ".." in path:
        raise HTTPException(400, "no repo, or a path that tries to escape it")
    try:
        return {"path": path, "text": await github_client.read_file(p["repo"], path)}
    except Exception as e:
        raise HTTPException(400, str(e)[:200])


@router.get("/api/projects/{project_id}/artifacts")
async def get_artifacts(project_id: int, request: Request) -> dict:
    """Everything the project produced: repo, branches, PRs, and the public site URL."""
    project = owned_project(project_id, request)
    repo = project["repo"]
    tasks = db.list_tasks(project_id)
    # A plain-language record of what the team actually did, per task.
    work = [{"id": t["id"], "role": t["role"], "title": t["title"], "status": t["status"],
             "pr": t["pr_number"], "attempts": t["attempts"], "model": t["model"],
             "outcome": (t["report"] or "").strip()[:1200]}
            for t in tasks]
    out = {
        "repo": repo, "repo_url": f"https://github.com/{repo}" if repo else None,
        "project": project["name"], "brief": project["brief"],
        "status": project["status"], "conclusion": project["summary"],
        "preview_url": f"/preview/{project_id}/" if preview.preview_root(project_id) else None,
        "preview_synced": preview.synced_at(project_id),
        "work": work, "prs": [], "branches": [],
    }
    if github_client.enabled(repo):
        try:
            out["prs"] = await github_client.list_prs(repo)
            out["branches"] = await github_client.list_branches(repo)
        except Exception as e:
            out["error"] = str(e)[:200]
    return out


@router.post("/api/projects/{project_id}/preview")
async def build_preview(project_id: int, request: Request) -> dict:
    """Sync the project's static site into our own preview host → viewable at /preview/{id}/."""
    owned_project(project_id, request)
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
        # SPA fallback — but NOT for API-shaped or file-extension paths. Returning
        # index.html for /api/... makes the app's own backend calls "succeed" with
        # HTML, which the frontend then fails to parse. Name the real problem.
        looks_api = path.startswith("api/") or "/api/" in path
        has_ext = "." in path.rsplit("/", 1)[-1]
        if looks_api or has_ext:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=404, content={
                "error": "This is the static preview — it serves files only and "
                         "cannot run this app's backend. Use Artifacts -> Full "
                         "deployment to run the real app."})
        target = root / "index.html"
    # Sandbox the previewed app: it may run its own JS, but it can't call our API
    # (same-origin fetch to /api is blocked) and can't be embedded elsewhere.
    csp = "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: https:; " \
          "connect-src https: http:; frame-ancestors 'self'"
    return FileResponse(target, headers={"Content-Security-Policy": csp,
                                         "X-Devteam-Preview": str(project_id)})


@router.get("/api/tasks/{task_id}/events")
def get_task_events(task_id: int, request: Request) -> list[dict]:
    """Full start-to-end transcript for one task's agent (messages + tool calls)."""
    owned_task(task_id, request)
    return db.list_task_events(task_id)


# --- boss controls -----------------------------------------------------------

@router.post("/api/projects/{project_id}/directive")
def send_directive(project_id: int, body: Directive, request: Request) -> dict:
    """Boss -> manager message. Delivered at the manager's next decision point."""
    owned_project(project_id, request)
    db.add_directive(project_id, body.text)
    bus.emit(project_id, None, "boss", "directive", body.text)
    return {"ok": True}


@router.get("/api/projects/{project_id}/question")
def get_pending_question(project_id: int, request: Request) -> dict:
    """The manager's open question for the boss, if any."""
    owned_project(project_id, request)
    q = db.pending_question(project_id)
    if not q:
        return {"question": None}
    # NOTE: the key is "question" (not "text") — the dashboard keys off it to raise
    # the approval modal. Keep this name stable.
    return {"id": q["id"], "question": q["text"], "options": db.json.loads(q["options"])}


@router.post("/api/questions/{qid}/answer")
def answer(qid: int, body: Answer, request: Request) -> dict:
    q = db.get_question(qid)
    if not q:
        raise HTTPException(404, "no such question")
    owned_project(q["project_id"], request)     # only the boss answers their manager
    db.answer_question(qid, body.answer)
    bus.emit(q["project_id"], None, "boss", "answered",
             {"question": q["text"], "answer": body.answer})
    return {"ok": True}


@router.post("/api/projects/{project_id}/budget")
def set_budget(project_id: int, body: Budget, request: Request) -> dict:
    owned_project(project_id, request)
    db.set_project_budget(project_id, body.budget_usd)
    bus.emit(project_id, None, "boss", "budget_changed", {"budget_usd": body.budget_usd})
    return {"ok": True}


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


# --- internal endpoints used by workers ---

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


@router.post("/internal/report")
def worker_report(body: WorkerReport, x_worker_token: str | None = Header(None)) -> dict:
    _check_token(x_worker_token)
    _owns_task(body.project_id, body.task_id)
    status = "pushed" if body.status == "pushed" else "failed"
    task = db.get_task(body.task_id)

    # A rival attempt reports into its own row; the task only advances once every
    # rival is in, and then it goes to the manager to judge — not straight to a PR.
    if body.contender_id:
        db.update_contender(body.contender_id, status=status, report=body.report[:12000])
        db.add_project_cost(body.project_id, body.cost_usd)
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
    from .launcher import looks_rate_limited, note_rate_limit, cooldown_left
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
                p = db.get_project(pid) if pid else None
                visible[pid] = bool(p and can_see(p, user))
            if visible[pid]:
                await ws.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(q)
