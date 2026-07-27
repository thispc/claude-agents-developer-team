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
        self.rules: str = ""                  # free-text scene note, folded into the rules prompt
        self.rules_rows: list[dict] = []      # ordered typed rule rows (AWS-ingress style)
        self.threads: list[dict] = []         # the agent graph: each = {id,name,edges,closed,rules_rows,manager}
        self.thread_seq = 0                   # stable thread ids within this scene
        self.log: list[dict] = []
        self.turn = 0
        self.round_no = 0                     # bumped once per beat, stamped on each log row (for the flow view)

    # --- setup --------------------------------------------------------------

    def seat(self, human: Human) -> None:
        if human.id not in self.seats:
            self.seats.append(human.id)

    def place(self, artifact) -> None:
        if artifact.id not in self.props:
            self.props.append(artifact.id)

    def players(self) -> list[Human]:
        return [self.world.get(i) for i in self.seats if isinstance(self.world.get(i), Human)]

    # --- threads: the agent graph -------------------------------------------

    def _thread_of(self, agent_id: int) -> dict | None:
        from .threads import members_of
        for t in self.threads:
            if agent_id in members_of(t):
                return t
        return None

    def connect(self, a: int, b: int, dir: str = "both", closed: bool = False) -> dict:
        """Thread two agents together — extending an existing thread, merging two, or starting one."""
        from .threads import edge_eq, default_manager
        dir = dir if dir in ("both", "a2b", "b2a") else "both"
        ta, tb = self._thread_of(a), self._thread_of(b)
        if ta and tb and ta is not tb:                       # bridging two threads → merge b's into a's
            ta["edges"] += tb["edges"]
            ta["rules_rows"] += tb.get("rules_rows", [])
            self.threads.remove(tb)
            t = ta
        elif ta or tb:
            t = ta or tb
        else:
            self.thread_seq += 1
            t = {"id": self.thread_seq, "name": f"thread {self.thread_seq}", "edges": [],
                 "closed": bool(closed), "rules_rows": [], "manager": default_manager()}
            self.threads.append(t)
        if not any(edge_eq(e, a, b) for e in t["edges"]):
            t["edges"].append([a, b, dir])
        if closed:
            t["closed"] = True
        return t

    def disconnect(self, a: int, b: int) -> None:
        from .threads import edge_eq
        for t in self.threads:
            t["edges"] = [e for e in t["edges"] if not edge_eq(e, a, b)]
        self._normalize_threads()

    def _normalize_threads(self) -> None:
        """After an edit, split any thread that has fallen into several connected components; the
        first keeps the thread's id/rules/manager, the rest become fresh threads. Empty ones drop."""
        from .threads import components, default_manager
        out: list[dict] = []
        for t in self.threads:
            orig = list(t["edges"])                          # capture before mutating t in place
            comps = components(orig)
            if len(comps) <= 1:
                if t["edges"]:
                    out.append(t)
                continue
            for i, comp in enumerate(comps):
                edges = [e for e in orig if e[0] in comp and e[1] in comp]
                if i == 0:
                    t["edges"] = edges
                    out.append(t)
                else:
                    self.thread_seq += 1
                    out.append({"id": self.thread_seq, "name": f"thread {self.thread_seq}", "edges": edges,
                                "closed": False, "rules_rows": [], "manager": default_manager()})
        self.threads = out

    def thread(self, tid: int) -> dict | None:
        return next((t for t in self.threads if t["id"] == tid), None)

    def _deck_here(self):
        from .components import has_component
        for a in self.props_here():
            if a.kind == "deck" or (a.kind == "composite" and has_component(getattr(a, "spec", {}), "multiset")):
                return a
        return None

    def _record(self, kind: str, who: int | None, text: str, packet: Packet | None = None,
                frm: int | None = None) -> None:
        # who = the subject the beat lands on (receiver agent, or the artifact); frm = the sender
        # that caused it (the acting agent), or None. Both, plus the round, let the flow view draw
        # a directed sender→receiver edge per beat.
        self.log.append({"n": len(self.log), "kind": kind, "who": who, "frm": frm,
                         "round": self.round_no, "text": text[:240],
                         "billed": bool(packet and packet.spent), "tier": packet.tier if packet else -1,
                         "ts": time.time()})

    # --- the verbs (each ends in one scan for the receiver) -----------------

    async def deliver(self, signal: Signal, to: Human) -> Packet:
        """The atom of scene time: one human perceives one signal. The domain is stamped
        so the experience credits the right competency."""
        signal.domain = signal.domain or self.domain
        self.world.enter_scene_flags(self.flag_overrides, self.rules, self.rules_rows)
        # Seam 1 — the gate: a denied beat never reaches perceive, so it spends nothing.
        if not self.world.ruleset().gate(signal, {}):
            self.world.tau += 1
            self._record("blocked", to.id, f"{to.name}: (a scene rule blocked this)", frm=signal.from_id)
            return Packet(understood="(blocked by a scene rule)")
        packet = await to.perceive(signal, self.world)
        self.world.tau += 1
        say = (packet.action or {}).get("text") or packet.understood
        self._record("act", to.id, f"{to.name}: {say}", packet, frm=signal.from_id)
        return packet

    async def interact(self, human: Human, artifact_id: int, verb: str) -> Packet:
        """A human acts on an artifact; the artifact reacts (free, deterministic) and the
        human perceives the binding event it hands back."""
        art = self.world.get(artifact_id)
        if art is None:
            return Packet(understood="(no such thing)")
        signal = art.interact(verb, human, self.world)
        self._record("effect", artifact_id, f"{art.name}: {signal.text()}", frm=human.id)
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

    # --- the round, played over a thread by its hidden manager (the Host) ----

    async def run_thread(self, thread: dict) -> None:
        """Play one thread: its Host surveys the graph and enforces the thread's rules (a hidden,
        bounded, root-visible black box), then the members act under those rules."""
        from .threads import members_of
        ring = [h for h in (self.world.get(i) for i in members_of(thread)) if isinstance(h, Human)]
        if not ring:
            return
        saved = self.rules_rows
        self.rules_rows = (saved or []) + (thread.get("rules_rows") or [])   # thread rules bite on its beats
        try:
            await self._host_manage(thread, ring)
            await self._play_ring(ring)
        finally:
            self.rules_rows = saved

    async def _host_manage(self, thread: dict, ring: list[Human]) -> None:
        """The hidden manager. It surveys the thread and issues up to `budget` rule-enforcement lines
        to specific agents. Logged as 'manage' beats — a black box to normal users, visible to root.
        Free and deterministic by default; in Live mode it composes the lines with ONE bounded call."""
        cfg = thread.get("manager", {}) or {}
        budget = max(0, min(int(cfg.get("budget", 2) or 0), 4))            # cost guard: never runaway
        rows = thread.get("rules_rows", []) or []
        self._record("manage", None, f"host surveys thread {thread['id']} — {len(ring)} agents, {len(rows)} rules")
        notes = [r.get("note", "").strip() for r in rows if (r.get("note") or "").strip()]
        if (cfg.get("note") or "").strip():
            notes.insert(0, cfg["note"].strip())
        if not notes or budget == 0:
            return
        composed = None
        if self.world.is_live() and cfg.get("model"):
            composed = await self.world.host_enforce(cfg, ring, notes[:budget])   # the manager's one spend
        for i in range(min(budget, len(notes))):
            target = ring[i % len(ring)]
            line = (composed[i] if composed and i < len(composed) else notes[i])[:140]
            self._record("manage", target.id, f"host → {target.name}: {line}", frm=None)

    async def _play_ring(self, ring: list[Human]) -> None:
        deck = self._deck_here()
        for i, h in enumerate(ring):
            if deck:
                await self.interact(h, deck.id, "draw")
            if len(ring) > 1:
                await self.greet(h, ring[(i + 1) % len(ring)])

    # --- persistence (the world serialises its scenes through these) --------

    def to_state(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "type": self.type, "theme": self.theme,
                "domain": self.domain, "flag_overrides": self.flag_overrides,
                "seats": list(self.seats), "props": list(self.props),
                "rules": self.rules, "rules_rows": self.rules_rows,
                "threads": self.threads, "thread_seq": self.thread_seq,
                "log": self.log[-200:], "turn": self.turn, "round_no": self.round_no}

    @classmethod
    def from_state(cls, world, d: dict[str, Any]) -> "Scene":
        s = cls(world, id=d["id"], name=d.get("name", ""), domain=d.get("domain", "life"),
                flag_overrides=d.get("flag_overrides", {}),
                type=d.get("type", "freeplay"), theme=d.get("theme", "open"))
        s.seats = list(d.get("seats", []))
        s.props = list(d.get("props", []))
        s.rules = d.get("rules", "")
        s.rules_rows = list(d.get("rules_rows", []))
        s.threads = list(d.get("threads", []))
        s.thread_seq = int(d.get("thread_seq", 0))
        s.log = list(d.get("log", []))
        s.turn = int(d.get("turn", 0))
        s.round_no = int(d.get("round_no", 0))
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
                "rules": self.rules, "rules_rows": self.rules_rows,
                # agents placed on the canvas, positioned by pos; the UI lays them out
                "agents": [{"id": h.id, "name": h.name, "figure": h.figure,
                            "pos": list(h.pos), "mood": h.psyche.mood,
                            "wants": h.drives.dominant_goal()[0], "tau": h.tau}
                           for h in self.players()],
                "props": [{"id": a.id, "name": a.name, "kind": a.kind, "figure": a.figure,
                           "pos": list(a.pos), "public": a.public, "sealed": bool(a.secret),
                           "slots": a.slots, "seated": a.seated,
                           "shape": getattr(a, "shape", "circle"), "path": getattr(a, "path", [])}
                          for a in self.props_here()],
                "clusters": self.clusters(),
                "threads": self.threads,
                "log": self.log[-60:]}


def _tone(kind: str) -> str:
    return {"scold": "curt", "praise": "warm", "win": "elated", "lose": "flat"}.get(kind, "neutral")
