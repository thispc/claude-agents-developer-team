"""Feature: verifying credentials in the Settings dialog.

"saved" only ever meant "we stored the characters you typed". These tests pin the
distinctions that actually matter to a user staring at the dialog: rejected vs
out-of-credit vs not-entitled vs merely busy.
"""

import json
import re
from pathlib import Path

import httpx
import pytest

from app import config, credcheck, providers

DASH = Path(config.__file__).resolve().parents[2] / "dashboard"


def _mock(handler):
    """Patch httpx.AsyncClient so a check talks to a scripted transport."""
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, *a, **k):
            k["transport"] = transport
            super().__init__(*a, **k)
    return Client


@pytest.fixture()
def http(monkeypatch):
    def _install(handler):
        monkeypatch.setattr(credcheck.httpx, "AsyncClient", _mock(handler))
    return _install


# ---- nothing to check ------------------------------------------------------

@pytest.mark.asyncio
async def test_blank_value_is_reported_not_checked():
    r = await credcheck.check("gemini_api_key", "")
    assert r["ok"] is False and "nothing to check" in r["detail"]


@pytest.mark.asyncio
async def test_unknown_kind_is_refused():
    assert (await credcheck.check("aws_secret", "x"))["ok"] is False


# ---- github ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_github_ok_reports_the_account(http):
    http(lambda req: httpx.Response(200, json={"login": "thispc"},
                                    headers={"x-oauth-scopes": "repo, workflow"}))
    r = await credcheck.check("github_token", "ghp_x")
    assert r["ok"] and "thispc" in r["detail"]


@pytest.mark.asyncio
async def test_github_missing_repo_scope_fails_the_check(http):
    """A token that authenticates but can't push is the nastiest kind: it looks
    fine until a worker tries to open a PR."""
    http(lambda req: httpx.Response(200, json={"login": "bob"},
                                    headers={"x-oauth-scopes": "read:user"}))
    r = await credcheck.check("github_token", "ghp_x")
    assert r["ok"] is False and "repo" in r["detail"]


@pytest.mark.asyncio
async def test_github_401_is_explained(http):
    http(lambda req: httpx.Response(401, json={}))
    r = await credcheck.check("github_token", "ghp_bad")
    assert r["ok"] is False and "401" in r["detail"] and r["hint"]


# ---- anthropic api key -----------------------------------------------------

@pytest.mark.asyncio
async def test_anthropic_key_ok(http):
    http(lambda req: httpx.Response(200, json={"content": []}))
    assert (await credcheck.check("anthropic_api_key", "sk-ant-x"))["ok"]


@pytest.mark.asyncio
async def test_anthropic_out_of_credit_is_distinguished_from_a_bad_key(http):
    """Both are 'it doesn't work', but only one is fixed by pasting a new key."""
    http(lambda req: httpx.Response(400, json={
        "error": {"message": "Your credit balance is too low"}}))
    r = await credcheck.check("anthropic_api_key", "sk-ant-x")
    assert r["ok"] is False
    assert "credit" in r["detail"] and "valid" in r["detail"]


# ---- gemini: entitlement is not throttling ---------------------------------

FREE_TIER_429 = {"error": {"message": (
    "You exceeded your current quota.\n* Quota exceeded for metric: "
    "generate_content_free_tier_requests, limit: 0, model: gemini-3.1-pro")}}


@pytest.mark.asyncio
async def test_gemini_reports_flash_working_and_pro_unentitled(http):
    def handler(req):
        if "pro" in str(req.url):
            return httpx.Response(429, json=FREE_TIER_429)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "OK"}]}}]})
    http(handler)
    r = await credcheck.check("gemini_api_key", "AIza")
    assert r["ok"], "flash works, so the key is usable"
    assert "flash — works" in r["detail"]
    assert "limit is 0" in r["detail"]
    assert "billing" in r["hint"].lower()


@pytest.mark.asyncio
async def test_gemini_key_that_answers_nothing_fails(http):
    http(lambda req: httpx.Response(429, json=FREE_TIER_429))
    r = await credcheck.check("gemini_api_key", "AIza")
    assert r["ok"] is False and "no model would answer" in r["detail"]


