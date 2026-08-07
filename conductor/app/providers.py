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

The three vendors above are the ones this file knows by name. Everything else a
user might own — a vLLM box on their own cluster, Ollama on a workstation, an
Azure OpenAI deployment, a LiteLLM proxy in front of Bedrock — arrives as a
*custom endpoint*: a base URL, an optional key, and a list of model ids. All of
them are driven through `POST {base}/chat/completions` in OpenAI's request shape,
which is the one wire format the whole ecosystem converged on. That is the entire
reason this is cheap: no new client code per vendor, and a server that speaks the
shape works the day it is configured rather than the day we get round to it.

What that deliberately does NOT cover is anything whose *authentication* is not a
static header. Bedrock's native runtime wants SigV4 request signing and Vertex
wants a short-lived OAuth2 access token minted from a service account; neither is
a string a user can paste into Settings and have keep working. Both are reachable
here only through something that already terminates that auth — Bedrock's own
OpenAI-compatible endpoint with a bearer API key, or a proxy the user runs. So
this file makes them *configurable*, and says nothing about having tested them.
"""

import asyncio
import os
import random
import re
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

# Custom endpoints live in the same per-user settings blob as the keys, under one
# JSON list, and every provider id derived from one is prefixed. The prefix is
# what keeps a user-supplied name from ever colliding with — or shadowing — a
# built-in: someone who calls their Ollama box "openai" gets `custom:openai`, and
# the real OpenAI keeps working.
CUSTOM_SETTING = "custom_endpoints"
CUSTOM_PREFIX = "custom:"

# Header names we will send a key under. Bearer is the OpenAI convention that
# almost everything copied; Azure OpenAI is the exception that matters, and it
# wants the raw key under `api-key` with no scheme word.
BEARER_HEADER = "Authorization"


class ProviderError(RuntimeError):
    """A seat could not produce a turn. Carries a message safe to show the boss."""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")[:40]


def _model_entry(raw: Any) -> dict | None:
    """Accept `"llama-3.1-70b"` or `{"id": ..., "label": ...}`.

    Users type model lists by hand and a bare string is what they type; refusing
    it would make the common case the one that needs documentation.
    """
    if isinstance(raw, str):
        return {"id": raw.strip(), "label": raw.strip()} if raw.strip() else None
    if isinstance(raw, dict) and (raw.get("id") or "").strip():
        return {"id": raw["id"].strip(), "label": (raw.get("label") or raw["id"]).strip()}
    return None


def normalise_endpoint(raw: dict) -> dict | None:
    """One stored endpoint, in the shape the rest of this file assumes.

    Returns None for anything without both an id and a base URL, because a
    half-written endpoint in the catalog is worse than an absent one: it appears
    in the picker, gets chosen, and fails at the moment work is dispatched.
    """
    if not isinstance(raw, dict):
        return None
    eid = _slug(raw.get("id") or raw.get("label") or "")
    base = (raw.get("base_url") or "").strip().rstrip("/")
    if not eid or not base:
        return None
    models = [m for m in (_model_entry(x) for x in (raw.get("models") or [])) if m]
    return {
        "id": eid,
        "label": (raw.get("label") or eid).strip(),
        "base_url": base,
        "api_key": raw.get("api_key") or "",
        "key_header": (raw.get("key_header") or BEARER_HEADER).strip() or BEARER_HEADER,
        "models": models,
    }


def custom_endpoints(settings: dict | None) -> list[dict]:
    """This user's own inference endpoints, bad rows dropped rather than raised."""
    raw = (settings or {}).get(CUSTOM_SETTING) or []
    if not isinstance(raw, list):
        return []
    return [e for e in (normalise_endpoint(r) for r in raw) if e]


def endpoint(provider: str, settings: dict | None) -> dict | None:
    """The endpoint behind a `custom:` provider id, or None if it isn't one."""
    if not provider.startswith(CUSTOM_PREFIX):
        return None
    want = provider[len(CUSTOM_PREFIX):]
    for e in custom_endpoints(settings):
        if e["id"] == want:
            return e
    return None


def catalog(settings: dict | None = None) -> list[dict]:
    """Provider/model list for the seat picker, with the settings key each needs.

    Custom endpoints are appended, never merged into the built-ins, and their keys
    are not included — this answer goes to a browser.
    """
    out = [{"id": name, **{k: v for k, v in p.items()}} for name, p in PROVIDERS.items()]
    for e in custom_endpoints(settings):
        out.append({
            "id": CUSTOM_PREFIX + e["id"],
            "label": e["label"],
            "custom": True,
            "base_url": e["base_url"],
            "key_setting": CUSTOM_SETTING,
            "models": e["models"],
        })
    return out


def available(settings: dict) -> list[str]:
    """Which providers this user actually has credentials for."""
    out = []
    if settings.get("anthropic_api_key") or settings.get("claude_oauth_token"):
        out.append("anthropic")
    if settings.get("openai_api_key"):
        out.append("openai")
    if settings.get("gemini_api_key"):
        out.append("google")
    # A configured endpoint counts as available even with no key. Ollama and a
    # plain vLLM server on a private network authenticate nobody, and demanding a
    # key would make the commonest self-hosted case impossible to configure —
    # exactly the users this exists for.
    out += [CUSTOM_PREFIX + e["id"] for e in custom_endpoints(settings)]
    return out


def key_for(provider: str, settings: dict) -> str:
    if provider == "anthropic":
        return settings.get("anthropic_api_key") or settings.get("claude_oauth_token") or ""
    if provider == "openai":
        return settings.get("openai_api_key", "")
    if provider == "google":
        return settings.get("gemini_api_key", "")
    return (endpoint(provider, settings) or {}).get("api_key", "")


