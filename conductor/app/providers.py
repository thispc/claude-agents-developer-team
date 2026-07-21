"""One interface over several model providers, so a round table can seat
Claude next to GPT next to Gemini.

This is not a convenience layer — it is the mechanism. Estornell & Liu (2025)
found multi-agent debate often fails to beat a single good model, and that the
one intervention which reliably helped was *model heterogeneity*: agents drawing
from different foundation models. A table of five identical models is theatre.

Anthropic goes through the Claude Agent SDK rather than raw HTTP, because that
path accepts a subscription OAuth token as well as an API key — the raw Messages
API accepts only an API key, and most users here are on a subscription. OpenAI
and Gemini are plain HTTPS calls; these are one-shot completions, not agent
loops, so pulling in two more SDKs would buy nothing.
"""

import asyncio
import os
import random
from typing import Any

import httpx

# name -> {label, needs (settings key), models}
PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Claude",
        "key_setting": "anthropic_api_key",     # or claude_oauth_token
        "models": [
            {"id": "claude-opus-4-8", "label": "Opus 4.8 — most capable"},
            {"id": "claude-sonnet-5", "label": "Sonnet 5 — balanced"},
            {"id": "claude-fable-5", "label": "Fable 5 — deepest reasoning"},
            {"id": "claude-haiku-4-5", "label": "Haiku 4.5 — fast & cheap"},
        ],
    },
    "openai": {
        "label": "OpenAI",
        "key_setting": "openai_api_key",
        "models": [
            {"id": "gpt-5", "label": "GPT-5"},
            {"id": "gpt-5-mini", "label": "GPT-5 mini — cheap"},
            {"id": "o3", "label": "o3 — reasoning"},
            {"id": "gpt-4o", "label": "GPT-4o"},
        ],
    },
    "google": {
        "label": "Gemini",
        "key_setting": "gemini_api_key",
        # The `-latest` aliases are stable pointers; pinned ids go away without
        # warning. `gemini-2.5-flash` was hard-coded here and had already been
        # retired — the ListModels endpoint still advertises it, but generateContent
        # answers "no longer available to new users", so a seat using it just died.
        # Pro-tier models 429 on the free tier; flash-tier works without billing.
        "models": [
            {"id": "gemini-flash-latest", "label": "Gemini Flash (latest) — free tier"},
            {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash"},
            {"id": "gemini-flash-lite-latest", "label": "Gemini Flash Lite — cheapest"},
            {"id": "gemini-pro-latest", "label": "Gemini Pro (latest) — needs a paid plan"},
        ],
    },
}

TIMEOUT = 180


class ProviderError(RuntimeError):
    """A seat could not produce a turn. Carries a message safe to show the boss."""


def catalog() -> list[dict]:
    """Provider/model list for the seat picker, with the settings key each needs."""
    return [{"id": name, **{k: v for k, v in p.items()}} for name, p in PROVIDERS.items()]


def available(settings: dict) -> list[str]:
    """Which providers this user actually has credentials for."""
    out = []
    if settings.get("anthropic_api_key") or settings.get("claude_oauth_token"):
        out.append("anthropic")
    if settings.get("openai_api_key"):
        out.append("openai")
    if settings.get("gemini_api_key"):
        out.append("google")
    return out


def key_for(provider: str, settings: dict) -> str:
    if provider == "anthropic":
        return settings.get("anthropic_api_key") or settings.get("claude_oauth_token") or ""
    if provider == "openai":
        return settings.get("openai_api_key", "")
    if provider == "google":
        return settings.get("gemini_api_key", "")
    return ""


# --------------------------------------------------------------------------


# Transient by nature: throttling and capacity. Anything else is a real error and
# retrying it just burns time and quota.
RETRY_STATUS = {408, 429, 500, 502, 503, 529}
RETRIES = 3


def _error_message(resp: httpx.Response) -> str:
    try:
        return resp.json().get("error", {}).get("message", "")[:300]
    except Exception:
        return resp.text[:200]


def not_entitled(resp: httpx.Response) -> bool:
    """A 429 that will never clear: the account is not allowed this model at all.

    Google returns the same RESOURCE_EXHAUSTED for "you are going too fast" and
    for "your free-tier allowance for this model is zero" — but the second says
    `limit: 0`, and no amount of waiting fixes it. Retrying it wastes the caller's
    time and teaches the platform to treat an entitlement wall as a throttle.
    """
    try:
        msg = resp.json().get("error", {}).get("message", "")
    except Exception:
        return False
    return "limit: 0" in msg


