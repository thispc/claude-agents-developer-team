"""The Chair artifact pod — deterministic mechanics, honoured properties, and the
one interaction the owner asked to test: a person comes near, sits, the chair
changes state and binds them, and the person is rested by it.

Everything here is free code. No model decides whether a chair is occupied; the only
interpreted part (how sitting *feels*) is the human pod's free rule table.
"""

from pathlib import Path

import pytest

from app import chair_pod, human_pod

APP = Path(__file__).resolve().parent.parent / "conductor" / "app"
CHAIR_SRC = (APP / "chair_pod.py").read_text()


def _adult(name="P", pos=(0.0, 0.0), **sliders):
    p = human_pod.HumanPod.create(name, sliders=sliders or None, age=19)
    p.pos = pos
    p.id = id(p)          # a stable identity for occupancy
    return p


# --------------------------------------------------------------------------
# state and affordances
# --------------------------------------------------------------------------

def test_a_fresh_chair_is_empty_and_intact():
    c = chair_pod.ChairPod(id=1)
    assert c.occupied_by is None
    assert c.integrity() == 1.0
    assert c.is_intact()


def test_only_a_person_in_reach_is_offered_the_seat():
    """The owner's vicinity: getting near is what unlocks sitting. A far person is
    offered nothing; a near one is offered 'sit'."""
    c = chair_pod.ChairPod(id=1, pos=(0, 0))
    near = _adult("Near", pos=(0.5, 0.0))
    far = _adult("Far", pos=(10.0, 0.0))
    assert c.affordances(near) == ["sit"]
    assert c.affordances(far) == []


def test_the_effects_module_is_free_no_model_in_the_chair():
    """Structural: a chair spends nothing. Its mechanics must never call a model —
    physics is code, per the cost discipline."""
    assert "providers" not in CHAIR_SRC


# --------------------------------------------------------------------------
# interaction: sitting changes the chair and binds the person
# --------------------------------------------------------------------------

def test_sitting_marks_the_chair_used_and_wears_it():
    """The owner's exact ask: sitting changes the chair's state to used and adds
    wear-and-tear."""
    c = chair_pod.ChairPod(id=7)
    p = _adult(pos=(0, 0))
    assert c.occupied_by is None and c.wear == 0.0
    c.apply_action(p, "sit")
    assert c.occupied_by == p.id
    assert c.wear == pytest.approx(chair_pod.WEAR_PER_SIT)
    assert c.integrity() < 1.0


def test_sitting_binds_the_person_to_the_chair():
    """The chair emits the binding: the person is now seated on this chair."""
    c = chair_pod.ChairPod(id=3)
    p = _adult(pos=(0, 0))
    ev = c.apply_action(p, "sit")
    assert p.seated_on == c.id
    assert ev["kind"] == "sit" and ev["artifact"] == c.id


def test_a_seated_person_is_rested_by_the_chair():
    """The interpreted half: the binding event is appraised, and the person is
    rested — energy up, stress down — for free."""
    c = chair_pod.ChairPod(id=9)
    p = _adult(pos=(0, 0))
    p.perceive({"kind": "scold", "intensity": 0.8})   # make them stressed and tired
    stressed, tired = p.psyche.mood["stress"], p.psyche.vitals["energy"]
    res = chair_pod.offer_seat(c, p)
    assert res["seated"] is True
    assert p.psyche.mood["stress"] < stressed
    assert p.psyche.vitals["energy"] > tired
    assert p.psyche.is_sane()


def test_two_people_cannot_take_one_chair():
    c = chair_pod.ChairPod(id=2)
    a = _adult("A", pos=(0, 0))
    b = _adult("B", pos=(0, 0))
    c.apply_action(a, "sit")
    assert c.affordances(b) == []                      # occupied: nothing on offer
    blocked = c.apply_action(b, "sit")
    assert blocked["kind"] == "blocked"
    assert c.occupied_by == a.id                       # A keeps the seat


def test_standing_frees_the_chair():
    c = chair_pod.ChairPod(id=4)
    p = _adult(pos=(0, 0))
    c.apply_action(p, "sit")
    c.apply_action(p, "stand")
    assert c.occupied_by is None and p.seated_on is None


def test_out_of_reach_offer_seat_does_nothing():
    c = chair_pod.ChairPod(id=5, pos=(0, 0))
    p = _adult(pos=(20, 20))
    res = chair_pod.offer_seat(c, p)
    assert res["seated"] is False
    assert c.occupied_by is None


# --------------------------------------------------------------------------
# the object must not differ into nonsense either
# --------------------------------------------------------------------------

def test_wear_never_exceeds_one_however_many_times_it_is_used():
    """A chair sat in ten thousand times wears out but does not crumble into a
    negative or NaN integrity — the object's version of the anti-crazy guard."""
    c = chair_pod.ChairPod(id=6)
    for _ in range(10000):
        p = _adult(pos=(0, 0))
        c.apply_action(p, "sit")
        c.apply_action(p, "stand")
        assert c.is_intact()
    assert 0.0 <= c.wear <= 1.0
    assert c.integrity() >= 0.0


# --------------------------------------------------------------------------
# the scene the owner described: four adults and a chair
# --------------------------------------------------------------------------

def test_four_babies_grown_to_nineteen_and_one_takes_the_seat():
    """The owner's setup: create four digital babies, dial them to 19, place them by
    a chair in a casino. One in reach takes the seat; the chair is used and worn; the
    sitter is bound and rested; everyone stays sane. All free."""
    chair = chair_pod.ChairPod(id=1, pos=(0.0, 0.0))
    babies = [human_pod.HumanPod.create(f"P{i}", scene="casino",
                                        sliders={"willpower": 40 + 15 * i}, age=0)
              for i in range(4)]
    for b in babies:
        b.id = id(b)
        b.psyche.set_age(19)                            # grow them to adults
        assert b.psyche.maturation() == pytest.approx(1.0)

    babies[0].pos = (0.4, 0.0)                           # one steps up to the chair
    for b in babies[1:]:
        b.pos = (5.0, 5.0)

    res = chair_pod.offer_seat(chair, babies[0])
    assert res["seated"] is True
    assert chair.occupied_by == babies[0].id
    assert chair.wear > 0.0 and chair.is_intact()
    assert babies[0].seated_on == chair.id
    # the others could not reach it
    assert all(chair.affordances(b) == [] for b in babies[1:])
    assert all(b.psyche.is_sane() for b in babies)
