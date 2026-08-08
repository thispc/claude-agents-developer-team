"""modgraph.py — every project as a graph of verified modules, starting with this one.

The platform's own code reads as one vibe-coded blob, and a blob cannot be improved a
module at a time because nobody can say where one module ends. This store holds the
alternative: a PLAN — aim node → GROUP nodes (the architecture layers: backend,
frontend, data, …) → their module children (parent_key names the group) → conclusion
node — where each node carries its spec, its boundary manifest (paths), its test suite,
and its own agent/model config, and each edge is TYPED and carries the contract the two
sides actually honour. Two levels, like an architecture diagram you can zoom: the top
shows aim + groups + conclusion with group-to-group edges DERIVED from their children's
(derive_group_edges); clicking a group is the microscope into its modules. A group's
paths are always the union of its children's, its tests and activity roll up, and work
lands on LEAVES only. The graph is the work model, not a diagram of it: verification
selects tests from it, the crew's live activity is matched onto it, and dispatch (phase
V2) walks it.

WHY PLANS ARE IMMUTABLE VERSIONS. A replan never edits rows — it writes a new plan and
marks the old one superseded, the sprint_artifacts lesson again: a mutable plan makes
"what did we believe when this was built" unanswerable the moment anyone improves it.
Node KEYS stay stable across versions, so assignments and positions survive a replan;
everything else is frozen the day it is authored. The trace (graph_node_runs) and the
planner-authored test source are append-only for the same reason — they are evidence,
and evidence that can be rewritten is not evidence.

WHY ITS OWN SCHEMA. Same precedent as knowledge.py: CREATE IF NOT EXISTS from init(),
zero entries in db.py's append-only migration tuple, so this whole subsystem adds no
migration risk to databases that predate it.

WHY POSITIONS LIVE IN KV, NOT ON NODES. Where a node sits on the canvas is the one
mutable, cosmetic fact in the whole feature, and a column for it would poke a hole in
"plan rows never change". kv `graph:pos:{plan_id}` holds it instead, and ABSENT MEANS
UNSET — a node with no stored position gets auto-layout, never a fabricated [0,0].

`seed_self_graph()` is the OFFLINE fallback (authored_by='seed'): a deterministic read
of the real tree — real docstrings as specs, test mapping parsed from real imports — so
the graph exists on first boot with no credentials and no model call. The crew's manager
supersedes it with its own plan the first time it runs live; the seed never overwrites a
plan a manager authored.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import config, db

SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL,                  -- 0 = the platform's own code
    version     INTEGER NOT NULL DEFAULT 1,        -- replan = new row, never an UPDATE
    kind        TEXT NOT NULL DEFAULT 'template',  -- template | run
    status      TEXT NOT NULL DEFAULT 'draft',     -- draft | active | superseded
    authored_by TEXT NOT NULL DEFAULT 'seed',      -- seed | manager | a username
    notes       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_graph_plans_project ON graph_plans(project_id, status);
CREATE TABLE IF NOT EXISTS graph_nodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id    INTEGER NOT NULL,
    key        TEXT NOT NULL,                      -- stable across plan versions
    title      TEXT NOT NULL DEFAULT '',
    node_type  TEXT NOT NULL DEFAULT 'code',       -- aim | group | code | research | data | conclusion
    spec       TEXT NOT NULL DEFAULT '',           -- what this module IS, in prose
    join_mode  TEXT NOT NULL DEFAULT 'all_of',     -- all_of | any_of (how inputs settle)
    parent_key TEXT NOT NULL DEFAULT '',           -- '' = top level, else the owning group's key
    tags       TEXT NOT NULL DEFAULT '[]',         -- JSON, Nx-style
    paths      TEXT NOT NULL DEFAULT '[]',         -- JSON boundary manifest; dirs end in '/'
    UNIQUE(plan_id, key)
);
CREATE TABLE IF NOT EXISTS graph_edges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id       INTEGER NOT NULL,
    src_key       TEXT NOT NULL,
    dst_key       TEXT NOT NULL,
    edge_type     TEXT NOT NULL DEFAULT 'depends', -- depends | interface | data | artifact
    contract      TEXT NOT NULL DEFAULT '{}',      -- JSON: the rule both sides honour
    contract_test TEXT NOT NULL DEFAULT '',        -- the edge OWNS its contract test
    UNIQUE(plan_id, src_key, dst_key, edge_type)
);
CREATE TABLE IF NOT EXISTS graph_node_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- append-only trace; never updated but to close
    plan_id    INTEGER NOT NULL,
    node_key   TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'build',      -- build | verify | contract | judge
    task_id    INTEGER,
    agent_id   INTEGER,
    status     TEXT NOT NULL DEFAULT 'running',
    detail     TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    ended_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_graph_runs_node ON graph_node_runs(plan_id, node_key, id);
CREATE TABLE IF NOT EXISTS graph_node_tests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     INTEGER NOT NULL,
    node_key    TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'suite',     -- suite | contract
    path        TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',          -- planner-authored, stored immutably
    status      TEXT NOT NULL DEFAULT 'mapped',    -- mapped | written | passing | failing | error
    last_result TEXT NOT NULL DEFAULT '',          -- advisory: informs, never blocks (V1)
    UNIQUE(plan_id, node_key, kind, path)
);
CREATE TABLE IF NOT EXISTS graph_assign (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,    -- mutable on purpose: config acts, not replans
    plan_id  INTEGER NOT NULL,
    node_key TEXT NOT NULL,
    agent_id INTEGER,
    home_id  INTEGER,
    model    TEXT NOT NULL DEFAULT '',
    autonomy TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_assign ON graph_assign(plan_id, node_key);
"""


