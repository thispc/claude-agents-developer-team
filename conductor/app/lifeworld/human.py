"""H — a human: a Being that composes a whole inner life behind one verb, `perceive`.

Inheritance for the *taxonomy* (Entity → Being → Human); composition for the *mind* — a
Human HAS a psyche, senses, memory, skills, drives, a rule engine, a social graph, and a
ledger, each a small class with a clean interface. That split is the whole point: the
type tree stays shallow and honest, while capabilities are swappable parts, so nothing is
a tangle and every piece is testable alone.

`perceive` is the reflex arc, and it reads like one: sense → attention gate → try a free
habit → else deliberate (the one possible spend) → apply the clamped consequence → age.
Every branch that isn't the deliberation is free. An idle human costs nothing.
"""

from __future__ import annotations

from typing import Any

from .entity import Being, register
from .types import Packet, Signal
from .psyche import Psyche
from .senses import Senses, ATTENTION_FLOOR
from .memory import Memory
from .skills import Skills
from .drives import Drives
from .rules import RuleEngine
from .social import Social
from .ledger import Ledger

# How much a scan teaches, by the tier that answered it: a hard deliberation teaches more
# than a reflex. Scaled by the signal's stakes.
ENGAGE = {0: 0.3, 1: 0.4, 2: 1.0}


