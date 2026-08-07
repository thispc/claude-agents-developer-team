"""The substrate's ONE door to the platform above it.

The lifeworld is a self-contained society: everything under this package speaks
in its own types and persists through its own store. But it lives inside a
platform — the register that shows an agent as busy, the knowledge base it
recalls from, the tuning knobs and session caps an operator sets — and each of
those is a reach upward, out of the substrate into the app.

The boundary rule this module enforces by existing: **the substrate may only
reach the platform through this file.** Every accessor is lazy — the import
happens inside the function — so importing the lifeworld never drags the
platform in with it, and the full list of what the substrate depends on fits on
one screen, here. A `from .. import` anywhere else in the package is a boundary
violation, not a convenience.
"""

from __future__ import annotations


def db():
    # Worlds persist as rows in the platform's SQLite; the substrate owns the blob, not the file.
    from .. import db as m
    return m


def providers():
    # Live mode wires the platform's one model-call door in; the substrate never imports a vendor SDK.
    from .. import providers as m
    return m


def tuning():
    # Runtime-tunable knobs (default model, token ceilings, session caps) are operator-owned.
    from .. import tuning as m
    return m


def agents():
    # The platform-wide activity register, so a lifeworld agent shows up beside workers and crew.
    from .. import agents as m
    return m


def knowledge():
    # The shared knowledge base an agent recalls from — free on the local backend.
    from .. import knowledge as m
    return m


def agent_key_for(kind: str, *parts) -> str:
    # An agent's identity in the register, minted by the register's own rule.
    from ..agents import key_for
    return key_for(kind, *parts)


def agent_get(key: str) -> dict:
    # One register row — what an agent is doing right now, trimmed for a canvas.
    from ..agents import get
    return get(key)


def knowledge_tokens(text: str) -> list[str]:
    # knowledge's own tokenizer, reused so "did this speaker HEAR that word" and
    # recall agree on what a word is. Private there by name; consumed here by contract.
    from ..knowledge import _tokens
    return _tokens(text)


def session_caps() -> tuple[int, int]:
    # The env-configured per-agent spend ceiling: (calls per window, window seconds).
    from ..config import AGENT_SESSION_CAP, AGENT_SESSION_WINDOW_S
    return AGENT_SESSION_CAP, AGENT_SESSION_WINDOW_S
