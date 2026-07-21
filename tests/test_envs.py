"""Environments: build once, try it, promote the same artifact.

Production runs from a mutable git checkout today, which is why "the app is
half-updated" is even possible and why what you tested and what you shipped can
be different builds. These tests pin the properties that fix that — most of them
are about refusing to do the wrong thing, since the wrong thing here overwrites
a running platform.
"""

from pathlib import Path

import subprocess

import pytest

# These read files from the repository (manifests, workflows) or drive host
# tooling like git and docker. Neither exists inside the shipped image, so they
# verify the REPO rather than the ARTIFACT — staging deselects them.
pytestmark = pytest.mark.hostonly

from conftest import make_project, make_task
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


def _fake_kubectl(monkeypatch, calls, images=None):
    """A kubectl that answers the two `get` probes and records everything."""
    images = {"devteam-conductor": "devteam-conductor:old"} if images is None else images

    def run(*cmd, **kw):
        calls.append(cmd)
        out = ""
        if "get" in cmd:
            dep = cmd[cmd.index("deployment") + 1]
            if dep in images:
                out = images[dep] if "image" in cmd[-1] else "conductor"
        return subprocess.CompletedProcess(cmd, 0, out, "")
    monkeypatch.setattr(envs, "kubectl_ok", lambda: True)
    monkeypatch.setattr(envs, "_publish", lambda tag: (True, "published"))
    monkeypatch.setattr(envs, "_state", lambda: {"images": [{"tag": "img:1"}], "envs": {}})
    monkeypatch.setattr(envs, "_save", lambda st: None)
    monkeypatch.setattr(envs, "_sh", run)


def test_promotion_names_the_deployment_it_is_changing(monkeypatch):
    """One app in the namespace made "the deployment" unambiguous. A second one
    makes it a guess, and a guess here points production at someone else's image."""
    calls: list[tuple] = []
    _fake_kubectl(monkeypatch, calls, {"devteam-api": "devteam-api:old"})
    r = envs.promote("img:1", deployment="devteam-api")
    assert r["ok"] is True and r["deployment"] == "devteam-api"
    setimg = next(c for c in calls if "set" in c and "image" in c)
    assert "deployment/devteam-api" in setimg


def test_promoting_into_a_deployment_that_is_not_there_fails_loudly(monkeypatch):
    """A promote that quietly does nothing is worse than one that errors: it
    reports the new image as live while production serves the old one."""
    calls: list[tuple] = []
    _fake_kubectl(monkeypatch, calls, {})          # nothing in the namespace
    r = envs.promote("img:1", deployment="devteam-typo")
    assert r["ok"] is False and "devteam-typo" in r["error"]
    assert not any("set" in c and "image" in c for c in calls), \
        "it changed something after failing to find the target"


def test_the_target_is_named_and_never_selected(monkeypatch):
    """Verified against the live cluster: `kubectl set image deployment/<name>`
    exits 1 for a Deployment that is not there, but `--all` or a label selector
    matching nothing exits 0 and prints nothing at all — success, no rollout."""
    src = (REPO / "conductor" / "app" / "envs.py").read_text()
    body = "\n".join(l for l in src.split("def promote(")[1].split("\ndef ")[0].splitlines()
                     if not l.strip().startswith("#"))
    assert "--all" not in body and '"-l"' not in body


def test_a_deployment_name_kubectl_would_read_as_something_else_is_refused(monkeypatch):
    """argv leaves no shell to inject into, but a leading '-' is still parsed as an
    option and a '/' changes which resource TYPE is addressed."""
    monkeypatch.setattr(envs, "kubectl_ok", lambda: True)
    monkeypatch.setattr(envs, "_state", lambda: {"images": [{"tag": "img:1"}], "envs": {}})

    def _boom(*a, **k):
        raise AssertionError("reached the cluster with a name it should have refused")
    monkeypatch.setattr(envs, "_sh", _boom)
    for bad in ("--all", "-l app=x", "statefulset/devteam-conductor", "   "):
        assert envs.promote("img:1", deployment=bad)["ok"] is False, bad
        assert envs.rollback(deployment=bad)["ok"] is False, bad


