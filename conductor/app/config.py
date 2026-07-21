import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # repo root


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
# Subscription auth (Claude Pro/Max): long-lived OAuth token from `claude setup-token`.
# If both are set, the API key wins and BILLS API CREDIT — set only one.
CLAUDE_CODE_OAUTH_TOKEN = _env("CLAUDE_CODE_OAUTH_TOKEN")


def _has_cli_login() -> bool:
    """True when the local `claude` CLI holds stored subscription credentials
    (~/.claude/.credentials.json on Linux, keychain on macOS). Local-launcher
    agents inherit them automatically when no API key/token env is set."""
    import subprocess
    if (Path.home() / ".claude" / ".credentials.json").exists():
        return True
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials"],
            capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


CLI_LOGIN = not ANTHROPIC_API_KEY and not CLAUDE_CODE_OAUTH_TOKEN and _has_cli_login()
AUTH_CONFIGURED = bool(ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN or CLI_LOGIN)


def auth_mode() -> str:
    # A sandbox has no credentials on purpose, so it would report "none" — and the
    # dashboard reads this to decide whether to show agent RUNS or DOLLARS. The
    # candidate build would then render "$0.00 / $5.00" where the real one shows
    # "9/40 runs", which is a difference in the thing under test rather than in
    # the sandbox. The parent passes down the shape of its own auth, never the
    # secret behind it.
    if DEMO_MODE:
        return _env("DEMO_AUTH_MODE", "subscription")
    if ANTHROPIC_API_KEY:
        return "api-key"
    if CLAUDE_CODE_OAUTH_TOKEN or CLI_LOGIN:
        return "subscription"
    return "none"
# Other providers, for planning and the round table. Both are API-key only —
# there is no subscription equivalent, so every call on these bills real money.
# GEMINI_KEY is accepted as an alias because that is what people actually write
# in a .env, and a variable the app never reads looks exactly like a broken key.
GEMINI_API_KEY = _env("GEMINI_API_KEY") or _env("GEMINI_KEY")
OPENAI_API_KEY = _env("OPENAI_API_KEY")


def _custom_endpoints() -> list:
    """The operator's own inference endpoints, as JSON in one variable.

    Bring-your-own-keys was never only about keys: a user with a vLLM server on
    their own cluster, an Azure deployment or a Bedrock gateway had no way in at
    all. Endpoints are normally configured per user in Settings; this variable
    exists for the same reason the key variables do — an operator running the
    server does not want to type their own infrastructure into a web form on
    every fresh database.

    Malformed JSON is ignored rather than fatal. A typo here must not stop the
    server from booting, because at that point nobody can log in to fix it.
    """
    import json
    raw = _env("CUSTOM_MODEL_ENDPOINTS").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        print(f"[startup] CUSTOM_MODEL_ENDPOINTS is not valid JSON, ignoring it: {e}")
        return []
    return parsed if isinstance(parsed, list) else []


CUSTOM_ENDPOINTS = _custom_endpoints()

LEAD_MODEL = _env("LEAD_MODEL", "claude-sonnet-5")
WORKER_MODEL = _env("WORKER_MODEL", "claude-haiku-4-5")
ESCALATION_MODEL = _env("ESCALATION_MODEL", "claude-sonnet-5")

GITHUB_TOKEN = _env("GITHUB_TOKEN")
GITHUB_REPO = _env("GITHUB_REPO")
# Which git host this instance talks to. "your own git" was the one promise the
# platform did not keep: the API host was a constant, so an in-house server could
# not be used at all. GitHub Enterprise Server needs nothing here but its own base
# URL (https://<host>/api/v3) — same API, different address.
GIT_API = _env("GIT_API", "https://api.github.com").rstrip("/")
# Human-facing links are built from this, never from GIT_API. On Enterprise and
# Gitea the two are different hosts, and a link built from the API host lands the
# reviewer on JSON or a 404.
GIT_WEB = _env("GIT_WEB", "https://github.com").rstrip("/")
# Only for the handful of calls where an API genuinely disagrees with GitHub's —
# a different host is configuration, not a provider. Set this wrong against a
# Gitea server and merges fail; set it wrong against GitHub and everything fails.
GIT_PROVIDER = _env("GIT_PROVIDER", "github").strip().lower()
# The repo holding this platform's own code. Blank = derive from the git remote.
SELF_REPO = _env("SELF_REPO")
# Non-root usernames allowed to run self-repair, comma-separated. Granted here and
# not in the app on purpose: self-repair writes to the repo this server runs from,
# so a compromised account must not be able to grant it to itself.
SELFREPAIR_USERS = [u.strip().lower() for u in _env("SELFREPAIR_USERS").split(",") if u.strip()]


def may_self_repair(username: str, is_root: bool) -> bool:
    return bool(is_root or (username or "").lower() in SELFREPAIR_USERS)
# Wildcard domain for deployed apps (e.g. apps.example.com -> app-11.apps.example.com).
# Blank = fall back to a per-app LoadBalancer, which the cloud bills for per app.
APPS_DOMAIN = _env("APPS_DOMAIN")
# Target CPU architecture for images pushed to a remote cluster. DigitalOcean
# droplets are amd64; a Mac builds arm64 by default, and that image dies with
# "exec format error" on the node. Blank = build natively (local kind only).
DEPLOY_PLATFORM = _env("DEPLOY_PLATFORM", "linux/amd64")

