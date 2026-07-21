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
    monkeypatch.setattr(envs, "_load_into_kind", _boom)
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


def test_a_preview_gets_its_own_hostname():
    m = envs.manifests("pr-42", "img:1", "devteam-pr-42")
    assert "host: pr-42." in m


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
