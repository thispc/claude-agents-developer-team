"""The biology of a synthetic person — the tunable variables under an H pod.

This is the "base skeleton" of a digital baby: a small, explicit set of variables,
grounded in real trait/affect models, that shape how the person behaves and how
events land on them. It is deliberately the *fast, cheap* layer of the two-layer
design (see docs/SELF_LEARNING_SUBSTRATE.md §6): learning here is arithmetic on a
handful of numbers, never gradient descent — no backprop, no forgetting, fully
readable, per person.

Three families of variable, on three timescales:

- **TRAITS** — the genome. Slow. Set by the creator's sliders, biased by the scene
  (a casino wants different people than an office). These barely move from any one
  event; they are who the person *is*. (A compact Big-Five-plus-drives basis.)
- **VITALS** — the body. Medium. Energy and health; shaped by age and by what the
  person does.
- **MOOD** — the weather. Fast. Confidence, stress, hope, focus; these are what a
  single event moves, and they drift back toward a baseline on their own.

Two rules keep a person a person and not a diverging monster — the owner's explicit
requirement that nobody "differs into a crazy person":

1. **Everything is clamped to [0, 1], and every single event can move a variable by
   at most a bounded step** (small for traits, larger for mood). No event can spike
   an identity.
2. **Homeostasis.** Mood decays toward a baseline every idle beat, so stress cannot
   ratchet to 1 and stay pinned — a bad moment fades, exactly as it should.

Age is a **uniform drip**: as age rises from baby to adult, the *expressed* strength
of every capability rises together toward its genetic base — the owner's "age
increases all stats uniformly, which is experience."
"""

from dataclasses import dataclass, field, replace
from typing import Any

# The genome: slow traits, each 0..1. Names are the behavioural meaning, not the
# clinical term, so a slider labelled "willpower" reads to a human.
TRAITS = ("willpower", "risk_appetite", "addiction_proneness",
          "composure", "sociability", "empathy", "curiosity")

VITALS = ("energy", "health")

MOOD = ("confidence", "stress", "hope", "focus")

# Where mood rests when nothing is happening. A person is not neutral by default —
# mildly hopeful, lightly stressed — and homeostasis pulls back here.
MOOD_BASELINE = {"confidence": 0.5, "stress": 0.2, "hope": 0.6, "focus": 0.6}

# The anti-crazy guard, quantified. One event may move a trait almost not at all (it
# is the skeleton), a vital a little, a mood a fair amount — but never past these.
MAX_STEP = {"trait": 0.03, "vital": 0.12, "mood": 0.25}

# Age at which a capability is fully expressed. A baby has the genes but not the
# grown strength; the drip toward `base` completes around adulthood.
ADULT_AGE = 18.0
# A newborn already expresses this fraction of its genome; the rest drips in with age.
NEWBORN_EXPRESSION = 0.15


