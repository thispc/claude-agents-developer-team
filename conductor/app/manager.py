"""The Manager agent: a headless Claude session (Sonnet 5) that runs the project.

It can only act through the `team` MCP tools defined here — no file or bash access.
One manager session runs per active project, inside the conductor process. It plans
the task DAG once, then reviews; it can grow the DAG at runtime (add_tasks) on its
own judgment or when team members escalate in their reports.
"""

import asyncio
import json
import time
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from . import bus, config, db, github_client, scheduler


def valid_roles() -> set[str]:
    return {r["name"] for r in config.load_roles()}


def role_catalog_text() -> str:
    lines = ["Your team roles (assign each task a role from this list):"]
    for r in config.load_roles():
        lines.append(f"- {r['name']}: {r['summary']}")
        if r.get("fan_out"):
            lines.append(f"    fan-out: {r['fan_out']} (up to {r['max_parallel']} in parallel)")
    return "\n".join(lines)


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


def _task_line(t: dict) -> str:
    deps = json.loads(t["deps"] or "[]")
    dep_note = f" deps={deps}" if deps else ""
    return (f"task {t['id']} [{t['role']}] '{t['title']}' status={t['status']}{dep_note} "
            f"attempts={t['attempts']} pr={t['pr_number'] or '-'}")


