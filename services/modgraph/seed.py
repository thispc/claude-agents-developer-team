"""The seed — the platform's own module inventory, read from the working tree.

`seed_self_graph()` is the OFFLINE fallback (authored_by='seed'): a deterministic
read of the real tree — real docstrings as specs, test mapping parsed from real
imports — so the graph exists on first boot with no credentials and no model
call. The crew's manager supersedes it with its own plan the first time it runs
live (`modgraph_author`, which stays in the conductor because it needs providers,
the repair headroom and the crew); the seed never overwrites a plan a manager
authored.

WHY A SERVICE READS SOURCE FILES. It reads the WORKING TREE — the thing the graph
is a description of — and it reads it the way a linter does: open, parse, close.
That is not the rule this fleet's isolation is about. The rule is one process per
database (`SERVICE_CONTRACT` 1) and no imports across directories (rule 9), and
both hold: nothing here imports the conductor, and the only database this process
opens is its own. What it must never do is EXECUTE any of it, or shell out into
the checkout — the affected-tests runner stays conductor-side for exactly that
reason.

`REPO_ROOT` is env-only like everything else, defaulting to the process's cwd,
which is the repo root under process-compose (and under `run-local.sh --legacy`,
and in every test that mounts this service). The same convention `DB_PATH=data/…`
and `LEGACY_DB_PATH=devteam.db` already rely on.

P6 REPLACES THE CONTENT OF THIS FILE, not its shape. The tables below name the
platform's CODE modules; the plan's next phase makes the nodes the FLEET's
services, read from `services.yaml` and process-compose's live state
(`seed_fleet_graph`). The builder, the manifest comparison and the
idempotence-by-dict-equality discipline all survive that; the three tables do not.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import derive
import store

ROOT = Path(os.environ.get("REPO_ROOT") or ".").resolve()

# key, title, docstring source, tags, boundary manifest (dirs end in '/')
_SELF_MODULES: list[tuple[str, str, str, list[str], list[str]]] = [
    ("routes", "The HTTP surface", "conductor/app/routes/__init__.py",
     ["backend", "surface"],
     ["conductor/app/routes/", "conductor/app/main.py"]),
    ("guards", "Ownership gates", "conductor/app/guards.py",
     ["backend", "kernel"],
     ["conductor/app/guards.py", "conductor/app/auth.py"]),
    ("shell", "Child processes", "conductor/app/shell.py",
     ["backend", "kernel"],
     ["conductor/app/shell.py"]),
    ("db", "Persistence", "conductor/app/db.py",
     ["backend", "kernel"],
     ["conductor/app/db.py"]),
    ("manager", "The manager agent", "conductor/app/manager.py",
     ["backend", "domain"],
     ["conductor/app/manager.py", "conductor/app/planner.py",
      "conductor/app/interview.py", "conductor/app/process.py"]),
    ("orchestration", "Scheduling and dispatch", "conductor/app/scheduler.py",
     ["backend", "domain"],
     ["conductor/app/scheduler.py", "conductor/app/launcher.py", "conductor/app/team.py"]),
    ("repair", "Self-repair — the IT crew", "conductor/app/repair.py",
     ["backend", "domain"],
     ["conductor/app/repair.py", "conductor/app/repair_builder.py",
      "conductor/app/repair_routes.py"]),
    # P4 moved the substrate into services/lifeworld. What is left conductor-side is
    # the doorway (/api/lw/* proxy) and the client; both are this node's boundary too,
    # because a change to either is a change to the lifeworld from where anyone stands.
    ("lifeworld", "The Lifeworld", "services/lifeworld/app.py",
     ["backend", "domain"],
     ["services/lifeworld/", "conductor/app/lifeworld_routes.py",
      "conductor/app/lifeworld_client.py"]),
    ("knowledge", "What agents have learned", "conductor/app/knowledge.py",
     ["backend", "domain"],
     ["conductor/app/knowledge.py"]),
    ("ops", "The platform watching itself", "conductor/app/selfops.py",
     ["backend", "domain"],
     ["conductor/app/selfops.py", "conductor/app/logs.py", "conductor/app/logs_routes.py",
      "conductor/app/monitor.py", "conductor/app/upkeep.py", "conductor/app/usage.py"]),
    ("worker", "The worker agent", "worker/worker.py",
     ["backend", "agent"],
     ["worker/"]),
    ("dash-core", "Dashboard shell", "dashboard/js/core.js",
     ["frontend", "kernel"],
     ["dashboard/index.html", "dashboard/style.css", "dashboard/js/core.js",
      "dashboard/js/lib.js", "dashboard/js/boot.js"]),
    ("dash-views", "Dashboard views", "dashboard/js/projects.js",
     ["frontend"],
     ["dashboard/js/projects.js", "dashboard/js/agent.js", "dashboard/js/repair.js",
      "dashboard/js/ops.js", "dashboard/js/studio.js", "dashboard/js/studio-legacy.js"]),
    ("canvas", "The canvas", "dashboard/canvas2/index.js",
     ["frontend"],
     ["dashboard/canvas2/", "dashboard/js/canvas1.js"]),
]

# The architecture layers — the TOP level of the two-level graph. key, title, one
# honest sentence, tags, and which of the modules above are its children. A group
# carries no boundary of its own: its paths are the UNION of its children's,
# computed in _self_manifest so the top level can never drift from what the
# modules underneath it actually cover.
_SELF_GROUPS: list[tuple[str, str, str, list[str], list[str]]] = [
    ("backend", "Backend",
     "The HTTP surface and the machinery under it: routes, the ownership gates, "
     "child processes, and the deterministic scheduling that dispatches work.",
     ["backend"], ["routes", "guards", "shell", "orchestration"]),
    ("frontend", "Frontend",
     "Everything the browser runs: the dashboard shell, its views, and the canvas.",
     ["frontend"], ["dash-core", "dash-views", "canvas"]),
    ("data", "Data layer",
     "Where facts live: the SQLite persistence helpers and the knowledge the "
     "agents have banked.",
     ["backend", "data"], ["db", "knowledge"]),
    ("agents", "Agents",
     "The beings that do the work: the hidden manager, the worker process, and "
     "the lifeworld substrate they inhabit.",
     ["backend", "agent"], ["manager", "worker", "lifeworld"]),
    ("selfrepair", "Self-repair",
     "The platform improving itself: the IT crew's engine and the self-watching "
     "ops record it acts on.",
     ["backend", "domain"], ["repair", "ops"]),
]

# Typed cross-module edges with the REAL contracts the two sides honour today. The
# `rule` is prose for the inspector; where a contract is machine-checkable its terms
# are separate JSON fields so a test can hold the repo to them literally.
_SELF_EDGES: list[tuple[str, str, str, dict]] = [
    ("db", "routes", "data",
     {"rule": "rows are read and written only through app.db helpers; no route opens "
              "its own connection"}),
    ("guards", "routes", "interface",
     {"rule": "every route resolves who-may-see through app.guards; missing and "
              "forbidden both answer 404, so a guessed id learns nothing"}),
    ("lifeworld", "routes", "interface",
     {"kind": "ports", "package": "services/lifeworld/substrate", "door": "ports.py",
      "pattern": "from ..",
      "rule": "a parent-package import appears nowhere in services/lifeworld/substrate/ "
              "except ports.py — the substrate talks to the platform through one door, "
              "and since P4 that door is HTTP"}),
    ("db", "orchestration", "data",
     {"rule": "the scheduler's whole world view — tasks, deps, budgets — is rows; "
              "no scheduling state lives in memory"}),
    ("manager", "orchestration", "interface",
     {"rule": "the manager declares tasks and verdicts; the deterministic scheduler "
              "does the dispatch mechanics for free — no model call ever dispatches"}),
    ("orchestration", "worker", "interface",
     {"rule": "the launcher passes the worker env contract (TASK_ID, PROJECT_ID, BRANCH, "
              "CONDUCTOR_URL, WORKER_TOKEN, MODEL); worker.py reads exactly that"}),
    ("worker", "routes", "interface",
     {"rule": "a worker reports only through /internal/* with the shared token, and only "
              "on the task it was actually given (_owns_task)"}),
    ("shell", "repair", "interface",
     {"rule": "the repair builder runs child processes only through shell.sh / shell.git — "
              "one place owns the missing-binary and timeout contract"}),
    ("knowledge", "repair", "data",
     {"rule": "a specialist's experience reaches its next build via knowledge.recall, "
              "matched on the situation (cue), never on the lesson"}),
    ("ops", "repair", "data",
     {"rule": "scout and retro read the platform's own record (logs, notices, usage); "
              "repair acts on what ops wrote down"}),
    ("routes", "dash-core", "interface",
     {"rule": "the dashboard talks to the backend only through /api/* and /ws; "
              "no other channel exists"}),
    ("dash-core", "dash-views", "interface",
     {"kind": "load-order", "file": "dashboard/index.html", "before": "js/core.js",
      "after": ["js/ops.js", "js/projects.js", "js/studio-legacy.js", "js/studio.js",
                "js/agent.js", "js/repair.js"],
      "rule": "classic scripts share one global scope; index.html's script order IS the "
              "dependency order, and core.js loads before every view"}),
    ("dash-core", "canvas", "interface",
     {"kind": "load-order", "file": "dashboard/index.html", "before": "js/core.js",
      "after": ["js/canvas1.js"],
      "rule": "canvas1.js mounts inside screens the shell owns; core.js loads first"}),
]


def _first_para(rel: str) -> str:
    """The first paragraph of a module's real docstring (or leading // block for JS),
    read from the tree at seed time — the spec is what the module says it is, not what
    a seed file remembered it being."""
    p = ROOT / rel
    try:
        text = p.read_text()
    except OSError:
        return ""
    if rel.endswith(".py"):
        parts = text.split('"""')
        if len(parts) < 2:
            return ""
        para = parts[1].strip().split("\n\n")[0]
    else:
        lines = []
        for line in text.splitlines():
            if not line.startswith("//"):
                break
            stripped = line.lstrip("/").strip()
            if not stripped and lines:
                break                          # a bare // ends the first paragraph
            if stripped:
                lines.append(stripped)
        para = " ".join(lines)
    return " ".join(para.split())[:600]


