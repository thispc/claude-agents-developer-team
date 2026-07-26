"""The Rule Compiler — the moat.

A human deliberates over how to hold a fork exactly once; then it is a reflex. Our agents
compile experience into reflexes the same way, so an agent gets *cheaper* as it gets
*better*: token cost is inversely proportional to experience.

Three tiers, cheapest that can answer wins:
  0 · Reflex        shipped deterministic rules (the seed appraiser)     free
  1 · Habit         rules the agent compiled from its own deliberations  free
  2 · Deliberation  one bounded model call — the only thing that spends  1 call

A habit is a typed row — a match pattern and a delta template — interpreted by a fixed
evaluator, NEVER a string that is exec'd (the RCE door stays shut). New habits are
probationary and their confidence decays if reality contradicts them, so a habit compiled
from a biased streak is caught before it calcifies. On conflict, the most specific rule
wins (CSS-style), so a precise habit is never overridden by a general one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .types import Packet, Signal

PROMOTE_AFTER = 3        # identical deliberations before a habit is compiled
CONFIDENCE_START = 0.6
CONFIDENCE_RETIRE = 0.25
CONFIDENCE_STEP = 0.08


@dataclass
class Rule:
    """when a signal matches `match`, emit `emit` (a packet template). Confidence rises
    when it agrees with a later deliberation and falls when it doesn't."""
    match: dict[str, Any]                 # {kind, tone?, from_trusted?} — subset must match
    emit: dict[str, Any]                  # a Packet.to_dict() template
    confidence: float = CONFIDENCE_START
    fires: int = 0
    born_tau: int = 0

    def specificity(self) -> int:
        return len(self.match)            # more matched keys = more specific

    def matches(self, signal: Signal, ctx: dict) -> bool:
        for k, want in self.match.items():
            got = ctx.get(k, getattr(signal, k, signal.payload.get(k)))
            if got != want:
                return False
        return True


class RuleEngine:
    """Holds an agent's learned habits and the counter that decides when to compile one."""

    def __init__(self, rules: list[Rule] | None = None, tally: dict | None = None):
        self.rules: list[Rule] = rules or []
        self._tally: dict[str, dict] = tally or {}      # signature -> {count, emit}

    # --- tier 1 lookup ------------------------------------------------------

    def reflex(self, signal: Signal, ctx: dict) -> Packet | None:
        """The most specific, confident habit that matches — or None to fall through to
        deliberation. Free."""
        best: Rule | None = None
        for r in self.rules:
            if r.confidence >= CONFIDENCE_RETIRE and r.matches(signal, ctx):
                if best is None or r.specificity() > best.specificity():
                    best = r
        if not best:
            return None
        best.fires += 1
        p = Packet.from_dict(best.emit)
        p.tier = 1
        return p

    # --- learning: compile, confirm, contradict -----------------------------

    def observe(self, signal: Signal, packet: Packet, ctx: dict, tau: int) -> None:
        """Feed a Tier-2 result back. If the same signal shape keeps yielding the same
        consequence shape, compile a habit; if an existing habit agreed, reinforce it;
        if it disagreed, erode it."""
        sig = self._signature(signal, ctx)
        shape = self._shape(packet)
        # reinforce / contradict existing habits that matched
        for r in self.rules:
            if r.matches(signal, ctx):
                agree = self._shape(Packet.from_dict(r.emit)) == shape
                r.confidence = min(1.0, r.confidence + CONFIDENCE_STEP) if agree \
                    else max(0.0, r.confidence - CONFIDENCE_STEP)
        self.rules = [r for r in self.rules if r.confidence >= CONFIDENCE_RETIRE or r.fires < 2]
        # count toward compiling a new one
        rec = self._tally.setdefault(sig, {"count": 0, "shape": shape, "emit": None})
        if rec["shape"] == shape:
            rec["count"] += 1
            rec["emit"] = packet.to_dict()
        else:
            rec.update(count=1, shape=shape, emit=packet.to_dict())
        if rec["count"] >= PROMOTE_AFTER and not self._has_rule(signal, ctx, shape):
            self.rules.append(Rule(match=self._match_of(signal, ctx),
                                   emit=rec["emit"], born_tau=tau))

    # --- helpers ------------------------------------------------------------

    def _match_of(self, signal: Signal, ctx: dict) -> dict:
        m = {"kind": signal.kind}
        if "tone" in signal.payload:
            m["tone"] = signal.payload["tone"]
        if ctx.get("from_trusted") is not None:
            m["from_trusted"] = ctx["from_trusted"]
        return m

    def _signature(self, signal: Signal, ctx: dict) -> str:
        return f"{signal.kind}|{signal.payload.get('tone','')}|{ctx.get('from_trusted','')}"

    def _shape(self, packet: Packet) -> str:
        """A coarse fingerprint of a consequence — its sign pattern, not exact values —
        so 'confidence down, stress up' from two events counts as the same reflex."""
        keys = []
        for fam in (packet.mood, packet.vitals):
            for k, v in sorted(fam.items()):
                keys.append(f"{k}{'+' if v >= 0 else '-'}")
        return ",".join(keys) + "|" + str(packet.action.get("kind", ""))

    def _has_rule(self, signal: Signal, ctx: dict, shape: str) -> bool:
        return any(r.matches(signal, ctx) and self._shape(Packet.from_dict(r.emit)) == shape
                   for r in self.rules)

    def to_dict(self) -> dict[str, Any]:
        return {"rules": [r.__dict__.copy() for r in self.rules], "tally": self._tally}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RuleEngine":
        d = d or {}
        rules = [Rule(**{k: v for k, v in r.items() if k in Rule.__annotations__})
                 for r in d.get("rules", [])]
        return cls(rules=rules, tally=d.get("tally", {}))