def build_team_server(project_id: int):
    """Create the per-project MCP toolset the manager agent uses."""

    def project() -> dict:
        return db.get_project(project_id) or {}

    async def _create_batch(items: list[dict], existing_dep_ids_ok: bool) -> str:
        """Create a batch of tasks. depends_on = 0-based indices within this batch;
        depends_on_existing (if allowed) = ids of tasks that already exist."""
        origin = "runtime" if existing_dep_ids_ok else "initial"
        roles = valid_roles()
        lines: list[str] = []
        repo = project().get("repo", "")
        existing_ids = {t["id"] for t in db.list_tasks(project_id)}
        created_ids: list[int | None] = []
        for item in items:
            role = item.get("role", "")
            if role not in roles:
                created_ids.append(None)
                lines.append(f"skipped '{item.get('title')}': invalid role '{role}'")
                continue
            task_id = db.create_task(project_id, role, item["title"], item["description"],
                                     origin=origin)
            created_ids.append(task_id)
        for item, task_id in zip(items, created_ids):
            if task_id is None:
                continue
            deps: list[int] = []
            for idx in item.get("depends_on", []) or []:
                if isinstance(idx, int) and 0 <= idx < len(created_ids) \
                        and created_ids[idx] and created_ids[idx] != task_id:
                    deps.append(created_ids[idx])
            if existing_dep_ids_ok:
                for dep_id in item.get("depends_on_existing", []) or []:
                    if isinstance(dep_id, int) and dep_id in existing_ids:
                        deps.append(dep_id)
            db.update_task(task_id, deps=json.dumps(sorted(set(deps))))
            issue_line = ""
            if github_client.enabled(repo):
                try:
                    n = await github_client.create_issue(
                        repo, f"[{item['role']}] {item['title']}",
                        item["description"] + f"\n\n_devteam task {task_id}_")
                    db.update_task(task_id, issue_number=n)
                    issue_line = f" (issue #{n})"
                except Exception as e:
                    issue_line = f" (issue creation failed: {e})"
            bus.emit(project_id, task_id, "manager", "task_created",
                     {"role": item["role"], "title": item["title"], "deps": deps})
            dep_note = f" after {deps}" if deps else ""
            lines.append(f"created task {task_id} [{item['role']}] "
                         f"'{item['title']}'{dep_note}{issue_line}")
        return "\n".join(lines)

    @tool("create_tasks", "Create the project's initial task DAG in one call. Pass a JSON "
          "array of objects with keys: role (backend|frontend|tester), title, description, "
          "and depends_on (array of 0-based indices of OTHER tasks in this same array that "
          "must be merged first). Descriptions must be fully self-contained specs. The "
          "scheduler then dispatches tasks automatically as dependencies merge — you never "
          "dispatch anything yourself.", {"tasks_json": str})
    async def create_tasks(args: dict[str, Any]) -> dict[str, Any]:
        try:
            items = json.loads(args["tasks_json"])
            assert isinstance(items, list) and items
        except Exception:
            return _text("error: tasks_json must be a non-empty JSON array")
        body = await _create_batch(items, existing_dep_ids_ok=False)
        db.set_project_status(project_id, "running")
        scheduler.ensure(project_id)
        return _text(body + "\nScheduler started: ready tasks dispatch automatically; "
                     "PRs auto-open when workers push. Call wait, then review.")

    @tool("add_tasks", "Grow the DAG at runtime — e.g. an extra tester task for a shaky "
          "area, a fix task for a bug found late, or work a team member escalated in a "
          "report. Same JSON array format as create_tasks, plus each item may also have "
          "depends_on_existing: an array of EXISTING task IDs that must be merged first "
          "(depends_on still refers to 0-based indices within this new batch).",
          {"tasks_json": str})
    async def add_tasks(args: dict[str, Any]) -> dict[str, Any]:
        try:
            items = json.loads(args["tasks_json"])
            assert isinstance(items, list) and items
        except Exception:
            return _text("error: tasks_json must be a non-empty JSON array")
        body = await _create_batch(items, existing_dep_ids_ok=True)
        scheduler.ensure(project_id)
        return _text(body + "\nThe scheduler will dispatch these when their dependencies merge.")

    @tool("status", "List all tasks with their current statuses and dependencies.", {})
    async def status(args: dict[str, Any]) -> dict[str, Any]:
        p = project()
        tasks = db.list_tasks(project_id)
        head = (f"project '{p.get('name')}' status={p.get('status')} "
                f"cost=${p.get('cost_usd', 0):.2f}/${p.get('budget_usd', 0):.2f}")
        return _text("\n".join([head] + [_task_line(t) for t in tasks]))

    @tool("wait", "Sleep until a task needs your attention — a worker finished (PR opened "
          "or failure) — or timeout_seconds elapses. Returns updated task statuses. "
          "Costs no tokens while waiting.", {"timeout_seconds": int})
    async def wait(args: dict[str, Any]) -> dict[str, Any]:
        timeout = min(int(args.get("timeout_seconds", 600)), 1800)
        deadline = time.time() + timeout
        before = {t["id"]: t["status"] for t in db.list_tasks(project_id)}
        note = ""
        while time.time() < deadline:
            p = project()
            if p.get("status") == "cancelled":
                note = "PROJECT CANCELLED by the user - stop all work and call finish now."
                break
            if p.get("runs_used", 0) >= p.get("max_runs", 1e9):
                note = ("AGENT-RUN CAP REACHED - do not add or retry more tasks; "
                        "wrap up and call finish.")
                break
            if config.ANTHROPIC_API_KEY and p.get("cost_usd", 0) >= p.get("budget_usd", 1e9):
                note = ("BUDGET EXHAUSTED - do not add more tasks; "
                        "wrap up and call finish.")
                break
            directives = db.take_directives(project_id)
            if directives:
                note = "MESSAGE(S) FROM THE BOSS (the user) — treat as high priority:\n" + \
                    "\n".join(f"- {d}" for d in directives)
                break
            now = {t["id"]: t["status"] for t in db.list_tasks(project_id)}
            changed = [tid for tid, s in now.items() if before.get(tid) != s]
            terminal = [tid for tid in changed if now[tid] in ("review", "failed", "done")]
            if terminal or (db.count_running(project_id) == 0 and changed):
                break
            if db.count_running(project_id) == 0 and not changed:
                note = "no workers are running; nothing to wait for."
                break
            await asyncio.sleep(5)
        tasks = db.list_tasks(project_id)
        body = "\n".join(_task_line(t) for t in tasks)
        return _text((note + "\n" if note else "") + body)

    @tool("ask_boss", "Ask the user (your boss) to make a decision when the choice is "
          "genuinely theirs — a product tradeoff, scope question, or spending call you "
          "shouldn't make alone. Blocks until they answer. Provide 2-4 concrete options; "
          "the boss may also type their own answer.", {"question": str, "options_json": str})
    async def ask_boss(args: dict[str, Any]) -> dict[str, Any]:
        try:
            opts = json.loads(args.get("options_json", "[]"))
            assert isinstance(opts, list)
        except Exception:
            opts = []
        qid = db.ask_question(project_id, args["question"], [str(o) for o in opts][:4])
        prev_status = project().get("status", "running")
        db.set_project_status(project_id, "hold")  # surfaces as "on hold" in the UI
        bus.emit(project_id, None, "manager", "boss_question",
                 {"id": qid, "question": args["question"], "options": opts})
        deadline = time.time() + 3600
        while time.time() < deadline:
            if project().get("status") == "cancelled":
                return _text("project cancelled while awaiting your answer; call finish.")
            q = db.get_question(qid)
            if q and q["status"] == "answered":
                db.set_project_status(project_id, "running" if prev_status == "hold" else prev_status)
                bus.emit(project_id, None, "boss", "answer", q["answer"])
                return _text(f"The boss answered: {q['answer']}")
            # The boss may reply by typing a message instead of clicking an option —
            # treat any directive that arrives while we wait as the answer.
            directives = db.take_directives(project_id)
            if directives:
                db.answer_question(qid, "; ".join(directives))
                db.set_project_status(project_id, "running" if prev_status == "hold" else prev_status)
                reply = "The boss replied: " + "; ".join(directives)
                bus.emit(project_id, None, "boss", "answer", reply)
                return _text(reply)
            await asyncio.sleep(4)
        db.set_project_status(project_id, "running")
        return _text("No answer within 60 minutes; use your best judgment and proceed.")

    @tool("get_report", "Read the final report a team member produced for a task. Check "
          "the end for an ESCALATION: section — that is a request for extra tasks.",
          {"task_id": int})
    async def get_report(args: dict[str, Any]) -> dict[str, Any]:
        t = db.get_task(int(args["task_id"]))
        if not t:
            return _text("error: no such task")
        return _text(t["report"] or "(no report yet)")

    @tool("request_changes", "Send a task back to its team member with specific feedback. "
          "The scheduler re-runs it on the same branch automatically. After 2 failed "
          "rounds the task escalates to a stronger model.",
          {"task_id": int, "feedback": str})
    async def request_changes(args: dict[str, Any]) -> dict[str, Any]:
        task_id = int(args["task_id"])
        t = db.get_task(task_id)
        if not t:
            return _text("error: no such task")
        db.update_task(task_id, feedback=args["feedback"], status="planned")
        bus.emit(project_id, task_id, "manager", "changes_requested",
                 {"feedback": args["feedback"]})
        repo = project().get("repo", "")
        if github_client.enabled(repo) and t["pr_number"]:
            try:
                await github_client.comment_issue(repo, t["pr_number"], args["feedback"])
            except Exception:
                pass
        return _text(f"task {task_id} queued for rework; the scheduler will re-dispatch it. "
                     "Call wait for the result.")

    @tool("accept_task", "Mark a task done after judging its report, when there is no PR to "
          "merge — e.g. a tester task that only verified and made no code changes. Use this "
          "to close verification tasks so dependents unblock and the board stays clean. Pass "
          "a one-line verdict.", {"task_id": int, "verdict": str})
    async def accept_task(args: dict[str, Any]) -> dict[str, Any]:
        t = db.get_task(int(args["task_id"]))
        if not t:
            return _text("error: no such task")
        db.update_task(t["id"], status="done")
        bus.emit(project_id, t["id"], "manager", "task_accepted",
                 {"verdict": args.get("verdict", "")})
        return _text(f"task {t['id']} accepted and marked done.")

    @tool("merge_pr", "Squash-merge a task's pull request. Merging unblocks dependent tasks.",
          {"task_id": int})
    async def merge_pr(args: dict[str, Any]) -> dict[str, Any]:
        t = db.get_task(int(args["task_id"]))
        repo = project().get("repo", "")
        if not t:
            return _text("error: no such task")
        if not github_client.enabled(repo) or not t["pr_number"]:
            db.update_task(t["id"], status="done")
            return _text("no PR to merge; task marked done.")
        ok = await github_client.merge_pr(repo, t["pr_number"])
        if ok:
            db.update_task(t["id"], status="done")
            if t["issue_number"]:
                try:
                    await github_client.close_issue(repo, t["issue_number"])
                except Exception:
                    pass
            bus.emit(project_id, t["id"], "manager", "pr_merged", {"pr": t["pr_number"]})
            return _text(f"merged PR #{t['pr_number']}; task {t['id']} done")
        return _text(f"error: PR #{t['pr_number']} could not be merged (conflicts or checks)")

    @tool("finish", "Finish the project. status must be 'done' or 'failed'. Include a "
          "short shipping summary.", {"status": str, "summary": str})
    async def finish(args: dict[str, Any]) -> dict[str, Any]:
        s = "done" if args.get("status") == "done" else "failed"
        db.set_project_status(project_id, s, args.get("summary", ""))
        bus.emit(project_id, None, "manager", "project_finished", {"status": s})
        return _text(f"project marked {s}. You can stop now.")

    return create_sdk_mcp_server(
        name="team", version="1.0.0",
        tools=[create_tasks, add_tasks, status, wait, ask_boss, get_report,
               request_changes, accept_task, merge_pr, finish],
    )


