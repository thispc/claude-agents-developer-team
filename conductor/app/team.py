"""Teammates that persist, rather than roles that get re-staffed every task.

A role used to be a label on a task. Every dispatch created a brand-new session
with the role prompt and nothing else, so "the backend engineer" was not a
teammate — it was a costume worn by a stranger each time. Three things the plan
asks for were impossible to express as a result:

- **A persona per agent.** There was one persona field on the whole project, for
  the manager. You could not say what kind of engineer you wanted, only what kind
  of manager.
- **Changing a persona later.** Nothing to change it on.
- **Continuity.** An agent that built the API had no memory of it when it came
  back to extend the API, so it re-derived its own decisions and sometimes
  contradicted them.

An `agents` row fixes all three by being the durable thing a task is assigned to.
It is deliberately thin: a name, a persona, a provider/model, and a short set of
carry-forward notes. It is *not* a conversation history — the notes are capped
hard, because the failure mode of "remember everything" is a context window that
grows until it crowds out the actual task, and an agent that spends its budget
re-reading its own past is worse than one with no memory at all.

Naming is not decoration. A manager that says "send it back to Priya" and a feed
that shows Priya picking it up are describing one continuous worker; "backend
agent #2" describes an interchangeable slot, and the interchangeability was the
bug.
"""

import json
import random
from typing import Any

from . import bus, config, db

# A fixed, ordered pool so a project's third backend engineer is the same name on
# every restart and in every replay of the event feed. Drawn from several naming
# traditions on purpose: the team a user reads about should not look like it came
# from one place. Names only — the platform never assigns a teammate a gender, and
# nothing in the prompts or the UI refers to a teammate as anything but "they".
NAMES = [
    "Priya", "Marco", "Sena", "Tobias", "Amara", "Kenji", "Rosa", "Idris",
    "Lena", "Mateo", "Nour", "Silas", "Anika", "Dara", "Yuki", "Beatriz",
    "Omar", "Freya", "Tariq", "Ines", "Kwame", "Mira", "Joon", "Elif",
]

# Hard cap on carry-forward notes. Small on purpose: this is a reminder of what
# this teammate did, not a transcript. Anything longer competes with the task.
NOTES_LIMIT = 1200


def _name_for(project_id: int, taken: set[str]) -> str:
    for n in NAMES:
        if n not in taken:
            return n
    # More teammates than names is not an error, just unusual. Suffix rather than
    # fail: a project cannot be blocked from hiring because we ran out of nouns.
    return f"{random.choice(NAMES)}-{len(taken) + 1}"


def hire(project_id: int, roster: list[dict]) -> list[dict]:
    """Turn a roster — [{role, count, model, provider, persona}] — into named people.

    Idempotent per role: re-running with the same roster does not duplicate anyone,
    because recruiting happens on project creation AND whenever the boss edits the
    team, and a second edit must not silently double the payroll.
    """
    existing = db.list_agents(project_id)
    taken = {a["name"] for a in existing}
    by_role: dict[str, list[dict]] = {}
    for a in existing:
        by_role.setdefault(a["role"], []).append(a)

    hired = []
    for member in roster or []:
        role = (member.get("role") or "").strip()
        if not role:
            continue
        want = max(1, int(member.get("count") or 1))
        have = by_role.get(role, [])
        for i in range(len(have), want):
            name = _name_for(project_id, taken)
            taken.add(name)
            aid = db.create_agent(
                project_id, name, role, idx=i + 1,
                persona=(member.get("persona") or "").strip(),
                provider=(member.get("provider") or "anthropic"),
                model=(member.get("model") or ""))
            hired.append(db.get_agent(aid))
    if hired:
        bus.emit(project_id, None, "system", "team_hired",
                 {"who": [{"name": a["name"], "role": a["role"]} for a in hired]})
    return hired