CONDUCTOR_URL = _env("CONDUCTOR_URL", "http://localhost:8000")
WORKER_TOKEN = _env("WORKER_TOKEN", "dev-token")
# Anchored to the repo root, NOT the process's working directory. A relative
# path means the conductor silently opens (and creates) a different, empty
# database if it is ever started from another directory — losing every project
# with no error at all.
DB_PATH = str(Path(_env("DB_PATH", "devteam.db")) if Path(_env("DB_PATH", "devteam.db")).is_absolute()
              else ROOT / _env("DB_PATH", "devteam.db"))

# A sandboxed candidate build of THIS platform runs with DEMO_MODE=1: no real
# agent sessions, no credentials, no GitHub, seeded data instead. It exists so a
# self-repair PR can be clicked through before it is deployed over the live app.
# Never set this on a real instance — it makes the platform pretend to work.
DEMO_MODE = _env("DEMO_MODE") == "1"

# Staging opens pull requests but does not merge them: a branch is harmless, a
# merge into the branch production builds from is not. Same repo, same account,
# different power — which is what makes one shared repo safe for both.
ALLOW_MERGE = _env("ALLOW_MERGE", "1") != "0"
# Everything this instance pushes is prefixed, so staging work is identifiable at
# a glance and cannot be mistaken for production's.
BRANCH_PREFIX = _env("BRANCH_PREFIX", "")
DEVTEAM_ENV = _env("DEVTEAM_ENV", "production")

LAUNCHER = _env("LAUNCHER", "local")  # local | k8s
WORKER_IMAGE = _env("WORKER_IMAGE", "devteam-worker:latest")
K8S_NAMESPACE = _env("K8S_NAMESPACE", "devteam")

MAX_CONCURRENT_WORKERS = int(_env("MAX_CONCURRENT_WORKERS", "3"))
# Turns inside ONE agent session (tool-call round trips). Hitting this kills work
# mid-flight, so keep it generous — a full-stack build legitimately needs many turns.
WORKER_MAX_TURNS = int(_env("WORKER_MAX_TURNS", "120"))
# A retry after a turn-limit death gets more room than the attempt that ran out.
WORKER_MAX_TURNS_RETRY = int(_env("WORKER_MAX_TURNS_RETRY", "180"))
LEAD_MAX_TURNS = int(_env("LEAD_MAX_TURNS", "120"))
PROJECT_BUDGET_USD = float(_env("PROJECT_BUDGET_USD", "5.0"))
# Primary safety rail: max total agent runs (worker dispatches) per project.
# This is the meaningful cap on a subscription, where dollar cost is not billed.
MAX_AGENT_RUNS = int(_env("MAX_AGENT_RUNS", "40"))
# How long a FULL-AUTONOMY manager waits for an answer before deciding for itself.
# Overnight runs cannot afford to lose an hour per question to a sleeping boss.
AUTONOMOUS_QUESTION_GRACE = int(_env("AUTONOMOUS_QUESTION_GRACE", "300"))

AGENTS_DIR = Path(_env("AGENTS_DIR", str(ROOT / "agents")))
DASHBOARD_DIR = Path(_env("DASHBOARD_DIR", str(ROOT / "dashboard")))
WORKSPACES_DIR = Path(_env("WORKSPACES_DIR", str(ROOT / "workspaces")))
# Ceiling on what all worker clones together may occupy. A count of clones is not
# a bound — repo size is unbounded, so a handful of large checkouts fill the 10Gi
# volume as thoroughly as hundreds of small ones, and a full volume stops the
# entire platform because every agent needs somewhere to clone. Well under the
# volume so the database and an in-flight clone still have room.
WORKSPACES_BUDGET_BYTES = int(_env("WORKSPACES_BUDGET_BYTES", str(6 * 1024**3)))
WORKER_SCRIPT = Path(_env("WORKER_SCRIPT", str(ROOT / "worker" / "worker.py")))


def load_role_prompt(role: str) -> str:
    path = AGENTS_DIR / f"{role}.md"
    if not path.exists():
        raise FileNotFoundError(f"No role prompt for '{role}' at {path}")
    return path.read_text()


def _resolve_model(spec: str) -> str:
    """Map a role's 'model' field to a concrete model id."""
    return {"worker": WORKER_MODEL, "lead": LEAD_MODEL, "escalation": ESCALATION_MODEL}.get(
        spec, spec or WORKER_MODEL)


def load_roles() -> list[dict]:
    """Declarative team roles from agents/roles.json. Each: name, model (resolved to an
    id), max_parallel, summary, fan_out. Adding a role = one entry here + a <name>.md prompt."""
    import json
    path = AGENTS_DIR / "roles.json"
    if not path.exists():
        # Sensible default matching the shipped prompts.
        return [{"name": r, "model": WORKER_MODEL, "max_parallel": 3, "summary": "", "fan_out": ""}
                for r in ("backend", "frontend", "tester")]
    data = json.loads(path.read_text())
    roles = []
    for r in data.get("roles", []):
        roles.append({
            "name": r["name"],
            "model": _resolve_model(r.get("model", "worker")),
            "max_parallel": int(r.get("max_parallel", 3)),
            "summary": r.get("summary", ""),
            "fan_out": r.get("fan_out", ""),
        })
    return roles


def roles_by_name() -> dict[str, dict]:
    return {r["name"]: r for r in load_roles()}
