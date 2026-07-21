"""Features: full deployment (runtime detection), k8s manifests (shared ingress,
labels, cost design), cross-arch build config.

Commits: e50c3a0 (full deployment), 33a6d3f + d716e24 (k8s ingress/labels/arch).
These test pure logic — no docker/kubectl needed.
"""

import tempfile
from pathlib import Path

import pytest

from conftest import make_project, make_task
import yaml

from app import config, deploy


def _mkrepo(files: dict) -> Path:
    d = Path(tempfile.mkdtemp(prefix="deploy-test-"))
    for name, content in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


# ---- runtime detection -----------------------------------------------------

def test_detect_node_start_script():
    r = _mkrepo({"package.json": '{"scripts": {"start": "node server.js"}}',
                 "server.js": "..."})
    assert deploy.detect(r)["kind"] == "node"


def test_detect_node_server_entrypoint_without_start():
    r = _mkrepo({"package.json": '{"name": "x"}', "server.js": "..."})
    assert deploy.detect(r)["kind"] == "node"


def test_detect_python_fastapi():
    r = _mkrepo({"requirements.txt": "fastapi\nuvicorn", "main.py": "app = ..."})
    spec = deploy.detect(r)
    assert spec["kind"] == "python"
    assert "uvicorn" in spec["run"]


def test_detect_dockerfile_wins():
    r = _mkrepo({"Dockerfile": "FROM x", "package.json": '{"scripts":{"start":"x"}}'})
    assert deploy.detect(r)["kind"] == "docker"


def test_detect_static_only():
    r = _mkrepo({"index.html": "<html></html>"})
    assert deploy.detect(r)["kind"] == "static"


def test_detect_unknown():
    r = _mkrepo({"README.md": "hi"})
    assert deploy.detect(r)["kind"] == "unknown"


# ---- k8s manifests: the cost design ---------------------------------------

def test_manifests_use_shared_ingress_when_domain_set(monkeypatch):
    monkeypatch.setattr(config, "APPS_DOMAIN", "apps.example.com")
    docs = list(yaml.safe_load_all(deploy.manifests(7, "reg/img:1")))
    kinds = {d["kind"] for d in docs}
    assert kinds == {"Deployment", "Service", "Ingress"}
    svc = next(d for d in docs if d["kind"] == "Service")
    assert svc["spec"]["type"] == "ClusterIP"     # NOT a per-app LoadBalancer
    ing = next(d for d in docs if d["kind"] == "Ingress")
    assert ing["spec"]["rules"][0]["host"] == "app-7.apps.example.com"


def test_manifests_fall_back_to_loadbalancer_without_domain(monkeypatch):
    monkeypatch.setattr(config, "APPS_DOMAIN", "")
    docs = list(yaml.safe_load_all(deploy.manifests(7, "reg/img:1", use_ingress=False)))
    svc = next(d for d in docs if d["kind"] == "Service")
    assert svc["spec"]["type"] == "LoadBalancer"


def test_all_manifests_carry_project_label_for_teardown(monkeypatch):
    """deploy.stop() deletes by label — every object must carry it or it leaks."""
    monkeypatch.setattr(config, "APPS_DOMAIN", "apps.example.com")
    for d in yaml.safe_load_all(deploy.manifests(7, "reg/img:1")):
        labels = (d.get("metadata", {}).get("labels") or {})
        assert labels.get("devteam/project") == "7", f"{d['kind']} missing label"


def test_deploy_platform_defaults_to_amd64():
    # cloud nodes are amd64; a Mac would otherwise ship an unusable arm64 image
    assert config.DEPLOY_PLATFORM == "linux/amd64"


def test_generated_dockerfile_matches_runtime():
    node = deploy._generated_dockerfile({"kind": "node", "run": "npm start"})
    assert "node:" in node and "npm" in node
    py = deploy._generated_dockerfile({"kind": "python", "run": "python -m uvicorn main:app --port $PORT"})
    assert "python:" in py


