"""Features: full deployment (runtime detection), k8s manifests (shared ingress,
labels, cost design), cross-arch build config.

Commits: e50c3a0 (full deployment), 33a6d3f + d716e24 (k8s ingress/labels/arch).
These test pure logic — no docker/kubectl needed.
"""

import socket
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


# ---- port allocation holds the port instead of check-then-close -----------
# The bug: _free_port() used to connect_ex a port, close that probe socket, and
# hand back the number — leaving it unreserved for however long the build then
# took. Two deploys started together could both scan, both see it free, and
# both be handed it. It now binds the port itself and returns the open socket,
# so a concurrent scan's own bind() fails instead of racing.

def test_free_port_returns_a_still_bound_socket():
    port, sock = deploy._free_port()
    try:
        assert deploy.PORT_RANGE_START <= port < deploy.PORT_RANGE_START + 200
        other = socket.socket()
        with pytest.raises(OSError):
            other.bind(("127.0.0.1", port))     # taken — deploy.py is holding it
        other.close()
    finally:
        sock.close()


def test_free_port_will_not_hand_out_a_port_still_held_by_an_earlier_call():
    port1, sock1 = deploy._free_port()
    try:
        port2, sock2 = deploy._free_port()
        try:
            assert port1 != port2, "handed out a port that's still reserved"
        finally:
            sock2.close()
    finally:
        sock1.close()


def test_free_port_is_reusable_once_the_reservation_is_released():
    port1, sock1 = deploy._free_port()
    sock1.close()                               # e.g. deploy_local's build-phase finally
    port2, sock2 = deploy._free_port()
    try:
        assert deploy.PORT_RANGE_START <= port2 < deploy.PORT_RANGE_START + 200
    finally:
        sock2.close()


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


# ---- what the platform had and a user's app did not (G24) -----------------
# The platform's deploy path grew content-hash image tags, a trial run before
# promotion, and rollback from the ReplicaSet history. A user's app had none of
# it: build, apply, hope. rollout.py is that difference, removed.

import asyncio

from app import provider, rollout

from conftest import dashboard_js  # the split dashboard JS, concatenated in load order


def _cluster(monkeypatch, *, previous="", canary_ok=True, promote_ok=True):
    """A whole cluster made of monkeypatches. Nothing here may reach a real one."""
    seen = {"canary": None, "promoted": None, "applied": None, "built": None}

    monkeypatch.setattr(deploy.shutil, "which", lambda _n: "/usr/bin/" + _n)
    monkeypatch.setattr(deploy, "ingress_available", lambda: True)
    monkeypatch.setattr(config, "APPS_DOMAIN", "apps.example.com")
    monkeypatch.setattr(config, "K8S_NAMESPACE", "devteam")
    monkeypatch.setenv("DEPLOY_REGISTRY", "reg.example.com/x")

    def _kubectl(*args, stdin=None):
        if "apply" in args:
            seen["applied"] = stdin
        import subprocess as sp
        out = "kind-devteam" if "current-context" in args else ""
        return sp.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(deploy, "_kubectl", _kubectl)

    def _build(dockerfile, context, tag, **kw):
        seen["built"] = tag
        return {"ok": True, "tag": tag}
    monkeypatch.setattr(rollout, "build_image", _build)

    def _canary(ns, name, image, **kw):
        seen["canary"] = image
        return {"ok": canary_ok, "error": "" if canary_ok else "CrashLoopBackOff: "}
    monkeypatch.setattr(rollout, "canary", _canary)
    monkeypatch.setattr(rollout, "running_image", lambda ns, n: (previous, "app"))

    def _promote(ns, name, image, **kw):
        seen["promoted"] = (name, image)
        return {"ok": promote_ok, "error": "" if promote_ok else "timed out"}
    monkeypatch.setattr(rollout, "promote", _promote)
    monkeypatch.setattr(deploy.bus, "emit", lambda *a, **k: None)
    return seen


def _repo_for(monkeypatch, tmp_path, project_id, files=None):
    root = tmp_path / str(project_id) / "repo"
    root.mkdir(parents=True)
    for name, body in (files or {"Dockerfile": "FROM x", "app.py": "print(1)"}).items():
        (root / name).write_text(body)
    monkeypatch.setattr(deploy, "workdir", lambda pid, branch="": root)
    monkeypatch.setattr(deploy, "_log", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "_history_file", lambda pid: tmp_path / "h.json")

    async def _sync(pid, branch=""):
        return True, "checked out"
    monkeypatch.setattr(deploy, "sync_code", _sync)
    return root


