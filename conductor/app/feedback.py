"""Notes on a specific thing, delivered to the manager with what they are about.

The boss could already send a directive. A directive is a message: free text,
consumed once at the manager's next decision point, stored against nothing. That
works for "stop and do X" and fails for the thing people actually want to say,
which is "here are my notes on THIS" — because the manager receives the notes
without the THIS. "The signup flow is wrong" is unactionable when three sprints
have touched signup; "this release note overclaims" names nothing at all.

So a note keeps its target. Three deliberate choices:

- **The channel is not reinvented.** The manager reads directives and only
  directives, so a note is *rendered into* one. What changes is the payload: the
  directive carries the task's number, title, status, PR and verification, or the
  sprint's counts — the context the boss was looking at when they typed. The
  manager should never have to go and find out what is being discussed.
- **Recording and delivering are separate.** A note on a finished project has
  nowhere to go: a directive queued for a manager that will never run again is
  swallowed silently, and the boss has no way to tell that from being ignored. So
  the note is always recorded against its target, and delivery happens when there
  is something listening — at post time if the project is live, and at restart
  otherwise. `status` says which of the two happened, out loud.
- **Old sprints are still worth commenting on, and say their age.** A note on
  sprint 1 written during sprint 3 is delivered, not expired: feedback that
  arrives late is still the boss's opinion, and dropping it silently is the worst
  of the options. But it is delivered *labelled* — the sprint it is about, how
  many cycles ago that was, and that any rework lands in the current sprint. A
  manager told to "fix this" about frozen work would otherwise either rewrite
  history or ignore the note, and it cannot choose without being told which.
"""

from typing import Any

from . import artifacts, db

TARGETS = ("task", "sprint", "project")

# Statuses where a manager session exists or will be started for this project. A
# note posted outside these is held: 'done', 'failed' and 'cancelled' have nobody
# reading the inbox, and there is no scheduled moment when one appears.
LIVE = ("planning", "running", "hold", "review")

# Enough of a note to identify it in the manager's prompt without turning the
# directive into an essay. The full text is on the record either way.
MAX_NOTE_CHARS = 2000


def _task_context(task: dict) -> str:
    v = artifacts._verification(task)
    mark = ("its checks passed" if v["ok"] else
            "its checks FAILED" if v["ran"] else "nothing verified it")
    pr = f", PR #{task['pr_number']}" if task["pr_number"] else ""
    return (f"task #{task['seq']} \"{task['title']}\" ({task['role']}, "
            f"status {task['status']}{pr}, {mark}, sprint {task['sprint']})")


def _sprint_context(project_id: int, sprint: int) -> str:
    view = artifacts.read(project_id, sprint)
    c = view["counts"]
    state = "frozen" if view["frozen"] else "not snapshotted, reads live"
    return (f"sprint {sprint} ({c['delivered']} delivered, {c['failed']} failed, "
            f"{c['open']} unfinished, {c['unverified']} of the delivered never "
            f"checked; {state})")


def describe(project_id: int, target: str, target_id: int) -> str:
    """What the note is attached to, in one line the manager can act on.

    Computed at DELIVERY time rather than copied at write time on purpose: the
    boss's note is the durable thing, and the state of the work is what the
    manager needs to be current. A task that has since been reworked should reach
    the manager as it is now, with the note as it was written.
    """
    if target == "task":
        t = db.get_task(target_id)
        return _task_context(t) if t else f"task {target_id} (since deleted)"
    if target == "sprint":
        return _sprint_context(project_id, target_id)
    return "the project as a whole"


def validate(project_id: int, target: str, target_id: int) -> str:
    """Why this note cannot be attached, or "" if it can.

    A note pointing at nothing is worse than a rejected one: it shows up in the
    artifact view of no object, and reaches the manager as a reference it cannot
    resolve.
    """
    if target not in TARGETS:
        return f"a note attaches to one of {', '.join(TARGETS)}, not {target!r}"
    if target == "task":
        t = db.get_task(target_id)
        if not t or t["project_id"] != project_id:
            return "no such task on this project"
    if target == "sprint":
        p = db.get_project(project_id) or {}
        if target_id < 1 or target_id > max(1, int(p.get("sprint") or 1)):
            return f"this project has not reached sprint {target_id}"
    return ""


