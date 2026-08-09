"""The inheritance spine — one atom, two kinds.

Everything in the Lifeworld is an Entity: it has an id, a name, a position, and it lives
in time counted by scans. From there the tree forks exactly once, on the one distinction
that matters:

    Entity
    ├── Being      — has an inner life that LEARNS (Human is the concrete one)
    └── Artifact   — is CODE that holds state and secrets (Card, Deck, …)

`perceive(signal, world) -> Packet` is the universal verb — the reflex arc — and it is
abstract here so each kind fills it with its own machinery. Serialisation is polymorphic:
`Entity.load(row)` dispatches on `kind` to the right subclass's `from_dict`, so the store
never needs to know the taxonomy.
"""

from __future__ import annotations

from typing import Any

from .types import Packet, Signal

# kind -> concrete class, populated by subclasses via `register`. Lets the store load a
# row without importing every type or knowing the hierarchy.
_KINDS: dict[str, type] = {}


def register(cls: type) -> type:
    _KINDS[cls.kind] = cls
    return cls


class Entity:
    kind: str = "entity"

    def __init__(self, id: int, name: str = "", pos: tuple = (0.0, 0.0), tau: int = 0):
        self.id = id
        self.name = name
        self.pos = tuple(pos)
        self.tau = tau                       # this entity's own age, in scans

    # the universal verb — subclasses implement it
    def perceive(self, signal: Signal, world) -> Packet:
        raise NotImplementedError

    def summary(self) -> str:
        """A one-line human-readable state note, used for the ledger and the UI."""
        return f"{self.kind} {self.name}"

    # --- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "name": self.name,
                "pos": list(self.pos), "tau": self.tau}

    @classmethod
    def load(cls, row: dict[str, Any]) -> "Entity":
        target = _KINDS.get(row.get("kind", ""), cls)
        return target.from_dict(row) if target is not cls else cls(row["id"], row.get("name", ""))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Entity":
        return cls(id=d["id"], name=d.get("name", ""), pos=tuple(d.get("pos", (0, 0))),
                   tau=int(d.get("tau", 0)))


class Being(Entity):
    """A living entity: it owns a ledger and advances its own time. Concrete life
    (Human) subclasses this; the shared machinery of pinning a state change and ageing
    lives here so every living thing does it the same way."""
    kind = "being"

    def __init__(self, id: int, name: str = "", pos: tuple = (0.0, 0.0), tau: int = 0):
        super().__init__(id, name, pos, tau)
        from .ledger import Ledger
        self.ledger = Ledger()

    def age(self, summary: str, *, changed: bool, ledger_on: bool = True) -> None:
        """One scan older. A state change commits a ledger pin (batched — a no-op scan
        writes nothing), so the chain measures meaningful events, not raw ticks."""
        self.tau += 1
        if changed and ledger_on:
            self.ledger.commit(self.tau, summary)