def _covers(paths: list[str], rel: str) -> bool:
    """Whether a boundary manifest claims this file. Dirs end in '/'."""
    return any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in paths)


def _node_for_module(mod: str, by_key: dict[str, list[str]]) -> str:
    """Which node owns python module app.<mod> — resolved through the manifests
    themselves, so the mapping can never disagree with the boundaries."""
    file_c, dir_c = f"conductor/app/{mod}.py", f"conductor/app/{mod}/"
    for key, paths in by_key.items():
        if dir_c in paths or _covers(paths, file_c):
            return key
    return ""


# Leading whitespace is allowed on purpose: tests routinely import inside a test
# function ("from app.routes import Settings" four spaces deep), and anchoring at
# column 0 made every such suite invisible — the `routes` leaf sat at "no tests
# mapped" while its group rolled up 22, because all its imports were indented.
_IMPORT_RES = (
    re.compile(r"^[ \t]*from app import ([\w ,]+)", re.M),
    re.compile(r"^[ \t]*from app\.(\w+)", re.M),
    re.compile(r"^[ \t]*import app\.(\w+)", re.M),
)


def tests_for_nodes(by_key: dict[str, list[str]]) -> dict[str, set[str]]:
    """Map each tests/test_*.py to the node(s) it exercises — mechanically.

    Backend: parsed imports (`from app import x, y`, `from app.y…`) resolved through
    the boundary manifests. Frontend/worker: the file reads the tests actually do —
    conftest's dashboard_js() concatenates every view script, reading index.html or
    style.css is the shell, naming canvas files is the canvas, adding worker/ to
    sys.path is the worker. Derived, not curated: a new test file maps itself.

    Shared with the manager's authoring pass over the wire (`POST /tests-for-nodes`),
    because the manager authors BOUNDARIES and never authors which tests exist —
    and two copies of this parser would disagree the first time one was improved."""
    out: dict[str, set[str]] = {}
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        src = f.read_text()
        rel = f"tests/{f.name}"
        hit: set[str] = set()
        mods: set[str] = set()
        for m in _IMPORT_RES[0].findall(src):
            for name in m.split(","):
                mods.add(name.strip().split(" as ")[0].strip())
        for pat in _IMPORT_RES[1:]:
            mods.update(pat.findall(src))
        for mod in mods:
            key = _node_for_module(mod, by_key)
            if key:
                hit.add(key)
        if "dashboard_js" in src:
            hit.update(("dash-core", "dash-views"))
        if "index.html" in src or "style.css" in src:
            hit.add("dash-core")
        if "canvas2" in src or "canvas1" in src:
            hit.add("canvas")
        if '"worker"' in src or "worker/" in src:
            hit.add("worker")
        if hit:
            out[rel] = hit
    return out


