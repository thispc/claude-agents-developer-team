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
                   manager_persona: str = "", owner_id: int = 0) -> int:
    cur = _execute(
        "INSERT INTO projects (name, brief, repo, budget_usd, max_workers, max_runs, team, "
        "autonomy, manager_model, manager_persona, owner_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, brief, repo, budget_usd, max_workers, max_runs, json.dumps(team or []),
         autonomy, manager_model, manager_persona, owner_id, time.time()),
    )
    return cur.lastrowid


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
    # branch stays keyed on the global id so branch names are unique across projects
    _execute("UPDATE tasks SET branch=?, seq=? WHERE id=?",
             (f"task/{task_id}", next_seq(project_id), task_id))
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
    return _rows(
        "SELECT * FROM events WHERE project_id=? AND id>? ORDER BY id LIMIT ?",
        (project_id, after_id, limit),
    )


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


def get_contender(contender_id: int) -> dict | None:
    rows = _rows("SELECT * FROM contenders WHERE id=?", (contender_id,))
    return rows[0] if rows else None


def update_contender(contender_id: int, **fields: Any) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    _execute(f"UPDATE contenders SET {cols} WHERE id=?", (*fields.values(), contender_id))


def clear_contenders(task_id: int) -> None:
    _execute("DELETE FROM contenders WHERE task_id=?", (task_id,))