def label_for(provider: str, settings: dict | None = None) -> str:
    """A name to put in front of a user. Falls back to the id rather than to
    nothing, so an error about an endpoint they deleted still says which one."""
    if provider in PROVIDERS:
        return PROVIDERS[provider]["label"]
    ep = endpoint(provider, settings)
    return ep["label"] if ep else provider


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
    ep = endpoint(provider, settings)
    if not ep and provider not in PROVIDERS:
        # Says "no longer configured" rather than "unknown", because for a custom
        # id the usual cause is an endpoint deleted out from under saved work — a
        # round table or a set of release notes that still names it.
        raise ProviderError(f"no provider or endpoint configured as {provider!r}")
    label = label_for(provider, settings)
    if not ep and not key_for(provider, settings):
        raise ProviderError(f"no credentials for {label} — add a key in Settings")

    for attempt in range(RETRIES):
        try:
            if ep:
                return await _openai_compatible(
                    ep["base_url"], ep["api_key"], ep["key_header"], model,
                    system, prompt, max_tokens, label)
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
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                                  TextBlock, query)

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
            # A seat reasons and writes; it gets NO tools at all. `disallowed_tools`
            # alone was not that: the CLI still advertised its remaining built-ins
            # and every MCP server from the box's own user config — and a model that
            # spends its single turn on a tool call (observed live: the graph
            # replan burned turn 1 on ToolSearch) dies as error_max_turns instead
            # of answering. tools=[] plus strict_mcp_config makes "one bounded
            # completion" structural rather than a hope.
            tools=[], strict_mcp_config=True,
            disallowed_tools=["Bash", "Write", "Edit", "Read", "Task", "TodoWrite",
                              "Glob", "Grep", "WebFetch", "WebSearch"],
            permission_mode="bypassPermissions", env=env)):
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    text += b.text
        elif isinstance(msg, ResultMessage):
            # Studio seats spend the same subscription quota as everything else on the
            # box; the self-repair crew's idleness check is only honest if it sees them.
            from . import usage
            usage.note_result(usage.current_source("studio"), model, msg)
    if not text.strip():
        raise ProviderError("Claude returned an empty response")
    return text.strip()


def chat_url(base_url: str) -> str:
    """Where the chat-completions call goes for a given base URL.

    Three shapes have to work, and the difference between them is not something a
    user should have to reason about:

    - `https://box.internal/v1` — vLLM, Ollama, LiteLLM. Append the path.
    - `https://x.openai.azure.com/openai/deployments/gpt4o?api-version=2024-10-21`
      — Azure carries the deployment in the path and the version in the query.
      Appending naively would put `/chat/completions` after the query string and
      produce a URL that 404s, so the query is detached and reattached.
    - a full `.../chat/completions` someone pasted from a curl example. Appending
      again would give `/chat/completions/chat/completions`.
    """
    base, sep, query = base_url.partition("?")
    base = base.rstrip("/")
    if not base.endswith("/chat/completions"):
        base += "/chat/completions"
    return base + sep + query


def auth_headers(key: str, header: str = BEARER_HEADER) -> dict[str, str]:
    """No key means no header at all — sending `Authorization: Bearer ` is not the
    same as sending nothing, and some servers reject the empty credential rather
    than treating the request as anonymous."""
    if not key:
        return {}
    if header.lower() == BEARER_HEADER.lower():
        return {BEARER_HEADER: f"Bearer {key}"}
    return {header: key}


async def _openai_compatible(base_url: str, key: str, key_header: str, model: str,
                             system: str, prompt: str, max_tokens: int,
                             label: str) -> str:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }
    # The reasoning models reject temperature and rename the token budget. Keyed on
    # the model id rather than the host, because these ids reach us through Azure
    # deployments and proxies too, where the URL says nothing about the model.
    if model.startswith(("o1", "o3", "o4", "gpt-5")):
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens
        body["temperature"] = 0.7
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        r = await c.post(chat_url(base_url), headers=auth_headers(key, key_header),
                         json=body)
        r.raise_for_status()
        data = r.json()
    try:
        text = (data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError):
        # A 200 whose body is not a chat completion is the signature of a base URL
        # pointing at something else entirely — a dashboard, a proxy's landing
        # page. Saying so beats a KeyError the user cannot act on.
        raise ProviderError(
            f"{label} answered, but not with an OpenAI-shaped completion — check "
            f"the base URL points at the API root")
    if not text:
        raise ProviderError(f"{label} returned an empty response")
    return text


async def _openai(model: str, system: str, prompt: str, settings: dict,
                  max_tokens: int) -> str:
    return await _openai_compatible("https://api.openai.com/v1",
                                    settings["openai_api_key"], BEARER_HEADER,
                                    model, system, prompt, max_tokens, "OpenAI")


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


def default_model(provider: str, settings: dict | None = None) -> str:
    """A sensible model when a caller has a provider but no specific model.

    Second in each list, not first: the top entry is the most capable and the most
    expensive, and something that runs on every task should not default to the
    priciest option available. An empty model string reaches the vendor API as a
    404 with an unhelpful message, so this exists to make "provider only" a valid
    thing to ask for.

    A custom endpoint takes the FIRST of its models instead. That ordering is a
    cost ranking we wrote; a user's list is whatever they typed, usually one model,
    and skipping past it to serve nothing would be a rule applied where it means
    nothing.
    """
    ep = endpoint(provider, settings)
    if ep:
        return ep["models"][0]["id"] if ep["models"] else ""
    models = PROVIDERS.get(provider, {}).get("models") or []
    if not models:
        return ""
    return models[min(1, len(models) - 1)]["id"]
