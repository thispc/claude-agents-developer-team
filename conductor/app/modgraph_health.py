"""modgraph_health.py — the graph's runtime truth: real heartbeats, honest switches, earned
mastery.

Three things live here, and all three exist to keep the canvas from lying:

**HEALTH.** Every node's `beat` comes from a REAL probe of the module it names — the router
is actually mounted, the database actually answers SELECT 1, shell actually spawns a child —
not from "the file exists so it must be fine". All probes are offline-safe (no network, no
model, no credentials) and the whole sweep is cached ~10s because /api/graph/self is polled.
A probe that raises is a probe that failed; nothing here may take the payload down with it.

**SERVICE.** Start/Stop is only offered where a real runtime switch exists, and each entry in
SERVICES says which switch that is. A module without one reports state "none" and
control:false with the reason — the alternative is a stop button that pretends, and a stop
button that pretends is worse than none, because the operator plans around what it claims.

**MASTERY.** An agent that keeps finishing work on a module becomes its master — computed
from the trace (graph_node_runs), never stored, so it can not drift from what actually
happened. Counted across ALL plan versions by node key, because keys are the stable identity:
a replan must not amnesty away who knows this module best.
"""

from __future__ import annotations

import ast
import os
import time
from typing import Callable

from . import config, db

# How long a heartbeat sweep stays trusted. The payload is polled every few seconds;
# probing on every poll would spend a process spawn (shell) per poll for no new truth.
BEAT_TTL_S = 10

# Verified ok runs on one node before its top agent is called the master. Three, not one:
# a single lucky landing is not mastery, and the number must be small enough to be earnable
# within a day of sprints.
MASTER_RUNS = 3


# --- the probes: one per module, each a real check of the thing it names ------

def _probe_routes() -> bool:
    """The HTTP surface: the shared router actually holds routes and the app imports."""
    from .routes import router
    return len(router.routes) > 0


def _probe_guards() -> bool:
    """The ownership gates: importable and the gate functions are still there."""
    from . import auth, guards
    return callable(guards._root) and callable(guards.current_user) and hasattr(auth, "init")


def _probe_shell() -> bool:
    """Child processes: actually spawn one. `true` exiting 0 is the whole contract."""
    from . import shell
    return shell.sh("true").returncode == 0


def _probe_db() -> bool:
    """Persistence: a trivial SELECT through the app's own helpers."""
    rows = db._rows("SELECT 1 AS one")
    return bool(rows and rows[0]["one"] == 1)


def _probe_knowledge() -> bool:
    """The knowledge store: its table exists and the embedder is callable."""
    from . import knowledge
    rows = db._rows("SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge'")
    return bool(rows) and callable(knowledge.embed)


def _probe_repair() -> bool:
    """The IT crew's engine: lease and state readable, and the loop tick fresh-or-idle.
    A stale lease only condemns an engine that claims to be mid-phase — a box where the
    crew never ran (or rests) is healthy, not dead."""
    from . import repair
    st = repair.state()
    lease = db.kv_get("repair:lease") or {}
    fresh = time.time() - float(lease.get("ts") or 0) < repair.TICK_SECONDS * 3
    idle = (not repair.enabled()) or st.get("phase") in ("idle", "sleeping")
    return bool(fresh or idle)


def _probe_ops() -> bool:
    """The self-watching record rides shell for everything it does — same real spawn."""
    return _probe_shell()


_WORKER_SRC: dict = {"mtime": None, "ok": False}


def _probe_worker() -> bool:
    """The worker agent: its script parses (cached by mtime — parsing per poll is waste)
    and the workspaces directory is actually writable, because a worker that cannot
    clone is a worker in name only."""
    script = config.WORKER_SCRIPT
    try:
        mtime = script.stat().st_mtime
    except OSError:
        return False
    if _WORKER_SRC["mtime"] != mtime:
        try:
            ast.parse(script.read_text())
            _WORKER_SRC.update(mtime=mtime, ok=True)
        except (OSError, SyntaxError):
            _WORKER_SRC.update(mtime=mtime, ok=False)
    if not _WORKER_SRC["ok"]:
        return False
    try:
        config.WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(config.WORKSPACES_DIR, os.W_OK)


def _probe_canvas() -> bool:
    """The canvas: the v2 engine's files are present where the server serves them from."""
    c2 = config.DASHBOARD_DIR / "canvas2"
    return c2.is_dir() and (c2 / "index.js").exists() and (c2 / "world.js").exists()


