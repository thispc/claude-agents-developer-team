"""SQLite persistence. Single conductor replica; a threading lock serializes writes."""

import json
import sqlite3
import threading
import time
from typing import Any

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    brief TEXT NOT NULL,
    repo TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'planning',
    budget_usd REAL NOT NULL,
    max_workers INTEGER NOT NULL,
    max_runs INTEGER NOT NULL DEFAULT 40,
    runs_used INTEGER NOT NULL DEFAULT 0,
    team TEXT NOT NULL DEFAULT '[]',   -- recruited roster: [{role, count, model}]
    autonomy TEXT NOT NULL DEFAULT 'supervised',  -- supervised | autonomous
    owner_id INTEGER NOT NULL DEFAULT 0,          -- whose credentials the agents use
    manager_model TEXT NOT NULL DEFAULT '',       -- '' = server default
    manager_persona TEXT NOT NULL DEFAULT '',     -- extra character instructions
    is_self INTEGER NOT NULL DEFAULT 0,           -- this row is the platform's own codebase
    sprints INTEGER NOT NULL DEFAULT 1,           -- how many delivery cycles to run
    sprint INTEGER NOT NULL DEFAULT 1,            -- which one is in progress
    cost_usd REAL NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    deps TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT 'initial',   -- initial | runtime (added mid-project)
    model TEXT NOT NULL DEFAULT '',           -- model used for the LAST run (informational)
    pinned_model TEXT NOT NULL DEFAULT '',    -- explicit manager override; wins over auto-selection
    compete INTEGER NOT NULL DEFAULT 0,       -- >1 = run N rival attempts, manager picks the winner
    seq INTEGER NOT NULL DEFAULT 0,           -- per-project task number (1,2,3…) shown to humans
    verification TEXT NOT NULL DEFAULT '',    -- JSON: the harness-run test/build result
    branch TEXT NOT NULL DEFAULT '',
    issue_number INTEGER,
    pr_number INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    feedback TEXT NOT NULL DEFAULT '',
    report TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
-- A planning round table: a circle of heterogeneous agents that deliberate a
-- rough idea into a blueprint before any engineer is hired.
CREATE TABLE IF NOT EXISTS roundtables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    project_id INTEGER,                       -- set once the blueprint is built into a project
    title TEXT NOT NULL DEFAULT '',
    brief TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',     -- draft | running | done | failed
    mode TEXT NOT NULL DEFAULT 'debate',      -- diverge (N+1 calls) | debate (3N+1)
    mod_provider TEXT NOT NULL DEFAULT '',    -- the moderator in the centre
    mod_model TEXT NOT NULL DEFAULT '',
    blueprint TEXT NOT NULL DEFAULT '',       -- JSON, once synthesised
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS seats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id INTEGER NOT NULL,
    seat_index INTEGER NOT NULL,              -- position around the circle
    name TEXT NOT NULL,
    provider TEXT NOT NULL,                   -- anthropic | openai | google
    model TEXT NOT NULL,
    persona TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_seats_table ON seats(table_id);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id INTEGER NOT NULL,
    seat_id INTEGER,                          -- NULL = the moderator
    phase TEXT NOT NULL,                      -- propose | critique | revise | synthesis
    round INTEGER NOT NULL,
    text TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_table ON turns(table_id, id);
