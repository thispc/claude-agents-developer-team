"""Plan mode: the round table, the provider catalog, and what blocks a project.

A round table argues an idea into a blueprint before anyone spends a token
building it; the blueprint can then become a real project with the team the
seats reasoned about. The provider catalog lives here because choosing seats
is where a user first needs to know which models their keys can actually run.
"""

import asyncio
import json

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .. import (auth, blockers, bus, config, db, github_client, manager,
                providers, roundtable, team)
from .base import _manager_tasks, current_user, owned_project, owned_table, router


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


@router.get("/api/providers")
def list_providers(request: Request) -> dict:
    """Provider/model catalog plus which ones this user actually has keys for."""
    u = current_user(request)
    s = auth.get_settings(u)
    return {"providers": providers.catalog(s), "available": providers.available(s)}


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
    settings = auth.get_settings(u)
    have = providers.available(settings)
    missing = sorted({s.provider for s in body.seats} - set(have))
    if missing:
        labels = ", ".join(providers.label_for(m, settings) for m in missing)
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

    # The round table argues about what kind of people the idea needs, and that
    # judgement used to be discarded at the exact moment it mattered: only the head
    # count survived, and the personas the seats had reasoned about went nowhere.
    roster = [{**m, "role": str(m["role"]).strip().lower().replace(" ", "_"),
               "count": max(1, min(int(m.get("count", 1) or 1), 4)),
               "model": m.get("model") or "worker"}
              for m in team.from_blueprint(bp)]
    brief = _brief_from_blueprint(t["brief"], bp)
    repo = body.repo.strip() or (config.GITHUB_REPO if u["is_root"] else "")
    pid = db.create_project(
        body.name.strip() or (t["title"] or "planned project"), brief, repo,
        config.PROJECT_BUDGET_USD, config.MAX_CONCURRENT_WORKERS,
        max_runs=config.MAX_AGENT_RUNS, team=roster, autonomy=body.autonomy,
        manager_model=body.manager_model, owner_id=u["id"])
    team.hire(pid, roster)
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
