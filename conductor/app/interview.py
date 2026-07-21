"""The conversation that should happen before anyone starts building.

The manager planned straight from the brief. Briefs are short, and the gap
between what someone wrote and what they meant is where most wasted work lives —
"a metroidvania like Hollow Knight" could mean the combat feel, or the map
design, or the atmosphere, and building the wrong one costs a sprint to discover.

Three decisions shape this, and each one had an alternative worth rejecting out
loud.

**It happens before planning, not alongside it.** Planning in parallel and
folding the answers in afterwards sounds faster and is not: the plan is a
dependency graph whose *shape* depends on the answers, so a late answer either
invalidates the graph — wasting exactly what the parallelism saved — or gets
quietly ignored. Worse, the scheduler dispatches whatever is unblocked, so by the
time an answer arrives a worker may already have built the wrong thing, which
costs an agent run plus the rework.

**One round, asked as a batch.** A real one-to-one is a conversation, but each
round costs an interruption, and interruptions are the thing the product exists
to avoid. Three good questions asked together beat six asked one at a time.

**The questions are about THIS brief.** Generic questions — "who are the users?"
— get skimmed and answered generically, which is worse than not asking. The
questions are drafted against the actual text, so they name the ambiguity that is
actually there.

Bounded throughout: it asks, it waits for a while, and then it proceeds and says
what it assumed. An unattended run must not be stopped by a question, and a
question nobody was there to answer must not become silence.
"""

import json
from typing import Any

from . import bus, db, process, providers, tuning

# What the process model needs to have settled before it can plan. These are the
# angles the drafter is told to probe, not the questions themselves — the actual
# wording has to come from the brief or it is generic.
FOCUS = {
    "agile": (
        "You slice by outcome, so what you must settle is what the FIRST usable "
        "version contains and who judges it. Probe: the smallest thing that would "
        "be worth showing someone; which parts of the brief are the point versus "
        "which are decoration; what 'good enough for round one' means."),
    "waterfall": (
        "You slice by layer, so what you must settle is the shape, because a "
        "contract that changes later invalidates finished work. Probe: whether "
        "the specification is fixed or still moving; the interfaces and data "
        "shapes other work will be built against; anything that must not change."),
}

SYSTEM = (
    "You are a software delivery manager about to plan a project, talking to the "
    "client for the only time before work starts. Ask the questions whose answers "
    "would change your plan.\n\n"
    "Rules:\n"
    "- Ask about THIS brief. Quote the phrase you are unsure about. A question "
    "  that would fit any project is a wasted question.\n"
    "- Never ask what you can decide yourself. You were hired to make those calls.\n"
    "- Never ask for information you would not act on.\n"
    "- Offer concrete options where you can, so the answer can be one click.\n"
    "- If the brief is genuinely clear enough to plan from, return no questions "
    "  at all. That is a real and useful answer.\n\n"
    "Return ONLY valid JSON:\n"
    '{"questions": [{"ask": "the question", "why": "what it changes about the plan", '
    '"options": ["a concrete option", "another"]}]}'
)


def _prompt(brief: str, proc: str, roster: list[dict]) -> str:
    who = ", ".join(f"{m.get('count', 1)}× {m.get('role')}" for m in roster) or "not yet decided"
    return (
        f"THE BRIEF:\n{brief.strip()[:3000]}\n\n"
        f"THE TEAM YOU HAVE: {who}\n\n"
        f"HOW YOU WILL BREAK THE WORK UP:\n{FOCUS.get(proc, FOCUS['agile'])}\n\n"
        f"Ask at most {int(tuning.get('interview_questions'))} questions.")


def _parse(raw: str, limit: int) -> list[dict]:
    """Pull questions out without being brittle about the wrapping.

    A drafter that returns prose instead of JSON yields no questions, which means
    the project plans straight from the brief — the behaviour before this existed.
    Degrading to the old behaviour is the right failure for something whose whole
    purpose is to be an improvement on it.
    """
    text = (raw or "").strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    out = []
    for q in (data.get("questions") or [])[:limit]:
        if not isinstance(q, dict) or not str(q.get("ask", "")).strip():
            continue
        opts = [str(o)[:80] for o in (q.get("options") or []) if str(o).strip()][:4]
        out.append({"ask": str(q["ask"])[:400], "why": str(q.get("why", ""))[:200],
                    "options": opts})
    return out


async def draft(project: dict, settings: dict) -> list[dict]:
    """The questions worth asking about this brief, or none."""
    limit = int(tuning.get("interview_questions"))
    if limit <= 0:
        return []
    try:
        roster = json.loads(project.get("team") or "[]")
    except json.JSONDecodeError:
        roster = []
    proc = process.normalise(project.get("process"))
    provider = "anthropic"
    model = project.get("manager_model") or providers.default_model(provider)
    try:
        raw = await providers.complete(
            provider, model, SYSTEM, _prompt(project.get("brief", ""), proc, roster),
            settings, max_tokens=1200)
    except Exception as e:
        bus.emit(project["id"], None, "system", "interview_skipped",
                 {"reason": str(e)[:200]})
        return []
    return _parse(raw, limit)


def as_message(questions: list[dict]) -> str:
    """The questions as the boss sees them: numbered, each saying what it changes.

    'Why' is included deliberately. A question with a visible consequence gets a
    considered answer; one without reads as a form to fill in.
    """
    lines = ["Before I plan this, there are a few things only you can settle."]
    for i, q in enumerate(questions, 1):
        lines.append(f"\n{i}. {q['ask']}")
        if q.get("why"):
            lines.append(f"   — {q['why']}")
        if q.get("options"):
            lines.append("   e.g. " + " · ".join(q["options"]))
    lines.append("\nAnswer what you care about and ignore the rest — I will decide "
                 "anything you leave, and tell you what I assumed.")
    return "\n".join(lines)


def record(project_id: int, questions: list[dict], answer: str) -> str:
    """Store the exchange and return the block that goes into the planning prompt."""
    db.kv_set(f"interview:{project_id}",
              {"questions": questions, "answer": answer or ""})
    if not (answer or "").strip():
        return ("\nYou asked the boss about the brief and they did not answer in time. "
                "Plan it on your own judgement, and in your first message state the "
                "assumptions you made so they can correct you cheaply.\n")
    asked = "; ".join(q["ask"] for q in questions)
    return (f"\nYOU ASKED THE BOSS ABOUT THE BRIEF BEFORE PLANNING.\n"
            f"You asked: {asked}\n"
            f"They answered: {answer.strip()}\n"
            f"These answers outrank your own reading of the brief. Where they leave "
            f"something open, decide it yourself and say so.\n")


def stored(project_id: int) -> dict[str, Any]:
    return db.kv_get(f"interview:{project_id}", {}) or {}
