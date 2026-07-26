"""A — artifacts: code, not minds.

An Artifact is an Entity that holds state and, optionally, a secret sealed to a key. It
does not learn and it never calls a model; interacting with one is deterministic and
free. Its `perceive` reacts to an interaction by mutating its own state and handing back a
Signal — the "binding event" — for the acting Human to appraise. That asymmetry is the
whole H/A distinction: the Human is the actor, the Artifact is the world it acts on, and
one interaction moves both.

Card and Deck are the concrete artifacts for the deck-of-cards test. A Card's value is
sealed; only an agent holding its key (received when it was dealt) can reveal it — the
face-down card that is genuinely unreadable, not merely flagged.
"""

from __future__ import annotations

from typing import Any

from .entity import Entity, register
from .types import Packet, Signal
from .util import new_key, seal, unseal

SUITS = ["s", "h", "d", "c"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
_RANK_VAL = {r: i + 2 for i, r in enumerate(RANKS)}


class Artifact(Entity):
    """Base for inert, stateful, secret-holding objects. Public state is readable by all;
    `secret` is ciphertext only. Subclasses add typed, deterministic effects."""
    kind = "artifact"

    def __init__(self, id: int, name: str = "", pos: tuple = (0.0, 0.0), tau: int = 0,
                 public: dict | None = None, secret: str = "", holder: int | None = None):
        super().__init__(id, name, pos, tau)
        self.public: dict[str, Any] = public or {}
        self.secret: str = secret            # sealed ciphertext, or ""
        self.holder: int | None = holder     # the agent who holds it (and its key)

    def perceive(self, signal: Signal, world) -> Packet:
        """Artifacts don't appraise; an interaction with one is handled by `interact`.
        This exists only to satisfy the Entity contract."""
        return Packet(understood=f"{self.name} noted {signal.kind}")

    def interact(self, verb: str, agent, world) -> Signal:
        """Perform a deterministic effect and return the event the agent perceives.
        The base object can only be observed."""
        return Signal(kind="see", from_id=self.id,
                      payload={"text": f"you look at {self.name}", **self.view(agent)})

    def view(self, agent) -> dict[str, Any]:
        """What `agent` may see: public state always; the secret only if they hold it."""
        v = {"id": self.id, "name": self.name, "public": dict(self.public), "sealed": bool(self.secret)}
        val = self.reveal(agent)
        if val is not None:
            v["value"] = val
        return v

    def reveal(self, agent) -> Any:
        if not self.secret or agent is None:
            return None
        key = agent.social.key_for(f"art:{self.id}")
        try:
            return unseal(self.secret, key) if key else None
        except Exception:
            return None

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d.update(public=self.public, secret=self.secret, holder=self.holder)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Artifact":
        return cls(id=d["id"], name=d.get("name", ""), pos=tuple(d.get("pos", (0, 0))),
                   tau=int(d.get("tau", 0)), public=d.get("public", {}),
                   secret=d.get("secret", ""), holder=d.get("holder"))


@register
class Card(Artifact):
    kind = "card"

    def score(self, agent) -> int:
        v = self.reveal(agent) or {}
        return _RANK_VAL.get(v.get("rank", ""), 0)

    def flip(self) -> None:
        """Turn the card over — a free effect the owner's analogy calls for. Toggles both
        ways; `face_up` is the visual state the UI reads, while the value stays keyed to
        its holder regardless (a face-up card is a later refinement)."""
        self.public["face_up"] = not self.public.get("face_up", False)

    def interact(self, verb: str, agent, world) -> Signal:
        if verb == "flip":
            self.flip()
            return Signal(kind="see", from_id=self.id, domain="cards",
                          payload={"text": f"{self.name} turns over"})
        return Signal(kind="see", from_id=self.id, domain="cards",
                      payload={"text": f"you hold {self.name}", **self.view(agent)},
                      intensity=0.5, stakes=0.6)


@register
class Deck(Artifact):
    """The shoe. Its order is a SECRET the world holds — never in public state and never
    returned by any view — so no agent can read the deal order or recover a card sealed to
    someone else. Public state is only the count and the cursor. (Earlier this kept the
    plaintext order in `public`, which defeated every per-card seal; that is fixed here.)"""
    kind = "deck"

    def __init__(self, id, name="deck", pos=(0.0, 0.0), tau=0, public=None,
                 secret="", holder=None, order=None):
        super().__init__(id, name, pos, tau, public, secret, holder)
        self.order: list = order or []        # the shuffled deal order — private, never viewed

    @classmethod
    def fresh(cls, id: int, seed: int = 0, name: str = "deck") -> "Deck":
        import random
        cards = [{"rank": r, "suit": s} for s in SUITS for r in RANKS]
        random.Random(seed).shuffle(cards)
        return cls(id, name, public={"cursor": 0, "count": len(cards)}, order=cards)

    def draw_to(self, agent, world) -> "Card | None":
        """Deal the top card to `agent`, sealed to a fresh key handed only to them. The
        order is read from the deck's PRIVATE `order`, never from public state."""
        cur = self.public.get("cursor", 0)
        if cur >= len(self.order):
            return None
        value = self.order[cur]
        self.public["cursor"] = cur + 1
        self.public["count"] = len(self.order) - self.public["cursor"]   # remaining only
        key = new_key()
        cid = world.next_id()
        card = Card(cid, name="a card", public={"back": "blue", "face_up": False},
                    secret=seal(value, key), holder=agent.id)
        agent.social.join_circle(f"art:{cid}", key)     # the key lands in the agent's private scope
        world.add(card)
        return card

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["order"] = self.order          # persisted for reload; NOT exposed by view()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Deck":
        return cls(id=d["id"], name=d.get("name", "deck"), pos=tuple(d.get("pos", (0, 0))),
                   tau=int(d.get("tau", 0)), public=d.get("public", {}),
                   secret=d.get("secret", ""), holder=d.get("holder"), order=d.get("order", []))

    def interact(self, verb: str, agent, world) -> Signal:
        if verb == "draw":
            card = self.draw_to(agent, world)
            if not card:
                return Signal(kind="see", from_id=self.id, domain="cards",
                              payload={"text": "the deck is empty"})
            return Signal(kind="deal", from_id=self.id, domain="cards", intensity=0.6, stakes=0.6,
                          payload={"text": f"you draw {card.name}", "card": card.id})
        return super().interact(verb, agent, world)
