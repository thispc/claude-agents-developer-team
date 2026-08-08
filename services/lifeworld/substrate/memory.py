"""The memory palace — a black box behind three methods.

    remember(packet, signal)  free   — decides IF (salience gate) and WHERE this lands
    recall(context)           free   — what the agent brings to mind now, bounded
    sleep()                   free*   — folds episodic into semantic, forgets the trivia

Only intense things persist — emotional intensity or a spectacular finding — and both
show up as a large packet, which we already compute, so "intense" is a free number. The
vast majority of a life is correctly forgotten at zero cost; that is the token firewall.
(*sleep is deterministic here; a model consolidator can be injected later for richer
folding, at which point it becomes the one budgeted spender besides deliberation.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .util import clamp01
from .types import Packet, Signal

SALIENCE_THRESHOLD = 0.35     # below this, an event evaporates unremembered
CHAR_CAP = 1200               # the recalled blob is bounded — never crowds out the task
DECAY = 0.04                  # per-scan erosion of a memory's retrieval weight
BUFFER_K = 12                 # sensory buffer depth
EPISODIC_CAP = 200            # hard ceiling; the lowest-weight memories fall off


@dataclass
class Episode:
    gist: str
    salience: float
    weight: float             # retrieval strength; decays unless reinforced
    domain: str = ""
    agents: list = field(default_factory=list)   # named others involved
    scope: str = "public"     # a secret memory must not be blurted in public
    tau: int = 0
    consolidated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Memory:
    def __init__(self, episodic: list[Episode] | None = None,
                 semantic: dict[str, str] | None = None):
        self.buffer: list[str] = []                 # last K raw gists (in-memory only)
        self.episodic: list[Episode] = episodic or []
        self.semantic: dict[str, str] = semantic or {}   # domain -> distilled facts

    # --- the salience gate -------------------------------------------------

    def salience(self, packet: Packet, signal: Signal, weights: dict[str, float],
                 goal_relevance: float = 0.0, social_strength: float = 0.0) -> float:
        """How memorable this event is — a free number over the packet we already have.
        `social_strength` is how much the other party matters to this agent (0 for a
        stranger), so a routine hello is forgotten and a friend's act is not."""
        emotion = sum(abs(v) for v in packet.mood.values())
        social = social_strength                          # relationship, not merely a name
        novelty = self._novelty(packet.understood or signal.text())
        surprise = 1.0 if packet.tier == 2 else 0.3       # deliberation implies novelty
        s = (weights["emotion"] * emotion + weights["novelty"] * novelty
             + weights["social"] * social + weights["goal"] * goal_relevance
             + weights["surprise"] * surprise * signal.stakes)
        return clamp01(s / 3.0)                            # normalise into 0..1

    def _novelty(self, gist: str) -> float:
        if not gist:
            return 0.0
        toks = set(gist.lower().split())
        for seen in self.buffer[-BUFFER_K:]:
            overlap = len(toks & set(seen.lower().split())) / max(1, len(toks))
            if overlap > 0.6:
                return 0.2                                # felt this recently
        return 1.0

    # --- remember / recall / sleep -----------------------------------------

    def note(self, gist: str) -> None:
        """Push one gist into the bounded sensory buffer — the single place the buffer is
        maintained, so it can never grow without bound however many signals are ignored."""
        if gist:
            self.buffer.append(gist)
            del self.buffer[:-BUFFER_K]

    def remember(self, packet: Packet, signal: Signal, weights: dict[str, float],
                 tau: int, goal_relevance: float = 0.0, social_strength: float = 0.0) -> bool:
        gist = packet.memory or packet.understood or signal.text()
        # Score BEFORE noting, so novelty compares the current gist against what came
        # before it — not against a buffer that already contains itself (which would make
        # everything look familiar).
        s = self.salience(packet, signal, weights, goal_relevance, social_strength)
        self.note(gist)
        if s < SALIENCE_THRESHOLD or not gist.strip():
            return False                                  # correctly forgotten, free
        self.episodic.append(Episode(
            gist=gist[:240], salience=round(s, 3), weight=round(0.5 + 0.5 * s, 3),
            domain=signal.domain, agents=[signal.from_id] if signal.from_id else [],
            scope=signal.scope, tau=tau))
        if len(self.episodic) > EPISODIC_CAP:             # evict the faintest
            self.episodic.sort(key=lambda e: -e.weight)
            del self.episodic[EPISODIC_CAP:]
        return True

    def recall(self, domain: str = "", present: set | None = None,
               audience_scope: str = "public") -> str:
        """Cost-ordered, free filters first, seal-aware. A secret memory is never
        surfaced to an audience that isn't in its circle."""
        present = present or set()
        scored: list[tuple[float, Episode]] = []
        for e in self.episodic:
            if e.scope != "public" and audience_scope == "public":
                continue                                  # don't blurt a secret in public
            rel = e.weight
            if domain and e.domain and e.domain.startswith(domain.split(".")[0]):
                rel += 0.4                                # same kind of scene
            if present & set(e.agents):
                rel += 0.5                                # someone here is in the memory
            scored.append((rel, e))
        scored.sort(key=lambda x: -x[0])
        out, size = [], 0
        for _rel, e in scored:
            line = f"- {e.gist}"
            if size + len(line) > CHAR_CAP:
                break
            out.append(line)
            size += len(line)
        sem = "\n".join(f"[{k}] {v}" for k, v in self.semantic.items())
        return (sem + "\n" + "\n".join(out)).strip()[-CHAR_CAP:]

    def decay(self) -> None:
        """One scan of forgetting — retrieval weight erodes unless reinforced; the
        flashbulb (high-salience) memories decay slowest."""
        for e in self.episodic:
            e.weight = max(0.0, e.weight - DECAY * (1.0 - e.salience))

    def sleep(self, active_domains: set[str] | None = None) -> int:
        """Consolidate: fold unconsolidated episodes into per-domain semantic facts, then
        mark them done. Deterministic and free — the floor a model consolidator can later
        improve on. Returns how many episodes were folded."""
        # Only PUBLIC episodes are folded into the (public) semantic block. A secret
        # memory (scope circle/direct) is never distilled into a store that a public
        # recall would emit — it stays episodic, where recall's scope filter guards it.
        fresh = [e for e in self.episodic if not e.consolidated and e.scope == "public"]
        if len(fresh) < 6:                                # only worth it once work piled up
            return 0
        by_domain: dict[str, list[str]] = {}
        for e in fresh:
            by_domain.setdefault(e.domain or "life", []).append(e.gist)
        for dom, gists in by_domain.items():
            prior = self.semantic.get(dom, "")
            merged = (prior + " " + " · ".join(gists)).strip()
            self.semantic[dom] = merged[-600:]            # bounded distilled facts
        for e in fresh:
            e.consolidated = True
        return len(fresh)

    def to_dict(self) -> dict[str, Any]:
        return {"episodic": [e.to_dict() for e in self.episodic], "semantic": dict(self.semantic)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Memory":
        d = d or {}
        eps = [Episode(**{k: v for k, v in e.items() if k in Episode.__annotations__})
               for e in d.get("episodic", [])]
        return cls(episodic=eps, semantic=dict(d.get("semantic", {})))
