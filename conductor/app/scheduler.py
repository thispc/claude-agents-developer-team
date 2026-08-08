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

from . import bus, config, db, deliverables, github_client, launcher, logs, tuning

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
    # Unfinished is not the same as resumable. Work frozen behind a failed
    # dependency will never run, so reopening on it put the project back to
    # 'running' with nothing to dispatch — a permanent lie on the status badge.
    frozen = {t["id"] for t in unreachable(project_id)}
    movable = [t for t in o["unfinished"] if t["id"] not in frozen]
    if movable:
        db.set_project_status(project_id, "running")
        bus.emit(project_id, None, "system", "reopened",
                 {"reason": f"{len(movable)} task(s) still unfinished"})
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


def unreachable(project_id: int, tasks: list[dict] | None = None) -> list[dict]:
    """Planned tasks that can never start as things stand, because something they
    stand on — directly or through a chain — failed.

    The direct test (`deps & failed`) is not enough. A task whose dependency is
    itself frozen is just as dead, and reporting only the first layer understates
    the damage: on the mars-rover project #5 depended on failed work and #6
    depended on #5, so the feed named one stuck task when two were.
    """
    tasks = tasks if tasks is not None else db.list_tasks(project_id)
    known = {t["id"] for t in tasks}
    dead = {t["id"] for t in tasks if t["status"] == "failed"}
    changed = True
    while changed:                      # grow the dead set until it stops moving
        changed = False
        for t in tasks:
            if t["id"] in dead or t["status"] != "planned":
                continue
            deps = set(json.loads(t["deps"] or "[]")) & known
            if deps & dead:
                dead.add(t["id"])
                changed = True
    return [t for t in tasks if t["id"] in dead and t["status"] == "planned"]


def downstream(tasks: list[dict]) -> dict[int, int]:
    """How many other tasks each task is holding up, transitively.

    The scheduler used to dispatch the ready set in whatever order the database
    returned, which is creation order. That is fine when capacity exceeds the ready
    set and actively harmful when it does not: a task nothing depends on can take
    the last worker slot ahead of the one three other people are waiting behind,
    and the whole project waits out the difference. Nothing here creates work — it
    only decides what goes first when not everything can.
    """
    blocks: dict[int, set[int]] = {t["id"]: set() for t in tasks}
    for t in tasks:
        for dep in json.loads(t["deps"] or "[]"):
            if dep in blocks:
                blocks[dep].add(t["id"])
    # Transitive closure by repeated expansion. The DAG is small (tens of tasks),
    # so the simple fixpoint is cheaper than being clever, and it terminates even
    # if a cycle sneaks in — the sets stop growing.
    changed = True
    while changed:
        changed = False
        for tid, direct in blocks.items():
            grown = set(direct)
            for other in direct:
                grown |= blocks.get(other, set())
            grown.discard(tid)
            if grown != direct:
                blocks[tid] = grown
                changed = True
    return {tid: len(v) for tid, v in blocks.items()}


def order_by_impact(ready: list[dict], tasks: list[dict]) -> list[dict]:
    """Critical path first: whatever unblocks the most other work goes now.

    Ties break on task id so the order is stable — an unstable order makes two
    runs of the same plan incomparable, which would undermine the whole point of
    recording per-run metrics.
    """
    if len(ready) < 2:
        return ready
    impact = downstream(tasks)
    return sorted(ready, key=lambda t: (-impact.get(t["id"], 0), t["id"]))


def idle_capacity(project_id: int, project: dict) -> dict:
    """Worker slots going unused, and who is sitting out.

    Reported rather than filled. The scheduler cannot invent work — two agents on
    one branch collide, so "help with that task" is not a thing it can arrange —
    and the manager is the only part of the system that can widen the plan. Making
    the waste visible to the thing that can act on it beats a scheduler quietly
    doing nothing about it.
    """
    running = db.count_running(project_id)
    slots = max(0, int(project.get("max_workers") or 0) - running)
    free = db.free_agents(project_id)
    return {"free_slots": slots, "running": running,
            "idle": [{"name": a["name"], "role": a["role"]} for a in free]}


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
    if t is not None and not t.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return   # no event loop (e.g. called from a sync context) — nothing to schedule
    _schedulers[project_id] = loop.create_task(_run(project_id))


def stop(project_id: int) -> None:
    t = _schedulers.get(project_id)
    if t and not t.done():
        t.cancel()


def _restart_manager(project_id: int) -> None:
    """Bring a manager session back up, reusing the routes' registry so we never end
    up with two sessions on one project talking over each other."""
    from . import manager as mgr
    from .routes import _manager_tasks
    live = _manager_tasks.get(project_id)
    if live and not live.done():
        return
    _manager_tasks[project_id] = asyncio.get_event_loop().create_task(
        mgr.run_manager(project_id))


