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


def role_catalog_text(project: dict) -> str:
    """The roles the manager must assign tasks to. If the boss recruited a team, THOSE
    are the roles (any domain). Otherwise fall back to the software default catalog."""
    roster = json.loads(project.get("team") or "[]")
    if roster:
        lines = ["Your team (assign every task a role from EXACTLY this recruited list — "
                 "use these exact role names, do not substitute software roles):"]
        for m in roster:
            tier = "stronger model" if m.get("model") == "lead" else "cheap model"
            lines.append(f"- {m['role']} ({m.get('count', 1)} recruited, {tier})")
        lines.append("Create one task per distinct piece of work, assigning the most fitting "
                     "recruited role. When a role was recruited with count > 1 and the work "
                     "splits into independent parallel pieces, create that many tasks for it.")
        return "\n".join(lines)
    lines = ["Your team roles (assign each task a role from this list):"]
    for r in config.load_roles():
        lines.append(f"- {r['name']}: {r['summary']}")
        if r.get("fan_out"):
            lines.append(f"    fan-out: {r['fan_out']} (up to {r['max_parallel']} in parallel)")
    return "\n".join(lines)


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


def _seq_of(deps: list[int]) -> list[int]:
    """Translate internal task ids into the per-project numbers humans see."""
    out = []
    for d in deps:
        t = db.get_task(d)
        out.append(t["seq"] if t and t["seq"] else d)
    return out


def _task_line(t: dict) -> str:
    deps = json.loads(t["deps"] or "[]")
    dep_note = f" deps={_seq_of(deps)}" if deps else ""
    return (f"task {t['seq']} [{t['role']}] '{t['title']}' status={t['status']}{dep_note} "
            f"attempts={t['attempts']} pr={t['pr_number'] or '-'}")


