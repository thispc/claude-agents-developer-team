"""Does a credential actually work — right now, from this server?

A key that is *present* is not a key that *works*. The settings dialog reported
"saved", which only ever meant "we stored the characters you typed". A retired
model id, a Google project without billing, a GitHub token missing a scope and an
expired subscription token all looked identical to a working setup, and the first
sign of trouble was a project failing hours later with an error nobody connected
back to Settings.

Every check here makes the smallest real call that proves the credential can do
the thing the platform will ask of it. Cheap, but not free — so it runs when the
user asks for it, not on every page load.
"""

import asyncio
from typing import Any

import httpx

from . import config, github_client, providers

TIMEOUT = 20

# What the platform actually needs from each credential, in the user's terms.
KINDS = ("github_token", "anthropic_api_key", "claude_oauth_token",
         "openai_api_key", "gemini_api_key")

# A custom endpoint is checked by id, not by value: the thing being proved is a
# URL, a key and a model list together, and any one of them alone proves nothing.
ENDPOINT_KIND = "custom_endpoint"


def _r(ok: bool, detail: str, hint: str = "") -> dict[str, Any]:
    return {"ok": ok, "detail": detail, "hint": hint}


async def check(kind: str, value: str) -> dict[str, Any]:
    """Verify one credential. Never raises — a failed check is a result, not an error."""
    if kind not in KINDS:
        return _r(False, f"unknown credential {kind!r}")
    if not value:
        return _r(False, "nothing to check — no value stored or entered")
    try:
        if kind == "github_token":
            return await _github(value)
        if kind == "anthropic_api_key":
            return await _anthropic_key(value)
        if kind == "claude_oauth_token":
            return await _claude_subscription(value)
        if kind == "openai_api_key":
            return await _openai(value)
        return await _gemini(value)
    except asyncio.TimeoutError:
        return _r(False, "timed out", "the provider did not respond in time; try again")
    except Exception as e:                       # a check must never 500 the dialog
        return _r(False, f"check failed: {str(e)[:200]}")


async def check_endpoint(ep: dict) -> dict[str, Any]:
    """Verify a custom inference endpoint the same way a key is verified.

    An endpoint that silently does not work is indistinguishable from a broken
    key — the project fails hours later and nothing points back at Settings — so
    this makes the same real call the platform will make: one chat completion, in
    OpenAI's shape, against a model the user actually configured.

    The model list is fetched first where the server offers one, because the most
    common misconfiguration is not a dead server but a right server with a model
    id that is not on it, and "returns 404" is a far worse answer than "this
    server has llama-3.3-70b, not llama-3.1-70b".

    Never raises. A failed check is a result.
    """
    ep = providers.normalise_endpoint(ep) or {}
    if not ep:
        return _r(False, "this endpoint has no base URL")
    headers = providers.auth_headers(ep["api_key"], ep["key_header"])
    listed: list[str] = []
    list_note = ""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(ep["base_url"].partition("?")[0].rstrip("/") + "/models",
                            headers=headers)
        if r.status_code == 200:
            listed = [m.get("id", "") for m in (r.json().get("data") or []) if m.get("id")]
        elif r.status_code in (401, 403):
            return _r(False, f"{ep['label']} rejected the key ({r.status_code})",
                      "check the key, and whether this server wants it under a "
                      "header other than Authorization (Azure uses `api-key`)")
        else:
            # Azure exposes no /models at the deployment path and proxies often
            # disable it. Not being able to list is not a failure — it only means
            # the completion below is the whole of the evidence.
            list_note = f"no model list (GET /models returned {r.status_code})"
    except Exception as e:
        return _r(False, f"could not reach {ep['label']}: {str(e)[:150]}",
                  "check the base URL, and that this server is reachable from the "
                  "conductor — a private cluster address will not be")

    model = (ep["models"][0]["id"] if ep["models"]
             else (listed[0] if listed else ""))
    if not model:
        return _r(False, f"{ep['label']} is reachable but no model is configured",
                  "add at least one model id; the endpoint offered no list to pick from")

    try:
        text = await providers._openai_compatible(
            ep["base_url"], ep["api_key"], ep["key_header"], model,
            "Reply with OK.", "Reply with OK.", 16, ep["label"])
    except Exception as e:
        detail = str(e)[:250]
        hint = ""
        if listed and model not in listed:
            hint = (f"this server does not list {model} — it offers: "
                    f"{', '.join(listed[:6])}")
        elif "404" in detail:
            hint = ("a 404 usually means the base URL is missing its version "
                    "segment — most servers want it to end in /v1")
        return _r(False, f"{ep['label']} would not complete on {model}: {detail}", hint)

    unknown = [m["id"] for m in ep["models"] if listed and m["id"] not in listed]
    detail = f"{ep['label']} answered on {model} ({text.strip()[:40]})"
    if listed:
        detail += f" — {len(listed)} model(s) available"
    hint = list_note
    if unknown:
        # Only the first model was proved. Saying the rest are fine would be the
        # same lie this module exists to stop: a seat configured on one of them
        # dies at dispatch, hours later, with no trail back here.
        hint = f"configured but not offered by this server: {', '.join(unknown[:6])}"
    return {**_r(True, detail, hint), "models": listed}