-- Rate-limit cooldowns, persisted. Held only in memory before, so every restart
-- forgot which models were throttled and immediately hammered them again.
CREATE TABLE IF NOT EXISTS model_cooldown (
    model TEXT PRIMARY KEY,
    until_ts REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
-- A named teammate, not a fresh session. Roles used to be labels: every task got
-- a brand-new agent with no memory of what "the backend engineer" had already
-- built, which made continuity, per-role personas and per-role providers all
-- impossible to express. An agent row is the durable thing a task is assigned to.
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    name TEXT NOT NULL,                       -- stable human name, shown everywhere
    role TEXT NOT NULL,
    idx INTEGER NOT NULL DEFAULT 1,           -- which of N holding this role
    persona TEXT NOT NULL DEFAULT '',         -- character instructions, editable mid-project
    provider TEXT NOT NULL DEFAULT 'anthropic',
    model TEXT NOT NULL DEFAULT '',           -- '' = the role default
    status TEXT NOT NULL DEFAULT 'idle',      -- idle | busy
    tasks_done INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',           -- carried into the next task's context
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_project ON agents(project_id);
-- One row per agent dispatch. Without this there is no way to tell whether a
-- change to the orchestration algorithm helped: cost and duration were recorded
-- per project, so a tweak that halved rework and a tweak that did nothing looked
-- identical. Every knob change is stamped with the profile in force, so runs can
-- be compared across configurations rather than only across time.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    task_id INTEGER,
    agent_id INTEGER,
    contender_id INTEGER,                     -- set when this run was one rival in a contest
    sprint INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'anthropic',
    model TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL DEFAULT 'task',        -- task | contender | manager | review
    outcome TEXT NOT NULL DEFAULT 'running',  -- running | accepted | rework | failed | abandoned
    reason TEXT NOT NULL DEFAULT '',
    cost_usd REAL NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    profile TEXT NOT NULL DEFAULT '',         -- tuning profile this ran under
    started_at REAL NOT NULL,
    ended_at REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
-- Small durable facts that outlive a process: notification fingerprints, login
-- failure counts, the last scheduled-scan time. Each was in memory, and each was
-- wrong for the same reason — a crash-looping pod re-notified on every restart,
-- and a restart cleared a lockout an attacker could then walk straight through.
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    ts REAL NOT NULL
);
-- The orchestration knobs. Constants in source cannot be tuned on a running
-- instance, and an algorithm that needs regular tweaking cannot live behind a
-- rebuild-and-redeploy cycle.
CREATE TABLE IF NOT EXISTS tuning (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    task_id INTEGER,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, id);
-- Boss <-> Manager channel.
-- directive: boss -> manager, consumed at the manager's next decision point.
-- question:  manager -> boss, answered from the dashboard; manager blocks until answered.
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- 'directive' | 'question'
    text TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '[]',
    answer TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | delivered | answered
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbox_project ON inbox(project_id, status);
-- Rival attempts at ONE task. Each works on its own branch; the manager judges them
-- and promotes a single winner, so parallel attempts never clobber each other.
CREATE TABLE IF NOT EXISTS contenders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,                     -- 1..N, shown to the boss as "A/B"
    branch TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',   -- running | pushed | failed | won | lost
    report TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contenders_task ON contenders(task_id);
