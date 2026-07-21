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


SESSION_TTL = 30 * 86400   # matches the cookie max-age set in routes.login


def user_for_token(token: str | None) -> dict | None:
    if not token:
        return None
    rows = db._rows("SELECT user_id, created_at FROM sessions WHERE token=?", (token,))
    if not rows:
        return None
    if time.time() - rows[0]["created_at"] > SESSION_TTL:
        db._execute("DELETE FROM sessions WHERE token=?", (token,))   # expired
        return None
    return get_user(rows[0]["user_id"])


def prune_sessions() -> int:
    """Drop expired sessions. Called at startup so the table can't grow forever."""
    cutoff = time.time() - SESSION_TTL
    cur = db._execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
    return cur.rowcount if cur else 0


def get_settings(user: dict) -> dict:
    """Per-user secrets. ONLY the root/operator account inherits the server .env values;
    normal users must supply their own so they never spend the operator's quota."""
    try:
        s = json.loads(user["settings"] or "{}")
    except Exception:
        s = {}
    if user["is_root"]:
        s.setdefault("github_token", config.GITHUB_TOKEN)
        s.setdefault("anthropic_api_key", config.ANTHROPIC_API_KEY)
        # The rest of the operator's .env, on the same root-only rule. Without
        # these, a key sitting in .env was invisible to everything that reads
        # settings — the round table, the planner and the Settings dialog all
        # reported "not set" for a key the operator had definitely configured.
        s.setdefault("claude_oauth_token", config.CLAUDE_CODE_OAUTH_TOKEN)
        s.setdefault("gemini_api_key", config.GEMINI_API_KEY)
        s.setdefault("openai_api_key", config.OPENAI_API_KEY)
        return {k: v for k, v in s.items() if v}   # drop the blanks setdefault added
    return s


def has_own_ai_credentials(user: dict) -> bool:
    """True when this user can run agents on their own account. Root also counts if
    the server itself is authenticated (its own key / token / machine CLI login)."""
    # A sandbox deliberately holds no credentials — that is the guarantee. Asking
    # it for keys is backwards: there is nothing to authenticate because no agent
    # will ever run, and pasting a real key into a throwaway build is the one
    # thing nobody should be encouraged to do.
    if config.DEMO_MODE:
        return True
    s = get_settings(user)
    if s.get("anthropic_api_key") or s.get("claude_oauth_token"):
        return True
    return bool(user["is_root"] and config.AUTH_CONFIGURED)


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
    if config.DEMO_MODE:
        # A sandbox holds nothing, but showing every credential as "not set" makes
        # the Settings screen under test look broken and nags for keys that would
        # be pointless to enter. Report the configured shape instead.
        return {k: True for k in ("github_token_set", "anthropic_api_key_set",
                                  "claude_oauth_token_set")} | {
            "openai_api_key_set": False, "gemini_api_key_set": False,
            "github_login": "sandbox"}
    return {
        "github_token_set": bool(settings.get("github_token")),
        "anthropic_api_key_set": bool(settings.get("anthropic_api_key")),
        "claude_oauth_token_set": bool(settings.get("claude_oauth_token")),
        "openai_api_key_set": bool(settings.get("openai_api_key")),
        "gemini_api_key_set": bool(settings.get("gemini_api_key")),
        "github_login": settings.get("github_login", ""),
    }