def _retry_after(resp: httpx.Response) -> float:
    """How long the provider asked us to wait, in seconds; 0 if it didn't say.

    Google doesn't use the Retry-After header — it buries the delay in
    error.details[].retryDelay as a duration string like "38s".
    """
    hdr = resp.headers.get("retry-after", "")
    if hdr.strip().replace(".", "", 1).isdigit():
        return float(hdr)
    try:
        for d in resp.json().get("error", {}).get("details", []):
            raw = str(d.get("retryDelay", ""))
            if raw.endswith("s") and raw[:-1].replace(".", "", 1).isdigit():
                return float(raw[:-1])
    except Exception:
        pass
    return 0.0


async def complete(provider: str, model: str, system: str, prompt: str,
                   settings: dict, max_tokens: int = 2000) -> str:
    """One completion from any provider. Raises ProviderError with a readable cause.

    Retries throttles and capacity blips. Without this a single 503 — which Gemini
    hands out freely on the free tier — permanently silenced a round-table seat, or
    dropped recruiting to the keyword heuristic, for a fault that clears in seconds.
    """
    if provider not in PROVIDERS:
        raise ProviderError(f"unknown provider {provider!r}")
    if not key_for(provider, settings):
        raise ProviderError(
            f"no credentials for {PROVIDERS[provider]['label']} — add a key in Settings")
    label = PROVIDERS[provider]["label"]

    for attempt in range(RETRIES):
        try:
            if provider == "anthropic":
                return await _anthropic(model, system, prompt, settings, max_tokens)
            if provider == "openai":
                return await _openai(model, system, prompt, settings, max_tokens)
            return await _google(model, system, prompt, settings, max_tokens)
        except ProviderError:
            raise
        except httpx.HTTPStatusError as e:
            status, body = e.response.status_code, _error_message(e.response)
            if status == 429 and not_entitled(e.response):
                raise ProviderError(
                    f"{label}: this account is not entitled to {model} (free-tier "
                    f"limit is 0). Enable billing on the key's project, or pick a "
                    f"model your plan includes.") from e
            if status not in RETRY_STATUS or attempt == RETRIES - 1:
                raise ProviderError(f"{label} returned {status}: {body}") from e
            # Honour the provider's own hint; otherwise exponential with jitter, so
            # concurrent seats don't retry in lockstep into the same capacity wall.
            wait = _retry_after(e.response) or (1.5 * 2 ** attempt)
            await asyncio.sleep(min(wait, 30) + random.random())
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt == RETRIES - 1:
                raise ProviderError(f"{label} was unreachable: {e}") from e
            await asyncio.sleep(1.5 * 2 ** attempt + random.random())
        except Exception as e:
            raise ProviderError(f"{label} call failed: {e}") from e
    raise ProviderError(f"{label} failed after {RETRIES} attempts")


async def _anthropic(model: str, system: str, prompt: str, settings: dict,
                     max_tokens: int) -> str:
    """Via the Agent SDK so a subscription OAuth token works, not just an API key."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    env = {}
    if settings.get("anthropic_api_key"):
        env["ANTHROPIC_API_KEY"] = settings["anthropic_api_key"]
        env["CLAUDE_CODE_OAUTH_TOKEN"] = ""
    elif settings.get("claude_oauth_token"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings["claude_oauth_token"]
        env["ANTHROPIC_API_KEY"] = ""

    text = ""
    async for msg in query(prompt=prompt, options=ClaudeAgentOptions(
            system_prompt=system, model=model, max_turns=1,
            # a seat reasons and writes; it must not touch the filesystem
            disallowed_tools=["Bash", "Write", "Edit", "Read", "Task", "TodoWrite",
                              "Glob", "Grep", "WebFetch", "WebSearch"],
            permission_mode="bypassPermissions", env=env)):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    text += b.text
    if not text.strip():
        raise ProviderError("Claude returned an empty response")
    return text.strip()


async def _openai(model: str, system: str, prompt: str, settings: dict,
                  max_tokens: int) -> str:
    key = settings["openai_api_key"]
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }
    # The reasoning models reject temperature and rename the token budget.
    if model.startswith(("o1", "o3", "o4", "gpt-5")):
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens
        body["temperature"] = 0.7
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post("https://api.openai.com/v1/chat/completions",
                         headers={"Authorization": f"Bearer {key}"}, json=body)
        r.raise_for_status()
        data = r.json()
    text = (data["choices"][0]["message"].get("content") or "").strip()
    if not text:
        raise ProviderError("OpenAI returned an empty response")
    return text


async def _google(model: str, system: str, prompt: str, settings: dict,
                  max_tokens: int) -> str:
    key = settings["gemini_api_key"]
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7},
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(url, headers={"x-goog-api-key": key}, json=body)
        r.raise_for_status()
        data = r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError):
        blocked = data.get("promptFeedback", {}).get("blockReason")
        raise ProviderError(f"Gemini returned no content{f' ({blocked})' if blocked else ''}")
    if not text:
        raise ProviderError("Gemini returned an empty response")
    return text