"""


def init() -> None:
    global _conn
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    for stmt in (  # migrations for pre-existing DBs (ignore "duplicate column")
        "ALTER TABLE tasks ADD COLUMN deps TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE projects ADD COLUMN max_runs INTEGER NOT NULL DEFAULT 40",
        "ALTER TABLE projects ADD COLUMN runs_used INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN origin TEXT NOT NULL DEFAULT 'initial'",
        "ALTER TABLE projects ADD COLUMN team TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE projects ADD COLUMN autonomy TEXT NOT NULL DEFAULT 'supervised'",
        "ALTER TABLE projects ADD COLUMN manager_model TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE projects ADD COLUMN manager_persona TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE projects ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN model TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN pinned_model TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN compete INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN seq INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE projects ADD COLUMN is_self INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE projects ADD COLUMN sprints INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE projects ADD COLUMN sprint INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE tasks ADD COLUMN sprint INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE roundtables ADD COLUMN mode TEXT NOT NULL DEFAULT 'debate'",
        "ALTER TABLE tasks ADD COLUMN verification TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN agent_id INTEGER",
        "ALTER TABLE projects ADD COLUMN process TEXT NOT NULL DEFAULT 'agile'",
        "ALTER TABLE projects ADD COLUMN profile TEXT NOT NULL DEFAULT 'default'",
        # roundtables/seats/turns are created by SCHEMA above (CREATE TABLE IF NOT
        # EXISTS), so existing databases pick them up without a migration here.
    ):
        try:
            _conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    # Backfill per-project task numbers for tasks created before seq existed,
    # numbering them 1..N in creation order within each project.
    _conn.execute("""
        UPDATE tasks SET seq = (
            SELECT COUNT(*) FROM tasks AS t2
            WHERE t2.project_id = tasks.project_id AND t2.id <= tasks.id
        ) WHERE seq = 0
    """)
    _conn.commit()


def _execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    assert _conn is not None, "db.init() not called"
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    assert _conn is not None, "db.init() not called"
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


# --- projects ---

def create_project(name: str, brief: str, repo: str, budget_usd: float,
                   max_workers: int, max_runs: int = 40, team: list | None = None,
                   autonomy: str = "supervised", manager_model: str = "",
                   manager_persona: str = "", owner_id: int = 0, sprints: int = 1) -> int:
    cur = _execute(
        "INSERT INTO projects (name, brief, repo, budget_usd, max_workers, max_runs, team, "
        "autonomy, manager_model, manager_persona, owner_id, sprints, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, brief, repo, budget_usd, max_workers, max_runs, json.dumps(team or []),
         autonomy, manager_model, manager_persona, owner_id, max(1, int(sprints)),
         time.time()),
    )
    return cur.lastrowid


def advance_sprint(project_id: int) -> int:
    """Move the project into its next delivery cycle. Returns the new sprint number."""
    _execute("UPDATE projects SET sprint = sprint + 1 WHERE id=?", (project_id,))
    p = get_project(project_id)
    return (p or {}).get("sprint", 1)


def set_max_runs(project_id: int, max_runs: int) -> None:
    _execute("UPDATE projects SET max_runs=? WHERE id=?", (max(1, int(max_runs)), project_id))


def set_sprints(project_id: int, sprints: int) -> None:
    """Change the target number of cycles mid-run — the boss may want more or fewer
    once they have seen what one sprint actually produces."""
    _execute("UPDATE projects SET sprints=? WHERE id=?", (max(1, int(sprints)), project_id))


def sprint_summary(project_id: int) -> list[dict]:
    """What each completed sprint delivered — the input to planning the next one."""
    return _rows(
        "SELECT sprint, COUNT(*) n, "
        "SUM(status='done') done, SUM(status='failed') failed "
        "FROM tasks WHERE project_id=? GROUP BY sprint ORDER BY sprint", (project_id,))


# --- round tables ---------------------------------------------------------

def create_table(owner_id: int, brief: str, title: str = "",
                 mod_provider: str = "", mod_model: str = "",
                 mode: str = "debate") -> int:
    cur = _execute(
        "INSERT INTO roundtables (owner_id, brief, title, mod_provider, mod_model, "
        "mode, created_at) VALUES (?,?,?,?,?,?,?)",
        (owner_id, brief, title, mod_provider, mod_model, mode, time.time()))
    return cur.lastrowid


def get_table(table_id: int) -> dict | None:
    rows = _rows("SELECT * FROM roundtables WHERE id=?", (table_id,))
    return rows[0] if rows else None


def list_tables(owner_id: int | None = None) -> list[dict]:
    if owner_id is None:
        return _rows("SELECT * FROM roundtables ORDER BY id DESC")
    return _rows("SELECT * FROM roundtables WHERE owner_id=? ORDER BY id DESC", (owner_id,))


def update_table(table_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"project_id", "title", "brief", "status", "mod_provider",
               "mod_model", "mode", "blueprint", "started_at", "finished_at"}
    bad = set(fields) - allowed
    assert not bad, f"update_table got unknown field(s): {bad}"
    sets = ", ".join(f"{k}=?" for k in fields)
    _execute(f"UPDATE roundtables SET {sets} WHERE id=?",
             (*fields.values(), table_id))


def add_seat(table_id: int, seat_index: int, name: str, provider: str,
             model: str, persona: str = "") -> int:
    cur = _execute(
        "INSERT INTO seats (table_id, seat_index, name, provider, model, persona) "
        "VALUES (?,?,?,?,?,?)",
        (table_id, seat_index, name, provider, model, persona))
    return cur.lastrowid


def list_seats(table_id: int) -> list[dict]:
    return _rows("SELECT * FROM seats WHERE table_id=? ORDER BY seat_index, id", (table_id,))


def clear_seats(table_id: int) -> None:
    _execute("DELETE FROM seats WHERE table_id=?", (table_id,))


def add_turn(table_id: int, seat_id: int | None, phase: str, rnd: int,
             text: str, ok: bool = True) -> int:
    cur = _execute(
        "INSERT INTO turns (table_id, seat_id, phase, round, text, ok, ts) "
        "VALUES (?,?,?,?,?,?,?)",
        (table_id, seat_id, phase, rnd, text, 1 if ok else 0, time.time()))
    return cur.lastrowid


def list_turns(table_id: int) -> list[dict]:
    return _rows("SELECT * FROM turns WHERE table_id=? ORDER BY id", (table_id,))


def set_cooldown(model: str, until_ts: float, reason: str = "") -> None:
    _execute("INSERT INTO model_cooldown (model, until_ts, reason) VALUES (?,?,?) "
             "ON CONFLICT(model) DO UPDATE SET until_ts=excluded.until_ts, "
             "reason=excluded.reason",
             (model, until_ts, reason[:300]))


def load_cooldowns() -> dict[str, float]:
    """Live cooldowns, expired ones dropped."""
    _execute("DELETE FROM model_cooldown WHERE until_ts < ?", (time.time(),))
    return {r["model"]: r["until_ts"] for r in _rows("SELECT * FROM model_cooldown")}


def delete_project(project_id: int) -> dict:
    """Remove a project and everything hanging off it. Irreversible.

    Cascades by hand because SQLite foreign keys are off by default here; missing
    one of these would leave orphan rows that still show up in counts and feeds.
    """
    tasks = [t["id"] for t in list_tasks(project_id)]
    counts = {"tasks": len(tasks)}
    for tid in tasks:
        _execute("DELETE FROM contenders WHERE task_id=?", (tid,))
    counts["events"] = len(_rows("SELECT id FROM events WHERE project_id=?", (project_id,)))
    _execute("DELETE FROM events WHERE project_id=?", (project_id,))
    _execute("DELETE FROM inbox WHERE project_id=?", (project_id,))
    _execute("DELETE FROM tasks WHERE project_id=?", (project_id,))
    # a round table that produced this project loses only the link, not itself
    _execute("UPDATE roundtables SET project_id=NULL WHERE project_id=?", (project_id,))
    _execute("DELETE FROM projects WHERE id=?", (project_id,))
    return counts


def delete_table(table_id: int) -> None:
    _execute("DELETE FROM turns WHERE table_id=?", (table_id,))
    _execute("DELETE FROM seats WHERE table_id=?", (table_id,))
    _execute("DELETE FROM roundtables WHERE id=?", (table_id,))


def set_project_autonomy(project_id: int, autonomy: str) -> None:
    _execute("UPDATE projects SET autonomy=? WHERE id=?", (autonomy, project_id))


def set_project_budget(project_id: int, budget_usd: float) -> None:
    _execute("UPDATE projects SET budget_usd=? WHERE id=?", (budget_usd, project_id))


def set_project_self(project_id: int, is_self: bool = True) -> None:
    """Mark the row that represents this platform's own codebase."""
    _execute("UPDATE projects SET is_self=? WHERE id=?", (1 if is_self else 0, project_id))


