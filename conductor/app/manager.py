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

from . import (ambition, bus, config, db, github_client, interview, process,
               review, scheduler, team, tuning)


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


def verification_of(t: dict) -> dict:
    try:
        return json.loads(t.get("verification") or "{}")
    except Exception:
        return {}


def _with_evidence(t: dict, body: str) -> str:
    """Put the harness-run result in front of the model's own account of itself.

    The report is the worker's CLAIM. This block is EVIDENCE — our process ran the
    project's own checks, so the model could not soften or invent the outcome.
    """
    v = verification_of(t)
    if not v:
        return body
    if not v.get("ran"):
        head = ("VERIFICATION: none available — " + str(v.get("reason", "")) +
                "\nTreat everything below as an unverified claim.")
    elif v.get("ok"):
        # A syntax check passing means the file parses. It does not mean the thing
        # works, and letting it be read as "tests passed" would be the platform
        # over-claiming on its own evidence — the one place it must not.
        shallow = "syntax" in str(v.get("cmd", "")).lower()
        head = (f"VERIFICATION: PASSED. `{v.get('cmd')}` exited 0 — run by the platform, "
                f"not by the worker, so this is evidence rather than a claim.\n"
                + ("NOTE: this is a SYNTAX check only. It proves the code parses; it "
                   "proves nothing about whether it behaves correctly. Treat the "
                   "behaviour claims below as unverified, and consider asking for a "
                   "real test.\n" if shallow else "")
                + f"--- output (tail) ---\n{(v.get('output') or '')[-1200:]}")
    else:
        # Name what broke before showing raw log. A pass/fail bit makes the judge
        # guess; the failing test names and assertions are the actual evidence, and
        # they let request_changes cite specifics instead of "the tests failed".
        failures = v.get("failures") or []
        detail = ""
        if v.get("headline"):
            detail += f"\nCount: {v['headline']}"
        if failures:
            detail += "\nWhat failed:\n" + "\n".join(f"  - {f}" for f in failures)
            detail += ("\nQuote these when sending the task back — a worker told "
                       "which assertion broke fixes it far more often than one told "
                       "'the tests failed'.")
        head = (f"VERIFICATION: FAILED. `{v.get('cmd')}` exited {v.get('exit_code')}. "
                f"The worker's summary below may still claim success — believe the exit "
                f"code, not the prose.{detail}\n--- output (tail) ---\n"
                f"{(v.get('output') or '')[-1500:]}")
    return f"{head}\n\n=== the worker's own report ===\n{body}"


def verification_brief(project: dict) -> str:
    """Tell the manager to CREATE a way to check the work when none exists.

    The platform runs whatever check a repository declares, and treats that
    result as evidence rather than a claim — which is its single most important
    safeguard. When a repository declares nothing, the safeguard silently does
    nothing, and every task is accepted on the worker's own account of itself.

    That happened on a real run: a game project shipped five tasks, none of them
    checked, because a canvas game written as index.html + game.js declares no
    test command and nobody was ever asked to add one. The platform noticed and
    told the boss — which is backwards. The team is what should fix it, and
    creating the check is cheap at the start and awkward once five tasks are
    already merged on trust.
    """
    return (
        "\n## Making your work checkable\n\n"
        "The platform runs whatever check this repository declares — an npm "
        "`test`, `build` or `lint` script, a pytest suite, `go test`, `cargo "
        "test`, or a `test:` Makefile target — and gives you the exit code as "
        "EVIDENCE rather than the worker's claim. It refuses to merge a branch "
        "that fails one.\n"
        "If the repository declares none of those, that safeguard does nothing "
        "and you are judging prose. So if there is no check yet, make creating "
        "one an early task — before the work it is supposed to protect. Even a "
        "thin one earns its keep: for a browser project a `lint` or `build` "
        "script that fails on a syntax error catches the exact fault that turns "
        "a game into a blank screen.\n"
        "This is cheap now and awkward after five tasks have been merged on "
        "trust.\n")


