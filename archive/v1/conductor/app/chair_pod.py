"""A — a chair artifact pod: code, not a mind.

A chair is the simplest honest test of the owner's "an artifact is a pod running a
framework that changes its own state when interacted with." It holds state (is it
occupied, how worn is it), it advertises what a nearby person may do with it (its
"decision tree" / affordance), and when someone sits it changes its own state and
*binds* the person — emitting the event that becomes the person's next input.

Everything here is DETERMINISTIC code. This is the owner's cost discipline made
literal: the *mechanics* of a chair — who is on it, how much it wore, whether a
far-away person may reach it — are physics, computed for free. No model decides
whether a chair is occupied. The one thing that needs interpretation — how the
person *feels* about sitting — is the human pod's job, and the chair only hands it a
plain event to appraise.

Like a person, an object must not diverge into nonsense: wear is clamped to [0, 1], a
chair cannot be sat in twice, and `is_intact()` is the invariant the tests pin.
"""

from dataclasses import dataclass, field
from typing import Any

# How near a person must be to reach the chair, and how much one sitting wears it.
REACH = 1.5
WEAR_PER_SIT = 0.02


@dataclass
class ChairPod:
    """A chair in the world. Its whole self is four numbers and an occupant."""
    id: int
    pos: tuple[float, float] = (0.0, 0.0)
    occupied_by: int | None = None      # the id of the seated person, or None
    wear: float = 0.0                   # 0 = pristine, 1 = worn out
    label: str = "chair"

    # --- perception: how near, and what is on offer ----------------------

    def distance_to(self, pos: tuple[float, float]) -> float:
        return ((self.pos[0] - pos[0]) ** 2 + (self.pos[1] - pos[1]) ** 2) ** 0.5

    def within_reach(self, pos: tuple[float, float]) -> bool:
        return self.distance_to(pos) <= REACH

    def affordances(self, person) -> list[str]:
        """The chair's decision tree: what THIS person may do with it right now. A
        person already sitting here may stand; a person in reach of an empty chair
        may sit; anyone else is offered nothing. Scoping the choice this tightly is
        what keeps any downstream model decision trivial — the context is known."""
        pid = getattr(person, "id", id(person))
        if self.occupied_by == pid:
            return ["stand"]
        if self.occupied_by is None and self.within_reach(getattr(person, "pos", (0, 0))):
            return ["sit"]
        return []

    # --- interaction: change own state, bind the person, emit an event ---

    def apply_action(self, person, action: str) -> dict[str, Any]:
        """Perform an offered action. Returns a **binding event** — the input the
        chair hands back to the person to appraise. Refuses anything not currently
        afforded, so an object can never be driven into an impossible state (two
        occupants, a stand with no sitter)."""
        pid = getattr(person, "id", id(person))
        if action not in self.affordances(person):
            return {"kind": "blocked", "why": f"{action} not available", "artifact": self.id}

        if action == "sit":
            self.occupied_by = pid
            self.wear = _clamp01(self.wear + WEAR_PER_SIT)      # it wears, a little
            comfort = self.integrity()                          # a worn chair is less restful
            person.seated_on = self.id
            return {"kind": "sit", "comfort": comfort, "intensity": 0.5,
                    "artifact": self.id, "gist": f"sat down on the {self.label}"}

        # stand
        self.occupied_by = None
        if getattr(person, "seated_on", None) == self.id:
            person.seated_on = None
        return {"kind": "stand", "artifact": self.id, "gist": f"stood up from the {self.label}"}

    def integrity(self) -> float:
        """How good the chair still is: the inverse of its wear."""
        return _clamp01(1.0 - self.wear)

    def is_intact(self) -> bool:
        """The object's sanity invariant, mirroring Psyche.is_sane: wear in range,
        occupancy coherent. A chair must never 'differ into a crazy object'."""
        return (0.0 <= self.wear <= 1.0) and (self.wear == self.wear)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "pos": list(self.pos), "occupied_by": self.occupied_by,
                "wear": self.wear, "label": self.label}


def offer_seat(chair: "ChairPod", person, *, appraiser=None) -> dict[str, Any]:
    """The full interaction, end to end: the person approaches, the chair offers a
    seat if they are in reach, they take it, the chair changes and binds them, and
    the person appraises how it felt. Returns {seated, event, x}. Free by default —
    the appraisal uses the human pod's deterministic rules unless a model is passed.
    """
    if "sit" not in chair.affordances(person):
        return {"seated": False, "event": {"kind": "out_of_reach"}, "x": {}}
    event = chair.apply_action(person, "sit")
    x = person.perceive(event, appraiser=appraiser)
    return {"seated": True, "event": event, "x": x}


def _clamp01(x: float) -> float:
    if x != x:
        return 0.0
    return 0.0 if x < 0 else 1.0 if x > 1 else x
