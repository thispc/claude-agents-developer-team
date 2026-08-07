"""The platform working on itself, and the mid-project levers on a team.

Everything behind the _root gate that files or judges work on this codebase —
status, refined tickets, triage, findings, the daily self-check — plus the
project-team endpoints that grew up alongside it (metrics, personas,
how-to-work). Triage runs BEFORE an issue is accepted so the independence a
fix will get is never a surprise afterwards.
"""

import asyncio

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .. import (ambition, auth, config, db, findings, manager, metrics,
                process, selfops, team, triage, upkeep)
from .base import _manager_tasks, _root, current_user, owned_project, router


@router.get("/api/self")
def self_status(request: Request) -> dict:
    u = _root(request)
    pid = selfops.ensure_project(u["id"])
    st = selfops.can_redeploy()
    tasks = db.list_tasks(pid)
    repo = st["repo"]
    return {"project_id": pid, "repo": repo,
            "repo_missing": ("" if repo else
                             "SELF_REPO is not set, and a container cannot discover "
                             "its own repository — the image carries no .git. Set it "
                             "in the secret or self-repair has nothing to clone."),
            "head": st["head"],
            "can_redeploy": st["ok"], "blocked_reasons": st["reasons"],
            "last_deploy": st["last_deploy"],
            "open_issues": [
                {"seq": t["seq"], "id": t["id"], "title": t["title"], "status": t["status"],
                 "pr": t["pr_number"], "issue": t["issue_number"]}
                for t in tasks if t["status"] != "done"],
            "shipped": [
                {"seq": t["seq"], "title": t["title"], "pr": t["pr_number"]}
                for t in tasks if t["status"] == "done"][-10:]}


class SelfIssue(BaseModel):
    title: str
    body: str
    severity: str = "bug"
    sprints: int = 1


class RoughIssue(BaseModel):
    rough: str


@router.post("/api/self/refine")
async def self_refine(payload: RoughIssue, request: Request) -> dict:
    """Turn a one-line complaint into a ticket worth handing to an agent.

    A vague ticket is the cheapest way to waste a sprint — the team builds the
    wrong thing, competently. This is a draft for the human to edit, never
    submitted on their behalf.
    """
    u = _root(request)
    rough = payload.rough.strip()
    if len(rough) < 8:
        raise HTTPException(400, "say a little more about what's wrong")
    return await selfops.refine_issue(rough, auth.get_settings(u))


@router.get("/api/projects/{project_id}/metrics")
def project_metrics(project_id: int, request: Request) -> dict:
    """How this project's runs actually went — rework, first-pass acceptance, cost
    per delivered task. Stale 'running' rows are swept first, because a run left
    open by a restart is neither a success nor a failure and would quietly skew
    every average below it."""
    owned_project(project_id, request)
    metrics.reconcile(project_id)
    return metrics.project(project_id)


@router.get("/api/projects/{project_id}/team")
def project_team(project_id: int, request: Request) -> dict:
    """The named people on this project, what they are doing, and what they have
    already built. Projects created before teammates existed return an empty list
    and keep working exactly as they did."""
    owned_project(project_id, request)
    return {"team": team.describe(project_id)}


class PersonaUpdate(BaseModel):
    persona: str = ""
    model: str | None = None
    provider: str | None = None


@router.patch("/api/projects/{project_id}/team/{agent_id}")
def update_teammate(project_id: int, agent_id: int, body: PersonaUpdate,
                    request: Request) -> dict:
    """Change who a teammate is, or what they run on, mid-project.

    Applies to their next task, not the one in flight: a running session already
    has its system prompt and there is no way to amend it without killing work
    that is part-done.
    """
    owned_project(project_id, request)
    agent = db.get_agent(agent_id)
    if not agent or agent["project_id"] != project_id:
        raise HTTPException(404, "no such teammate on this project")
    if body.model is not None or body.provider is not None:
        db.update_agent(agent_id, **{k: v for k, v in
                                     (("model", body.model), ("provider", body.provider))
                                     if v is not None})
    updated = team.set_persona(agent_id, body.persona)
    return {"teammate": updated, "applies": "from their next task"}


