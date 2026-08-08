"""Generate the fleet from services.yaml — the registry is the only thing you edit.

Reads the repo's services.yaml and writes four things:

  process-compose.yaml      the managed processes (kind core/service) with an
                            http_get readiness probe on each service's health path,
                            depends_on process_healthy, restart on_failure with
                            backoff, and one unified log at data/logs/fleet.log
  data/env/<name>.env       each managed service's environment: PORT, DB_PATH,
                            SERVICE_TOKEN, a <PEER>_URL per managed peer,
                            CONDUCTOR_URL derived from the conductor's port, and
                            any key the service's `env:` list declares, copied
                            from the generating shell (the escape hatch that lets
                            notify's GITHUB_TOKEN follow the GitHub call — see
                            SERVICE_CONTRACT rule 4)
  data/tokens/<name>.token  minted ONCE (secrets.token_hex) and then stable —
                            re-running the generator never rotates a token.
                            Also mints fleet-api.token for process-compose's REST API.
  data/fleet_topology.json  every service (managed or not) with kind/port/health/
                            depends_on/ui/public, plus the doors/knobs allowlists
                            the conductor's /internal/bus and /internal/tuning
                            check — what the /svc gateway resolves against and
                            what the Atlas will render (P6).

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

# The conductor's service doors (routes/internal.py). Validated here rather than
# discovered at runtime: a typo in `doors:` would otherwise grant nothing at all
# and show up as a 403 at 3am instead of as a broken registry at generation time.
#
#   bus      POST /internal/bus          put an event on the platform bus
#   tuning   GET  /internal/tuning       read one allowlisted knob of the owner's
#   model    POST /internal/complete     ask for a completion WITHOUT holding a key
#   agents   /internal/agents/…          write and read the activity register
KNOWN_DOORS = ("bus", "tuning", "model", "agents")


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
        for door in svc.get("doors") or []:
            if door not in KNOWN_DOORS:
                raise ValueError(f"service {name!r} asks for door {door!r}; "
                                 f"the conductor has {list(KNOWN_DOORS)}")
        if svc.get("knobs") and "tuning" not in (svc.get("doors") or []):
            raise ValueError(f"service {name!r} lists knobs but not the tuning door")
        for peer in svc.get("peers") or []:
            other = reg["services"].get(peer)
            if not other or other.get("kind") not in MANAGED_KINDS:
                raise ValueError(f"service {name!r} declares peer {peer!r}, "
                                 f"which is not a managed service")
            if peer == name:
                raise ValueError(f"service {name!r} declares itself as a peer")
        if not all(isinstance(k, str) for k in svc.get("env") or []):
            raise ValueError(f"service {name!r} has a non-string env key")
        svc.setdefault("ui", False)
        if not isinstance(svc.get("public", False), bool):
            raise ValueError(f"service {name!r} has a non-boolean public:")
        svc.setdefault("public", False)
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
    # A peer's TOKEN, and only for a peer this service DECLARED. An address is
    # public inside the fleet; the credential that opens it is not, and writing
    # every token into every env file would make "which services can call which"
    # unanswerable. `peers: [knowledge]` is one reviewed line, exactly like
    # `doors:` — the lifeworld service calls the knowledge service directly
    # (asking the conductor to ask would be two hops for one answer), and that
    # is the only such edge in the fleet today.
    for peer in sorted(svc.get("peers") or []):
        lines.append(f"{peer.upper().replace('-', '_')}_TOKEN={_token(root, peer)}")
    # Declared passthrough, and ONLY declared: a service's env is a list somebody
    # reviewed in services.yaml, never "whatever the operator happened to export".
    # An unset key is written empty rather than skipped, so the file's shape does
    # not depend on the shell and re-runs stay byte-identical.
    for key in sorted(svc.get("env") or []):
        lines.append(f"{key}={env.get(key, '')}")
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
            # The conductor's fleet doors read these: which /internal/* doors this
            # service may use, and which tuning knobs it may read through one.
            entry["doors"] = sorted(svc.get("doors") or [])
            entry["knobs"] = sorted(svc.get("knobs") or [])
            # Service→service edges, declared. The Atlas draws them (P6) and the
            # env file above carries exactly these peers' tokens.
            entry["peers"] = sorted(svc.get("peers") or [])
            # The /svc gateway's gate. False by default and written explicitly, so
            # a topology never leaves the answer to whoever reads it.
            entry["public"] = bool(svc.get("public"))
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