@register
class Human(Being):
    kind = "human"

    def __init__(self, id: int, name: str = "", pos: tuple = (0.0, 0.0), tau: int = 0,
                 psyche: Psyche | None = None, narrative: str = ""):
        super().__init__(id, name, pos, tau)
        self.psyche = psyche or Psyche()
        self.senses = Senses()
        self.memory = Memory()
        self.skills = Skills()
        self.drives = Drives()
        self.rules = RuleEngine()
        self.social = Social()
        self.narrative = narrative or f"{name}, newly arrived."
        self.last_action: dict[str, Any] = {}

    @classmethod
    def newborn(cls, id: int, name: str, *, dials: dict | None = None,
                senses: list | None = None) -> "Human":
        h = cls(id, name, psyche=Psyche.newborn(dials))
        if senses:
            h.senses.active = list(senses)
        return h

    # --- the reflex arc -----------------------------------------------------

    async def perceive(self, signal: Signal, world) -> Packet:
        flags = world.flags_for(self)
        s = self.senses.receive(signal)
        if s is None:
            return Packet(understood="(unsensed)")
        goal, pressure = self.drives.dominant_goal()
        drive_rel = 1.0 if (s.domain and goal in s.domain) else 0.3
        if self.senses.attention(s, drive_rel) < ATTENTION_FLOOR:
            self.memory.buffer.append(s.text())          # noticed, ignored, free
            self._background()
            return Packet(understood="(ignored)")

        ctx = self._ctx(s)
        packet = self.rules.reflex(s, ctx) if flags.on("rule_compiler") else None
        if packet is None:
            packet = await world.appraise(self, s, ctx)  # Tier 0 (free) or Tier 2 (spend)
            # Learn from any real deliberation, not only the paid one: a habit compiled
            # from Tier 0 is still a genuine reflex, and in production (Tier 2) it is what
            # stops re-paying the model for a reaction the agent has already settled.
            if flags.on("rule_compiler") and packet.tier != 1:
                self.rules.observe(s, packet, ctx, self.tau)

        self._apply(s, packet, flags, pressure)
        return packet

    def _ctx(self, s: Signal) -> dict:
        """What the tiers need beyond the raw signal — chiefly whether the sender is
        trusted, which is how the same words from a friend and a stranger diverge."""
        trust = self.social.trusts(s.from_id) if s.from_id else 0.5
        return {"from_trusted": trust > 0.6, "tone": s.payload.get("tone", "")}

    def _apply(self, s: Signal, p: Packet, flags, goal_pressure: float) -> None:
        self.psyche.apply(p.mood, p.vitals, p.traits,
                          mood_volatility=flags.on("mood_volatility") or not flags.on("emotions"))
        if flags.on("drives"):
            self.drives.apply(p.drives)
        if flags.on("skill_growth") and s.domain:
            self.skills.credit(s.domain, ENGAGE.get(p.tier, 0.3) * s.stakes)
        if flags.on("memory"):
            # A stranger's hello is forgettable; a friend's betrayal is not. Weight the
            # social salience by the *strength* of the bond, not merely that a name exists.
            strength = abs(self.social.trusts(s.from_id) - 0.5) * 2 if s.from_id else 0.0
            self.memory.remember(p, s, self.psyche.salience_weights(), self.tau,
                                 goal_pressure, social_strength=strength)
        for oid, deltas in (p.social or {}).items():
            try:
                self.social.update(int(oid), deltas, self.tau, drama_on=flags.on("theory_of_mind"))
            except (ValueError, TypeError):
                pass
        self.last_action = p.action or {}
        self._background()
        self.age(p.understood or s.kind, changed=not p.is_noop(), ledger_on=flags.on("ledger"))

    def _background(self) -> None:
        """The free per-scan housekeeping every human does: mood drifts toward baseline
        (homeostasis — a bad moment fades), needs deplete, memory dims, organs wear a
        hair. None of it spends, and it is what keeps a person a person."""
        self.psyche.relax(0.04)
        self.drives.tick()
        self.memory.decay()
        self.senses.wear("hearing")

    def sleep(self, active_domains: set[str] | None = None) -> int:
        """Consolidate the day. A night's rest resets mood substantially, folds episodic →
        semantic, forgets unpractised skills, and refreshes the self-narrative from what
        actually happened. Free (deterministic)."""
        self.psyche.relax(0.6)
        folded = self.memory.sleep(active_domains)
        self.skills.forget(active_domains or set())
        top = ", ".join(f"{p.split('.')[-1]}" for p, _ in self.skills.resume(3))
        goal, _ = self.drives.dominant_goal()
        self.narrative = f"{self.name}. Known for {top or 'nothing yet'}. Right now, seeks {goal}."[:280]
        return folded

    # --- what the agent brings when it speaks or is appraised ---------------

    def self_view(self) -> str:
        return (f"{self.narrative}\nMood: "
                + ", ".join(f"{k} {v:.2f}" for k, v in self.psyche.mood.items())
                + "\nRecall: " + self.memory.recall()[:400])

    def profile(self) -> dict[str, Any]:
        """The résumé card — identity, dominant drive, top skills, verified ledger."""
        return {"id": self.id, "name": self.name, "tau": self.tau,
                "narrative": self.narrative, "mood": self.psyche.mood,
                "traits": self.psyche.traits, "wants": self.drives.dominant_goal(),
                "skills": self.skills.resume(), "resume": self.ledger.resume_stats(),
                "memories": len(self.memory.episodic), "habits": len(self.rules.rules)}

    # --- serialisation: one JSON blob, each subsystem folds itself in -------

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d.update(narrative=self.narrative, last_action=self.last_action,
                 psyche=self.psyche.to_dict(), senses=self.senses.to_dict(),
                 memory=self.memory.to_dict(), skills=self.skills.to_dict(),
                 drives=self.drives.to_dict(), rules=self.rules.to_dict(),
                 social=self.social.to_dict(), ledger=self.ledger.to_dict())
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Human":
        h = cls(id=d["id"], name=d.get("name", ""), pos=tuple(d.get("pos", (0, 0))),
                tau=int(d.get("tau", 0)), psyche=Psyche.from_dict(d.get("psyche")),
                narrative=d.get("narrative", ""))
        h.senses = Senses.from_dict(d.get("senses"))
        h.memory = Memory.from_dict(d.get("memory"))
        h.skills = Skills.from_dict(d.get("skills"))
        h.drives = Drives.from_dict(d.get("drives"))
        h.rules = RuleEngine.from_dict(d.get("rules"))
        h.social = Social.from_dict(d.get("social"))
        h.ledger = Ledger.from_dict(d.get("ledger"))
        h.last_action = d.get("last_action", {})
        return h
