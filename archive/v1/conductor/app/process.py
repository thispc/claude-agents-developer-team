"""Whether the team works in slices or in stages, said out loud.

Neither model was expressed anywhere. That did not mean the platform had no
process — it meant it had an accidental one: a DAG planned in a single pass, with
dependencies wired by role, which is waterfall in everything but name. Every task
of a kind was planned together, and nothing shipped until the layer beneath it
was finished. That is the right shape for some work and badly wrong for most of
what people actually build, and it was never a choice anyone made.

The two models differ in one decision — what a task is a slice of:

- **Agile** slices by *outcome*. A task delivers something demonstrable end to
  end, so the first thing that works arrives early and is wrong in ways you can
  see. The cost is repeatedly touching the same files, which for agents means
  more merge friction than a human team would accept.
- **Waterfall** slices by *layer*. Every task of a kind is planned together and a
  layer completes before the next begins. The cost is that nothing is
  demonstrable until late, so a misunderstanding of the brief survives until it
  is expensive. It genuinely wins when the shape is known and fixed — a
  migration, a port, a specified API — which is more common in agent work than
  agile orthodoxy would admit.

This is guidance in the planning prompt rather than a constraint in the
scheduler, and deliberately so. The scheduler enforces dependencies; it has no
idea what a task means. A process model is a claim about how to *decompose*
work, and the only thing that decomposes work here is the manager.
"""

from typing import Any

MODELS: dict[str, dict[str, Any]] = {
    "agile": {
        "label": "Agile — thin slices, working software early",
        "when": "The outcome is not fully known, or the boss will want to change "
                "direction after seeing something run.",
        "guidance": (
            "## How to break this work up\n\n"
            "Slice by OUTCOME, not by layer. Every task should deliver something "
            "demonstrable on its own — a user-visible capability that works end to "
            "end, however narrow — rather than 'all the models' or 'the API layer'.\n"
            "- The first sprint must produce something that RUNS. A thin path all the "
            "  way through beats a complete foundation with nothing on top of it.\n"
            "- Prefer several narrow vertical tasks over one broad horizontal one, "
            "  even when the horizontal one looks tidier on the dependency graph.\n"
            "- Depth over breadth is a later problem. Do not plan for scale, "
            "  configurability or edge cases that nothing has asked for yet.\n"
            "- Two tasks that must edit the same file are a dependency, not a "
            "  parallel pair. Agents on one branch collide, and a collision costs "
            "  more than the parallelism saved."
        ),
    },
    "waterfall": {
        "label": "Waterfall — settle each layer before the next",
        "when": "The shape is already known and fixed: a migration, a port, an "
                "implementation of a written specification.",
        "guidance": (
            "## How to break this work up\n\n"
            "Slice by LAYER. Settle each one before the next depends on it: "
            "contracts and schemas first, then the implementation beneath them, then "
            "what sits on top.\n"
            "- Get the interfaces right before anything is built against them. In "
            "  this model a contract that changes later invalidates finished work, "
            "  which is the specific cost you are accepting for the ordering.\n"
            "- Tasks in the same layer usually have no dependency on each other and "
            "  should run in parallel.\n"
            "- The risk you are carrying is that nothing is demonstrable until late, "
            "  so a misreading of the brief survives until it is expensive to fix. "
            "  If any part of the brief is ambiguous, ask NOW rather than after a "
            "  layer is built on the assumption."
        ),
    },
}

DEFAULT = "agile"


def normalise(name: str | None) -> str:
    return name if name in MODELS else DEFAULT


def guidance(project: dict) -> str:
    """The planning instructions for this project's process model."""
    model = MODELS[normalise((project or {}).get("process"))]
    return model["guidance"]


def catalog() -> list[dict]:
    """For the UI, so the choice is informed rather than a coin flip."""
    return [{"id": k, "label": v["label"], "when": v["when"], "default": k == DEFAULT}
            for k, v in MODELS.items()]
