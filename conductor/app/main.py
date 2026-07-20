import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import auth, bus, config, db, launcher, manager, scheduler
from .routes import router, _manager_tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    auth.init()   # seeds the root superuser from .env on first run
    loop = asyncio.get_event_loop()
    bus.set_loop(loop)
    config.WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    # No manager session survives a restart, so any question still marked pending
    # has no waiter — clear them so the dashboard doesn't re-raise dead questions.
    db.abandon_questions()
    # Same reasoning for workers: a worker is a child of this process, so anything
    # still marked running belongs to a conductor that no longer exists. Left alone
    # it shows in the Agents tab forever as something you cannot kill.
    ghosts = launcher.sweep_orphans()
    if ghosts:
        print(f"[startup] released {ghosts} task(s) orphaned by the previous run")
    cooled = launcher.load_cooldowns()
    if cooled:
        print(f"[startup] restored {cooled} model cooldown(s) from the last run")
    pruned = launcher.prune_workspaces()
    if pruned:
        print(f"[startup] pruned {pruned} old worker workspace(s)")
    stale = auth.prune_sessions()
    if stale:
        print(f"[startup] removed {stale} expired session(s)")
    # Resume any project that was mid-flight when the conductor last stopped, so a
    # restart (deploy, crash) doesn't strand a running project without its manager.
    for p in db.list_projects():
        scheduler.reconcile_status(p["id"])   # a 'done' project with pending work reopens
        p = db.get_project(p["id"]) or p
        if p["status"] in ("planning", "running", "hold") and config.AUTH_CONFIGURED:
            scheduler.ensure(p["id"])
            _manager_tasks[p["id"]] = loop.create_task(manager.run_manager(p["id"]))
            bus.emit(p["id"], None, "system", "resumed_after_restart", {})
    yield


app = FastAPI(title="devteam conductor", lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=str(config.DASHBOARD_DIR), html=True), name="dashboard")
