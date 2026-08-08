"""Vendored helpers — copied into every service, never imported across service dirs.

~60 lines is the deliberate price of structural isolation: a shared "fleet_common"
package would make every service importable from every other, and the whole point
of a service is that nothing outside its directory reaches inside it. The token
check is modeled on conductor/app/routes/internal.py's _check_token (constant-time,
fail closed); the sqlite discipline is the kernel's (WAL, ONE owner per file — this
service alone opens its DB_PATH; anyone else asks over HTTP).

Env-only config, per the contract: PORT, DB_PATH, SERVICE_TOKEN, peer *_URL —
all arrive from data/env/<name>.env, written by tools/gen_fleet.py.
"""

import hmac
import os
import sqlite3
from pathlib import Path

from fastapi import Header, HTTPException

HERE = Path(__file__).resolve().parent
SERVICE = os.environ.get("SERVICE_NAME", HERE.name)
DB_PATH = Path(os.environ.get("DB_PATH", f"data/{SERVICE}.db"))
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")


def require_token(x_service_token: str | None = Header(None)) -> None:
    """Constant-time X-Service-Token check. A plain != leaks the token one
    character at a time to anyone who can measure the response. No token
    configured = refuse everything: fail closed, loudly, not open."""
    if not SERVICE_TOKEN:
        raise HTTPException(503, "SERVICE_TOKEN is not configured — run tools/gen_fleet.py")
    if not x_service_token or not hmac.compare_digest(x_service_token, SERVICE_TOKEN):
        raise HTTPException(401, "bad service token")


_conn: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    """This service's OWN database — WAL so a reader never blocks the writer."""
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        _conn.commit()
    return _conn


def db_ok() -> bool:
    try:
        db().execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def kv_get(key: str, default: str | None = None) -> str | None:
    row = db().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(key: str, value: str) -> None:
    db().execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    db().commit()
