"""modgraph_health.py — the graph's tri-state, and earned mastery.

WHAT IS LEFT HERE AFTER P6, and what left. This file used to hold three things.
Two of them were honest only while the platform's "modules" were in-process code,
and P6 deleted them outright:

  PROBES        a function per module that IMPORTED the module it checked and
                asserted something about it — the router has routes, shell spawns
                a child, the package still greps clean. There is nothing to import
                any more: a card is a process behind a port. The beat is now
                process-compose's readiness AND the service's own `GET /health`,
                asked through the same shim every caller uses, in `fleet.py`.
  SERVICES      the honest-switch table, whose entire content was "almost nothing
                here has an off switch — here is the reason". Everything has one
                now (`POST /process/start/{name}`, `PATCH /process/stop/{name}`),
                and the handful of cards that genuinely refuse do it in `fleet.py`
                beside the switches that work, so the two can never disagree.

What stayed is what was never about in-process code in the first place — three
pure functions over a beat and some test rows, and the name of the mastery query:

**HEALTH (the mapping).** `health_of` is the tri-state contract: red iff the beat
fails; yellow iff the beat is fine but the suite is red; green otherwise,
including never-run, because absence of evidence is not a warning. `rollup` is a
group answering for its children, worst-of. `tests_state` is what a node's test
rows are allowed to claim.

**MASTERY.** An agent that keeps finishing work on a card becomes its master —
computed from the trace (`graph_node_runs`), never stored, so it cannot drift from
what actually happened. Counted across ALL plan versions BY NODE KEY, and since
P6 a node key is a SERVICE NAME, which makes that identity stronger than it has
ever been: a replan, a retitle and a reordered registry all leave it alone. The
arithmetic lives in the modgraph service (it is a JOIN over two of its tables);
this module keeps the name so nothing above it had to learn where it went.
"""

from __future__ import annotations


# --- the tri-state mapping: pure functions over beat + test rows --------------

def tests_state(rows: list[dict]) -> str:
    """"pass" | "fail" | "none" from a node's graph_node_tests rows. Only rows a run
    has actually spoken about count — 'mapped'/'written' are suites that exist but
    have never been exercised, and claiming "pass" for those would be the canvas
    inventing green."""
    ran = [t for t in rows if t.get("status") in ("passing", "failing", "error")]
    if not ran:
        return "none"
    return "fail" if any(t["status"] in ("failing", "error") for t in ran) else "pass"


def health_of(beat: str, tests: str) -> dict:
    """The contract's tri-state: red iff the beat fails; yellow iff the beat is fine
    but the suite is red; green otherwise (including never-run — absence of evidence
    is not a warning)."""
    status = "red" if beat == "fail" else ("yellow" if tests == "fail" else "green")
    return {"beat": beat, "tests": tests, "status": status}


def rollup(children: list[dict]) -> dict:
    """A group's health from its children's: red if any child is red, else yellow if
    any yellow, else green — with beat and tests aggregated the same way so the three
    fields can never tell two different stories.

    Since P6 the only groups are the two ROOMS (the worker pool, the apps room), and
    an empty room is green: a platform with nothing to build has no workers running,
    and that is idle, not broken."""
    if not children:
        return {"beat": "ok", "tests": "none", "status": "green"}
    beat = "fail" if any(c["beat"] == "fail" for c in children) else "ok"
    if any(c["tests"] == "fail" for c in children):
        tests = "fail"
    elif any(c["tests"] == "pass" for c in children):
        tests = "pass"
    else:
        tests = "none"
    if any(c["status"] == "red" for c in children):
        status = "red"
    elif any(c["status"] == "yellow" for c in children):
        status = "yellow"
    else:
        status = "green"
    return {"beat": beat, "tests": tests, "status": status}


# --- mastery: earned from the trace, never stored -----------------------------

def mastery(project_id: int = 0) -> dict[str, dict] | None:
    """{node_key: {"agent_id", "runs", "master"}} — the top agent per node, or None.

    Counted over CLOSED ok runs of kind build|verify across EVERY plan version of the
    project: node keys are the stable identity, so mastery survives a replan by
    construction. Ties keep the incumbent — the agent whose first ok run came earlier —
    because a challenger 'catches up to' a master, it does not split the title, and a
    later arrival must EXCEED the master's count to take over.

    The query moved to the modgraph service with the trace it reads; this is the
    conductor's name for it. NONE, NOT {}, when the store cannot be read: the
    authoring pass's "a master keeps its module" rule consults this, and an outage
    that looked like "nobody has earned anything" would let one reshuffle undo every
    earned continuity on the box. Callers that only DISPLAY mastery treat None as
    "nothing to show" — which is honest, and which the payload's own `degraded` flag
    already explains."""
    from . import modgraph
    return modgraph.mastery(project_id)
