"""Artifact components — the vocabulary a Composite is assembled from.

An artifact TYPE is pure JSON: a list of typed components (+ an optional nested `sub` type). A
component is a registered handler owning a fixed verb set; the Composite evaluator dispatches a
verb to the one component that owns it — a name KEYING real code and a verb IN a frozenset, never
`exec`, never `getattr` on an authored string (the rules.py law). The same handful of components
compose a deck of cards, dice, chips, a board — the only variation is which components a type lists.

New behaviour means shipping a reviewed component here, not authoring one from data. That ceiling
IS the no-exec guarantee: non-engineers recombine vetted effects but cannot mint behaviour.
"""

from __future__ import annotations

import random
from typing import Any, Callable

from .types import Signal
from .util import new_key, seal

_COMPONENTS: dict[str, "Component"] = {}       # kind -> singleton handler
_BUILDERS: dict[str, Callable[[], list]] = {}  # name -> value-table factory (the deck contents, dice faces, …)


def component(cls):
    _COMPONENTS[cls.kind] = cls()
    return cls


def builder(name: str):
    def deco(fn):
        _BUILDERS[name] = fn
        return fn
    return deco


SUITS = ["s", "h", "d", "c"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
RANK_VAL = {r: i + 2 for i, r in enumerate(RANKS)}


@builder("standard52")
def _standard52():
    return [{"rank": r, "suit": s} for s in SUITS for r in RANKS]


@builder("dice6")
def _dice6():
    return [{"face": n} for n in range(1, 7)]


class Component:
    kind = "base"
    verbs: frozenset = frozenset()

    def init_state(self, art, cfg: dict) -> None:
        """Set up the artifact's public/state at creation. Default: nothing."""

    def apply(self, art, verb: str, agent, world, args: dict) -> Signal:
        raise NotImplementedError


@component
class Multiset(Component):
    """A shuffled collection that deals its top item as a fresh sub-artifact (a deck → cards)."""
    kind = "multiset"
    verbs = frozenset({"draw", "shuffle"})

    def init_state(self, art, cfg: dict) -> None:
        vals = _BUILDERS[cfg["builder"]]() if cfg.get("builder") in _BUILDERS else list(cfg.get("values", []))
        random.Random(int(cfg.get("seed", 0))).shuffle(vals)
        art.state["order"] = vals                       # PRIVATE — never surfaced by view()
        art.public["count"] = len(vals)
        art.public["cursor"] = 0

    def apply(self, art, verb, agent, world, args):
        if verb == "shuffle":
            random.Random(int(args.get("seed", 0))).shuffle(art.state.get("order", []))
            art.public["cursor"] = 0
            art.public["count"] = len(art.state.get("order", []))
            return Signal(kind="see", from_id=art.id, domain="cards", payload={"text": f"{art.name} is shuffled"})
        # draw
        order = art.state.get("order", [])
        cur = art.public.get("cursor", 0)
        if cur >= len(order):
            return Signal(kind="see", from_id=art.id, domain="cards", payload={"text": f"{art.name} is empty"})
        value = order[cur]
        art.public["cursor"] = cur + 1
        art.public["count"] = len(order) - art.public["cursor"]
        item = art._mint_sub(world, value, agent)       # a fresh sub-artifact, sealed to the drawer if sealable
        return Signal(kind="deal", from_id=art.id, domain="cards", intensity=0.6, stakes=0.6,
                      payload={"text": f"you draw {item.name}", "item": item.id})


@component
class Sealable(Component):
    """Marks a (sub-)artifact whose value is sealed to its holder. The SOLE writer of `secret`;
    the value lands only in the drawer's private scope, unreadable by anyone else."""
    kind = "sealable"
    verbs = frozenset()

    @staticmethod
    def seal_value(item, value, agent, world) -> None:
        key = new_key()
        item.secret = seal(value, key)
        item.holder = agent.id
        agent.social.join_circle(f"art:{item.id}", key)


@component
class Flippable(Component):
    kind = "flippable"
    verbs = frozenset({"flip"})

    def apply(self, art, verb, agent, world, args):
        art.public["face_up"] = not art.public.get("face_up", False)
        return Signal(kind="see", from_id=art.id, domain="cards", payload={"text": f"{art.name} turns over"})


@component
class Rollable(Component):
    """Dice/spinner — a random face, in public state (no secret)."""
    kind = "rollable"
    verbs = frozenset({"roll"})

    def init_state(self, art, cfg: dict) -> None:
        art.public["faces"] = int(cfg.get("faces", 6))
        art.public["value"] = None

    def apply(self, art, verb, agent, world, args):
        faces = int(art.public.get("faces", 6))
        v = random.Random(int(args.get("seed", world.tau))).randint(1, max(1, faces))
        art.public["value"] = v
        return Signal(kind="see", from_id=art.id, intensity=0.5, stakes=0.5,
                      payload={"text": f"{art.name} rolls {v}", "value": v})


@component
class Countable(Component):
    """A pot/tally — a public integer the round can bump."""
    kind = "countable"
    verbs = frozenset({"inc", "dec"})

    def init_state(self, art, cfg: dict) -> None:
        art.public.setdefault("count", int(cfg.get("start", 0)))

    def apply(self, art, verb, agent, world, args):
        step = int(args.get("by", 1))
        art.public["count"] = art.public.get("count", 0) + (step if verb == "inc" else -step)
        return Signal(kind="see", from_id=art.id, payload={"text": f"{art.name} is {art.public['count']}"})


@component
class Slotted(Component):
    """The collating behaviour — seats agents into a ring (unchanged from the base Artifact)."""
    kind = "slotted"
    verbs = frozenset()

    def init_state(self, art, cfg: dict) -> None:
        n = int(cfg.get("slots", 0))
        if n and not art.slots:
            art.slots = n
            art.seated = [None] * n


def has_component(spec: dict, kind: str) -> bool:
    return any(c.get("kind") == kind for c in (spec or {}).get("components", []))


# --- the shipped custom library + the validator (the trust boundary) --------

LIBRARY: dict[str, dict] = {
    "deck": {"type": "deck", "figure": "ic:deck",
             "components": [{"kind": "multiset", "builder": "standard52"}],
             "sub": {"type": "a card", "components": [{"kind": "sealable"}, {"kind": "flippable"}]}},
    "die": {"type": "die", "figure": "ic:die",
            "components": [{"kind": "rollable", "faces": 6}]},
    "pot": {"type": "pot", "figure": "ic:coin",
            "components": [{"kind": "countable", "start": 0}]},
    "table": {"type": "table", "figure": "ic:table",
              "components": [{"kind": "slotted", "slots": 4}]},
}

_PARAM_KEYS = {"builder", "faces", "slots", "start", "of"}   # runtime-safe scalar params (seed is not persisted)
MAX_VALUES = 208                                             # a spec's explicit value table is bounded


def validate_spec(raw: Any, depth: int = 0) -> dict | None:
    """Coerce a client artifact spec to a safe, typed one: only KNOWN components, KNOWN builders,
    scalar params, one level of nesting. Unknown parts are dropped; None if nothing valid remains."""
    if not isinstance(raw, dict):
        return None
    comps = []
    for c in (raw.get("components") or [])[:8]:
        if not isinstance(c, dict) or c.get("kind") not in _COMPONENTS:
            continue
        out: dict[str, Any] = {"kind": c["kind"]}
        for k in _PARAM_KEYS:
            if k in c and isinstance(c[k], (str, int, float, bool)):
                out[k] = c[k]
        if "builder" in out and out["builder"] not in _BUILDERS:
            out.pop("builder")
        if isinstance(c.get("values"), list):
            out["values"] = [v for v in c["values"] if isinstance(v, (dict, str, int, float))][:MAX_VALUES]
        comps.append(out)
    if not comps:
        return None
    spec: dict[str, Any] = {"type": str(raw.get("type", "object"))[:40], "components": comps}
    if isinstance(raw.get("figure"), str):
        spec["figure"] = raw["figure"][:40]
    sub = raw.get("sub")
    if sub and depth == 0:                                   # one level of nesting only (deck ⊃ card)
        vs = validate_spec(sub, depth + 1)
        if vs:
            spec["sub"] = vs
    return spec