def test_a_users_app_is_run_once_before_it_replaces_the_one_that_works(
        fresh_db, tmp_path, monkeypatch):
    """Applying the manifest used to be the first time the image ran anywhere, so
    a broken build became a broken app with the working one already gone."""
    seen = _cluster(monkeypatch, previous="reg.example.com/x/devteam-app-7:old")
    _repo_for(monkeypatch, tmp_path, 7)
    r = asyncio.run(deploy.deploy_k8s(7))
    assert r["ok"] is True and r["verified"] is True
    assert seen["canary"] == seen["built"], "it promoted an image it had not tried"
    assert seen["promoted"][1] == seen["built"]


def test_an_image_that_fails_its_trial_run_never_reaches_the_app(
        fresh_db, tmp_path, monkeypatch):
    seen = _cluster(monkeypatch, previous="reg.example.com/x/devteam-app-7:old",
                    canary_ok=False)
    _repo_for(monkeypatch, tmp_path, 7)
    r = asyncio.run(deploy.deploy_k8s(7))
    assert r["ok"] is False and "trial run" in r["error"]
    assert seen["promoted"] is None and seen["applied"] is None, \
        "the running app was replaced by an image that could not start"


def test_a_failed_trial_is_remembered_like_any_other_failure(
        fresh_db, tmp_path, monkeypatch):
    """A history of only successes cannot tell you three attempts from one branch
    died the same way."""
    _cluster(monkeypatch, previous="x:old", canary_ok=False)
    _repo_for(monkeypatch, tmp_path, 7)
    asyncio.run(deploy.deploy_k8s(7))
    h = deploy.history(7)
    assert h and h[0]["ok"] is False and "trial run" in h[0]["error"]


def test_the_image_is_named_after_the_code_and_not_the_clock(
        fresh_db, tmp_path, monkeypatch):
    """A timestamp tag made two deploys of identical code two different artifacts,
    which left "is production running my change?" with no answer but the clock."""
    seen = _cluster(monkeypatch, previous="x:old")
    root = _repo_for(monkeypatch, tmp_path, 7)
    asyncio.run(deploy.deploy_k8s(7))
    first = seen["built"]
    asyncio.run(deploy.deploy_k8s(7))
    assert seen["built"] == first, "identical code produced a different artifact"
    (root / "app.py").write_text("print(2)")
    asyncio.run(deploy.deploy_k8s(7))
    assert seen["built"] != first


def test_an_existing_deployment_has_its_image_changed_rather_than_reapplied(
        fresh_db, tmp_path, monkeypatch):
    """Applying a manifest that pins an image is how a rollout silently goes
    BACKWARDS to whatever the file happens to say."""
    seen = _cluster(monkeypatch, previous="reg.example.com/x/devteam-app-7:old")
    _repo_for(monkeypatch, tmp_path, 7)
    asyncio.run(deploy.deploy_k8s(7))
    assert seen["applied"] is None and seen["promoted"] is not None


def test_the_first_deploy_still_creates_the_service_and_ingress(
        fresh_db, tmp_path, monkeypatch):
    seen = _cluster(monkeypatch, previous="")      # nothing there yet
    _repo_for(monkeypatch, tmp_path, 7)
    asyncio.run(deploy.deploy_k8s(7))
    assert seen["applied"] and "kind: Service" in seen["applied"]


def test_a_cluster_rollback_does_not_rebuild(fresh_db, monkeypatch):
    """Rebuilding to go backwards produces a NEW image from the same source, not
    the one that was known to work."""
    monkeypatch.setattr(deploy.shutil, "which", lambda _n: "/usr/bin/" + _n)
    seen = {}
    monkeypatch.setattr(rollout, "rollback",
                        lambda ns, name: seen.update(target=name) or {"ok": True})
    r = asyncio.run(deploy.rollback_k8s(7))
    assert r["ok"] and seen["target"] == "devteam-app-7"


# ---- per-branch previews (G25) --------------------------------------------

