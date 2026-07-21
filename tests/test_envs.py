"""Environments: build once, try it, promote the same artifact.

Production runs from a mutable git checkout today, which is why "the app is
half-updated" is even possible and why what you tested and what you shipped can
be different builds. These tests pin the properties that fix that — most of them
are about refusing to do the wrong thing, since the wrong thing here overwrites
a running platform.
"""

from pathlib import Path

import pytest

from app import config, envs

REPO = Path(__file__).resolve().parent.parent


# ---- naming: k8s and image tags are both fussy ---------------------------

def test_env_names_become_valid_dns_labels():
    assert envs._safe("feat/Sprints Verification!") == "feat-sprints-verification"
    assert envs._safe("---") == "env"
    assert len(envs._safe("x" * 200)) <= 40


# ---- the artifact is identified by CONTENT, not by branch ----------------

def test_two_builds_of_the_same_branch_are_different_artifacts(tmp_path):
    """Uncommitted work is a legal source, so a branch name is not an identity —
    two builds of `live` an hour apart are different software, and sharing a tag
    would make "promote the one I tested" a guess."""
    a = tmp_path / "a"
    (a / "conductor").mkdir(parents=True)
    (a / "conductor" / "x.py").write_text("v1")
    first = envs._tree_hash(a)
    (a / "conductor" / "x.py").write_text("v2")
    assert envs._tree_hash(a) != first


def test_the_hash_ignores_what_is_never_shipped(tmp_path):
    a = tmp_path / "a"
    (a / "conductor").mkdir(parents=True)
    (a / "conductor" / "x.py").write_text("v1")
    before = envs._tree_hash(a)
    (a / "node_modules").mkdir()
    (a / "node_modules" / "junk.js").write_text("noise")
    assert envs._tree_hash(a) == before


# ---- refusals: this code can overwrite a running platform ----------------

def test_a_preview_may_never_target_production(monkeypatch):
    """The first guard compared the DERIVED namespace against production, which is
    always prefixed and so could never match — a blank name sailed past it and
    really deployed an environment called "env"."""
    monkeypatch.setattr(envs, "PROD_NAMESPACE", "devteam")

    def _boom(*a, **k):
        raise AssertionError("refused too late — the cluster was already touched")
    monkeypatch.setattr(envs, "kubectl_ok", lambda: True)
    monkeypatch.setattr(envs, "_publish", _boom)
    for bad in ("", "   ", "devteam", "production", "PROD", "main"):
        r = envs.deploy_preview("devteam-conductor:x", bad)
        assert r["ok"] is False, f"{bad!r} was accepted"


def test_destroy_refuses_production(monkeypatch):
    monkeypatch.setattr(envs, "PROD_NAMESPACE", "devteam")
    for bad in ("", "devteam", "production", "main"):
        assert envs.destroy(bad)["ok"] is False, bad


def test_only_an_image_this_platform_built_can_be_promoted(monkeypatch):
    """Promotion points production at a tag. Accepting an arbitrary string would
    let a typo — or anything that can reach the API — run unknown code as the
    platform."""
    monkeypatch.setattr(envs, "kubectl_ok", lambda: True)
    monkeypatch.setattr(envs, "_state", lambda: {"images": [], "envs": {}})
    r = envs.promote("evil/image:latest")
    assert r["ok"] is False and "did not build" in r["error"]


def test_a_hostile_source_is_refused(monkeypatch):
    monkeypatch.setattr(envs, "docker_ok", lambda: True)
    for bad in ("ref:a b", "ref:../../etc", "workspace:../../etc", "nonsense:x"):
        assert envs.build(bad)["ok"] is False, bad


def test_build_says_so_when_docker_is_down(monkeypatch):
    monkeypatch.setattr(envs, "docker_ok", lambda: False)
    r = envs.build("live")
    assert r["ok"] is False and "Docker" in r["error"]


# ---- the manifest is what makes an environment isolated ------------------

def test_each_environment_gets_its_own_database():
    """Sharing production's volume would let a bad migration destroy real
    projects — and a destructive migration is exactly what a preview is for."""
    m = envs.manifests("pr-42", "img:1", "devteam-pr-42")
    assert "PersistentVolumeClaim" in m
    assert "namespace: devteam-pr-42" in m
    assert "/data/devteam.db" in m


def test_a_preview_runs_in_demo_mode_with_no_secrets():
    m = envs.manifests("pr-42", "img:1", "devteam-pr-42")
    assert 'name: DEMO_MODE, value: "1"' in m
    assert "devteam-secrets" not in m, "a preview must not mount production secrets"


def test_a_preview_gets_its_own_hostname_when_there_is_a_domain(monkeypatch):
    """Only meaningful with a wildcard domain to hang it off. Without one there is
    no Ingress at all, because an Ingress with nowhere to point costs a
    LoadBalancer and buys nothing."""
    monkeypatch.setattr(config, "APPS_DOMAIN", "apps.example.com")
    m = envs.manifests("pr-42", "img:1", "devteam-pr-42")
    assert "host: pr-42.apps.example.com" in m and "Ingress" in m


def test_no_domain_means_no_ingress(monkeypatch):
    monkeypatch.setattr(envs, "on_kind", lambda: False)
    monkeypatch.setattr(config, "APPS_DOMAIN", "")
    assert "Ingress" not in envs.manifests("pr-42", "img:1", "devteam-pr-42")


