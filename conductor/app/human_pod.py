"""H — a human pod: input → (appraise) → a consequence packet → a changed self.

A HumanPod is the owner's atom made concrete: `input → pod → output`, where the
output (the consequence packet X) also rewrites the pod. It wraps a `Psyche` (the
biology), a position and a body-state in the world, and a bounded memory. One method
matters — `perceive(event)` — and it is the whole loop:

    1. an event arrives (a scold, an empty chair, a win)
    2. it is APPRAISED into X — a typed packet of deltas, a memory gist, an action
    3. X is APPLIED through psyche.apply_deltas, which CLAMPS every change

The appraisal is the only step that could ever cost a token, and it does not have
to: the default appraiser is a free, deterministic rule table, gated by the person's
own traits, so the same scold wounds a fragile person and glances off a composed one
— with no model in the loop. An LLM appraiser is available for open-ended events and
funnels through a single choke point (`_appraise_with_model`), exactly like every
other spend on the platform. Tests run entirely on the free path.

X is DATA, never code. It changes the person's *state*, never their *program* — the
line that keeps this cheap, stable, and impossible to turn into an RCE (the door held
shut across docs/PATENTABILITY.md and the effects registry).
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from . import providers, psyche as psyche_mod, tuning

MEMORY_CAP = 50   # a person remembers the recent, not everything — bounded by design

# The free, deterministic appraisal rules. Each maps an event KIND to the mood/vital
# consequence it has, GATED by the person's expressed traits, so who they are decides
# how much it lands. This is the "human psychology in variables" layer doing real
# work with zero inference — the fast loop from docs/SELF_LEARNING_SUBSTRATE.md §6.
def _rule_appraise(p: "psyche_mod.Psyche", event: dict) -> dict:
    kind = event.get("kind", "")
    i = float(event.get("intensity", 0.5))
    composure = p.expressed("composure")
    willpower = p.expressed("willpower")
    empathy = p.expressed("empathy")
    addict = p.expressed("addiction_proneness")
    mood: dict[str, float] = {}
    vital: dict[str, float] = {}
    said = ""

    if kind == "scold":
        # A composed person absorbs it; a fragile one is wounded. Willpower protects
        # confidence. This is the owner's exact example: the scold's effect is the
        # event's meaning times the accumulated self.
        mood = {"stress": +i * (1 - composure),
                "confidence": -i * (1 - willpower),
                "hope": -0.4 * i * (1 - composure)}
        said = "…understood."
    elif kind == "praise":
        mood = {"confidence": +i * (0.5 + 0.5 * empathy), "hope": +0.5 * i,
                "stress": -0.4 * i}
        said = "thank you."
    elif kind in ("rest", "sit"):
        comfort = float(event.get("comfort", 0.6))
        mood = {"stress": -0.4 * comfort, "focus": +0.2 * comfort}
        vital = {"energy": +0.3 * comfort}
        said = "that's better."
    elif kind == "win":
        mood = {"confidence": +i, "hope": +0.6 * i, "stress": -0.3 * i}
        said = "yes!"
    elif kind == "lose":
        # An addiction-prone person keeps their hope up after a loss — chasing it —
        # while their stress still climbs. The trait shapes the wound's shape.
        mood = {"confidence": -i, "stress": +0.7 * i,
                "hope": +0.2 * i * addict - 0.3 * i * (1 - addict)}
        said = "again."
    else:
        mood = {"focus": +0.05}     # a nothing-event barely stirs the water

    return {
        "understood": f"{kind or 'something'} (intensity {i:.2f})",
        "mood": mood, "vitals": vital, "traits": {},
        "memory": event.get("gist") or kind or "something happened",
        "action": {"kind": "say", "text": said} if said else {},
    }


@dataclass
class HumanPod:
    """A named person in the world. Identity + biology + body-state + memory."""
    name: str
    psyche: "psyche_mod.Psyche" = field(default_factory=psyche_mod.Psyche.newborn)
    pos: tuple[float, float] = (0.0, 0.0)
    seated_on: int | None = None
    memory: list[str] = field(default_factory=list)
    home_id: int | None = None          # optional link to a Studio identity
    last_action: dict[str, Any] = field(default_factory=dict)

    # --- creation: the easy path from sliders + scene + age --------------

    @classmethod
    def create(cls, name: str, *, sliders: dict[str, float] | None = None,
               scene: str = "neutral", age: float = 0.0,
               home_id: int | None = None) -> "HumanPod":
        """Make a digital baby and grow it: set its genome from the sliders, bias it
        for the scene through the equalizer, and dial its age. The whole 'baby →
        adjust → place in a room → set adult' flow in one call."""
        p = psyche_mod.Psyche.newborn(sliders)
        p = psyche_mod.apply_scene(p, scene)
        p.set_age(age)
        return cls(name=name, psyche=p, home_id=home_id)

    # --- the loop --------------------------------------------------------

    def perceive(self, event: dict, *, appraiser: Callable | None = None) -> dict:
        """Take one event, become slightly someone else. Returns X (for inspection),
        having already applied it. `appraiser` defaults to the free rule table; pass
        the model appraiser for open-ended events."""
        appraise = appraiser or _rule_appraise
        x = appraise(self.psyche, event)
        return self.apply_x(x)

    def apply_x(self, x: dict) -> dict:
        """Apply a consequence packet: clamp-and-add the deltas, remember the gist,
        record the action. Every change is bounded by psyche.apply_deltas, so a
        malformed or extreme X cannot drive the person out of range — the guarantee
        the owner asked for, enforced here rather than trusted."""
        psyche_mod.apply_deltas(self.psyche, {
            "traits": x.get("traits", {}) or {},
            "vitals": x.get("vitals", {}) or {},
            "mood": x.get("mood", {}) or {},
        })
        gist = str(x.get("memory", "")).strip()
        if gist:
            self.memory.append(gist)
            del self.memory[:-MEMORY_CAP]      # keep only the recent, bounded
        self.last_action = x.get("action", {}) or {}
        return x

    def rest(self, rate: float = 0.1) -> "HumanPod":
        """A beat of doing nothing — mood relaxes toward baseline. Idle time heals."""
        psyche_mod.relax(self.psyche, rate)
        return self

    # --- the model appraiser: the single, optional spend -----------------

    async def _appraise_with_model(self, event: dict, settings: dict,
                                   model: str = "") -> dict:
        """Appraise an open-ended event with an LLM. The ONE place a human pod can
        spend a token. Returns X in the same shape as the rule table, so the apply
        loop and its clamps are identical whichever appraiser ran — the model only
        ever *proposes*; the deterministic guard still disposes."""
        model = model or tuning.get("scene_default_model")
        sys = ("You read one event and report its effect on a person's inner state as "
               "STRICT JSON with keys mood/vitals/traits (each a map of variable→delta "
               "in [-1,1]), memory (a short gist), action ({kind,text}). Variables: "
               f"mood={list(psyche_mod.MOOD)}, vitals={list(psyche_mod.VITALS)}, "
               f"traits={list(psyche_mod.TRAITS)}. Deltas are suggestions; small.")
        prompt = (f"PERSON (0..1): {json.dumps(self.psyche.snapshot())}\n"
                  f"EVENT: {json.dumps(event)}\n\nReturn only the JSON packet.")
        raw = await providers.complete("anthropic", model, sys, prompt, settings,
                                       max_tokens=int(tuning.get("scene_utterance_max_tokens")))
        return _parse_x(raw)

    async def perceive_with_model(self, event: dict, settings: dict,
                                  model: str = "") -> dict:
        x = await self._appraise_with_model(event, settings, model)
        return self.apply_x(x)

    # --- serialisation: a person is just rows ----------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "pos": list(self.pos), "seated_on": self.seated_on,
                "home_id": self.home_id, "memory": list(self.memory),
                "psyche": self.psyche.snapshot()}

    @classmethod
    def from_dict(cls, d: dict) -> "HumanPod":
        snap = d.get("psyche", {})
        p = psyche_mod.Psyche(age=snap.get("age", 0.0),
                              traits={**{t: 0.5 for t in psyche_mod.TRAITS}, **snap.get("traits", {})},
                              vitals={**{v: 0.8 for v in psyche_mod.VITALS}, **snap.get("vitals", {})},
                              mood={**dict(psyche_mod.MOOD_BASELINE), **snap.get("mood", {})})
        return cls(name=d.get("name", "?"), psyche=p, pos=tuple(d.get("pos", (0, 0))),
                   seated_on=d.get("seated_on"), home_id=d.get("home_id"),
                   memory=list(d.get("memory", [])))


def _parse_x(raw: str) -> dict:
    """Pull the JSON packet out of a model reply. A malformed reply yields an empty
    packet — a no-op — never a crash and never an unclamped change."""
    text = (raw or "").strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    try:
        data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
