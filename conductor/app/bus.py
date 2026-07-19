"""Event bus: persists events and fans them out to connected dashboard websockets."""

import asyncio
import json
from typing import Any

from . import db

_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def _broadcast(event: dict) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def emit(project_id: int, task_id: int | None, source: str, kind: str, payload: Any) -> dict:
    """Persist an event and broadcast it. Safe to call from any thread."""
    event = db.add_event(project_id, task_id, source, kind, payload)
    if _loop is not None:
        _loop.call_soon_threadsafe(_broadcast, event)
    return event


def emit_state(kind: str, data: dict) -> None:
    """Broadcast a non-persisted state refresh hint (e.g. task status changed)."""
    event = {"id": 0, "project_id": data.get("project_id", 0), "task_id": data.get("task_id"),
             "source": "system", "kind": kind, "payload": json.dumps(data), "ts": 0}
    if _loop is not None:
        _loop.call_soon_threadsafe(_broadcast, event)
