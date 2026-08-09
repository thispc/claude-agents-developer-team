import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import (auth, bus, config, db, findings, home, knowledge, launcher,
               lifeworld_client, logs,
               manager, modgraph, notify, pool, scheduler, upkeep, usage)
from .routes import router, _manager_tasks


STARTED_AT = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    logs.info("lifecycle", "starting", "conductor starting up")
    auth.init()       # seeds the root superuser from .env on first run
    findings.init()
    # The five extracted stores. Each init() refuses loudly when its service is
    # not configured, and drops whatever the strangler left behind in the
    # conductor's database — the honest last step of each extraction.
    knowledge.init()
    usage.init()
    notify.init()
    # logs.init() covers the monitor too: one seam, one service, one loud door.
    # Two of them ASK before they drop, because what they drop cannot be
    # recreated: watch holds the owner's own answers, and the lifeworld holds
    # every world on the box. See their docstrings.
    logs.init()
    lifeworld_client.init()
    modgraph.init()
    try:
        # The offline fallback graph for the platform itself. Guarded: a seed failure
        # (an unreadable file, a moved module) must never block boot — the graph screen
        # can heal it later, the rest of the app does not depend on it.
        modgraph.seed_fleet_graph()
    except Exception as e:
        logs.warn("lifecycle", "graph_seed_failed", f"module-graph seed skipped: {e}")
    loop = asyncio.get_event_loop()
    bus.set_loop(loop)
    # Photograph HEAD now, not lazily — code_currency() compares against this forever
    # after, and a lazy first call would miss any commit that landed in between.
    from . import selfops
    selfops.mark_boot()
    # Every never-die background loop, so shutdown can actually end them (see below).
    background: list[asyncio.Task] = []
    config.WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    # Pending questions are NOT swept wholesale here any more — see the resume loop
    # below, which sweeps only the projects no manager is coming back for.
    #
    # The original reason ("no manager session survives a restart, so a pending
    # question has no waiter") stopped being true when this same boot learned to
    # resume mid-flight projects: those get their manager back a few lines further
    # down, and a resumed manager re-reads what is pending — the interview it never
    # got an answer to is folded into the plan before it creates tasks. Wiping first
    # meant a restart silently destroyed the boss's chance to answer, and the new
    # session asked the same question over again. The half of the reason that DOES
    # still hold is the other half: a project that will not be resumed really has
    # nobody behind its questions, and leaving those pending is how the dashboard
    # ends up raising asks from a session that died last week.
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
    # BEFORE the pruner, always. A project with no remote keeps its output in the
    # workspace and nowhere else, and everything delivered before deliverables.py
    # existed is still sitting there with nothing to claim it. Copy first, delete
    # second — the other order is the bug this exists to close.
    try:
        from . import deliverables
        kept = await deliverables.backfill()
        if kept:
            logs.info("data", "deliverables_backfilled",
                      f"preserved {len(kept)} delivered task(s) that had no copy "
                      f"outside their workspace: {', '.join(kept[:8])}", n=len(kept))
    except Exception as e:
        # A backfill failure must not stop the conductor booting — but it must not
        # be silent either, because the next line deletes things.
        logs.warn("data", "deliverables_backfill_failed", str(e)[:200])
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
        resuming = (not p["is_self"]
                    and p["status"] in ("planning", "running", "hold")
                    and config.AUTH_CONFIGURED)
        if not resuming:
            db.abandon_questions(p["id"])   # nobody is coming back to read them
            continue
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
    # ...and the shared httpx clients (app/pool.py). LAST, after the loops that use
    # them have been cancelled and after logs' own atexit flush would still find a
    # working client: a pooled client holds real sockets, and a process that exits
    # without closing them leaves the fleet's services holding half-open connections
    # until their own timeouts reap them. `aclose_all` closes this loop's async
    # clients and every sync one; a later call rebuilds, so a shim that logs on the
    # way out still works rather than raising into a shutdown path.
    await pool.aclose_all()


app = FastAPI(title="devteam conductor", lifespan=lifespan)


@app.middleware("http")
async def _log_failures(request, call_next):
    """Every API failure goes through here, so no handler has to remember to log its own.

    A 500 used to exist only as a stack trace in whatever terminal the server was started
    from; a 4xx did not exist at all. Both are now rows anyone can search, and both are what
    the monitor turns into notices. Successes are deliberately NOT logged — an access log is
    a different thing, uvicorn already writes one, and burying twelve real failures under
    ten thousand 200s is how logs become unreadable.
    """
    try:
        resp = await call_next(request)
    except Exception as e:
        logs.error("http", "request_crashed", f"{request.method} {request.url.path}: {e}",
                   path=request.url.path, method=request.method, kind=type(e).__name__)
        raise
    if resp.status_code >= 500:
        logs.error("http", "server_error", f"{request.method} {request.url.path}",
                   path=request.url.path, status=resp.status_code, dedupe_s=60)
    elif resp.status_code in (401, 403):
        logs.log("auth", "refused", f"{request.method} {request.url.path}", level="warn",
                 path=request.url.path, status=resp.status_code, dedupe_s=300)
    elif resp.status_code >= 400:
        logs.log("http", "client_error", f"{request.method} {request.url.path}", level="warn",
                 path=request.url.path, status=resp.status_code, dedupe_s=120)
    return resp


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
from .lifeworld_routes import router as lifeworld_router   # the Lifeworld: a doorway
from .lifeworld_routes import compose_router as lifeworld_compose_router
# The two routes the conductor ANSWERS (the world list minus the crew's own world;
# the agent panel plus root's log rows) go FIRST, so they win over the catch-all
# that would otherwise match them and forward blind. Every other /api/lw path is a
# thin authenticated proxy onto services/lifeworld — see lifeworld_routes.py.
app.include_router(lifeworld_compose_router)
app.include_router(lifeworld_router)
from .repair_routes import router as repair_router         # self-repair v2: its own router
app.include_router(repair_router)
from .logs_routes import router as logs_router             # the log pipeline: its own router
app.include_router(logs_router)
app.mount("/", StaticFiles(directory=str(config.DASHBOARD_DIR), html=True), name="dashboard")