def dedupe_tests(test_list: list[dict]) -> list[dict]:
    """A file can qualify as a contract on the same node it suites; the UNIQUE index
    dedupes the rows, so dedupe the manifest the same way or equality would lie."""
    seen, out = set(), []
    for t in sorted(test_list, key=lambda t: (t["node"], t["kind"], t["path"])):
        k = (t["node"], t["kind"], t["path"])
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def self_manifest() -> dict:
    """The whole seed graph as one comparable value — TWO LEVELS: aim → group
    nodes (the architecture layers) → their module children → conclusion.
    Nodes and edges in a fixed order, tests sorted. Idempotence is dict
    equality on this, nothing cleverer.

    The ONE manifest builder: shared by the offline seed and, over the wire, by
    the manager's authoring pass. Both describe the same tree, so both must read
    it the same way; two builders would disagree the first time one of them was
    improved."""
    by_key = {key: paths for key, _, _, _, paths in _SELF_MODULES}
    parent_of = {child: gkey for gkey, _, _, _, children in _SELF_GROUPS
                 for child in children}
    node_list: list[dict] = [{
        "key": "aim", "title": "devteam — a platform whose agent teams build software, "
                               "including this platform", "node_type": "aim",
        "spec": "Decomposed into the layers below, each layer into its modules; every "
                "module carries its own spec, boundary, suite and agent. The aim is "
                "what every module exists in service of.",
        "join_mode": "all_of", "parent_key": "", "tags": [], "paths": []}]
    for gkey, gtitle, gspec, gtags, children in _SELF_GROUPS:
        node_list.append({"key": gkey, "title": gtitle, "node_type": "group",
                          "spec": gspec, "join_mode": "all_of", "parent_key": "",
                          "tags": gtags,
                          "paths": sorted({p for c in children for p in by_key[c]})})
    for key, title, doc, tags, paths in _SELF_MODULES:
        node_list.append({"key": key, "title": title, "node_type": "code",
                          "spec": _first_para(doc), "join_mode": "all_of",
                          "parent_key": parent_of.get(key, ""), "tags": tags,
                          "paths": paths})
    node_list.append({
        "key": "conclusion", "title": "The running platform", "node_type": "conclusion",
        "spec": "The deliverable: this server, serving this dashboard, improving this "
                "repository. Health is read from the platform's own monitor and the "
                "crew's phase, not asserted.",
        "join_mode": "all_of", "parent_key": "", "tags": [], "paths": []})

    tmap = tests_for_nodes(by_key)
    child_edges: list[dict] = []
    for src, dst, etype, contract in _SELF_EDGES:
        # A test that names both sides of an edge is that edge's contract test —
        # mechanically, first in sorted order when several qualify.
        both = sorted(p for p, keys in tmap.items() if src in keys and dst in keys)
        child_edges.append({"src": src, "dst": dst, "edge_type": etype,
                            "contract": contract, "contract_test": both[0] if both else ""})
    # The top level frames GROUPS: aim feeds every layer, every layer feeds the
    # conclusion, and layer-to-layer edges are derived from their children's.
    # Child edges stay as they are, within and across groups — the canvas clips
    # by level, so both stories are told without either lying.
    edge_list: list[dict] = (
        [{"src": "aim", "dst": gkey, "edge_type": "depends",
          "contract": {}, "contract_test": ""} for gkey, *_ in _SELF_GROUPS]
        + derive.derive_group_edges(child_edges, parent_of)
        + child_edges
        + [{"src": gkey, "dst": "conclusion", "edge_type": "depends",
            "contract": {}, "contract_test": ""} for gkey, *_ in _SELF_GROUPS])

    # Test rows live on LEAVES: contract rows come from child edges only — a
    # group answers through its children's rollup, not through rows of its own.
    test_list = dedupe_tests(
        [{"node": key, "kind": "suite", "path": path}
         for path, keys in tmap.items() for key in keys] +
        [{"node": e["dst"], "kind": "contract", "path": e["contract_test"]}
         for e in child_edges if e["contract_test"]])
    return {"nodes": node_list, "edges": edge_list, "tests": test_list}


