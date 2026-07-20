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
        "models": [
            {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
            {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash — cheap"},
            {"id": "gemini-2.0-flash", "label": "Gemini 2.0 Flash"},
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


async def complete(provider: str, model: str, system: str, prompt: str,
                   settings: dict, max_tokens: int = 2000) -> str:
    """One completion from any provider. Raises ProviderError with a readable cause."""
    if provider not in PROVIDERS:
        raise ProviderError(f"unknown provider {provider!r}")
    if not key_for(provider, settings):
        raise ProviderError(
            f"no credentials for {PROVIDERS[provider]['label']} — add a key in Settings")
    try:
        if provider == "anthropic":
            return await _anthropic(model, system, prompt, settings, max_tokens)
        if provider == "openai":
            return await _openai(model, system, prompt, settings, max_tokens)
        return await _google(model, system, prompt, settings, max_tokens)
    except ProviderError:
        raise
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.json().get("error", {}).get("message", "")
        except Exception:
            body = e.response.text[:200]
        raise ProviderError(
            f"{PROVIDERS[provider]['label']} returned {e.response.status_code}: {body}") from e
    except Exception as e:
        raise ProviderError(f"{PROVIDERS[provider]['label']} call failed: {e}") from e


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
