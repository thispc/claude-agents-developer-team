"""The senses — pluggable input organs, and the attention gate.

Perception arrives through organs, each with a reach and a health that history wears
down. Hearing (text) is always on because most of the world is text; the rest are opt-in
per scene, so a plain conversation carries no sight/smell machinery. The most important
thing here is the **attention gate**: a cheap pre-score decides whether a signal is even
worth a thought. Most of what happens to a person is ignored, and ignoring is free — this
is the single biggest cost lever in the whole model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .types import Signal

ALL_SENSES = ("hearing", "sight", "touch", "smell", "taste", "interoception")
ATTENTION_FLOOR = 0.18       # below this a signal updates the buffer and dies


@dataclass
class Senses:
    active: list = field(default_factory=lambda: ["hearing", "interoception"])
    organ: dict = field(default_factory=dict)     # sense -> health 0..1 (default 1.0)

    def health(self, sense: str) -> float:
        return float(self.organ.get(sense, 1.0))

    def receive(self, signal: Signal) -> Signal | None:
        """Let a signal in if the sense is active and the organ can still register it.
        A worn organ dampens intensity — an old nose smells less."""
        if signal.sense not in self.active:
            return None
        h = self.health(signal.sense)
        if h <= 0.05:
            return None
        signal.intensity = max(0.0, min(1.0, signal.intensity * h))
        return signal

    def attention(self, signal: Signal, drive_relevance: float = 0.0) -> float:
        """How much this signal deserves a thought. Drives sharpen it — a hungry agent
        notices food — so the gate is need-aware, not a fixed cutoff."""
        return max(0.0, min(1.0, 0.6 * signal.intensity * signal.stakes
                            + 0.4 * signal.intensity + 0.3 * drive_relevance))

    def wear(self, sense: str, amount: float = 0.0005) -> None:
        self.organ[sense] = max(0.0, self.health(sense) - amount)

    def to_dict(self) -> dict[str, Any]:
        return {"active": list(self.active), "organ": dict(self.organ)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Senses":
        d = d or {}
        return cls(active=list(d.get("active", ["hearing", "interoception"])),
                   organ=dict(d.get("organ", {})))
