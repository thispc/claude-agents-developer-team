"""Triage: how much independence a self-repair issue gets.

The design is one asymmetry — rules set a floor, a model may only raise it. So
most of these tests are about what a persuasive or compromised classifier CANNOT
do, which is the property the whole thing rests on.
"""

import pytest

from conftest import make_project, make_task

from app import db, triage


# ---- the floor is deterministic ------------------------------------------

def test_ordinary_polish_is_routine():
    tier, why = triage.floor_for("the blockers tab has a typo and bad spacing")
    assert tier == triage.ROUTINE and not why


@pytest.mark.parametrize("text,expected", [
    ("fix the auth check on the settings route", triage.SUBSTANTIAL),
    ("add a migration to the tasks schema", triage.SUBSTANTIAL),
    ("update the kubernetes manifest for the conductor", triage.SUBSTANTIAL),
    ("raise max_runs because projects run out", triage.SUBSTANTIAL),
    ("skip the failing test so merges stop being blocked", triage.RESTRICTED),
    ("delete the old project and its database", triage.RESTRICTED),
])
def test_dangerous_areas_have_a_floor(text, expected):
    tier, why = triage.floor_for(text)
    assert tier == expected, f"{text!r} -> {tier}"
    assert why, "a floor must explain itself"


def test_the_highest_floor_wins():
    """An issue touching several sensitive areas gets the strictest of them."""
    tier, why = triage.floor_for("change the auth token AND drop the projects database")
    assert tier == triage.RESTRICTED
    assert len(why) >= 2


# ---- a model may raise the floor, never lower it -------------------------

@pytest.mark.asyncio
async def test_a_model_cannot_talk_its_way_past_a_rule(monkeypatch):
    """The property the design rests on: a convincing 'this auth change is
    trivial' must not lower the bar. The worst an over-confident or compromised
    classifier can do is make the platform MORE cautious."""
    async def eager(*a, **k):
        return '{"tier": "routine", "why": "tiny one-line change, obviously safe"}'
    monkeypatch.setattr("app.providers.complete", eager)
    monkeypatch.setattr("app.providers.available", lambda s: ["anthropic"])
    r = await triage.classify("tweak the auth check", "one line", {"anthropic_api_key": "k"})
    assert r["tier"] == triage.SUBSTANTIAL
    assert r["floor"] == triage.SUBSTANTIAL
    assert r["model"]["tier"] == triage.ROUTINE      # it did argue for less


@pytest.mark.asyncio
async def test_a_model_may_ask_for_more_caution(monkeypatch):
    async def cautious(*a, **k):
        return '{"tier": "substantial", "why": "this touches how tasks are dispatched"}'
    monkeypatch.setattr("app.providers.complete", cautious)
    monkeypatch.setattr("app.providers.available", lambda s: ["anthropic"])
    r = await triage.classify("change the retry wording", "…", {"anthropic_api_key": "k"})
    assert r["floor"] == triage.ROUTINE and r["tier"] == triage.SUBSTANTIAL


@pytest.mark.asyncio
async def test_nonsense_from_the_model_becomes_caution(monkeypatch):
    async def junk(*a, **k):
        return "I think this is probably fine!"
    monkeypatch.setattr("app.providers.complete", junk)
    monkeypatch.setattr("app.providers.available", lambda s: ["anthropic"])
    r = await triage.classify("some change", "…", {"anthropic_api_key": "k"})
    assert r["tier"] == triage.SUBSTANTIAL


@pytest.mark.asyncio
async def test_no_model_means_the_floor_stands(monkeypatch):
    monkeypatch.setattr("app.providers.available", lambda s: [])
    r = await triage.classify("fix a typo", "…", {})
    assert r["tier"] == triage.ROUTINE
    r2 = await triage.classify("change the auth flow", "…", {})
    assert r2["tier"] == triage.SUBSTANTIAL


