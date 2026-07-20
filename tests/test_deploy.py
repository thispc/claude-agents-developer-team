"""Features: full deployment (runtime detection), k8s manifests (shared ingress,
labels, cost design), cross-arch build config.

Commits: e50c3a0 (full deployment), 33a6d3f + d716e24 (k8s ingress/labels/arch).
These test pure logic — no docker/kubectl needed.
"""

import tempfile
from pathlib import Path

import pytest
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
