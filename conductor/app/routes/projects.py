"""Projects, whole: creation through cancellation, and everything a boss reads
back out — events, files, artifacts, sprints, previews.

The lifecycle half and the read surface live together because they share one
spine: a project row, the manager session in _manager_tasks that drives it,
and the ownership gate on every request. Creating a project starts a manager;
most of what follows is watching what that manager and its team produced.
"""

import asyncio

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .. import (ambition, artifacts, auth, bus, config, db, deliverables, deploy,
                feedback, github_client, launcher, logs, manager, planner,
                preview, process, projgraph, providers, scheduler, team)
from .base import (_manager_tasks, can_see, current_user, owned_project,
                   owned_table, owned_task, router)


class TeamMember(BaseModel):
    role: str
    count: int = 1
    model: str = "worker"
    # Per-role choices. Both optional and both default to what the platform would
    # have done anyway, so an existing client that sends neither is unaffected.
    provider: str = "anthropic"
    persona: str = ""


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
    staff_team: str = ""        # "" = the manager hires per task | "new" | "<world>:<room>"
    # agile | waterfall. Unset means agile, which is what the platform was already
    # doing badly — planning in one pass with dependencies wired by role is
    # waterfall in everything but name, and it was never a choice anyone made.
    process: str = ""
    # draft | standard | exacting. The one input the boss never had: whether time
    # or quality is the constraint on this particular piece of work.
    ambition: str = ""


class BriefOnly(BaseModel):
    brief: str


class Autonomy(BaseModel):
    autonomy: str          # supervised | autonomous


class SprintNotes(BaseModel):
    # Empty provider means "write them from the record", which needs no key and
    # is the only mode that works on an instance with no credentials at all.
    provider: str = ""
    model: str = ""


@router.post("/api/suggest-team")
async def suggest_team(body: BriefOnly, request: Request) -> dict:
    """Recruiting: propose a starting team from the brief for the boss to tweak."""
    u = current_user(request)   # spends tokens — never anonymous
    # Their own credentials, on their own provider. Without this the planner had
    # nothing to authenticate with and every user silently got the keyword heuristic.
    return {"team": await planner.suggest_team(body.brief, auth.get_settings(u)),
            "known_roles": [r["name"] for r in config.load_roles()]}


