"""Deterministic DAG scheduler.

The lead agent declares tasks once (with dependencies); this loop does the
orchestration mechanics for free — no model tokens involved:

- dispatches every 'planned' task whose dependencies are all merged ('done'),
  bounded by max_workers and the project budget
- auto-opens a PR the moment a worker pushes its branch
- re-dispatches tasks the lead sent back via request_changes (they return to
  'planned' with feedback set)
- flags a blocked DAG (unmet deps because a prerequisite failed) so the lead
  can decide what to do

The lead only makes judgment calls: review reports, merge or request changes,
finish the project.
"""

import asyncio
import json

from . import bus, db, github_client, launcher

_schedulers: dict[int, asyncio.Task] = {}


def ensure(project_id: int) -> None:
    t = _schedulers.get(project_id)
    if t is None or t.done():
        _schedulers[project_id] = asyncio.get_event_loop().create_task(_run(project_id))


def stop(project_id: int) -> None:
    t = _schedulers.get(project_id)
    if t and not t.done():
        t.cancel()


async def _auto_open_pr(project: dict, task: dict) -> None:
    repo = project["repo"]
    if not github_client.enabled(repo):
        db.update_task(task["id"], status="review")
        bus.emit(project["id"], task["id"], "scheduler", "ready_for_review", {})
        return
    try:
        base = await github_client.default_branch(repo)
        body = (task["report"] or "")[:1500]
        if task["issue_number"]:
            body += f"\n\nCloses #{task['issue_number']}"
        n = await github_client.create_pr(
            repo, task["branch"], base, f"[{task['role']}] {task['title']}", body)
        db.update_task(task["id"], pr_number=n, status="review")
        bus.emit(project["id"], task["id"], "scheduler", "pr_opened", {"pr": n})
    except Exception as e:
        # Branch may have no diff, or PR already exists — hand to the lead either way.
        db.update_task(task["id"], status="review")
        bus.emit(project["id"], task["id"], "scheduler", "pr_open_failed", str(e)[:300])


async def _run(project_id: int) -> None:
    blocked_notified = False
    while True:
        project = db.get_project(project_id)
        if not project or project["status"] in ("done", "failed", "cancelled"):
            return
        tasks = db.list_tasks(project_id)
        done_ids = {t["id"] for t in tasks if t["status"] == "done"}
        failed_ids = {t["id"] for t in tasks if t["status"] == "failed"}

        for t in tasks:
            if t["status"] == "pushed":
                await _auto_open_pr(project, t)

        ready, blocked = [], []
        for t in tasks:
            if t["status"] != "planned":
                continue
            deps = set(json.loads(t["deps"] or "[]"))
            if deps <= done_ids:
                ready.append(t)
            elif deps & failed_ids:
                blocked.append(t)

        for t in ready:
            if db.count_running(project_id) >= project["max_workers"]:
                break
            if project["cost_usd"] >= project["budget_usd"]:
                break
            result = await launcher.dispatch_task(t["id"], source="scheduler")
            if result.startswith("error"):
                bus.emit(project_id, t["id"], "scheduler", "dispatch_error", result)

        if blocked and not ready and db.count_running(project_id) == 0 and not blocked_notified:
            blocked_notified = True
            bus.emit(project_id, None, "scheduler", "dag_blocked",
                     {"blocked_tasks": [t["id"] for t in blocked],
                      "failed_deps": sorted(failed_ids)})
        elif not blocked:
            blocked_notified = False

        await asyncio.sleep(8)
