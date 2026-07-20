"""Feature: Plan mode — the round table.

The seating rules are evidence-led (docs/ROUNDTABLE_DESIGN.md), so the tests
assert the *mechanisms*, not just that it runs: independent round 1, forced
dissent, equal turn-taking, and the homogeneity warning that tells a user when
their table is the configuration research says does not work.

No tokens are spent — the provider layer is stubbed.
"""

import json

import pytest

from conftest import make_project
from app import db, providers, roundtable


def _seat(name, provider="anthropic", model="m1", persona=""):
    return {"name": name, "provider": provider, "model": model, "persona": persona}


@pytest.fixture()
def table(fresh_db):
    tid = db.create_table(1, "build a thing", "t", "anthropic", "m1")
    for i, s in enumerate([_seat("A"), _seat("B", "openai", "gpt"), _seat("C", "google", "gem")]):
        db.add_seat(tid, i, s["name"], s["provider"], s["model"], s["persona"])
    return tid


# ---- the honesty check on table composition -------------------------------

def test_identical_seats_are_warned_about():
    seats = [{"provider": "anthropic", "model": "m"} for _ in range(4)]
    w = roundtable.homogeneity_warning(seats)
    assert "same" in w.lower() and "NOT" in w


def test_one_provider_many_models_is_a_softer_warning():
    seats = [{"provider": "anthropic", "model": m} for m in ("a", "b", "c")]
    w = roundtable.homogeneity_warning(seats)
    assert w and "one provider" in w.lower()


def test_mixed_providers_no_warning():
    seats = [{"provider": "anthropic", "model": "a"},
             {"provider": "openai", "model": "b"},
             {"provider": "google", "model": "c"}]
    assert roundtable.homogeneity_warning(seats) == ""


# ---- provider layer --------------------------------------------------------

def test_available_reflects_only_keys_the_user_has():
    assert providers.available({}) == []
    assert providers.available({"claude_oauth_token": "x"}) == ["anthropic"]
    got = providers.available({"anthropic_api_key": "a", "openai_api_key": "b",
                               "gemini_api_key": "c"})
    assert set(got) == {"anthropic", "openai", "google"}


@pytest.mark.asyncio
async def test_complete_refuses_without_credentials():
    with pytest.raises(providers.ProviderError) as e:
        await providers.complete("openai", "gpt-5", "sys", "hi", {})
    assert "no credentials" in str(e.value).lower()


# ---- the deliberation mechanics -------------------------------------------

@pytest.mark.asyncio
async def test_round_one_is_independent(table, monkeypatch):
    """No seat may see another seat's text in round 1 — that is what removes
    production blocking and first-speaker anchoring."""
    seen = []

    async def fake(provider, model, system, prompt, settings, max_tokens=2000):
        seen.append({"phase_prompt": prompt})
        return f"proposal from {model}"

    monkeypatch.setattr(providers, "complete", fake)
    monkeypatch.setattr("app.roundtable.providers.complete", fake)
    from app import auth
    monkeypatch.setattr(auth, "get_settings", lambda u: {"anthropic_api_key": "k",
                                                         "openai_api_key": "k",
                                                         "gemini_api_key": "k"})
    monkeypatch.setattr(auth, "get_user", lambda uid: {"id": 1, "is_root": 1, "settings": "{}"})

    await roundtable.run_table(table)
    turns = db.list_turns(table)
    round1 = [t for t in turns if t["phase"] == "propose"]
    assert len(round1) == 3                       # every seat spoke exactly once
    # the round-1 prompts must not contain any other seat's contribution
    first_prompts = [s["phase_prompt"] for s in seen[:3]]
    for p in first_prompts:
        assert "proposal from" not in p, "round 1 leaked another seat's text"


