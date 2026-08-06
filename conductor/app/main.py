import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import auth, bus, config, db, findings, home, launcher, logs, manager, scheduler, upkeep
from .routes import router, _manager_tasks


STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    logs.info("lifecycle", "starting", "conductor starting up")
    auth.init()       # seeds the root superuser from .env on first run
    findings.init()
    loop = asyncio.get_event_loop()
    bus.set_loop(loop)
    # Every never-die background loop, so shutdown can actually end them (see below).
    background: list[asyncio.Task] = []
    config.WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    # No manager session survives a restart, so any question still marked pending
    # has no waiter — clear them so the dashboard doesn't re-raise dead questions.
    db.abandon_questions()
    # Same reasoning for workers: a worker is a child of this process, so anything
    # still marked running belongs to a conductor that no longer exists. Left alone
    # it shows in the Agents tab forever as something you cannot kill.
    ghosts = launcher.sweep_orphans()
    if ghosts:
        logs.info("lifecycle", "orphans_released",
                  f"released {ghosts} task(s) orphaned by the previous run", n=ghosts)
    cooled = launcher.load_cooldowns()
    if cooled:
        logs.info("quota", "cooldowns_restored",
                  f"restored {cooled} model cooldown(s) from the last run", n=cooled)
    pruned = launcher.prune_workspaces()
    if pruned:
        logs.info("lifecycle", "workspaces_pruned",
                  f"pruned {pruned} old worker workspace(s)", n=pruned)
    stale = auth.prune_sessions()
    if stale:
        logs.info("auth", "sessions_expired",
                  f"removed {stale} expired session(s)", n=stale)
    # Resume any project that was mid-flight when the conductor last stopped, so a
    # restart (deploy, crash) doesn't strand a running project without its manager.
    if config.DEMO_MODE:
        from . import demo
        demo.seed()          # an empty sandbox has no screens worth checking
    for p in db.list_projects():
        scheduler.reconcile_status(p["id"])   # a 'done' project with pending work reopens
        p = db.get_project(p["id"]) or p
        # is_self is excluded deliberately: the platform only works on itself when
        # someone triggers it, never because the conductor happened to restart.
        if p["is_self"]:
            continue
        if p["status"] in ("planning", "running", "hold") and config.AUTH_CONFIGURED:
            scheduler.ensure(p["id"])
            _manager_tasks[p["id"]] = loop.create_task(manager.run_manager(p["id"]))
            bus.emit(p["id"], None, "system", "resumed_after_restart", {})
    # Notice new versions of ourselves. Without this the loop is autonomous but
    # not unattended: CI publishes an image and nothing adopts it.
    from . import cloud
    if cloud.in_cluster():
        background.append(loop.create_task(cloud.watch()))
    # Look at what went wrong, on a schedule, without being asked. Self-repair that
    # only runs when a human notices something is broken is not self-repair — the
    # value of the loop is entirely in the part nobody is present for.
    background.append(loop.create_task(upkeep.loop()))
    # The Studio's background life. Free at rest — it wakes, reads rows, and almost
    # always finds nothing due. It spends a token only when an agent has genuinely
    # accumulated enough work to be worth remembering, under a hard daily budget,
    # on each owner's OWN credentials (default_settings_for).
    background.append(loop.create_task(home.loop(home.default_settings_for)))
    # Self-repair v2 — the IT crew's sprint loop. A no-op every tick until the owner flips
    # the button; resumes mid-sprint after any restart because its state lives in kv.
    from . import repair
    repair.claim_lease()          # we bound the port; the engine is ours, not a zombie's
    background.append(loop.create_task(repair.loop()))
    yield
    # SHUTDOWN. These loops are written never to die, which is right while the server is up
    # and wrong the moment it is going down: nothing cancelled them, so a SIGTERM'd process
    # could keep its event loop alive — still holding the repair lease and still ticking the
    # engine against the same database as its replacement. That is not theoretical; it is
    # the "why is another devteam running" bug, and it wasted a sprint.
    # Resumed manager sessions go with them: a manager that outlives the server is the same
    # hazard wearing a different hat, and it holds an SDK subprocess open too.
    logs.info("lifecycle", "stopping", "shutting down — cancelling background loops")
    dying = background + [t for t in _manager_tasks.values() if not t.done()]
    for t in dying:
        t.cancel()
    if dying:
        await asyncio.wait(dying, timeout=5)


app = FastAPI(title="devteam conductor", lifespan=lifespan)


@app.middleware("http")
async def _preview_host(request, call_next):
    """A request on a preview host (p<id>.<PREVIEW_HOST>) is a request FOR that
    project's running app, not for the dashboard — reverse-proxy it. Every other
    request passes straight through, so nothing about normal use changes. Inert
    unless PREVIEW_HOST is configured."""
    from . import preview_proxy
    pid = preview_proxy.project_for_host(request.headers.get("host", ""))
    if pid is not None:
        return await preview_proxy.proxy(request, pid)
    return await call_next(request)


@app.middleware("http")
async def _no_stale_dashboard(request, call_next):
    """The dashboard is hand-written JS/CSS (dashboard/js/*.js + style.css) with no build hash, so an
    aggressive browser cache can keep serving yesterday's JS after a redeploy — which
    looks exactly like "the fix didn't ship" (e.g. dragging silently broken). Force a
    revalidation on the HTML/JS/CSS so a deploy is always picked up; StaticFiles still
    answers 304 when nothing changed, so it stays cheap."""
    resp = await call_next(request)
    path = request.url.path
    if request.method in ("GET", "HEAD") and (path == "/" or path.endswith((".html", ".js", ".css", ".map"))):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.include_router(router)
from .lifeworld_routes import router as lifeworld_router   # the Lifeworld: its own router
app.include_router(lifeworld_router)
from .repair_routes import router as repair_router         # self-repair v2: its own router
app.include_router(repair_router)
from .logs_routes import router as logs_router             # the log pipeline: its own router
app.include_router(logs_router)
app.mount("/", StaticFiles(directory=str(config.DASHBOARD_DIR), html=True), name="dashboard")