@router.get("/api/self/findings")
def list_findings(request: Request) -> dict:
    """What the platform currently knows is wrong with itself, worst first.

    Readable without letting the check act, because someone who wants to know what
    it thinks is broken should not have to authorise a repair to find out.
    """
    _root(request)
    return findings.summary()


class FindingVerdict(BaseModel):
    status: str      # fixed | dismissed | open


@router.post("/api/self/findings/{finding_id}")
def judge_finding(finding_id: int, body: FindingVerdict, request: Request) -> dict:
    """Close a finding, or put a dismissed one back on the list.

    Marking something fixed is not the end of it: a recurrence re-opens it and
    says so, because a repair that did not hold is more interesting than a fresh
    fault and must not be quietly re-filed as one.
    """
    _root(request)
    if body.status not in ("fixed", "dismissed", "open"):
        raise HTTPException(400, "status must be fixed, dismissed or open")
    if not findings.get(finding_id):
        raise HTTPException(404, "no such finding")
    findings.set_status(finding_id, body.status)
    return findings.summary()


@router.post("/api/self/upkeep")
async def run_upkeep(request: Request, force: bool = False) -> dict:
    """Run the daily self-check now instead of waiting for it."""
    _root(request)
    return await upkeep.run_once(force=force)


@router.get("/api/how-to-work")
def how_to_work(request: Request) -> dict:
    """The two choices that shape a project: how work is split, and whether time
    or quality is the constraint. Served rather than hardcoded in the page so the
    trade each one makes is stated in one place."""
    current_user(request)
    return {"process": process.catalog(), "ambition": ambition.catalog()}


@router.post("/api/self/triage")
async def self_triage(payload: RoughIssue, request: Request) -> dict:
    """How much independence this issue would get, before any work starts.

    Shown before you file, so the answer is never a surprise afterwards.
    """
    u = _root(request)
    t = await triage.classify(payload.rough[:200], payload.rough,
                              auth.get_settings(u))
    return {**t, "policy": triage.policy(t["tier"])}


@router.post("/api/self/issue")
async def self_issue(payload: SelfIssue, request: Request) -> dict:
    u = _root(request)
    if not payload.title.strip() or not payload.body.strip():
        raise HTTPException(400, "describe the issue and give it a title")
    pid = selfops.ensure_project(u["id"])
    # Self-repair is meant to run start-to-finish on its own: fix, verify, ship.
    # Asking the operator to approve each step of a fix they already asked for is
    # the interruption this feature exists to remove.
    # Decide the independence BEFORE any work starts, so "it merged something it
    # should have asked about" cannot happen after the fact.
    t = await triage.classify(payload.title, payload.body, auth.get_settings(u))
    pol = triage.policy(t["tier"])
    if not pol["may_work"]:
        raise HTTPException(400, f"{pol['note']} ({'; '.join(t['why'])})")
    sprints = max(1, min(10, payload.sprints or 1))
    db.set_sprints(pid, sprints)
    # Only a routine fix runs unsupervised to completion. Anything substantial
    # does the work and stops at a pull request.
    db.set_project_autonomy(pid, "autonomous" if pol["may_merge"] else "supervised")
    if sprints > 1:
        db.set_max_runs(pid, min(400, config.MAX_AGENT_RUNS * sprints))
    res = await selfops.file_issue(pid, payload.title.strip(), payload.body.strip(),
                                   payload.severity)
    res["triage"] = {**t, "policy": pol}
    # The manager plans the fix. If one is already running it picks the issue up as
    # a directive on its next decision point, so we must not start a second session.
    if pid not in _manager_tasks or _manager_tasks[pid].done():
        _manager_tasks[pid] = asyncio.get_event_loop().create_task(manager.run_manager(pid))
        res["manager"] = "started"
    else:
        res["manager"] = "already running — the issue was handed to it"
    return {"project_id": pid, **res}
