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
from typing import Any

from . import bus, config, db

DEMO_PROJECT = "Demo — a link shortener (sandbox data)"

# The simulated manager is a daemon; bound it so a forgotten sandbox cannot
# keep a loop alive indefinitely.
MANAGER_MAX_LIFE = 6 * 3600
POLL_SECONDS = 3

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

# A worker's turn, as one actually reads: orient, act, observe, correct. Each
# step is (message, tool, tool-input) so the feed carries the same interleaving
# of prose and tool_use the real stream does — that interleaving is most of what
# the Activity tab exists to show, and a mock that only emits prose would make
# the busiest screen in the app look wrong.
_TURN = [
    ("Reading the existing code before I touch anything.",
     "Grep", {"pattern": "def create_link", "output_mode": "content"}),
    ("Found it. There's no test covering the collision path — writing one first "
     "so the fix has something to prove.",
     "Read", {"file_path": "tests/test_links.py", "offset": 1, "limit": 60}),
    ("Now the change itself, kept narrow.",
     "Edit", {"file_path": "app/links.py", "replace_all": False}),
    ("Running the suite.",
     "Bash", {"command": "python -m pytest -q"}),
    ("13 passed, 1 failed — the new test caught a real ordering bug, not a typo "
     "in the test. Looking at it properly.",
     "Read", {"file_path": "app/links.py", "offset": 40, "limit": 30}),
    ("The retry reused the exhausted generator. Fixed and re-running to be sure "
     "it wasn't a fluke.",
     "Bash", {"command": "python -m pytest -q"}),
]

_REPORT = """## Summary

Added slug-collision handling to `POST /api/links` and the test that proves it.

**What changed**
- `app/links.py` — retry with a longer slug instead of reusing an exhausted
  generator (the first version looked right and silently returned the same slug).
- `tests/test_links.py` — a burst test that forces a collision.

**Verification**
`pytest` exits 0, 14 passed. The new test fails against the previous commit,
which is the check that it is testing anything at all.

**One thing worth your judgement**
The retry is bounded at 5 attempts. Beyond that it 503s rather than looping. That
is a guess at the right trade — say if you would rather it widened the slug space.

_(sandbox: no code was written and nothing was pushed)_"""


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
        src = f"worker:{t['role']}"
        for msg, tool, args in _TURN:
            await asyncio.sleep(random.uniform(1.8, 3.4))
            if not db.get_task(task_id):
                return          # the task was deleted or the project cancelled
            bus.emit(t["project_id"], task_id, src, "message", msg)
            await asyncio.sleep(random.uniform(0.6, 1.4))
            bus.emit(t["project_id"], task_id, src, "tool_use",
                     {"tool": tool, "input": args})
        # The harness verification, exactly as the real one is reported.
        await asyncio.sleep(1.5)
        bus.emit(t["project_id"], task_id, src, "verifying", "running pytest")
        await asyncio.sleep(2)
        bus.emit(t["project_id"], task_id, src, "verified", "pytest exited 0")
        bus.emit(t["project_id"], task_id, "system", "verified",
                 {"cmd": "pytest", "exit_code": 0})
        db.update_task(
            task_id, status="pushed", report=_REPORT,
            verification=json.dumps({"ran": True, "ok": True, "cmd": "pytest",
                                     "exit_code": 0, "output": "14 passed in 2.31s"}))
        bus.emit(t["project_id"], task_id, src, "report",
                 {"status": "pushed", "cost_usd": 0.0, "summary": _REPORT[:2000]})
        # 'pushed' rather than 'review' so the scheduler opens a PR against the
        # mock GitHub — the reviewer should see the PR flow, not skip it.

    asyncio.get_event_loop().create_task(run())
    return f"simulated: {t['role']} is working on task {task_id} (sandbox, no agent ran)"


_REPLIES = [
    "Right now #5 (tester) is running the end-to-end pass and #6 is waiting on it. "
    "#7 failed once on turns and is queued for a stronger model — I'd rather spend "
    "one more run there than ship rate limiting I can't prove.",
    "Sprint 1 shipped create-and-redirect. This sprint is stats and rate limiting; "
    "I picked those because they're the two things a user notices first on a link "
    "shortener that already works.",
    "I merged #4 on the strength of the platform-run tests, not the worker's summary "
    "— it exited 0 on a clean clone. #7's suite is red, so that one is not merging "
    "until it isn't.",
]