def sprint_review_prompt(project_id: int, sprint_no: int) -> tuple[str, list[str]]:
    """What to ask the boss at the end of a sprint, and the answers to offer.

    A digest filed as a GitHub issue tells the boss what happened. It does not ask
    them anything, so a run of six sprints was six unilateral decisions with a
    receipt after each one. The point of a sprint boundary is that it is the
    cheapest moment to change direction — the next sprint has not been planned yet,
    so redirecting costs nothing, whereas the same correction three sprints later
    means discarding built work.
    """
    tasks = [t for t in db.list_tasks(project_id) if t["sprint"] == sprint_no]
    done = [t for t in tasks if t["status"] == "done"]
    failed = [t for t in tasks if t["status"] == "failed"]
    shipped = "; ".join(f"#{t['seq']} {t['title']}" for t in done[:10]) or "nothing"
    note = f" {len(failed)} task(s) failed." if failed else ""
    return (
        f"Sprint {sprint_no} is done. Shipped: {shipped}.{note}\n\n"
        f"I am about to plan the next sprint. Anything you want changed, dropped, "
        f"or prioritised before I do?",
        ["Carry on — you decide the next sprint",
         "I have notes, let me type them",
         "Stop here, this is enough"],
    )


def sprint_gate(project_id: int, summary: str = "") -> str:
    """If cycles remain, roll into the next one and say what to do; else "" to finish.

    The whole point of asking for N sprints up front is that the boss does not have
    to come back N times. So a completed sprint hands the manager its own next brief
    — decide what the product still needs, and build that — rather than stopping and
    waiting for someone to type "now do more".
    """
    p = db.get_project(project_id)
    if not p or p["sprint"] >= p["sprints"]:
        return ""
    shipped = [t for t in db.list_tasks(project_id)
               if t["sprint"] == p["sprint"] and t["status"] == "done"]
    n = db.advance_sprint(project_id)
    db.set_project_status(project_id, "running", "")
    bus.emit(project_id, None, "manager", "sprint_finished",
             {"sprint": n - 1, "of": p["sprints"], "delivered": len(shipped),
              "summary": summary[:400]})
    # "Six sprints ran overnight, here is what came out" is the whole reason for
    # asking for six sprints. Fire and forget — a digest must never block delivery.
    try:
        import asyncio as _a
        from . import notify
        _a.get_event_loop().create_task(notify.sprint_digest(project_id, n - 1))
    except Exception:
        pass
    delivered = "; ".join(f"#{t['seq']} {t['title']}" for t in shipped[:12]) or "nothing"
    return (
        f"SPRINT {n - 1} OF {p['sprints']} IS COMPLETE — do NOT finish the project.\n"
        f"Delivered this sprint: {delivered}.\n\n"
        f"You are now running SPRINT {n}. Plan it yourself; do not ask the boss what "
        f"to build unless you are truly blocked.\n"
        f"1. Look at what exists now and judge it as a demanding product owner would: "
        f"what is missing, thin, unproven, slow, ugly, or fragile?\n"
        f"2. Pick the {2 if p['sprints'] > 3 else 3}-4 highest-value improvements — real "
        f"advances, not busywork, and not a restatement of what already shipped.\n"
        f"3. Call add_tasks with them, with dependencies where the order matters.\n"
        f"Prefer things a user would notice. If the last sprint left known gaps or "
        f"escalations unresolved, those come first.")


def _task_line(t: dict) -> str:
    deps = json.loads(t["deps"] or "[]")
    dep_note = f" deps={_seq_of(deps)}" if deps else ""
    return (f"task {t['seq']} [{t['role']}] '{t['title']}' status={t['status']}{dep_note} "
            f"attempts={t['attempts']} pr={t['pr_number'] or '-'}")


