"""Persistence — the six graph tables, and the one cosmetic fact kept out of them.

A PLAN is an immutable version: aim node → GROUP nodes (the architecture layers)
→ their module children → conclusion node. Each node carries its spec, its
boundary manifest (paths), its test suite and its own agent/model config; each
edge is TYPED and carries the contract both sides honour. A replan never edits
rows — it writes a new plan and marks the old one superseded, because a mutable
plan makes "what did we believe when this was built" unanswerable the moment
anyone improves it. Node KEYS stay stable across versions, so assignments and
positions survive a replan; everything else is frozen the day it is authored.
The trace (`graph_node_runs`) and the planner-authored test source are
append-only for the same reason — they are evidence, and evidence that can be
rewritten is not evidence.

Since P5 these rows live in THIS service's own `data/modgraph.db` and nothing
else opens the file. The conductor asks over HTTP like anyone else.

WHY POSITIONS LIVE IN KV, NOT ON NODES. Where a node sits on a canvas is the one
mutable, cosmetic fact in the whole feature, and a column for it would poke a
hole in "plan rows never change". kv `graph:pos:{plan_id}` holds it instead — the
same key the conductor used, so the first-boot copy is a straight lift — and
ABSENT MEANS UNSET: a node with no stored position gets auto-layout, never a
fabricated [0,0].

WHY THE MASTERY JOIN IS INTERNAL. `graph_node_runs ⋈ graph_plans` is the one
cross-table read in the feature and BOTH tables are this service's, so it stays a
JOIN instead of becoming HTTP composition. That is the whole test of a boundary:
a join you can still write is a join that was inside it.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager

import helpers                      # vendored per service — never imported across services

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

TABLES = ("graph_plans", "graph_nodes", "graph_edges", "graph_node_runs",
          "graph_node_tests", "graph_assign")


def init_store() -> None:
    con = helpers.db()
    con.executescript(SCHEMA)
    con.commit()


# SQLite's INTEGER is signed 64-bit, and a bigger one is not a row that is missing
# — it is an OverflowError three frames down, which reaches a caller as a 500. An
# id no row can ever have is an id that does not exist, so it answers like one.
# (The lifeworld's contract fuzzer found this by asking for world 2**63; the same
# fuzzer drives this service's plan ids.)
_MAX_ID = 2**63 - 1


def _real_id(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if -_MAX_ID - 1 <= n <= _MAX_ID else None


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in helpers.db().execute(sql, params).fetchall()]


# Every row verb commits its own statement — the house discipline, and right for
# twenty-odd writes that are each a whole act. `import_plan` is the exception and
# needs them NOT to, so it opens this and the verbs inside it defer. A flag rather
# than a second set of no-commit verbs: two copies of `add_node` is how one of
# them quietly stops matching the schema.
_IN_TX = False


@contextmanager
def _transaction():
    """One commit at the end, or one rollback. Reentrancy is not supported and
    not needed: the single caller is `import_plan`."""
    global _IN_TX
    con = helpers.db()
    _IN_TX = True
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        _IN_TX = False


def _execute(sql: str, params: tuple = ()):
    con = helpers.db()
    cur = con.execute(sql, params)
    if not _IN_TX:
        con.commit()
    return cur


def _kv_get(key: str) -> str | None:
    row = helpers.db().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _kv_set(key: str, value: str) -> None:
    """kv writes go through `_execute`, NOT `helpers.kv_set`, so they honour an
    open transaction. The vendored helper commits on every call, and a commit
    inside `import_plan` or the first-boot copy would harden exactly the half-done
    state those two exist to make impossible."""
    _execute("INSERT INTO kv (key, value) VALUES (?, ?) "
             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# --- plans: written once, then only their status moves ------------------------

def create_plan(project_id: int, *, kind: str = "template", authored_by: str = "seed",
                notes: str = "") -> int:
    proj = _real_id(project_id)
    if proj is None:
        return 0
    rows = _rows("SELECT COALESCE(MAX(version), 0) AS v FROM graph_plans WHERE project_id=?",
                 (proj,))
    cur = _execute(
        "INSERT INTO graph_plans (project_id, version, kind, status, authored_by, notes,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (proj, int(rows[0]["v"]) + 1, kind, "draft", authored_by, notes, time.time()))
    return int(cur.lastrowid)


def get_plan(plan_id: int) -> dict | None:
    pid = _real_id(plan_id)
    if pid is None:
        return None
    rows = _rows("SELECT * FROM graph_plans WHERE id=?", (pid,))
    return rows[0] if rows else None


def active_plan(project_id: int) -> dict | None:
    pid = _real_id(project_id)
    if pid is None:
        return None
    rows = _rows("SELECT * FROM graph_plans WHERE project_id=? AND status='active'"
                 " ORDER BY version DESC LIMIT 1", (pid,))
    return rows[0] if rows else None


def activate(plan_id: int) -> None:
    """draft → active; whatever was active for the same project → superseded.

    This is the ONLY write a plan row ever sees after creation. Everything else
    about a superseded plan stays byte-identical forever, which is what makes
    "what did version 3 say" a query instead of an archaeology project."""
    p = get_plan(plan_id)
    if not p:
        return
    pid = _real_id(plan_id)
    _execute("UPDATE graph_plans SET status='superseded'"
             " WHERE project_id=? AND status='active' AND id != ?",
             (p["project_id"], pid))
    _execute("UPDATE graph_plans SET status='active' WHERE id=?", (pid,))


def add_node(plan_id: int, key: str, title: str, *, node_type: str = "code", spec: str = "",
             join_mode: str = "all_of", parent_key: str = "", tags: list | None = None,
             paths: list | None = None) -> int:
    pid = _real_id(plan_id)
    if pid is None:
        return 0
    cur = _execute(
        "INSERT INTO graph_nodes (plan_id, key, title, node_type, spec, join_mode,"
        " parent_key, tags, paths) VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, key, title, node_type, spec, join_mode, parent_key,
         json.dumps(tags or []), json.dumps(paths or [])))
    return int(cur.lastrowid)


def add_edge(plan_id: int, src_key: str, dst_key: str, *, edge_type: str = "depends",
             contract: dict | None = None, contract_test: str = "") -> int:
    pid = _real_id(plan_id)
    if pid is None:
        return 0
    cur = _execute(
        "INSERT INTO graph_edges (plan_id, src_key, dst_key, edge_type, contract,"
        " contract_test) VALUES (?,?,?,?,?,?)",
        (pid, src_key, dst_key, edge_type, json.dumps(contract or {}), contract_test))
    return int(cur.lastrowid)


def nodes(plan_id: int) -> list[dict]:
    pid = _real_id(plan_id)
    if pid is None:
        return []
    out = []
    for r in _rows("SELECT * FROM graph_nodes WHERE plan_id=? ORDER BY id", (pid,)):
        r["tags"] = json.loads(r["tags"] or "[]")
        r["paths"] = json.loads(r["paths"] or "[]")
        out.append(r)
    return out


def edges(plan_id: int) -> list[dict]:
    pid = _real_id(plan_id)
    if pid is None:
        return []
    out = []
    for r in _rows("SELECT * FROM graph_edges WHERE plan_id=? ORDER BY id", (pid,)):
        r["contract"] = json.loads(r["contract"] or "{}")
        out.append(r)
    return out


# --- the trace: append-only, closed once --------------------------------------

def note_run(plan_id: int, node_key: str, kind: str, *, task_id: int | None = None,
             agent_id: int | None = None, status: str = "running", detail: str = "") -> int:
    """One appended trace row per attempt at a node. The trace answers "what has
    actually happened to this module", which no amount of current-status can."""
    pid, tid, aid = _real_id(plan_id), _real_id(task_id), _real_id(agent_id)
    if pid is None:
        return 0
    cur = _execute(
        "INSERT INTO graph_node_runs (plan_id, node_key, kind, task_id, agent_id, status,"
        " detail, started_at) VALUES (?,?,?,?,?,?,?,?)",
        (pid, node_key, kind, tid, aid, status, detail[:2000], time.time()))
    return int(cur.lastrowid)


