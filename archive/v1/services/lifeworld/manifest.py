"""The manifest — a whole team as one declarative spec, and the function that
materialises it.

This is the piece the self-repair engine used to reach across a package boundary
for: `repair.py` imported `ManifestAgent`, `ManifestBody` and
`materialise_manifest` straight out of `conductor/app/lifeworld_routes.py` — a
route module — and then did deep surgery on the live `Human` objects that came
back. It lives here now, beside the substrate it drives, so both the HTTP route
(`POST /worlds/{id}/manifest`) and the crew's seating endpoint use one
implementation and nothing outside this service has to know it exists.
"""

from __future__ import annotations

import math

from fastapi import HTTPException
from pydantic import BaseModel, Field


class ManifestAgent(BaseModel):
    name: str
    model: str = ""                              # from MODEL_WHITELIST, or "" to inherit
    dials: dict = Field(default_factory=dict)    # trait -> 0-100 (or 0-1); unknown ignored
    drives: dict = Field(default_factory=dict)   # drive -> 0-1 setpoints
    brief: str = ""                              # one-line narrative, verbatim (no spend)
    figure: str = ""


class ManifestBody(BaseModel):
    """Deterministic and free to APPLY; a run spends only with ?live=1."""
    name: str = ""                               # the scene's name
    # The 1-12 bound is SCHEMA, not a hand-written check: expressed here it is in
    # the committed contract, FastAPI enforces it, and a caller sending an empty
    # team gets the same validation shape as any other bad field. Written as a
    # check it was a 422 whose body did not match the 422 the contract declared —
    # which is exactly the drift the contract tests exist to catch.
    agents: list[ManifestAgent] = Field(min_length=1, max_length=12)
    edges: list = Field(default_factory=list)    # [nameA, nameB, dir?]; dir: both|a2b|b2a
    rules: str = ""                              # the graph's rulebook
    manager: dict = Field(default_factory=dict)  # {model, budget}
    protocol: dict = Field(default_factory=dict) # deliberation policy
    run: dict = Field(default_factory=dict)      # {rounds: 1-4} → deliberate now


def clean_budget(m: dict) -> int:
    """Manager budget from untrusted JSON: junk ("abc", Infinity, lists) → the
    default 2, never a 500."""
    try:
        return max(0, min(int(m.get("budget", 2) or 0), 4))
    except (TypeError, ValueError, OverflowError):
        return 2


def invalid(loc: list, msg: str) -> HTTPException:
    """A 422 shaped like FastAPI's own.

    A plain `HTTPException(422, "some prose")` answers `{"detail": "some prose"}`,
    while the 422 every FastAPI app declares is `{"detail": [ValidationError]}`.
    Two shapes behind one status is a contract that lies to whoever generated a
    client from it, so the hand-written refusals speak the same dialect as the
    generated ones.
    """
    return HTTPException(422, [{"loc": loc, "msg": msg, "type": "value_error"}])


def materialise(w, body: ManifestBody):
    """Spawn the agents deterministically from their dials, wire the edges by name,
    install rules + manager + protocol. Returns the new Scene. Raises HTTPException on a
    bad spec — callers with no request context catch it like any other error."""
    from substrate.psyche import TRAITS
    from substrate.drives import SPEC as DRIVE_SPEC
    from substrate.world import MODEL_WHITELIST
    from substrate.util import clamp01
    from substrate.threads import clean_protocol
    if not body.agents or len(body.agents) > 12:
        raise invalid(["body", "agents"], "a manifest needs 1-12 agents")
    names = [a.name.strip()[:60] for a in body.agents]
    if len(set(names)) != len(names) or not all(names):
        raise invalid(["body", "agents"],
                      "agent names must be present and unique (edges address by name)")
    s = w.new_room((body.name or "manifest").strip()[:60], "freeplay")
    by_name: dict[str, int] = {}
    n = len(body.agents)
    for i, spec in enumerate(body.agents):
        dials = {}
        for k, v in (spec.dials or {}).items():
            if k in TRAITS:
                dials[k] = round(clamp01(float(v) / 100.0 if float(v) > 1
                                         else float(v)) * 100)
        h = w.spawn_human(names[i], dials=dials or None, figure=spec.figure)
        h.model = spec.model if spec.model in MODEL_WHITELIST else ""
        for k, v in (spec.drives or {}).items():
            if k in DRIVE_SPEC:
                h.drives.level[k] = clamp01(float(v))
        if spec.brief.strip():
            h.narrative = spec.brief.strip()[:280]
        s.seat(h)
        # a ring, top-first — reads instantly on the canvas
        ang = -math.pi / 2 + (i / max(1, n)) * math.tau
        h.pos = (420 + math.cos(ang) * (90 + 24 * n), 300 + math.sin(ang) * (70 + 18 * n))
        by_name[names[i]] = h.id
    for e in body.edges:
        if not (isinstance(e, (list, tuple)) and len(e) >= 2):
            raise invalid(["body", "edges"], f"bad edge {e!r} — use [nameA, nameB, dir?]")
        a, b = by_name.get(str(e[0])), by_name.get(str(e[1]))
        if a is None or b is None or a == b:
            raise invalid(["body", "edges"],
                          f"edge {e!r} names an unknown (or same) agent")
        d = str(e[2]) if len(e) > 2 else "both"
        if d not in ("both", "a2b", "b2a"):
            raise invalid(["body", "edges"], f"edge dir must be both|a2b|b2a, got {d!r}")
        s.connect(a, b, dir=d)
    for t in s.threads:                     # the graph's brief + its manager + protocol
        if body.rules:
            t["rulebook"] = body.rules[:2000]
        m = body.manager or {}
        t["manager"] = {"model": (m.get("model") if m.get("model") in MODEL_WHITELIST
                                  else ""),
                        "budget": clean_budget(m)}
        t["protocol"] = clean_protocol(body.protocol)
    return s