async def _auto_open_pr(project: dict, task: dict) -> None:
    repo = project["repo"]
    if not github_client.enabled(repo):
        # No remote means no branch to read this work back out of, and the only
        # copy is a workspace the pruner will eventually delete. Preserve it
        # BEFORE the task goes to review: from this moment the delivery is a
        # thing the boss can open, list and download rather than a directory
        # nobody was told about.
        ok, note = await deliverables.preserve(project, task)
        bus.emit(project["id"], task["id"], "scheduler",
                 "work_preserved" if ok else "preserve_failed",
                 {"detail": note, "task": task["seq"]})
        if not ok:
            logs.warn("data", "deliverable_not_preserved", note,
                      project=project["id"], task=task["id"])
        db.update_task(task["id"], status="review")
        bus.emit(project["id"], task["id"], "scheduler", "ready_for_review", {})
        return
    # A worker has Bash and may have opened its own PR already. Adopt it rather
    # than racing it — otherwise the PR exists but we never store its number, so
    # merge_pr has nothing to merge and the UI shows no link.
    existing = await github_client.find_pr_for_branch(repo, task["branch"])
    if existing:
        db.update_task(task["id"], pr_number=existing, status="review")
        bus.emit(project["id"], task["id"], "scheduler", "pr_opened",
                 {"pr": existing, "adopted": True})
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
        # Lost a race, or the branch has no diff. Check once more before giving up:
        # "a pull request already exists" means there IS one to adopt.
        raced = await github_client.find_pr_for_branch(repo, task["branch"])
        if raced:
            db.update_task(task["id"], pr_number=raced, status="review")
            bus.emit(project["id"], task["id"], "scheduler", "pr_opened",
                     {"pr": raced, "adopted": True})
            return
        db.update_task(task["id"], status="review")
        bus.emit(project["id"], task["id"], "scheduler", "pr_open_failed", str(e)[:300])


async def _run(project_id: int) -> None:
    blocked_notified = False
    while True:
        project = db.get_project(project_id)
        if not project:
            return
        if project["status"] in ("done", "failed", "cancelled"):
            # A sprint can finish while no manager session is alive — it hit its own
            # limit, or the conductor restarted. Without this an unattended run that
            # was asked for six cycles quietly stops after the first.
            if project["status"] == "done" and project["sprint"] < project["sprints"]:
                from . import manager as mgr
                if mgr.sprint_gate(project_id):
                    _restart_manager(project_id)
                    await asyncio.sleep(8)
                    continue
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

        ready = order_by_impact(ready, tasks)
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
            try:
                result = await launcher.dispatch_task(t["id"], source="scheduler")
            except Exception as e:
                # A launch failure (k8s API error, subprocess spawn failure) must
                # not escape and kill this loop permanently — mark the task failed
                # and carry on with the rest of the ready set.
                db.update_task(t["id"], status="failed",
                               report=f"dispatch raised: {e}")
                bus.emit(project_id, t["id"], "scheduler", "dispatch_error", str(e)[:300])
                continue
            if result.startswith("error"):
                bus.emit(project_id, t["id"], "scheduler", "dispatch_error", result)

        # 'blocked' above is only the first layer; the whole frozen set is what the
        # project is actually stuck behind.
        stuck = unreachable(project_id, tasks)
        live = db.count_running(project_id)

        if stuck and not ready and live == 0:
            failed_seqs = sorted(t["seq"] for t in tasks if t["id"] in failed_ids)
            stuck_seqs = sorted(t["seq"] for t in stuck)
            if not blocked_notified:
                blocked_notified = True
                bus.emit(project_id, None, "scheduler", "dag_blocked",
                         {"blocked_tasks": stuck_seqs, "failed_deps": failed_seqs})
            # Emitting an event was all this used to do, so the project went on
            # reading 'running' forever while nothing could possibly move — the
            # Blockers tab said "critical" and the status badge said "working".
            # Blocked is not running: put it in review so every surface agrees.
            if project["status"] == "running":
                fs = ", ".join(f"#{s}" for s in failed_seqs)
                ss = ", ".join(f"#{s}" for s in stuck_seqs)
                db.set_project_status(
                    project_id, "review",
                    f"Blocked: {fs} failed, so {ss} can never start. Nothing is "
                    f"running and nothing can be dispatched until the failed work "
                    f"is retried or the plan is changed.")
                bus.emit(project_id, None, "system", "needs_attention",
                         {"reason": "the plan is blocked behind failed work",
                          "failed": failed_seqs, "blocked": stuck_seqs})
        else:
            blocked_notified = False
            # The block cleared — a retry landed, or the deps were edited. Only
            # resume a review we ourselves imposed; a review the manager set for
            # its own reasons is not ours to overwrite.
            if (project["status"] == "review" and (ready or live)
                    and (project["summary"] or "").startswith("Blocked:")):
                db.set_project_status(project_id, "running", "")
                bus.emit(project_id, None, "scheduler", "unblocked", {})

        await asyncio.sleep(8)
