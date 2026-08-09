"""Drives — the *why* behind behaviour.

Traits say how an agent reacts; drives say what it wants. Each is a homeostatic setpoint;
deviation from it is pressure, and the largest pressure names the goal the agent seeks
next. Drives run on different clocks (energy fast, purpose slow) and obey a Maslow-lite
prerequisite: a starving or unsafe agent does not chase esteem. Pure arithmetic — the
motivation is free; interoception (a sense) is what lets the agent *feel* it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .util import clamp01

# name -> (setpoint, drift-per-scan toward empty, prerequisite tier). Lower tiers gate
# higher ones: while safety/energy are unmet, social/esteem/curiosity/purpose are muted.
SPEC = {
    "energy":    (0.8, 0.02, 0),
    "safety":    (0.9, 0.005, 0),
    "social":    (0.6, 0.01, 1),
    "esteem":    (0.6, 0.006, 2),
    "curiosity": (0.5, 0.012, 2),
    "purpose":   (0.7, 0.003, 3),
}


@dataclass
class Drives:
    level: dict[str, float] = field(default_factory=lambda: {k: SPEC[k][0] for k in SPEC})

    def tick(self) -> None:
        """One scan of natural drift — needs deplete toward empty at their own rate."""
        for k, (_sp, drift, _t) in SPEC.items():
            self.level[k] = clamp01(self.level[k] - drift)

    def apply(self, deltas: dict[str, float]) -> None:
        for k, d in (deltas or {}).items():
            if k in self.level:
                self.level[k] = clamp01(self.level[k] + float(d))

    def pressures(self) -> dict[str, float]:
        """Unmet need per drive, gated by prerequisites: a higher drive's pressure is
        damped while a lower one is starving."""
        lower_ok = 1.0
        out: dict[str, float] = {}
        for k in SPEC:                       # SPEC is ordered by tier
            sp, _drift, tier = SPEC[k]
            raw = max(0.0, sp - self.level[k])
            out[k] = raw * (lower_ok if tier > 0 else 1.0)
            if tier == 0:                    # foundational needs gate everything above
                lower_ok = min(lower_ok, self.level[k] / max(sp, 1e-6))
        return out

    def dominant_goal(self) -> tuple[str, float]:
        p = self.pressures()
        k = max(p, key=p.get)
        return k, round(p[k], 3)

    def to_dict(self) -> dict[str, Any]:
        return {"level": dict(self.level)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Drives":
        return cls(level={**{k: SPEC[k][0] for k in SPEC}, **(d or {}).get("level", {})})