def test_promotion_still_defaults_to_production(monkeypatch):
    """Explicit does not mean mandatory — the API and the dashboard call this with
    a tag alone, and that must keep meaning the production deployment."""
    calls: list[tuple] = []
    _fake_kubectl(monkeypatch, calls)
    monkeypatch.setattr(envs, "PROD_DEPLOYMENT", "devteam-conductor")
    assert envs.promote("img:1")["deployment"] == "devteam-conductor"


def test_rollback_undoes_the_deployment_it_was_asked_about(monkeypatch):
    calls: list[tuple] = []
    _fake_kubectl(monkeypatch, calls)
    envs.rollback(deployment="devteam-api")
    assert any("deployment/devteam-api" in c for c in calls)


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

def test_an_environment_is_never_put_on_the_internet(monkeypatch):
    """DOKS reconciles k8s-public-access-<cluster> from the NodePort services it
    finds and opens them to 0.0.0.0/0 — so a NodePort makes exposure the DEFAULT
    and any restriction something you hold against the platform's own controller.
    Verified live: a fresh NodePort service was world-reachable within seconds."""
    for kind in (True, False):
        monkeypatch.setattr(envs, "on_kind", lambda k=kind: k)
        m = envs.manifests("pr-1", "img:1", "devteam-pr-1")
        assert "type: ClusterIP" in m
        assert "NodePort" not in m
        assert "LoadBalancer" not in m, "every LB is billed, per environment"


def test_no_loadbalancer_without_a_domain(monkeypatch):
    monkeypatch.setattr(envs, "on_kind", lambda: False)
    monkeypatch.setattr(config, "APPS_DOMAIN", "")
    assert "Ingress" not in envs.manifests("pr-1", "img:1", "devteam-pr-1")


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


# ---- self-repair when there is no git checkout ---------------------------

def test_a_container_is_detected_by_its_service_account(monkeypatch, tmp_path):
    """selfops.redeploy() git-pulls the tree it runs from. In a pod there is no
    tree, no .git, no docker and no kubectl — so the last step of self-repair,
    the one that actually ships the fix, has nowhere to happen."""
    from app import cloud
    monkeypatch.setattr(cloud, "SA_DIR", tmp_path)
    assert cloud.in_cluster() is False
    (tmp_path / "token").write_text("x")
    assert cloud.in_cluster() is True


def test_a_restart_is_refused_while_agents_are_working(fresh_db, monkeypatch):
    """Patching the Deployment kills this pod, and every worker is a child process
    of it — an agent 80 turns into a task loses all of it."""
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "current_image", lambda: "img:1")
    p = make_project(owner_id=1)
    make_task(p, status="running")
    r = cloud.self_update("img:2")
    assert r["ok"] is False and r["busy"]


def test_an_idle_instance_may_update_itself(fresh_db, monkeypatch):
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "current_image", lambda: "img:1")
    make_task(make_project(owner_id=1), status="done")
    assert cloud.can_self_update()["ok"] is True


def test_it_refuses_to_ship_the_image_it_is_already_running(monkeypatch):
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "current_image", lambda: "img:1")
    monkeypatch.setattr(cloud, "busy", lambda: [])
    assert cloud.self_update("img:1")["ok"] is False


def test_it_refuses_something_that_is_not_an_image(monkeypatch):
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "current_image", lambda: "img:1")
    monkeypatch.setattr(cloud, "busy", lambda: [])
    assert cloud.self_update("main")["ok"] is False


def test_missing_rbac_is_explained_rather_than_just_failing(monkeypatch):
    """'Forbidden' at the last step of self-repair reads like the feature is
    broken. Name the actual cause."""
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "current_image", lambda: "")
    r = cloud.can_self_update()
    assert r["ok"] is False
    assert "ServiceAccount" in " ".join(r["reasons"])


def test_the_pod_is_given_a_service_account():
    """Without it the pod runs as 'default', which cannot patch anything."""
    y = (REPO / "deploy" / "k8s" / "doks-production.yaml").read_text()
    assert "serviceAccountName: devteam-conductor" in y


def test_ci_builds_but_does_not_roll_out():
    """A rollout from CI would kill an agent mid-task; only the conductor knows
    whether anything is in flight."""
    wf = (REPO / ".github" / "workflows" / "build-image.yml").read_text()
    assert "docker push" in wf
    assert "kubectl" not in wf and "set image" not in wf


