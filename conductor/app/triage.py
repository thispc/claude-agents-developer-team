"""How much autonomy a self-repair issue gets, decided before work starts.

"Fix the small things quietly, ask me about the big ones" is a policy, not a
personality — so it lives in code, where it is testable and cannot be argued out
of by a persuasive model.

Three tiers:

    routine      fix it, verify it, ship it. You see it happened; you are not asked.
    substantial  do the work on a branch, open a PR, tell the operator. Nothing
                 reaches the running platform until a human merges.
    restricted   do not start. Some things should not be attempted unsupervised
                 at all, however small the diff looks.

The shape that makes this safe is that **the floor is deterministic and the model
can only raise it**. A set of rules decides the minimum tier from what the issue
touches; a model may then argue for MORE caution, never less. So a convincing
argument that "this auth change is trivial, let me ship it" cannot lower the bar
— the worst a compromised or over-confident classifier can do is make the
platform more cautious than necessary.

That asymmetry is the whole design. Everything else is bookkeeping.
"""

import json
import re
from typing import Any

from . import config

ROUTINE, SUBSTANTIAL, RESTRICTED = "routine", "substantial", "restricted"
_ORDER = {ROUTINE: 0, SUBSTANTIAL: 1, RESTRICTED: 2}

# Touching any of these means a human looks at it, whatever the model thinks.
# Each is here because the failure is silent, expensive, or both.
FLOORS: list[tuple[str, str, str]] = [
    # credentials and access — a mistake here is not visible in the UI at all
    (SUBSTANTIAL, r"\b(auth|credential|token|secret|password|permission|rbac)\b",
     "changes how something authenticates or what it may do"),
    # data shape — a bad migration destroys projects and cannot be undone by a redeploy
    (SUBSTANTIAL, r"\b(migration|schema|ALTER TABLE|drop column|db\.py)\b",
     "changes the database shape, which a rollback does not undo"),
    # the deploy path — breaking this removes the ability to fix the break
    (SUBSTANTIAL, r"\b(deploy|rollout|kubernetes|k8s|dockerfile|manifest|ingress)\b",
     "changes how the platform is deployed"),
    # money
    (SUBSTANTIAL, r"\b(budget|billing|cost|max_runs|rate.?limit)\b",
     "changes what the platform is allowed to spend"),
    # the guard rails themselves
    (RESTRICTED, r"\b(triage\.py|disable.*(verification|test)|skip.*test|--no-verify)\b",
     "would weaken the checks that make unattended running safe"),
    (RESTRICTED, r"\b(delete|destroy|drop)\b.*\b(project|cluster|namespace|volume|database)\b",
     "destroys something that cannot be recreated from the repo"),
]

# Genuinely small, self-contained things. Matching one of these is not sufficient
# on its own — it only means nothing above objected.
ROUTINE_HINTS = re.compile(
    r"\b(typo|wording|copy|label|tooltip|spacing|padding|margin|colour|color|"
    r"alignment|overflow|truncat|contrast|icon|hover|placeholder|log message|"
    r"console error|null check|missing key|off.by.one)\b", re.I)


def floor_for(text: str) -> tuple[str, list[str]]:
    """The minimum tier, from rules alone. No model involved, no judgement."""
    tier, why = ROUTINE, []
    for level, pattern, reason in FLOORS:
        if re.search(pattern, text or "", re.I):
            why.append(reason)
            if _ORDER[level] > _ORDER[tier]:
                tier = level
    return tier, why


def _at_least(a: str, b: str) -> str:
    return a if _ORDER[a] >= _ORDER[b] else b


CLASSIFY_SYSTEM = (
    "You decide how much independence a change to a running platform should get.\n\n"
    "Answer with ONLY valid JSON:\n"
    '{"tier": "routine" | "substantial", "why": "one sentence"}\n\n'
    "routine — small, contained, and obviously safe to ship without a human "
    "looking: wording, spacing, a missing null check, a log message, a UI glitch. "
    "The kind of thing where a reviewer would say 'why are you asking me'.\n\n"
    "substantial — anything that changes behaviour someone depends on, touches "
    "more than a few files, adds a feature, or where you are not certain.\n\n"
    "When in doubt, say substantial. Being asked unnecessarily costs a moment; "
    "shipping something unreviewed that breaks the platform costs the platform. "
    "You cannot make something less restricted than it already is — a separate "
    "rule decides that — so there is no reason to argue for less caution."
)


async def classify(title: str, body: str, settings: dict) -> dict[str, Any]:
    """Decide the tier: rules set the floor, a model may only raise it.

    Falls back to the floor when no model answers, which for anything the rules
    flagged is the cautious direction anyway.
    """
    text = f"{title}\n{body}"
    floor, floor_why = floor_for(text)
    result = {"tier": floor, "floor": floor, "why": list(floor_why), "model": None}

    if floor == RESTRICTED:
        # Nothing to decide: this is not attempted unsupervised regardless.
        return result

    from . import providers
    have = providers.available(settings)
    provider = next((p for p in ("anthropic", "google", "openai") if p in have), None)
    if not provider:
        result["why"].append("no model available to judge, so the rule floor stands")
        return result

    model = {"anthropic": config.WORKER_MODEL, "google": "gemini-flash-latest",
             "openai": "gpt-5-mini"}[provider]
    try:
        raw = await providers.complete(provider, model, CLASSIFY_SYSTEM,
                                       f"Title: {title}\n\n{body[:3000]}", settings,
                                       max_tokens=300)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        said = data.get("tier") if data.get("tier") in (ROUTINE, SUBSTANTIAL) else SUBSTANTIAL
        result["model"] = {"tier": said, "why": str(data.get("why", ""))[:300]}
        # The asymmetry: only ever more cautious than the floor.
        result["tier"] = _at_least(said, floor)
        if said != result["tier"]:
            result["why"].append(
                f"the model judged this {said}, but a rule requires {result['tier']}")
        elif data.get("why"):
            result["why"].append(str(data["why"])[:300])
    except Exception as e:
        result["why"].append(f"could not reach a model to judge ({str(e)[:80]}); "
                             f"the rule floor stands")
    return result


def policy(tier: str) -> dict[str, Any]:
    """What each tier is actually allowed to do, in one place so it is auditable."""
    return {
        ROUTINE: {
            "may_work": True, "may_merge": True, "may_deploy": True,
            "notify": False,
            "label": "self-healed",
            "note": "Fixed, verified and shipped without interrupting you. Listed "
                    "on the Improve page so you can see it happened.",
        },
        SUBSTANTIAL: {
            "may_work": True, "may_merge": False, "may_deploy": False,
            "notify": True,
            "label": "needs your review",
            "note": "The work is done on a branch and a pull request is waiting. "
                    "Nothing reaches the running platform until you merge it.",
        },
        RESTRICTED: {
            "may_work": False, "may_merge": False, "may_deploy": False,
            "notify": True,
            "label": "not attempted",
            "note": "Not started unsupervised. Raise it yourself if you want it "
                    "done, so the decision is on the record as yours.",
        },
    }[tier]
