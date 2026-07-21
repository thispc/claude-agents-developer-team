"""What each sprint produced, kept after the next sprint has run over it.

A project's output was only ever readable as "now": the repo tree, the open pull
requests, the task list in whatever state it had most recently been edited into.
That is fine for one delivery cycle and wrong for several, because a task row is
mutable — a sprint-2 rework changes the status, the report, the PR and the
verification of a sprint-1 task in place — and the repo has no memory of which
sprint put a file there. Sprint 1 did not disappear; it became unaskable.

So a finished sprint is *frozen*: the task facts as they read at the end of that
cycle, plus the repo's file list at that moment, written once into a row that
nothing later rewrites. Three properties are deliberate:

- **Frozen from records, never from prose.** Everything in a snapshot is copied
  from the task and team tables. Nothing here asks a model what happened, because
  a record of what shipped that can drift from what shipped is not a record.
- **Captured lazily, on read.** The sprint boundary lives in the manager session,
  and a snapshot that only exists when that code path runs would miss every
  project that already ran, every project whose manager died mid-cycle, and every
  project whose sprint was advanced by hand. Freezing "every sprint the project
  has already left" when someone looks is late but complete; hooking the boundary
  would be timely and full of holes.
- **Absence is not an error.** A project from before this existed has no rows, so
  reads fall back to computing the sprint live from tasks. The fallback is worse —
  it shows the task as it reads *now* — and it is labelled as such rather than
  presented as history.

Release notes are the same discipline applied to prose. The notes are assembled
from the frozen facts first; a model may then rewrite them into something
readable, but its output is checked against the record and thrown away whole if
it cites a task the sprint did not contain. A release note nobody can trace back
to a task is worse than no release note — it is a claim about your software with
a plausible tone and nothing underneath.
"""

import json
import re
from typing import Any

from . import db, github_client, providers

# Enough of an agent's own account to recognise the work, not enough to turn a
# release note into a transcript. The full report stays on the task.
GIST_CHARS = 240

# Statuses that mean the project is not coming back, so the sprint in progress is
# also finished — otherwise the last sprint of every completed project is the one
# sprint that never gets frozen. 'review' is deliberately absent: the boss can send
# a project in review back to work, and freezing a cycle that can still change is
# the one way this could record something that never shipped.
CLOSED = ("done", "failed", "cancelled")

# A task reference in prose, which is `#4` and not the `#12` in "PR #12".
_REF = re.compile(r"(?<!pr )(?<!issue )#(\d+)", re.IGNORECASE)

NOTES_SYSTEM = (
    "You are writing release notes for one sprint of a software project. You are "
    "given the complete record of what that sprint delivered. Write only what the "
    "record supports.\n\n"
    "Rules:\n"
    "- Every claim must name the task it came from, as #<number>, exactly as given.\n"
    "- Do not mention a task number that is not in the record.\n"
    "- Do not describe work as verified unless the record says its check passed.\n"
    "- Group related tasks into a sentence each. No preamble, no marketing, no "
    "  'we are excited to'. Six lines at most.\n"
    "- If the sprint delivered nothing, say that in one line."
)


