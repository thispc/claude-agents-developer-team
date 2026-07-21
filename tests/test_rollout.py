"""The shipping mechanics both deploy paths now share.

These used to exist once, for the platform only, and a user's app was deployed by
building an image and applying a manifest over whatever was serving. Everything
here is a property that was true for the platform and false for a user's app —
which is the whole of G24 — plus the incidents that shaped each rule, because a
rule whose reason is forgotten is a rule someone simplifies away.

Nothing in this file may reach a cluster. The one that matters is live.
"""

import subprocess

import pytest

from app import rollout


def _sh_recorder(monkeypatch, answers=None, fail=()):
    """Stand in for kubectl/docker: record argv, answer from a lookup table.

    `answers` maps a substring of the jsonpath (or any argument) to stdout;
    `fail` is a set of first-arguments that should come back non-zero.
    """
    calls: list[tuple] = []
    answers = answers or {}

    def sh(*cmd, **kw):
        calls.append(cmd)
        out = ""
        for needle, value in answers.items():
            if any(needle in str(c) for c in cmd):
                out = value
                break
        code = 1 if cmd[0] in fail or any(c in fail for c in cmd) else 0
        return subprocess.CompletedProcess(cmd, code, out, "" if not code else "boom")
    monkeypatch.setattr(rollout, "sh", sh)
    return calls


# ---- identity: an image is what it was built from -------------------------

def test_the_same_source_produces_the_same_tag_and_a_change_produces_another(tmp_path):
    """A timestamp tag made two deploys of identical code two different artifacts,
    so "is production running my change?" had no answer but the clock."""
    (tmp_path / "app.py").write_text("v1")
    first = rollout.content_tag("app", "main", rollout.tree_hash(tmp_path))
    assert rollout.content_tag("app", "main", rollout.tree_hash(tmp_path)) == first
    (tmp_path / "app.py").write_text("v2")
    assert rollout.content_tag("app", "main", rollout.tree_hash(tmp_path)) != first


def test_a_tag_survives_a_branch_name_kubernetes_would_reject(tmp_path):
    (tmp_path / "app.py").write_text("v1")
    tag = rollout.content_tag("app", "feat/Some Thing!", rollout.tree_hash(tmp_path))
    ref = tag.split(":", 1)[1]
    assert ref.startswith("feat-some-thing-")
    assert all(c.isalnum() or c == "-" for c in ref)


def test_the_hash_ignores_what_is_never_shipped(tmp_path):
    (tmp_path / "app.py").write_text("v1")
    before = rollout.tree_hash(tmp_path)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("noise")
    assert rollout.tree_hash(tmp_path) == before


# ---- a failed build must not yield a tag ----------------------------------

def test_a_failed_build_returns_no_tag_at_all(monkeypatch, tmp_path):
    """The tag was once read out of docker's own output with `awk '{print $3}'`.
    On a failed build that column held the word "docker", production was set to an
    image literally called `docker`, and the cluster sat in ImagePullBackOff
    saying nothing about a build."""
    monkeypatch.setattr(rollout, "sh", lambda *c, **k:
                        subprocess.CompletedProcess(c, 1, "", "no space left on device"))
    r = rollout.build_image("Dockerfile", tmp_path, "app:abc")
    assert r["ok"] is False
    assert "tag" not in r, "a failed build handed back something deployable"


def test_an_expired_registry_login_is_named_rather_than_dumped(monkeypatch, tmp_path):
    """Registry credentials are short-lived by design, so 'unauthorized' means an
    expired login far more often than anything else. Say so."""
    monkeypatch.setattr(rollout, "sh", lambda *c, **k:
                        subprocess.CompletedProcess(c, 1, "", "unauthorized: authentication required"))
    r = rollout.build_image("Dockerfile", tmp_path, "app:abc")
    assert "login has expired" in r["error"]


def test_a_remote_cluster_is_cross_built_and_pushed_in_one_step(monkeypatch, tmp_path):
    """DOKS nodes are amd64 and a Mac builds arm64; that image dies on the node
    with "exec format error", which says nothing about architecture. There is
    nothing to gain from holding a foreign-arch image locally first."""
    calls = _sh_recorder(monkeypatch)
    rollout.build_image("Dockerfile", tmp_path, "reg/app:abc",
                        platform="linux/amd64", push=True)
    cmd = calls[0]
    assert "buildx" in cmd and "linux/amd64" in cmd and "--push" in cmd
    assert "--load" not in cmd


def test_a_local_build_does_not_reach_for_buildx(monkeypatch, tmp_path):
    calls = _sh_recorder(monkeypatch)
    rollout.build_image("Dockerfile", tmp_path, "app:abc")
    assert "buildx" not in calls[0]


# ---- the trial run: proving an image works before it can matter -----------

