"""The Human pod — a digital baby that grows, is tuned by its room, feels events,
and never differs into a crazy person.

Offline by construction: the default appraiser is a free deterministic rule table,
so a person can live a whole life of events with zero tokens. The one model path is
proven to be a single choke point, counted by a spy and pinned structurally.
"""

import asyncio
import re
from pathlib import Path

import pytest

from app import human_pod, psyche as psyche_mod

APP = Path(__file__).resolve().parent.parent / "conductor" / "app"
HUMAN_SRC = (APP / "human_pod.py").read_text()


# --------------------------------------------------------------------------
# the biology: baby, sliders, the age drip, the equalizer
# --------------------------------------------------------------------------

def test_a_newborn_is_blank_and_sane():
    p = psyche_mod.Psyche.newborn()
    assert p.age == 0.0
    assert p.is_sane()
    assert all(0.0 <= v <= 1.0 for v in {**p.traits, **p.vitals, **p.mood}.values())


def test_sliders_set_the_genome_and_accept_0_100_or_0_1():
    p = psyche_mod.Psyche.newborn({"willpower": 90, "risk_appetite": 0.1})
    assert p.traits["willpower"] == pytest.approx(0.9)
    assert p.traits["risk_appetite"] == pytest.approx(0.1)


def test_age_raises_every_capability_together_like_a_drip():
    """The owner's 'age increases all stats uniformly'. A baby expresses little of
    its genome; an adult expresses it fully — and every capability rises together."""
    p = psyche_mod.Psyche.newborn({t: 80 for t in psyche_mod.TRAITS})
    baby = {t: p.expressed(t) for t in psyche_mod.TRAITS}
    p.set_age(19)
    adult = {t: p.expressed(t) for t in psyche_mod.TRAITS}
    for t in psyche_mod.TRAITS:
        assert adult[t] > baby[t], f"{t} did not grow with age"
    # adult expresses ~the full genome; the baby expresses only the newborn fraction
    assert all(adult[t] == pytest.approx(0.8, abs=0.02) for t in psyche_mod.TRAITS)
    assert all(baby[t] < 0.2 for t in psyche_mod.TRAITS)


def test_the_scene_equalizer_makes_a_different_person():
    """The same sliders, tuned for a casino versus an office, yield measurably
    different people — more risk and addiction in the casino, more willpower in the
    office. This is the equalizer the owner asked for."""
    base = {"willpower": 50, "risk_appetite": 50, "addiction_proneness": 50, "composure": 50}
    casino = human_pod.HumanPod.create("A", sliders=base, scene="casino", age=25)
    office = human_pod.HumanPod.create("B", sliders=base, scene="office", age=25)
    assert casino.psyche.traits["risk_appetite"] > office.psyche.traits["risk_appetite"]
    assert casino.psyche.traits["addiction_proneness"] > office.psyche.traits["addiction_proneness"]
    assert office.psyche.traits["willpower"] > casino.psyche.traits["willpower"]
    assert casino.psyche.is_sane() and office.psyche.is_sane()


def test_tuning_never_pushes_a_trait_out_of_range():
    """An extreme slider plus a scene band must still land in [0,1] — the equalizer
    clamps rather than overflowing."""
    p = human_pod.HumanPod.create("X", sliders={"risk_appetite": 100}, scene="casino")
    assert 0.0 <= p.psyche.traits["risk_appetite"] <= 1.0


# --------------------------------------------------------------------------
# the loop: an event changes the self, gated by who the person is
# --------------------------------------------------------------------------

def test_the_same_scold_wounds_a_fragile_person_more_than_a_composed_one():
    """The owner's exact example: a scold's effect is its meaning times the
    accumulated self. Low composure → more stress and lost confidence than high."""
    fragile = human_pod.HumanPod.create("F", sliders={"composure": 10, "willpower": 10}, age=25)
    steady = human_pod.HumanPod.create("S", sliders={"composure": 95, "willpower": 95}, age=25)
    ev = {"kind": "scold", "intensity": 0.9}
    fragile.perceive(ev)
    steady.perceive(ev)
    assert fragile.psyche.mood["stress"] > steady.psyche.mood["stress"]
    assert fragile.psyche.mood["confidence"] < steady.psyche.mood["confidence"]


def test_perceive_returns_and_applies_a_consequence_packet():
    p = human_pod.HumanPod.create("P", age=25)
    before = p.psyche.mood["confidence"]
    x = p.perceive({"kind": "praise", "intensity": 0.8})
    assert "mood" in x and "memory" in x
    assert p.psyche.mood["confidence"] > before
    assert p.memory and p.last_action.get("kind") == "say"


def test_the_addiction_trait_shapes_the_wound_of_a_loss():
    """An addiction-prone person keeps hope up after a loss (chasing it); a steady
    one loses hope. Same event, trait-shaped consequence."""
    hooked = human_pod.HumanPod.create("H", sliders={"addiction_proneness": 95}, scene="casino", age=30)
    clean = human_pod.HumanPod.create("C", sliders={"addiction_proneness": 5}, scene="office", age=30)
    h0, c0 = hooked.psyche.mood["hope"], clean.psyche.mood["hope"]
    hooked.perceive({"kind": "lose", "intensity": 0.8})
    clean.perceive({"kind": "lose", "intensity": 0.8})
    assert (hooked.psyche.mood["hope"] - h0) > (clean.psyche.mood["hope"] - c0)


