"""A Scene — the setting, the cast, and the scan loop that advances time.

A scene is deliberately thin and generic: it knows its domain (which competency the
experience here credits), who is seated, which artifacts are present, its flag overrides,
and a transcript. It exposes the verbs — interact, greet, say — that produce Signals and
route them to a Human's `perceive`. It does NOT hard-code poker or law; a specific game is
just a script that calls these verbs in an order. That is what makes the same scene engine
serve a card table, a courtroom, or a dev standup.

Time passes here: one delivered signal is one scan. Everything the scene does is free
except the appraisal inside `perceive`, which the world gates to at most one bounded call.
"""

from __future__ import annotations

import time
from typing import Any

from .human import Human
from .types import Signal, Packet


# A room is a scene with a relatable TYPE, which sets its domain (what experience it
# credits), its flag profile, and its visual theme. This replaces the abstract
# sandbox/serious/theatre presets with places a person recognises. Only a card room shows
# a card table; an office looks like an office.
ROOM_TYPES: dict[str, dict] = {
    "freeplay": {"domain": "life", "flags": {}, "theme": "open",
                 "blurb": "unrestricted — anything goes"},
    "home":     {"domain": "home.family", "flags": {}, "theme": "home",
                 "blurb": "warm, personal, off the clock"},
    "school":   {"domain": "study.school", "flags": {"deception": False}, "theme": "classroom",
                 "blurb": "learning, structure, young minds"},
    "college":  {"domain": "study.college", "flags": {}, "theme": "campus",
                 "blurb": "ideas, ambition, social churn"},
    "office":   {"domain": "work.tech", "flags": {"switch_drama_off": True}, "theme": "office",
                 "blurb": "a tech team — focus, no soap opera"},
    "casino":   {"domain": "cards.poker", "flags": {}, "theme": "casino",
                 "blurb": "cards, risk, tells and bluffs"},
}


def resolve_room(type: str) -> dict:
    return ROOM_TYPES.get(type, ROOM_TYPES["freeplay"])


class Scene:
    def __init__(self, world, id: int, name: str = "", domain: str = "life",
                 flag_overrides: dict | None = None, type: str = "freeplay", theme: str = "open"):
        self.world = world
        self.id = id
        self.name = name
        self.type = type
        self.theme = theme
        self.domain = domain
        self.flag_overrides = flag_overrides or {}
        self.seats: list[int] = []            # seated human ids, in turn order
        self.props: list[int] = []            # artifact ids present
        self.log: list[dict] = []
        self.turn = 0

    # --- setup --------------------------------------------------------------

    def seat(self, human: Human) -> None:
        if human.id not in self.seats:
            self.seats.append(human.id)

    def place(self, artifact) -> None:
        if artifact.id not in self.props:
            self.props.append(artifact.id)

    def players(self) -> list[Human]:
        return [self.world.get(i) for i in self.seats if isinstance(self.world.get(i), Human)]

    def _record(self, kind: str, who: int | None, text: str, packet: Packet | None = None) -> None:
        self.log.append({"n": len(self.log), "kind": kind, "who": who, "text": text[:240],
                         "billed": bool(packet and packet.spent), "tier": packet.tier if packet else -1,
                         "ts": time.time()})

    # --- the verbs (each ends in one scan for the receiver) -----------------

    async def deliver(self, signal: Signal, to: Human) -> Packet:
        """The atom of scene time: one human perceives one signal. The domain is stamped
        so the experience credits the right competency."""
        signal.domain = signal.domain or self.domain
        self.world.enter_scene_flags(self.flag_overrides)
        packet = await to.perceive(signal, self.world)
        self.world.tau += 1
        say = (packet.action or {}).get("text") or packet.understood
        self._record("act", to.id, f"{to.name}: {say}", packet)
        return packet

    async def interact(self, human: Human, artifact_id: int, verb: str) -> Packet:
        """A human acts on an artifact; the artifact reacts (free, deterministic) and the
        human perceives the binding event it hands back."""
        art = self.world.get(artifact_id)
        if art is None:
            return Packet(understood="(no such thing)")
        signal = art.interact(verb, human, self.world)
        self._record("effect", artifact_id, f"{art.name}: {signal.text()}")
        return await self.deliver(signal, human)

    async def greet(self, a: Human, b: Human) -> Packet:
        """a greets b; b perceives it, so b's model of a warms and b remembers a name."""
        return await self.deliver(
            Signal(kind="greet", from_id=a.id, sense="hearing", intensity=0.4, stakes=0.4,
                   payload={"text": f"{a.name} greets you"}), b)

    async def say(self, a: Human, b: Human, text: str, kind: str = "say",
                  intensity: float = 0.6, stakes: float = 0.6) -> Packet:
        return await self.deliver(
            Signal(kind=kind, from_id=a.id, sense="hearing", intensity=intensity, stakes=stakes,
                   payload={"text": text, "tone": _tone(kind)}), b)

    # --- rest: a free beat where everyone consolidates ----------------------

    def rest(self) -> None:
        """Between rounds, everyone sleeps a little — free consolidation. This is where
        memory folds and self-narratives refresh, and it never spends."""
        for h in self.players():
            h.sleep({self.domain})

    # --- persistence (the world serialises its scenes through these) --------

    def to_state(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "type": self.type, "theme": self.theme,
                "domain": self.domain, "flag_overrides": self.flag_overrides,
                "seats": list(self.seats), "props": list(self.props),
                "log": self.log[-200:], "turn": self.turn}

    @classmethod
    def from_state(cls, world, d: dict[str, Any]) -> "Scene":
        s = cls(world, id=d["id"], name=d.get("name", ""), domain=d.get("domain", "life"),
                flag_overrides=d.get("flag_overrides", {}),
                type=d.get("type", "freeplay"), theme=d.get("theme", "open"))
        s.seats = list(d.get("seats", []))
        s.props = list(d.get("props", []))
        s.log = list(d.get("log", []))
        s.turn = int(d.get("turn", 0))
        return s

    def props_here(self):
        return [a for a in (self.world.get(i) for i in self.props) if a]

    def clusters(self) -> list[dict]:
        """Each collating artifact and the agents ringed around it — a glowing single
        entity. The unit a round plays over."""
        out = []
        for a in self.props_here():
            if getattr(a, "collating", lambda: False)():
                out.append({"artifact": a.id, "seated": a.cluster(), "slots": a.slots,
                            "full": len(a.cluster()) == a.slots})
        return out

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "type": self.type, "theme": self.theme,
                "domain": self.domain, "flags": self.flag_overrides,
                # agents placed on the canvas, positioned by pos; the UI lays them out
                "agents": [{"id": h.id, "name": h.name, "figure": h.figure,
                            "pos": list(h.pos), "mood": h.psyche.mood,
                            "wants": h.drives.dominant_goal()[0], "tau": h.tau}
                           for h in self.players()],
                "props": [{"id": a.id, "name": a.name, "kind": a.kind, "figure": a.figure,
                           "pos": list(a.pos), "public": a.public, "sealed": bool(a.secret),
                           "slots": a.slots, "seated": a.seated}
                          for a in self.props_here()],
                "clusters": self.clusters(),
                "log": self.log[-60:]}


def _tone(kind: str) -> str:
    return {"scold": "curt", "praise": "warm", "win": "elated", "lose": "flat"}.get(kind, "neutral")
