"""The substrate's ONE door to the platform above it — now a set of CLIENTS.

The boundary rule is unchanged and still enforced by this file existing: **the
substrate may only reach the platform through this module.** `from ..` appears
nowhere else under `substrate/`. What changed in P4 is the other side of the
door. The platform is no longer an import away; it is another process, so each
accessor here is an HTTP client instead of a lazy `from .. import`:

    knowledge()   → the knowledge SERVICE (P1, 8881), called directly. A service
                    that asked the conductor to ask knowledge would be two hops
                    for one answer and a coupling nobody needs.
    providers()   → the conductor's POST /internal/complete — the MODEL DOOR.
                    The substrate sends {provider, model, system, prompt,
                    max_tokens, source, settings_ref} and gets text back. It
                    never sees a key: `settings_ref` is an opaque string the
                    CONDUCTOR minted and only the conductor can resolve, so a
                    credential never enters this process. That is the invariant
                    the whole extraction is judged on.
    tuning()      → the conductor's GET /internal/tuning, per-knob allowlisted in
                    services.yaml. Cached briefly, and STALE-BUT-REAL beats
                    DEFAULT-BUT-WRONG on a refresh failure (P2's lesson).
    agents()      → the conductor's /internal/agents doors: the platform-wide
                    activity register, so a lifeworld agent still shows up beside
                    workers and crew on one board.
    db()          → GONE. Worlds live in this service's own data/lifeworld.db and
                    `store.py` (one level up, beside app.py) is the only thing
                    that opens it. Persistence is not the substrate's business
                    and never was — it only looked that way while both lived in
                    one process.

EVERYTHING HERE IS BEST-EFFORT. A register that cannot be written, a knob that
cannot be read, a lesson that cannot be recalled: none of them may stop an agent
from thinking. Each accessor degrades to the same shape the in-process module
returned when it failed, so the substrate above cannot tell the difference — the
one exception being the model door, which raises like a provider error always
did, because a scene that silently stopped spending would look like a scene that
had nothing to say.

CONFIG lives in module attributes, read from the environment at import. Tests
override the attributes (and inject `TRANSPORT`) rather than the environment, so
two mounts of this service in one interpreter — the conductor's suite and this
service's own — never fight over an env var that was restored under them.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

CONDUCTOR_URL = (os.environ.get("CONDUCTOR_URL") or "").strip().rstrip("/")
KNOWLEDGE_URL = (os.environ.get("KNOWLEDGE_URL") or "").strip().rstrip("/")
# Peer tokens arrive as env, minted by tools/gen_fleet.py — this service never
# reads another service's directory (SERVICE_CONTRACT rule 9).
KNOWLEDGE_TOKEN = (os.environ.get("KNOWLEDGE_TOKEN") or "").strip()
SERVICE_TOKEN = (os.environ.get("SERVICE_TOKEN") or "").strip()

TIMEOUT = 2.0            # every door but the model one: a localhost round trip
MODEL_TIMEOUT = 180.0    # the model door carries a real completion behind it

# Tests inject httpx transports here; the client code path stays identical.
TRANSPORT: httpx.AsyncBaseTransport | None = None            # → the conductor
SYNC_TRANSPORT: httpx.BaseTransport | None = None            # → the conductor (sync)
KNOWLEDGE_TRANSPORT: httpx.AsyncBaseTransport | None = None  # → the knowledge service

KNOB_TTL = 30.0          # seconds; tests set 0 to read through
_KNOBS: dict[str, tuple[float, Any]] = {}

# The hard floors the conductor's own config declares (AGENT_SESSION_CAP,
# AGENT_SESSION_WINDOW_S). Used only when the tuning door cannot be reached at
# all — an agent whose session ceiling is unknown gets the documented default,
# never "no ceiling".
SESSION_CAP_DEFAULT = 30
SESSION_WINDOW_DEFAULT = 5 * 3600


def _headers() -> dict:
    return {"X-Service-Token": SERVICE_TOKEN}


# --- the model door ----------------------------------------------------------

class _Providers:
    """`complete(provider, model, system, prompt, settings, max_tokens=…)` — the
    exact signature the substrate has always injected, so world.py, appraise.py
    and authoring.py are untouched by the extraction.

    `settings` is an OPAQUE REFERENCE here, not a credential bag. The world is
    built with a `settings_ref` string the conductor minted (`user:7`, `root`),
    it travels through the substrate exactly where the settings dict used to,
    and the conductor resolves it on the far side of the door. Nothing in this
    process can turn one into a key.
    """

    def __init__(self, source: str = "studio"):
        self.source = source

    async def complete(self, provider: str, model: str, system: str, prompt: str,
                       settings: Any = None, max_tokens: int = 2000,
                       source: str = "") -> str:
        if not CONDUCTOR_URL and TRANSPORT is None:
            raise RuntimeError("CONDUCTOR_URL is not set — no model door to call")
        body = {"provider": provider or "anthropic", "model": model or "",
                "system": system or "", "prompt": prompt or "",
                "max_tokens": int(max_tokens or 0),
                "source": source or self.source or "studio",
                "settings_ref": settings if isinstance(settings, str) else ""}
        async with httpx.AsyncClient(base_url=CONDUCTOR_URL, timeout=MODEL_TIMEOUT,
                                     transport=TRANSPORT, headers=_headers()) as c:
            r = await c.post("/internal/complete", json=body)
            if r.status_code >= 400:
                # The conductor answers a provider failure as 502 + {detail}. Raised,
                # not swallowed: every caller in the substrate already has a free
                # fallback for a raising `complete`, and a door that returned "" would
                # look like a model with nothing to say.
                raise RuntimeError(_detail(r))
            return str(r.json().get("text") or "")


def _detail(r: httpx.Response) -> str:
    try:
        d = r.json()
        return str(d.get("detail") or d)[:300]
    except Exception:
        return f"the model door answered {r.status_code}"


def providers(source: str = "studio") -> _Providers:
    return _Providers(source)


# --- the knowledge service ---------------------------------------------------

class _Knowledge:
    """recall / remember over the knowledge service's own contract.

    NO EMBEDDING KEY. knowledge's remote backend is chosen by a key that rides
    the request (SERVICE_CONTRACT rule 4) and the conductor is where that key
    lives, so a lifeworld recall uses knowledge's free local backend. The service
    re-embeds a row locally when the backends differ, so rows written by the
    conductor with a real embedder are still found here — coarser, never absent,
    and worth it to keep this process credential-free.
    """

    async def recall(self, owner: str, query: str, k: int = 5, *, kind: str = "",
                     settings: Any = None, include_global: bool = True) -> list[dict]:
        if not str(query or "").strip():
            return []
        try:
            async with self._client() as c:
                r = await c.post("/recall", json={
                    "owner": owner, "query": query, "k": max(1, min(int(k), 25)),
                    "kind": kind, "include_global": bool(include_global),
                    "settings": {}})
                r.raise_for_status()
                return list(r.json().get("hits") or [])
        except Exception:
            return []                      # never blocks a thought

    async def remember(self, owner: str, cue: str, says: str, *, kind: str = "belief",
                       sig: str = "", payload: dict | None = None, good: int = 0,
                       bad: int = 0, settings: Any = None) -> int:
        try:
            async with self._client() as c:
                r = await c.post("/remember", json={
                    "owner": owner, "cue": str(cue or ""), "says": str(says or ""),
                    "kind": kind, "sig": sig, "payload": payload or {},
                    "good": int(good), "bad": int(bad), "settings": {}})
                r.raise_for_status()
                return int(r.json().get("id") or 0)
        except Exception:
            return 0

    @staticmethod
    def _client() -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=KNOWLEDGE_URL, timeout=TIMEOUT,
                                 transport=KNOWLEDGE_TRANSPORT,
                                 headers={"X-Service-Token": KNOWLEDGE_TOKEN})


_KNOWLEDGE = _Knowledge()


def knowledge() -> _Knowledge:
    return _KNOWLEDGE


# --- the tuning door ---------------------------------------------------------

class _Tuning:
    """One knob, from the conductor, cached for KNOB_TTL.

    SYNC on purpose: `Human._session_params` is a plain method on the free scan
    path and always has been. The call is a localhost round trip bounded at two
    seconds, and a miss keeps the last real value rather than reverting to a
    baked default that could silently re-widen a ceiling the owner narrowed.
    """

    def get(self, name: str) -> Any:
        hit = _KNOBS.get(name)
        if hit and (time.time() - hit[0]) < KNOB_TTL:
            return hit[1]
        try:
            with httpx.Client(base_url=CONDUCTOR_URL, timeout=TIMEOUT,
                              transport=SYNC_TRANSPORT, headers=_headers()) as c:
                r = c.get("/internal/tuning", params={"name": name})
                r.raise_for_status()
                value = r.json()["value"]
            _KNOBS[name] = (time.time(), value)
            return value
        except Exception:
            if hit:
                return hit[1]
            raise                      # the caller's own fallback decides


_TUNING = _Tuning()


def tuning() -> _Tuning:
    return _TUNING


def session_caps() -> tuple[int, int]:
    return SESSION_CAP_DEFAULT, SESSION_WINDOW_DEFAULT


# --- the activity register ---------------------------------------------------

class _Agents:
    """note / done / working / get against the conductor's /internal/agents doors.

    Fire-and-forget by construction, exactly like the in-process register was: a
    board that can break the work it is describing is worse than no board.
    """

    def note(self, key: str, state: str, what: str = "", **fields) -> dict:
        row = {"key": key, "state": state, "what": str(what)[:160],
               "fields": {k: v for k, v in (fields or {}).items()
                          if isinstance(v, (str, int, float, bool)) or v is None}}
        try:
            with httpx.Client(base_url=CONDUCTOR_URL, timeout=TIMEOUT,
                              transport=SYNC_TRANSPORT, headers=_headers()) as c:
                r = c.post("/internal/agents/note", json=row)
                r.raise_for_status()
                return r.json()
        except Exception:
            return {"state": state, "what": row["what"]}

    def done(self, key: str, what: str = "") -> dict:
        return self.note(key, "idle", what)

    def get(self, key: str) -> dict:
        idle = {"state": "idle", "busy": False, "what": "", "stale": False,
                "for_s": 0, "means": "not doing anything"}
        try:
            with httpx.Client(base_url=CONDUCTOR_URL, timeout=TIMEOUT,
                              transport=SYNC_TRANSPORT, headers=_headers()) as c:
                r = c.get(f"/internal/agents/{key}")
                r.raise_for_status()
                return r.json()
        except Exception:
            return idle

    def working(self, key: str, state: str, what: str = "", **fields):
        return _Working(self, key, state, what, fields)


class _Working:
    """`with agents.working(key, "thinking", …):` — note, then always release.
    The failure that matters is the one where the work raises: an agent that threw
    halfway through must not be left claiming to think."""

    def __init__(self, reg: _Agents, key: str, state: str, what: str, fields: dict):
        self.reg, self.key, self.state, self.what, self.fields = reg, key, state, what, fields

    def __enter__(self):
        self.reg.note(self.key, self.state, self.what, **self.fields)
        return self

    def __exit__(self, *exc):
        self.reg.done(self.key)
        return False


_AGENTS = _Agents()


def agents() -> _Agents:
    return _AGENTS


def agent_key_for(kind: str, *parts) -> str:
    """An agent's identity in the register. Minted HERE rather than asked for:
    `lw:3:14` is world 3's agent 14 forever, the rule is two lines long, and a
    round trip to learn a string this process already knows would be a door for
    the sake of having one. The conductor's agents.key_for is the same join."""
    return ":".join([kind, *[str(p) for p in parts]])


