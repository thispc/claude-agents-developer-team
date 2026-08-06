"""What an agent decided, why, and what it learned from how it turned out.

An episode in `memory.py` is a sentence: "the suite went red". That is a memory of an EVENT.
This module records the memory of a DECISION — the moment the agent settled on something —
and keeps the causes attached, because "I chose X" is not useful six turns later without
"…because Y, while under Z".

Three things live here, and they build on each other:

    Decision   one node: what it saw, what it made of it, what it chose, and the causes we
               already computed at that instant. Assembled from the Packet the appraisal
               returned, so recording it costs NOTHING — no extra model call, no new pass.

    canon      a decision that many later decisions descend from. Canon is MEASURED, not
               declared: a pivot is a pivot because things turned on it, and in-degree in
               the tree says so without anyone labelling it. When one is later shown to be
               wrong, `invalidate` walks its descendants — everything downstream of a bad
               premise deserves a second look, which is the whole reason to keep the tree.

    Assoc      the useful part. A SIGNATURE of a situation ("http:505", "verify:ImportError")
               mapped to what the agent concluded last time, with the record of how often
               that conclusion preceded a good outcome. Looked up in O(1) BEFORE any model
               call, so a situation the agent has met before costs nothing to handle — a
               cache in the strict sense, where a hit means it does not have to think.

The evidence bar matters more than the mechanism. An association drawn from one coincidence
is superstition, and an agent that acts on superstition is worse than one with no memory at
all — so nothing influences anything until `MIN_EVIDENCE` outcomes agree, and a conclusion
that keeps preceding failure decays out of use on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Caps. These live inside the world blob, which is loaded and saved whole, so an unbounded
# tree makes every save slower forever. Canon nodes are exempt from pruning: they are, by
# construction, the ones later decisions still point at.
MAX_DECISIONS = 180
MAX_ASSOC = 80

# How many agreeing outcomes before an association is allowed to influence anything.
MIN_EVIDENCE = 2
# How many descendants make a decision canon.
CANON_DEGREE = 3


def signature(text: str, kind: str = "") -> str:
    """A cheap, stable fingerprint of a SITUATION — the cache key.

    Deliberately coarse and deterministic: the point is that the same kind of trouble
    produces the same key on a different day, in a different file, with different wording.
    "HTTP 505 from api.example.com/v2/users" and "505 talking to the billing host" are the
    same lesson, and an agent that files them separately never learns it.
    """
    t = " ".join(str(text or "").split())[:400]
    if not t:
        return ""
    m = re.search(r"\b([1-5]\d{2})\b(?=\s|$|[^\d])", t)
    if m and int(m.group(1)) >= 400:
        return f"http:{m.group(1)}"
    m = re.search(r"\b([A-Z][A-Za-z]*(?:Error|Exception|Warning))\b", t)
    if m:
        return f"error:{m.group(1)}"
    m = re.search(r"\b(timed? ?out|timeout|connection refused|permission denied|"
                  r"not found|rate limit|out of turns|conflict)\b", t, re.I)
    if m:
        return f"cond:{m.group(1).lower().replace(' ', '_')}"
    if kind:
        return f"{kind}:{re.sub(r'[^a-z0-9]+', '_', t.lower())[:40].strip('_')}"
    return ""


@dataclass
class Decision:
    id: int
    tau: int
    sig: str = ""                                   # the situation, fingerprinted
    saw: str = ""                                   # what arrived, one line
    understood: str = ""                            # what it made of it
    chose: str = ""                                 # what it did about it
    because: dict = field(default_factory=dict)     # the causes, as computed at the time
    parents: list = field(default_factory=list)     # decision ids it drew on
    outcome: str = ""                               # "", "good", "bad" — stamped later
    scope: str = "public"                           # a private decision stays private
    canon: bool = False
    stale: bool = False                             # a premise below it turned out wrong

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Assoc:
    sig: str
    says: str
    good: int = 0
    bad: int = 0
    tau: int = 0

    @property
    def evidence(self) -> int:
        return self.good + self.bad

    @property
    def confidence(self) -> float:
        """How much to believe it. Unproven associations sit at zero rather than at a hopeful
        half: silence is the honest answer before the evidence is in."""
        if self.evidence < MIN_EVIDENCE:
            return 0.0
        return round(self.good / max(1, self.evidence), 3)

    def to_dict(self) -> dict[str, Any]:
        return {"sig": self.sig, "says": self.says, "good": self.good, "bad": self.bad,
                "tau": self.tau, "confidence": self.confidence, "evidence": self.evidence}


class DecisionLog:
    """One agent's tree of decisions, plus what it has learned to expect."""

    def __init__(self, nodes: list[Decision] | None = None,
                 assoc: dict[str, Assoc] | None = None, seq: int = 0):
        self.nodes: list[Decision] = nodes or []
        self.assoc: dict[str, Assoc] = assoc or {}
        self.seq = seq

    # --- recording ---------------------------------------------------------

    def record(self, tau: int, saw: str, understood: str, chose: str,
               because: dict | None = None, sig: str = "", parents: list | None = None,
               scope: str = "public") -> Decision:
        self.seq += 1
        d = Decision(id=self.seq, tau=tau, sig=sig or signature(saw), saw=str(saw)[:200],
                     understood=str(understood)[:200], chose=str(chose)[:200],
                     because=because or {}, parents=list(parents or []), scope=scope)
        self.nodes.append(d)
        self._mark_canon()
        self._prune()
        return d

    def get(self, did: int) -> Decision | None:
        return next((n for n in self.nodes if n.id == did), None)

    def _mark_canon(self) -> None:
        deg: dict[int, int] = {}
        for n in self.nodes:
            for p in n.parents:
                deg[p] = deg.get(p, 0) + 1
        for n in self.nodes:
            # Sticky. Canon is a fact about what HAPPENED — things turned on this — and
            # pruning the descendants that proved it does not un-prove it. Recomputing from
            # live in-degree alone made a pivot lose its status the moment the tree filled
            # up, and then it was pruned like any other node, orphaning everything below.
            n.canon = n.canon or deg.get(n.id, 0) >= CANON_DEGREE

    def _prune(self) -> None:
        """Drop the oldest ordinary nodes when over cap. Canon survives — it is what the
        rest of the tree still hangs from, and pruning it would orphan everything below."""
        if len(self.nodes) <= MAX_DECISIONS:
            return
        keep_canon = [n for n in self.nodes if n.canon]
        rest = [n for n in self.nodes if not n.canon]
        rest = rest[-(MAX_DECISIONS - len(keep_canon)):] if len(keep_canon) < MAX_DECISIONS else []
        self.nodes = sorted(keep_canon + rest, key=lambda n: n.id)

    # --- outcomes, and what they teach -------------------------------------

    def resolve(self, did: int, outcome: str, says: str = "") -> None:
        """Stamp how a decision turned out, and let its situation learn from that.

        This is the only place an association's evidence moves. Learning from the OUTCOME
        rather than from the decision is the whole point: an agent that reinforces whatever
        it happened to think is not learning, it is calcifying.
        """
        d = self.get(did)
        if not d or outcome not in ("good", "bad"):
            return
        d.outcome = outcome
        if not d.sig:
            return
        a = self.assoc.get(d.sig) or Assoc(sig=d.sig, says=says or d.understood)
        if says and outcome == "good":
            a.says = says                       # the conclusion that actually worked
        setattr(a, outcome, getattr(a, outcome) + 1)
        a.tau = max(a.tau, d.tau)
        self.assoc[d.sig] = a
        self._trim_assoc()

    def _trim_assoc(self) -> None:
        if len(self.assoc) <= MAX_ASSOC:
            return
        # Keep what has been most useful; drop the weakest and least recent first.
        ranked = sorted(self.assoc.values(), key=lambda a: (a.confidence, a.evidence, a.tau))
        for a in ranked[:len(self.assoc) - MAX_ASSOC]:
            self.assoc.pop(a.sig, None)

    def recall(self, sig: str) -> Assoc | None:
        """What this agent expects of this situation — the cache read. O(1), free, and silent
        until the evidence bar is met."""
        a = self.assoc.get(sig or "")
        return a if a and a.confidence >= 0.5 and a.evidence >= MIN_EVIDENCE else None

    # --- when a premise turns out to be wrong ------------------------------

    def invalidate(self, did: int) -> int:
        """Mark everything downstream of a bad decision as worth revisiting.

        Not deletion: the tree is the record of what the agent believed and when, and erasing
        it would hide the very mistake worth remembering. `stale` says "this rested on
        something that turned out to be wrong" — which is exactly what you want to see
        highlighted when you open the tree and ask how it ended up here.
        """
        root = self.get(did)
        if not root:
            return 0
        root.stale = True
        touched, frontier = 0, {did}
        while frontier:
            nxt = set()
            for n in self.nodes:
                if n.stale or not (set(n.parents) & frontier):
                    continue
                n.stale = True
                touched += 1
                nxt.add(n.id)
            frontier = nxt
        return touched

    # --- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "nodes": [n.to_dict() for n in self.nodes],
                "assoc": {k: {"sig": a.sig, "says": a.says, "good": a.good,
                              "bad": a.bad, "tau": a.tau} for k, a in self.assoc.items()}}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "DecisionLog":
        d = d or {}
        nodes = []
        for raw in d.get("nodes", []) or []:
            try:
                nodes.append(Decision(**{k: v for k, v in raw.items()
                                         if k in Decision.__dataclass_fields__}))
            except Exception:
                continue
        assoc = {}
        for k, raw in (d.get("assoc") or {}).items():
            try:
                assoc[k] = Assoc(sig=raw.get("sig", k), says=raw.get("says", ""),
                                 good=int(raw.get("good", 0)), bad=int(raw.get("bad", 0)),
                                 tau=int(raw.get("tau", 0)))
            except Exception:
                continue
        return cls(nodes=nodes, assoc=assoc, seq=int(d.get("seq", 0) or 0))
