"""P0 rails: services.yaml → gen_fleet outputs, defect-1 (CONDUCTOR_URL), the /svc gateway.

Everything here is offline. The generator runs against a temp root; the gateway
tests drive the ASGI app in-process; the one subprocess evaluates the ACTUAL
export line from run-local.sh, because that line is where defect 1 lived.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_project, make_task

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import gen_fleet  # noqa: E402


# --- services.yaml: the registry parses and says what the plan locked ---------

def test_the_registry_parses_and_the_ports_are_the_locked_ones():
    reg = gen_fleet.load_registry(REPO / "services.yaml")
    assert reg["fleet_api"]["port"] == 8899
    svcs = reg["services"]
    assert svcs["conductor"]["kind"] == "core" and svcs["conductor"]["port"] == 8787
    assert svcs["conductor"]["accepts"] == ["worker", "verify"]
    assert svcs["worker-pool"] == {"kind": "ephemeral", "managed_by": "launcher", "ui": False}
    assert svcs["sandbox"]["port_range"] == [8500, 8599]
    assert svcs["apps"]["port_range"] == [8600, 8799]
    # the defect-2 fix holds structurally: the two external ranges are disjoint
    assert svcs["sandbox"]["port_range"][1] < svcs["apps"]["port_range"][0]


def test_a_managed_service_without_a_port_is_refused():
    import yaml
    broken = {"services": {"x": {"kind": "service", "cmd": "true"}}}
    p = Path(gen_fleet.__file__).parent  # anywhere; we only need a file to parse
    tmp = p / "_broken_test.yaml"
    tmp.write_text(yaml.safe_dump(broken))
    try:
        with pytest.raises(ValueError):
            gen_fleet.load_registry(tmp)
    finally:
        tmp.unlink()


# --- generation: deterministic, stable tokens, derived URLs -------------------

@pytest.fixture()
def fleet_root(tmp_path):
    shutil.copy(REPO / "services.yaml", tmp_path / "services.yaml")
    return tmp_path


def _read_all(root: Path) -> dict[str, str]:
    return {rel: (root / rel).read_text()
            for rel in ("process-compose.yaml", "data/env/conductor.env",
                        "data/fleet_topology.json")}


def test_generation_is_deterministic_so_reruns_never_churn(fleet_root):
    env = {}
    gen_fleet.generate(fleet_root, env)
    first = _read_all(fleet_root)
    states = gen_fleet.generate(fleet_root, env)
    assert _read_all(fleet_root) == first
    assert set(states.values()) == {"unchanged"}, states


def test_tokens_are_minted_once_and_never_rotate(fleet_root):
    gen_fleet.generate(fleet_root, {})
    tok = (fleet_root / "data/tokens/conductor.token").read_text()
    assert len(tok.strip()) == 64                      # token_hex(32)
    gen_fleet.generate(fleet_root, {})
    assert (fleet_root / "data/tokens/conductor.token").read_text() == tok
    assert (fleet_root / "data/tokens/fleet-api.token").exists()


def test_conductor_url_is_derived_from_the_registered_port(fleet_root):
    gen_fleet.generate(fleet_root, {})
    env_file = (fleet_root / "data/env/conductor.env").read_text()
    assert "CONDUCTOR_URL=http://127.0.0.1:8787" in env_file
    assert "PORT=8787" in env_file
    assert "SERVICE_TOKEN=" in env_file and "DB_PATH=devteam.db" in env_file


def test_a_port_override_flows_everywhere_not_just_somewhere(fleet_root):
    """PORT=9001 must move the bind, the advertised URL and the probe together —
    half-applied is exactly the shape defect 1 had."""
    gen_fleet.generate(fleet_root, {"PORT": "9001"})
    assert "CONDUCTOR_URL=http://127.0.0.1:9001" in (fleet_root / "data/env/conductor.env").read_text()
    compose = (fleet_root / "process-compose.yaml").read_text()
    assert "--port 9001" in compose and "port: 9001" in compose
    topo = json.loads((fleet_root / "data/fleet_topology.json").read_text())
    assert topo["services"]["conductor"]["port"] == 9001


def test_the_compose_file_probes_health_and_restarts_on_failure(fleet_root):
    import yaml
    gen_fleet.generate(fleet_root, {})
    compose = yaml.safe_load((fleet_root / "process-compose.yaml").read_text())
    assert compose["log_location"] == "data/logs/fleet.log"
    proc = compose["processes"]["conductor"]
    assert proc["readiness_probe"]["http_get"]["path"] == "/api/health"
    assert proc["readiness_probe"]["http_get"]["port"] == 8787
    assert proc["availability"]["restart"] == "on_failure"
    assert proc["availability"]["backoff_seconds"] >= 1
    assert "data/env/conductor.env" in proc["command"]


def test_the_topology_names_the_unmanaged_world_too(fleet_root):
    gen_fleet.generate(fleet_root, {})
    topo = json.loads((fleet_root / "data/fleet_topology.json").read_text())
    svcs = topo["services"]
    assert svcs["conductor"]["managed"] is True and svcs["conductor"]["health"] == "/api/health"
    assert svcs["sandbox"] == {"kind": "external", "managed": False, "ui": False,
                               "managed_by": "sandbox", "port_range": [8500, 8599]}
    assert svcs["apps"]["port_range"] == [8600, 8799]
    assert svcs["worker-pool"]["managed_by"] == "launcher"
    assert topo["fleet_api"]["port"] == 8899
    assert all("ui" in s for s in svcs.values())


# --- defect 1: the regression chain, link by link -----------------------------

@pytest.mark.hostonly
def test_run_local_exports_conductor_url_from_the_port_it_binds():
    """The subprocess evaluates the ACTUAL line from run-local.sh, so if someone
    edits the script back to the broken shape, this fails on the script — not on
    a copy of it embedded in a test."""
    script = (REPO / "run-local.sh").read_text()
    lines = [l for l in script.splitlines() if l.startswith("export CONDUCTOR_URL=")]
    assert lines, "run-local.sh no longer exports CONDUCTOR_URL"
    r = subprocess.run(["zsh", "-c", f'PORT=8917; {lines[0]}; print -rn -- "$CONDUCTOR_URL"'],
                       capture_output=True, text=True, timeout=15)
    assert r.stdout == "http://127.0.0.1:8917", r.stdout
    # and the export happens before EITHER exec, so both boot paths get it
    export_at = script.index("export CONDUCTOR_URL")
    assert export_at < script.index("exec .venv/bin/uvicorn")
    assert export_at < script.index("exec process-compose")


@pytest.mark.hostonly
def test_config_reads_that_env_and_would_stop_defaulting_to_8000():
    code = ("import os; os.environ['CONDUCTOR_URL']='http://127.0.0.1:8917'; "
            "import sys; sys.path.insert(0, 'conductor'); "
            "from app import config; print(config.CONDUCTOR_URL)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=REPO, timeout=60)
    assert r.stdout.strip() == "http://127.0.0.1:8917", r.stderr[-500:]


def test_the_launcher_hands_workers_configs_conductor_url(fresh_db, monkeypatch):
    """The last link: whatever config says IS what a worker child gets."""
    from app import config, launcher
    monkeypatch.setattr(config, "CONDUCTOR_URL", "http://127.0.0.1:8917")
    pid = make_project()
    tid = make_task(pid)
    from app import db as _db
    env = launcher._worker_env(_db.get_task(tid), _db.get_project(pid), "claude-haiku-4-5")
    assert env["CONDUCTOR_URL"] == "http://127.0.0.1:8917"


# --- the /svc gateway: resolver + auth gate -----------------------------------

def test_svc_requires_a_signed_in_user_before_anything_resolves(client):
    r = client.get("/svc/anything/at/all")
    assert r.status_code == 401


def test_svc_404s_unknown_external_and_ephemeral_names(root_client):
    for name in ("nope", "sandbox", "apps", "worker-pool"):
        r = root_client.get(f"/svc/{name}/health")
        assert r.status_code == 404, f"/svc/{name} answered {r.status_code}"


def test_svc_excludes_the_conductor_itself(root_client):
    assert root_client.get("/svc/conductor/api/health").status_code == 404


def test_the_resolver_reads_topology_and_token(tmp_path, monkeypatch):
    from app.routes import svc
    (tmp_path / "tokens").mkdir(parents=True)
    (tmp_path / "tokens" / "knowledge.token").write_text("tok-123\n")
    (tmp_path / "fleet_topology.json").write_text(json.dumps({
        "services": {"knowledge": {"kind": "service", "managed": True,
                                   "url": "http://127.0.0.1:8881"},
                     "sandbox": {"kind": "external", "managed": False}}}))
    monkeypatch.setattr(svc, "_DATA", tmp_path)
    assert svc.resolve("knowledge") == {"url": "http://127.0.0.1:8881", "token": "tok-123"}
    assert svc.resolve("sandbox") is None
    assert svc.resolve("conductor") is None
    assert svc.resolve("ghost") is None


def test_a_resolved_but_dead_service_is_a_502_not_a_hang(root_client, tmp_path, monkeypatch):
    """Offline-honest: 127.0.0.1:9 refuses instantly — no server, no network."""
    from app.routes import svc
    (tmp_path / "fleet_topology.json").write_text(json.dumps({
        "services": {"knowledge": {"kind": "service", "managed": True,
                                   "url": "http://127.0.0.1:9"}}}))
    monkeypatch.setattr(svc, "_DATA", tmp_path)
    r = root_client.get("/svc/knowledge/health")
    assert r.status_code == 502
    assert "knowledge" in r.text
