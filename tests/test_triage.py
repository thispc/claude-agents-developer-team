"""Triage: how much independence a self-repair issue gets.

The design is one asymmetry — rules set a floor, a model may only raise it. So
most of these tests are about what a persuasive or compromised classifier CANNOT
do, which is the property the whole thing rests on.
"""

import pytest

from app import triage


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
