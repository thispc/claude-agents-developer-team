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
- `ports`    — the ONE door upward: the model door, knowledge, tuning, the agent
               register. Since P4 every accessor is an HTTP client, and a
               parent-package import still appears nowhere else under here — a
               rule the service's own smoke test checks literally.

Persistence is NOT here. `store.py` sits one level up, beside `app.py`, because
it opens a database and the substrate does not: an entity serialises to one JSON
blob and the service writes it. That is the whole of the change P4 made to this
package — everything below `ports.py` is the code that was in
`conductor/app/lifeworld/`, unchanged.

Everything free is free by construction; the single thing that can ever spend a token
is one bounded Tier-2 deliberation, gated by attention. A whole Lifeworld idles at zero.
"""

from .world import World
from .config import Flags, PRESETS
from .human import Human
from .artifact import Artifact, Card, Deck
from .scene import Scene

__all__ = ["World", "Flags", "PRESETS", "Human", "Artifact", "Card", "Deck", "Scene"]
