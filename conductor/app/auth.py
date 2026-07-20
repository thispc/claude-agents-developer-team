"""Users, sessions, and per-user settings.

A root superuser is seeded from .env (its GitHub token / API key come from the server
config), so the current single-user setup keeps working. Additional users sign up and
supply their own GitHub token and Anthropic key, stored per user — the path toward
real multi-tenant use and GitHub OAuth later.
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time

from . import config, db

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    pw_hash TEXT NOT NULL,
    pw_salt TEXT NOT NULL,
    is_root INTEGER NOT NULL DEFAULT 0,
    settings TEXT NOT NULL DEFAULT '{}',   -- {github_token, anthropic_api_key, github_login}
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL
);
"""

ROOT_USERNAME = config._env("ROOT_USERNAME", "root")
ROOT_PASSWORD = config._env("ROOT_PASSWORD", "devteam")


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def init() -> None:
    db._conn.executescript(SCHEMA)
    db._conn.commit()
    if not get_user_by_name(ROOT_USERNAME):
        create_user(ROOT_USERNAME, ROOT_PASSWORD, is_root=True)


def create_user(username: str, password: str, is_root: bool = False) -> int:
    salt = secrets.token_hex(16)
    cur = db._execute(
        "INSERT INTO users (username, pw_hash, pw_salt, is_root, created_at) VALUES (?,?,?,?,?)",
        (username, _hash(password, salt), salt, 1 if is_root else 0, time.time()),
    )
    return cur.lastrowid


def get_user_by_name(username: str) -> dict | None:
    rows = db._rows("SELECT * FROM users WHERE username=?", (username,))
    return rows[0] if rows else None


def get_user(user_id: int) -> dict | None:
    rows = db._rows("SELECT * FROM users WHERE id=?", (user_id,))
    return rows[0] if rows else None


def verify(username: str, password: str) -> dict | None:
    u = get_user_by_name(username)
    if not u:
        return None
    if hmac.compare_digest(_hash(password, u["pw_salt"]), u["pw_hash"]):
        return u
    return None


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db._execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)",
                (token, user_id, time.time()))
    return token


def end_session(token: str) -> None:
    db._execute("DELETE FROM sessions WHERE token=?", (token,))


def user_for_token(token: str | None) -> dict | None:
    if not token:
        return None
    rows = db._rows("SELECT user_id FROM sessions WHERE token=?", (token,))
    return get_user(rows[0]["user_id"]) if rows else None


def get_settings(user: dict) -> dict:
    """Per-user secrets. The root user falls back to the server .env values so the
    existing single-user setup keeps working with nothing to configure."""
    try:
        s = json.loads(user["settings"] or "{}")
    except Exception:
        s = {}
    if user["is_root"]:
        s.setdefault("github_token", config.GITHUB_TOKEN)
        s.setdefault("anthropic_api_key", config.ANTHROPIC_API_KEY)
        s.setdefault("default_repo_owner", "")
    return s


def save_settings(user_id: int, updates: dict) -> dict:
    u = get_user(user_id)
    current = json.loads(u["settings"] or "{}") if u else {}
    for k, v in updates.items():
        if v == "":          # empty string clears a value
            current.pop(k, None)
        elif v is not None:
            current[k] = v
    db._execute("UPDATE users SET settings=? WHERE id=?", (json.dumps(current), user_id))
    return current


def redacted(settings: dict) -> dict:
    """Never send secrets back to the browser — only whether they're set."""
    return {
        "github_token_set": bool(settings.get("github_token")),
        "anthropic_api_key_set": bool(settings.get("anthropic_api_key")),
        "github_login": settings.get("github_login", ""),
    }