def init() -> None:
    db._conn.executescript(SCHEMA)
    db._conn.commit()


# --- the store: plans are written once, then only their status moves ---------

def create_plan(project_id: int, *, kind: str = "template", authored_by: str = "seed",
                notes: str = "") -> int:
    rows = db._rows("SELECT COALESCE(MAX(version), 0) AS v FROM graph_plans WHERE project_id=?",
                    (int(project_id),))
    cur = db._execute(
        "INSERT INTO graph_plans (project_id, version, kind, status, authored_by, notes,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (int(project_id), int(rows[0]["v"]) + 1, kind, "draft", authored_by, notes, time.time()))
    return int(cur.lastrowid)


def get_plan(plan_id: int) -> dict | None:
    rows = db._rows("SELECT * FROM graph_plans WHERE id=?", (int(plan_id),))
    return rows[0] if rows else None


def active_plan(project_id: int) -> dict | None:
    rows = db._rows("SELECT * FROM graph_plans WHERE project_id=? AND status='active'"
                    " ORDER BY version DESC LIMIT 1", (int(project_id),))
    return rows[0] if rows else None


def activate(plan_id: int) -> None:
    """draft → active; whatever was active for the same project → superseded.

    This is the ONLY write a plan row ever sees after creation. Everything else
    about a superseded plan stays byte-identical forever, which is what makes
    "what did version 3 say" a query instead of an archaeology project."""
    p = get_plan(plan_id)
    if not p:
        return
    db._execute("UPDATE graph_plans SET status='superseded'"
                " WHERE project_id=? AND status='active' AND id != ?",
                (p["project_id"], int(plan_id)))
    db._execute("UPDATE graph_plans SET status='active' WHERE id=?", (int(plan_id),))


def add_node(plan_id: int, key: str, title: str, *, node_type: str = "code", spec: str = "",
             join_mode: str = "all_of", parent_key: str = "", tags: list | None = None,
             paths: list | None = None) -> int:
    cur = db._execute(
        "INSERT INTO graph_nodes (plan_id, key, title, node_type, spec, join_mode,"
        " parent_key, tags, paths) VALUES (?,?,?,?,?,?,?,?,?)",
        (int(plan_id), key, title, node_type, spec, join_mode, parent_key,
         json.dumps(tags or []), json.dumps(paths or [])))
    return int(cur.lastrowid)


def add_edge(plan_id: int, src_key: str, dst_key: str, *, edge_type: str = "depends",
             contract: dict | None = None, contract_test: str = "") -> int:
    cur = db._execute(
        "INSERT INTO graph_edges (plan_id, src_key, dst_key, edge_type, contract,"
        " contract_test) VALUES (?,?,?,?,?,?)",
        (int(plan_id), src_key, dst_key, edge_type, json.dumps(contract or {}), contract_test))
    return int(cur.lastrowid)


def nodes(plan_id: int) -> list[dict]:
    out = []
    for r in db._rows("SELECT * FROM graph_nodes WHERE plan_id=? ORDER BY id", (int(plan_id),)):
        r["tags"] = json.loads(r["tags"] or "[]")
        r["paths"] = json.loads(r["paths"] or "[]")
        out.append(r)
    return out


def edges(plan_id: int) -> list[dict]:
    out = []
    for r in db._rows("SELECT * FROM graph_edges WHERE plan_id=? ORDER BY id", (int(plan_id),)):
        r["contract"] = json.loads(r["contract"] or "{}")
        out.append(r)
    return out


def note_run(plan_id: int, node_key: str, kind: str, *, task_id: int | None = None,
             agent_id: int | None = None, status: str = "running", detail: str = "") -> int:
    """One appended trace row per attempt at a node. The trace answers "what has
    actually happened to this module", which no amount of current-status can."""
    cur = db._execute(
        "INSERT INTO graph_node_runs (plan_id, node_key, kind, task_id, agent_id, status,"
        " detail, started_at) VALUES (?,?,?,?,?,?,?,?)",
        (int(plan_id), node_key, kind, task_id, agent_id, status, detail[:2000], time.time()))
    return int(cur.lastrowid)


def close_run(run_id: int, status: str, detail: str = "") -> None:
    db._execute("UPDATE graph_node_runs SET status=?, detail=?, ended_at=? WHERE id=?",
                (status, detail[:2000], time.time(), int(run_id)))


def runs(plan_id: int, node_key: str = "", limit: int = 50) -> list[dict]:
    """The trace tail, oldest first."""
    if node_key:
        rows = db._rows("SELECT * FROM graph_node_runs WHERE plan_id=? AND node_key=?"
                        " ORDER BY id DESC LIMIT ?", (int(plan_id), node_key, int(limit)))
    else:
        rows = db._rows("SELECT * FROM graph_node_runs WHERE plan_id=?"
                        " ORDER BY id DESC LIMIT ?", (int(plan_id), int(limit)))
    return list(reversed(rows))


def map_test(plan_id: int, node_key: str, path: str, *, kind: str = "suite",
             source: str = "", status: str = "mapped") -> int:
    """Attach a test file to a node. INSERT OR IGNORE: a suite row, once written,
    is never overwritten — the planner-authored source is part of the evidence."""
    db._execute(
        "INSERT OR IGNORE INTO graph_node_tests (plan_id, node_key, kind, path, source,"
        " status) VALUES (?,?,?,?,?,?)",
        (int(plan_id), node_key, kind, path, source, status))
    row = db._rows("SELECT id FROM graph_node_tests WHERE plan_id=? AND node_key=? AND kind=?"
                   " AND path=?", (int(plan_id), node_key, kind, path))
    return int(row[0]["id"]) if row else 0


def tests(plan_id: int, node_key: str = "") -> list[dict]:
    if node_key:
        return db._rows("SELECT * FROM graph_node_tests WHERE plan_id=? AND node_key=?"
                        " ORDER BY kind, path", (int(plan_id), node_key))
    return db._rows("SELECT * FROM graph_node_tests WHERE plan_id=? ORDER BY node_key, kind, path",
                    (int(plan_id),))


def update_test_result(plan_id: int, path: str, status: str, last_result: str = "") -> int:
    """Record how a run of this file went, everywhere the file is mapped.

    ADVISORY by design (V1): this touches graph_node_tests and nothing else — a red
    suite informs the person looking at the node, it never blocks or rolls anything
    back. The gate grows teeth only when the graph starts dispatching work (V2+)."""
    cur = db._execute("UPDATE graph_node_tests SET status=?, last_result=?"
                      " WHERE plan_id=? AND path=?",
                      (status, last_result[:2000], int(plan_id), path))
    return cur.rowcount


def set_assign(plan_id: int, node_key: str, *, agent_id: int | None = None,
               home_id: int | None = None, model: str | None = None,
               autonomy: str | None = None) -> dict:
    """Per-node config, deliberately MUTABLE: pointing a node at a different agent or
    model is an act of steering, not a change to what the plan says the module is —
    so it lives outside the immutable rows and survives nothing (a replan carries it
    forward by node key). None leaves the stored value alone; '' clears it."""
    cur = get_assign(plan_id, node_key) or {"agent_id": None, "home_id": None,
                                            "model": "", "autonomy": ""}
    merged = {
        "agent_id": cur["agent_id"] if agent_id is None else int(agent_id) or None,
        "home_id": cur["home_id"] if home_id is None else int(home_id) or None,
        "model": cur["model"] if model is None else model,
        "autonomy": cur["autonomy"] if autonomy is None else autonomy,
    }
    db._execute(
        "INSERT INTO graph_assign (plan_id, node_key, agent_id, home_id, model, autonomy)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(plan_id, node_key) DO UPDATE SET"
        " agent_id=excluded.agent_id, home_id=excluded.home_id, model=excluded.model,"
        " autonomy=excluded.autonomy",
        (int(plan_id), node_key, merged["agent_id"], merged["home_id"],
         merged["model"], merged["autonomy"]))
    return get_assign(plan_id, node_key) or merged


def get_assign(plan_id: int, node_key: str) -> dict | None:
    rows = db._rows("SELECT * FROM graph_assign WHERE plan_id=? AND node_key=?",
                    (int(plan_id), node_key))
    return rows[0] if rows else None


# --- positions: the one cosmetic fact, kept out of the immutable rows --------

def positions(plan_id: int) -> dict:
    """{node_key: [x, y]} for nodes someone has dragged. A key that is absent was
    never placed — the canvas auto-lays it out. Nothing here ever invents a
    coordinate, which is why [0,0] can only ever mean "someone put it there"."""
    return db.kv_get(f"graph:pos:{int(plan_id)}") or {}


def save_positions(plan_id: int, pos: dict) -> dict:
    """Merge dragged positions in. A partial save (one node dragged) must not erase
    the rest of the layout, and a malformed value is dropped rather than stored —
    absent-means-unset only stays true if nothing writes junk."""
    cur = positions(plan_id)
    for key, xy in (pos or {}).items():
        if (isinstance(xy, (list, tuple)) and len(xy) == 2
                and all(isinstance(v, (int, float)) for v in xy)):
            cur[str(key)] = [float(xy[0]), float(xy[1])]
    db.kv_set(f"graph:pos:{int(plan_id)}", cur)
    return cur


# --- affected-only selection -------------------------------------------------

def affected_tests(plan_id: int, node_key: str) -> list[str]:
    """The files a change to this node obliges us to run: the node's own suite plus
    the contract test of every edge the node touches, either end. A GROUP node's
    affected set is the union of its children's — verifying a layer means
    verifying every module in it, nothing more and nothing less; test rows
    themselves live on LEAVES only.

    Pure selection — reads rows, returns paths, runs nothing and no model. This is
    the Nx-style affected-only discipline: verifying `guards` should not cost a
    full-suite run, but it MUST cost every contract `guards` participates in,
    because the other side of an interface is exactly who a change here breaks."""
    kids = [n["key"] for n in nodes(plan_id)
            if node_key and n["parent_key"] == node_key]
    if kids:
        merged: set[str] = set()
        for k in kids:
            merged.update(affected_tests(plan_id, k))
        return sorted(merged)
    out = {t["path"] for t in tests(plan_id, node_key) if t["kind"] == "suite"}
    for e in edges(plan_id):
        if node_key in (e["src_key"], e["dst_key"]) and e["contract_test"]:
            out.add(e["contract_test"])
    return sorted(out)


# --- seed_self_graph: the offline fallback -----------------------------------
#
# Everything below is deterministic reading of the working tree. No model call, no
# network, well under a second — the runs-offline-free invariant applies to boot.

_FILE_RE = re.compile(r"[\w./-]+\.(?:py|js|css|html|md|sh|json|yml|yaml)")

# key, title, node_type, docstring source, tags, boundary manifest (dirs end in '/')
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
    # P4 moved the substrate into services/lifeworld; conductor/app/lifeworld/ is
    # the rollback until the cutover, and lifeworld_client.py is the door to the
    # service. All three are this node's boundary while that is true.
    ("lifeworld", "The Lifeworld", "services/lifeworld/app.py",
     ["backend", "domain"],
     ["services/lifeworld/", "conductor/app/lifeworld/",
      "conductor/app/lifeworld_routes.py", "conductor/app/lifeworld_client.py"]),
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
    p = config.ROOT / rel
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


def _tests_for_nodes(by_key: dict[str, list[str]]) -> dict[str, set[str]]:
    """Map each tests/test_*.py to the node(s) it exercises — mechanically.

    Backend: parsed imports (`from app import x, y`, `from app.y…`) resolved through
    the boundary manifests. Frontend/worker: the file reads the tests actually do —
    conftest's dashboard_js() concatenates every view script, reading index.html or
    style.css is the shell, naming canvas files is the canvas, adding worker/ to
    sys.path is the worker. Derived, not curated: a new test file maps itself."""
    out: dict[str, set[str]] = {}
    for f in sorted((config.ROOT / "tests").glob("test_*.py")):
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


def derive_group_edges(child_edges: list[dict], parent_of: dict[str, str],
                       group_edges: list[dict] | None = None) -> list[dict]:
    """Top-level edges are COMPUTED, never curated: group A → group B iff any
    child of A has an edge to any child of B (and A is not B — an edge inside
    one layer is that layer's private business). The first qualifying child
    edge in input order is the representative: its type, contract and contract
    test ride up, so the top level shows a real rule, not a hollow arrow.

    `group_edges` — an AUTHORED top tier (the manager writes its own) — is
    reconciled rather than trusted. The live hole this closes: plan v6 carried
    the child edge ops→db while its authored tier had no selfrepair→data over
    it, and one selfrepair→agents arrow no child edge backed. So: a crossing
    the author missed is derived anyway; an authored arrow that matches a real
    crossing keeps its deliberate type/contract over the mechanical
    representative; and an authored arrow with no child crossing behind it is
    DROPPED — an arrow into nothing is exactly the lie this derivation exists
    to prevent. Pure — lists in, list out, no rows, no files, no model."""
    authored: dict[tuple[str, str], dict] = {}
    for e in group_edges or []:
        authored.setdefault((e["src"], e["dst"]), e)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for e in child_edges:
        ga, gb = parent_of.get(e["src"], ""), parent_of.get(e["dst"], "")
        if not ga or not gb or ga == gb or (ga, gb) in seen:
            continue
        seen.add((ga, gb))
        rep = authored.get((ga, gb), e)
        out.append({"src": ga, "dst": gb, "edge_type": rep["edge_type"],
                    "contract": rep["contract"],
                    "contract_test": rep.get("contract_test", "")})
    return out


def _self_manifest() -> dict:
    """The whole seed graph as one comparable value — TWO LEVELS: aim → group
    nodes (the architecture layers) → their module children → conclusion.
    Nodes and edges in a fixed order, tests sorted. Idempotence is dict
    equality on this, nothing cleverer."""
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

    tmap = _tests_for_nodes(by_key)
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
        + derive_group_edges(child_edges, parent_of)
        + child_edges
        + [{"src": gkey, "dst": "conclusion", "edge_type": "depends",
            "contract": {}, "contract_test": ""} for gkey, *_ in _SELF_GROUPS])

    # Test rows live on LEAVES: contract rows come from child edges only — a
    # group answers through its children's rollup, not through rows of its own.
    test_list = sorted(
        [{"node": key, "kind": "suite", "path": path}
         for path, keys in tmap.items() for key in keys] +
        [{"node": e["dst"], "kind": "contract", "path": e["contract_test"]}
         for e in child_edges if e["contract_test"]],
        key=lambda t: (t["node"], t["kind"], t["path"]))
    # A file can qualify as a contract on the same node it suites; UNIQUE dedupes
    # rows, so dedupe the manifest the same way or equality would lie.
    seen, deduped = set(), []
    for t in test_list:
        k = (t["node"], t["kind"], t["path"])
        if k not in seen:
            seen.add(k)
            deduped.append(t)
    return {"nodes": node_list, "edges": edge_list, "tests": deduped}