def build_team_server(project_id: int):
    """Create the per-project MCP toolset the manager agent uses."""

    def project() -> dict:
        return db.get_project(project_id) or {}

    async def _create_batch(items: list[dict], existing_dep_ids_ok: bool) -> str:
        """Create a batch of tasks. depends_on = 0-based indices within this batch;
        depends_on_existing (if allowed) = ids of tasks that already exist."""
        origin = "runtime" if existing_dep_ids_ok else "initial"
        lines: list[str] = []
        repo = project().get("repo", "")
        existing_ids = {t["id"] for t in db.list_tasks(project_id)}
        created_ids: list[int | None] = []
        for item in items:
            # Accept ANY role name (recruited aerospace role, custom role, software role) —
            # unknown roles run on a capable generic worker prompt. Only skip if empty.
            role = str(item.get("role", "")).strip().lower().replace(" ", "-")
            if not role:
                created_ids.append(None)
                lines.append(f"skipped '{item.get('title')}': missing role")
                continue
            task_id = db.create_task(project_id, role, item["title"], item["description"],
                                     origin=origin)
            compete = item.get("compete")
            if isinstance(compete, int) and compete > 1:
                db.update_task(task_id, compete=min(compete, 3))
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
                for dep_n in item.get("depends_on_existing", []) or []:
                    if not isinstance(dep_n, int):
                        continue
                    prior = db.resolve_task(project_id, dep_n)
                    if prior and prior["id"] in existing_ids:
                        deps.append(prior["id"])
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
            dep_note = f" after {_seq_of(deps)}" if deps else ""
            created = db.get_task(task_id) or {}
            lines.append(f"created task {created.get('seq', task_id)} [{item['role']}] "
                         f"'{item['title']}'{dep_note}{issue_line}")
        return "\n".join(lines)

    @tool("create_tasks", "Create the project's initial task DAG in one call. Pass a JSON "
          "array of objects with keys: role (backend|frontend|tester), title, description, "
          "depends_on (array of 0-based indices of OTHER tasks in this same array that "
          "must be merged first), and optionally compete: 2 or 3 to run that many RIVAL "
          "attempts at the task in parallel (each on its own branch, ideally different "
          "models) which you then judge with compare_work + pick_winner. "
          "Descriptions must be fully self-contained specs. The "
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
        # Only mention money when money is actually being spent. On a subscription the
        # dollar figure is a meaningless estimate, and showing it made managers cut
        # projects short ("budget is at $4.33/$5.00, wrapping up") for no reason.
        if config.ANTHROPIC_API_KEY:
            usage = f"spend=${p.get('cost_usd', 0):.2f}/${p.get('budget_usd', 0):.2f}"
        else:
            usage = (f"agent runs used={p.get('runs_used', 0)}/{p.get('max_runs', 40)} "
                     "(no monetary cost — this project runs on a flat-rate subscription)")
        head = f"project '{p.get('name')}' status={p.get('status')} {usage}"
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
                note = ("MESSAGE(S) FROM THE BOSS (the user) — STOP and handle these "
                        "before anything else:\n" +
                        "\n".join(f"- {d}" for d in directives) +
                        "\n\nIf any of that is a QUESTION, your very next action must be "
                        "reply_to_boss with a real answer — which task is running, on "
                        "which model, how long it has been going, what it is waiting on. "
                        "Leaving the boss unanswered is not acceptable; they cannot see "
                        "what you can.")
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
        db.abandon_questions(project_id)   # a new question supersedes any older one
        qid = db.ask_question(project_id, args["question"], [str(o) for o in opts][:4])
        prev_status = project().get("status", "running")
        db.set_project_status(project_id, "hold")  # surfaces as "on hold" in the UI
        bus.emit(project_id, None, "manager", "boss_question",
                 {"id": qid, "question": args["question"], "options": opts})
        # An unattended overnight run cannot afford to stall an hour on a question
        # nobody is awake to answer. With full autonomy the boss already said
        # "decide for me", so wait a short grace period and then proceed on your own
        # judgement, recording the assumption so it is auditable.
        autonomous = (project().get("autonomy") == "autonomous")
        grace = config.AUTONOMOUS_QUESTION_GRACE if autonomous else 3600
        deadline = time.time() + grace
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
        db.abandon_questions(project_id)      # don't leave a dead modal on the dashboard
        mins = int(grace / 60)
        bus.emit(project_id, None, "manager", "proceeded_without_answer",
                 {"question": args["question"], "waited_minutes": mins})
        if autonomous:
            return _text(
                f"No answer in {mins} min and you have FULL AUTONOMY — do not ask again. "
                f"Decide it yourself now, pick the option you judge best, and say in your "
                f"next message which assumption you made so the boss can audit it later. "
                f"An unattended run must keep moving.")
        return _text(f"No answer within {mins} minutes; use your best judgment and proceed.")

    @tool("reply_to_boss",
          "Answer the boss directly. Use this the moment a MESSAGE FROM THE BOSS "
          "asks you anything — especially 'why is this taking so long'. Give the "
          "real status: what is running, on which model, for how long, what it is "
          "waiting on, and what you are doing about it. Never leave a question "
          "unanswered.",
          {"message": str})
    async def reply_to_boss(args: dict[str, Any]) -> dict[str, Any]:
        msg = str(args.get("message", "")).strip()
        if not msg:
            return _text("error: say something")
        # Attach the facts so the answer is checkable, not a vibe.
        import time as _t
        now = _t.time()
        live = []
        for t in db.list_tasks(project_id):
            if t["status"] in ("queued", "running"):
                mins = int((now - t["updated_at"]) / 60)
                live.append(f"#{t['seq']} {t['role']} on {t['model'] or '?'} "
                            f"({t['status']}, {mins}m, attempt {t['attempts']})")
        bus.emit(project_id, None, "manager", "boss_reply",
                 {"message": msg, "running": live})
        return _text("Delivered to the boss. Carry on.")

    @tool("get_report", "Read the final report a team member produced for a task. Check "
          "the end for an ESCALATION: section — that is a request for extra tasks.",
          {"task_id": int})
    async def get_report(args: dict[str, Any]) -> dict[str, Any]:
        t = db.resolve_task(project_id, int(args["task_id"]))
        if not t:
            return _text("error: no such task")
        # A contest's work lives on the rival rows, not on the task. Returning
        # "(no report yet)" here made managers believe nothing had been built.
        rivals = db.list_contenders(t["id"])
        if rivals and not (t["report"] or "").strip():
            lines = [f"This task ran as a CONTEST with {len(rivals)} rival attempts. "
                     f"Their work is below; judge it with compare_work and pick_winner.",
                     ""]
            for r in rivals:
                lines.append(f"--- rival #{r['idx']} ({r['model']}) [{r['status']}] "
                             f"branch {r['branch']} ---")
                lines.append((r["report"] or "(no report)")[:1500])
            return _text("\n".join(lines))
        return _text(t["report"] or "(no report yet)")

    @tool("request_changes", "Send a task back to its team member with specific feedback. "
          "The scheduler re-runs it on the same branch automatically. After 2 failed "
          "rounds the task escalates to a stronger model.",
          {"task_id": int, "feedback": str})
    async def request_changes(args: dict[str, Any]) -> dict[str, Any]:
        t = db.resolve_task(project_id, int(args["task_id"]))
        task_id = t["id"] if t else -1
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
        return _text(f"task {t['seq']} queued for rework; the scheduler will re-dispatch it. "
                     "Call wait for the result.")

    @tool("compare_work", "For a task you ran as a contest (compete > 1), see every rival's "
          "attempt side by side — their model, branch, and full report — so you can judge "
          "which one actually delivered. Judge against the task's acceptance criteria, not "
          "which report reads nicer.", {"task_id": int})
    async def compare_work(args: dict[str, Any]) -> dict[str, Any]:
        _t = db.resolve_task(project_id, int(args["task_id"]))
        task_id = _t["id"] if _t else -1
        rivals = db.list_contenders(task_id)
        if not rivals:
            return _text("that task was not run as a contest; use get_report instead.")
        t = db.get_task(task_id)
        # Only judge attempts that actually delivered. Selection over a pool that
        # includes failures measurably underperforms — filter first, then judge.
        usable = [r for r in rivals if r["status"] == "pushed" and (r["report"] or "").strip()]
        dropped = [r for r in rivals if r not in usable]
        if not usable:
            return _text("no rival produced usable work; every attempt failed. "
                         "Use request_changes or reassign_task rather than picking a winner.")

        # Judges are measurably sensitive to the ORDER candidates appear in, and
        # they favour output from their own model family. So shuffle the running
        # order and withhold which model wrote which attempt: the judgement should
        # come from the work, not from the byline or the position.
        import random as _random
        shown = list(usable)
        _random.shuffle(shown)

        parts = [f"Contest for task {_t['seq'] if _t else task_id}: {_t['title'] if _t else ''}",
                 f"Acceptance criteria were:\n{(t or {}).get('description', '')[:1500]}",
                 "",
                 "The attempts below are in random order and the authoring model is "
                 "deliberately withheld. Judge the work itself against the criteria.",
                 ""]
        for r in shown:
            parts.append(
                f"===== ATTEMPT #{r['idx']} — branch {r['branch']} =====\n"
                f"{(r['report'] or '(no report)')[:6000]}")
        if dropped:
            parts.append("\n(Not shown, because they delivered nothing: " +
                         ", ".join(f"#{r['idx']} [{r['status']}]" for r in dropped) + ")")
        parts.append("\nPick one with pick_winner(task_id, rival_idx, reason) using the "
                     "ATTEMPT # shown above. Judge against the acceptance criteria, not on "
                     "which report reads better. If a loser had a good idea the winner "
                     "missed, say so in the reason — it becomes feedback.")
        return _text("\n\n".join(parts))

    @tool("pick_winner", "Declare which rival attempt wins a contest. Its branch becomes the "
          "task's branch and goes to PR; the others are discarded. Give the reason you chose "
          "it (and anything from a loser worth folding in).",
          {"task_id": int, "rival_idx": int, "reason": str})
    async def pick_winner(args: dict[str, Any]) -> dict[str, Any]:
        _t = db.resolve_task(project_id, int(args["task_id"]))
        task_id = _t["id"] if _t else -1
        num = _t["seq"] if _t else args["task_id"]
        idx = int(args["rival_idx"])
        rivals = db.list_contenders(task_id)
        winner = next((r for r in rivals if r["idx"] == idx), None)
        if not winner:
            return _text(f"error: no rival #{idx} on task {num}")
        if winner["status"] != "pushed":
            return _text(f"error: rival #{idx} did not finish successfully; pick one that did")
        for r in rivals:
            db.update_contender(r["id"], status="won" if r["id"] == winner["id"] else "lost")
        # Promote the winning branch, then let the normal PR flow take over.
        db.update_task(task_id, branch=winner["branch"], status="pushed",
                       model=winner["model"], report=winner["report"])
        bus.emit(project_id, task_id, "manager", "winner_picked",
                 {"rival": idx, "model": winner["model"], "reason": args.get("reason", "")})
        scheduler.ensure(project_id)
        return _text(f"rival #{idx} ({winner['model']}) wins task {num}; its branch "
                     f"{winner['branch']} goes to PR. Others discarded.")

    @tool("reassign_task", "Pull a task off its current agent and re-run it on a different "
          "model — use when a model is rate-limited/overloaded, when cheap work needs a "
          "stronger brain, or when an expensive model is overkill. Valid models: "
          "claude-haiku-4-5 (fast/cheap, most headroom), claude-sonnet-5 (balanced), "
          "claude-opus-4-8 (most capable). Give a short reason.",
          {"task_id": int, "model": str, "reason": str})
    async def reassign_task(args: dict[str, Any]) -> dict[str, Any]:
        t = db.resolve_task(project_id, int(args["task_id"]))
        task_id = t["id"] if t else -1
        if not t:
            return _text("error: no such task")
        model = str(args.get("model", "")).strip()
        allowed = {"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8", "claude-fable-5"}
        if model not in allowed:
            return _text(f"error: model must be one of {sorted(allowed)}")
        db.update_task(task_id, pinned_model=model, status="planned",
                       feedback=(t["feedback"] or "") +
                                f"\n[reassigned to {model}: {args.get('reason', '')}]")
        bus.emit(project_id, task_id, "manager", "reassigned",
                 {"model": model, "reason": args.get("reason", "")})
        scheduler.ensure(project_id)
        return _text(f"task {t['seq']} reassigned to {model}; the scheduler will re-run it.")

    @tool("accept_task", "Mark a task done after judging its report, when there is no PR to "
          "merge — e.g. a tester task that only verified and made no code changes. Use this "
          "to close verification tasks so dependents unblock and the board stays clean. Pass "
          "a one-line verdict.", {"task_id": int, "verdict": str})
    async def accept_task(args: dict[str, Any]) -> dict[str, Any]:
        t = db.resolve_task(project_id, int(args["task_id"]))
        if not t:
            return _text("error: no such task")
        db.update_task(t["id"], status="done")
        bus.emit(project_id, t["id"], "manager", "task_accepted",
                 {"verdict": args.get("verdict", "")})
        scheduler.ensure(project_id)   # accepting unblocks dependents; wake the loop
        return _text(f"task {t['seq']} accepted and marked done.")

    @tool("merge_pr", "Squash-merge a task's pull request. Merging unblocks dependent tasks.",
          {"task_id": int})
    async def merge_pr(args: dict[str, Any]) -> dict[str, Any]:
        t = db.resolve_task(project_id, int(args["task_id"]))
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
            # Keep the previewed demo in step with main, so the boss never opens an
            # old build after new work lands.
            from . import preview
            if preview.preview_root(project_id) is not None:
                asyncio.get_event_loop().create_task(preview.sync(project_id))
            return _text(f"merged PR #{t['pr_number']}; task {t['seq']} done")
        return _text(f"error: PR #{t['pr_number']} could not be merged (conflicts or checks)")

    @tool("finish", "Finish the project. status must be 'done' or 'failed'. Include a "
          "short shipping summary.", {"status": str, "summary": str})
    async def finish(args: dict[str, Any]) -> dict[str, Any]:
        s = "done" if args.get("status") == "done" else "failed"
        # Guard: 'done' must mean the work actually landed. A failed task means part
        # of the product is missing, which is how broken builds get signed off.
        if s == "done":
            stuck = [t for t in db.list_tasks(project_id)
                     if t["status"] in ("failed", "planned", "queued", "running", "pushed", "review")]
            if stuck:
                lines = "; ".join(f"#{t['seq']} {t['role']} is {t['status']}" for t in stuck[:6])
                return _text(
                    f"REFUSED: you cannot finish as done while work is outstanding — {lines}. "
                    "Rework or reassign the failed ones, close the verified ones with "
                    "accept_task/merge_pr, or finish with status 'failed' and explain.")
        db.set_project_status(project_id, s, args.get("summary", ""))
        bus.emit(project_id, None, "manager", "project_finished", {"status": s})
        return _text(f"project marked {s}. You can stop now.")

    return create_sdk_mcp_server(
        name="team", version="1.0.0",
        tools=[create_tasks, add_tasks, status, wait, ask_boss, reply_to_boss, get_report,
               request_changes, reassign_task, compare_work, pick_winner,
               accept_task, merge_pr, finish],
    )


MANAGER_TOOLS = [f"mcp__team__{n}" for n in
                 ("create_tasks", "add_tasks", "status", "wait", "ask_boss",
                  "reply_to_boss", "get_report", "request_changes", "reassign_task",
                  "compare_work", "pick_winner", "accept_task", "merge_pr", "finish")]

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
        f"{role_catalog_text(project)}\n"
        f"{roster_text}\n"
        f"Brief from the user:\n{project['brief']}\n\n"
        "Plan the work, run your team, and ship it. This may be a restarted session: "
        "call status first, and only create_tasks if none exist yet."
    )
    # The manager's character is the biggest lever on the whole team: it decides who
    # gets recruited, how hard the work is reviewed, and what ships. Let the boss set it.
    system_prompt = config.load_role_prompt("manager")
    if project.get("is_self"):
        system_prompt += (
            "\n\n## You are working on this platform itself\n\n"
            "This repository is the devteam platform — the very application running "
            "you and your team right now. Treat every change as production surgery:\n"
            "- Prefer the smallest change that fixes the issue. Reject work that "
            "refactors unrelated code, however tempting.\n"
            "- Never let anyone modify `devteam.db`, `.env`, or `workspaces/` — those "
            "are live state, not source.\n"
            "- Require evidence that the app still starts before you merge. A worker "
            "claiming 'it works' is not enough; the report must show the import or "
            "start command and its output.\n"
            "- A broken merge here takes down the platform for everyone. When in doubt, "
            "request changes rather than merging.\n"
            "- The boss deploys separately, so merging is safe; it does not restart "
            "anything by itself."
        )
    persona = (project.get("manager_persona") or "").strip()
    if persona:
        system_prompt += ("\n\n## Additional character instructions from your boss "
                          "(these take precedence)\n\n" + persona)
    # Run the manager under the project owner's own credentials (their key or their
    # subscription token), falling back to the server's.
    from .launcher import owner_credentials
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        env=owner_credentials(project),
        model=(project.get("manager_model") or "").strip() or config.LEAD_MODEL,
        max_turns=config.LEAD_MAX_TURNS,
        mcp_servers={"team": build_team_server(project_id)},
        allowed_tools=MANAGER_TOOLS,
        disallowed_tools=BUILTIN_TOOLS_OFF,
        permission_mode="bypassPermissions",
    )

    # Make sure the target repo exists before the team tries to clone it. Projects
    # created before auto-create (or whose repo was deleted) would otherwise ask the
    # boss about a missing repo on every restart instead of just fixing it.
    if project["repo"] and github_client.enabled(project["repo"]):
        try:
            ok, note = await github_client.ensure_repo(project["repo"])
            if ok and "created" in note:
                bus.emit(project_id, None, "system", "repo_ready", note)
        except Exception as e:
            bus.emit(project_id, None, "system", "repo_error", str(e)[:200])

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
        db.abandon_questions(project_id)   # nothing is waiting on them now
        bus.emit(project_id, None, "manager", "error", detail)
        db.set_project_status(project_id, "failed", f"manager session failed: {detail[:400]}")
        return
    finally:
        db.abandon_questions(project_id)   # session over — clear any unanswered ask

    # If the manager ended without calling finish, flag for human review.
    fresh = db.get_project(project_id)
    if fresh and fresh["status"] not in ("done", "failed", "cancelled"):
        db.set_project_status(project_id, "review",
                              "manager session ended without finish(); needs human review")
        bus.emit(project_id, None, "system", "needs_review", {})
