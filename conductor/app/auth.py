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

from . import config, db, providers

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
    existing = get_user_by_name(ROOT_USERNAME)
    if not existing:
        create_user(ROOT_USERNAME, ROOT_PASSWORD, is_root=True)
        return
    # Rotating ROOT_PASSWORD used to do nothing at all: root was seeded on first
    # boot and never touched again, so changing the secret and restarting left the
    # old password working and the new one rejected — with no error anywhere to
    # say so. Anyone who thought they had rotated a credential had not.
    #
    # Safe to reconcile on every boot because there is no way to change this
    # password inside the app; the environment is the only source of truth for it.
    if ROOT_PASSWORD and not hmac.compare_digest(
            _hash(ROOT_PASSWORD, existing["pw_salt"]), existing["pw_hash"]):
        salt = secrets.token_hex(16)
        db._execute("UPDATE users SET pw_hash=?, pw_salt=? WHERE id=?",
                    (_hash(ROOT_PASSWORD, salt), salt, existing["id"]))
        from . import logs
        logs.info("auth", "root_password_updated",
                  "root password updated from the environment")


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


# Failed logins per username: [timestamps], stored in the database.
#
# This lived in memory, on the reasoning that a restart is not something an
# attacker can cause from the login form. True, and beside the point: they do not
# need to cause one. The pod restarts on its own — a rollout, a node drain, an
# OOM kill, a self-update — and each time it does, every lockout in force is
# lifted. An attacker who paces their guesses does not have to break anything to
# get their attempts back, only to wait for something ordinary to happen.
LOCKOUT_AFTER = int(config._env("LOGIN_LOCKOUT_AFTER", "8"))
LOCKOUT_WINDOW = int(config._env("LOGIN_LOCKOUT_WINDOW", "300"))


def _fails(username: str) -> list[float]:
    now = time.time()
    return [t for t in db.kv_get(f"login_fail:{username}", []) or []
            if now - t < LOCKOUT_WINDOW]


def _record_fail(username: str) -> None:
    tries = _fails(username)
    tries.append(time.time())
    # Bounded: a sustained attack must not grow a row without limit, and only the
    # oldest of the last LOCKOUT_AFTER matters for computing the release time.
    db.kv_set(f"login_fail:{username}", tries[-(LOCKOUT_AFTER * 2):])


def _clear_fails(username: str) -> None:
    db.kv_delete(f"login_fail:{username}")


def locked_out(username: str) -> int:
    """Seconds until this username may try again; 0 if it may try now."""
    tries = _fails(username)
    if len(tries) < LOCKOUT_AFTER:
        return 0
    return max(1, int(LOCKOUT_WINDOW - (time.time() - tries[0])))


def verify(username: str, password: str) -> dict | None:
    """Check a password, with a ceiling on how fast it can be guessed.

    Unbounded attempts are only survivable behind a firewall. The moment this is
    reachable from the internet the password becomes the entire security boundary,
    and a boundary you can test a thousand times a second is not one.
    """
    if locked_out(username):
        return None
    u = get_user_by_name(username)
    if not u:
        # Record the attempt anyway: otherwise guessing USERNAMES is free, and
        # "that account does not exist" is itself worth knowing to an attacker.
        _record_fail(username)
        return None
    if hmac.compare_digest(_hash(password, u["pw_salt"]), u["pw_hash"]):
        _clear_fails(username)
        return u
    _record_fail(username)
    return None