async def _staff_from_team(project_id: int, body, owner) -> None:
    """Attach a Studio team to a project, building one from the brief if asked.

    Naming a team is not a new requirement — with nothing chosen the manager hires per task
    exactly as it always has. It is the option to arrange the people BEFORE the work, and to
    open them from the project afterwards, which is the whole reason the Studio and the
    pipeline should not be two unrelated products.
    """
    choice = (getattr(body, "staff_team", "") or "").strip()
    if not choice:
        return
    from .. import lifeworld_client
    try:
        if choice != "new":
            wid, _, rid = choice.partition(":")
            db.set_team(project_id, int(wid), int(rid or 0))
            return
        # "new": one team, named after the project, staffed from the roster the wizard
        # already resolved — the same people, arranged where you can see and rewire them.
        # Two calls to the lifeworld service: a world, then the manifest that fills it.
        wid = await lifeworld_client.create_world(owner, f"{body.name} team")
        names = [str(m.role or "").strip() or f"agent {i+1}"
                 for i, m in enumerate(getattr(body, "team", []) or [])][:8]
        if not names:
            names = ["Lead", "Builder", "Reviewer"]
        out = await lifeworld_client.apply_manifest(owner, wid, {
            "name": body.name[:60] or "team",
            "agents": [{"name": n, "brief": f"{n} on {body.name[:40]}"} for n in names],
            "edges": [[names[i], names[(i + 1) % len(names)]] for i in range(len(names))]
                     if len(names) > 1 else [],
            "rules": body.brief[:400], "manager": {"model": "", "budget": 2},
            "protocol": {"preset": "evidence-2026"}})
        rid = int((out.get("room") or {}).get("id") or 0)
        db.set_team(project_id, wid, rid)
        bus.emit(project_id, None, "system", "team_built",
                 {"world": wid, "room": rid, "agents": len(names)})
    except Exception as e:
        # A project must never fail to start because the Studio hiccuped.
        logs.warn("lifecycle", "team_attach_failed", str(e)[:200], project=project_id)


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
    # Only when they ASKED for a repo. GitHub is how you get pull requests, not
    # how you get software: this gate used to refuse every project from a user
    # with no GitHub token, including the ones that neither want nor need a
    # remote, and the work of a no-repo project is preserved and downloadable
    # without one.
    if (body.repo.strip() and not config.DEMO_MODE and not owner["is_root"]
            and not auth.get_settings(owner).get("github_token")):
        raise HTTPException(400, "Add your own GitHub token in Settings (⚙) to use a "
                                 "repo — or leave the repo blank and your team's work "
                                 "is kept here, downloadable from Artifacts.")
    # A sandbox has no credentials by design; gating on them would make the one
    # build you most want to click through the only one you cannot use.
    if not config.AUTH_CONFIGURED and not config.DEMO_MODE:
        raise HTTPException(400, "Set ANTHROPIC_API_KEY (API billing) or "
                                 "CLAUDE_CODE_OAUTH_TOKEN (Pro/Max subscription) on the conductor")
    if config.LAUNCHER == "k8s" and config.CLI_LOGIN:
        raise HTTPException(400, "k8s workers cannot inherit local CLI credentials — "
                                 "set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) "
                                 "or ANTHROPIC_API_KEY")
    # The operator's default repo is a default for the operator, not for a guest
    # who left the field blank — that was the other half of the cascade, a user's
    # project pointed at the operator's repository.
    repo = body.repo or (config.GITHUB_REPO if owner and owner["is_root"] else "")
    roster = [m.model_dump() for m in body.team]
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
        team=roster, autonomy=autonomy,
        manager_model=body.manager_model.strip(),
        manager_persona=body.manager_persona.strip(),
        owner_id=(owner["id"] if owner else 0),
        sprints=sprints,
    )
    db._execute("UPDATE projects SET process=?, ambition=? WHERE id=?",
                (process.normalise(body.process),
                 ambition.normalise(body.ambition), project_id))
    bus.emit(project_id, None, "system", "project_created", {"name": body.name})
    # THE ATLAS IS THE PROJECT'S FIRST SCREEN, so it must have something to show
    # before the manager has planned anything: the aim (this brief) and the
    # deliverable, with one honest arrow between them. Every task the manager lands
    # afterwards joins the same graph and animates in, because `_create_batch`
    # re-syncs and announces. Guarded: a project must never fail to start because
    # the map's store is asleep — `sync` returns 0 and the screen heals on its own
    # first read.
    try:
        projgraph.sync(project_id, announce=True)
    except Exception as e:
        logs.warn("lifecycle", "project_graph_seed_failed", str(e)[:200],
                  project=project_id)
    await _staff_from_team(project_id, body, owner)
    team.hire(project_id, roster)
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
    # Notes written while the project was stopped were held rather than queued,
    # because a directive nobody consumes is indistinguishable from one ignored.
    # This is the moment they have somewhere to go, and it must happen before the
    # manager session starts so its first decision point already sees them.
    held = feedback.deliver(project_id)
    _manager_tasks[project_id] = asyncio.get_event_loop().create_task(manager.run_manager(project_id))
    return {"ok": True, "notes_delivered": len(held)}


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


class ManagerModel(BaseModel):
    model: str