def self_manifest() -> dict:
    """The ONE manifest builder — the repo's real module inventory, shared by the
    offline seed and the manager's authoring pass (modgraph_author). Both describe
    the same tree, so both must read it the same way; two builders would disagree
    the first time one of them was improved."""
    return _self_manifest()


def _manifest_of(plan_id: int) -> dict:
    """The stored plan, re-shaped for comparison with _self_manifest()."""
    return {
        "nodes": [{"key": n["key"], "title": n["title"], "node_type": n["node_type"],
                   "spec": n["spec"], "join_mode": n["join_mode"],
                   "parent_key": n["parent_key"], "tags": n["tags"], "paths": n["paths"]}
                  for n in nodes(plan_id)],
        "edges": [{"src": e["src_key"], "dst": e["dst_key"], "edge_type": e["edge_type"],
                   "contract": e["contract"], "contract_test": e["contract_test"]}
                  for e in edges(plan_id)],
        "tests": [{"node": t["node_key"], "kind": t["kind"], "path": t["path"]}
                  for t in tests(plan_id)],
    }


def seed_self_graph() -> int:
    """Ensure project 0 has an active plan; return its id.

    Idempotent: an unchanged tree reseeds to zero writes. Drift — a module docstring
    reworded, a test file added — makes a NEW version and supersedes the old one; the
    old rows are never touched, because a fallback that edits history is exactly as
    untrustworthy as a manager that does. A plan the crew's manager authored outranks
    the seed entirely: the fallback never overwrites deliberate work."""
    cur = active_plan(0)
    if cur and cur["authored_by"] != "seed":
        return int(cur["id"])
    man = _self_manifest()
    if cur and _manifest_of(cur["id"]) == man:
        return int(cur["id"])
    plan_id = create_plan(0, kind="template", authored_by="seed",
                          notes="offline fallback, regenerated from the working tree")
    for n in man["nodes"]:
        add_node(plan_id, n["key"], n["title"], node_type=n["node_type"], spec=n["spec"],
                 join_mode=n["join_mode"], parent_key=n["parent_key"], tags=n["tags"],
                 paths=n["paths"])
    for e in man["edges"]:
        add_edge(plan_id, e["src"], e["dst"], edge_type=e["edge_type"],
                 contract=e["contract"], contract_test=e["contract_test"])
    for t in man["tests"]:
        map_test(plan_id, t["node"], t["path"], kind=t["kind"], status="mapped")
    activate(plan_id)
    return plan_id