async def run_manager(project_id: int) -> None:
    """A manager that answers you, without a model behind it.

    The Manager tab is the one screen whose whole value is that something replies.
    Left inert, a reviewer would conclude the chat was broken in the candidate
    build. So the sandbox manager watches the inbox and answers in the voice and
    the shape the real one uses — a live fact, a decision, and the reason for it.

    This is a daemon, like the real manager session: callers create it as a task
    rather than awaiting it. It is bounded anyway — an unbounded loop in a process
    nobody is watching is how a sandbox outlives the review it was started for.
    """
    bus.emit(project_id, None, "manager", "agent_status",
             {"status": "simulated (sandbox build)"})
    seen = 0
    deadline = time.time() + MANAGER_MAX_LIFE
    while config.DEMO_MODE and time.time() < deadline:
        p = db.get_project(project_id)
        if not p or p["status"] in ("done", "failed", "cancelled", "idle"):
            return
        for _d in db.take_directives(project_id):
            await asyncio.sleep(random.uniform(1.5, 3.0))
            bus.emit(project_id, None, "manager", "boss_reply",
                     {"message": _REPLIES[seen % len(_REPLIES)],
                      "running": ["#5 tester — end-to-end pass"]})
            seen += 1
        await asyncio.sleep(POLL_SECONDS)


# --- a GitHub that behaves, without a GitHub ------------------------------
#
# Stateful on purpose. A stub returning a random number each call would give a
# task a different PR every refresh, merges that never stick, and a branch list
# that contradicts the board — so the reviewer would be judging the mock's
# inconsistency instead of the candidate's behaviour. This keeps a small ledger
# so the same branch always maps to the same PR, and a merged PR stays merged.
_GH: dict[str, Any] = {"next": 101, "prs": {}, "issues": {}, "merged": set()}


def github_call(method: str, path: str, kwargs: dict) -> Any:
    body = (kwargs or {}).get("json") or {}
    parts = [p for p in path.split("/") if p]

    if method == "POST" and path.endswith("/issues"):
        n = _GH["next"]; _GH["next"] += 1
        _GH["issues"][n] = {"number": n, "title": body.get("title", ""), "state": "open"}
        return {"number": n}
    if method == "POST" and path.endswith("/pulls"):
        head = body.get("head", "")
        # Same branch -> same PR, exactly as GitHub behaves.
        for n, pr in _GH["prs"].items():
            if pr["head"] == head:
                return {"number": n}
        n = _GH["next"]; _GH["next"] += 1
        _GH["prs"][n] = {"number": n, "head": head, "base": body.get("base", "main"),
                         "title": body.get("title", ""), "state": "open",
                         "html_url": f"https://example.invalid/pull/{n}"}
        return {"number": n}
    if method == "GET" and "/pulls" in path:
        # find_pr_for_branch passes ?head=owner:branch through params
        want = ((kwargs or {}).get("params") or {}).get("head", "")
        branch = want.split(":")[-1]
        hits = [pr for pr in _GH["prs"].values()
                if not branch or pr["head"] == branch]
        return hits if branch else list(_GH["prs"].values())
    if method == "PUT" and path.endswith("/merge"):
        try:
            _GH["merged"].add(int(parts[parts.index("pulls") + 1]))
        except Exception:
            pass
        return {"merged": True}
    if method == "GET" and "/branches" in path:
        return [{"name": "main"}] + [{"name": pr["head"]} for pr in _GH["prs"].values()]
    if method == "GET" and path.startswith("/repos/") and len(parts) == 3:
        return {"default_branch": "main", "name": parts[2], "full_name": "/".join(parts[1:])}
    if method == "GET" and path == "/user/repos":
        return [{"full_name": "demo-org/link-shortener", "private": True,
                 "updated_at": "2026-07-20T00:00:00Z"}]
    return {}


def banner() -> dict:
    """What the UI shows so nobody mistakes a sandbox for the real thing."""
    return {"demo": True, "since": time.time(),
            "note": "Sandbox build — agents are simulated, no credentials are loaded, "
                    "nothing is pushed to GitHub."}