def password_is_weak(password: str) -> str:
    """Why this password should not guard an internet-reachable control plane."""
    if len(password) < 12:
        return f"only {len(password)} characters — 12 is the practical minimum"
    classes = sum(bool(__import__("re").search(p, password))
                  for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if classes < 3:
        return "uses fewer than three character classes"
    return ""


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
        # Same root-only rule for the operator's own inference endpoints. setdefault
        # and not merge: once root has saved endpoints in the app, those are what
        # root meant, and silently re-adding a decommissioned server from .env would
        # put an endpoint back that the operator had deliberately removed.
        s.setdefault("custom_endpoints", config.CUSTOM_ENDPOINTS)
        return {k: v for k, v in s.items() if v}   # drop the blanks setdefault added
    return s


# --- settings REFERENCES: how a service spends without holding a key ---------
#
# Since P4 the lifeworld runs in its own process and still has to make model
# calls on the owner's account. A credential must never cross that wire, so what
# crosses instead is a REFERENCE: a short signed string naming a principal, minted
# here, carried by the service exactly where the settings dict used to sit, and
# resolved back to real settings only at the conductor's model door
# (POST /internal/complete).
#
# SIGNED, not merely opaque. Without the signature the reference would be a bare
# user id and the door would resolve whatever principal a caller typed — one
# compromised service could then spend any account's quota. The HMAC is over the
# conductor's own WORKER_TOKEN, which is the secret every other door on this box
# already trusts, so there is no new key to distribute or rotate.
#
# It carries no expiry on purpose: a live world is loaded and dropped inside one
# request, and a reference that expired mid-deliberation would fail a sprint for
# a clock. The bound is the token — rotate WORKER_TOKEN and every outstanding
# reference stops resolving.

def _ref_sig(body: str) -> str:
    return hmac.new(config.WORKER_TOKEN.encode(), body.encode(),
                    hashlib.sha256).hexdigest()[:32]


def mint_settings_ref(user: dict | None) -> str:
    """A signed reference to this user's settings, for a service that must spend
    on their behalf. Empty string for no user — which every client reads as
    "free mode", never as "spend anyway"."""
    if not user or not user.get("id"):
        return ""
    body = f"user:{int(user['id'])}"
    return f"{body}.{_ref_sig(body)}"


def resolve_settings_ref(ref: str) -> dict:
    """The settings a reference names — or {} for anything that does not verify.

    Constant-time comparison: a plain != on the signature leaks it one character
    at a time to anyone who can measure the response, and this signature is the
    only thing standing between a service token and every account's credentials.
    """
    body, _, sig = str(ref or "").rpartition(".")
    if not body or not sig or not hmac.compare_digest(sig, _ref_sig(body)):
        return {}
    kind, _, ident = body.partition(":")
    if kind != "user" or not ident.isdigit():
        return {}
    u = get_user(int(ident))
    return get_settings(u) if u else {}


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


def save_endpoint(user: dict, raw: dict) -> dict:
    """Add or replace one custom inference endpoint on this user.

    Replaces by id rather than appending, so saving the same endpoint twice is a
    correction and not a duplicate the picker then shows twice.

    An omitted `api_key` KEEPS the stored one. The dialog never sends a key back
    to the browser, so a user editing the model list of an endpoint would submit a
    blank key field and silently un-authenticate a working server — the same trap
    the redaction exists to avoid. Clearing a key is an explicit empty string,
    matching how `save_settings` already treats one.
    """
    ep = providers.normalise_endpoint(raw)
    if not ep:
        raise ValueError("an endpoint needs a name and a base URL")
    existing = {e["id"]: e for e in providers.custom_endpoints(get_settings(user))}
    if raw.get("api_key") is None and ep["id"] in existing:
        ep["api_key"] = existing[ep["id"]]["api_key"]
    existing[ep["id"]] = ep
    save_settings(user["id"], {"custom_endpoints": list(existing.values())})
    return ep


def delete_endpoint(user: dict, endpoint_id: str) -> bool:
    kept = [e for e in providers.custom_endpoints(get_settings(user))
            if e["id"] != endpoint_id]
    if len(kept) == len(providers.custom_endpoints(get_settings(user))):
        return False
    # An empty list, not a cleared key: save_settings drops a key on "" only, and
    # dropping this one would hand root back whatever .env still names.
    save_settings(user["id"], {"custom_endpoints": kept})
    return True


def redacted(settings: dict) -> dict:
    """Never send secrets back to the browser — only whether they're set."""
    if config.DEMO_MODE:
        # A sandbox holds nothing, but showing every credential as "not set" makes
        # the Settings screen under test look broken and nags for keys that would
        # be pointless to enter. Report the configured shape instead.
        return {k: True for k in ("github_token_set", "anthropic_api_key_set",
                                  "claude_oauth_token_set")} | {
            "openai_api_key_set": False, "gemini_api_key_set": False,
            "custom_endpoints": [], "github_login": "sandbox"}
    return {
        "github_token_set": bool(settings.get("github_token")),
        "anthropic_api_key_set": bool(settings.get("anthropic_api_key")),
        "claude_oauth_token_set": bool(settings.get("claude_oauth_token")),
        "openai_api_key_set": bool(settings.get("openai_api_key")),
        "gemini_api_key_set": bool(settings.get("gemini_api_key")),
        # An endpoint is configuration, not a secret — the user needs to see the
        # base URL to know which server they pointed at. Only the key is withheld,
        # and whether one is set is itself worth showing: a server that needs a key
        # and has none fails identically to a server that is down.
        "custom_endpoints": [
            {"id": e["id"], "label": e["label"], "base_url": e["base_url"],
             "key_header": e["key_header"], "key_set": bool(e["api_key"]),
             "models": [m["id"] for m in e["models"]]}
            for e in providers.custom_endpoints(settings)],
        "github_login": settings.get("github_login", ""),
    }


def clear_lockouts(username: str | None = None) -> int:
    """Release locked-out accounts. Root does this when a legitimate user locks
    themselves out, which is the common case by a wide margin — the alternative is
    telling them to wait, or restarting the server, which used to be the same thing.
    """
    if username:
        _clear_fails(username)
        return 1
    keys = list(db.kv_prefix("login_fail:"))
    for k in keys:
        db.kv_delete(k)
    return len(keys)
