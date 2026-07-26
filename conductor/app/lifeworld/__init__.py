"""The Lifeworld — a small society of living agents, separate from the projects engine.

This package is the implementation of docs/HUMAN_MODEL.md: humans (H) and artifacts
(A) that share one atom — input → pod → output that rewrites the pod — and differ only
in what fills the box. A Human has an inner life that learns; an Artifact is code that
holds state and secrets. They live in Scenes, time is counted in LLM scans, and the one
law is absolute: **the model proposes, the code disposes** — an LLM only ever proposes a
typed consequence packet, and clamped deterministic code applies it.

The design is deliberately layered:

- `config`   — the world's feature flags (switch_drama_off and friends), layered.
- `types`    — the value objects every layer speaks in (Signal, Packet).
- `entity`   — the inheritance spine: Entity → Being → Human, Entity → Artifact → …
- the mind   — psyche, senses, memory, skills, drives, rules, social, ledger: each a
               small composed subsystem with a clean interface, not a tangle.
- `scene`    — the setting, the cast, and the scan loop that advances time.
- `world`    — the facade the rest of the platform talks to.
- `store`    — persistence: an entity serialises to one JSON blob; scenes and the
               shared event log are rows. No ORM sprawl.

Everything free is free by construction; the single thing that can ever spend a token
is one bounded Tier-2 deliberation, gated by attention. A whole Lifeworld idles at zero.
"""

from .world import World
from .config import Flags, PRESETS
from .human import Human
from .artifact import Artifact, Card, Deck
from .scene import Scene

__all__ = ["World", "Flags", "PRESETS", "Human", "Artifact", "Card", "Deck", "Scene"]