def _probe_manager() -> bool:
    """The manager agent: importable, and the entry point projects actually call exists."""
    from . import manager, planner  # noqa: F401  (import IS the check)
    return callable(manager.run_manager)


def _probe_orchestration() -> bool:
    """Scheduling and dispatch: importable, and the two doors all work goes through —
    the deterministic scheduler and the launcher — are still doors."""
    from . import launcher, scheduler
    return callable(scheduler.ensure) and callable(launcher.dispatch_task)


_PORTS_RULE: dict = {"ts": 0.0, "ok": False}
PORTS_RULE_TTL_S = 300   # a whole-package grep; the rule changes when code lands, not per poll


def _probe_lifeworld() -> bool:
    """The Lifeworld: importable, plus its key invariant held literally — `from ..`
    appears nowhere in the package except ports.py, the one door to the app."""
    from . import lifeworld  # noqa: F401
    now = time.time()
    if now - _PORTS_RULE["ts"] > PORTS_RULE_TTL_S:
        pkg = config.ROOT / "conductor" / "app" / "lifeworld"
        try:
            offenders = [f.name for f in pkg.glob("*.py")
                         if "from .." in f.read_text() and f.name != "ports.py"]
            _PORTS_RULE.update(ts=now, ok=not offenders)
        except OSError:
            _PORTS_RULE.update(ts=now, ok=False)
    return _PORTS_RULE["ok"]


def _paths_probe(paths: list[str]) -> bool:
    """The fallback (and the dash leaves' real check): every file in the node's boundary
    manifest stats cleanly. A node with no paths (aim, conclusion, an authored frame
    node) has nothing that can be missing, so it beats ok."""
    for p in paths or []:
        if not (config.ROOT / p).exists():
            return False
    return True


# Node key -> its probe. Keys the table does not name (a manager-authored plan invents
# its own) fall back to _paths_probe over the node's manifest — still a real stat, never
# an assumed ok on a named-but-missing boundary.
PROBES: dict[str, Callable[[], bool]] = {
    "routes": _probe_routes,
    "guards": _probe_guards,
    "shell": _probe_shell,
    "db": _probe_db,
    "knowledge": _probe_knowledge,
    "repair": _probe_repair,
    "ops": _probe_ops,
    "worker": _probe_worker,
    "canvas": _probe_canvas,
    "manager": _probe_manager,
    "orchestration": _probe_orchestration,
    "lifeworld": _probe_lifeworld,
    # dash-core / dash-views deliberately absent: their honest check IS the fallback —
    # the real files of their manifests stat cleanly.
}

_BEATS: dict = {"ts": 0.0, "keys": frozenset(), "by_key": {}}


def beats(nodes: list[dict]) -> dict[str, str]:
    """{leaf key: "ok"|"fail"} for every non-group node, one cached sweep.

    Each probe runs inside its own try — a probe that raises IS a failed beat, and one
    broken module must never take the others' answers (or the payload) down with it."""
    keys = frozenset(n["key"] for n in nodes)
    now = time.time()
    if now - _BEATS["ts"] < BEAT_TTL_S and _BEATS["keys"] == keys:
        return dict(_BEATS["by_key"])
    out: dict[str, str] = {}
    for n in nodes:
        if n.get("node_type") == "group":
            continue                      # groups answer by rollup, never by probe
        probe = PROBES.get(n["key"])
        try:
            ok = probe() if probe else _paths_probe(n.get("paths") or [])
        except Exception:
            ok = False
        out[n["key"]] = "ok" if ok else "fail"
    _BEATS.update(ts=now, keys=keys, by_key=dict(out))
    return out


# --- the tri-state mapping: pure functions over beat + test rows --------------

def tests_state(rows: list[dict]) -> str:
    """"pass" | "fail" | "none" from a node's graph_node_tests rows. Only rows a run has
    actually spoken about count — 'mapped'/'written' are suites that exist but have never
    been exercised, and claiming "pass" for those would be the canvas inventing green."""
    ran = [t for t in rows if t.get("status") in ("passing", "failing", "error")]
    if not ran:
        return "none"
    return "fail" if any(t["status"] in ("failing", "error") for t in ran) else "pass"


def health_of(beat: str, tests: str) -> dict:
    """The contract's tri-state: red iff the beat fails; yellow iff the beat is fine but
    the suite is red; green otherwise (including never-run — absence of evidence is not
    a warning)."""
    status = "red" if beat == "fail" else ("yellow" if tests == "fail" else "green")
    return {"beat": beat, "tests": tests, "status": status}


def rollup(children: list[dict]) -> dict:
    """A group's health from its children's: red if any child is red, else yellow if any
    yellow, else green — with beat and tests aggregated the same way so the three fields
    can never tell two different stories."""
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