@pytest.mark.asyncio
async def test_gemini_bad_key_is_an_auth_error_not_a_quota_one(http):
    http(lambda req: httpx.Response(403, json={"error": {"message": "invalid"}}))
    r = await credcheck.check("gemini_api_key", "AIza-bad")
    assert r["ok"] is False and "rejected" in r["detail"]


@pytest.mark.asyncio
async def test_a_check_never_raises(http):
    def boom(req):
        raise RuntimeError("network exploded")
    http(boom)
    r = await credcheck.check("github_token", "x")
    assert r["ok"] is False and "check failed" in r["detail"]


# ---- the entitlement rule the provider layer shares ------------------------

def _resp(payload):
    return httpx.Response(429, json=payload,
                          request=httpx.Request("POST", "https://example.test"))


def test_limit_zero_is_recognised_as_an_entitlement_wall():
    assert providers.not_entitled(_resp(FREE_TIER_429)) is True


def test_an_ordinary_throttle_is_not():
    assert providers.not_entitled(_resp(
        {"error": {"message": "Quota exceeded, limit: 60, retry in 17s"}})) is False


@pytest.mark.asyncio
async def test_an_unentitled_model_is_not_retried(monkeypatch):
    """Waiting cannot fix 'your plan includes zero of this model'."""
    calls = {"n": 0}

    async def gone(model, system, prompt, settings, max_tokens):
        calls["n"] += 1
        raise httpx.HTTPStatusError("no", request=None, response=_resp(FREE_TIER_429))

    monkeypatch.setattr(providers, "_google", gone)
    with pytest.raises(providers.ProviderError) as e:
        await providers.complete("google", "gemini-pro-latest", "s", "p",
                                 {"gemini_api_key": "k"})
    assert calls["n"] == 1, "retried a wall that never comes down"
    assert "not entitled" in str(e.value) and "billing" in str(e.value)


# ---- the route -------------------------------------------------------------

def test_verify_requires_login(client):
    r = client.post("/api/settings/verify", json={"kind": "github_token", "value": "x"})
    assert r.status_code == 401


def test_verify_rejects_a_kind_that_is_not_a_credential(root_client):
    r = root_client.post("/api/settings/verify", json={"kind": "password", "value": "x"})
    assert r.status_code == 400


def test_verify_falls_back_to_the_stored_value(root_client, monkeypatch):
    """Blank input must check what's saved, so Check works long after entry."""
    seen = {}

    async def fake(kind, value):
        seen.update(kind=kind, value=value)
        return {"ok": True, "detail": "fine", "hint": ""}
    monkeypatch.setattr("app.routes.credcheck.check", fake)
    root_client.post("/api/settings", json={"gemini_api_key": "stored-key"})
    r = root_client.post("/api/settings/verify", json={"kind": "gemini_api_key"})
    assert r.status_code == 200 and r.json()["ok"]
    assert seen["value"] == "stored-key"


# ---- the dialog must actually wire every credential to a check -------------

def test_every_credential_field_has_a_check_button():
    html = (DASH / "index.html").read_text()
    form = html.split('id="settingsForm"', 1)[1].split("</form>", 1)[0]
    names = set(re.findall(r'<input[^>]*\bname="([^"]+)"', form))
    checks = set(re.findall(r'data-check="([^"]+)"', form))
    results = set(re.findall(r'data-result="([^"]+)"', form))
    assert names == checks == results, (
        f"a credential with no way to verify it: "
        f"inputs={names} checks={checks} results={results}")


def test_every_checkable_kind_is_one_the_backend_knows():
    html = (DASH / "index.html").read_text()
    checks = set(re.findall(r'data-check="([^"]+)"', html))
    assert checks <= set(credcheck.KINDS)


@pytest.mark.asyncio
async def test_gemini_invalid_key_is_reported_as_invalid_not_as_no_models(http):
    """Google answers a bad key with 400 INVALID_ARGUMENT, not 401 — so a typo'd
    key used to read 'key authenticated, but no model would answer'."""
    http(lambda req: httpx.Response(400, json={"error": {
        "status": "INVALID_ARGUMENT",
        "message": "API key not valid. Please pass a valid API key."}}))
    r = await credcheck.check("gemini_api_key", "AIza-not-a-real-key")
    assert r["ok"] is False
    assert "not valid" in r["detail"]
    assert "no model would answer" not in r["detail"]