def inc_runs(project_id: int) -> int:
    _execute("UPDATE projects SET runs_used = runs_used + 1 WHERE id=?", (project_id,))
    row = get_project(project_id)
    return row["runs_used"] if row else 0


def get_project(project_id: int) -> dict | None:
    rows = _rows("SELECT * FROM projects WHERE id=?", (project_id,))
    return rows[0] if rows else None


def list_projects() -> list[dict]:
    return _rows("SELECT * FROM projects ORDER BY id DESC")


def set_project_status(project_id: int, status: str, summary: str | None = None) -> None:
    if summary is not None:
        _execute("UPDATE projects SET status=?, summary=? WHERE id=?", (status, summary, project_id))
    else:
        _execute("UPDATE projects SET status=? WHERE id=?", (status, project_id))


def add_project_cost(project_id: int, usd: float) -> float:
    """Accumulate estimated spend. On a subscription nothing is billed, so the SDK's
    estimate is recorded as 0 to avoid it being mistaken for real money."""
    if not config.ANTHROPIC_API_KEY:
        usd = 0.0
    _execute("UPDATE projects SET cost_usd = cost_usd + ? WHERE id=?", (usd, project_id))
    row = get_project(project_id)
    return row["cost_usd"] if row else 0.0


# --- tasks ---

def next_seq(project_id: int) -> int:
    """Per-project task numbers. The primary key is global and keeps climbing
    across projects, which makes '#38' meaningless to a boss looking at their
    third project — they see task 1, 2, 3 within their own project instead."""
    rows = _rows("SELECT COALESCE(MAX(seq), 0) AS m FROM tasks WHERE project_id=?", (project_id,))
    return int(rows[0]["m"]) + 1