def close_run(run_id: int, status: str, detail: str = "") -> None:
    rid = _real_id(run_id)
    if rid is None:
        return
    _execute("UPDATE graph_node_runs SET status=?, detail=?, ended_at=? WHERE id=?",
             (status, detail[:2000], time.time(), rid))


def runs(plan_id: int, node_key: str = "", limit: int = 50) -> list[dict]:
    """The trace tail, oldest first."""
    pid = _real_id(plan_id)
    if pid is None:
        return []
    if node_key:
        rows = _rows("SELECT * FROM graph_node_runs WHERE plan_id=? AND node_key=?"
                     " ORDER BY id DESC LIMIT ?", (pid, node_key, int(limit)))
    else:
        rows = _rows("SELECT * FROM graph_node_runs WHERE plan_id=?"
                     " ORDER BY id DESC LIMIT ?", (pid, int(limit)))
    return list(reversed(rows))


# --- tests: mapped once, results advisory -------------------------------------

def map_test(plan_id: int, node_key: str, path: str, *, kind: str = "suite",
             source: str = "", status: str = "mapped") -> int:
    """Attach a test file to a node. INSERT OR IGNORE: a suite row, once written,
    is never overwritten — the planner-authored source is part of the evidence."""
    pid = _real_id(plan_id)
    if pid is None:
        return 0
    _execute(
        "INSERT OR IGNORE INTO graph_node_tests (plan_id, node_key, kind, path, source,"
        " status) VALUES (?,?,?,?,?,?)",
        (pid, node_key, kind, path, source, status))
    row = _rows("SELECT id FROM graph_node_tests WHERE plan_id=? AND node_key=? AND kind=?"
                " AND path=?", (pid, node_key, kind, path))
    return int(row[0]["id"]) if row else 0


