"""Everything standing in the way of a project, in one place.

The activity feed shows what *happened* and notifications show what needs a
click; neither answers "why is this project not moving?". A blocker is the
answer to that question: a current, unresolved obstacle, with the reason it
exists and the one thing that clears it.

Blockers are derived on read — nothing is stored. A blocker that no longer
applies simply stops being reported, so the tab can never go stale.
"""

import json
import time
from typing import Any

from . import auth, config, db, github_client, launcher

# Ordering for the UI: a critical blocker has stopped the project outright; a
# warning is degrading it; info is worth knowing but nothing is stuck.
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

# A task 'running' longer than this is suspicious even before the watchdog
# (STUCK_SECONDS) gives up on it.
SLOW_SECONDS = 900


def _b(severity: str, kind: str, title: str, detail: str,
       fix: str = "", task_seq: int | None = None, task_id: int | None = None,
       action: str = "", since: float | None = None) -> dict[str, Any]:
    return {"severity": severity, "kind": kind, "title": title, "detail": detail,
            "fix": fix, "task_seq": task_seq, "task_id": task_id,
            "action": action, "since": since}


def _why_failed(report: str) -> str:
    """Pull the human-meaningful cause out of a failure report."""
    r = (report or "").strip()
    if not r:
        return "the worker reported no reason"
    low = r.lower()
    if "maximum number of turns" in low:
        return ("the worker ran out of turns before finishing — the task is too big "
                "for one agent, or it got stuck in a loop")
    if launcher.looks_rate_limited(r):
        return "the model was rate limited or overloaded"
    if "git push failed" in low:
        return "the work was done but could not be pushed to GitHub"
    if "could not prepare repository" in low or "git clone failed" in low:
        return "the worker could not clone the repo — check the GitHub token and repo name"
    if "credit balance" in low:
        return "the Anthropic account is out of credit"
    tail = r.strip().splitlines()
    return tail[-1][:300] if tail else r[:300]