# ---- child env is scrubbed of platform secrets ----------------------------

def test_local_child_env_has_no_platform_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    monkeypatch.setenv("WORKER_TOKEN", "wt-secret")
    env = deploy._child_env(8600)
    for leak in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "WORKER_TOKEN",
                 "CLAUDE_CODE_OAUTH_TOKEN"):
        assert leak not in env, f"deployed app can read {leak}"
    assert env["PORT"] == "8600"


# ---- what the platform learned about itself, applied to project apps ------
# deploy.py had no canary, no rollback, no history and no source choice, while
# the platform side grew all four. That gap was the bug.

def test_a_project_deploy_can_be_rolled_back(fresh_db, tmp_path, monkeypatch):
    """A project deploy could only go forwards: if it broke the app, the only
    move was to fix it and deploy again, with the app down in between."""
    import asyncio
    from app import deploy
    p = make_project(owner_id=1)
    monkeypatch.setattr(deploy, "_history_file", lambda pid: tmp_path / f"h{pid}.json")
    deploy._remember(p, {"ok": True, "workspace": "task-1-a1", "source": "task-1-a1"})
    deploy._remember(p, {"ok": True, "workspace": "task-2-a1", "source": "task-2-a1"})

    called = {}

    async def fake_deploy(pid, workspace=""):
        called["workspace"] = workspace
        return {"ok": True}
    monkeypatch.setattr(deploy, "deploy_local", fake_deploy)
    r = asyncio.run(deploy.rollback(p))
    assert r["ok"] and called["workspace"] == "task-1-a1", "went back to the wrong one"


def test_rollback_refuses_when_there_is_nothing_to_return_to(fresh_db, tmp_path,
                                                             monkeypatch):
    import asyncio
    from app import deploy
    p = make_project(owner_id=1)
    monkeypatch.setattr(deploy, "_history_file", lambda pid: tmp_path / "h.json")
    deploy._remember(p, {"ok": True, "workspace": "only-one"})
    r = asyncio.run(deploy.rollback(p))
    assert r["ok"] is False and "no earlier healthy deploy" in r["error"]


def test_failures_are_recorded_too(fresh_db, tmp_path, monkeypatch):
    """A history of only successes cannot tell you that three attempts from one
    branch all died the same way."""
    from app import deploy
    p = make_project(owner_id=1)
    monkeypatch.setattr(deploy, "_history_file", lambda pid: tmp_path / "h.json")
    deploy._remember(p, {"ok": False, "workspace": "task-9-a1", "error": "port in use"})
    h = deploy.history(p)
    assert h and h[0]["ok"] is False and "port in use" in h[0]["error"]


def test_rollback_only_considers_healthy_deploys(fresh_db, tmp_path, monkeypatch):
    import asyncio
    from app import deploy
    p = make_project(owner_id=1)
    monkeypatch.setattr(deploy, "_history_file", lambda pid: tmp_path / "h.json")
    deploy._remember(p, {"ok": True, "workspace": "good-old"})
    deploy._remember(p, {"ok": False, "workspace": "broken"})
    deploy._remember(p, {"ok": True, "workspace": "good-new"})
    picked = {}

    async def fake_deploy(pid, workspace=""):
        picked["w"] = workspace
        return {"ok": True}
    monkeypatch.setattr(deploy, "deploy_local", fake_deploy)
    asyncio.run(deploy.rollback(p))
    assert picked["w"] == "good-old", "rolled back to a deploy that had failed"


def test_history_is_bounded(fresh_db, tmp_path, monkeypatch):
    from app import deploy
    p = make_project(owner_id=1)
    monkeypatch.setattr(deploy, "_history_file", lambda pid: tmp_path / "h.json")
    for i in range(25):
        deploy._remember(p, {"ok": True, "workspace": f"w{i}"})
    assert len(deploy.history(p)) <= 10