@pytest.mark.asyncio
async def test_a_provider_outage_does_not_grant_autonomy(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr("app.providers.complete", boom)
    monkeypatch.setattr("app.providers.available", lambda s: ["anthropic"])
    r = await triage.classify("update the deploy manifest", "…", {"anthropic_api_key": "k"})
    assert r["tier"] == triage.SUBSTANTIAL


@pytest.mark.asyncio
async def test_restricted_never_even_asks_a_model(monkeypatch):
    """Nothing to decide, and no opportunity for a model to be persuasive."""
    async def _boom(*a, **k):
        raise AssertionError("asked a model about a restricted change")
    monkeypatch.setattr("app.providers.complete", _boom)
    monkeypatch.setattr("app.providers.available", lambda s: ["anthropic"])
    r = await triage.classify("drop the projects database", "…", {"anthropic_api_key": "k"})
    assert r["tier"] == triage.RESTRICTED


# ---- what each tier is allowed to do ------------------------------------

def test_routine_ships_without_asking():
    p = triage.policy(triage.ROUTINE)
    assert p["may_merge"] and p["may_deploy"] and not p["notify"]


def test_substantial_stops_at_a_pull_request():
    p = triage.policy(triage.SUBSTANTIAL)
    assert p["may_work"] and not p["may_merge"] and not p["may_deploy"]
    assert p["notify"]


def test_restricted_does_not_start():
    p = triage.policy(triage.RESTRICTED)
    assert not p["may_work"] and not p["may_merge"] and not p["may_deploy"]


def test_no_tier_can_deploy_without_merging():
    """Deploying something unmerged would put code on the platform that exists
    nowhere in the repo's history."""
    for t in (triage.ROUTINE, triage.SUBSTANTIAL, triage.RESTRICTED):
        p = triage.policy(t)
        assert not (p["may_deploy"] and not p["may_merge"])


def test_the_guard_rails_protect_themselves():
    """An issue proposing to weaken triage is exactly what must not be routine."""
    tier, _ = triage.floor_for("edit triage.py to allow auto-merge for auth changes")
    assert tier == triage.RESTRICTED


# ---- the operator finds out before, not after ---------------------------

def test_the_verdict_is_shown_while_writing_the_ticket():
    """Learning afterwards that the platform merged something you expected to
    review is the outcome this whole mechanism exists to prevent."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "dashboard" / "app.js").read_text()
    assert "/api/self/triage" in js
    assert 'addEventListener("input", previewTriage)' in js


@pytest.mark.asyncio
async def test_the_triage_route_answers_before_anything_is_filed(root_client, fresh_db,
                                                                monkeypatch):
    monkeypatch.setattr("app.providers.available", lambda s: [])
    r = root_client.post("/api/self/triage",
                         json={"rough": "the deploy manifest needs a memory limit"})
    assert r.status_code == 200
    d = r.json()
    assert d["tier"] == "substantial"
    assert d["policy"]["may_merge"] is False


def test_a_restricted_issue_is_refused_rather_than_started(root_client, fresh_db,
                                                           monkeypatch):
    """Refusing to start is the point: work begun unsupervised on something that
    should not be attempted is already damage, even if never merged."""
    monkeypatch.setattr("app.providers.available", lambda s: [])
    r = root_client.post("/api/self/issue", json={
        "title": "drop the projects database and rebuild it",
        "body": "it has stale rows"})
    assert r.status_code == 400
    assert "Not started unsupervised" in r.json()["detail"]


# ---- a new version must prove it boots before it becomes the platform ----

def test_self_update_runs_a_canary_first(monkeypatch):
    """Kubernetes protects against a pod that never becomes ready — but only
    AFTER the rollout has begun. Trying the image first is cheaper than finding
    out by replacing a working platform with a broken one."""
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "current_image", lambda: "img:1")
    monkeypatch.setattr(cloud, "busy", lambda: [])
    monkeypatch.setattr(cloud, "canary", lambda i: {"ok": False, "error": "CrashLoopBackOff"})

    def _boom(*a, **k):
        raise AssertionError("adopted an image that failed its trial run")
    monkeypatch.setattr(cloud, "_api", _boom)
    r = cloud.self_update("img:2")
    assert r["ok"] is False and "did not pass a trial run" in r["error"]


def test_a_passing_canary_lets_the_update_through(monkeypatch):
    from app import cloud
    patched = {}
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "current_image", lambda: "img:1")
    monkeypatch.setattr(cloud, "busy", lambda: [])
    monkeypatch.setattr(cloud, "canary", lambda i: {"ok": True})

    class _Api:
        def patch_namespaced_deployment(self, *a, **k):
            patched["done"] = True
    monkeypatch.setattr(cloud, "_api", lambda: _Api())
    assert cloud.self_update("img:2")["ok"] is True and patched["done"]


def test_force_skips_the_canary(monkeypatch):
    """force is for getting OUT of a bad state, where a canary would be a second
    thing to go wrong while the platform is already broken."""
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "current_image", lambda: "img:1")

    def _boom(i):
        raise AssertionError("ran a canary during a forced recovery")
    monkeypatch.setattr(cloud, "canary", _boom)

    class _Api:
        def patch_namespaced_deployment(self, *a, **k): pass
    monkeypatch.setattr(cloud, "_api", lambda: _Api())
    assert cloud.self_update("img:2", force=True)["ok"] is True


def test_the_canary_never_touches_the_real_database():
    """A migration that destroys data must do it to a scratch file — that is the
    entire reason for running the trial at all."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "conductor" / "app" / "cloud.py").read_text()
    body = src.split("def canary(")[1].split("\ndef ")[0]
    assert 'value="/tmp/canary.db"' in body
    assert 'name="DEMO_MODE", value="1"' in body, "a canary must not run real agents"
    assert "V1Service" not in body, "nothing should be able to route to a canary"


# ---- what it did on its own stays visible -------------------------------

def test_the_healing_feed_shows_rejected_builds_too():
    """The platform declining to ship something to itself is the most valuable
    line in that list, and the easiest to miss."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "dashboard" / "app.js").read_text()
    assert "renderHealing" in js and "/api/self/healing" in js
    assert "canary_failed" in js


def test_the_healing_route_reports_what_happened(root_client, fresh_db):
    from app import selfops, bus
    pid = selfops.ensure_project(1)
    bus.emit(pid, None, "system", "self_healed",
             {"summary": "fixed the blockers tab contrast", "fixed": ["#1 contrast"]})
    bus.emit(0, None, "system", "canary_failed",
             {"image": "x:2", "why": "CrashLoopBackOff"})
    d = root_client.get("/api/self/healing").json()
    kinds = [i["kind"] for i in d["items"]]
    assert "self_healed" in kinds and "canary_failed" in kinds


# ---- staging: real credentials, different identity -----------------------

def test_staging_never_shares_productions_identity():
    """"Full-fledged credentials" must mean the same KIND of credentials, never
    the same identity — otherwise staging becomes a way to damage production
    rather than a way to protect it."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "conductor" / "app" / "cloud.py").read_text()
    body = src.split("def _staging_secret(")[1].split("\ndef ")[0]
    assert '_set("WORKER_TOKEN"' in body, "its workers could report into production"
    assert '_set("MAX_AGENT_RUNS"' in body, "a runaway would spend the whole plan"
    assert '_set("ROOT_PASSWORD"' in body


def test_staging_shares_the_repo_but_not_the_power_to_merge():
    """Withholding GitHub entirely was over-cautious and made staging useless.
    Staging works on its own branches and opens PRs, so the repo is already
    shared safely — branch namespacing is the isolation. What must be separate is
    narrower: the ability to MERGE into the branch production builds from."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "conductor" / "app" / "cloud.py").read_text()
    body = src.split("def _staging_secret(")[1].split("\ndef ")[0]
    assert '_set("PROTECTED_REPOS"' in body, (
        "staging could merge into the repository this platform is built from")
    assert '_set("BRANCH_PREFIX", "staging/")' in body


def test_a_no_merge_environment_actually_refuses_to_merge(fresh_db, monkeypatch):
    """Enforced in the tool, not by prompt: "please do not merge" is a request,
    this is a rule."""
    import asyncio
    from app import config as cfg, manager
    monkeypatch.setattr(cfg, "ALLOW_MERGE", False)
    p = make_project(owner_id=1, repo="o/r")
    make_task(p, status="review")
    manager.build_team_server(p)
    out = str(asyncio.run(manager.HANDLERS[p]["handlers"]["merge_pr"]({"task_id": 1})))
    assert "REFUSED" in out and "cannot merge anything" in out


def test_the_platforms_own_repo_is_protected_even_where_merging_is_allowed(
        fresh_db, monkeypatch):
    """The rule that actually matters, and the reason the blanket ban could go.
    It is a property of the TARGET, not of the instance — so it holds on an
    instance that merges everything else quite happily."""
    import asyncio
    from app import config as cfg, manager
    monkeypatch.setattr(cfg, "ALLOW_MERGE", True)
    monkeypatch.setattr(cfg, "PROTECTED_REPOS", ["me/platform"])
    p = make_project(owner_id=1, repo="Me/Platform")     # case must not matter
    make_task(p, status="review")
    manager.build_team_server(p)
    out = str(asyncio.run(manager.HANDLERS[p]["handlers"]["merge_pr"]({"task_id": 1})))
    assert "REFUSED" in out
    assert "built from" in out
    # And it says what CAN be done, rather than only what cannot.
    assert "Everything else you can merge normally" in out


def test_an_ordinary_project_on_staging_can_still_be_merged(fresh_db, monkeypatch):
    """The point of the change. A pull request on a throwaway project in a scratch
    repository is exactly the workflow worth rehearsing before production sees it,
    and the blanket ban made rehearsing it impossible."""
    from app import config as cfg
    monkeypatch.setattr(cfg, "ALLOW_MERGE", True)
    monkeypatch.setattr(cfg, "PROTECTED_REPOS", ["me/platform"])
    allowed, why = cfg.may_merge("someone/scratch-project")
    assert allowed and why == ""


def test_branch_prefix_keeps_staging_work_identifiable(fresh_db, monkeypatch):
    from app import config as cfg, db as _db
    monkeypatch.setattr(cfg, "BRANCH_PREFIX", "staging/")
    p = make_project(owner_id=1)
    t = make_task(p)
    assert _db.get_task(t)["branch"].startswith("staging/task/")


def test_production_branches_are_unprefixed_by_default(fresh_db):
    p = make_project(owner_id=1)
    t = make_task(p)
    assert db.get_task(t)["branch"].startswith("task/")


@pytest.mark.hostonly
def test_staging_has_its_own_namespace_and_volume():
    from app import cloud
    assert cloud.STAGING_NS != cloud.namespace()
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "conductor" / "app" / "cloud.py").read_text()
    body = src.split("def staging_deploy(")[1].split("\ndef ")[0]
    assert "empty_dir" in body, "staging must not mount production's volume"


def test_staging_run_cap_is_small_by_default():
    from app import cloud
    assert int(cloud.STAGING_MAX_RUNS) <= 20


def test_staging_routes_are_root_only(client, make_user, fresh_db, monkeypatch):
    from app import config as cfg
    monkeypatch.setattr(cfg, "SELFREPAIR_USERS", [])
    _uid, c2 = make_user("mallory")
    assert c2.post("/api/self/staging", json={"image": "x:1"}).status_code == 403
    assert c2.delete("/api/self/staging").status_code == 403


# ---- the gate: staging must vouch for the exact image --------------------

def test_production_refuses_an_image_staging_has_not_verified(monkeypatch):
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "current_image", lambda: "img:1")
    monkeypatch.setattr(cloud, "busy", lambda: [])
    monkeypatch.setattr(cloud, "REQUIRE_STAGING", True)
    monkeypatch.setattr(cloud, "verified_image", lambda: "img:0")

    def _boom(i):
        raise AssertionError("ran a canary on an unverified image")
    monkeypatch.setattr(cloud, "canary", _boom)
    r = cloud.self_update("img:2")
    assert r["ok"] is False and "staging has not verified" in r["error"]


def test_the_gate_is_per_image_not_per_run(monkeypatch):
    """Verifying image A must not authorise image B — otherwise one green run
    licenses everything that follows it."""
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "current_image", lambda: "img:1")
    monkeypatch.setattr(cloud, "busy", lambda: [])
    monkeypatch.setattr(cloud, "REQUIRE_STAGING", True)
    monkeypatch.setattr(cloud, "verified_image", lambda: "img:2")
    monkeypatch.setattr(cloud, "canary", lambda i: {"ok": True})

    class _Api:
        def patch_namespaced_deployment(self, *a, **k): pass
    monkeypatch.setattr(cloud, "_api", lambda: _Api())
    assert cloud.self_update("img:2")["ok"] is True
    monkeypatch.setattr(cloud, "verified_image", lambda: "img:9")
    assert cloud.self_update("img:2")["ok"] is False


def test_force_bypasses_the_gate(monkeypatch):
    """A gate that blocks the only route out of a broken deployment is worse than
    no gate."""
    from app import cloud
    monkeypatch.setattr(cloud, "in_cluster", lambda: True)
    monkeypatch.setattr(cloud, "current_image", lambda: "img:1")
    monkeypatch.setattr(cloud, "REQUIRE_STAGING", True)
    monkeypatch.setattr(cloud, "verified_image", lambda: "")

    class _Api:
        def patch_namespaced_deployment(self, *a, **k): pass
    monkeypatch.setattr(cloud, "_api", lambda: _Api())
    assert cloud.self_update("img:2", force=True)["ok"] is True


def test_the_gate_is_off_by_default():
    from app import cloud
    assert cloud.REQUIRE_STAGING is False


def test_the_verdict_outlives_the_pod_that_made_it():
    """The pod that verifies is the pod about to be replaced by the update it
    authorises, so the record cannot live in its memory or filesystem."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "conductor" / "app" / "cloud.py").read_text()
    body = src.split("def _record_verified(")[1].split("\ndef ")[0]
    assert "patch_namespaced_deployment" in body and "annotations" in body


@pytest.mark.hostonly
def test_the_image_carries_its_own_tests():
    """Running the suite from a checkout would test whatever the checkout happened
    to be — the drift this whole arrangement exists to remove."""
    from pathlib import Path
    df = (Path(__file__).resolve().parent.parent / "deploy" / "Dockerfile.conductor").read_text()
    assert "COPY tests/ tests/" in df and "pytest" in df


# --- the self-update gate has to be trustworthy ----------------------------

@pytest.mark.hostonly
def test_a_verdict_that_could_not_be_recorded_is_not_reported_as_success():
    """Otherwise a suite passes, nothing is written, and the next self-update
    refuses an image somebody just watched go green with no explanation."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "conductor" / "app" / "cloud.py").read_text()
    assert "def _record_verified(image: str) -> tuple[bool, str]" in src
    assert "could not be recorded" in src


@pytest.mark.hostonly
def test_production_is_granted_what_it_needs_in_the_staging_namespace():
    """The gate cannot work without these, and the failure mode was a misleading
    error rather than an obvious one."""
    from pathlib import Path
    rbac = (Path(__file__).resolve().parent.parent
            / "deploy" / "k8s" / "rbac.yaml").read_text()
    assert "namespace: devteam-staging" in rbac
    assert "pods/exec" in rbac
    # And NOT secrets: staging's secret is created at provisioning time with a
    # real kubeconfig, not by the production pod.
    driver = rbac.split("devteam-staging-driver")[1].split("---")[0]
    assert "secrets" not in driver


@pytest.mark.hostonly
def test_no_kubernetes_client_is_built_without_being_pointed_at_the_cluster():
    """Only _api() loaded the config, so anything constructing a CoreV1Api
    directly got a client with no host and failed with "No host specified" —
    which reads as a networking problem and is not. Depending on another
    function's side effect for something this load-bearing is how that happens."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "conductor" / "app" / "cloud.py").read_text()
    # Strip the two constructors that are allowed to do it, then look for others.
    for helper in ("def _api():", "def _core():"):
        block = src.split(helper)[1].split("\ndef ")[0]
        src = src.replace(block, "")
    stray = re.findall(r"client\.(?:CoreV1Api|AppsV1Api)\(\)", src)
    assert not stray, f"{len(stray)} client(s) built outside _api()/_core()"


@pytest.mark.hostonly
def test_the_verify_check_does_not_reuse_the_worker_token():
    """Staging gets its own worker token precisely so a staging worker cannot
    report into production. Reusing it for this check would undo that isolation
    to save a variable."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "conductor" / "app"
    cfg = (root / "config.py").read_text()
    cloud_src = (root / "cloud.py").read_text()
    assert "VERIFY_TOKEN = _env(\"VERIFY_TOKEN\") or WORKER_TOKEN" in cfg
    assert "config.VERIFY_TOKEN" in cloud_src
    body = cloud_src.split("def staging_verify(")[1].split("\ndef ")[0]
    assert "config.WORKER_TOKEN" not in body


@pytest.mark.hostonly
def test_staging_verify_refuses_to_vouch_for_a_build_staging_is_not_running():
    """Otherwise production is told an image is safe on the strength of a suite
    that ran against a different one."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "conductor" / "app" / "cloud.py").read_text()
    body = src.split("def staging_verify(")[1].split("\ndef ")[0]
    assert "not" in body and "vouching for the wrong build" in body


@pytest.mark.hostonly
def test_an_instance_can_run_its_own_suite():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "conductor" / "app" / "routes.py").read_text()
    assert '@router.post("/internal/self-verify")' in src
    body = src.split('def self_verify(')[1].split("\n@router")[0]
    assert "r.returncode == 0" in body, "still judging the suite by its prose"


@pytest.mark.hostonly
def test_the_gate_judges_staging_by_an_exit_code():
    """It decided whether an image could become production by searching pytest's
    output for the word "failed" — judging prose, which this system refuses to do
    everywhere else. Staging now runs its own suite and reports returncode."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "conductor" / "app"
    cloud_src = (root / "cloud.py").read_text()
    routes = (root / "routes.py").read_text()
    body = cloud_src.split("def staging_verify(")[1].split("\ndef ")[0]
    assert '" failed" not in' not in body
    assert 'd.get("ok")' in body
    assert "r.returncode == 0" in routes


@pytest.mark.hostonly
def test_the_gate_no_longer_depends_on_a_kubernetes_exec():
    """The websocket path in the Python client is broken against urllib3 2.x, and
    its own error handler crashes decoding a None body — so a permissions problem,
    a misconfigured client and a working setup all reported the same
    "'NoneType' object has no attribute 'decode'". Four causes hid behind one
    message before the exec was removed."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "conductor" / "app" / "cloud.py").read_text()
    body = src.split("def staging_verify(")[1].split("\ndef ")[0]
    assert "connect_get_namespaced_pod_exec" not in body
    assert "httpx.post" in body
