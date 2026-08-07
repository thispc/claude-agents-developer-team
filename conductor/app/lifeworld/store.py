"""Persistence — a whole World is one JSON blob, saved and loaded whole.

A Lifeworld is a playground read and rewritten per scan, not queried column by column, so
serialising the entire World (its cast, scenes, ledgers) to one row is the honest and
simplest fit. The store also decides the *runtime*: whether a loaded world can spend. By
default it loads FREE — the deterministic Tier-0 appraiser — so operating the Studio costs
nothing; pass `live=True` and the owner's settings to wire in the model appraiser (the one
bounded spend), for when you want the agents to genuinely think.
"""

from __future__ import annotations

import asyncio

import json
from typing import Any

from . import ports
from .config import Flags
from .world import World


def create(owner_id: int, name: str, preset: str = "sandbox") -> World:
    w = World(name=name, flags=Flags.preset(preset))
    wid = ports.db().create_lw_world(owner_id, name, "{}")
    w.id = wid
    save(w)
    return w


def owns(owner_id: int, world_id: int) -> bool:
    row = ports.db().get_lw_world(world_id)
    return bool(row and row["owner_id"] == owner_id)


def load(world_id: int, *, live: bool = False, settings: dict | None = None) -> World | None:
    row = ports.db().get_lw_world(world_id)
    if not row:
        return None
    runtime: dict[str, Any] = {}
    if live:
        providers, tuning = ports.providers(), ports.tuning()
        runtime = {"complete": providers.complete, "settings": settings or {},
                   "model_name": tuning.get("scene_default_model"),
                   "utter_tokens": int(tuning.get("scene_utterance_max_tokens"))}
    w = World.from_dict(json.loads(row["data"] or "{}"), **runtime)
    w.id = world_id
    return w


def save(world: World) -> None:
    ports.db().update_lw_world(world.id, json.dumps(world.to_dict()), name=world.name)


def listing(owner_id: int) -> list[dict]:
    return ports.db().list_lw_worlds(owner_id)


def delete(world_id: int) -> None:
    ports.db().delete_lw_world(world_id)


# One lock per world, so a load→await→save cycle cannot be interleaved with another.
#
# A World is deserialized fresh on every request and saved back as a whole blob, so two
# overlapping cycles are a lost update: the self-repair crew's sprint runs `load → await
# run_deliberation → save` on the engine's task while `POST /api/repair/chat` does the same
# round trip, and whichever finished last silently erased the other's memo, log and results.
# Both live in one event loop, so an asyncio lock is the right grain.
_WORLD_LOCKS: dict[int, asyncio.Lock] = {}


def lock_for(world_id: int) -> asyncio.Lock:
    """`async with store.lock_for(wid):` around any load…mutate…save."""
    lk = _WORLD_LOCKS.get(int(world_id))
    if lk is None:
        lk = _WORLD_LOCKS[int(world_id)] = asyncio.Lock()
    return lk