def test_a_branch_preview_gets_its_own_name_and_host_not_the_deployed_apps(monkeypatch):
    """A preview that shared the app's Deployment would be a deployment with
    extra steps."""
    monkeypatch.setattr(config, "APPS_DOMAIN", "apps.example.com")
    assert deploy.app_name(7) == "devteam-app-7"
    assert deploy.app_name(7, "feat/login") == "devteam-app-7-feat-login"
    assert deploy.app_host(7, "feat/login") == "app-7-feat-login.apps.example.com"
    assert deploy.app_host(7, "feat/login") != deploy.app_host(7)


def test_a_preview_is_labelled_so_it_can_be_torn_down_on_its_own(monkeypatch):
    """Previews are the objects most likely to be forgotten, and an unlabelled
    forgotten object is one nothing can find to delete."""
    monkeypatch.setattr(config, "APPS_DOMAIN", "apps.example.com")
    for d in yaml.safe_load_all(deploy.manifests(7, "reg/img:1", branch="feat/login")):
        labels = d["metadata"]["labels"]
        assert labels["devteam/project"] == "7"
        assert labels["devteam/branch"] == "feat-login"


def test_stopping_the_app_does_not_discard_the_branches_someone_is_reading(monkeypatch):
    """Stopping the deployed app and discarding every preview of it are different
    intentions, and one button should not mean both."""
    seen = {}
    monkeypatch.setattr(deploy.shutil, "which", lambda _n: "/usr/bin/kubectl")
    monkeypatch.setattr(deploy.subprocess, "run",
                        lambda cmd, **k: seen.update(cmd=cmd) or
                        __import__("subprocess").CompletedProcess(cmd, 0, "", ""))
    deploy.stop(7)
    assert "devteam/project=7,!devteam/branch" in seen["cmd"]
    deploy.stop(7, "feat/login")
    assert "devteam/project=7,devteam/branch=feat-login" in seen["cmd"]


def test_a_branch_a_clone_would_misread_is_refused(fresh_db, monkeypatch):
    """It reaches argv, not a shell, so this is not about quoting: a ref starting
    with '-' is read by git as an option and '..' escapes the directory."""
    for bad in ("--upload-pack=evil", "-x", "../../etc", "a b", ""):
        assert deploy.safe_branch(bad) == "", bad
    assert deploy.safe_branch("feat/login") == "feat/login"


def test_a_hostile_branch_never_reaches_git(fresh_db, monkeypatch):
    from app import github_client
    # Signatures now carry a token — enabled(repo, token) and token_for(project) —
    # because a project's git operations run under its OWNER'S token, not the
    # server's. The stubs match that; the test itself is about branch sanitising.
    monkeypatch.setattr(github_client, "enabled", lambda repo, token="": True)
    monkeypatch.setattr(github_client, "token_for", lambda project: "t")
    monkeypatch.setattr(github_client, "clone_url", lambda repo, tok="": "https://x/y.git")

    def _boom(*a, **k):
        raise AssertionError("ran git with a ref it should have refused")
    monkeypatch.setattr(deploy.subprocess, "run", _boom)
    p = make_project(owner_id=1, repo="owner/repo")
    ok, note = asyncio.run(deploy.sync_code(p, "--upload-pack=evil"))
    assert ok is False and "refusing" in note


def test_a_preview_never_becomes_the_thing_you_roll_back_to(fresh_db, tmp_path,
                                                            monkeypatch):
    """Rolling the deployed app back to a state that was only ever a branch
    preview would ship, as production, something nobody chose to merge."""
    p = make_project(owner_id=1)
    monkeypatch.setattr(deploy, "_history_file", lambda pid: tmp_path / "h.json")
    deploy._remember(p, {"ok": True, "workspace": "", "branch": "", "source": "main-1"})
    deploy._remember(p, {"ok": True, "workspace": "", "branch": "", "source": "main-2"})
    deploy._remember(p, {"ok": True, "workspace": "", "branch": "feat/x",
                         "source": "branch"})
    picked = {}

    async def fake_deploy(pid, workspace="", branch=""):
        picked["branch"] = branch
        return {"ok": True}
    monkeypatch.setattr(deploy, "deploy_local", fake_deploy)
    r = asyncio.run(deploy.rollback(p))
    assert r["rolled_back_to"]["source"] == "main-1"
    assert picked["branch"] == ""


def test_a_preview_and_the_deployed_app_do_not_share_a_checkout(monkeypatch):
    """Sharing one would make the next rollback redeploy the branch."""
    assert deploy.workdir(7) != deploy.workdir(7, "feat/login")
    assert deploy.pid_file(7) != deploy.pid_file(7, "feat/login")
    assert deploy.log_path(7) != deploy.log_path(7, "feat/login")


