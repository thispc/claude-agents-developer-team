"""The psyche — traits (the genome), mood (the weather), vitals (the body).

Three timescales: traits barely move (they are who the agent *is*), mood moves freely
and relaxes back to a baseline on its own, vitals sit in between. Everything is a number
in [0, 1], every event is bounded, and homeostasis pulls mood home — so a person never
"differs into a crazy person". Salience weights are read straight off the traits, so what
an agent finds memorable is a fingerprint of who they are, not five magic constants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .util import clamp01, step

TRAITS = ("willpower", "risk_appetite", "composure", "curiosity",
          "sociability", "empathy", "conscientiousness")
VITALS = ("energy", "health")
MOOD = ("confidence", "stress", "hope", "focus")

MOOD_BASELINE = {"confidence": 0.5, "stress": 0.2, "hope": 0.6, "focus": 0.6}
MAX_STEP = {"traits": 0.03, "vitals": 0.12, "mood": 0.25}


@dataclass
class Psyche:
    traits: dict[str, float] = field(default_factory=lambda: {t: 0.5 for t in TRAITS})
    vitals: dict[str, float] = field(default_factory=lambda: {v: 0.8 for v in VITALS})
    mood: dict[str, float] = field(default_factory=lambda: dict(MOOD_BASELINE))

    @classmethod
    def newborn(cls, dials: dict[str, float] | None = None) -> "Psyche":
        p = cls()
        for name, raw in (dials or {}).items():
            if name in TRAITS:
                v = float(raw)
                p.traits[name] = clamp01(v / 100.0 if v > 1 else v)
        return p

    # --- the one mutation path: apply a clamped consequence -----------------

    def apply(self, mood: dict, vitals: dict, traits: dict, *, mood_volatility: bool = True) -> None:
        """Apply a packet's deltas, each bounded by its family cap. When mood_volatility
        is off (serious world) mood deltas are softened, not silenced — a professional
        still feels a sting, just doesn't spiral."""
        scale = 1.0 if mood_volatility else 0.4
        for var, d in (mood or {}).items():
            if var in self.mood:
                self.mood[var] = step(self.mood[var], d * scale, MAX_STEP["mood"])
        for var, d in (vitals or {}).items():
            if var in self.vitals:
                self.vitals[var] = step(self.vitals[var], d, MAX_STEP["vitals"])
        for var, d in (traits or {}).items():
            if var in self.traits:
                self.traits[var] = step(self.traits[var], d, MAX_STEP["traits"])

    def relax(self, rate: float = 0.1) -> None:
        """One beat of homeostasis — mood drifts toward baseline, so a bad moment fades."""
        for var, base in MOOD_BASELINE.items():
            self.mood[var] = clamp01(self.mood[var] + (base - self.mood[var]) * rate)

    # --- read helpers -------------------------------------------------------

    def salience_weights(self) -> dict[str, float]:
        """How this particular person scores what matters, drawn from their traits: a
        curious agent weights novelty; an empathetic one weights the social; a neurotic
        (low composure) one weights emotion."""
        return {
            "emotion": 0.6 + 0.8 * (1 - self.traits["composure"]),
            "novelty": 0.4 + 0.8 * self.traits["curiosity"],
            "social": 0.4 + 0.8 * self.traits["sociability"],
            "goal": 0.6,
            "surprise": 0.5 + 0.5 * self.traits["curiosity"],
        }

    def is_sane(self) -> bool:
        for pool in (self.traits, self.vitals, self.mood):
            for v in pool.values():
                if v != v or v < 0.0 or v > 1.0:
                    return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {"traits": dict(self.traits), "vitals": dict(self.vitals), "mood": dict(self.mood)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Psyche":
        d = d or {}
        return cls(traits={**{t: 0.5 for t in TRAITS}, **d.get("traits", {})},
                   vitals={**{v: 0.8 for v in VITALS}, **d.get("vitals", {})},
                   mood={**dict(MOOD_BASELINE), **d.get("mood", {})})