def tests(plan_id: int, node_key: str = "") -> list[dict]:
    pid = _real_id(plan_id)
    if pid is None:
        return []
    if node_key:
        return _rows("SELECT * FROM graph_node_tests WHERE plan_id=? AND node_key=?"
                     " ORDER BY kind, path", (pid, node_key))
    return _rows("SELECT * FROM graph_node_tests WHERE plan_id=? ORDER BY node_key, kind, path",
                 (pid,))


def update_test_result(plan_id: int, path: str, status: str, last_result: str = "") -> int:
    """Record how a run of this file went, everywhere the file is mapped.

    ADVISORY by design (V1): this touches graph_node_tests and nothing else — a red
    suite informs the person looking at the node, it never blocks or rolls anything
    back. The gate grows teeth only when the graph starts dispatching work (V2+).

    The RUNNER is not here and never will be: it runs the repo's own pytest over
    real files in the conductor's checkout, and a service that shelled out to
    another process's working tree would have imported that tree's whole world.
    This service stores the verdict; the conductor produces it."""
    pid = _real_id(plan_id)
    if pid is None:
        return 0
    cur = _execute("UPDATE graph_node_tests SET status=?, last_result=?"
                   " WHERE plan_id=? AND path=?",
                   (status, last_result[:2000], pid, path))
    return cur.rowcount


# --- assignment: mutable on purpose ------------------------------------------

def set_assign(plan_id: int, node_key: str, *, agent_id: int | None = None,
               home_id: int | None = None, model: str | None = None,
               autonomy: str | None = None) -> dict:
    """Per-node config, deliberately MUTABLE: pointing a node at a different agent or
    model is an act of steering, not a change to what the plan says the module is —
    so it lives outside the immutable rows and survives nothing (a replan carries it
    forward by node key). None leaves the stored value alone; '' clears it."""
    pid = _real_id(plan_id)
    cur = get_assign(plan_id, node_key) or {"agent_id": None, "home_id": None,
                                            "model": "", "autonomy": ""}
    merged = {
        "agent_id": cur["agent_id"] if agent_id is None else _real_id(agent_id) or None,
        "home_id": cur["home_id"] if home_id is None else _real_id(home_id) or None,
        "model": cur["model"] if model is None else model,
        "autonomy": cur["autonomy"] if autonomy is None else autonomy,
    }
    if pid is None:
        return merged
    _execute(
        "INSERT INTO graph_assign (plan_id, node_key, agent_id, home_id, model, autonomy)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(plan_id, node_key) DO UPDATE SET"
        " agent_id=excluded.agent_id, home_id=excluded.home_id, model=excluded.model,"
        " autonomy=excluded.autonomy",
        (pid, node_key, merged["agent_id"], merged["home_id"],
         merged["model"], merged["autonomy"]))
    return get_assign(plan_id, node_key) or merged


def get_assign(plan_id: int, node_key: str) -> dict | None:
    pid = _real_id(plan_id)
    if pid is None:
        return None
    rows = _rows("SELECT * FROM graph_assign WHERE plan_id=? AND node_key=?",
                 (pid, node_key))
    return rows[0] if rows else None


def assigns(plan_id: int) -> dict[str, dict]:
    """EVERY assignment on a plan, by node key — the read the graph payload and the
    engine's node matcher actually make.

    It exists because the boundary made the old shape expensive. In-process,
    `get_assign` per node was a dict lookup in a loop; over a wire it is one round
    trip per node on a payload the Atlas polls, and fourteen of those per poll is
    a latency regression dressed up as a faithful port. One call answers the
    question both callers were really asking."""
    pid = _real_id(plan_id)
    if pid is None:
        return {}
    return {r["node_key"]: r for r in
            _rows("SELECT * FROM graph_assign WHERE plan_id=?", (pid,))}


