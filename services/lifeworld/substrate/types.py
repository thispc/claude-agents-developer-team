"""The two value objects the whole Lifeworld speaks in.

A `Signal` is what reaches an entity: something happened. A `Packet` (the "X" of the
design docs) is the consequence: a bag of *proposed* deltas. Nothing else crosses the
boundary between the thinking part and the state — an appraiser (rules or a model)
returns a Packet, and clamped code applies it. Keeping this the only currency is what
makes the reflex arc uniform and the cost auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Message scopes — reused for both signals on the bus and sealed secrets.
PUBLIC, CIRCLE, DIRECT = "public", "circle", "direct"


@dataclass
class Signal:
    """Something an entity perceives. `sense` is the organ it arrives through (hearing
    by default, since most of the world is text); `intensity` and `stakes` seed the
    free salience pre-score that decides whether it is even worth a thought."""
    kind: str                              # scold | praise | deal | reveal | say | act …
    payload: dict[str, Any] = field(default_factory=dict)
    from_id: int | None = None             # who/what emitted it
    scope: str = PUBLIC
    sense: str = "hearing"
    intensity: float = 0.5                 # 0..1, how strong the stimulus is
    stakes: float = 0.5                    # 0..1, how much it matters to the receiver
    domain: str = ""                       # competency path this experience belongs to

    def text(self) -> str:
        return str(self.payload.get("text", "") or self.payload.get("gist", ""))


@dataclass
class Packet:
    """A consequence — the only thing an appraiser may return, and the only thing that
    changes an entity. Every field is a *delta* (add to current), except `understood`,
    `memory`, and `action` which are records. Deltas are clamped on apply; the packet
    itself is never trusted to be in range."""
    understood: str = ""                                   # the meaning, one line
    mood: dict[str, float] = field(default_factory=dict)   # confidence, stress, hope, focus
    traits: dict[str, float] = field(default_factory=dict) # the genome — moves a tenth as fast
    vitals: dict[str, float] = field(default_factory=dict) # energy, health
    drives: dict[str, float] = field(default_factory=dict) # need levels nudged
    memory: str = ""                                       # gist to (maybe) remember
    action: dict[str, Any] = field(default_factory=dict)   # {kind, text, target}
    social: dict[str, Any] = field(default_factory=dict)   # {agent_id: {trust, warmth, …}}
    tier: int = 0                                          # which tier produced this (0/1/2)
    spent: int = 0                                         # model calls billed (0 or 1)

    def is_noop(self) -> bool:
        return not any((self.mood, self.traits, self.vitals, self.drives,
                        self.memory, self.action, self.social))

    def to_dict(self) -> dict[str, Any]:
        return {"understood": self.understood, "mood": self.mood, "traits": self.traits,
                "vitals": self.vitals, "drives": self.drives, "memory": self.memory,
                "action": self.action, "social": self.social, "tier": self.tier,
                "spent": self.spent}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Packet":
        d = d or {}
        return cls(understood=d.get("understood", ""), mood=d.get("mood", {}) or {},
                   traits=d.get("traits", {}) or {}, vitals=d.get("vitals", {}) or {},
                   drives=d.get("drives", {}) or {}, memory=str(d.get("memory", "") or ""),
                   action=d.get("action", {}) or {}, social=d.get("social", {}) or {},
                   tier=int(d.get("tier", 0)), spent=int(d.get("spent", 0)))
