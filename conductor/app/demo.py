"""The mock engine: makes a sandboxed build explorable without doing anything real.

A self-repair PR changes the platform you are running. Reading a diff tells you
whether the code is plausible; it does not tell you whether the *app* still works.
So the candidate build is started in a sandbox — and a sandbox must never hold
credentials, never push to GitHub, and never spend a run.

That leaves a problem: an empty platform is not explorable. Every screen worth
checking (the board, the sprint archive, blockers, the manager chat) only exists
when there is work to look at. So DEMO_MODE seeds a project that looks like a real
one mid-flight, and fakes the one thing that would otherwise cost money: the agent.

Nothing here is reachable unless config.DEMO_MODE is on, which the live conductor
never sets.
"""

import asyncio
import json
import random
import time

from . import bus, config, db

DEMO_PROJECT = "Demo — a link shortener (sandbox data)"

# A believable slice of a project mid-sprint: shipped work behind it, something
# running now, something waiting, and one failure so the unhappy paths render too.
_TASKS = [
    ("backend", "Postgres schema + migrations", "done", 1, 1,
     "Added `links` and `hits` tables with a unique index on the slug."),
    ("backend", "POST /api/links with slug collision retry", "done", 1, 1,
     "Endpoint live. Collisions retry with a longer slug; 12 tests pass."),
    ("frontend", "Paste-a-URL page", "done", 1, 1,
     "Single page, copies the short link on create."),
    ("backend", "Redirect handler + hit counting", "done", 2, 1,
     "301 with async hit recording so the redirect is not blocked."),
    ("tester", "End-to-end: create, redirect, count", "running", 2, 0, ""),
    ("frontend", "Per-link stats view", "planned", 2, 0, ""),
    ("backend", "Rate limiting on create", "failed", 2, 2,
     "Worker ran out of turns before finishing the middleware."),
]

_CHATTER = [
    "Reading the existing migration before touching the schema.",
    "Writing the failing test first so the fix has something to prove.",
    "That endpoint has no test — adding one before I change it.",
    "Running the suite: 14 passed, 1 failed. Looking at the failure.",
    "Fixed. Re-running to be sure it wasn't a fluke.",
]


def is_seeded() -> bool:
    return any(p["name"] == DEMO_PROJECT for p in db.list_projects())


def seed() -> int | None:
    """Create the sandbox's demo project. Idempotent; returns the project id."""
    if not config.DEMO_MODE:
        return None
    for p in db.list_projects():
        if p["name"] == DEMO_PROJECT:
            return p["id"]

    pid = db.create_project(
        DEMO_PROJECT,
        "Build a URL shortener: paste a long link, get a short one, count the hits. "
        "This project is sandbox data — nothing here talks to GitHub or spends a run.",
        "demo-org/link-shortener", 5.0, 3, max_runs=40,
        team=[{"role": "backend", "count": 2, "model": "worker"},
              {"role": "frontend", "count": 1, "model": "worker"},
              {"role": "tester", "count": 1, "model": "lead"}],
        autonomy="supervised", owner_id=1, sprints=3,
    )
    db.set_project_status(pid, "running")
    db._execute("UPDATE projects SET sprint=2, runs_used=9 WHERE id=?", (pid,))

    ids = []
    for role, title, status, sprint, attempts, report in _TASKS:
        tid = db.create_task(pid, role, title, f"{title}. (sandbox data)")
        db._execute("UPDATE tasks SET sprint=? WHERE id=?", (sprint, tid))
        fields = {"status": status, "attempts": attempts}
        if report:
            fields["report"] = report
        if status in ("done", "failed"):
            fields["model"] = "claude-haiku-4-5" if role != "tester" else "claude-sonnet-5"
        if status == "done":
            fields["pr_number"] = 100 + len(ids)
            fields["verification"] = json.dumps(
                {"ran": True, "ok": True, "cmd": "pytest", "exit_code": 0,
                 "output": "14 passed in 2.31s"})
        if status == "failed":
            fields["verification"] = json.dumps(
                {"ran": True, "ok": False, "cmd": "pytest", "exit_code": 1,
                 "output": "1 failed, 13 passed",
                 "headline": "1 failed, 13 passed in 2.02s",
                 "failures": ["FAILED tests/test_ratelimit.py::test_burst - "
                              "AssertionError: expected 429, got 200"]})
        db.update_task(tid, **fields)
        ids.append(tid)

    # a couple of dependencies so the DAG view has something to draw
    db.update_task(ids[4], deps=json.dumps([ids[1], ids[3]]))
    db.update_task(ids[5], deps=json.dumps([ids[2]]))

    for kind, source, payload in [
        ("project_created", "system", {"name": DEMO_PROJECT}),
        ("task_created", "manager", {"role": "backend", "title": _TASKS[0][1]}),
        ("pr_merged", "manager", {"pr": 100}),
        ("sprint_finished", "manager", {"sprint": 1, "of": 3, "delivered": 4}),
        ("dispatched", "scheduler", {"role": "tester", "model": "claude-sonnet-5",
                                     "attempt": 1}),
    ]:
        bus.emit(pid, None, source, kind, payload)
    bus.emit(pid, None, "manager", "boss_reply",
             {"message": "Sprint 1 shipped the create-and-redirect path. Sprint 2 is "
                         "stats and rate limiting; the rate-limit task failed once and "
                         "is queued for a stronger model."})
    return pid


async def simulate(task_id: int) -> str:
    """Stand in for a worker agent: advance the board, spend nothing.

    Deliberately not instant — the point of the sandbox is to watch the UI behave
    while work is in flight, and a task that teleports to 'done' shows none of
    that. It also never touches git, so no branch or PR is invented.
    """
    t = db.get_task(task_id)
    if not t:
        return "error: no such task"
    db.update_task(task_id, status="running", attempts=(t["attempts"] or 0) + 1,
                   model="claude-haiku-4-5")
    bus.emit(t["project_id"], task_id, "scheduler", "dispatched",
             {"role": t["role"], "model": "claude-haiku-4-5", "attempt": 1})

    async def run() -> None:
        for line in random.sample(_CHATTER, 3):
            await asyncio.sleep(2.5)
            if not db.get_task(task_id):
                return
            bus.emit(t["project_id"], task_id, f"worker:{t['role']}", "message", line)
        await asyncio.sleep(2)
        db.update_task(
            task_id, status="review",
            report=f"(sandbox) simulated {t['role']} work for “{t['title']}”. "
                   f"No code was written and nothing was pushed.",
            verification=json.dumps({"ran": True, "ok": True, "cmd": "pytest",
                                     "exit_code": 0, "output": "14 passed"}))
        bus.emit(t["project_id"], task_id, "scheduler", "ready_for_review",
                 {"simulated": True})

    asyncio.get_event_loop().create_task(run())
    return f"simulated: {t['role']} is working on task {task_id} (sandbox, no agent ran)"


def banner() -> dict:
    """What the UI shows so nobody mistakes a sandbox for the real thing."""
    return {"demo": True, "since": time.time(),
            "note": "Sandbox build — agents are simulated, no credentials are loaded, "
                    "nothing is pushed to GitHub."}
