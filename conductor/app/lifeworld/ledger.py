"""The ledger — a git-for-a-life hash chain that makes a résumé verifiable.

Every state-changing scan commits `hash(summary ‖ parent)`, so an agent's history is
tamper-evident: you cannot rewrite an entry without every later hash failing to line up.
Pins are batched — a no-op scan writes nothing — so the chain is O(meaningful events),
not O(scans).

Honest about what this proves: a single-node chain proves internal consistency and no
silent edit. Cross-party trust comes later, from proof-of-encounter co-signing (agents
that interact sign each other's head), verified majority-SHA like git — no coin, no
mining. That is a federation wave; the cheap, real part is here now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .util import sha

GENESIS = "0" * 16


@dataclass
class Pin:
    tau: int
    summary: str            # a compact, human-readable note of what changed
    parent: str
    digest: str
    cosigners: list = field(default_factory=list)   # ids that attested this head (future)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Ledger:
    def __init__(self, pins: list[Pin] | None = None):
        self.pins: list[Pin] = pins or []

    @property
    def head(self) -> str:
        return self.pins[-1].digest if self.pins else GENESIS

    def commit(self, tau: int, summary: str) -> Pin:
        parent = self.head
        pin = Pin(tau=tau, summary=summary[:200], parent=parent,
                  digest=sha(str(tau), summary, parent)[:16])
        self.pins.append(pin)
        return pin

    def verify(self) -> bool:
        """Replay the chain — every pin's digest must equal hash(tau ‖ summary ‖ parent)
        and its parent must equal the previous head. A single flip anywhere fails this."""
        parent = GENESIS
        for p in self.pins:
            if p.parent != parent:
                return False
            if p.digest != sha(str(p.tau), p.summary, p.parent)[:16]:
                return False
            parent = p.digest
        return True

    def resume_stats(self) -> dict[str, Any]:
        return {"pins": len(self.pins), "head": self.head, "intact": self.verify()}

    def to_dict(self) -> dict[str, Any]:
        return {"pins": [p.to_dict() for p in self.pins]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Ledger":
        pins = [Pin(**{k: v for k, v in p.items() if k in Pin.__annotations__})
                for p in (d or {}).get("pins", [])]
        return cls(pins=pins)
