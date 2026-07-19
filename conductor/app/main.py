import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import bus, config, db, manager, scheduler
from .routes import router, _manager_tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    loop = asyncio.get_event_loop()
    bus.set_loop(loop)
    config.WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    # Resume any project that was mid-flight when the conductor last stopped, so a
    # restart (deploy, crash) doesn't strand a running project without its manager.
    for p in db.list_projects():
        if p["status"] in ("planning", "running", "hold") and config.AUTH_CONFIGURED:
            scheduler.ensure(p["id"])
            _manager_tasks[p["id"]] = loop.create_task(manager.run_manager(p["id"]))
            bus.emit(p["id"], None, "system", "resumed_after_restart", {})
    yield


app = FastAPI(title="devteam conductor", lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=str(config.DASHBOARD_DIR), html=True), name="dashboard")