def test_previews_are_read_from_the_cluster_not_from_memory(monkeypatch):
    """A preview outlives the conductor process that started it, and a list that
    forgets what is still running is how a namespace fills up."""
    monkeypatch.setattr(deploy.shutil, "which", lambda _n: "/usr/bin/kubectl")
    monkeypatch.setattr(config, "APPS_DOMAIN", "apps.example.com")
    import subprocess as sp
    monkeypatch.setattr(deploy, "_kubectl", lambda *a, **k:
                        sp.CompletedProcess(a, 0, "feat-login|reg/img:1|1\n", ""))
    got = deploy.previews(7)
    assert got and got[0]["branch"] == "feat-login" and got[0]["ready"] is True
    assert got[0]["url"] == "http://app-7-feat-login.apps.example.com"


# ---- the provider seam (G4) ----------------------------------------------

def test_the_registry_falls_back_to_whatever_the_cloud_provides(monkeypatch):
    """DEPLOY_REGISTRY still wins so nothing configured today changes; on
    DigitalOcean the fallback is DOCR, where the platform's images already live."""
    monkeypatch.delenv("DEPLOY_REGISTRY", raising=False)
    monkeypatch.setenv("DOCR_REGISTRY", "registry.digitalocean.com/pulkit")
    assert deploy._registry() == "registry.digitalocean.com/pulkit"
    monkeypatch.setenv("DEPLOY_REGISTRY", "reg.example.com/x")
    assert deploy._registry() == "reg.example.com/x"


def test_a_private_registry_means_the_app_pod_needs_a_pull_secret(monkeypatch):
    """Without it the pod sits in ImagePullBackOff, which reads as "image not
    found" and sends you hunting for a build problem that isn't there."""
    monkeypatch.setenv("DOCR_REGISTRY", "registry.digitalocean.com/pulkit")
    monkeypatch.delenv("DEPLOY_REGISTRY", raising=False)
    assert "imagePullSecrets: [{name: docr-creds}]" in deploy.manifests(7, "img:1")


def test_no_registry_means_no_pull_secret(monkeypatch):
    monkeypatch.delenv("DOCR_REGISTRY", raising=False)
    monkeypatch.delenv("DEPLOY_REGISTRY", raising=False)
    assert "imagePullSecrets" not in deploy.manifests(7, "img:1")


def test_a_preview_is_someones_and_not_everyones(root_client, make_user, monkeypatch):
    """Every other project route checks ownership; a preview route that did not
    would let anyone run — and read — a branch of someone else's app."""
    monkeypatch.setattr(deploy, "previews", lambda pid: [])
    p = make_project(owner_id=1)
    assert root_client.get(f"/api/projects/{p}/previews").status_code == 200
    _uid, other = make_user("someone-else")
    assert other.get(f"/api/projects/{p}/previews").status_code in (403, 404)


def test_the_preview_route_refuses_a_branch_git_would_misread(root_client):
    p = make_project(owner_id=1)
    r = root_client.post(f"/api/projects/{p}/previews", params={"branch": "-x"})
    assert r.status_code == 400


def test_deploy_detection_does_not_answer_from_a_stale_checkout(fresh_db, monkeypatch):
    """A real run left a game permanently undeployable. The checkout was taken
    while the programmer's pull request was still open, so it held art and helper
    scripts and no entrypoint — and "could not work out how to start this" was
    then frozen against a repository that gained an index.html twenty minutes
    later, because nothing ever re-cloned."""
    from app import deploy
    calls = []
    monkeypatch.setattr(deploy, "checkout",
                        lambda pid, branch="": (calls.append(pid), (True, "ok"))[1])
    pid = make_project(name="d1", repo="o/r")
    deploy.status(pid)
    assert calls, "status() detected without refreshing the checkout"


