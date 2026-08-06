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
from .decisions import DecisionLog, signature
from .memory import Memory
from .skills import Skills
from .drives import Drives
from .rules import RuleEngine
from .social import Social
from .ledger import Ledger

# How much a scan teaches, by the tier that answered it: a hard deliberation teaches more
# than a reflex. Scaled by the signal's stakes.
ENGAGE = {0: 0.3, 1: 0.4, 2: 1.0}

# How recently an agent must have spent to count as working on something. Long enough to
# cover the gap between calls in one deliberation round, short enough that it stops glowing
# soon after the round ends.
BUSY_SECONDS = 90


@register
class Human(Being):
    kind = "human"

    def __init__(self, id: int, name: str = "", pos: tuple = (0.0, 0.0), tau: int = 0,
                 psyche: Psyche | None = None, narrative: str = "", figure: str = "",
                 model: str = ""):
        super().__init__(id, name, pos, tau)
        self.figure = figure                         # the chosen figurine/icon (how they look)
        self.model = model                           # the LLM this agent possesses ("" = inherit the world default)
        self.psyche = psyche or Psyche()
        self.senses = Senses()
        self.memory = Memory()
        # What it decided and why, and what it has learned to expect. Free to keep: every
        # field comes from the packet the appraisal already returned.
        self.decisions = DecisionLog()
        self.skills = Skills()
        self.drives = Drives()
        self.rules = RuleEngine()
        self.social = Social()
        self.narrative = narrative or f"{name}, newly arrived."
        self.last_action: dict[str, Any] = {}
        self.flag_overrides: dict[str, bool] = {}    # the AGENT flag layer (world < scene < agent)
        self.spends: list[float] = []                # wall-clock timestamps of this agent's model-uses (session usage)

    @classmethod
    def newborn(cls, id: int, name: str, *, dials: dict | None = None,
                senses: list | None = None, figure: str = "", model: str = "") -> "Human":
        h = cls(id, name, psyche=Psyche.newborn(dials), figure=figure, model=model)
        if senses:
            h.senses.active = list(senses)
        return h

    # --- the reflex arc -----------------------------------------------------

    async def perceive(self, signal: Signal, world, free: bool = False) -> Packet:
        flags = world.flags_for(self)
        s = self.senses.receive(signal)
        if s is None:
            return Packet(understood="(unsensed)")
        goal, pressure = self.drives.dominant_goal()
        drive_rel = 1.0 if (s.domain and goal in s.domain) else 0.3
        if self.senses.attention(s, drive_rel) < ATTENTION_FLOOR:
            self.memory.note(s.text())                   # noticed, ignored, free — buffer stays bounded
            self._background()
            return Packet(understood="(ignored)")

        ctx = self._ctx(s)
        # THE CACHE READ. If this agent has met this kind of situation before and what it
        # concluded then held up, that conclusion goes in before anything is asked of a
        # model — the point of remembering is not having to think again.
        sig = signature(s.text())
        known = self.decisions.recall(sig)
        if known is not None:
            ctx["recalled"] = {"sig": known.sig, "says": known.says,
                               "confidence": known.confidence, "seen": known.evidence}
        packet = self.rules.reflex(s, ctx) if flags.on("rule_compiler") else None
        if packet is None:
            packet = await world.appraise(self, s, ctx, free=free)  # Tier 0 (free) or Tier 2 (spend)
            # Learn from any real deliberation, not only the paid one: a habit compiled
            # from Tier 0 is still a genuine reflex, and in production (Tier 2) it is what
            # stops re-paying the model for a reaction the agent has already settled.
            if flags.on("rule_compiler") and packet.tier != 1:
                self.rules.observe(s, packet, ctx, self.tau)

        self._apply(s, packet, flags, pressure)
        self._note_decision(s, packet, sig, goal, pressure, known)
        return packet

    def _note_decision(self, s: Signal, p: Packet, sig: str, goal: str,
                       pressure: float, known) -> None:
        """Record the decision and the causes we already computed. Costs nothing.

        Only decisions with an actual choice in them are kept — an agent that logs every
        ignored signal fills its own tree with noise and buries the moments that turned."""
        act = (p.action or {}).get("kind") or ""
        if not act and not p.understood:
            return
        # A Tier-0 packet's `understood` is diagnostic — "say (i=0.30)" is the appraiser
        # telling itself how intense the signal was, not a thing the agent decided. Recording
        # it makes a decision tree that reads like a debugger, so a reflex records what it
        # REACTED TO instead.
        told = p.understood if p.tier != 0 else (str(s.text() or "")[:120] or p.understood)
        try:
            self.decisions.record(
                tau=self.tau, sig=sig, saw=s.text(), understood=told,
                chose=(f"{act}: {(p.action or {}).get('text', '')}".strip(": ")
                       if p.tier != 0 else (act or "reacted")),
                because={"wanted": goal, "pressure": round(float(pressure), 3),
                         "mood": max(p.mood.items(), key=lambda kv: abs(kv[1]))[0] if p.mood else "",
                         "tier": p.tier, "recalled": bool(known)},
                parents=[known_id for known_id in ([] if known is None else
                         [n.id for n in self.decisions.nodes[-12:] if n.sig == sig][-2:])],
                scope="private" if s.payload.get("secret") else "public")
        except Exception:
            pass                      # a diary must never be able to break the day

    def _ctx(self, s: Signal) -> dict:
        """What the tiers need beyond the raw signal — chiefly whether the sender is
        trusted, which is how the same words from a friend and a stranger diverge."""
        trust = self.social.trusts(s.from_id) if s.from_id else 0.5
        return {"from_trusted": trust > 0.6, "tone": s.payload.get("tone", "")}

    def _apply(self, s: Signal, p: Packet, flags, goal_pressure: float) -> None:
        # Full mood swings only when both emotions AND volatility are on. Serious mode
        # (volatility off) or emotions off both DAMPEN — the earlier `or not emotions`
        # was inverted and made an unemotional world the most volatile one.
        self.psyche.apply(p.mood, p.vitals, p.traits,
                          mood_volatility=flags.on("mood_volatility") and flags.on("emotions"))
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

    # --- model session usage: an agent that burns its quota sleeps until the window passes ----
    @staticmethod
    def _session_params():
        try:                                    # runtime-tunable first (a spend door like any other)…
            from .. import tuning
            return (max(1, int(tuning.get("agent_session_cap"))),
                    max(1, int(tuning.get("agent_session_window_s"))))
        except Exception:
            pass
        try:                                    # …env-only config next, hard defaults last
            from ..config import AGENT_SESSION_CAP, AGENT_SESSION_WINDOW_S
            return max(1, AGENT_SESSION_CAP), max(1, AGENT_SESSION_WINDOW_S)
        except Exception:
            return 30, 5 * 3600

    def _now(self, now=None):
        if now is not None:
            return now
        import time
        return time.time()

    def note_spend(self, now=None) -> None:
        """Record one use of this agent's model (a Tier-2 call on its behalf) against its session."""
        now = self._now(now)
        _, win = self._session_params()
        self.spends = [t for t in self.spends if now - t < win]
        self.spends.append(now)

    def usage(self, now=None) -> dict[str, Any]:
        now = self._now(now)
        cap, win = self._session_params()
        self.spends = [t for t in self.spends if now - t < win]
        used = len(self.spends)
        asleep = used >= cap
        # "Busy" is free: the agent already records when it last spent, and a call inside the
        # last minute-and-a-half means it is thinking about something right now. Worth showing
        # on the canvas — you can still talk to a busy agent, but you should be able to SEE
        # that it is mid-task rather than idle.
        last = max(self.spends) if self.spends else 0.0
        return {"used": used, "cap": cap, "frac": round(min(1.0, used / cap), 3),
                "asleep": asleep, "wakes_at": (min(self.spends) + win) if (asleep and self.spends) else 0.0,
                "busy": bool(last and now - last < BUSY_SECONDS), "last_spend": last,
                "window": win, "model": self.model}

    def asleep(self, now=None) -> bool:
        cap, win = self._session_params()
        now = self._now(now)
        return len([t for t in self.spends if now - t < win]) >= cap

    def profile(self) -> dict[str, Any]:
        """The résumé card — identity, dominant drive, top skills, verified ledger."""
        return {"id": self.id, "name": self.name, "tau": self.tau, "figure": self.figure,
                "pos": list(self.pos), "model": self.model,
                "narrative": self.narrative, "mood": self.psyche.mood,
                "traits": self.psyche.traits, "wants": self.drives.dominant_goal(),
                "skills": self.skills.resume(), "resume": self.ledger.resume_stats(),
                "memories": len(self.memory.episodic), "habits": len(self.rules.rules)}

    # --- serialisation: one JSON blob, each subsystem folds itself in -------

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d.update(narrative=self.narrative, last_action=self.last_action, figure=self.figure,
                 model=self.model, flag_overrides=self.flag_overrides, spends=self.spends,
                 psyche=self.psyche.to_dict(), senses=self.senses.to_dict(),
                 memory=self.memory.to_dict(), skills=self.skills.to_dict(),
                 drives=self.drives.to_dict(), rules=self.rules.to_dict(),
                 social=self.social.to_dict(), ledger=self.ledger.to_dict(),
                 decisions=self.decisions.to_dict())
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Human":
        h = cls(id=d["id"], name=d.get("name", ""), pos=tuple(d.get("pos", (0, 0))),
                tau=int(d.get("tau", 0)), psyche=Psyche.from_dict(d.get("psyche")),
                narrative=d.get("narrative", ""), figure=d.get("figure", ""))
        h.senses = Senses.from_dict(d.get("senses"))
        h.memory = Memory.from_dict(d.get("memory"))
        h.skills = Skills.from_dict(d.get("skills"))
        h.drives = Drives.from_dict(d.get("drives"))
        h.rules = RuleEngine.from_dict(d.get("rules"))
        h.social = Social.from_dict(d.get("social"))
        h.ledger = Ledger.from_dict(d.get("ledger"))
        h.decisions = DecisionLog.from_dict(d.get("decisions"))
        h.last_action = d.get("last_action", {})
        h.model = d.get("model", "")
        h.flag_overrides = d.get("flag_overrides", {})
        h.spends = list(d.get("spends", []) or [])
        return h