def test_the_manifest_pins_the_exact_image():
    m = envs.manifests("pr-42", "devteam-conductor:live-abc123", "devteam-pr-42")
    assert "image: devteam-conductor:live-abc123" in m
    assert ":latest" not in m, "a floating tag defeats the point of an artifact"


# ---- promotion ships the tested bytes ------------------------------------

def test_promote_does_not_rebuild(monkeypatch):
    """The whole point: the bytes that served the preview are the bytes that
    serve production. A rebuild would reintroduce the drift this replaces."""
    src = (REPO / "conductor" / "app" / "envs.py").read_text()
    body = src.split("def promote(")[1].split("\ndef ")[0]
    assert "docker build" not in body and "git pull" not in body
    assert '"set", "image"' in body      # kubectl set image, as separate argv


def test_a_failed_rollout_is_undone(monkeypatch):
    """Half a rollout on the platform that runs your team is worse than none."""
    src = (REPO / "conductor" / "app" / "envs.py").read_text()
    body = src.split("def promote(")[1].split("\ndef ")[0]
    assert "rollout" in body and "undo" in body


def test_overview_reports_what_is_actually_running():
    d = envs.overview()
    for k in ("docker", "kubernetes", "images", "envs", "prod_namespace"):
        assert k in d


# ---- kind and a real cluster need different things ------------------------

def test_a_real_cluster_needs_a_registry(monkeypatch):
    """Nodes on DOKS pull from a registry; they cannot see the host's Docker
    daemon. Getting this wrong is the classic ImagePullBackOff with no message."""
    monkeypatch.setattr(envs, "on_kind", lambda: False)
    monkeypatch.setattr(envs, "REGISTRY", "")
    ok, note = envs._publish("devteam-conductor:x")
    assert ok is False and "DOCR_REGISTRY" in note


def test_kind_loads_rather_than_pushes(monkeypatch):
    calls = []
    monkeypatch.setattr(envs, "on_kind", lambda: True)
    monkeypatch.setattr(envs, "_sh",
                        lambda *c, **k: calls.append(c) or type("R", (), {
                            "returncode": 0, "stdout": "", "stderr": ""})())
    envs._publish("devteam-conductor:x")
    assert calls and calls[0][0] == "kind", "pushed to a registry that kind cannot use"


def test_the_cluster_pulls_by_the_registry_name(monkeypatch):
    """A local tag means nothing to a DOKS node — the manifest must name the
    registry path, not the name it happens to have on this laptop."""
    monkeypatch.setattr(envs, "on_kind", lambda: False)
    monkeypatch.setattr(envs, "REGISTRY", "registry.digitalocean.com/pulkit")
    assert envs.cluster_tag("devteam-conductor:abc") == \
        "registry.digitalocean.com/pulkit/devteam-conductor:abc"


def test_a_registry_name_is_not_doubled_up(monkeypatch):
    monkeypatch.setattr(envs, "REGISTRY", "registry.digitalocean.com/pulkit")
    already = "registry.digitalocean.com/pulkit/devteam-conductor:abc"
    assert envs.registry_tag(already) == already


def test_on_kind_uses_the_local_tag(monkeypatch):
    monkeypatch.setattr(envs, "on_kind", lambda: True)
    assert envs.cluster_tag("devteam-conductor:abc") == "devteam-conductor:abc"


# ---- a managed cluster is not kind ---------------------------------------

def test_a_managed_cluster_gets_nodeport_not_a_loadbalancer(monkeypatch):
    """Every LoadBalancer is billed, and one per preview is how a credit
    disappears into networking rather than compute."""
    monkeypatch.setattr(envs, "on_kind", lambda: False)
    monkeypatch.setattr(config, "APPS_DOMAIN", "")
    m = envs.manifests("pr-1", "img:1", "devteam-pr-1")
    assert "type: NodePort" in m
    assert "LoadBalancer" not in m
    assert "Ingress" not in m, "an ingress with no wildcard domain buys nothing"


def test_kind_keeps_clusterip_and_ingress(monkeypatch):
    monkeypatch.setattr(envs, "on_kind", lambda: True)
    m = envs.manifests("pr-1", "img:1", "devteam-pr-1")
    assert "type: ClusterIP" in m and "Ingress" in m


def test_a_private_registry_means_the_pod_needs_a_pull_secret(monkeypatch):
    """Without it the pods sit in ImagePullBackOff, which reads as "image not
    found" and sends you hunting for a build problem that isn't there."""
    monkeypatch.setattr(envs, "on_kind", lambda: False)
    monkeypatch.setattr(envs, "REGISTRY", "registry.digitalocean.com/x")
    assert "imagePullSecrets" in envs.manifests("pr-1", "img:1", "devteam-pr-1")


def test_no_pull_secret_when_no_registry(monkeypatch):
    monkeypatch.setattr(envs, "on_kind", lambda: True)
    monkeypatch.setattr(envs, "REGISTRY", "")
    assert "imagePullSecrets" not in envs.manifests("pr-1", "img:1", "devteam-pr-1")


def test_a_real_cluster_cross_builds_for_its_own_architecture():
    """kind reuses the host's arch, so local testing can never surface this: DOKS
    nodes are amd64, a Mac builds arm64, and that image dies on the node with
    "exec format error" — which says nothing about architecture."""
    src = (REPO / "conductor" / "app" / "envs.py").read_text()
    body = src.split("def build(")[1].split("\ndef ")[0]
    assert "buildx" in body and "DEPLOY_PLATFORM" in body
    assert "--push" in body