# project_id -> {handlers, escalate, ask_impl}. Populated by build_team_server so
# tests can drive the manager's tools; never part of the SDK payload.
HANDLERS: dict[int, dict] = {}


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
                width = ambition.get(project(), "contest_max_width",
                                     int(tuning.get("contest_max_width")))
                db.update_task(task_id, compete=min(compete, width))
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
            gh = github_client.token_for(project())
            if github_client.enabled(repo, gh):
                try:
                    n = await github_client.create_issue(
                        repo, f"[{item['role']}] {item['title']}",
                        item["description"] + f"\n\n_devteam task {task_id}_", token=gh)
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
        # Idle capacity, because you are the only part of the system that can do
        # anything about it. The scheduler cannot invent work — two agents on one
        # branch collide — so unused slots stay unused until you widen the plan.
        cap = scheduler.idle_capacity(project_id, p)
        lines = [head]
        if cap["free_slots"] and any(t["status"] == "planned" for t in tasks) is False:
            who = ", ".join(f"{a['name']} ({a['role']})" for a in cap["idle"]) or "nobody named"
            lines.append(
                f"CAPACITY: {cap['free_slots']} worker slot(s) free and {who} idle, with "
                f"nothing planned to give them. If there is genuinely independent work "
                f"left in this sprint, add_tasks now rather than letting the slots idle.")
        return _text("\n".join(lines + [_task_line(t) for t in tasks]))

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
            stuck = [t for t in db.list_tasks(project_id)
                     if t["status"] == "failed" and t["attempts"] >= 3]
            if stuck and project().get("autonomy") != "autonomous":
                names = ", ".join(f"#{t['seq']} {t['role']}" for t in stuck[:3])
                note = (f"STUCK — NEEDS YOUR BOSS'S JUDGEMENT: {names} has failed "
                        f"{stuck[0]['attempts']}+ times. Repeated failure is rarely fixed "
                        f"by another retry; something is wrong with the task, the tooling "
                        f"or the plan. Use ask_boss with concrete options (re-scope it, "
                        f"drop it, change approach) rather than retrying again.")
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

        def _opt_text(o) -> str:
            """A choice the boss can actually read on a button.

            Models legitimately answer with structured options like
            {"label": "Merge & proceed", "detail": "..."} — flatten them here, once,
            so the stored row and the broadcast event never disagree.
            """
            if isinstance(o, dict):
                label = o.get("label") or o.get("option") or o.get("title") or ""
                detail = o.get("detail") or o.get("description") or o.get("why") or ""
                if label and detail:
                    return f"{label} — {detail}"
                return str(label or detail or json.dumps(o, ensure_ascii=False))
            return str(o)

        opts = [_opt_text(o)[:400] for o in opts][:4]
        db.abandon_questions(project_id)   # a new question supersedes any older one
        qid = db.ask_question(project_id, args["question"], opts)
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
                # Deliberately no event here. The route that took the boss's answer
                # already emitted one ('answered' when they click an option,
                # 'directive' when they type). Emitting a second, differently-worded
                # copy under source="boss" made the feed show the boss's own words
                # twice — the echo reading "The boss replied: …" as though they had
                # said it again. The return value below is for the model, not the UI.
                return _text(f"The boss answered: {q['answer']}")
            # The boss may reply by typing a message instead of clicking an option —
            # treat any directive that arrives while we wait as the answer.
            directives = db.take_directives(project_id)
            if directives:
                db.answer_question(qid, "; ".join(directives))
                db.set_project_status(project_id, "running" if prev_status == "hold" else prev_status)
                return _text("The boss replied: " + "; ".join(directives))
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

    async def _sprint_checkin(sprint_no: int) -> str:
        """Offer the boss the wheel at a sprint boundary — without stopping for it.

        This deliberately does not go through _escalate. Those are safety gates:
        accepting undelivered work, merging past a red build. Blocking for an hour
        is the right behaviour there, because proceeding wrongly is worse than not
        proceeding at all. A sprint review is the opposite — it is a courtesy, and
        a courtesy that stalls an unattended overnight run for an hour per sprint
        has destroyed the thing the boss actually asked for.

        So the question is posted and the manager carries on. The answer is not
        lost: whatever the boss types becomes a directive, and directives are
        consumed at the manager's next decision point anyway. Set
        `sprint_checkin_seconds` above zero to make it genuinely wait.
        """
        q, options = sprint_review_prompt(project_id, sprint_no)
        wait = int(tuning.get("sprint_checkin_seconds"))
        qid = db.ask_question(project_id, q, options, topic="sprint_review")
        bus.emit(project_id, None, "manager", "boss_question",
                 {"question": q, "topic": "sprint_review"})
        bus.emit(project_id, None, "manager", "sprint_checkin",
                 {"sprint": sprint_no, "blocking_for": wait})
        deadline = time.time() + wait
        while time.time() < deadline:
            row = db.get_question(qid)
            if row and row["status"] == "answered":
                break
            await asyncio.sleep(2)
        row = db.get_question(qid)
        ans = (row or {}).get("answer") or ""
        if not ans:
            # Left on the board rather than abandoned: the boss may answer during
            # the next sprint, and it is still worth reading when they do.
            return ""
        low = ans.lower()
        if "stop" in low or "enough" in low:
            db.set_sprints(project_id, sprint_no)
            bus.emit(project_id, None, "boss", "sprints_ended_early", {"after": sprint_no})
            return ""
        if "carry on" in low:
            return ""
        return (f"\n\nYOUR BOSS'S NOTES ON THE LAST SPRINT — these outrank your own "
                f"judgement about what comes next:\n{ans}")

    ask_impl: dict = {}      # filled once ask_boss exists; swappable in tests

    async def _escalate(kind: str, question: str, options: list[str]) -> str | None:
        """Stop and get a human on a decision that papers over a problem.

        These are the moments where a confident manager does real damage: accepting
        work that was never delivered, merging past failing tests, giving up on a
        task after repeated failure. They are not product decisions, so the normal
        "ask about scope" rule never caught them — in this project's own history the
        manager silently substituted its own work for the team's on a WRONG premise.

        Supervised: block until the boss answers. Autonomous: proceed (they asked for
        that) but emit a loud, auditable event so it can be reviewed afterwards.
        Returns the boss's answer, or None when it may proceed.
        """
        autonomous = (project().get("autonomy") == "autonomous")
        bus.emit(project_id, None, "manager", "judgement_call",
                 {"kind": kind, "question": question, "autonomous": autonomous})
        if autonomous:
            return None          # full autonomy: on the record, but not blocking
        # @tool wraps the function in an SdkMcpTool, which is not directly callable —
        # reach the underlying coroutine.
        fn = ask_impl.get("fn")
        if fn is None:
            return None
        res = await fn({"question": question, "options_json": json.dumps(options[:4])})
        return res["content"][0]["text"]

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
        return _text(_with_evidence(t, t["report"] or "(no report yet)"))

    @tool("interview_boss", "Ask the boss about their brief BEFORE you plan it. Use "
          "this once, first, on any brief with real ambiguity in it. It returns their "
          "answers, which you then plan from. Skip it only when the brief genuinely "
          "leaves you nothing to decide.",
          {})
    async def interview_boss(args: dict[str, Any]) -> dict[str, Any]:
        """Before planning, not alongside it.

        The plan is a dependency graph whose SHAPE depends on the answers, so a
        late answer either invalidates the graph — wasting exactly what running
        them in parallel would have saved — or gets quietly ignored. And the
        scheduler dispatches whatever is unblocked, so by the time an answer
        arrived a worker could already have built the wrong thing.
        """
        p = project()
        if interview.stored(project_id).get("questions") is not None:
            return _text("you have already done this — plan the work now.")
        from . import auth as auth_mod
        owner = auth_mod.get_user(p.get("owner_id") or 0)
        qs = await interview.draft(p, auth_mod.get_settings(owner) if owner else {})
        if not qs:
            interview.record(project_id, [], "")
            return _text("Nothing worth asking — the brief is clear enough. Plan it.")

        bus.emit(project_id, None, "manager", "interview_started",
                 {"questions": [q["ask"] for q in qs]})
        wait = int(tuning.get("interview_wait_seconds"))
        if p.get("autonomy") == "autonomous":
            wait = 0     # they asked for unattended; do not stall the start
        qid = db.ask_question(project_id, interview.as_message(qs),
                              ["Answer below", "Just get on with it"],
                              topic="interview")
        bus.emit(project_id, None, "manager", "boss_question",
                 {"question": interview.as_message(qs), "topic": "interview"})
        deadline = time.time() + wait
        while time.time() < deadline:
            row = db.get_question(qid)
            if row and row["status"] == "answered":
                break
            for d in db.take_directives(project_id):
                db.answer_question(qid, d)
                break
            await asyncio.sleep(3)
        row = db.get_question(qid) or {}
        answer = row.get("answer") or ""
        if "get on with it" in answer.lower():
            answer = ""
        if not answer:
            db.abandon_questions(project_id)
        bus.emit(project_id, None, "manager", "interview_done",
                 {"answered": bool(answer)})
        return _text(interview.record(project_id, qs, answer))

    @tool("plan_sprints", "Set how many delivery cycles this project needs, once you "
          "understand the brief. Do this early: the boss's number was a guess before "
          "anyone looked at the work. Lowering it below the sprint you are on is "
          "refused. Say why in `reason` — it is shown to the boss.",
          {"sprints": int, "reason": str})
    async def plan_sprints(args: dict[str, Any]) -> dict[str, Any]:
        """The boss picks a sprint count at project creation, before a single line
        has been read. That is the worst-informed moment in the project's life to
        be making the call, and the manager is the only party that ever learns
        enough to correct it."""
        p = project()
        want = max(1, min(20, int(args.get("sprints") or 1)))
        current = p.get("sprint", 1)
        if want < current:
            return _text(f"error: you are already in sprint {current}; you cannot plan "
                         f"fewer than that. Finish, or set {current} to make this the last.")
        was = p.get("sprints", 1)
        if want == was:
            return _text(f"already set to {was} sprint(s); nothing changed.")
        db.set_sprints(project_id, want)
        bus.emit(project_id, None, "manager", "sprints_replanned",
                 {"from": was, "to": want, "reason": (args.get("reason") or "")[:300]})
        return _text(f"sprints changed from {was} to {want}. Reason recorded for the boss.")

    @tool("discuss_work", "Ask your team what they think of a finished task before you "
          "decide. They review independently and you get the split, including anyone who "
          "disagrees. Worth it on anything substantial; skip it on trivial work.",
          {"task_id": int})
    async def discuss_work(args: dict[str, Any]) -> dict[str, Any]:
        """Aggregating several independent readings is where multi-agent review
        earns its cost — worth several times more than adding diversity for its
        own sake. Running rival implementations and then having one person skim
        the winner spends the money on the part that pays least."""
        t = db.resolve_task(project_id, int(args["task_id"]))
        if not t:
            return _text("error: no such task")
        if not (t.get("report") or "").strip():
            return _text("error: there is nothing to review yet — that task has not "
                         "reported.")
        from . import auth as auth_mod
        owner = auth_mod.get_user(project().get("owner_id") or 0)
        settings = auth_mod.get_settings(owner) if owner else {}
        result = await review.panel(project_id, t, settings)
        return _text(review.summarise(result))

    @tool("coach_teammate", "Change how one of your team members works — their standing "
          "instructions, not a task. Use when someone keeps making the same class of "
          "mistake, or when the work turns out to need a different temperament than you "
          "hired for. Takes effect on their NEXT task, not the one they are on now.",
          {"name": str, "persona": str})
    async def coach_teammate(args: dict[str, Any]) -> dict[str, Any]:
        """Persona changes belong to whoever is watching the work, and that is the
        manager far more often than the boss. Restricted to standing instructions
        on purpose: per-task direction already has request_changes, and a manager
        that could rewrite someone's character to push one task through would be
        editing the record of why the work turned out as it did."""
        who = (args.get("name") or "").strip()
        people = db.list_agents(project_id)
        match = next((a for a in people if a["name"].lower() == who.lower()), None)
        if not match:
            names = ", ".join(a["name"] for a in people) or "nobody yet"
            return _text(f"error: no teammate called '{who}'. Your team: {names}")
        team.set_persona(match["id"], args.get("persona", ""))
        bus.emit(project_id, None, "manager", "coached",
                 {"name": match["name"], "role": match["role"]})
        return _text(f"{match['name']} ({match['role']}) briefed. It applies from their "
                     f"next task — the one they are on now already has its instructions.")

    @tool("request_changes", "Send a task back to its team member with specific feedback. "
          "The scheduler re-runs it on the same branch automatically. After 2 failed "
          "rounds the task escalates to a stronger model.",
          {"task_id": int, "feedback": str})
    async def request_changes(args: dict[str, Any]) -> dict[str, Any]:
        t = db.resolve_task(project_id, int(args["task_id"]))
        task_id = t["id"] if t else -1
        if not t:
            return _text("error: no such task")
        # Sending work back marks the task 'planned', and the scheduler dispatches
        # anything planned — so if an agent is STILL WORKING on it, a second one
        # starts beside the first. Both then push to the same branch and both
        # report, and whichever lands last overwrites the other's outcome. That
        # happened on task #7 of the mars-rover run. Retire the old worker first.
        from . import launcher
        if str(task_id) in launcher.ACTIVE:
            launcher.kill_task(task_id, "superseded: the manager sent this work back "
                                        "while the agent was still on it")
        db.update_task(task_id, feedback=args["feedback"], status="planned")
        db.judge_run(task_id, "rework", args["feedback"])
        bus.emit(project_id, task_id, "manager", "changes_requested",
                 {"feedback": args["feedback"]})
        repo = project().get("repo", "")
        gh = github_client.token_for(project())
        if github_client.enabled(repo, gh) and t["pr_number"]:
            try:
                await github_client.comment_issue(repo, t["pr_number"], args["feedback"], token=gh)
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
        # Accepting a task that produced nothing is how a project silently ships a
        # hole. It is also how this project once "completed" a research task the
        # team never actually delivered.
        delivered = bool((t["report"] or "").strip()) or t["pr_number"] or \
            bool(db.list_contenders(t["id"]))
        if not delivered:
            ans = await _escalate(
                "accepting undelivered work",
                f"Task #{t['seq']} ({t['role']}: {t['title'][:60]}) produced no report, "
                f"no PR and no rival output after {t['attempts']} attempt(s). I can close "
                f"it as done anyway, or keep pushing. Closing it means the project ships "
                f"without that work. How do you want to handle it?",
                ["Close it as done anyway", "Retry it once more",
                 "Reassign it to a stronger model", "Stop and let me look"])
            if ans and "stop" in ans.lower():
                return _text(f"Held at your request: {ans}. Task #{t['seq']} left as is.")
        db.update_task(t["id"], status="done")
        db.judge_run(t["id"], "accepted", "accepted by the manager")
        team.release(t, t.get("report") or "", accepted=True)
        bus.emit(project_id, t["id"], "manager", "task_accepted",
                 {"verdict": args.get("verdict", "")})
        scheduler.ensure(project_id)   # accepting unblocks dependents; wake the loop
        return _text(f"task {t['seq']} accepted and marked done.")

    @tool("merge_pr", "Squash-merge a task's pull request. Merging unblocks dependent "
          "tasks. Refused when the project's own tests/build failed on that branch — the "
          "platform runs them itself, so that result is evidence, not the worker's claim. "
          "override_failed_tests is a last resort and needs the boss's agreement first.",
          {"task_id": int, "override_failed_tests": bool})
    async def merge_pr(args: dict[str, Any]) -> dict[str, Any]:
        # A staging instance opens pull requests and stops there. Enforced here
        # rather than by prompt, because "please do not merge" is a request and
        # this is a rule.
        t = db.resolve_task(project_id, int(args["task_id"]))
        repo = project().get("repo", "")
        if not t:
            return _text("error: no such task")
        # Checked against the TARGET repository, not just against the instance.
        # A staging instance merging a pull request on a throwaway project in a
        # scratch repository is harmless and is worth rehearsing; the same
        # instance merging into the repository this platform is built from is
        # not. Only the second is refused.
        allowed, why = config.may_merge(repo)
        if not allowed:
            return _text(f"REFUSED: {why} Continue with other work.")
        # Hard gate: never merge work that failed the project's own checks. This is
        # the one judgement that does not depend on reading prose, so it is not left
        # to persuasion — an override needs the boss, not a convincing report.
        v = verification_of(t)
        if v.get("ran") and not v.get("ok") and args.get("override_failed_tests"):
            ans = await _escalate(
                "merging past failing tests",
                f"Task #{t['seq']}: `{v.get('cmd')}` exits {v.get('exit_code')} on this "
                f"branch, and I want to merge anyway because I believe the check itself "
                f"is at fault, not the code. Merging puts failing code on main. Agreed?",
                ["Yes, merge it — the check is wrong", "No, send it back to be fixed",
                 "Stop and let me look"])
            if ans and ("no" in ans.lower() or "stop" in ans.lower()):
                return _text(f"Not merged, at your request: {ans}")
        if v.get("ran") and not v.get("ok") and not args.get("override_failed_tests"):
            return _text(
                f"REFUSED: `{v.get('cmd')}` exited {v.get('exit_code')} on this branch. "
                f"The worker's report may claim success — the exit code says otherwise, "
                f"and the exit code is the evidence.\n\n"
                f"--- output (tail) ---\n{(v.get('output') or '')[-1200:]}\n\n"
                f"Send it back with request_changes quoting this output. If you truly "
                f"believe the check itself is broken (not the code), ask the boss first "
                f"with ask_boss, then retry with override_failed_tests=true.")
        gh = github_client.token_for(project())
        if not github_client.enabled(repo, gh) or not t["pr_number"]:
            db.update_task(t["id"], status="done")
            db.judge_run(t["id"], "accepted", "no PR; accepted")
            return _text("no PR to merge; task marked done.")
        ok = await github_client.merge_pr(repo, t["pr_number"], token=gh)
        if ok:
            db.update_task(t["id"], status="done")
            db.judge_run(t["id"], "accepted", "merged")
            if t["issue_number"]:
                try:
                    await github_client.close_issue(repo, t["issue_number"], token=gh)
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
            # A finished sprint is not a finished product. If cycles remain, the boss
            # asked for iteration, not for one pass — so keep going without them.
            # The cheapest moment to change direction is right here: the next
            # sprint is not planned yet, so a correction costs nothing, while the
            # same correction three sprints later means discarding built work.
            steer = ""
            p = project()
            if p.get("sprint", 1) < p.get("sprints", 1):
                steer = await _sprint_checkin(p.get("sprint", 1))
            nxt = sprint_gate(project_id, args.get("summary", ""))
            if nxt:
                return _text(nxt + steer)
        db.set_project_status(project_id, s, args.get("summary", ""))
        bus.emit(project_id, None, "manager", "project_finished", {"status": s})
        # A routine self-repair finishing is the thing the operator wanted to SEE
        # without being asked about. Recorded distinctly so the Improve page can
        # show "the platform fixed these itself" rather than burying it in a feed.
        if s == "done" and (db.get_project(project_id) or {}).get("is_self"):
            done = [t for t in db.list_tasks(project_id) if t["status"] == "done"]
            bus.emit(project_id, None, "system", "self_healed",
                     {"summary": args.get("summary", "")[:300],
                      "fixed": [f"#{t['seq']} {t['title']}" for t in done[-5:]]})
        return _text(f"project marked {s}. You can stop now.")

    ask_impl["fn"] = ask_boss.handler

    srv = create_sdk_mcp_server(
        name="team", version="1.0.0",
        tools=[create_tasks, add_tasks, status, wait, ask_boss, reply_to_boss, get_report,
               request_changes, reassign_task, compare_work, pick_winner,
               accept_task, merge_pr, coach_teammate, discuss_work,
               plan_sprints, interview_boss, finish],
    )
    # Testing seam. This must NOT live on the returned dict: that dict is handed to
    # the SDK, which serialises the server config to JSON for the CLI subprocess —
    # putting functions on it breaks every real run with "Object of type function is
    # not JSON serializable". Park it in a module-level registry instead.
    HANDLERS[project_id] = {
        "handlers": {t.name: t.handler for t in
                     (create_tasks, add_tasks, status, wait, ask_boss, reply_to_boss,
                      get_report, request_changes, reassign_task, compare_work,
                      pick_winner, accept_task, merge_pr, coach_teammate,
                      discuss_work, plan_sprints, interview_boss, finish)},
        "escalate": _escalate,
        "ask_impl": ask_impl,
    }
    return srv


