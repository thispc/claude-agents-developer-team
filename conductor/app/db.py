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
"""


def init() -> None:
    global _conn
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    try:  # migration for DBs created before the deps column existed
        _conn.execute("ALTER TABLE tasks ADD COLUMN deps TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass
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

def create_project(name: str, brief: str, repo: str, budget_usd: float, max_workers: int) -> int:
    cur = _execute(
        "INSERT INTO projects (name, brief, repo, budget_usd, max_workers, created_at) VALUES (?,?,?,?,?,?)",
        (name, brief, repo, budget_usd, max_workers, time.time()),
    )
    return cur.lastrowid


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
    _execute("UPDATE projects SET cost_usd = cost_usd + ? WHERE id=?", (usd, project_id))
    row = get_project(project_id)
    return row["cost_usd"] if row else 0.0


# --- tasks ---

def create_task(project_id: int, role: str, title: str, description: str,
                deps: list[int] | None = None) -> int:
    now = time.time()
    cur = _execute(
        "INSERT INTO tasks (project_id, role, title, description, deps, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (project_id, role, title, description, json.dumps(deps or []), now, now),
    )
    task_id = cur.lastrowid
    _execute("UPDATE tasks SET branch=? WHERE id=?", (f"task/{task_id}", task_id))
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
