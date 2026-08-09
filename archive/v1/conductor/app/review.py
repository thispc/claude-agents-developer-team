"""Reviewed work, discussed with the team rather than judged alone.

The manager was the only reader of finished work. That is the cheapest possible
review and, by the evidence, the wrong place to have economised: across the
multi-agent literature the gain from *aggregating* several independent readings
is worth several times the gain from adding diversity for its own sake. Running
three rival implementations and having one person skim the winner spends the
money on the part that pays least.

So the panel here is small and the aggregation is explicit:

- **Independent first.** Each reviewer sees the work and the evidence, never each
  other's opinions. Reviewers who read each other converge — the second one
  agrees with the first, and you have paid twice for one opinion.
- **The author is excluded.** Asking someone whether their own work is good is
  not a review, and it reliably returns yes.
- **Evidence leads.** Every reviewer is shown the harness's own test result
  before the agent's account of itself. An imperfect verifier caps how good the
  outcome can get no matter how much inference you buy, so the verifier's word
  goes first and the prose is context for it.
- **Disagreement survives.** The manager receives the split, not a verdict. A
  panel that returns "2 of 3 would accept, and here is why the third would not"
  is useful; one that returns "accept" has thrown away the only part the manager
  could not have worked out alone.

Reviewers are teammates, so they answer with their own persona and with what they
have already built on this project in front of them. A tester who wrote the suite
reviewing a change to it is a different reader from a generic critic, and that
difference is the whole reason the team has names.
"""

import asyncio
import json
from typing import Any

from . import ambition, bus, db, providers, team, tuning

# Reviews are read, not written. A long review is a worse review — the manager has
# to act on it, and a page of prose per reviewer turns a decision into homework.
MAX_REVIEW_CHARS = 1500

SYSTEM = (
    "You are reviewing a teammate's finished work on a software project. You are "
    "not the author. Be specific and short.\n\n"
    "Answer in exactly this shape:\n"
    "VERDICT: accept | rework\n"
    "WHY: one or two sentences\n"
    "SPECIFICS: up to three concrete points, each naming a file, a function or a "
    "test. Skip anything you cannot point at.\n\n"
    "Rules that matter:\n"
    "- If the project's own tests were run and failed, that is decisive. Do not "
    "  talk yourself past a red build because the write-up is persuasive.\n"
    "- If nothing verified the work, say so and treat the claims as unverified.\n"
    "- 'Looks good' with no specifics is not a review. If you genuinely have "
    "  nothing to add, say 'accept, nothing to add' and stop."
)


def _evidence(task: dict) -> str:
    """The harness's own result, ahead of the agent's account of itself."""
    try:
        v = json.loads(task.get("verification") or "{}")
    except json.JSONDecodeError:
        v = {}
    if not v.get("ran"):
        return ("NOTHING VERIFIED THIS WORK. The project declares no test command, so "
                "the only account of it is the author's own, below.")
    if v.get("ok"):
        return f"THE PROJECT'S OWN CHECK PASSED: `{v.get('cmd')}` exited 0."
    return (f"THE PROJECT'S OWN CHECK FAILED: `{v.get('cmd')}` exited "
            f"{v.get('exit_code')}.\n--- output (tail) ---\n"
            f"{(v.get('output') or '')[-1500:]}")


def reviewers(project_id: int, task: dict, size: int) -> list[dict]:
    """Who reads this work.

    Prefers teammates in a different role from the author: someone who does the
    same job tends to share the same blind spot, and the point of a second reader
    is to not have the first one's blind spot.
    """
    people = [a for a in db.list_agents(project_id) if a["id"] != task.get("agent_id")]
    if not people:
        return []
    other_role = [a for a in people if a["role"] != task["role"]]
    same_role = [a for a in people if a["role"] == task["role"]]
    return (other_role + same_role)[:max(0, size)]


