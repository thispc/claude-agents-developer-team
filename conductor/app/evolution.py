"""An agent moving up or down the model ladder from what it has actually done.

Zero tokens, by construction. The decision is arithmetic over the `runs` rows the
platform already writes — first-pass rate, rework rate, escalation rate — the same
numbers `metrics` computes. No model is asked whether an agent should be upgraded;
the run record already answers it.

Two directions, both earned:

- **Up** one rung when an agent keeps hitting work its model cannot clear: it
  escalates a lot AND either reworks a lot or lands little on the first try. This
  is the same economics `pick_model` already states — if a cheap model repeatedly
  needs escalation, starting it lower just buys a wasted first attempt.
- **Down** one rung when the work is easy for the model: it lands almost
  everything first-pass and escalates essentially never. An expensive model doing
  what a cheaper one would land first-pass is money spent for nothing.

Three guards keep it honest: a minimum run count (a model changed on three data
points is a coin flip dressed as a decision), one rung per change (never a jump
straight to Opus), and a cooldown (the hysteresis that stops an agent flapping up
and down on an alternating signal, which would thrash spend and make its history
incomparable).

The new model is written only to the global agent. It reaches a project instance
at next hire, and `pick_model` ranks the teammate's own model BELOW escalation and
manager override — so evolution sets a better *starting point* and never overrides
a live in-flight correction. That precedence is the invariant `pick_model`'s
docstring exists to protect.
"""

import time
from typing import Any

from . import bus, db, launcher, metrics, tuning

LADDER = launcher.FALLBACK_ORDER   # [haiku, sonnet, opus]


def _rung(model: str) -> int:
    from . import config
    resolved = config._resolve_model(model) if model else config.WORKER_MODEL
    return LADDER.index(resolved) if resolved in LADDER else 0


def _cooling_down(agent: dict, now: float) -> bool:
    gap = int(tuning.get("home_evolve_cooldown_hours")) * 3600
    return (now - (agent.get("last_evolved_at") or 0)) < gap


def decide(agent: dict, stats: dict) -> tuple[str, str] | None:
    """The pure decision: ('up'|'down', reason) or None. No I/O, no clock — so a
    test can hand it any stats block and read the verdict directly."""
    if agent.get("model_locked"):
        return None
    runs = stats.get("runs", 0)
    if runs < int(tuning.get("home_evolve_min_runs")):
        return None

    fp = stats.get("first_pass", 0.0)
    rework = stats.get("rework_rate", 0.0)
    esc = stats.get("escalation_rate", 0.0)
    rung = _rung(agent.get("model", ""))

    # Up: struggling. Escalating often and either reworking or missing first-pass.
    if rung < len(LADDER) - 1 and esc >= 0.4 and (rework >= 0.25 or fp <= 0.5):
        return "up", (f"escalated on {int(esc * 100)}% of {runs} runs with "
                      f"{int(rework * 100)}% rework — the model is being outrun by the work")
    # Down: coasting. Landing almost everything first try, essentially never escalating.
    if rung > 0 and fp >= 0.9 and esc <= 0.05:
        return "down", (f"landed {int(fp * 100)}% first-pass across {runs} runs with "
                        f"almost no escalation — a cheaper model would clear this")
    return None


def evolve_one(home_id: int) -> dict[str, Any] | None:
    """Apply evolution to one agent if earned. Free. Returns the change or None."""
    if not tuning.get("home_evolve_enabled"):
        return None
    agent = db.get_home_agent(home_id)
    if not agent or agent["status"] != "active":
        return None
    now = time.time()
    if _cooling_down(agent, now):
        return None

    runs = db.runs_for_home(home_id, limit=int(tuning.get("home_evolve_min_runs")) * 5)
    stats = metrics._summarise(runs)
    verdict = decide(agent, stats)
    if not verdict:
        return None

    direction, reason = verdict
    rung = _rung(agent.get("model", ""))
    target = LADDER[rung + (1 if direction == "up" else -1)]
    from_model = agent.get("model") or "(role default)"

    db.update_home_agent(home_id, model=target, last_evolved_at=now)
    signal = {k: stats.get(k) for k in ("runs", "first_pass", "rework_rate",
                                        "escalation_rate", "cost_per_task")}
    db.add_evolution(home_id, direction, from_model, target, reason, signal)
    bus.emit(0, None, "system", "home_evolved",
             {"home": home_id, "name": agent["name"], "direction": direction,
              "to": target, "reason": reason})
    return {"home": home_id, "direction": direction, "from": from_model,
            "to": target, "reason": reason}
