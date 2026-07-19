import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import bus, config, db
from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    bus.set_loop(asyncio.get_event_loop())
    config.WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="devteam conductor", lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=str(config.DASHBOARD_DIR), html=True), name="dashboard")
