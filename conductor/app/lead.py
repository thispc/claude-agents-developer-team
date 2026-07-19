"""The Lead agent: a headless Claude session (Sonnet 5) that manages the project.

It can only act through the `team` MCP tools defined here — no file or bash access.
One lead session runs per active project, inside the conductor process.
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

VALID_ROLES = {"backend", "frontend", "tester"}


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


def _task_line(t: dict) -> str:
    return (f"task {t['id']} [{t['role']}] '{t['title']}' status={t['status']} "
            f"branch={t['branch']} attempts={t['attempts']} pr={t['pr_number'] or '-'}")


def build_team_server(project_id: int):
    """Create the per-project MCP toolset the lead agent uses."""

    def project() -> dict:
        return db.get_project(project_id) or {}

    @tool("create_tasks", "Create the project's task DAG in one call. Pass a JSON array of "
          "objects with keys: role (backend|frontend|tester), title, description, and "
          "depends_on (array of 0-based indices of OTHER tasks in this same array that "
          "must be merged first). Descriptions must be fully self-contained specs. "
          "The scheduler then dispatches tasks automatically as dependencies merge — "
          "you do NOT dispatch anything yourself.", {"tasks_json": str})
    async def create_tasks(args: dict[str, Any]) -> dict[str, Any]:
        try:
            items = json.loads(args["tasks_json"])
            assert isinstance(items, list) and items
        except Exception:
            return _text("error: tasks_json must be a non-empty JSON array")
        lines = []
        repo = project().get("repo", "")
        created_ids: list[int | None] = []
        for item in items:  # first pass: create tasks so indices map to ids
            role = item.get("role", "")
            if role not in VALID_ROLES:
                created_ids.append(None)
                lines.append(f"skipped '{item.get('title')}': invalid role '{role}'")
                continue
            task_id = db.create_task(project_id, role, item["title"], item["description"])
            created_ids.append(task_id)
        for item, task_id in zip(items, created_ids):  # second pass: wire deps + issues
            if task_id is None:
                continue
            deps = []
            for idx in item.get("depends_on", []) or []:
                if isinstance(idx, int) and 0 <= idx < len(created_ids) \
                        and created_ids[idx] and created_ids[idx] != task_id:
                    deps.append(created_ids[idx])
            db.update_task(task_id, deps=json.dumps(deps))
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
            bus.emit(project_id, task_id, "lead", "task_created",
                     {"role": item["role"], "title": item["title"], "deps": deps})
            dep_note = f" after {deps}" if deps else ""
            lines.append(f"created task {task_id} [{item['role']}] '{item['title']}'{dep_note}{issue_line}")
        db.set_project_status(project_id, "running")
        scheduler.ensure(project_id)
        lines.append("Scheduler started: ready tasks dispatch automatically; PRs "
                     "auto-open when workers push. Call wait, then review.")
        return _text("\n".join(lines))

    @tool("status", "List all tasks with their current statuses.", {})
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
            if p.get("cost_usd", 0) >= p.get("budget_usd", 1e9):
                note = ("BUDGET EXHAUSTED - do not dispatch more workers; "
                        "wrap up and call finish.")
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

    @tool("get_report", "Read the final report a worker produced for a task.", {"task_id": int})
    async def get_report(args: dict[str, Any]) -> dict[str, Any]:
        t = db.get_task(int(args["task_id"]))
        if not t:
            return _text("error: no such task")
        return _text(t["report"] or "(no report yet)")

    @tool("request_changes", "Send a task back to its worker with specific feedback. "
          "The scheduler re-runs the worker on the same branch automatically. After 2 "
          "failed rounds the task escalates to a stronger model.",
          {"task_id": int, "feedback": str})
    async def request_changes(args: dict[str, Any]) -> dict[str, Any]:
        task_id = int(args["task_id"])
        t = db.get_task(task_id)
        if not t:
            return _text("error: no such task")
        db.update_task(task_id, feedback=args["feedback"], status="planned")
        bus.emit(project_id, task_id, "lead", "changes_requested", {"feedback": args["feedback"]})
        repo = project().get("repo", "")
        if github_client.enabled(repo) and t["pr_number"]:
            try:
                await github_client.comment_issue(repo, t["pr_number"], args["feedback"])
            except Exception:
                pass
        return _text(f"task {task_id} queued for rework; the scheduler will re-dispatch it. "
                     "Call wait for the result.")

    @tool("merge_pr", "Squash-merge a task's pull request.", {"task_id": int})
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
            bus.emit(project_id, t["id"], "lead", "pr_merged", {"pr": t["pr_number"]})
            return _text(f"merged PR #{t['pr_number']}; task {t['id']} done")
        return _text(f"error: PR #{t['pr_number']} could not be merged (conflicts or checks)")

    @tool("finish", "Finish the project. status must be 'done' or 'failed'. Include a "
          "short shipping summary.", {"status": str, "summary": str})
    async def finish(args: dict[str, Any]) -> dict[str, Any]:
        s = "done" if args.get("status") == "done" else "failed"
        db.set_project_status(project_id, s, args.get("summary", ""))
        bus.emit(project_id, None, "lead", "project_finished", {"status": s})
        return _text(f"project marked {s}. You can stop now.")

    return create_sdk_mcp_server(
        name="team", version="1.0.0",
        tools=[create_tasks, status, wait, get_report,
               request_changes, merge_pr, finish],
    )


LEAD_TOOLS = [f"mcp__team__{n}" for n in
              ("create_tasks", "status", "wait", "get_report",
               "request_changes", "merge_pr", "finish")]

BUILTIN_TOOLS_OFF = ["Bash", "Read", "Write", "Edit", "Glob", "Grep",
                     "WebSearch", "WebFetch", "Task", "NotebookEdit", "TodoWrite"]


async def run_lead(project_id: int) -> None:
    project = db.get_project(project_id)
    if not project:
        return
    bus.emit(project_id, None, "lead", "agent_status", {"status": "starting"})

    prompt = (
        f"Project: {project['name']}\n"
        f"Repository: {project['repo'] or '(none configured)'}\n"
        f"Budget: ${project['budget_usd']:.2f} | Max parallel workers: {project['max_workers']}\n\n"
        f"Brief from the user:\n{project['brief']}\n\n"
        "Plan the work, run your team, and ship it. This may be a restarted session: "
        "call status first, and only create_tasks if none exist yet."
    )
    options = ClaudeAgentOptions(
        system_prompt=config.load_role_prompt("lead"),
        model=config.LEAD_MODEL,
        max_turns=config.LEAD_MAX_TURNS,
        mcp_servers={"team": build_team_server(project_id)},
        allowed_tools=LEAD_TOOLS,
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
                        bus.emit(project_id, None, "lead", "thinking", think[:1500])
                for block in message.content:
                    think = getattr(block, "thinking", None)
                    if think and str(think).strip():
                        bus.emit(project_id, None, "lead", "thinking", str(think)[:1500])
                    if isinstance(block, TextBlock) and block.text.strip():
                        last_text = block.text.strip()
                        bus.emit(project_id, None, "lead", "message", block.text)
                    elif isinstance(block, ToolUseBlock):
                        bus.emit(project_id, None, "lead", "tool_use",
                                 {"tool": block.name.replace("mcp__team__", ""),
                                  "input": {k: (str(v)[:400]) for k, v in (block.input or {}).items()}})
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                db.add_project_cost(project_id, cost)
                bus.emit(project_id, None, "lead", "result", {"cost_usd": cost})
    except Exception as e:
        # Surface the model/API's own words (e.g. "Credit balance is too low")
        # instead of the SDK's generic wrapper message.
        detail = last_text or str(e)
        bus.emit(project_id, None, "lead", "error", detail)
        db.set_project_status(project_id, "failed", f"lead session failed: {detail[:400]}")
        return

    # If the lead ended without calling finish, flag for human review.
    fresh = db.get_project(project_id)
    if fresh and fresh["status"] not in ("done", "failed", "cancelled"):
        db.set_project_status(project_id, "review",
                              "lead session ended without finish(); needs human review")
        bus.emit(project_id, None, "system", "needs_review", {})