def manifest_of(plan_id: int) -> dict:
    """The stored plan, re-shaped for comparison with self_manifest()."""
    return {
        "nodes": [{"key": n["key"], "title": n["title"], "node_type": n["node_type"],
                   "spec": n["spec"], "join_mode": n["join_mode"],
                   "parent_key": n["parent_key"], "tags": n["tags"], "paths": n["paths"]}
                  for n in store.nodes(plan_id)],
        "edges": [{"src": e["src_key"], "dst": e["dst_key"], "edge_type": e["edge_type"],
                   "contract": e["contract"], "contract_test": e["contract_test"]}
                  for e in store.edges(plan_id)],
        "tests": [{"node": t["node_key"], "kind": t["kind"], "path": t["path"]}
                  for t in store.tests(plan_id)],
    }


def seed_self_graph() -> int:
    """Ensure project 0 has an active plan; return its id.

    Idempotent: an unchanged tree reseeds to zero writes. Drift — a module docstring
    reworded, a test file added — makes a NEW version and supersedes the old one; the
    old rows are never touched, because a fallback that edits history is exactly as
    untrustworthy as a manager that does. A plan the crew's manager authored outranks
    the seed entirely: the fallback never overwrites deliberate work."""
    cur = store.active_plan(0)
    if cur and cur["authored_by"] != "seed":
        return int(cur["id"])
    man = self_manifest()
    if cur and manifest_of(cur["id"]) == man:
        return int(cur["id"])
    plan = store.import_plan(
        0, kind="template", authored_by="seed",
        notes="offline fallback, regenerated from the working tree",
        nodes_in=man["nodes"], edges_in=man["edges"], tests_in=man["tests"])
    return int(plan["id"])