def assign(task: dict) -> dict | None:
    """Which teammate picks this task up.

    Preference order, and the order is the point:

    1. Whoever already worked this task. A retry going to a different person
       throws away everything the first attempt learned, which is exactly the
       waste `prior_attempt()` was added to stop.
    2. An idle teammate in the right role, least-loaded first, so one person does
       not absorb the whole project while colleagues sit out.
    3. The least-loaded teammate in the role even if busy — better a queue behind
       someone real than a nameless session.

    Returns None when the project has no team rows at all, which is the case for
    every project created before this existed. Callers must treat that as "carry
    on as before" rather than an error; a migration that broke old projects to
    add a feature would be a bad trade.
    """
    if task.get("agent_id"):
        prior = db.get_agent(task["agent_id"])
        if prior:
            return prior
    candidates = db.list_agents(task["project_id"], task["role"])
    if not candidates:
        return None
    idle = [a for a in candidates if a["status"] == "idle"]
    pool = idle or candidates
    return sorted(pool, key=lambda a: (a["tasks_done"], a["idx"]))[0]


def claim(task: dict) -> dict | None:
    """Assign the task if it has no one on it yet, and mark that person busy."""
    agent = assign(task)
    if not agent:
        return None
    if task.get("agent_id") != agent["id"]:
        db.update_task(task["id"], agent_id=agent["id"])
    db.update_agent(agent["id"], status="busy")
    return agent


def release(task: dict, report: str = "", accepted: bool = False) -> None:
    """Hand the teammate back to the pool, and let them keep a little of what they
    just did.

    The note is written from the report's own summary rather than generated by a
    model: another inference call per task to produce two sentences nobody may
    ever read is not worth the money, and the report's closing summary is already
    the agent's own account of what it built.
    """
    if not task.get("agent_id"):
        return
    agent = db.get_agent(task["agent_id"])
    if not agent:
        return
    fields: dict[str, Any] = {"status": "idle"}
    if accepted:
        fields["tasks_done"] = agent["tasks_done"] + 1
        gist = _gist(report)
        note = f"- {task['title']}: {gist}"
        # Newest last, oldest dropped. A teammate on their tenth task should carry
        # what they built recently, not what they built first.
        notes = (agent["notes"] + "\n" + note).strip()
        fields["notes"] = notes[-NOTES_LIMIT:]
    db.update_agent(agent["id"], **fields)
    # If this teammate is a deployment of a Studio agent, file the same gist as a
    # global episode — FREE, no extra model call, it is the summary just computed.
    # A no-op for agents with no home_id, i.e. every project created before the
    # Studio existed.
    if accepted and agent.get("home_id"):
        from . import home
        home.record_episode(agent, "task_done", gist,
                            project_id=task.get("project_id"), task_id=task.get("id"))


def _gist(report: str) -> str:
    """One line of what happened, from the report's own words.

    The worker appends its push result after a `---` rule, so the genuinely last
    line of every report is "pushed branch task/17" — which is what this returned
    on the first live run, making every teammate's memory a list of branch names
    and nothing about what they built. The trailer is dropped first.
    """
    text = (report or "").strip()
    if not text:
        return "done"
    # Everything before the final horizontal rule: the agent's own summary.
    body = text.rsplit("\n---\n", 1)[0].strip() or text
    lines = [ln.strip("-*# ").strip() for ln in body.splitlines() if ln.strip()]
    # The last substantive line, skipping trailing chatter too short to mean
    # anything ("Done!", "Perfect."), which models emit freely.
    for line in reversed(lines):
        if len(line) >= 25:
            return line[:200]
    return (lines[-1] if lines else "done")[:200]


def set_persona(agent_id: int, persona: str) -> dict | None:
    """Change who someone is, mid-project.

    Takes effect on their next task rather than the one in flight, because the
    running session already has its system prompt and there is no way to amend it
    without killing work in progress.
    """
    agent = db.get_agent(agent_id)
    if not agent:
        return None
    db.update_agent(agent_id, persona=persona.strip())
    bus.emit(agent["project_id"], None, "system", "persona_changed",
             {"name": agent["name"], "role": agent["role"]})
    return db.get_agent(agent_id)


