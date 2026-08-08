"""The boss talking to a running project: directives, notes, questions, budget.

Everything here is a message between the human and the manager session. The
one rule shared across it: nothing is silently swallowed. A note on a stopped
project is held and says so; a question is answered only by the boss who owns
the project; the manager acting on a note is not the same as the boss being
satisfied by it, so only the boss closes one.
"""

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .. import bus, db, feedback
from .base import current_user, owned_project, router


class Directive(BaseModel):
    text: str


class Answer(BaseModel):
    answer: str


class Budget(BaseModel):
    budget_usd: float


@router.post("/api/projects/{project_id}/directive")
def send_directive(project_id: int, body: Directive, request: Request) -> dict:
    """Boss -> manager message. Delivered at the manager's next decision point."""
    owned_project(project_id, request)
    db.add_directive(project_id, body.text)
    bus.emit(project_id, None, "boss", "directive", body.text)
    return {"ok": True}


class Note(BaseModel):
    target: str = "project"     # task | sprint | project
    target_id: int = 0
    text: str


@router.post("/api/projects/{project_id}/feedback")
def add_note(project_id: int, body: Note, request: Request) -> dict:
    """Notes on one task, one sprint, or the project — kept against that object.

    Delivered immediately as a directive when the project is live. When it is not,
    the note is recorded and held: a directive queued for a manager that will
    never run again is swallowed with no trace, and the boss cannot tell that from
    being ignored. `delivered` in the answer says which happened.
    """
    p = owned_project(project_id, request)
    u = current_user(request)
    try:
        note = feedback.record(project_id, body.target, body.target_id,
                               body.text, u["username"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    delivered = feedback.deliver(project_id)
    if delivered:
        bus.emit(project_id, None, "boss", "feedback",
                 {"target": note["target"], "target_id": note["target_id"],
                  "text": note["text"][:300]})
    return {"note": db.get_feedback(note["id"]), "delivered": bool(delivered),
            "held_reason": "" if delivered else
                           f"recorded but not delivered — this project is "
                           f"'{p['status']}' and no manager is reading it; "
                           f"restarting the project delivers it"}


@router.get("/api/projects/{project_id}/feedback")
def list_notes(project_id: int, request: Request, target: str = "",
               target_id: int | None = None) -> dict:
    """Notes on this project, optionally scoped to one task or sprint."""
    owned_project(project_id, request)
    if target and target not in feedback.TARGETS:
        raise HTTPException(400, f"unknown target {target!r}")
    return feedback.summary(project_id, target, target_id)


@router.post("/api/projects/{project_id}/feedback/deliver")
def deliver_notes(project_id: int, request: Request) -> dict:
    """Push every held note at the manager now. Used after a project is restarted,
    and by hand when the boss wants to know whether anything is still waiting."""
    owned_project(project_id, request)
    delivered = feedback.deliver(project_id)
    return {"delivered": [n["id"] for n in delivered],
            "still_open": feedback.pending_count(project_id)}


@router.post("/api/feedback/{feedback_id}/resolve")
def resolve_note(feedback_id: int, request: Request) -> dict:
    """Close a note. The boss decides when their own note is answered — the
    manager acting on it is not the same as the boss being satisfied by it."""
    note = db.get_feedback(feedback_id)
    if not note:
        raise HTTPException(404, "no such note")
    owned_project(note["project_id"], request)
    db.set_feedback_status(feedback_id, "resolved")
    return db.get_feedback(feedback_id)


@router.get("/api/projects/{project_id}/question")
def get_pending_question(project_id: int, request: Request) -> dict:
    """The manager's open question for the boss, if any."""
    owned_project(project_id, request)
    q = db.pending_question(project_id)
    if not q:
        return {"question": None}
    # NOTE: the key is "question" (not "text") — the dashboard keys off it to raise
    # the approval modal. Keep this name stable.
    # `topic` says what sort of moment this is — an interview before planning, a
    # sprint boundary, or a decision mid-flight. The dashboard frames each
    # differently, because labelling all three "your manager needs a decision"
    # makes the first thing a new project does look like a fault.
    return {"id": q["id"], "question": q["text"], "topic": q.get("topic", "decision"),
            "options": db.json.loads(q["options"])}


@router.post("/api/questions/{qid}/answer")
def answer(qid: int, body: Answer, request: Request) -> dict:
    q = db.get_question(qid)
    if not q:
        raise HTTPException(404, "no such question")
    owned_project(q["project_id"], request)     # only the boss answers their manager
    # An answer to a question nobody is behind any more must SAY so rather than
    # succeed. Every surface (the ask-card, the bell) is drawn from the pending
    # rows, so this only fires on a stale tab — but "200 OK" for an answer that
    # will never be read is exactly the failure this whole area had: the boss
    # typed a real answer, the platform said thank you, and it went nowhere.
    if q["status"] != "pending":
        raise HTTPException(409, f"that question is no longer open ({q['status']}) — "
                                 f"nobody is waiting on it. Send it as a message "
                                 f"instead and your manager will pick it up.")
    db.answer_question(qid, body.answer)
    bus.emit(q["project_id"], None, "boss", "answered",
             {"question": q["text"], "answer": body.answer})
    return {"ok": True}


@router.post("/api/projects/{project_id}/budget")
def set_budget(project_id: int, body: Budget, request: Request) -> dict:
    owned_project(project_id, request)
    db.set_project_budget(project_id, body.budget_usd)
    bus.emit(project_id, None, "boss", "budget_changed", {"budget_usd": body.budget_usd})
    return {"ok": True}
