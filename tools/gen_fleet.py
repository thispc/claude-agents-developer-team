"""Generate the fleet from services.yaml — the registry is the only thing you edit.

Reads the repo's services.yaml and writes four things:

  process-compose.yaml      the managed processes (kind core/service) with an
                            http_get readiness probe on each service's health path,
                            depends_on process_healthy, restart on_failure with
                            backoff, and one unified log at data/logs/fleet.log
  data/env/<name>.env       each managed service's environment: PORT, DB_PATH,
                            SERVICE_TOKEN, a <PEER>_URL per managed peer, and
                            CONDUCTOR_URL derived from the conductor's port
  data/tokens/<name>.token  minted ONCE (secrets.token_hex) and then stable —
                            re-running the generator never rotates a token.
                            Also mints fleet-api.token for process-compose's REST API.
  data/fleet_topology.json  every service (managed or not) with kind/port/health/
                            depends_on/ui — what the /svc gateway resolves against
                            and what the Atlas will render (P6).

Deterministic: same services.yaml + same environment → byte-identical outputs, so
re-runs never churn diffs. The one environment input is PORT, which overrides the
conductor's registered port so `PORT=9000 ./run-local.sh` binds — and advertises —
the port it actually uses (defect 1).

Offline and stdlib+PyYAML only. Run from anywhere: paths anchor to the repo root.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent

MANAGED_KINDS = ("core", "service")


def load_registry(path: Path) -> dict:
    """Parse and sanity-check services.yaml. Raises ValueError on a broken schema."""
    reg = yaml.safe_load(path.read_text())
    if not isinstance(reg, dict) or "services" not in reg:
        raise ValueError(f"{path} has no 'services' mapping")
    reg.setdefault("defaults", {})
    reg["defaults"].setdefault("host", "127.0.0.1")
    reg["defaults"].setdefault("health_path", "/health")
    reg["defaults"].setdefault("venv", ".venv")
    reg.setdefault("fleet_api", {}).setdefault("port", 8899)
    for name, svc in reg["services"].items():
        if not isinstance(svc, dict) or "kind" not in svc:
            raise ValueError(f"service {name!r} has no kind")
        if svc["kind"] in MANAGED_KINDS:
            if "port" not in svc:
                raise ValueError(f"managed service {name!r} has no port")
            if "cmd" not in svc:
                raise ValueError(f"managed service {name!r} has no cmd")
        svc.setdefault("ui", False)
    return reg


def _managed(reg: dict) -> dict[str, dict]:
    return {n: s for n, s in reg["services"].items() if s["kind"] in MANAGED_KINDS}


def _port_of(name: str, svc: dict, env: dict) -> int:
    """A service's effective port. PORT in the environment overrides the conductor's
    registered port, so the fleet advertises the port the operator actually chose."""
    if svc["kind"] == "core" and env.get("PORT"):
        return int(env["PORT"])
    return int(svc["port"])


def _token(root: Path, name: str) -> str:
    """Mint once, then stable forever — rotating a token on every boot would break
    any process still holding the old one."""
    tok = root / "data" / "tokens" / f"{name}.token"
    if not tok.exists():
        tok.parent.mkdir(parents=True, exist_ok=True)
        # No trailing newline: process-compose reads its --token-file raw, and 64
        # hex chars also clears its ≥20-char minimum for API tokens.
        tok.write_text(secrets.token_hex(32))
        tok.chmod(0o600)
    return tok.read_text().strip()


def _compose(reg: dict, root: Path, env: dict) -> str:
    host = reg["defaults"]["host"]
    processes: dict = {}
    for name, svc in sorted(_managed(reg).items()):
        port = _port_of(name, svc, env)
        health = svc.get("health_path", reg["defaults"]["health_path"])
        # The registered cmd may say ${PORT}; substitute it OURSELVES so the compose
        # file carries the literal port — process-compose must not depend on its own
        # parse-time environment agreeing with the env file the process sources.
        cmd = svc["cmd"].replace("${PORT}", str(port))
        proc: dict = {
            # Source the service's env file, then exec — one shell, no wrapper script.
            "command": f"set -a; . data/env/{name}.env; set +a; exec {cmd}",
            "readiness_probe": {
                "http_get": {"host": host, "scheme": "http", "port": port, "path": health},
                "initial_delay_seconds": 1,
                # 5s: quick enough to notice a death, without the probe's access-log
                # line dominating fleet.log (2s wrote one every other second, forever)
                "period_seconds": 5,
                "timeout_seconds": 3,
                "failure_threshold": 30,
            },
            "availability": {"restart": "on_failure", "backoff_seconds": 2},
        }
        deps = svc.get("depends_on") or []
        if deps:
            proc["depends_on"] = {d: {"condition": "process_healthy"} for d in sorted(deps)}
        processes[name] = proc
    doc = {
        "version": "0.5",
        "log_location": "data/logs/fleet.log",
        "processes": processes,
    }
    header = ("# GENERATED by tools/gen_fleet.py from services.yaml — do not edit.\n"
              "# Change services.yaml and re-run (run-local.sh does it on every boot).\n")
    return header + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def _env_file(name: str, svc: dict, reg: dict, root: Path, env: dict) -> str:
    host = reg["defaults"]["host"]
    managed = _managed(reg)
    conductor_port = next(_port_of(n, s, env) for n, s in managed.items() if s["kind"] == "core")
    lines = [
        f"# GENERATED by tools/gen_fleet.py for service '{name}' — do not edit.",
        f"PORT={_port_of(name, svc, env)}",
        f"DB_PATH={svc.get('db', f'data/{name}.db')}",
        f"SERVICE_TOKEN={_token(root, name)}",
        # Every service (the conductor included, for the workers it spawns) learns
        # the conductor's REAL port here — never config.py's 8000 default.
        f"CONDUCTOR_URL=http://{host}:{conductor_port}",
    ]
    for peer, psvc in sorted(managed.items()):
        if peer == name or psvc["kind"] == "core":     # conductor already covered above
            continue
        key = peer.upper().replace("-", "_") + "_URL"
        lines.append(f"{key}=http://{host}:{_port_of(peer, psvc, env)}")
    return "\n".join(lines) + "\n"


def _topology(reg: dict, root: Path, env: dict) -> str:
    host = reg["defaults"]["host"]
    services = {}
    for name, svc in reg["services"].items():          # registry order, stable
        managed = svc["kind"] in MANAGED_KINDS
        entry: dict = {"kind": svc["kind"], "managed": managed, "ui": bool(svc.get("ui"))}
        if svc.get("ui_screen"):
            entry["ui_screen"] = True
        if managed:
            port = _port_of(name, svc, env)
            entry["port"] = port
            entry["url"] = f"http://{host}:{port}"
            entry["health"] = svc.get("health_path", reg["defaults"]["health_path"])
            entry["depends_on"] = sorted(svc.get("depends_on") or [])
        else:
            entry["managed_by"] = svc.get("managed_by", "")
            if "port_range" in svc:
                entry["port_range"] = list(svc["port_range"])
        services[name] = entry
    doc = {
        "generated_from": "services.yaml",
        "fleet_api": {"port": int(reg["fleet_api"]["port"]),
                      "host": host,
                      "token_file": "data/tokens/fleet-api.token"},
        "services": services,
    }
    return json.dumps(doc, indent=2) + "\n"


def _write(path: Path, content: str) -> bool:
    """Write only on change, so an unchanged fleet leaves no fresh mtimes behind."""
    if path.exists() and path.read_text() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return True


def generate(root: Path | None = None, env: dict | None = None) -> dict[str, str]:
    """Generate everything. Returns {relative path: 'written'|'unchanged'}."""
    root = root or REPO
    env = dict(os.environ if env is None else env)
    reg = load_registry(root / "services.yaml")
    (root / "data" / "logs").mkdir(parents=True, exist_ok=True)
    _token(root, "fleet-api")                          # the pc REST API's own token
    out: dict[str, str] = {}
    out["process-compose.yaml"] = _compose(reg, root, env)
    for name, svc in _managed(reg).items():
        out[f"data/env/{name}.env"] = _env_file(name, svc, reg, root, env)
    out["data/fleet_topology.json"] = _topology(reg, root, env)
    return {rel: ("written" if _write(root / rel, content) else "unchanged")
            for rel, content in out.items()}


if __name__ == "__main__":
    try:
        results = generate()
    except ValueError as e:
        print(f"gen_fleet: {e}", file=sys.stderr)
        sys.exit(1)
    for rel, state in results.items():
        print(f"gen_fleet: {rel} {state}")
