"""How good it has to be, versus how fast you want it.

Every default in this platform leans the same way: cheap models, contests off,
one reviewer, escalate only after something has already failed, and planning
guidance that says to build the smallest thing that runs. Each of those is a
defensible default on its own. Together they are a machine tuned to produce
something quickly, and nothing anywhere ever asks for more than that.

A real run made it obvious. A brief asking for "a metroidvania like Hollow
Knight" produced six tasks, one sprint, six agent runs, and — this is the part
that matters — **the entire game was one task**, written by one agent in one
pass, never sent back, never reviewed by anyone but the manager, never verified
by anything. The output was exactly what the system was asked for. It was not a
failure of the agents; the plan never asked for more than a demo, and the agile
guidance explicitly told the manager not to build for anything nobody had asked
for yet.

So the missing input is not a better model or a smarter prompt. It is the one
thing the boss was never able to say: *time is not the constraint here, quality
is*. That is what this expresses.

It deliberately changes several things at once, because quality is not one dial
in the code. Planning depth, model tier, whether rivals compete, how many people
read the result, and whether the first attempt can be accepted at all are all
part of the same question, and moving only one of them produces a system that is
expensive without being better.
"""

from typing import Any

LEVELS: dict[str, dict[str, Any]] = {
    "draft": {
        "label": "Draft — fastest, cheapest",
        "when": "Trying an idea out. You want to see something running, not ship it.",
        # Knob overrides applied on top of whatever is otherwise configured.
        "knobs": {
            "review_panel_size": 0,
            "contest_max_width": 1,      # effectively off
            "escalate_after_attempts": 3,
        },
        "planning": (
            "## How much to aim for\n\n"
            "This is a DRAFT. Favour speed over completeness: the smallest thing that "
            "demonstrates the idea, in as few tasks as will do it. Do not build for "
            "cases nobody has asked about. It is fine for this to be rough."),
    },
    "standard": {
        "label": "Standard — balanced",
        "when": "Most work. Good quality without spending all night on it.",
        "knobs": {},
        "planning": (
            "## How much to aim for\n\n"
            "Aim for work you would be comfortable showing someone. Complete the "
            "things you start, handle the obvious failure cases, and do not leave "
            "placeholders where real behaviour belongs."),
    },
    "exacting": {
        "label": "Exacting — best possible, time is no object",
        "when": "When the result matters more than when it arrives. Expect it to "
                "take much longer and cost considerably more.",
        "knobs": {
            # A stronger tier from the start, rather than after two failures. At
            # this setting a cheap first attempt is not a saving — it is a round
            # trip you are going to pay for anyway.
            "escalate_after_attempts": 1,
            "review_panel_size": 2,
            "contest_max_width": 3,
            "max_attempts": 5,
            "review_requires_evidence": True,
        },
        "planning": (
            "## How much to aim for\n\n"
            "TIME IS NOT A CONSTRAINT ON THIS PROJECT. The boss has said explicitly "
            "that they would rather wait and get something genuinely good. Spending "
            "more agent runs to reach a better result is the correct trade, and "
            "finishing early with something thin is the failure mode to avoid.\n\n"
            "What that means concretely when you plan:\n"
            "- **One task per meaningful piece, never one task per deliverable.** "
            "  'Implement the game' is a planning failure at this setting. The "
            "  systems inside it — movement and collision, combat, level structure, "
            "  save state, audio, the feel of the controls — are each worth their own "
            "  task, and several are worth more than one.\n"
            "- **Plan the unglamorous parts.** Error states, empty states, input "
            "  edge cases, performance, and how someone finds out what went wrong. "
            "  These are what separate a demo from a product, and nobody asks for "
            "  them by name.\n"
            "- **Make it checkable early.** A real test or build command comes "
            "  first, before the work it protects — you will be relying on it a lot.\n"
            "- **Use contests on the pieces where approach matters** — the core "
            "  mechanic, the tricky algorithm — and compare them properly.\n"
            "- **Do not accept a first attempt just because it works.** Read it "
            "  against what a demanding reviewer would say, and send it back with "
            "  specifics if it is merely adequate.\n"
            "- **Plan more sprints than you think you need.** The first pass gets it "
            "  working; the ones after are where it becomes good."),
    },
}

DEFAULT = "standard"


def normalise(name: str | None) -> str:
    return name if name in LEVELS else DEFAULT


def guidance(project: dict | None) -> str:
    return LEVELS[normalise((project or {}).get("ambition"))]["planning"]


def knobs(project: dict | None) -> dict[str, Any]:
    """Knob overrides for this project's setting.

    Overrides rather than assignments: an operator who has deliberately tuned
    something globally should not have it silently reverted by a per-project
    choice they made for a different reason.
    """
    return dict(LEVELS[normalise((project or {}).get("ambition"))]["knobs"])


def get(project: dict | None, name: str, fallback: Any) -> Any:
    """A knob's value for this project — its override if it has one, else the
    globally configured value the caller already resolved."""
    return knobs(project).get(name, fallback)


def worker_tier(project: dict | None) -> str:
    """Which model tier the work should START on.

    At `exacting` a cheap first attempt is not a saving. The failure it produces
    still costs a full agent run, plus the retry, plus the reviewer's time — so
    starting where you would have ended up is both better and usually cheaper.
    """
    return {"draft": "worker", "standard": "", "exacting": "lead"}[
        normalise((project or {}).get("ambition"))]


def catalog() -> list[dict[str, Any]]:
    """For the UI, so the trade is stated rather than implied by a slider."""
    return [{"id": k, "label": v["label"], "when": v["when"], "default": k == DEFAULT}
            for k, v in LEVELS.items()]