def system_addendum(agent: dict | None) -> str:
    """What gets appended to the role prompt so an agent knows who they are.

    Deliberately appended rather than substituted: the role prompt is the part
    that has been tuned and tested, and a persona is a modifier on it, not a
    replacement for it. A persona that could override "run the tests before you
    finish" would be a way to talk the platform out of its own safeguards.
    """
    if not agent:
        return ""
    parts = [f"\n\n## Who you are\n\nYou are {agent['name']}, the {agent['role']} on this "
             f"team. Your teammates and your manager refer to you by name."]
    if (agent.get("persona") or "").strip():
        parts.append(
            "\n\n### How you work (from your boss)\n\n" + agent["persona"] +
            "\n\nThis shapes your judgement and style. It does not override the "
            "instructions above about verifying your work or how to finish.")
    if (agent.get("notes") or "").strip():
        parts.append(
            "\n\n### What you have already built on this project\n\n" + agent["notes"] +
            "\n\nBuild on these decisions rather than re-litigating them. If you now "
            "think one was wrong, say so explicitly instead of quietly changing it — "
            "your teammates built against it.")
    # A Studio agent carries what it learned across ALL its projects, folded into a
    # bounded long-term memory. Retrieval is free — the blob is small and inlined,
    # there is nothing to search — and it is what makes "Mike Ross remembers project
    # A while working project B" true without a per-task lookup.
    if agent.get("home_id"):
        from . import memory
        blob = memory.current_blob(agent["home_id"])
        if blob.strip():
            parts.append(
                "\n\n### What you carry from your past work\n\n" + blob +
                "\n\nThis is your accumulated experience across projects. Draw on it "
                "where relevant; it is a reminder, not a rulebook.")
    return "".join(parts)


def model_for(agent: dict | None, fallback: str) -> str:
    """A teammate's own model, resolved to something a vendor will accept.

    The roster speaks in tiers — "worker", "lead" — and those are aliases, not
    model ids. Returning one unresolved sent a live worker off to ask for a model
    literally called "worker", which failed the whole task. An empty value means
    "use whatever the caller would have picked", so a project with no per-teammate
    choice behaves exactly as it did.

    NOTE: this answers "whose model is it", which is a different question from
    "what should this dispatch run on". Dispatch goes through `launcher.pick_model`,
    where a teammate's model sits BELOW an explicit reassignment and below
    escalation — otherwise a task that keeps failing keeps failing on the same
    model, and the manager's correction is silently discarded.
    """
    own = (agent or {}).get("model")
    return config._resolve_model(own) if own else fallback


def provider_for(agent: dict | None) -> str:
    return (agent or {}).get("provider") or "anthropic"


def describe(project_id: int) -> list[dict]:
    """The team, for the UI and for the manager's prompt."""
    out = []
    for a in db.list_agents(project_id):
        out.append({
            "id": a["id"], "name": a["name"], "role": a["role"],
            "persona": a["persona"], "provider": a["provider"], "model": a["model"],
            "status": a["status"], "tasks_done": a["tasks_done"],
            "notes": a["notes"],
        })
    return out


def roster_text(project_id: int) -> str:
    """The team as the manager should see it: people with names, not head counts.

    A manager told "2× backend" can only address the role. A manager told "Priya
    and Marco are your backend engineers" can send work back to whoever wrote it,
    which is the difference between review and re-assignment.
    """
    people = db.list_agents(project_id)
    if not people:
        return ""
    by_role: dict[str, list[dict]] = {}
    for a in people:
        by_role.setdefault(a["role"], []).append(a)
    lines = []
    for role, members in by_role.items():
        who = ", ".join(
            f"{m['name']}" + (f" ({m['model']})" if m["model"] else "") +
            (" — busy" if m["status"] == "busy" else "")
            for m in members)
        lines.append(f"- **{role}**: {who}")
    return "\n".join(lines)


def from_blueprint(blueprint: str | dict) -> list[dict]:
    """Pull a roster with personas out of a round-table blueprint.

    The round table already argues about what kind of team the idea needs, and
    that judgement used to be thrown away at the exact moment it mattered: the
    blueprint produced head counts, and the personas the seats had reasoned about
    went nowhere. Tolerant of shape because a blueprint is model-written JSON and
    the schema drifts; anything unparseable yields an empty roster rather than an
    exception, since a bad blueprint should not stop a project being created.
    """
    data = blueprint
    if isinstance(blueprint, str):
        try:
            data = json.loads(blueprint or "{}")
        except json.JSONDecodeError:
            return []
    if not isinstance(data, dict):
        return []
    raw = data.get("team") or data.get("roster") or []
    roster = []
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or m.get("name") or "").strip()
        if not role:
            continue
        roster.append({
            "role": role,
            "count": int(m.get("count") or 1),
            "model": m.get("model") or "",
            "provider": m.get("provider") or "anthropic",
            "persona": (m.get("persona") or m.get("summary") or "").strip(),
        })
    return roster