def record(project_id: int, target: str, target_id: int, text: str,
           author: str = "") -> dict[str, Any]:
    """Write the note down. Delivery is a separate decision — see `deliver`."""
    text = (text or "").strip()[:MAX_NOTE_CHARS]
    if not text:
        raise ValueError("an empty note says nothing")
    problem = validate(project_id, target, target_id)
    if problem:
        raise ValueError(problem)
    fid = db.add_feedback(project_id, target, target_id, text, author)
    return db.get_feedback(fid)


def _age_note(project: dict, note: dict) -> str:
    """The one thing a manager cannot work out for itself: how stale this is.

    Without it a note on sprint 1 read during sprint 3 looks like a note on
    current work, and the manager either reopens a frozen cycle's task in place —
    which is what freezing exists to stop — or quietly drops the note as obsolete.
    Neither is a choice it should be making blind.
    """
    now = int(project.get("sprint") or 1)
    then = note["target_id"] if note["target"] == "sprint" else 0
    if note["target"] == "task":
        t = db.get_task(note["target_id"])
        then = int((t or {}).get("sprint") or 0)
    if not then or then >= now:
        return ""
    gap = now - then
    return (f" NOTE: this is about sprint {then}, {gap} sprint(s) ago; the project "
            f"is in sprint {now}. That work is finished and its record is frozen — "
            f"if this needs fixing, plan it as new work in the current sprint, do "
            f"not rewrite the old one.")


def as_directive(project: dict, note: dict) -> str:
    """One note, rendered as the message the manager will actually read."""
    who = f" from {note['author']}" if note["author"] else ""
    return (f"NOTE{who} on {describe(project['id'], note['target'], note['target_id'])}:\n"
            f"{note['text']}"
            f"{_age_note(project, note)}\n"
            f"Treat this as the boss's opinion on that specific object, not as a "
            f"general instruction. If it needs work, plan it; if it does not, say "
            f"why with reply_to_boss.")


def is_live(project: dict) -> bool:
    return (project or {}).get("status") in LIVE


def deliver(project_id: int) -> list[dict[str, Any]]:
    """Push every open note on this project into the directive channel.

    One directive per note, not one batch: directives are consumed as a group at a
    decision point anyway, and keeping them separate means a note's own row can
    record which message carried it. Batching would leave every note pointing at
    the same directive and no way to say which of them the manager answered.

    Returns the notes delivered, empty when there were none or when nothing is
    listening. Safe to call on any project at any time — that is what makes it
    usable from both the post path and the restart path.
    """
    p = db.get_project(project_id)
    if not p or not is_live(p):
        return []
    out = []
    for note in db.list_feedback(project_id, status="open"):
        did = db.add_directive(project_id, as_directive(p, note))
        db.mark_feedback_delivered(note["id"], did)
        out.append(db.get_feedback(note["id"]))
    return out


def summary(project_id: int, target: str = "", target_id: int | None = None) -> dict:
    """Notes for the artifact view, plus whether any are waiting on a dead project.

    `held` is surfaced rather than left implicit: a note nobody is listening to
    looks identical to one being ignored, and the only honest fix is to say so on
    the page where it was written.
    """
    p = db.get_project(project_id) or {}
    notes = db.list_feedback(project_id, target, target_id)
    held = [n for n in notes if n["status"] == "open"]
    return {
        "project_id": project_id,
        "target": target, "target_id": target_id,
        "notes": notes,
        "open": len(held),
        "live": is_live(p),
        "held": bool(held) and not is_live(p),
        "note": ("" if is_live(p) or not held else
                 f"{len(held)} note(s) are recorded but undelivered — this project "
                 f"is '{p.get('status')}' and no manager is reading. They will be "
                 f"delivered if it is restarted."),
    }


def pending_count(project_id: int) -> int:
    return len(db.list_feedback(project_id, status="open"))


__all__ = ["TARGETS", "record", "deliver", "summary", "describe", "as_directive",
           "validate", "is_live", "pending_count", "MAX_NOTE_CHARS"]