@pytest.mark.asyncio
async def test_every_seat_speaks_once_per_round(table, monkeypatch):
    async def fake(provider, model, system, prompt, settings, max_tokens=2000):
        if "ONLY valid JSON" in prompt:
            return json.dumps({"approach": "x", "team": [{"role": "backend", "count": 1}]})
        return "text"
    monkeypatch.setattr("app.roundtable.providers.complete", fake)
    from app import auth
    monkeypatch.setattr(auth, "get_settings", lambda u: {"anthropic_api_key": "k",
                                                         "openai_api_key": "k",
                                                         "gemini_api_key": "k"})
    monkeypatch.setattr(auth, "get_user", lambda uid: {"id": 1, "is_root": 1, "settings": "{}"})

    await roundtable.run_table(table)
    turns = db.list_turns(table)
    for phase in ("propose", "critique", "revise"):
        got = [t for t in turns if t["phase"] == phase]
        assert len(got) == 3, f"{phase}: expected one turn per seat, got {len(got)}"
        assert len({t["seat_id"] for t in got}) == 3, f"{phase}: a seat spoke twice"


@pytest.mark.asyncio
async def test_a_failing_seat_does_not_kill_the_table(table, monkeypatch):
    """One provider being down should silence that seat, not the deliberation."""
    async def fake(provider, model, system, prompt, settings, max_tokens=2000):
        if provider == "openai":
            raise providers.ProviderError("boom")
        if "ONLY valid JSON" in prompt:
            return json.dumps({"approach": "x", "team": []})
        return "ok"
    monkeypatch.setattr("app.roundtable.providers.complete", fake)
    from app import auth
    monkeypatch.setattr(auth, "get_settings", lambda u: {"anthropic_api_key": "k",
                                                         "openai_api_key": "k",
                                                         "gemini_api_key": "k"})
    monkeypatch.setattr(auth, "get_user", lambda uid: {"id": 1, "is_root": 1, "settings": "{}"})

    bp = await roundtable.run_table(table)
    assert bp is not None
    assert db.get_table(table)["status"] == "done"
    failed = [t for t in db.list_turns(table) if not t["ok"]]
    assert failed, "the broken seat should be recorded as unable to speak"


@pytest.mark.asyncio
async def test_table_fails_cleanly_when_no_seat_can_speak(table, monkeypatch):
    async def fake(*a, **k):
        raise providers.ProviderError("all down")
    monkeypatch.setattr("app.roundtable.providers.complete", fake)
    from app import auth
    monkeypatch.setattr(auth, "get_settings", lambda u: {"anthropic_api_key": "k"})
    monkeypatch.setattr(auth, "get_user", lambda uid: {"id": 1, "is_root": 1, "settings": "{}"})
    with pytest.raises(ValueError):
        await roundtable.run_table(table)
    assert db.get_table(table)["status"] == "failed"


# ---- blueprint parsing -----------------------------------------------------

def test_blueprint_parses_fenced_json():
    raw = 'Sure!\n```json\n{"approach": "a", "team": [{"role": "x", "count": 2}]}\n```\nDone.'
    bp = roundtable._parse_blueprint(raw)
    assert bp["approach"] == "a"
    assert bp["team"][0]["role"] == "x"


def test_blueprint_never_loses_prose_it_cannot_parse():
    bp = roundtable._parse_blueprint("I could not follow the format, but here is the plan…")
    assert bp["unparsed"] is True
    assert "could not follow" in bp["approach"]


# ---- routes ----------------------------------------------------------------

def test_table_is_private_to_its_owner(client, make_user, fresh_db):
    tid = db.create_table(1, "secret idea", "t")
    _uid, other = make_user("nosy")
    assert other.get(f"/api/tables/{tid}").status_code == 404
    assert client.get(f"/api/tables/{tid}").status_code == 401    # anonymous


def test_create_table_rejects_too_few_seats(root_client):
    r = root_client.post("/api/tables", json={
        "brief": "x", "seats": [_seat("A"), _seat("B")]})
    assert r.status_code == 400
    assert "at least" in r.json()["detail"]


def test_create_table_rejects_providers_without_keys(root_client, monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "get_settings", lambda u: {"anthropic_api_key": "k"})
    r = root_client.post("/api/tables", json={
        "brief": "x", "seats": [_seat("A"), _seat("B"), _seat("C", "openai", "gpt")]})
    assert r.status_code == 400
    assert "OpenAI" in r.json()["detail"]


def test_providers_endpoint_requires_login(client):
    assert client.get("/api/providers").status_code == 401