MANAGER_TOOLS = [f"mcp__team__{n}" for n in
                 ("create_tasks", "add_tasks", "status", "wait", "ask_boss",
                  "get_report", "request_changes", "accept_task", "merge_pr", "finish")]

BUILTIN_TOOLS_OFF = ["Bash", "Read", "Write", "Edit", "Glob", "Grep",
                     "WebSearch", "WebFetch", "Task", "NotebookEdit", "TodoWrite"]


async def run_manager(project_id: int) -> None:
    project = db.get_project(project_id)
    if not project:
        return
    bus.emit(project_id, None, "manager", "agent_status", {"status": "starting"})

    roster = json.loads(project.get("team") or "[]")
    roster_text = ""
    if roster:
        lines = ", ".join(f"{m['count']}× {m['role']}" for m in roster)
        roster_text = (
            f"\nThe boss recruited this starting team: {lines}.\n"
            "Plan around this headcount: create tasks for these roles, and when a role has a "
            "count > 1 and the work genuinely splits into independent parallel pieces, create "
            "that many tasks for it (wired with no mutual dependency so they run in parallel). "
            "If the work doesn't split cleanly, you may use fewer; if it clearly needs a role "
            "the boss didn't recruit, you may still add it, but respect the boss's intent.\n"
        )
    autonomy = project.get("autonomy", "supervised")
    if autonomy == "autonomous":
        autonomy_text = (
            "\nAUTONOMY: FULL. The boss gave you full control. Make every call yourself — "
            "plan, merge, add tasks, and finish without asking. Only use ask_boss if you are "
            "genuinely, unrecoverably blocked (e.g. a missing credential only they can supply).\n")
    else:
        autonomy_text = (
            "\nAUTONOMY: SUPERVISED. The boss wants a say on the important calls. Use ask_boss "
            "before merging the FIRST substantial PR and before finish, and whenever there's a "
            "real product/scope decision — give 2-4 concrete options. Don't ask about routine "
            "mechanics; do ask before anything the boss would want to weigh in on.\n")
    prompt = (
        f"Project: {project['name']}\n"
        f"Repository: {project['repo'] or '(none configured)'}\n"
        f"Max parallel workers: {project['max_workers']} | "
        f"Agent-run cap: {project['max_runs']}\n"
        f"{autonomy_text}\n"
        f"{role_catalog_text()}\n"
        f"{roster_text}\n"
        f"Brief from the user:\n{project['brief']}\n\n"
        "Plan the work, run your team, and ship it. This may be a restarted session: "
        "call status first, and only create_tasks if none exist yet."
    )
    options = ClaudeAgentOptions(
        system_prompt=config.load_role_prompt("manager"),
        model=config.LEAD_MODEL,
        max_turns=config.LEAD_MAX_TURNS,
        mcp_servers={"team": build_team_server(project_id)},
        allowed_tools=MANAGER_TOOLS,
        disallowed_tools=BUILTIN_TOOLS_OFF,
        permission_mode="bypassPermissions",
    )

    if db.list_tasks(project_id):  # restarted project with an existing DAG
        scheduler.ensure(project_id)

    last_text = ""
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for tb in (getattr(message, "thinking", None) or []):
                    think = getattr(tb, "thinking", "")
                    if think and think.strip():
                        bus.emit(project_id, None, "manager", "thinking", think[:1500])
                for block in message.content:
                    think = getattr(block, "thinking", None)
                    if think and str(think).strip():
                        bus.emit(project_id, None, "manager", "thinking", str(think)[:1500])
                    if isinstance(block, TextBlock) and block.text.strip():
                        last_text = block.text.strip()
                        bus.emit(project_id, None, "manager", "message", block.text)
                    elif isinstance(block, ToolUseBlock):
                        bus.emit(project_id, None, "manager", "tool_use",
                                 {"tool": block.name.replace("mcp__team__", ""),
                                  "input": {k: (str(v)[:400]) for k, v in (block.input or {}).items()}})
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                db.add_project_cost(project_id, cost)
                bus.emit(project_id, None, "manager", "result", {"cost_usd": cost})
    except Exception as e:
        # Surface the model/API's own words (e.g. "Credit balance is too low")
        # instead of the SDK's generic wrapper message.
        detail = last_text or str(e)
        bus.emit(project_id, None, "manager", "error", detail)
        db.set_project_status(project_id, "failed", f"manager session failed: {detail[:400]}")
        return

    # If the manager ended without calling finish, flag for human review.
    fresh = db.get_project(project_id)
    if fresh and fresh["status"] not in ("done", "failed", "cancelled"):
        db.set_project_status(project_id, "review",
                              "manager session ended without finish(); needs human review")
        bus.emit(project_id, None, "system", "needs_review", {})
