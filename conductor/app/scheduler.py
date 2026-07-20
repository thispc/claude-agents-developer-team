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
import time

from . import bus, config, db, github_client, launcher

# A task 'running' longer than this with no update is treated as a dead worker
# (k8s Job evicted, subprocess killed, SDK hang). Its Job has its own hard
# activeDeadlineSeconds; this is the conductor-side backstop.
STUCK_SECONDS = int(config._env("WORKER_STUCK_SECONDS", "1800"))

_schedulers: dict[int, asyncio.Task] = {}


UNFINISHED = ("planned", "queued", "running", "pushed", "review")


def outstanding(project_id: int) -> dict:
    """What is still not finished on this project, in plain terms."""
    tasks = db.list_tasks(project_id)
    return {
        "unfinished": [t for t in tasks if t["status"] in UNFINISHED],
        "failed": [t for t in tasks if t["status"] == "failed"],
    }


def reconcile_status(project_id: int) -> bool:
    """A project is only 'done' when nothing is outstanding — including failures.

    Shipping 'done' while a task sits failed is how a broken app gets approved: the
    work never landed, but the status said otherwise. A failed task keeps the project
    in 'review' (needs attention) with the reason named.
    """
    p = db.get_project(project_id)
    if not p or p["status"] not in ("done", "failed", "review"):
        return False
    o = outstanding(project_id)
    if o["unfinished"]:
        db.set_project_status(project_id, "running")
        bus.emit(project_id, None, "system", "reopened",
                 {"reason": f"{len(o['unfinished'])} task(s) still unfinished"})
        ensure(project_id)
        return True
    if o["failed"] and p["status"] == "done":
        names = ", ".join(f"#{t['seq']} {t['role']} ({t['title'][:40]})" for t in o["failed"])
        db.set_project_status(project_id, "review",
                              f"Cannot be done: {len(o['failed'])} task(s) failed and were "
                              f"never completed — {names}. The delivered app is missing "
                              f"that work.")
        bus.emit(project_id, None, "system", "needs_attention",
                 {"reason": "failed tasks were never completed", "tasks": names})
        return True
    return False


def has_cycle(project_id: int) -> list[int]:
    """Return a task cycle in the project's DAG if one exists (else empty list).
    Keeps the graph valid without needing a graph database — the DAG is small."""
    tasks = db.list_tasks(project_id)
    deps = {t["id"]: json.loads(t["deps"] or "[]") for t in tasks}
    WHITE, GREY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in deps}
    stack: list[int] = []

    def visit(u: int) -> list[int]:
        color[u] = GREY
        stack.append(u)
        for v in deps.get(u, []):
            if v not in color:
                continue
            if color[v] == GREY:  # back-edge → cycle
                return stack[stack.index(v):] + [v]
            if color[v] == WHITE:
                found = visit(v)
                if found:
                    return found
        color[u] = BLACK
        stack.pop()
        return []

    for tid in deps:
        if color[tid] == WHITE:
            found = visit(tid)
            if found:
                return found
    return []


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

        now = time.time()
        for t in tasks:
            if t["status"] == "pushed":
                await _auto_open_pr(project, t)
            elif t["status"] in ("running", "queued") and now - t["updated_at"] > STUCK_SECONDS:
                # 'queued' matters as much as 'running': a worker that dies before it
                # ever emits agent_status (image pull failure, crash on import, no
                # route to the conductor) never reaches 'running', and count_running
                # counts 'queued' — so without this the project deadlocks silently.
                db.update_task(t["id"], status="failed",
                               report=f"worker was {t['status']} with no activity for "
                                      f"{int((now - t['updated_at']) / 60)} min; "
                                      f"marked failed by the watchdog")
                bus.emit(project_id, t["id"], "scheduler", "worker_stalled",
                         {"idle_seconds": int(now - t["updated_at"]), "was": t["status"]})

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
            # Only a real API key spends real money. On a subscription cost_usd is an
            # SDK estimate that db.add_project_cost already forces to 0 — gating on it
            # here stopped dispatch with no event and no explanation anywhere.
            if config.ANTHROPIC_API_KEY and project["cost_usd"] >= project["budget_usd"]:
                bus.emit(project_id, None, "scheduler", "budget_reached",
                         {"cost_usd": project["cost_usd"], "budget_usd": project["budget_usd"]})
                break
            result = await launcher.dispatch_task(t["id"], source="scheduler")
            if result.startswith("error"):
                bus.emit(project_id, t["id"], "scheduler", "dispatch_error", result)

        if blocked and not ready and db.count_running(project_id) == 0 and not blocked_notified:
            blocked_notified = True
            failed_seqs = sorted(t["seq"] for t in tasks if t["id"] in failed_ids)
            bus.emit(project_id, None, "scheduler", "dag_blocked",
                     {"blocked_tasks": [t["seq"] for t in blocked],
                      "failed_deps": failed_seqs})
        elif not blocked:
            blocked_notified = False

        await asyncio.sleep(8)