def test_the_refresh_is_throttled(fresh_db, monkeypatch):
    """It is a polled endpoint. A fetch per poll is a git request every few
    seconds for every open dashboard."""
    from app import deploy
    calls = []
    monkeypatch.setattr(deploy, "checkout",
                        lambda pid, branch="": (calls.append(pid), (True, "ok"))[1])
    pid = make_project(name="d2", repo="o/r")
    deploy.workdir(pid).mkdir(parents=True, exist_ok=True)
    # Project ids restart with each fresh database but DEPLOY_DIR does not, so an
    # earlier test's freshness marker for the same id would throttle this one and
    # the assertion would pass for the wrong reason.
    (deploy._base(pid) / ".detected").unlink(missing_ok=True)
    deploy.status(pid)
    deploy.status(pid)
    assert len(calls) == 1, f"refreshed {len(calls)} times for two polls"


def test_an_unreachable_remote_keeps_the_previous_answer(fresh_db, monkeypatch):
    """Replacing a usable answer with an error because the network blipped is
    worse than showing a slightly old one."""
    from app import deploy
    def boom(pid, branch=""):
        raise RuntimeError("no route to host")
    monkeypatch.setattr(deploy, "checkout", boom)
    pid = make_project(name="d3", repo="o/r")
    deploy.status(pid)      # must not raise


def test_deployments_live_beside_the_workspaces_not_in_the_image(fresh_db):
    """DEPLOY_DIR defaulted under the repo root — /app in a container — which is
    ephemeral. Every rollout destroyed each project's checkout and running app
    while the database still recorded a live deployment."""
    from app import config, deploy
    assert deploy.DEPLOY_DIR.parent == config.WORKSPACES_DIR.parent


def test_a_static_project_gets_a_working_button_not_a_disabled_one(fresh_db):
    """A game that runs perfectly showed a greyed-out Deploy next to "use the
    static preview instead" — which reads as broken and leaves you to find the
    preview yourself. There is nothing to build; it is a different button."""
    from pathlib import Path
    js = dashboard_js()
    assert 'id="openStaticBtn"' in js
    assert "▶ Open it" in js
    # And it must actually reach the preview rather than just look like a button.
    handler = js.split('$("#openStaticBtn")')[1][:500]
    assert "/preview" in handler and "window.open" in handler


def test_an_undetectable_project_says_it_will_re_check_itself(fresh_db):
    """"Unknown" was permanent before, so telling someone it re-checks is only
    honest now that it does."""
    from pathlib import Path
    js = dashboard_js()
    assert "re-checks itself" in js


def test_a_bundled_project_is_built_before_it_is_shown(fresh_db):
    """A real run spent 17 tasks and $24 and produced a black screen. The project
    was Vite: its index.html loads a module graph the bundler resolves, so serving
    the raw checkout renders nothing and shows the dark background the page sets
    before its own script runs. No error appears anywhere."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "conductor" / "app" / "preview.py").read_text()
    assert "def _build_if_needed" in src
    assert "npm" in src and "run" in src
    # A failed build must be fatal: serving the source instead shows that same
    # black screen and calls it success.
    assert "return False, build_note" in src


def test_build_output_is_preferred_over_source(fresh_db):
    """The search started at the repo root, and a bundler's project has an
    index.html there — the unbuilt one."""
    from app import preview
    order = list(preview._STATIC_SUBDIRS)
    assert order.index("dist") < order.index("")
    assert order.index("build") < order.index("")


def test_the_per_teammate_view_is_actually_reachable(fresh_db):
    """The endpoint existed from the day teammates became rows and nothing called
    it, so auditing a seventeen-task run meant reading PR titles and guessing."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    js = dashboard_js()
    routes = (root / "conductor" / "app" / "routes.py").read_text()
    assert '/by-agent' in routes
    assert "renderByAgent" in js and "/by-agent" in js


def test_built_assets_work_when_the_preview_is_not_at_the_domain_root(fresh_db, tmp_path):
    """The bundle WAS built and WAS served — and the page asked for
    /assets/index-abc.js while the files sat at /preview/4/assets/. Every request
    404'd, no script ran, and the dark fallback background was the black screen."""
    from app import preview
    d = tmp_path / "repo"
    (d / "dist").mkdir(parents=True)
    (d / "dist" / "index.html").write_text(
        '<script type="module" src="/assets/app.js"></script>'
        '<link rel="stylesheet" href="/assets/app.css">'
        '<a href="https://example.com/x">external</a>')
    preview._relativise_assets(d)
    out = (d / "dist" / "index.html").read_text()
    assert 'src="./assets/app.js"' in out
    assert 'href="./assets/app.css"' in out
    # An absolute external URL is not a path on this site and must be left alone.
    assert 'href="https://example.com/x"' in out