async def _github(token: str) -> dict[str, Any]:
    # The configured host, not github.com: on a self-hosted server this check is
    # the only thing that can tell the user their token is for the wrong place,
    # and probing github.com would cheerfully pass a token that cannot reach the
    # server the agents will actually push to.
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get(f"{config.GIT_API}/user",
                        headers=github_client.auth_headers(token))
    if r.status_code == 401:
        return _r(False, "GitHub rejected this token (401)",
                  "it is expired, revoked, or was pasted incompletely")
    if r.status_code != 200:
        return _r(False, f"GitHub returned {r.status_code}")
    login = r.json().get("login", "?")
    # Classic tokens advertise their scopes; fine-grained ones return nothing here,
    # and their permissions can only be discovered by using them.
    scopes = r.headers.get("x-oauth-scopes", "")
    if scopes:
        missing = [s for s in ("repo",) if s not in scopes]
        if missing:
            return _r(False, f"authenticated as {login}, but missing scope: repo",
                      "without it the agents cannot push branches or open PRs")
        return _r(True, f"authenticated as {login} — classic token, scopes: {scopes[:80]}")
    return _r(True, f"authenticated as {login} — fine-grained token",
              "make sure it grants Contents, Issues and Pull requests read/write "
              "on the repos you'll use")


async def _anthropic_key(key: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
                         headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                         json={"model": "claude-haiku-4-5", "max_tokens": 1,
                               "messages": [{"role": "user", "content": "hi"}]})
    if r.status_code == 200:
        return _r(True, "Claude answered — API key valid (billed per token)")
    msg = ""
    try:
        msg = r.json().get("error", {}).get("message", "")[:200]
    except Exception:
        msg = r.text[:150]
    if r.status_code == 401:
        return _r(False, "Anthropic rejected this key (401)", msg)
    if r.status_code == 400 and "credit balance" in msg.lower():
        return _r(False, "the key is valid but the account is out of credit", msg)
    return _r(False, f"Anthropic returned {r.status_code}", msg)


async def _claude_subscription(token: str) -> dict[str, Any]:
    """Runs one real Agent-SDK turn, because that is the path workers use.

    Slower than an HTTP ping (it starts the bundled CLI), but a subscription token
    is only meaningful through the SDK — checking it any other way would verify a
    code path the platform never takes.
    """
    try:
        text = await asyncio.wait_for(
            providers._anthropic("claude-haiku-4-5", "Reply with OK.", "Reply with OK.",
                                 {"claude_oauth_token": token}, 16),
            timeout=90)
    except asyncio.TimeoutError:
        return _r(False, "the subscription check timed out after 90s",
                  "the token may be valid but the CLI was slow to start; try again")
    except Exception as e:
        detail = str(e)[:250]
        low = detail.lower()
        if "oauth" in low or "401" in low or "unauthor" in low or "expired" in low:
            return _r(False, "the subscription token was rejected",
                      "re-run `claude setup-token` and paste the new value")
        return _r(False, f"the subscription check failed: {detail}")
    return _r(True, f"Claude answered on your subscription — no per-token billing "
                    f"({text.strip()[:40]})")


async def _openai(key: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.get("https://api.openai.com/v1/models",
                        headers={"Authorization": f"Bearer {key}"})
    if r.status_code == 401:
        return _r(False, "OpenAI rejected this key (401)")
    if r.status_code != 200:
        return _r(False, f"OpenAI returned {r.status_code}")
    ids = {m["id"] for m in r.json().get("data", [])}
    usable = [m["id"] for m in providers.PROVIDERS["openai"]["models"] if m["id"] in ids]
    if not usable:
        return _r(True, f"key valid ({len(ids)} models), but none of the models this "
                        f"platform offers are available to it",
                  "the account may need a paid plan")
    return _r(True, f"key valid — can use {', '.join(usable[:4])}")


def _bad_api_key(resp: httpx.Response) -> bool:
    """Google reports an invalid key as 400 INVALID_ARGUMENT, not 401."""
    try:
        return "api key not valid" in resp.json().get("error", {}).get("message", "").lower()
    except Exception:
        return False


async def _gemini(key: str) -> dict[str, Any]:
    """Checks auth AND entitlement, because on Gemini they fail differently.

    Google answers the same RESOURCE_EXHAUSTED for "slow down" and for "your plan
    includes zero of this model" — the second says `limit: 0` and never clears.
    Reporting them the same way is how a billing problem masquerades as a blip.
    """
    tiers = [("flash", "gemini-flash-lite-latest"), ("pro", "gemini-pro-latest")]
    results: dict[str, str] = {}
    auth_failed = ""
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        for tier, model in tiers:
            try:
                r = await c.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent",
                    headers={"x-goog-api-key": key},
                    json={"contents": [{"role": "user", "parts": [{"text": "Reply OK"}]}],
                          "generationConfig": {"maxOutputTokens": 2000}})
            except Exception as e:
                results[tier] = f"unreachable ({str(e)[:60]})"
                continue
            if r.status_code == 200:
                results[tier] = "works"
            elif r.status_code in (401, 403) or _bad_api_key(r):
                # Google answers a bad key with 400 INVALID_ARGUMENT, not 401 — so
                # a typo'd key looked like "authenticated, but no model would answer".
                auth_failed = "Google rejected this key — it is not valid"
            elif r.status_code == 429 and providers.not_entitled(r):
                results[tier] = "not included in this plan (free-tier limit is 0)"
            elif r.status_code == 429:
                results[tier] = "rate limited right now"
            elif r.status_code == 503:
                results[tier] = "the model is busy — transient, retried automatically"
            else:
                results[tier] = f"returned {r.status_code}"

    if auth_failed:
        return _r(False, auth_failed, "check the key was copied in full from AI Studio")
    if not any(v == "works" for v in results.values()):
        return _r(False, "key authenticated, but no model would answer: "
                         + "; ".join(f"{k} — {v}" for k, v in results.items()))
    line = "; ".join(f"{k} — {v}" for k, v in results.items())
    hint = ""
    if "limit is 0" in results.get("pro", ""):
        hint = ("Pro models need billing enabled on the key's Google Cloud project. "
                "Flash-tier planning works without it.")
    return _r(True, line, hint)
