"""Whether a change to the orchestration algorithm actually helped.

Before this, the only numbers the platform kept were dollars and wall-clock per
project. Those are outcomes, not diagnostics: a project that cost $3 tells you
nothing about whether it cost $3 because the plan was good or because half the
work was thrown away and redone by a bigger model. Two different tweaks — one
that eliminated rework, one that changed nothing — produce the same project-level
number, so there was no way to tell them apart.

The unit here is the *run*: one agent dispatch, stamped with the role, the model,
the attempt number, and the tuning profile in force. From that, the numbers that
actually discriminate between a good algorithm and a lucky one:

- **first_pass** — share of tasks accepted on attempt 1. The single best signal
  that planning and briefing are working, because it is the one number that
  cannot be improved by spending more.
- **runs_per_task** — dispatches per delivered task. Rework, made visible.
- **cost_per_task** — what a delivered task costs, not what a project costs.
- **escalation_rate** — how often cheap models were not enough. If this is high,
  the cheap tier is not saving money, it is buying a failed attempt first.

`compare()` groups by tuning profile, which is the whole point: you change a
knob, stamp subsequent runs with a new profile, and read the two side by side.
Everything is computed from stored rows, so the comparison is retrospective —
you never have to have decided in advance to measure something.
"""

import time
from typing import Any

from . import db

# 'delivered' is a real terminal state, not a missing one: the worker finished and
# nobody judged it. Counting those as successes would flatter every configuration
# that ships fast and reviews nothing.
TERMINAL = ("accepted", "rework", "failed", "abandoned", "delivered")


def _summarise(runs: list[dict]) -> dict[str, Any]:
    done = [r for r in runs if r["outcome"] in TERMINAL]
    accepted = [r for r in done if r["outcome"] == "accepted"]
    tasks = {r["task_id"] for r in done if r["task_id"]}
    accepted_tasks = {r["task_id"] for r in accepted if r["task_id"]}
    first_pass = [r for r in accepted if r["attempt"] <= 1]
    escalated = [r for r in done if r["attempt"] > 1]
    cost = sum(r["cost_usd"] or 0 for r in done)
    durations = [r["ended_at"] - r["started_at"] for r in done
                 if r["ended_at"] and r["started_at"]]

    def per_task(total: float) -> float:
        # Denominator is DELIVERED tasks, not attempted ones. Dividing by
        # attempted tasks flatters a configuration that fails a lot: the failures
        # add to the denominator and make the average look cheap.
        return round(total / len(accepted_tasks), 4) if accepted_tasks else 0.0

    return {
        "runs": len(done),
        "running": len(runs) - len(done),
        "tasks_attempted": len(tasks),
        "tasks_delivered": len(accepted_tasks),
        "first_pass": round(len(first_pass) / len(accepted_tasks), 3) if accepted_tasks else 0.0,
        "runs_per_task": per_task(len(done)),
        "cost_usd": round(cost, 4),
        "cost_per_task": per_task(cost),
        "escalation_rate": round(len(escalated) / len(done), 3) if done else 0.0,
        "failure_rate": round(
            len([r for r in done if r["outcome"] == "failed"]) / len(done), 3) if done else 0.0,
        "rework_rate": round(
            len([r for r in done if r["outcome"] == "rework"]) / len(done), 3) if done else 0.0,
        "median_seconds": round(sorted(durations)[len(durations) // 2]) if durations else 0,
        "total_seconds": round(sum(durations)),
    }


def _group(runs: list[dict], key: str) -> list[dict]:
    buckets: dict[str, list[dict]] = {}
    for r in runs:
        buckets.setdefault(str(r.get(key) or "—"), []).append(r)
    out = [{key: name, **_summarise(rows)} for name, rows in buckets.items()]
    out.sort(key=lambda d: d["runs"], reverse=True)
    return out


def project(project_id: int) -> dict[str, Any]:
    """Everything worth knowing about one project's runs."""
    runs = db.list_runs(project_id, limit=5000)
    return {
        "overall": _summarise(runs),
        "by_role": _group(runs, "role"),
        "by_model": _group(runs, "model"),
        "by_sprint": _group(runs, "sprint"),
    }


def compare(limit: int = 5000) -> dict[str, Any]:
    """Tuning profiles side by side — the reason the runs table exists.

    A profile with very few runs is reported but flagged: the difference between
    two configurations measured over three tasks each is noise, and presenting it
    as a result is how you end up tuning towards randomness.
    """
    runs = db.list_runs(None, limit=limit)
    rows = _group(runs, "profile")
    for row in rows:
        row["confident"] = row["tasks_delivered"] >= 10
    return {
        "profiles": rows,
        "note": "Profiles with fewer than 10 delivered tasks are not a result yet.",
    }


def sprint(project_id: int, sprint_no: int) -> dict[str, Any]:
    runs = [r for r in db.list_runs(project_id, limit=5000) if r["sprint"] == sprint_no]
    return _summarise(runs)


def reconcile(project_id: int, max_age: float = 7200) -> int:
    """Close out runs whose process died without reporting.

    A run left 'running' forever poisons every average that follows it, because
    it is neither a success nor a failure and silently shrinks the denominator.
    A restart mid-dispatch is the normal way this happens, so it needs sweeping
    rather than preventing.
    """
    now = time.time()
    stale = [r for r in db.open_runs(project_id) if now - r["started_at"] > max_age]
    for r in stale:
        db.finish_run(r["id"], "abandoned", reason="no report before the process ended")
    return len(stale)
