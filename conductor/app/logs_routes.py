"""HTTP surface for the log pipeline — its own router, like the Lifeworld's and self-repair's,
so the handbook's endpoint-count gate on routes.py keeps meaning what it says.

Root-gated in full. Logs are the most revealing surface a system has: they name file paths,
model errors, branch names and the shape of the operator's own work, and none of that is a
user feature.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from . import logs
from .routes import _root

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
def list_logs(request: Request, level: str = "", cat: str = "", event: str = "",
              q: str = "", limit: int = 200) -> dict:
    """The tail, filtered. `level` is a FLOOR — asking for warn gives warnings AND errors,
    because the question is always "show me things at least this bad"."""
    _root(request)
    return {"logs": logs.recent(level=level, cat=cat, event=event, q=q, limit=limit),
            "categories": logs.CATEGORIES, "levels": list(logs.LEVELS)}


@router.get("/stats")
def log_stats(request: Request, window_s: int = 3600) -> dict:
    """What monitoring reads: how much of each kind, how bad, and the last thing that broke.
    A category vocabulary is what makes this answerable — "14 errors" is a number, "14 errors,
    all sandbox" is a diagnosis."""
    _root(request)
    return logs.stats(window_s)


@router.get("/errors")
def list_errors(request: Request, limit: int = 100) -> dict:
    """Errors keep a longer memory of their own, so a chatty hour cannot push the one that
    matters out of the ring."""
    _root(request)
    return {"errors": logs.rows(errors_only=True)[-max(1, min(limit, 300)):]}
