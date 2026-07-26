"""World feature flags — the master switches, resolved in layers.

Not every world wants drama. A casino wants betrayal and bluffing; a dev team wants
competent professionals who don't have feelings about a code review. So behaviour is
governed by flags that resolve exactly the way `tuning.py` resolves knobs:

    WORLD default  <  SCENE override  <  AGENT override

`Flags` is immutable: `derive()` returns a new merged layer, never mutating. A disabled
group is checked with `on()` at the one place its behaviour lives, so turning drama off
does not merely hide it — it stops computing it. **Off is free.**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Every flag, grouped, with its default. Groups are documentation; the flat name is the
# key everything checks. Keep this the single source of truth for what can be toggled.
#
# Flags marked (reserved) are declared now — so a world can be authored with them and the
# preset table stays complete — but their behaviour lands in a later wave (gossip and
# deception ride on theory_of_mind; mortality/inheritance are the lifecycle wave). They are
# intentional forward-declarations, not dead code; `switch_drama_off` already flips them.
GROUPS: dict[str, dict[str, bool]] = {
    "learning": {"rule_compiler": True, "skill_growth": True},
    "affect":   {"emotions": True, "drives": True},
    "drama":    {"theory_of_mind": True, "mood_volatility": True,
                 "gossip": True, "deception": True, "self_deception": True},   # last 3 reserved
    "security": {"secrets_circles": True, "ledger": True},
    "life":     {"mortality": True, "inheritance": True},                      # reserved (lifecycle wave)
    "memory":   {"memory": True},
}

# A flat {flag: default} view, derived once.
DEFAULTS: dict[str, bool] = {f: v for g in GROUPS.values() for f, v in g.items()}

# Named bundles. `serious` is what `switch_drama_off` selects: the whole drama group off,
# no mortality, mood dampened — while learning, memory, skills and secrets stay fully on.
PRESETS: dict[str, dict[str, bool]] = {
    "sandbox": {},                                    # every default as-is
    "serious": {
        "theory_of_mind": False, "gossip": False, "deception": False,
        "self_deception": False, "mood_volatility": False,
        "mortality": False,
    },
    "theatre": {},                                    # defaults on; amplitude tuned elsewhere
}


@dataclass(frozen=True)
class Flags:
    """An immutable resolved flag layer. Build the world's, then `derive` a scene's, then
    an agent's — each call merges on top, so the most specific wins."""
    values: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def preset(cls, name: str = "sandbox", **overrides: bool) -> "Flags":
        base = dict(DEFAULTS)
        base.update(PRESETS.get(name, {}))
        base.update(overrides)
        return cls(base)

    def derive(self, overrides: dict[str, bool] | None) -> "Flags":
        """A new layer on top of this one. `switch_drama_off=True` is sugar for the
        whole serious bundle, so a scene or agent can flip it with one key."""
        merged = dict(self.values)
        for k, v in (overrides or {}).items():
            if k == "switch_drama_off" and v:
                merged.update(PRESETS["serious"])
            elif k in DEFAULTS:
                merged[k] = bool(v)
        return Flags(merged)

    def on(self, flag: str) -> bool:
        return bool(self.values.get(flag, DEFAULTS.get(flag, False)))

    def to_dict(self) -> dict[str, bool]:
        # Only the deltas from default, so a stored config stays small and readable.
        return {k: v for k, v in self.values.items() if v != DEFAULTS.get(k)}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Flags":
        return cls.preset("sandbox", **{k: bool(v) for k, v in (d or {}).items()
                                        if k in DEFAULTS})
