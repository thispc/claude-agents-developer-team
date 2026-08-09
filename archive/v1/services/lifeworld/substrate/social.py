"""The social layer — relationships, circles (shared secrets), theory of mind.

One graph, not three structures: an edge from A to B carries the relationship (trust,
warmth, a debt tally) AND, when A models B, a small theory-of-mind belief. Circles are
labelled hyperedges — a shared key that seals a secret to a group. Everything here is
free arithmetic and lookups; the heavy, dramatic parts (theory of mind, gossip) are the
first thing `switch_drama_off` disables, and even on they are bounded to the agents an
agent actually cares about, so it is O(your circle), never O(everyone²).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .util import clamp01

TOM_KEEP = 16      # cap on how many others an agent bothers to model (LRU by |trust-0.5|)


@dataclass
class Bond:
    """A -> B: how A relates to B, and what A believes about B (theory of mind)."""
    trust: float = 0.5
    warmth: float = 0.5
    debt: float = 0.0                          # >0 = B did A a favour, A owes
    believed: dict[str, float] = field(default_factory=dict)   # A's model of B's traits
    promises_kept: int = 0
    promises_broken: int = 0
    last_tau: int = 0

    def reliability(self) -> float:
        total = self.promises_kept + self.promises_broken
        return self.promises_kept / total if total else 0.5

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Social:
    def __init__(self, bonds: dict | None = None, circles: dict | None = None):
        self.bonds: dict[int, Bond] = bonds or {}          # other_id -> Bond
        self.circles: dict[str, str] = circles or {}       # circle name -> key held

    def bond(self, other_id: int) -> Bond:
        b = self.bonds.get(other_id)
        if b is None:
            b = self.bonds[other_id] = Bond()
        return b

    def update(self, other_id: int, deltas: dict[str, Any], tau: int,
               drama_on: bool = True) -> None:
        """Fold a social consequence into the bond with `other_id`. With drama off, only
        the functional dimensions (trust, debt) move — no warmth theatre, no ToM drift."""
        b = self.bond(other_id)
        b.last_tau = tau
        for k in ("trust", "warmth", "debt"):
            if k in deltas and (drama_on or k in ("trust", "debt")):
                cur = getattr(b, k)
                setattr(b, k, clamp01(cur + float(deltas[k])) if k != "debt"
                        else round(cur + float(deltas[k]), 3))
        if deltas.get("kept"):
            b.promises_kept += 1
        if deltas.get("broken"):
            b.promises_broken += 1
        if drama_on and isinstance(deltas.get("believed"), dict):
            for t, dv in deltas["believed"].items():
                b.believed[t] = clamp01(b.believed.get(t, 0.5) + float(dv))
        self._prune()

    def trusts(self, other_id: int) -> float:
        return self.bonds[other_id].trust if other_id in self.bonds else 0.5

    # --- circles: shared-key secrecy, reusing the artifact seal model -------

    def join_circle(self, name: str, key: str) -> None:
        self.circles[name] = key

    def leaves_circle(self, name: str) -> None:
        self.circles.pop(name, None)

    def key_for(self, name: str) -> str | None:
        return self.circles.get(name)

    def _prune(self) -> None:
        if len(self.bonds) <= TOM_KEEP:
            return
        # keep the strongest relationships (furthest from neutral trust); drop the rest
        ranked = sorted(self.bonds.items(), key=lambda kv: -abs(kv[1].trust - 0.5))
        self.bonds = dict(ranked[:TOM_KEEP])

    def to_dict(self) -> dict[str, Any]:
        return {"bonds": {str(k): v.to_dict() for k, v in self.bonds.items()},
                "circles": dict(self.circles)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Social":
        d = d or {}
        bonds = {int(k): Bond(**{kk: vv for kk, vv in v.items() if kk in Bond.__annotations__})
                 for k, v in d.get("bonds", {}).items()}
        return cls(bonds=bonds, circles=dict(d.get("circles", {})))