def agent_get(key: str) -> dict:
    return _AGENTS.get(key)


def knowledge_tokens(text: str) -> list[str]:
    """knowledge's own tokenizer, over its POST /tokens contract, so "did this
    speaker HEAR that word" and recall agree on what a word is. Sync because the
    leak check runs inside the host's line-by-line pass; [] on failure, which
    makes the check find no leaks rather than invent one."""
    try:
        with httpx.Client(base_url=KNOWLEDGE_URL, timeout=TIMEOUT,
                          transport=_sync_knowledge_transport(),
                          headers={"X-Service-Token": KNOWLEDGE_TOKEN}) as c:
            r = c.post("/tokens", json={"text": str(text or "")})
            r.raise_for_status()
            return list(r.json().get("tokens") or [])
    except Exception:
        return []


SYNC_KNOWLEDGE_TRANSPORT: httpx.BaseTransport | None = None


def _sync_knowledge_transport():
    return SYNC_KNOWLEDGE_TRANSPORT


def db():
    """Deliberately absent. Worlds are this service's own rows now — `store.py`
    beside app.py is the only opener of data/lifeworld.db, and the substrate does
    not persist anything. Left as a loud failure rather than deleted so a
    `from . import ports; ports.db()` copied in from the old package cannot
    quietly find a conductor connection that no longer exists."""
    raise RuntimeError(
        "the substrate has no database door — persistence lives in the service's "
        "store.py (data/lifeworld.db), and nothing under substrate/ may open it")