def clamp01(x: float) -> float:
    """Every variable lives in [0, 1]. NaN collapses to the floor rather than
    propagating — a single bad number must not be able to poison a person."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    if x != x:            # NaN
        return 0.0
    return 0.0 if x < 0 else 1.0 if x > 1 else x


@dataclass
class Psyche:
    """One person's variables. A plain bag of numbers, serialisable to JSON, so a
    person is just rows and survives a restart."""
    age: float = 0.0
    traits: dict[str, float] = field(default_factory=lambda: {t: 0.5 for t in TRAITS})
    vitals: dict[str, float] = field(default_factory=lambda: {v: 0.8 for v in VITALS})
    mood: dict[str, float] = field(default_factory=lambda: dict(MOOD_BASELINE))

    # --- creation ---------------------------------------------------------

    @classmethod
    def newborn(cls, sliders: dict[str, float] | None = None) -> "Psyche":
        """A blank baby, its genome set by the creator's sliders (0..100 or 0..1;
        both accepted). Anything unset sits at the neutral middle."""
        p = cls(age=0.0)
        for name, raw in (sliders or {}).items():
            if name in TRAITS:
                p.traits[name] = clamp01(_as_unit(raw))
        return p

    # --- age: the uniform drip -------------------------------------------

    def maturation(self) -> float:
        """0 at birth, 1 by adulthood. The single curve behind the drip."""
        return clamp01(self.age / ADULT_AGE)

    def expressed(self, name: str) -> float:
        """The *effective* strength of a capability right now: its genome scaled by
        how grown the person is. This is what behaviour and appraisal read, so a
        child and an adult with the same genome act differently. Mood is not scaled —
        a baby can be perfectly miserable."""
        if name in self.mood:
            return clamp01(self.mood[name])
        base = self.traits.get(name, self.vitals.get(name, 0.5))
        factor = NEWBORN_EXPRESSION + (1 - NEWBORN_EXPRESSION) * self.maturation()
        return clamp01(base * factor)

    def set_age(self, age: float) -> "Psyche":
        """Set the age directly — the creator dials it. Every expressed capability
        rises together toward its base as age climbs: the uniform drip."""
        self.age = max(0.0, float(age))
        return self

    # --- the equalizer: tune a person for a scene ------------------------

    def is_sane(self) -> bool:
        """The invariant the whole design protects: every variable in range, nothing
        NaN. A person that fails this has 'differed into a crazy person', and the
        guards exist so this can never return False in normal operation."""
        for pool in (self.traits, self.vitals, self.mood):
            for v in pool.values():
                if v != v or v < 0.0 or v > 1.0:
                    return False
        return 0.0 <= self.age

    def snapshot(self) -> dict[str, Any]:
        return {"age": self.age, "traits": dict(self.traits),
                "vitals": dict(self.vitals), "mood": dict(self.mood)}


# --- the equalizer: a scene biases the genome ----------------------------------
#
# A room is not neutral about who belongs in it. A casino is full of people with an
# appetite for risk and a weakness for the next hand; an office rewards focus and
# self-control. These profiles are the equalizer bands: offsets added to the genome
# when a person is placed in the scene, so the same sliders make a subtly different
# person depending on where they stand. Scoping the person to the scene is also what
# keeps the LLM's job small — the context is already known.
SCENE_PROFILES: dict[str, dict[str, float]] = {
    "casino":  {"risk_appetite": +0.25, "addiction_proneness": +0.20,
                "composure": -0.10, "willpower": -0.08},
    "office":  {"willpower": +0.15, "composure": +0.12, "risk_appetite": -0.12},
    "studio":  {"curiosity": +0.20, "sociability": +0.12, "empathy": +0.08},
    "neutral": {},
}


def equalizer_bands() -> tuple[str, ...]:
    """The sliders a creator sees — one per trait. The 'equalizer' the owner asked
    for is literally this: a band per genetic dimension."""
    return TRAITS


def apply_scene(psyche: "Psyche", scene: str) -> "Psyche":
    """Bias a person's genome for a scene. Returns a NEW psyche (the original is the
    untuned baby), each trait shifted by the scene band and re-clamped — so tuning is
    reversible and never drives a value out of range."""
    profile = SCENE_PROFILES.get(scene, {})
    tuned = replace(psyche, traits=dict(psyche.traits),
                    vitals=dict(psyche.vitals), mood=dict(psyche.mood))
    for trait, offset in profile.items():
        if trait in tuned.traits:
            tuned.traits[trait] = clamp01(tuned.traits[trait] + offset)
    return tuned


# --- applying a consequence packet, safely -------------------------------------

def _domain(name: str) -> str:
    return "trait" if name in TRAITS else "vital" if name in VITALS else "mood"


def apply_deltas(psyche: "Psyche", deltas: dict[str, dict[str, float]]) -> "Psyche":
    """Apply an event's consequence to a person — the heart of the pod's learning.

    `deltas` is grouped by family, e.g. {"mood": {"stress": +0.4}, "traits": {...}}.
    Each delta is CLAMPED to its family's max step before it is applied, and the
    result is re-clamped to [0, 1]. This is the anti-crazy guard in force: no single
    event, however extreme the model's suggestion, can move a person more than a
    bounded amount, and traits (the skeleton) move a tenth of what mood does."""
    for family, pool, key in (("traits", psyche.traits, "trait"),
                              ("vitals", psyche.vitals, "vital"),
                              ("mood", psyche.mood, "mood")):
        cap = MAX_STEP[key]
        for var, raw in (deltas.get(family, {}) or {}).items():
            if var not in pool:
                continue                      # unknown variable is ignored, never created
            step = max(-cap, min(cap, float(raw)))
            pool[var] = clamp01(pool[var] + step)
    return psyche


def relax(psyche: "Psyche", rate: float = 0.1) -> "Psyche":
    """One beat of homeostasis: mood drifts back toward its baseline. This is what
    makes a bad moment *fade* instead of pinning a person at maximum stress forever —
    the other half of 'don't let a person differ into a crazy person'. Traits and
    vitals do not relax; only weather passes."""
    rate = max(0.0, min(1.0, rate))
    for var, base in MOOD_BASELINE.items():
        psyche.mood[var] = clamp01(psyche.mood[var] + (base - psyche.mood[var]) * rate)
    return psyche


def _as_unit(raw: float) -> float:
    """Accept a slider as 0..1 or 0..100; normalise to 0..1."""
    v = float(raw)
    return v / 100.0 if v > 1.0 else v
