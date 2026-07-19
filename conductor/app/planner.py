"""Suggest a starting team composition ("recruiting") from a project brief.

Runs one quick Claude query (no tools) to propose roles + counts + model tier, so
the boss sees a sensible pre-filled team to tweak. Falls back to a keyword heuristic
if the model call fails or returns junk, so the recruiting step never blocks.
"""

import json
import re

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

from . import config

_ALLOWED_MODELS = {"worker", "lead"}


def _known_roles() -> list[str]:
    return [r["name"] for r in config.load_roles()]


def _heuristic(brief: str) -> list[dict]:
    b = brief.lower()
    team: list[dict] = []
    wants_backend = any(k in b for k in
                        ("api", "backend", "server", "database", "endpoint", "auth", "fastapi", "express"))
    wants_frontend = any(k in b for k in
                         ("ui", "frontend", "page", "web app", "html", "react", "dashboard", "interface", "css"))
    if wants_backend or not wants_frontend:
        team.append({"role": "backend", "count": 1, "model": "worker"})
    if wants_frontend:
        team.append({"role": "frontend", "count": 1, "model": "worker"})
    team.append({"role": "tester", "count": 1, "model": "worker"})
    return team


def _sanitize(items: list, brief: str) -> list[dict]:
    known = set(_known_roles())
    out: list[dict] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or "role" not in it:
            continue
        role = str(it["role"]).strip().lower().replace(" ", "-")[:32]
        if not role:
            continue
        count = it.get("count", 1)
        count = count if isinstance(count, int) and 1 <= count <= 8 else 1
        model = it.get("model", "worker")
        model = model if model in _ALLOWED_MODELS else "worker"
        out.append({"role": role, "count": count, "model": model,
                    "known": role in known})
    return out or _heuristic(brief)


async def suggest_team(brief: str) -> list[dict]:
    known = ", ".join(_known_roles())
    prompt = (
        "You are a staffing planner for an autonomous software team. Given a project brief, "
        "propose the starting team as a JSON array of objects with keys: role (lowercase, "
        f"e.g. one of [{known}] or a new role name you invent if the work needs it), count "
        "(how many of that role, 1-4), and model ('worker' = cheap/fast for routine work, "
        "'lead' = stronger/pricier for hard design or research). Recruit more of a role only "
        "when the work has independent parallelizable parts. Always include at least one tester. "
        "Keep the team lean and appropriate to the brief. Output ONLY the JSON array.\n\n"
        f"Brief:\n{brief}"
    )
    try:
        options = ClaudeAgentOptions(
            model=config.WORKER_MODEL, max_turns=1,
            disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep",
                              "WebSearch", "WebFetch", "Task", "TodoWrite"],
            permission_mode="bypassPermissions",
        )
        text = ""
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        text += b.text
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            return _sanitize(json.loads(m.group(0)), brief)
    except Exception:
        pass
    return _heuristic(brief)