def scan(project_id: int) -> list[dict[str, Any]]:
    """Every current obstacle on this project, worst first."""
    p = db.get_project(project_id)
    if not p:
        return []
    tasks = db.list_tasks(project_id)
    by_id = {t["id"]: t for t in tasks}
    now = time.time()
    out: list[dict[str, Any]] = []

    failed_ids = {t["id"] for t in tasks if t["status"] == "failed"}

    # 1. Failed work — the project cannot be complete while these exist.
    for t in tasks:
        if t["status"] != "failed":
            continue
        out.append(_b(
            "critical", "task_failed",
            f"Task #{t['seq']} ({t['role']}) failed",
            f"“{t['title']}” — {_why_failed(t['report'])}",
            fix=("Re-run it, or tell the manager to split it into smaller tasks. "
                 "After 2 attempts it automatically moves to a stronger model."),
            task_seq=t["seq"], task_id=t["id"], action="retry", since=t["updated_at"]))

    # 2. Work that cannot start because something it needs failed — including
    # work stranded further down the chain, behind a task that is itself frozen.
    from . import scheduler  # local import: scheduler imports launcher, avoid a cycle
    frozen = scheduler.unreachable(project_id, tasks)
    frozen_ids = {t["id"] for t in frozen}
    for t in frozen:
        deps = set(json.loads(t["deps"] or "[]"))
        direct = deps & failed_ids
        via = deps & frozen_ids
        if direct:
            names = ", ".join(f"#{by_id[d]['seq']}" for d in sorted(direct) if d in by_id)
            why, fix = (f"“{t['title']}” is waiting on {names}, which failed.",
                        f"Clear {names} first — everything behind it is frozen.")
        else:
            names = ", ".join(f"#{by_id[d]['seq']}" for d in sorted(via) if d in by_id)
            why, fix = (f"“{t['title']}” is waiting on {names}, which is itself blocked.",
                        "It unfreezes on its own once the failed work above it is cleared.")
        out.append(_b(
            "critical" if direct else "warning", "dep_blocked",
            f"Task #{t['seq']} ({t['role']}) can't start", why, fix=fix,
            task_seq=t["seq"], task_id=t["id"]))

    # 3. A dependency cycle means nothing in the loop will ever run.
    cyc = scheduler.has_cycle(project_id)
    if cyc:
        names = " → ".join(f"#{by_id[c]['seq']}" for c in cyc if c in by_id)
        out.append(_b(
            "critical", "dag_cycle", "Circular dependency in the plan",
            f"These tasks depend on each other in a loop: {names}. None of them can start.",
            fix="Edit one of them in the Dependencies tab and remove the dependency "
                "that closes the loop."))

    # 4. The manager is waiting on the boss — nothing moves until it's answered.
    q = db.pending_question(project_id)
    if q:
        waited = int((now - q["created_at"]) / 60)
        out.append(_b(
            "critical" if waited > 5 else "warning", "awaiting_boss",
            "Your manager is waiting on you",
            f"“{q['text']}” — asked {waited} min ago. The manager is paused until you answer.",
            fix="Answer it on the Command tab.", action="answer", since=q["created_at"]))

    # 5. Workers that are running but look wedged.
    for t in tasks:
        if t["status"] == "running" and now - t["updated_at"] > SLOW_SECONDS:
            mins = int((now - t["updated_at"]) / 60)
            out.append(_b(
                "warning", "slow_worker",
                f"Task #{t['seq']} ({t['role']}) has been silent for {mins} min",
                f"“{t['title']}” is still marked running but hasn't reported anything.",
                fix=f"It is killed automatically at "
                    f"{int(scheduler.STUCK_SECONDS / 60)} min and retried.",
                task_seq=t["seq"], task_id=t["id"], since=t["updated_at"]))

    # 6. Models in rate-limit cooldown — work will be slow or re-routed.
    for model in launcher.FALLBACK_ORDER:
        left = launcher.cooldown_left(model)
        if left > 0:
            out.append(_b(
                "warning", "rate_limited", f"{model} is rate limited",
                f"Cooling down for another {left}s. Tasks are being routed to other models.",
                fix="Nothing to do — it clears itself. Reassign urgent tasks to a "
                    "model with headroom."))

    # 7. Capacity ceilings.
    if p["runs_used"] >= p["max_runs"]:
        out.append(_b(
            "critical", "run_cap", "Agent-run cap reached",
            f"This project has used all {p['max_runs']} agent runs, so no new work "
            f"can be dispatched.",
            fix="Raise the cap for this project to let the team continue."))
    elif p["runs_used"] >= p["max_runs"] * 0.85:
        out.append(_b(
            "warning", "run_cap_near", "Close to the agent-run cap",
            f"{p['runs_used']} of {p['max_runs']} runs used.",
            fix="Raise the cap now if the work isn't nearly done."))

    if config.ANTHROPIC_API_KEY and p["cost_usd"] >= p["budget_usd"]:
        out.append(_b("critical", "budget", "Budget spent",
                      f"${p['cost_usd']:.2f} of ${p['budget_usd']:.2f} used; dispatch is halted.",
                      fix="Raise the budget to continue."))

    # 8. Credentials and repo — the usual reason a project never starts at all.
    owner = auth.get_user(p["owner_id"]) if p["owner_id"] else None
    if owner and not owner["is_root"] and not auth.has_own_ai_credentials(owner):
        out.append(_b(
            "critical", "no_credentials", "No AI credentials on your account",
            "Agents can't run: this account has neither an Anthropic API key nor a "
            "Claude subscription token, and it does not inherit the operator's.",
            fix="Add one in Settings (⚙).", action="settings"))
    if not p["repo"]:
        out.append(_b("warning", "no_repo", "No GitHub repo attached",
                      "Work stays in the worker's scratch space; no branches, PRs or "
                      "artifacts are produced.",
                      fix="Attach a repo to get reviewable PRs."))
    elif not github_client.enabled(p["repo"]):
        out.append(_b("warning", "no_github_token", "GitHub isn't connected",
                      f"Can't open issues or PRs on {p['repo']}.",
                      fix="Add a GitHub token in Settings (⚙).", action="settings"))

    # 9. The project believes it is finished but isn't.
    if p["status"] == "review" and p["summary"] and "Cannot be done" in p["summary"]:
        out.append(_b("critical", "false_done", "Marked done while work was missing",
                      p["summary"],
                      fix="Tell the manager to redo the failed work before finishing."))

    # 10. Nothing is running and nothing can start — a silent stall.
    live = [t for t in tasks if t["status"] in ("queued", "running")]
    startable = [t for t in tasks if t["status"] == "planned"
                 and set(json.loads(t["deps"] or "[]")) <= {
                     x["id"] for x in tasks if x["status"] == "done"}]
    if p["status"] == "running" and not live and not startable and not q:
        waiting = [t for t in tasks if t["status"] in ("review", "pushed")]
        if waiting:
            out.append(_b(
                "warning", "awaiting_manager", "Waiting on the manager's review",
                f"{len(waiting)} task(s) are submitted and nothing else can start until "
                f"the manager judges them.",
                fix="If the manager isn't responding, restart it (↻ Restart manager)."))
        elif not out:
            out.append(_b(
                "warning", "stalled", "Project is stalled",
                "It's marked running, but nothing is executing and nothing can start.",
                fix="Restart the manager (↻) so it re-plans."))

    # 11. Nothing is checking this project's work.
    # Merging on the worker's prose is the weakest thing the platform does: an
    # imperfect verifier caps accuracy regardless of how much compute is spent, and
    # no verifier removes the ceiling entirely. Worth saying out loud, because the
    # symptom (a manager confidently merging) looks exactly like success.
    finished = [t for t in tasks if t["status"] in ("done", "review", "pushed")]
    if finished and not any(json.loads(t["verification"] or "{}").get("ran")
                            for t in finished):
        out.append(_b(
            "warning", "unverified",
            "Nothing is verifying this project",
            f"{len(finished)} task(s) have been submitted or merged, and not one was "
            f"checked by a test, build or lint command — the manager is judging the "
            f"worker's own account of its work.",
            fix="Add a test command the repo declares: a `test` script in package.json, "
                "a pytest suite, `go test`, `cargo test`, or a `test:` target in a "
                "Makefile. The platform runs it itself and refuses to merge a branch "
                "that fails it."))

    out.sort(key=lambda b: (SEVERITY_RANK.get(b["severity"], 9), -(b["since"] or 0)))
    return out


def summary(project_id: int) -> dict[str, Any]:
    items = scan(project_id)
    return {
        "blockers": items,
        "critical": sum(1 for b in items if b["severity"] == "critical"),
        "warning": sum(1 for b in items if b["severity"] == "warning"),
    }