# --- positions: the one cosmetic fact, kept out of the immutable rows --------

def positions(plan_id: int) -> dict:
    """{node_key: [x, y]} for nodes someone has dragged. A key that is absent was
    never placed — the canvas auto-lays it out. Nothing here ever invents a
    coordinate, which is why [0,0] can only ever mean "someone put it there"."""
    raw = _kv_get(f"graph:pos:{_real_id(plan_id)}")
    if not raw:
        return {}
    try:
        got = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return got if isinstance(got, dict) else {}


def save_positions(plan_id: int, pos: dict) -> dict:
    """Merge dragged positions in. A partial save (one node dragged) must not erase
    the rest of the layout, and a malformed value is dropped rather than stored —
    absent-means-unset only stays true if nothing writes junk."""
    cur = positions(plan_id)
    for key, xy in (pos or {}).items():
        if (isinstance(xy, (list, tuple)) and len(xy) == 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in xy)):
            cur[str(key)] = [float(xy[0]), float(xy[1])]
    if _real_id(plan_id) is None:
        return {}
    _kv_set(f"graph:pos:{_real_id(plan_id)}", json.dumps(cur))
    return cur


# --- writing a whole plan at once --------------------------------------------

def import_plan(project_id: int, *, kind: str = "template", authored_by: str = "seed",
                notes: str = "", nodes_in: list[dict], edges_in: list[dict],
                tests_in: list[dict], assigns_in: dict | None = None,
                positions_in: dict | None = None, activate_it: bool = True) -> dict:
    """One plan version, written whole.

    A WHOLE BEHAVIOUR, for the same reason the lifeworld's crew endpoints are:
    a plan is authored as one thing and a half-written one is a lie. Both bulk
    writers (the manager's authoring pass, and the operator removing a node —
    which rebuilds the survivors into a new version) used to do fifty-odd row
    calls inside one process, where a failure halfway left a DRAFT nobody had
    activated. Over a wire those fifty calls become fifty round trips AND fifty
    chances to be interrupted with the plan already active. So the whole write is
    one call and one transaction, and the caller cannot straddle it.

    Positions carry by node key (steering, not plan) and are stored under the NEW
    plan id, exactly as the conductor's own remove path did it.

    ONE COMMIT, and it had to be made real: the row verbs each commit their own
    statement, so wrapping them in a try/except and calling rollback() undid
    nothing at all — the drill caught a "failed" import that had left an activated
    plan behind. `_transaction()` makes them defer."""
    with _transaction():
        plan_id = create_plan(project_id, kind=kind, authored_by=authored_by, notes=notes)
        for n in nodes_in:
            add_node(plan_id, n["key"], n.get("title") or n["key"],
                     node_type=n.get("node_type") or "code", spec=n.get("spec") or "",
                     join_mode=n.get("join_mode") or "all_of",
                     parent_key=n.get("parent_key") or "", tags=n.get("tags") or [],
                     paths=n.get("paths") or [])
        for e in edges_in:
            add_edge(plan_id, e["src"], e["dst"], edge_type=e.get("edge_type") or "depends",
                     contract=e.get("contract") or {},
                     contract_test=e.get("contract_test") or "")
        for t in tests_in:
            map_test(plan_id, t["node"], t["path"], kind=t.get("kind") or "suite",
                     source=t.get("source") or "", status=t.get("status") or "mapped")
        for key, a in (assigns_in or {}).items():
            set_assign(plan_id, key, agent_id=a.get("agent_id"), home_id=a.get("home_id"),
                       model=a.get("model"), autonomy=a.get("autonomy"))
        if positions_in:
            save_positions(plan_id, positions_in)
        if activate_it:
            activate(plan_id)
    return get_plan(plan_id) or {}


# --- the one-time copy out of the conductor's monolith store -----------------

