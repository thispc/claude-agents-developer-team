"""The competency graph — subdomains done right.

Experience is not "domain += 1". Competencies form a materialised-path tree
(`eng.backend.auth`) with optional cross-edges where skill genuinely transfers
(`negotiation`, shared by law and sales). A scan credits its leaf by an *engagement
weight* (a hard deliberation teaches more than a reflex) and the gain propagates to
neighbours, decayed and capped at a couple of hops. Proficiency is the saturating curve
over effective xp; unused skills fade slowly, so mastery you never practise erodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .util import saturating

# Static cross-edges: leaf/ancestor path -> {neighbour: transfer coefficient}. Ancestor
# propagation up the tree is automatic (below); this table is only for *sideways*
# transfer between branches, added where a real shared skill exists.
CROSS_EDGES: dict[str, dict[str, float]] = {
    "law.contract": {"negotiation": 0.4},
    "sales": {"negotiation": 0.5},
    "eng.backend.auth": {"security": 0.35},
}

ANCESTOR_DECAY = 0.5      # xp flowing one level up the tree keeps this fraction
CROSS_CAP_HOPS = 1        # sideways transfer is one hop only — cheap and bounded
FORGET_PER_SLEEP = 0.002  # unused skill erosion, applied at consolidation


@dataclass
class Skills:
    xp: dict[str, float] = field(default_factory=dict)   # competency path -> raw experience

    def credit(self, domain: str, engagement: float) -> None:
        """Spend `engagement` scans of experience on a leaf competency, then let it flow
        up the ancestors (decayed) and across any cross-edges (one hop)."""
        if not domain or engagement <= 0:
            return
        self._add(domain, engagement)
        # up the tree: eng.backend.auth -> eng.backend -> eng, each decayed
        parts = domain.split(".")
        gain = engagement
        for i in range(len(parts) - 1, 0, -1):
            gain *= ANCESTOR_DECAY
            self._add(".".join(parts[:i]), gain)
        # sideways: any cross-edge from this leaf or an ancestor
        for src in (domain, *[".".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]):
            for nbr, coef in CROSS_EDGES.get(src, {}).items():
                self._add(nbr, engagement * coef)

    def _add(self, path: str, amount: float) -> None:
        self.xp[path] = round(self.xp.get(path, 0.0) + amount, 4)

    def level(self, domain: str) -> float:
        """Proficiency 0..1 at a node — its own xp plus a discounted read of its
        children (being good at every sub-area makes you good at the whole)."""
        own = self.xp.get(domain, 0.0)
        kids = sum(v for k, v in self.xp.items()
                   if k.startswith(domain + ".")) * ANCESTOR_DECAY
        return saturating(own + kids)

    def forget(self, active_domains: set[str], rate: float = FORGET_PER_SLEEP) -> None:
        """Erode competencies not exercised recently. Called at sleep, so it costs
        nothing on its own and keeps the graph honest rather than ever-growing."""
        for path in list(self.xp):
            if not any(path == d or path.startswith(d.split(".")[0]) for d in active_domains):
                self.xp[path] = max(0.0, self.xp[path] - rate)

    def resume(self, top: int = 5) -> list[tuple[str, float]]:
        """The agent's strongest competencies, for casting and the profile card."""
        ranked = sorted(((p, self.level(p)) for p in self.xp), key=lambda x: -x[1])
        return [(p, round(lvl, 3)) for p, lvl in ranked[:top]]

    def to_dict(self) -> dict[str, Any]:
        return {"xp": dict(self.xp)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Skills":
        return cls(xp={k: float(v) for k, v in (d or {}).get("xp", {}).items()})