def resolve_task(project_id: int, n: int) -> dict | None:
    """Look up a task by the number a human (or the manager) used.

    Prefers the per-project seq; falls back to the global id so older sessions
    and internal callers that still hold a real id keep working."""
    rows = _rows("SELECT * FROM tasks WHERE project_id=? AND seq=?", (project_id, int(n)))
    if rows:
        return rows[0]
    rows = _rows("SELECT * FROM tasks WHERE project_id=? AND id=?", (project_id, int(n)))
    return rows[0] if rows else None


def create_task(project_id: int, role: str, title: str, description: str,
                deps: list[int] | None = None, origin: str = "initial") -> int:
    now = time.time()
    cur = _execute(
        "INSERT INTO tasks (project_id, role, title, description, deps, origin, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (project_id, role, title, description, json.dumps(deps or []), origin, now, now),
    )
    task_id = cur.lastrowid
    # branch stays keyed on the global id so branch names are unique across projects.
    # The sprint is stamped from the project so a retrospective can tell this cycle's
    # work from the previous one without guessing by timestamp.
    p = get_project(project_id)
    # BRANCH_PREFIX lets a staging instance share the production repo without its
    # work being mistakable for production's.
    branch = f"{config.BRANCH_PREFIX}task/{task_id}"
    _execute("UPDATE tasks SET branch=?, seq=?, sprint=? WHERE id=?",
             (branch, next_seq(project_id), (p or {}).get("sprint", 1), task_id))
    return task_id


def get_task(task_id: int) -> dict | None:
    rows = _rows("SELECT * FROM tasks WHERE id=?", (task_id,))
    return rows[0] if rows else None


def list_tasks(project_id: int) -> list[dict]:
    return _rows("SELECT * FROM tasks WHERE project_id=? ORDER BY id", (project_id,))


def update_task(task_id: int, **fields: Any) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    _execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))


def touch_task(task_id: int) -> None:
    _execute("UPDATE tasks SET updated_at=? WHERE id=?", (time.time(), task_id))


def count_running(project_id: int) -> int:
    rows = _rows(
        "SELECT COUNT(*) AS n FROM tasks WHERE project_id=? AND status IN ('queued','running')",
        (project_id,),
    )
    return rows[0]["n"]


# --- events ---

def add_event(project_id: int, task_id: int | None, source: str, kind: str, payload: Any) -> dict:
    ts = time.time()
    text = payload if isinstance(payload, str) else json.dumps(payload)
    cur = _execute(
        "INSERT INTO events (project_id, task_id, source, kind, payload, ts) VALUES (?,?,?,?,?,?)",
        (project_id, task_id, source, kind, text, ts),
    )
    return {
        "id": cur.lastrowid,
        "project_id": project_id,
        "task_id": task_id,
        "source": source,
        "kind": kind,
        "payload": text,
        "ts": ts,
    }


def list_events(project_id: int, after_id: int = 0, limit: int = 500) -> list[dict]:
    """The MOST RECENT `limit` events, in chronological order.

    This used to be `ORDER BY id LIMIT 500`, which returns the OLDEST 500 — so
    once a project passed 500 events the dashboard silently froze on ancient
    history and never showed anything current again. Tailing (after_id > 0) still
    wants the oldest-first slice, because there the caller is asking for "what is
    new since X".
    """
    if after_id:
        return _rows(
            "SELECT * FROM events WHERE project_id=? AND id>? ORDER BY id LIMIT ?",
            (project_id, after_id, limit))
    rows = _rows(
        "SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT ?",
        (project_id, limit))
    return list(reversed(rows))