def settled() -> bool:
    """Has the first-boot decision about the conductor's six graph tables been made?

    The CONDUCTOR asks this — over `/health` — before it drops them, and that is
    not politeness. Nothing orders the two processes: the fleet starts them
    together with no `depends_on`, so on the first boot after the P5-B cutover
    the conductor's `init()` can easily run before this service has attached.

    What a premature drop would cost here is smaller than the lifeworld's and
    bigger than nothing, and the honest thing is to say which: it is the TRACE.
    Plans and edges regenerate from the tree in a second (`seed.py` is
    deterministic), and the manager re-authors on the next lineup change. What
    does NOT come back is `graph_node_runs` — every build and verify any
    specialist has ever closed — and MASTERY IS COMPUTED FROM IT. Drop that early
    and every module silently loses its master, so the next authoring pass
    reshuffles specialists off the modules they had earned, and nothing anywhere
    reports that anything was lost. `graph_assign` goes the same way: the
    operator's own per-node steering.

    True means "this service will never look at those tables again", which
    includes every case where it decided there was nothing to copy.
    """
    return bool(_kv_get("backfilled_from"))


def _settle(reason: str, **extra) -> None:
    _kv_set("backfilled_from", json.dumps(
        {"tables": list(TABLES), "reason": reason, "ts": time.time(), **extra}))


def backfill_from_legacy(legacy_db_path) -> int:
    """First boot only: copy the conductor's six graph_* tables into this store.

    THE ROWIDS ARE PRESERVED. Plan ids are pointers held outside these tables:
    kv `graph:pos:{plan_id}` is keyed by one, the manager's `graph:authored:0`
    stamp records one, and a renumbering would silently detach a layout and make
    the staleness check re-author on the next sprint. The positions come across in
    the same pass, for the same reason.

    Honest about ordering: the conductor may or may not have renamed its tables
    aside (P5-A deliberately did NOT — the rollback is the vendored legacy body,
    which reads them by name — but a box that took a different route may have), so
    both names are checked, per table.

    THE COPY IS ONE TRANSACTION, and it has to be. Any failure (the conductor
    mid-write, a locked file) leaves the marker UNSET so the next boot tries again
    — but the retry is guarded by "this store already had graph rows", so a
    HALF-copied store would settle on the partial copy and lose the rest forever.
    Six tables committed one by one is exactly that failure. Rolled back whole,
    the retry finds the empty store its guard assumes.

    Every outcome that means "there is nothing left to do here" SETTLES, including
    the boring ones — a fresh box with no legacy database, or a store that already
    has rows. Without that the conductor would wait forever for permission to drop
    tables that were never going to be read again.
    """
    from pathlib import Path
    legacy = Path(legacy_db_path)
    if settled():
        return 0
    if any(_rows(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in TABLES):
        _settle("this store already had graph rows — nothing to copy")
        return 0
    if not legacy.exists():
        _settle("no legacy database on this box")
        return 0
    con = helpers.db()
    copied = 0
    try:
        # ATTACH and DETACH must sit OUTSIDE the transaction — SQLite refuses both
        # inside one — so the copy is bracketed rather than wrapped.
        con.execute("ATTACH DATABASE ? AS legacy", (f"file:{legacy}?mode=ro",))
        try:
            present = {r["name"] for r in con.execute(
                "SELECT name FROM legacy.sqlite_master WHERE type='table'").fetchall()}
            if not any(t in present or f"{t}_legacy" in present for t in TABLES):
                _settle("no legacy graph tables")
                return 0
            per_table: dict[str, int] = {}
            with _transaction():
                for table in TABLES:
                    src = f"{table}_legacy" if f"{table}_legacy" in present else \
                          table if table in present else ""
                    if not src:
                        continue
                    cols = ", ".join(r[1] for r in
                                     con.execute(f"PRAGMA table_info({table})").fetchall())
                    con.execute(f"INSERT INTO main.{table} ({cols})"
                                f" SELECT {cols} FROM legacy.{src}")
                    per_table[table] = con.execute("SELECT changes()").fetchone()[0]
                    copied += per_table[table]
                # The layouts ride along: they are keyed by plan id, and a plan whose
                # positions stayed behind auto-lays-out with no sign anything moved.
                if "kv" in present:
                    for row in con.execute("SELECT k, v FROM legacy.kv"
                                           " WHERE k LIKE 'graph:pos:%'").fetchall():
                        _kv_set(row[0], row[1])
                # Inside the same transaction as the rows it is a marker FOR: a
                # settle that outlived a rolled-back copy would tell the conductor
                # to drop tables this store never took.
                _settle("copied", db=str(legacy), rows=copied, per_table=per_table)
        finally:
            con.execute("DETACH DATABASE legacy")
    except Exception:
        con.rollback()                  # try again next boot; the store is still empty
        return 0
    return copied