@router.post("/api/projects/{project_id}/manager-model")
def set_manager_model(project_id: int, body: ManagerModel, request: Request) -> dict:
    """Change which model runs the manager, mid-project.

    It was fixed at creation, which is the worst-informed moment to choose it:
    nobody knows yet whether this project needs a careful planner or a cheap one.
    A plan that keeps coming back thin is the signal to move up, and that signal
    only exists after some planning has happened.

    Unlike autonomy, this cannot take effect on the running session — a model is
    bound when the session starts and there is no way to swap it underneath. So
    the change is recorded and applied on the manager's next start, and the reply
    says so plainly rather than implying something happened that did not.
    """
    owned_project(project_id, request)
    model = (body.model or "").strip()
    known = {m["id"] for p in providers.PROVIDERS.values() for m in p["models"]}
    if model and model not in known:
        raise HTTPException(400, f"unknown model '{model}'")
    db._execute("UPDATE projects SET manager_model=? WHERE id=?", (model, project_id))
    running = project_id in _manager_tasks and not _manager_tasks[project_id].done()
    bus.emit(project_id, None, "boss", "manager_model_changed",
             {"model": model or "server default"})
    return {
        "ok": True, "model": model,
        "applies": "when the manager next starts" if running else "immediately",
        "restart_needed": running,
    }


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

    With a remote, that output is the repository. Without one it is the preserved
    deliverable — which is a real answer, and the reason this used to reply "no
    GitHub repo attached" to a project that had built a working application.
    """
    p = owned_project(project_id, request)
    if p["repo"] and github_client.enabled(p["repo"]):
        try:
            files = await github_client.list_tree(p["repo"])
        except Exception as e:
            return {"files": [], "reason": str(e)[:200]}
        # Group by what it IS, because "a README" and "a source file" are
        # different kinds of thing to a reader even though git treats them
        # identically. Shared with the no-remote listing so the tab reads the
        # same either way.
        return {"repo": p["repo"], "source": "repo",
                "files": [{**f, "kind": deliverables._kind(f["path"])} for f in files]}
    row = deliverables.latest(project_id)
    if not row:
        # Two different absences, said differently. "No repo is configured" is a
        # choice and the files arrive on delivery; "a repo is configured and we
        # cannot reach it" is a fault the boss can fix.
        return {"files": [], "source": "deliverable",
                "reason": ("nothing has been delivered yet — this project has no "
                           "remote, so its files appear here once a task delivers")
                          if not p["repo"] else
                          (f"{p['repo']} is configured but GitHub is not connected, "
                           f"and nothing has been delivered here either")}
    return {"source": "deliverable", "files": deliverables.list_files(project_id),
            "delivered_by": {"task_id": row.get("task_id"), "seq": row.get("seq"),
                             "title": row.get("title", ""), "role": row.get("role", ""),
                             "taken_at": row.get("taken_at")},
            "download_url": f"/api/projects/{project_id}/download"}


@router.get("/api/projects/{project_id}/file")
async def project_file(project_id: int, path: str, request: Request) -> dict:
    p = owned_project(project_id, request)
    if ".." in path:
        raise HTTPException(400, "a path that tries to escape the project")
    # The same order the listing uses, so a file you can SEE in the tab is a file
    # you can open. Reading from the repo while listing from the deliverable is
    # how a tab comes to show twelve files and open none of them.
    if not (p["repo"] and github_client.enabled(p["repo"])):
        try:
            return {"path": path, "text": deliverables.read_text(project_id, path)}
        except Exception as e:
            raise HTTPException(400, str(e)[:200])
    try:
        return {"path": path, "text": await github_client.read_file(p["repo"], path)}
    except Exception as e:
        raise HTTPException(400, str(e)[:200])


@router.get("/api/projects/{project_id}/download")
def download_project(project_id: int, request: Request) -> Response:
    """The deliverable, as a zip you can actually keep.

    There was no way to get the code out of this platform at all. With a remote
    that was survivable — clone it — but a project without one had exactly one
    copy of its application, in a directory the pruner deletes, and nothing in
    the product would hand it to you.

    Built into a temp file rather than memory: a deliverable is a whole
    application, and holding one in RAM per concurrent download is a way to lose
    the conductor to something as ordinary as two people clicking at once. The
    file is deleted after the response is sent.
    """
    import tempfile
    from pathlib import Path
    from starlette.background import BackgroundTask
    p = owned_project(project_id, request)
    row = deliverables.latest(project_id)
    if not row:
        raise HTTPException(404, "nothing has been delivered for this project yet")
    tmp = Path(tempfile.mkdtemp(prefix=f"devteam-dl-{project_id}-")) / "deliverable.zip"
    ok, note, _count = deliverables.write_zip(project_id, tmp)
    if not ok:
        import shutil as _shutil
        _shutil.rmtree(tmp.parent, ignore_errors=True)
        raise HTTPException(413 if "limit" in note else 404, note)

    def _cleanup() -> None:
        import shutil as _shutil
        _shutil.rmtree(tmp.parent, ignore_errors=True)

    return FileResponse(tmp, media_type="application/zip",
                        filename=deliverables.archive_name(p, row),
                        background=BackgroundTask(_cleanup))


@router.get("/api/projects/{project_id}/artifacts")
async def get_artifacts(project_id: int, request: Request) -> dict:
    """Everything the project produced: repo, branches, PRs, and the public site URL."""
    project = owned_project(project_id, request)
    repo = project["repo"]
    tasks = db.list_tasks(project_id)
    people = {a["id"]: a["name"] for a in db.list_agents(project_id)}
    # A plain-language record of what the team actually did, per task. Which sprint
    # and which teammate are carried on each row so this list can be filtered by
    # either without a second call; both are blank on projects that predate them.
    work = [{"id": t["id"], "role": t["role"], "title": t["title"], "status": t["status"],
             "pr": t["pr_number"], "attempts": t["attempts"], "model": t["model"],
             "sprint": t["sprint"], "agent_id": t.get("agent_id"),
             "agent": people.get(t.get("agent_id") or 0, ""),
             "outcome": (t["report"] or "").strip()[:1200]}
            for t in tasks]
    # What this project delivered when there is no remote to point at. Carried in
    # the same payload as `repo`/`prs` rather than behind another call, because
    # the page's honest answer to "where is the code" is one or the other and the
    # UI has to be able to tell which without asking twice.
    delivered = deliverables.latest(project_id) if not repo else None
    out = {
        # The per-sprint index, so the tab has a history dimension rather than
        # only "now". Cheap: it reads rows we already have.
        "sprints": artifacts.timeline(project_id),
        "repo": repo, "repo_url": github_client.repo_url(repo) if repo else None,
        # Stated rather than inferred from an empty repo string, so nothing in the
        # UI has to guess whether a missing PR list means "none yet" or "never".
        "has_remote": bool(repo),
        "deliverable": ({"task_id": delivered.get("task_id"), "seq": delivered.get("seq"),
                         "title": delivered.get("title", ""),
                         "role": delivered.get("role", ""),
                         "files": delivered.get("files"),
                         "bytes": delivered.get("bytes"),
                         "taken_at": delivered.get("taken_at")}
                        if delivered else None),
        "download_url": (f"/api/projects/{project_id}/download" if delivered else None),
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


@router.get("/api/projects/{project_id}/sprints")
async def sprint_timeline(project_id: int, request: Request) -> dict:
    """Every sprint this project has had, and what each one delivered.

    Freezes any sprint the project has already left before answering. The sprint
    boundary itself lives inside the manager session, so hooking it there would
    miss every project that ran before this existed and every one whose manager
    died mid-cycle; capturing on read is later but never absent.
    """
    owned_project(project_id, request)
    taken = await artifacts.ensure(project_id)
    return {"sprints": artifacts.timeline(project_id), "captured_now": taken}


@router.get("/api/projects/{project_id}/sprints/{sprint}")
async def sprint_artifacts(project_id: int, sprint: int, request: Request) -> dict:
    """One sprint's deliverables, as they read when that sprint ended.

    The boss's notes on this sprint come with it. A comment kept somewhere other
    than the thing it is about is a comment nobody reads twice.
    """
    owned_project(project_id, request)
    await artifacts.ensure(project_id)
    return {**artifacts.read(project_id, sprint),
            "feedback": feedback.summary(project_id, "sprint", sprint)}


@router.post("/api/projects/{project_id}/sprints/{sprint}/snapshot")
async def snapshot_sprint(project_id: int, sprint: int, request: Request,
                          force: bool = False) -> dict:
    """Freeze a sprint on demand.

    `force` re-reads the task rows as they are today and overwrites the record,
    which is only ever right when the first capture caught a sprint mid-flight —
    otherwise it replaces what shipped with a later edit of it.
    """
    owned_project(project_id, request)
    return await artifacts.capture(project_id, sprint, force=force)


@router.get("/api/projects/{project_id}/sprints/{sprint}/notes")
async def sprint_notes(project_id: int, sprint: int, request: Request) -> dict:
    """Release notes for a sprint, from the record alone — no model, no key needed."""
    owned_project(project_id, request)
    await artifacts.ensure(project_id)
    return await artifacts.release_notes(project_id, sprint)


@router.post("/api/projects/{project_id}/sprints/{sprint}/notes")
async def write_sprint_notes(project_id: int, sprint: int, body: SprintNotes,
                             request: Request) -> dict:
    """Have a model turn the record into readable notes.

    The prose is checked against the record and discarded whole if it cites a task
    this sprint did not contain, and the itemised facts come back with it either
    way — so the summary is never the only account of the sprint on the page.
    """
    u = current_user(request)
    owned_project(project_id, request)
    await artifacts.ensure(project_id)
    return await artifacts.release_notes(
        project_id, sprint, auth.get_settings(u) if u else {},
        provider=body.provider, model=body.model, rewrite=True)


@router.get("/api/projects/{project_id}/by-agent")
def artifacts_by_agent(project_id: int, request: Request, sprint: int = 0) -> dict:
    """What each teammate produced — a grouping of task output, not a second copy
    of it. Pass ?sprint=N to scope it to one cycle."""
    owned_project(project_id, request)
    return artifacts.by_agent(project_id, sprint or None)


@router.get("/api/projects/{project_id}/agents/{agent_id}/artifacts")
def one_agents_artifacts(project_id: int, agent_id: int, request: Request) -> dict:
    """One teammate's work across every sprint they were here for."""
    owned_project(project_id, request)
    view = artifacts.agent_view(project_id, agent_id)
    if not view:
        raise HTTPException(404, "no such teammate on this project")
    return view


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