def test_the_ui_offers_self_update_in_a_cluster_not_a_build_button():
    """In a pod there is no Docker daemon, so "Environments need Docker" would be
    both true and useless — the cloud equivalent is choosing when to take an image
    CI already built."""
    js = (REPO / "dashboard" / "app.js").read_text()
    assert "renderCloudInstance" in js
    assert "/api/self/instance" in js and "/api/self/update" in js
    block = js.split("function renderCloudInstance(", 1)[1][:2000]
    assert "busy" in block, "it must say when agents are working"


def test_a_missing_tool_is_a_failed_run_not_a_crash(monkeypatch):
    """The conductor's container has no docker and no kubectl on purpose.
    subprocess.run raises rather than returning non-zero for a missing binary,
    which turned every Environments call into a 500 in the cluster — the page
    said the server was broken when the honest answer is "that tool isn't here"."""
    r = envs._sh("definitely-not-a-real-binary-xyz", "--version")
    assert r.returncode != 0 and "not" in (r.stderr or "").lower()


def test_overview_survives_a_container_with_no_tooling(monkeypatch):
    monkeypatch.setattr(envs, "_sh",
                        lambda *c, **k: subprocess.CompletedProcess(c, 127, "", "missing"))
    d = envs.overview()
    assert d["docker"] is False and d["kubernetes"] is False


# ---- noticing a new version without being told ---------------------------

@pytest.mark.asyncio
async def test_it_defers_an_update_while_agents_are_working(fresh_db, monkeypatch):
    """A restart kills every worker — each is a child process of the conductor —
    so an agent eighty turns into a task loses all of it. Waiting costs minutes;
    not waiting costs the work."""
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "AUTO_UPDATE", True)

    async def newer():
        return {"tag": "reg/devteam-conductor:new", "short": "new"}
    monkeypatch.setattr(cloud, "newer_than_running", newer)

    def _boom(*a, **k):
        raise AssertionError("updated while an agent was mid-task")
    monkeypatch.setattr(cloud, "self_update", _boom)

    p = make_project(owner_id=1)
    make_task(p, status="running")
    r = await cloud.check_and_maybe_update()
    assert r["deferred"] is True and r["busy"]


@pytest.mark.asyncio
async def test_it_adopts_a_new_version_when_idle(fresh_db, monkeypatch):
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "AUTO_UPDATE", True)
    applied = {}

    async def newer():
        return {"tag": "reg/devteam-conductor:new", "short": "new"}
    monkeypatch.setattr(cloud, "newer_than_running", newer)
    def _apply(tag, force=False):
        applied["tag"] = tag
        return {"ok": True}
    monkeypatch.setattr(cloud, "self_update", _apply)
    make_task(make_project(owner_id=1), status="done")
    r = await cloud.check_and_maybe_update()
    assert applied["tag"].endswith(":new") and r["applied"]["ok"]


@pytest.mark.asyncio
async def test_auto_update_off_means_it_only_offers(fresh_db, monkeypatch):
    """Adopting a new version restarts the platform. That should be opt-in."""
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "AUTO_UPDATE", False)

    async def newer():
        return {"tag": "reg/x:new", "short": "new"}
    monkeypatch.setattr(cloud, "newer_than_running", newer)

    def _boom(*a, **k):
        raise AssertionError("updated itself without being asked")
    monkeypatch.setattr(cloud, "self_update", _boom)
    r = await cloud.check_and_maybe_update()
    assert r["deferred"] is True and "AUTO_UPDATE" in r["reason"]


@pytest.mark.asyncio
async def test_a_registry_blip_never_takes_the_conductor_down(monkeypatch):
    from app import cloud
    monkeypatch.setattr(cloud, "REGISTRY", "registry.digitalocean.com/x")
    monkeypatch.setenv("DIGITALOCEAN_API_TOKEN", "bad")
    assert await cloud.available_images() == []


def test_the_watcher_starts_only_in_a_cluster():
    src = (REPO / "conductor" / "app" / "main.py").read_text()
    assert "cloud.watch" in src and "cloud.in_cluster()" in src