def list_task_events(task_id: int) -> list[dict]:
    return _rows("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))


# --- boss <-> manager inbox ---

def add_directive(project_id: int, text: str) -> int:
    cur = _execute(
        "INSERT INTO inbox (project_id, kind, text, created_at) VALUES (?,?,?,?)",
        (project_id, "directive", text, time.time()),
    )
    return cur.lastrowid


def take_directives(project_id: int) -> list[str]:
    """Return undelivered boss directives and mark them delivered (consume once)."""
    rows = _rows(
        "SELECT * FROM inbox WHERE project_id=? AND kind='directive' AND status='pending' ORDER BY id",
        (project_id,),
    )
    for r in rows:
        _execute("UPDATE inbox SET status='delivered' WHERE id=?", (r["id"],))
    return [r["text"] for r in rows]


def ask_question(project_id: int, text: str, options: list[str]) -> int:
    cur = _execute(
        "INSERT INTO inbox (project_id, kind, text, options, created_at) VALUES (?,?,?,?,?)",
        (project_id, "question", text, json.dumps(options), time.time()),
    )
    return cur.lastrowid


def get_question(qid: int) -> dict | None:
    rows = _rows("SELECT * FROM inbox WHERE id=?", (qid,))
    return rows[0] if rows else None


def answer_question(qid: int, answer: str) -> None:
    _execute("UPDATE inbox SET answer=?, status='answered' WHERE id=?", (answer, qid))


def abandon_questions(project_id: int | None = None) -> int:
    """Mark pending questions as abandoned — nobody is waiting on them anymore.
    Called when a manager session ends/restarts, when a project stops, and when a
    new question supersedes older ones. Without this, a question from a dead
    session stays 'pending' forever and the dashboard keeps re-raising it."""
    if project_id is None:
        cur = _execute("UPDATE inbox SET status='abandoned' WHERE kind='question' AND status='pending'")
    else:
        cur = _execute("UPDATE inbox SET status='abandoned' WHERE kind='question' "
                       "AND status='pending' AND project_id=?", (project_id,))
    return cur.rowcount


def pending_question(project_id: int) -> dict | None:
    rows = _rows(
        "SELECT * FROM inbox WHERE project_id=? AND kind='question' AND status='pending' ORDER BY id DESC LIMIT 1",
        (project_id,),
    )
    return rows[0] if rows else None


# --- contenders (rival attempts at one task) ---

def create_contender(task_id: int, idx: int, branch: str, model: str) -> int:
    cur = _execute(
        "INSERT INTO contenders (task_id, idx, branch, model, created_at) VALUES (?,?,?,?,?)",
        (task_id, idx, branch, model, time.time()),
    )
    return cur.lastrowid


def list_contenders(task_id: int) -> list[dict]:
    return _rows("SELECT * FROM contenders WHERE task_id=? ORDER BY idx", (task_id,))


def list_running_contenders(project_id: int) -> list[dict]:
    """Rival attempts still marked running on a project — used when killing it."""
    return _rows(
        "SELECT c.* FROM contenders c JOIN tasks t ON t.id = c.task_id "
        "WHERE t.project_id=? AND c.status='running'", (project_id,))


def get_contender(contender_id: int) -> dict | None:
    rows = _rows("SELECT * FROM contenders WHERE id=?", (contender_id,))
    return rows[0] if rows else None


def update_contender(contender_id: int, **fields: Any) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    _execute(f"UPDATE contenders SET {cols} WHERE id=?", (*fields.values(), contender_id))


def clear_contenders(task_id: int) -> None:
    _execute("DELETE FROM contenders WHERE task_id=?", (task_id,))


# --- agents: named teammates that outlive a single task ---

def create_agent(project_id: int, name: str, role: str, idx: int = 1,
                 persona: str = "", provider: str = "anthropic",
                 model: str = "") -> int:
    cur = _execute(
        "INSERT INTO agents (project_id, name, role, idx, persona, provider, model, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (project_id, name, role, idx, persona, provider, model, time.time()))
    return int(cur.lastrowid)


def get_agent(agent_id: int) -> dict | None:
    rows = _rows("SELECT * FROM agents WHERE id=?", (agent_id,))
    return rows[0] if rows else None


def list_agents(project_id: int, role: str | None = None) -> list[dict]:
    if role:
        return _rows("SELECT * FROM agents WHERE project_id=? AND role=? ORDER BY idx",
                     (project_id, role))
    return _rows("SELECT * FROM agents WHERE project_id=? ORDER BY role, idx", (project_id,))


def update_agent(agent_id: int, **fields: Any) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    _execute(f"UPDATE agents SET {cols} WHERE id=?", (*fields.values(), agent_id))


def free_agents(project_id: int, role: str | None = None) -> list[dict]:
    """Teammates holding no running task. The scheduler needs this to answer
    'who could help?', which it could not ask before — capacity was implicit in
    the dependency graph and nowhere else."""
    return [a for a in list_agents(project_id, role) if a["status"] == "idle"]


# --- runs: one row per dispatch, so the algorithm can be measured ---

def start_run(project_id: int, task_id: int | None, *, role: str = "", model: str = "",
              provider: str = "anthropic", attempt: int = 1, kind: str = "task",
              sprint: int = 1, agent_id: int | None = None, profile: str = "",
              contender_id: int | None = None) -> int:
    cur = _execute(
        "INSERT INTO runs (project_id, task_id, agent_id, contender_id, sprint, role, "
        "provider, model, attempt, kind, profile, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (project_id, task_id, agent_id, contender_id, sprint, role, provider, model, attempt,
         kind, profile, time.time()))
    return int(cur.lastrowid)


def open_run_for(task_id: int, contender_id: int | None = None) -> dict | None:
    """The still-open run for this dispatch. Looked up rather than held in memory
    so a report that arrives after a restart still closes its run — otherwise the
    run stays 'running' forever and quietly distorts every average computed from it."""
    if contender_id:
        rows = _rows("SELECT * FROM runs WHERE contender_id=? AND outcome='running' "
                     "ORDER BY id DESC LIMIT 1", (contender_id,))
    else:
        rows = _rows("SELECT * FROM runs WHERE task_id=? AND contender_id IS NULL "
                     "AND outcome='running' ORDER BY id DESC LIMIT 1", (task_id,))
    return rows[0] if rows else None


def latest_run(task_id: int) -> dict | None:
    rows = _rows("SELECT * FROM runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,))
    return rows[0] if rows else None


def judge_run(task_id: int, outcome: str, reason: str = "") -> None:
    """Record the manager's verdict on the most recent delivered run.

    Delivery and acceptance are different events — a worker reporting 'pushed'
    means it finished, not that the work was any good — so the run is closed as
    'delivered' first and only relabelled once someone judges it. Collapsing the
    two would make first-pass acceptance unmeasurable, which is the one number
    worth having."""
    rows = _rows("SELECT id FROM runs WHERE task_id=? AND outcome='delivered' "
                 "ORDER BY id DESC LIMIT 1", (task_id,))
    if rows:
        _execute("UPDATE runs SET outcome=?, reason=? WHERE id=?",
                 (outcome, reason[:500], rows[0]["id"]))


def finish_run(run_id: int, outcome: str, *, reason: str = "", cost_usd: float = 0.0,
               turns: int = 0) -> None:
    _execute("UPDATE runs SET outcome=?, reason=?, cost_usd=?, turns=?, ended_at=? WHERE id=?",
             (outcome, reason[:500], cost_usd, turns, time.time(), run_id))


def list_runs(project_id: int | None = None, limit: int = 500) -> list[dict]:
    if project_id is None:
        return _rows("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
    return _rows("SELECT * FROM runs WHERE project_id=? ORDER BY id DESC LIMIT ?",
                 (project_id, limit))


def open_runs(project_id: int) -> list[dict]:
    return _rows("SELECT * FROM runs WHERE project_id=? AND outcome='running'", (project_id,))


# --- kv: durable small facts ---

def kv_get(key: str, default: Any = None) -> Any:
    rows = _rows("SELECT v FROM kv WHERE k=?", (key,))
    if not rows:
        return default
    try:
        return json.loads(rows[0]["v"])
    except json.JSONDecodeError:
        return default


def kv_set(key: str, value: Any) -> None:
    _execute("INSERT INTO kv (k, v, ts) VALUES (?,?,?) "
             "ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts=excluded.ts",
             (key, json.dumps(value), time.time()))


def kv_delete(key: str) -> None:
    _execute("DELETE FROM kv WHERE k=?", (key,))


def kv_prefix(prefix: str) -> dict[str, Any]:
    out = {}
    for r in _rows("SELECT k, v FROM kv WHERE k LIKE ?", (prefix + "%",)):
        try:
            out[r["k"]] = json.loads(r["v"])
        except json.JSONDecodeError:
            pass
    return out


# --- tuning: orchestration knobs ---

def tuning_all() -> dict[str, Any]:
    out = {}
    for r in _rows("SELECT k, v FROM tuning", ()):
        try:
            out[r["k"]] = json.loads(r["v"])
        except json.JSONDecodeError:
            pass
    return out


def tuning_set(key: str, value: Any, who: str = "") -> None:
    _execute("INSERT INTO tuning (k, v, updated_at, updated_by) VALUES (?,?,?,?) "
             "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at, "
             "updated_by=excluded.updated_by",
             (key, json.dumps(value), time.time(), who))


def tuning_clear(key: str) -> None:
    _execute("DELETE FROM tuning WHERE k=?", (key,))
