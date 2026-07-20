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
    if ANTHROPIC_API_KEY:
        return "api-key"
    if CLAUDE_CODE_OAUTH_TOKEN or CLI_LOGIN:
        return "subscription"
    return "none"
LEAD_MODEL = _env("LEAD_MODEL", "claude-sonnet-5")
WORKER_MODEL = _env("WORKER_MODEL", "claude-haiku-4-5")
ESCALATION_MODEL = _env("ESCALATION_MODEL", "claude-sonnet-5")

GITHUB_TOKEN = _env("GITHUB_TOKEN")
GITHUB_REPO = _env("GITHUB_REPO")
# The repo holding this platform's own code. Blank = derive from the git remote.
SELF_REPO = _env("SELF_REPO")

CONDUCTOR_URL = _env("CONDUCTOR_URL", "http://localhost:8000")
WORKER_TOKEN = _env("WORKER_TOKEN", "dev-token")
DB_PATH = _env("DB_PATH", "devteam.db")

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

AGENTS_DIR = Path(_env("AGENTS_DIR", str(ROOT / "agents")))
DASHBOARD_DIR = Path(_env("DASHBOARD_DIR", str(ROOT / "dashboard")))
WORKSPACES_DIR = Path(_env("WORKSPACES_DIR", str(ROOT / "workspaces")))
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