def test_a_canary_has_no_service_so_nothing_can_route_to_it():
    body = rollout._canary_manifest("devteam", "app-canary", "img:1", 8080, "/", "", {})
    assert "kind: Service" not in body
    assert "kind: Deployment" in body


def test_a_canary_is_probed_the_same_way_the_real_rollout_will_probe_it():
    """A canary that is easier to satisfy than the rollout it gates proves
    nothing: the image passes here and then hangs the rollout for the same
    reason."""
    body = rollout._canary_manifest("devteam", "c", "img:1", 8080, "/healthz", "", {})
    assert "path: /healthz" in body and "port: 8080" in body


def test_a_canary_that_becomes_ready_passes_and_is_torn_down(monkeypatch):
    calls = _sh_recorder(monkeypatch, {"readyReplicas": "1"})
    r = rollout.canary("devteam", "c", "img:1", poll=0, timeout=5)
    assert r["ok"] is True
    assert any("delete" in c for c in calls), "the trial pod was left running"


def test_a_canary_fails_immediately_on_a_reason_that_never_resolves(monkeypatch):
    """ImagePullBackOff does not become ready by waiting. Failing at the timeout
    instead would report "did not become ready in 150s" — true, and not the
    reason."""
    answers = {"containerStatuses": "ImagePullBackOff|manifest unknown\n"}
    _sh_recorder(monkeypatch, answers)
    r = rollout.canary("devteam", "c", "img:1", poll=0, timeout=60)
    assert r["ok"] is False and "ImagePullBackOff" in r["error"]


def test_a_canary_is_torn_down_even_when_it_fails(monkeypatch):
    calls = _sh_recorder(monkeypatch, {"containerStatuses": "CrashLoopBackOff|\n"})
    rollout.canary("devteam", "c", "img:1", poll=0, timeout=60)
    assert sum(1 for c in calls if "delete" in c) >= 2, \
        "a failed trial left a pod behind, and the next run would read it as its own"


def test_a_canary_that_cannot_even_be_created_says_so(monkeypatch):
    _sh_recorder(monkeypatch, fail=("apply",))
    r = rollout.canary("devteam", "c", "img:1", poll=0, timeout=5)
    assert r["ok"] is False and "could not start the canary" in r["error"]


# ---- promotion names its target ------------------------------------------

def test_a_deployment_name_kubectl_would_read_as_something_else_is_refused():
    """argv leaves no shell to inject into, but a leading '-' is still parsed as
    an option and a '/' changes which resource TYPE is addressed."""
    for bad in ("--all", "-l app=x", "statefulset/devteam-conductor", "   ", ""):
        assert rollout.deployment_name(bad) == "", bad
    assert rollout.deployment_name("", "devteam-conductor") == "devteam-conductor"


def test_promoting_into_a_deployment_that_is_not_there_changes_nothing(monkeypatch):
    """`kubectl set image` exits 0 and prints nothing for `--all` or a selector
    matching nothing. A promote that no-ops is worse than one that errors: it
    reports the new image as live while the old one keeps serving."""
    calls = _sh_recorder(monkeypatch)          # every `get` answers empty
    r = rollout.promote("devteam", "devteam-typo", "img:2")
    assert r["ok"] is False and "devteam-typo" in r["error"]
    assert not any("set" in c for c in calls), "it changed something after failing to find the target"


def test_promotion_asks_what_the_container_is_called(monkeypatch):
    """Assuming "conductor" fails with kubectl's own words — 'unable to find
    container named "conductor"' — which reads as if the image were wrong."""
    calls = _sh_recorder(monkeypatch, {"containers[0]": "sidecar"})
    rollout.promote("devteam", "app", "img:2")
    setimg = next(c for c in calls if "set" in c)
    assert "sidecar=img:2" in setimg


def test_a_rollout_that_does_not_come_up_is_undone(monkeypatch):
    """Half a rollout is worse than none: the old ReplicaSet is still there, so
    there is no reason to leave a broken one serving."""
    calls = _sh_recorder(monkeypatch, {"containers[0]": "app"}, fail=("status",))
    r = rollout.promote("devteam", "app", "img:2")
    assert r["ok"] is False and r["rolled_back"] is True
    assert any("undo" in c for c in calls)


def test_rollback_undoes_the_deployment_it_was_asked_about(monkeypatch):
    calls = _sh_recorder(monkeypatch)
    rollout.rollback("devteam", "devteam-api")
    assert any("deployment/devteam-api" in c and "undo" in c for c in calls)


# ---- the tooling is not always there --------------------------------------

def test_a_missing_binary_is_a_failed_run_not_a_crash():
    """The conductor's container has no docker and no kubectl on purpose.
    subprocess.run raises rather than returning non-zero for a missing binary,
    which turned every deployment call into a 500: the page said the server was
    broken when the honest answer is "that tool isn't here"."""
    r = rollout.sh("definitely-not-a-real-binary-xyz", "--version")
    assert r.returncode == 127
