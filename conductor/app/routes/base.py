"""The one APIRouter every domain module registers on, and the state they share.

One router instance rather than one per domain, so main.py mounts the whole
surface with a single include exactly as it did when routes.py was one file.
`_manager_tasks` is the live registry of manager sessions — main.py resumes
into it at boot and the scheduler checks it, so it must have exactly one home.
The guards are re-exported from app.guards so a domain module gets everything
it shares with its siblings from this one place.
"""

import asyncio

from fastapi import APIRouter

from ..guards import (_owned, _root, can_see, current_user,     # noqa: F401
                      owned_project, owned_table, owned_task)

router = APIRouter()
_manager_tasks: dict[int, asyncio.Task] = {}

__all__ = ["router", "_manager_tasks", "current_user", "can_see", "owned_task",
           "owned_project", "_owned", "owned_table", "_root"]