# --- service: Start/Stop only where a real switch exists ----------------------
#
# The honest map. Every entry names the ACTUAL runtime switch it flips, and anything not
# listed gets DEFAULT_REASON — state "none", control false — because a module compiled
# into the process has no independent off. Never fake a stop.
SERVICES: dict[str, dict] = {
    # The IT crew IS a button: repair.toggle writes kv repair:enabled and the engine's
    # 20s tick obeys it. The truest runtime switch in the whole platform.
    "repair": {"kind": "repair"},
    # The selfrepair GROUP is the crew plus the free ops record; the crew's toggle is the
    # only part of it that runs, so the group's switch is the same button.
    "selfrepair": {"kind": "repair"},
    # The daily self-check runs iff the upkeep_enabled knob is true — a tuning-table
    # value the upkeep loop reads on every pass, so flipping it acts without a restart.
    "ops": {"kind": "knob", "knob": "upkeep_enabled"},
    # The Studio's free background tick is governed by home_life_enabled, read from
    # tuning on every wake — a real runtime switch.
    "lifeworld": {"kind": "knob", "knob": "home_life_enabled"},
    # CANVAS_V2 is read from the environment ONCE, at config import — there is no
    # runtime path that re-reads it, so offering a switch here would be a lie.
    "canvas": {"kind": "none",
               "reason": "CANVAS_V2 is read from the environment at import — restart "
                         "with CANVAS_V2=0 to serve the v1 engine"},
    # The module graph itself: MODULE_GRAPH is the same import-time env flag, and a
    # surface cannot honestly switch itself off from inside a request it is serving.
    "modgraph": {"kind": "none",
                 "reason": "MODULE_GRAPH is read from the environment at import — "
                           "restart with MODULE_GRAPH=0 to remove the surface"},
}

DEFAULT_REASON = "no independent runtime switch — it is compiled into the platform"


def service_of(key: str) -> dict:
    """The contract's service dict for one node: {state, control} (+ reason when the
    node cannot be controlled)."""
    entry = SERVICES.get(key)
    if not entry:
        return {"state": "none", "control": False, "reason": DEFAULT_REASON}
    if entry["kind"] == "repair":
        from . import repair
        return {"state": "running" if repair.enabled() else "stopped", "control": True}
    if entry["kind"] == "knob":
        from . import tuning
        on = bool(tuning.get(entry["knob"]))
        return {"state": "running" if on else "stopped", "control": True}
    return {"state": "none", "control": False, "reason": entry.get("reason", DEFAULT_REASON)}


def service_set(key: str, action: str) -> dict:
    """Flip the REAL switch behind a node. Raises ValueError with the honest reason for
    a node that has none — the route turns that into the 400 the contract promises."""
    entry = SERVICES.get(key)
    on = action == "start"
    if not entry or entry["kind"] == "none":
        raise ValueError((entry or {}).get("reason", DEFAULT_REASON))
    if entry["kind"] == "repair":
        from . import repair
        repair.toggle(on)
    else:
        from . import tuning
        tuning.set(entry["knob"], on, who="graph")
    return service_of(key)


# --- mastery: earned from the trace, never stored -----------------------------

def mastery(project_id: int = 0) -> dict[str, dict]:
    """{node_key: {"agent_id", "runs", "master"}} — the top agent per node.

    Counted over CLOSED ok runs of kind build|verify across EVERY plan version of the
    project: node keys are the stable identity, so mastery survives a replan by
    construction. Ties keep the incumbent — the agent whose first ok run came earlier —
    because a challenger 'catches up to' a master, it does not split the title, and a
    later arrival must EXCEED the master's count to take over."""
    rows = db._rows(
        "SELECT r.node_key AS k, r.agent_id AS a, COUNT(*) AS n, MIN(r.id) AS first"
        " FROM graph_node_runs r JOIN graph_plans p ON p.id = r.plan_id"
        " WHERE p.project_id=? AND r.status='ok' AND r.ended_at IS NOT NULL"
        "   AND r.kind IN ('build','verify') AND r.agent_id IS NOT NULL"
        " GROUP BY r.node_key, r.agent_id", (int(project_id),))
    out: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: (r["k"], -r["n"], r["first"])):
        if r["k"] in out:
            continue                       # sorted best-first: the first row per key wins
        out[r["k"]] = {"agent_id": int(r["a"]), "runs": int(r["n"]),
                       "master": int(r["n"]) >= MASTER_RUNS}
    return out