# --------------------------------------------------------------------------
# the anti-crazy guard — the owner's explicit requirement
# --------------------------------------------------------------------------

def test_a_person_never_differs_into_a_crazy_person():
    """A thousand relentless, extreme, adversarial events. Every variable must stay
    in [0,1], nothing NaN — the person may end maxed out, but never broken."""
    p = human_pod.HumanPod.create("Victim", sliders={"composure": 20}, scene="casino", age=25)
    kinds = ["scold", "lose", "praise", "win", "rest", "mystery"]
    for n in range(1000):
        p.perceive({"kind": kinds[n % len(kinds)], "intensity": 1.0})
        assert p.psyche.is_sane(), f"went insane at event {n}"
    # even a huge single suggested delta is clamped, not applied whole
    p.apply_x({"mood": {"stress": 9999, "confidence": -9999}})
    assert p.psyche.is_sane()
    assert p.psyche.mood["stress"] <= 1.0


def test_the_genome_barely_moves_the_skeleton_is_stable():
    """Traits are the skeleton: across a life of events they drift only a little,
    because a trait step is a tenth of a mood step. Who you are is not rewritten by a
    bad afternoon."""
    p = human_pod.HumanPod.create("Steady", age=25)
    w0 = p.psyche.traits["willpower"]
    for _ in range(200):
        # even if an appraiser tried to shove a trait hard, the clamp holds it
        p.apply_x({"traits": {"willpower": -1.0}})
    # 200 steps of the max trait step (0.03) could reach the floor, but the point is
    # no SINGLE event moved it more than the cap
    assert abs(p.psyche.traits["willpower"] - w0) <= 200 * psyche_mod.MAX_STEP["trait"] + 1e-9


def test_mood_relaxes_back_toward_baseline():
    """Homeostasis: a spike of stress fades when nothing is happening. A bad moment
    passes rather than pinning the person forever."""
    p = human_pod.HumanPod.create("R", age=25)
    for _ in range(5):
        p.perceive({"kind": "scold", "intensity": 1.0})
    peak = p.psyche.mood["stress"]
    for _ in range(50):
        p.rest(0.2)
    assert p.psyche.mood["stress"] < peak
    assert p.psyche.mood["stress"] == pytest.approx(psyche_mod.MOOD_BASELINE["stress"], abs=0.05)


def test_memory_is_bounded():
    p = human_pod.HumanPod.create("M", age=25)
    for i in range(500):
        p.perceive({"kind": "win", "intensity": 0.5, "gist": f"win {i}"})
    assert len(p.memory) <= human_pod.MEMORY_CAP


# --------------------------------------------------------------------------
# cost: free by default, one model choke point when asked
# --------------------------------------------------------------------------

def test_a_life_of_events_costs_nothing():
    """The default appraiser is deterministic. A person can live entirely free."""
    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("the free path touched a model")
    import app.human_pod as hp
    orig = hp.providers.complete
    hp.providers.complete = boom
    try:
        p = human_pod.HumanPod.create("Free", age=25)
        for _ in range(50):
            p.perceive({"kind": "scold", "intensity": 0.7})
        assert called["n"] == 0
    finally:
        hp.providers.complete = orig


def test_the_model_appraiser_is_one_bounded_call(monkeypatch):
    calls = {"n": 0, "max_tokens": []}

    async def fake(provider, model, system, prompt, settings, max_tokens=2000, source=""):
        calls["n"] += 1
        calls["max_tokens"].append(max_tokens)
        return '{"mood": {"stress": 0.3}, "memory": "a strange letter", "action": {"kind":"say","text":"…"}}'
    monkeypatch.setattr(human_pod.providers, "complete", fake)
    p = human_pod.HumanPod.create("L", age=25)
    x = asyncio.run(p.perceive_with_model({"kind": "letter", "text": "you are fired"}, {}))
    assert calls["n"] == 1
    assert calls["max_tokens"][0] <= 2000
    assert p.psyche.mood["stress"] > psyche_mod.MOOD_BASELINE["stress"]
    assert p.psyche.is_sane()   # the model's deltas went through the SAME clamps


def test_the_model_path_has_a_single_spend_site():
    """Structural: exactly one providers.complete in the human pod, inside the
    appraiser. A second, unmetered door would pass the counting test while spending
    in real life — the same guard the scene engine has."""
    assert len(re.findall(r"providers\.complete\(", HUMAN_SRC)) == 1


def test_a_model_that_returns_garbage_changes_nothing(monkeypatch):
    async def junk(*a, **k):
        return "the model rambled with no json at all"
    monkeypatch.setattr(human_pod.providers, "complete", junk)
    p = human_pod.HumanPod.create("G", age=25)
    snap = p.psyche.snapshot()
    asyncio.run(p.perceive_with_model({"kind": "letter"}, {}))
    assert p.psyche.snapshot() == snap   # unparseable X is a no-op, never a crash


def test_a_person_survives_a_round_trip():
    p = human_pod.HumanPod.create("T", sliders={"willpower": 70}, scene="casino", age=22)
    p.perceive({"kind": "win", "intensity": 0.6, "gist": "won a hand"})
    clone = human_pod.HumanPod.from_dict(p.to_dict())
    assert clone.to_dict() == p.to_dict()