MANAGER_TOOLS = [f"mcp__team__{n}" for n in
                 ("create_tasks", "add_tasks", "status", "wait", "ask_boss",
                  "reply_to_boss", "get_report", "request_changes", "reassign_task",
                  "compare_work", "pick_winner", "accept_task", "merge_pr",
                  "coach_teammate", "discuss_work", "plan_sprints",
                  "interview_boss", "finish")]

BUILTIN_TOOLS_OFF = ["Bash", "Read", "Write", "Edit", "Glob", "Grep",
                     "WebSearch", "WebFetch", "Task", "NotebookEdit", "TodoWrite"]


async def run_manager(project_id: int) -> None:
    project = db.get_project(project_id)
    if not project:
        return
    if config.DEMO_MODE:
        from . import demo
        await demo.run_manager(project_id)
        return
    bus.emit(project_id, None, "manager", "agent_status", {"status": "starting"})

    roster = json.loads(project.get("team") or "[]")
    roster_text = ""
    if roster:
        # Name the people where there are people to name. A manager told "2×
        # backend" can only address the role; a manager told "Priya and Marco are
        # your backend engineers" can send work back to whoever actually wrote it,
        # which is the difference between review and re-assignment.
        named = team.roster_text(project_id)
        lines = named or ", ".join(f"{m['count']}× {m['role']}" for m in roster)
        roster_text = (
            f"\nThe boss recruited this starting team:\n{lines}\n"
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
    sprints, sprint = project.get("sprints", 1), project.get("sprint", 1)
    if sprints > 1:
        done_before = [t for t in db.list_tasks(project_id)
                       if t["sprint"] < sprint and t["status"] == "done"]
        sprint_text = (
            f"\nDELIVERY MODEL: {sprints} sprints. You are running SPRINT {sprint}.\n"
            "Each sprint is a full cycle: decide what to build, build it, verify it, ship it. "
            "You own the requirements — work out what the product needs and commit to it "
            "rather than asking the boss to specify it. When a sprint's work is all merged, "
            "call finish; if cycles remain you will be told to plan the next one instead of "
            "stopping. The boss asked for these cycles precisely so they do not have to come "
            "back between them.\n")
        if sprint > 1:
            shipped = "; ".join(f"#{t['seq']} {t['title']}" for t in done_before[:15])
            sprint_text += (
                f"Already delivered in earlier sprints: {shipped or 'nothing'}.\n"
                "Do NOT rebuild any of that. This sprint must move the product forward.\n")
    else:
        sprint_text = ""
    prompt = (
        f"Project: {project['name']}\n"
        f"Repository: {project['repo'] or '(none configured)'}\n"
        f"Max parallel workers: {project['max_workers']} | "
        f"Agent-run cap: {project['max_runs']}\n"
        f"{sprint_text}"
        f"{autonomy_text}\n"
        f"{role_catalog_text(project)}\n"
        f"{roster_text}\n"
        f"{process.guidance(project)}\n"
        f"{ambition.guidance(project)}\n"
        f"{verification_brief(project)}\n"
        f"Brief from the user:\n{project['brief']}\n\n"
        "Plan the work, run your team, and ship it. This may be a restarted session: "
        "call status first, and only create_tasks if none exist yet.\n"
        "If there are no tasks yet, call interview_boss BEFORE create_tasks — the "
        "shape of your plan depends on what they tell you, so asking afterwards "
        "means either redoing the plan or ignoring the answer."
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
    if project["repo"] and github_client.enabled(project["repo"],
                                                  github_client.token_for(project)):
        try:
            ok, note = await github_client.ensure_repo(
                project["repo"], token=github_client.token_for(project))
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
                from . import usage
                # The shared-quota meter, not the project bill: it is what tells the
                # self-repair crew that a human is using the subscription right now.
                cost = usage.note_result("manager", options.model or "", message)
                db.add_project_cost(project_id, cost)
                bus.emit(project_id, None, "manager", "result", {"cost_usd": cost})
    except Exception as e:
        # Surface the model/API's own words (e.g. "Credit balance is too low")
        # instead of the SDK's generic wrapper message.
        detail = last_text or str(e)
        db.abandon_questions(project_id)   # nothing is waiting on them now
        bus.emit(project_id, None, "manager", "error", detail)
        db.set_project_status(project_id, "failed", f"manager session failed: {detail[:400]}")
        # The project is now dead and nothing else will move it. Unattended, this
        # is the fault that costs a whole night, so it is the one worth waking for.
        try:
            from . import notify
            await notify.report_error("manager crashed", detail,
                                      {"project": project.get("name", "?"),
                                       "project_id": project_id})
        except Exception:
            pass
        return
    finally:
        db.abandon_questions(project_id)   # session over — clear any unanswered ask

    # If the manager ended without calling finish, flag for human review.
    fresh = db.get_project(project_id)
    if fresh and fresh["status"] not in ("done", "failed", "cancelled"):
        db.set_project_status(project_id, "review",
                              "manager session ended without finish(); needs human review")
        bus.emit(project_id, None, "system", "needs_review", {})