def _gist(report: str) -> str:
    """The agent's own last word on the task — its closing summary is nearly always
    the final non-empty line, and its opening is a restatement of the brief."""
    lines = [ln.strip("-* ").strip() for ln in (report or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:GIST_CHARS]


def _verification(task: dict) -> dict[str, Any]:
    try:
        v = json.loads(task.get("verification") or "{}")
    except json.JSONDecodeError:
        v = {}
    return {"ran": bool(v.get("ran")), "ok": bool(v.get("ok")), "cmd": v.get("cmd") or ""}


def _item(task: dict, people: dict[int, dict]) -> dict[str, Any]:
    """One task, as a deliverable rather than as a work item.

    The teammate's *name* is copied in, not just their id. Agents can be renamed,
    re-personad or hired over, and a two-sprint-old note that resolves to whoever
    currently holds that row is a rewriting of history.
    """
    agent = people.get(task.get("agent_id") or 0)
    return {
        "task_id": task["id"],
        "seq": task["seq"],
        "title": task["title"],
        "role": task["role"],
        "status": task["status"],
        "pr": task["pr_number"],
        "branch": task["branch"],
        "model": task["model"],
        "attempts": task["attempts"],
        "agent_id": task.get("agent_id"),
        "agent": (agent or {}).get("name", ""),
        "verification": _verification(task),
        "summary": _gist(task.get("report") or ""),
    }


def facts(project_id: int, sprint: int) -> dict[str, Any]:
    """What this sprint delivered, computed live from the task table.

    This is the input to a snapshot and the fallback when there is no snapshot.
    It reads the *current* state of the rows, which is exactly the problem
    freezing exists to solve — callers should prefer `read()`.
    """
    p = db.get_project(project_id) or {}
    people = {a["id"]: a for a in db.list_agents(project_id)}
    tasks = [t for t in db.list_tasks(project_id) if t["sprint"] == sprint]
    items = [_item(t, people) for t in tasks]
    delivered = [i for i in items if i["status"] == "done"]
    failed = [i for i in items if i["status"] == "failed"]
    open_ = [i for i in items if i["status"] not in ("done", "failed")]
    return {
        "project_id": project_id,
        "project": p.get("name", ""),
        "sprint": sprint,
        "repo": p.get("repo", ""),
        "delivered": delivered,
        "failed": failed,
        "open": open_,
        "counts": {
            "delivered": len(delivered),
            "failed": len(failed),
            "open": len(open_),
            # Counted separately from pass/fail on purpose. "Nothing checked this"
            # is a third answer, and folding it into either of the other two is how
            # unverified work comes to look like verified work.
            "verified": len([i for i in delivered if i["verification"]["ok"]]),
            "unverified": len([i for i in delivered if not i["verification"]["ran"]]),
        },
        "prs": sorted({i["pr"] for i in items if i["pr"]}),
    }


def closed_sprints(project: dict) -> list[int]:
    """Sprints the project has finished with, and will not write to again."""
    current = int(project.get("sprint") or 1)
    last = current if project.get("status") in CLOSED else current - 1
    return list(range(1, last + 1))


async def capture(project_id: int, sprint: int, *, force: bool = False) -> dict[str, Any]:
    """Freeze one sprint's output.

    Refuses to overwrite an existing snapshot unless asked, because the second
    capture would be taken after the next sprint had already edited the rows — it
    would silently replace history with a later reading of it, which is the exact
    failure this module exists to prevent.
    """
    existing = db.get_sprint_artifact(project_id, sprint)
    if existing and not force:
        return existing
    f = facts(project_id, sprint)
    # The file list is the part that cannot be reconstructed afterwards: git will
    # happily tell you what the repo holds today and nothing about what it held
    # three sprints ago. Best-effort — a snapshot without it is still worth having,
    # and an unreachable GitHub must not cost us the task record too.
    if f["repo"] and github_client.enabled(f["repo"]):
        try:
            tree = await github_client.list_tree(f["repo"])
            f["files"] = [{"path": t["path"], "size": t.get("size", 0)} for t in tree]
        except Exception as e:
            f["files_error"] = str(e)[:200]
    db.save_sprint_artifact(project_id, sprint, f)
    return db.get_sprint_artifact(project_id, sprint)


async def ensure(project_id: int) -> list[int]:
    """Freeze every sprint the project has already left. Returns the ones taken now.

    Idempotent and cheap when there is nothing to do, which matters because this
    runs on ordinary reads of the artifacts pages.
    """
    p = db.get_project(project_id)
    if not p:
        return []
    have = {s["sprint"] for s in db.list_sprint_artifacts(project_id)}
    taken = []
    for n in closed_sprints(p):
        if n not in have:
            await capture(project_id, n)
            taken.append(n)
    return taken


def read(project_id: int, sprint: int) -> dict[str, Any]:
    """One sprint's artifacts: the frozen record if there is one, else a live read.

    `frozen` is part of the answer rather than an implementation detail. A live
    read of an old sprint shows those tasks as they are *now*, and a reader who
    cannot tell the two apart will mistake a later edit for what shipped then.
    """
    snap = db.get_sprint_artifact(project_id, sprint)
    # A snapshot whose JSON could not be read is worth less than a live view, so
    # it is treated as absent rather than returned half-shaped for every caller
    # downstream to guard against.
    if snap and snap["facts"].get("counts"):
        return {**snap["facts"], "frozen": True, "taken_at": snap["taken_at"],
                "notes": snap["notes"], "notes_source": snap["notes_source"]}
    return {**facts(project_id, sprint), "frozen": False, "taken_at": None,
            "notes": "", "notes_source": "",
            "note": "not snapshotted — this reads the tasks as they are now, which "
                    "may differ from what this sprint actually shipped"}


def timeline(project_id: int) -> list[dict[str, Any]]:
    """Every sprint the project has had, oldest first — the index to the rest."""
    p = db.get_project(project_id)
    if not p:
        return []
    highest = max([int(p["sprint"] or 1)] +
                  [t["sprint"] for t in db.list_tasks(project_id)] +
                  [s["sprint"] for s in db.list_sprint_artifacts(project_id)])
    out = []
    for n in range(1, highest + 1):
        view = read(project_id, n)
        out.append({
            "sprint": n,
            "frozen": view["frozen"],
            "taken_at": view["taken_at"],
            "current": n == int(p["sprint"] or 1) and p["status"] not in CLOSED,
            "counts": view["counts"],
            "prs": view["prs"],
            "has_notes": bool(view["notes"]),
        })
    return out


# --- release notes: prose that has to survive being checked ------------------

def recorded_notes(view: dict[str, Any]) -> str:
    """Release notes assembled from the record alone, with no model involved.

    This is the floor, not a placeholder: it is always correct, always available
    without a key or a network, and every line points at a task the reader can
    open. When a model rewrites the notes, this text is still returned beside its
    prose, so the readable version never becomes the only version.
    """
    c = view["counts"]
    head = (f"## Sprint {view['sprint']} — {c['delivered']} delivered"
            + (f", {c['failed']} failed" if c["failed"] else "")
            + (f", {c['open']} unfinished" if c["open"] else ""))

    def line(i: dict) -> str:
        v = i["verification"]
        mark = "tests passed" if v["ok"] else ("tests failed" if v["ran"] else "unverified")
        who = f" · {i['agent']}" if i["agent"] else ""
        pr = f" · PR #{i['pr']}" if i["pr"] else ""
        gist = f"\n  {i['summary']}" if i["summary"] else ""
        return f"- **#{i['seq']} {i['title']}** — {i['role']}{who}{pr} · {mark}{gist}"

    parts = [head]
    parts.append("\n### Delivered\n" + ("\n".join(line(i) for i in view["delivered"])
                                        or "_nothing was delivered in this sprint_"))
    if view["failed"]:
        parts.append("\n### Failed\n" + "\n".join(line(i) for i in view["failed"]))
    if view["open"]:
        parts.append("\n### Still open at the end of the sprint\n" +
                     "\n".join(line(i) for i in view["open"]))
    if c["unverified"]:
        parts.append(f"\n_{c['unverified']} of the delivered tasks were never checked "
                     f"by anything but the agent that wrote them._")
    return "\n".join(parts)


def _cited(text: str, allowed: set[int]) -> tuple[set[int], set[int]]:
    """Task numbers the prose refers to, split into real ones and invented ones.

    `PR #12` and `issue #12` are not task references, and counting them as such
    would fail an honest draft for quoting the record correctly — the one mistake
    that would teach a reader to ignore this check.
    """
    seen = {int(m.group(1)) for m in _REF.finditer(text or "")}
    return seen & allowed, seen - allowed


def _prompt(view: dict[str, Any]) -> str:
    return ("## The record for this sprint\n\n" + recorded_notes(view) +
            "\n\n## What to write\n\nA short summary of this sprint for someone who "
            "was not watching. Use only the tasks above, by their numbers.")


async def release_notes(project_id: int, sprint: int, settings: dict | None = None,
                        *, provider: str = "", model: str = "",
                        rewrite: bool = False) -> dict[str, Any]:
    """Notes for one sprint, with the facts they came from attached.

    The model pass is optional and it is not trusted. Its output is accepted only
    if every task number it mentions exists in this sprint; one invented citation
    discards the whole draft rather than the offending line, because a writer that
    fabricated one reference has told you what its other sentences are worth.

    The recorded notes are returned either way, so the prose is never the only
    account of the sprint on the page.
    """
    view = read(project_id, sprint)
    recorded = recorded_notes(view)
    if view["notes"] and not rewrite:
        return {"sprint": sprint, "notes": view["notes"], "source": view["notes_source"],
                "recorded": recorded, "facts": view, "frozen": view["frozen"]}

    out = {"sprint": sprint, "notes": recorded, "source": "recorded",
           "recorded": recorded, "facts": view, "frozen": view["frozen"]}
    if provider and settings:
        allowed = {i["seq"] for i in view["delivered"] + view["failed"] + view["open"]}
        model = model or providers.default_model(provider)
        try:
            draft = await providers.complete(provider, model, NOTES_SYSTEM,
                                             _prompt(view), settings, max_tokens=800)
        except Exception as e:
            out["model_error"] = str(e)[:200]
            draft = ""
        real, invented = _cited(draft, allowed)
        if not draft.strip():
            pass                        # unreachable model; the cause is already recorded
        elif invented:
            out["model_error"] = (
                f"the draft referred to task(s) {sorted(invented)}, which this sprint "
                f"did not contain — discarded, showing the recorded notes instead")
        elif allowed and not real:
            # Uncitable prose about a sprint that had tasks is the failure mode this
            # whole check exists for: it reads like a summary and is anchored to
            # nothing, so there is no way for a reader to find out it is wrong.
            out["model_error"] = ("the draft named no task from this sprint, so none of "
                                  "it could be checked — discarded")
        else:
            out["notes"] = draft.strip()
            out["source"] = f"{provider}:{model}"
            out["cites"] = sorted(real)

    # Only a frozen sprint keeps its notes. Storing notes for a sprint still in
    # progress would pin a description of half a cycle to the finished one.
    if view["frozen"]:
        db.set_sprint_notes(project_id, sprint, out["notes"], out["source"])
    return out


# --- per-teammate view over the same task artifacts --------------------------

def by_agent(project_id: int, sprint: int | None = None) -> dict[str, Any]:
    """What each teammate produced, as a grouping of task output — not a second
    store of it.

    Artifacts belong to tasks. An agent is how a task got done, so "Priya's
    artifacts" is a question about which tasks carry her `agent_id`, and answering
    it with its own ownership table would mean two records of one thing that can
    disagree. Regrouping costs a pass over rows we already read.

    Projects that predate named teammates have no `agent_id` anywhere, and a view
    that showed them one empty bucket would read as "nobody did anything". Those
    fall back to grouping by role, which is the identity those projects actually
    had, and say so in `basis`.

    Built on `read()` rather than on the task table directly, so a frozen sprint
    reads the same here as it does everywhere else. Two views of one sprint that
    disagree about what shipped are worse than one view.
    """
    p = db.get_project(project_id) or {}
    people = {a["id"]: a for a in db.list_agents(project_id)}
    spans = [sprint] if sprint else [r["sprint"] for r in timeline(project_id)]
    items = []
    for n in spans:
        view = read(project_id, n)
        items += [{**i, "sprint": n}
                  for i in view["delivered"] + view["failed"] + view["open"]]
    basis = "agent" if any(i.get("agent_id") for i in items) else "role"

    groups: dict[Any, dict[str, Any]] = {}
    for item in items:
        if basis == "agent":
            key = item.get("agent_id") or 0
            who = people.get(key, {})
            # A task with no agent_id predates the team, or was added by hand. It
            # still belongs somewhere visible; hiding it would make the per-person
            # counts add up to less than the project.
            head = {"agent_id": key or None,
                    # The recorded name wins over the current one: a teammate
                    # renamed since is still who did that work.
                    "name": item.get("agent") or who.get("name") or "unassigned",
                    "role": who.get("role") or item["role"],
                    "model": who.get("model") or "",
                    "persona": who.get("persona") or ""}
        else:
            key = item["role"]
            head = {"agent_id": None, "name": item["role"], "role": item["role"],
                    "model": "", "persona": ""}
        g = groups.setdefault(key, {**head, "tasks": []})
        g["tasks"].append(item)

    out = []
    for g in groups.values():
        delivered = [i for i in g["tasks"] if i["status"] == "done"]
        out.append({
            **g,
            "delivered": len(delivered),
            "attempted": len(g["tasks"]),
            "prs": sorted({i["pr"] for i in g["tasks"] if i["pr"]}),
            # Attempts above one are that teammate's rework, and it is the number
            # worth seeing per person: it says who is being sent work they cannot
            # finish first time, which is a briefing problem, not a person problem.
            "rework": sum(max(0, i["attempts"] - 1) for i in g["tasks"]),
        })
    out.sort(key=lambda g: (-g["delivered"], g["name"]))
    return {
        "project": p.get("name", ""),
        "sprint": sprint,
        "basis": basis,
        "note": ("" if basis == "agent" else
                 "this project ran before teammates had names, so its work is "
                 "grouped by role — the identity it actually had"),
        "agents": out,
    }


def agent_view(project_id: int, agent_id: int) -> dict[str, Any] | None:
    """One teammate's output across every sprint, newest sprint first."""
    agent = db.get_agent(agent_id)
    if not agent or agent["project_id"] != project_id:
        return None
    people = {a["id"]: a for a in db.list_agents(project_id)}
    tasks = [t for t in db.list_tasks(project_id) if t.get("agent_id") == agent_id]
    by_sprint: dict[int, list[dict]] = {}
    for t in tasks:
        by_sprint.setdefault(t["sprint"], []).append(_item(t, people))
    return {
        "id": agent["id"], "name": agent["name"], "role": agent["role"],
        "persona": agent["persona"], "model": agent["model"],
        "provider": agent["provider"], "notes": agent["notes"],
        "tasks_done": agent["tasks_done"],
        "sprints": [{"sprint": n, "tasks": by_sprint[n]}
                    for n in sorted(by_sprint, reverse=True)],
    }