def _prompt(task: dict, reviewer: dict) -> str:
    parts = [
        f"## The task\n\n**{task['title']}** (assigned to the {task['role']} role)\n\n"
        f"{task['description'][:2000]}",
        f"## The evidence\n\n{_evidence(task)}",
        f"## What the author says they did\n\n{(task.get('report') or '(no report)')[:4000]}",
    ]
    if (reviewer.get("notes") or "").strip():
        parts.append(
            f"## What you have built on this project\n\n{reviewer['notes']}\n\n"
            "If this work contradicts something you built, say so — you are the only "
            "person who would notice.")
    return "\n\n".join(parts)


def _parse(text: str) -> dict[str, Any]:
    """Pull the verdict out without being brittle about formatting.

    Defaults to 'rework' when no verdict is stated. A reviewer whose answer could
    not be read should not be counted as approval — silence is not consent, and
    the failure mode of the opposite default is work waved through by a parse bug.
    """
    body = (text or "").strip()
    verdict = "rework"
    for line in body.splitlines():
        low = line.strip().lower()
        if low.startswith("verdict:"):
            verdict = "accept" if "accept" in low else "rework"
            break
    return {"verdict": verdict, "text": body[:MAX_REVIEW_CHARS]}


async def _one(reviewer: dict, task: dict, settings: dict) -> dict[str, Any]:
    system = SYSTEM + team.system_addendum(reviewer)
    provider = team.provider_for(reviewer)
    model = reviewer.get("model") or providers.default_model(provider)
    try:
        text = await providers.complete(provider, model, system, _prompt(task, reviewer),
                                        settings)
    except Exception as e:
        # A reviewer who could not be reached is reported as such and does not vote.
        # Counting an unreachable reviewer either way would be inventing an opinion.
        return {"name": reviewer["name"], "role": reviewer["role"],
                "verdict": "unavailable", "text": f"could not be reached: {e}"}
    return {"name": reviewer["name"], "role": reviewer["role"], **_parse(text)}


async def panel(project_id: int, task: dict, settings: dict,
                size: int | None = None) -> dict[str, Any]:
    """Ask the team about one piece of finished work.

    Returns the split rather than a decision. The manager still decides — this
    gives it something it could not have worked out by reading the report alone.
    """
    if size is None:
        size = int(ambition.get(db.get_project(project_id), "review_panel_size",
                                int(tuning.get("review_panel_size"))))
    chosen = reviewers(project_id, task, size)
    if not chosen:
        return {"reviews": [], "note": "nobody else on this project to ask"}

    # Concurrently, and each blind to the others. Reviewers who see each other's
    # opinions converge on the first one, and then you have paid N times for one.
    reviews = await asyncio.gather(*[_one(r, task, settings) for r in chosen])
    reviews = list(reviews)
    voting = [r for r in reviews if r["verdict"] in ("accept", "rework")]
    accepts = len([r for r in voting if r["verdict"] == "accept"])

    bus.emit(project_id, task["id"], "system", "panel_reviewed",
             {"reviewers": [r["name"] for r in reviews],
              "accepts": accepts, "of": len(voting)})
    return {
        "reviews": reviews,
        "accepts": accepts,
        "voting": len(voting),
        "unanimous": bool(voting) and accepts in (0, len(voting)),
    }


def summarise(result: dict[str, Any]) -> str:
    """The panel as the manager should read it: the split, then the reasons."""
    reviews = result.get("reviews") or []
    if not reviews:
        return result.get("note", "no reviewers available")
    head = (f"{result['accepts']} of {result['voting']} teammates would accept this."
            if result.get("voting") else "No teammate could be reached for a review.")
    if result.get("voting") and not result.get("unanimous"):
        head += (" They disagree, which is the part worth your attention — read the "
                 "dissent before you decide.")
    body = "\n\n".join(f"### {r['name']} ({r['role']}) — {r['verdict']}\n{r['text']}"
                       for r in reviews)
    return f"{head}\n\n{body}"
