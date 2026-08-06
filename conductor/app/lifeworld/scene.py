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


def _activity_of(world_id: int, human_id: int) -> dict:
    """One row from the agent register, trimmed to what a canvas needs."""
    try:
        from ..agents import get, key_for
        a = get(key_for("lw", world_id, human_id))
        return {"state": a["state"], "busy": a["busy"], "what": a.get("what", ""),
                "for_s": a.get("for_s", 0)}
    except Exception:
        return {"state": "idle", "busy": False, "what": "", "for_s": 0}


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
        """Thread two nodes together — extending an existing graph, merging two, or starting one."""
        from .threads import edge_eq, new_thread
        dir = dir if dir in ("both", "a2b", "b2a") else "both"
        ta, tb = self._thread_of(a), self._thread_of(b)
        if ta and tb and ta is not tb:                       # bridging two graphs → merge b's into a's
            ta["edges"] += tb["edges"]
            ta["rulebook"] = (ta.get("rulebook", "") + "\n" + tb.get("rulebook", "")).strip()
            # A configured deliberation protocol should survive the merge when the survivor has none —
            # and a silent drop of the loser's config would be a confusing canvas gesture, so log it.
            if not ta.get("protocol") and tb.get("protocol"):
                ta["protocol"] = tb["protocol"]
            elif tb.get("protocol") and tb.get("protocol") != ta.get("protocol"):
                self._record("manage", None, f"graphs merged — thread {tb['id']}'s protocol was superseded by thread {ta['id']}'s")
            self.threads.remove(tb)
            t = ta
        elif ta or tb:
            t = ta or tb
        else:
            self.thread_seq += 1
            t = new_thread(self.thread_seq)
            t["closed"] = bool(closed)
            self.threads.append(t)
        # Re-aiming an EXISTING edge used to be a silent no-op: `edge_eq` ignores the
        # direction slot, so "these two are already connected" was treated as "nothing to do"
        # and the one-way toggle did nothing at all on any arrow that already existed. That
        # is a large part of why direction never looked like it worked.
        existing = next((e for e in t["edges"] if edge_eq(e, a, b)), None)
        if existing is None:
            t["edges"].append([a, b, dir])
        elif len(existing) > 2 and (existing[0], existing[1], existing[2]) != (a, b, dir):
            existing[0], existing[1] = a, b        # the caller's a→b is the direction meant
            existing[2] = dir
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
        first keeps the thread's id/rules/manager/protocol, the rest become fresh threads (classic
        defaults). Empty ones drop."""
        from .threads import components, new_thread
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
                    nt = new_thread(self.thread_seq)
                    nt["edges"] = edges
                    out.append(nt)
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

    async def run_thread(self, thread: dict, *, first_round: bool = False,
                         devils_advocate: bool = False) -> dict | None:
        """Play one round over a thread, under its PROTOCOL (policy-as-data; see threads.py).
        Classic: the hidden Host enforces the rules and MEDIATES the round in one bounded call.
        Independent init (first round of a run): each agent states its position with its OWN
        model, blind to this round's other speakers — the diverse-initialization the debate
        literature shows ghost-writing loses. Returns the round's plan (carries `unanimous`)."""
        from .threads import members_of, protocol_of
        proto = protocol_of(thread)
        ring = [h for h in (self.world.get(i) for i in members_of(thread)) if isinstance(h, Human)]
        ring = [h for h in ring if not h.asleep()]     # agents that hit their model's session cap are resting
        if not ring:
            return None
        cfg = thread.get("manager", {}) or {}
        budget = max(0, min(int(cfg.get("budget", 2) or 0), 4))
        rulebook = (thread.get("rulebook") or thread.get("manager", {}).get("note", "") or "").strip()
        # Independent init still honours the mute: budget 0 means NO spend on this graph, period —
        # it falls through to the classic path, which already spends and says nothing when muted.
        # (An unset manager model does NOT mute it: the independent round uses each agent's OWN model.)
        if first_round and proto["init"] == "independent" and budget > 0:
            await self._host_manage(thread, ring, None)          # survey + free enforcement, no plan call
            await self._independent_round(thread, ring, rulebook, proto)
            return None
        # The manager's ONE bounded spend (Live only): a single call that both enforces and mediates.
        # Skip it entirely when the budget is 0 — a muted graph must never pay for a discarded call.
        plan = None
        if self.world.is_live() and cfg.get("model") and budget > 0:
            plan = await self.world.host_plan(cfg, ring, rulebook,
                                              self._thread_transcript(ring, anonymize=proto["anonymize"]),
                                              devils_advocate=devils_advocate,
                                              want_unanimous=proto["on_unanimity"] == "devils_advocate",
                                              can_hear=lambda spk, lst: self._hears(thread, spk, lst))
            if plan is None:                          # the model was expected but unreachable — say so, don't fake it
                why = getattr(self.world, "host_error", "") or "no reply"
                self._record("manage", None, f"host could not mediate thread {thread['id']} ({why}) — free replies this round")
        await self._host_manage(thread, ring, plan)   # survey + enforce (from the one plan, or free rulebook)
        await self._converse(thread, ring, plan)      # mediate one round; the members talk (or free round)
        return plan

    # --- the mediated conversation: the manager is the master, agents the slaves ----
    def _hears(self, thread: dict, speaker: int, listener: int) -> bool:
        """Does the speaker's utterance reach this listener? Straight from the ARROWS — a
        bidirectional edge lets both hear; a one-way edge only lets the tail reach the head."""
        for e in thread.get("edges", []):
            a, b = e[0], e[1]
            d = e[2] if len(e) > 2 else "both"
            if {a, b} != {speaker, listener}:
                continue
            if d == "both":
                return True
            if d == "a2b":
                return a == speaker
            if d == "b2a":
                return b == speaker
        return False

    def _thread_transcript(self, ring: list[Human], anonymize: bool = False,
                           thread: dict | None = None, for_agent: int | None = None) -> str:
        """What has been said in this thread — optionally, only the part ONE agent could hear.

        Without `for_agent` this is the ring-wide record, which is what the manager needs and
        what it has always got. With it, the arrows apply: an agent's own model call must not
        be seeded with lines it never heard, or the direction on the canvas is decoration.
        That was a real leak — under a strict one-way edge the listener's STATE was correctly
        untouched while its PROMPT contained the very utterance it was not supposed to have.
        """
        ids = {h.id for h in ring}
        if thread is not None and for_agent is not None:
            ids = {i for i in ids if i == for_agent or self._hears(thread, i, for_agent)}
        says = [r for r in self.log if r.get("kind") == "say" and r.get("frm") in ids][-8:]
        if not anonymize:
            return "\n".join(r["text"] for r in says)
        # Anonymized: names → "Agent N" (identity sycophancy outweighs self-bias, so the manager
        # weighs ARGUMENTS, not reputations). Beat text is "Name: line" — keep only the line, and
        # ALSO scrub ring names inside the line itself (composed lines often address peers by name).
        label = {h.id: f"Agent {i + 1}" for i, h in enumerate(ring)}
        by_name = {h.name: label[h.id] for h in ring if h.name}
        out = []
        for r in says:
            parts = str(r["text"]).split(":", 1)
            body = parts[1].strip() if len(parts) > 1 else parts[0].strip()
            for name, anon in by_name.items():
                body = body.replace(name, anon)
            out.append(f"{label.get(r.get('frm'), 'Agent ?')}: {body}")
        return "\n".join(out)

    def _free_line(self, h: Human, topic: str, subject: str = "") -> str:
        """A deterministic, persona-flavoured stance — free, so the whole round costs nothing
        when the world is offline (and is what the tests exercise).

        It must DIFFER per agent: keying only on the dominant drive made every agent with the
        same default psyche emit one identical sentence, so a six-person panel read as one
        opinion repeated six times. The stance now comes from the agent's most defining TRAIT
        as well as its drive, and the topic is trimmed to a real subject line rather than the
        first 70 characters of whatever instruction block was handed in."""
        goal, _ = h.drives.dominant_goal()
        traits = getattr(getattr(h, "psyche", None), "traits", {}) or {}
        trait = max(traits.items(), key=lambda kv: abs(kv[1] - 0.5))[0] if traits else ""
        by_trait = {"conscientiousness": "the correct answer matters more than the quick one",
                    "risk_appetite": "take the bold option; the cautious one costs more later",
                    "composure": "nothing here is urgent enough to do badly",
                    "curiosity": "the interesting question is what we have not looked at",
                    "sociability": "whoever has to live with this should have a say",
                    "empathy": "start from what it feels like to use",
                    "willpower": "pick one thing and actually finish it"}
        by_goal = {"social": "and whoever lives with it should have the last word",
                   "esteem": "and it should be something we would show off",
                   "curiosity": "and I would rather learn something than guess",
                   "purpose": "and only if it moves the actual goal",
                   "safety": "and I would rather be slow than sorry",
                   "energy": "and I want the payoff soon, not eventually"}
        # Trait AND drive, because either alone collides across a six-person panel.
        head = by_trait.get(trait) or "let's weigh it carefully"
        tail = by_goal.get(goal) or "and let's be honest about the trade"
        return f"{subject or _subject(topic)} — {head}, {tail}."

    def _free_round(self, ring: list[Human], topic: str, subject: str = "") -> list[dict]:
        return [{"who": h.id, "text": self._free_line(h, topic, subject)} for h in ring]

    async def _hear(self, speaker: Human, listener: Human, text: str) -> None:
        """A listener ABSORBS an utterance — a free Tier-0 state nudge, never the model, so a round
        never spends beyond the manager's single mediation call."""
        sig = Signal(kind="say", from_id=speaker.id, sense="hearing", intensity=0.3, stakes=0.2,
                     payload={"text": text, "tone": "neutral"}, domain=self.domain)
        await listener.perceive(sig, self.world, free=True)
        self.world.tau += 1

    async def _deliver_lines(self, thread: dict, ring: list[Human], lines: list[dict], spend: bool = False) -> None:
        """The dispose loop: each agent SAYS its line (a beat), broadcast free to whoever the
        arrows let hear it. `spend` notes a session-use per speaker (for manager-composed rounds;
        independent rounds already noted their own per-agent spends)."""
        for item in lines:
            speaker = self.world.get(item.get("who"))
            if not isinstance(speaker, Human):
                continue
            text = str(item.get("text", ""))[:200]
            if not text:
                continue
            if spend:
                speaker.note_spend()          # participating in a model-backed round uses this agent's session
            self._record("say", speaker.id, f"{speaker.name}: {text}", frm=speaker.id)
            for listener in ring:
                if listener.id != speaker.id and self._hears(thread, speaker.id, listener.id):
                    await self._hear(speaker, listener, text)

    async def _converse(self, thread: dict, ring: list[Human], plan: dict | None = None) -> None:
        cfg = thread.get("manager", {}) or {}
        budget = max(0, min(int(cfg.get("budget", 2) or 0), 4))
        if budget == 0 or not ring:
            return
        topic = (thread.get("rulebook") or "").strip() or "the matter at hand"
        # The round comes from the manager's single plan (Live); free & deterministic otherwise.
        live_round = bool(plan and plan.get("round"))
        lines = (plan or {}).get("round") or self._free_round(ring, topic, _topic_of(thread))
        await self._deliver_lines(thread, ring, lines, spend=live_round)

    async def _independent_round(self, thread: dict, ring: list[Human], rulebook: str, proto: dict) -> None:
        """The evidence-backed opening round: EVERY agent states its position via ITS OWN model
        (world.agent_position — one bounded call each, spend noted per agent), blind to what the
        others say this round (the transcript snapshot is taken before anyone speaks). Free
        deterministic stance lines offline. Cost: N calls, still bounded by construction."""
        topic = (rulebook or "").strip() or "the matter at hand"
        lines = []
        for h in ring:
            text = None
            if self.world.is_live():
                # Per agent, not one shared snapshot: the arrows decide what each of them has
                # actually heard. Still free — building a string costs nothing, and the call
                # count is unchanged at one per agent.
                snapshot = self._thread_transcript(ring, anonymize=proto.get("anonymize", False),
                                                   thread=thread, for_agent=h.id)
                text = await self.world.agent_position(h, topic, snapshot, self.rules)
            lines.append({"who": h.id, "text": text or self._free_line(h, topic, _topic_of(thread))})
        await self._deliver_lines(thread, ring, lines, spend=False)

    # --- the deliberation run: N rounds of perspectives, then ONE kept result --------------
    async def run_deliberation(self, thread: dict, rounds: int = 2) -> dict:
        """The product loop: repeat the mediated round `rounds` times (different perspectives ×
        repetition), then the manager synthesizes a DECISION MEMO — the durable 'fine result'.
        Cost is bounded BY CONSTRUCTION, free offline: at most max_rounds host plans + 1 memo,
        plus one per-agent opening call when the protocol's init is independent — O(N + rounds),
        never a loop that can run away. The memo is
        VERSIONED on the thread (results[]) so re-runs can be compared."""
        from .threads import members_of, protocol_of
        proto = protocol_of(thread)
        cap = int(proto["max_rounds"])
        rounds = max(1, min(int(rounds or 1), cap))
        # Under independent init the opening round can't judge unanimity (it makes no host_plan
        # call) — so a devils_advocate policy needs at least one mediated round after the opener,
        # or the policy would be silently inert at rounds=1.
        if proto["init"] == "independent" and proto["on_unanimity"] == "devils_advocate":
            rounds = max(rounds, min(2, cap))
        devils = False
        played = 0
        for i in range(rounds):
            self.round_no += 1                       # same convention as play_round: bump, then play
            plan = await self.run_thread(thread, first_round=(i == 0), devils_advocate=devils)
            played += 1
            # Premature-consensus rule: a unanimous round earns a dissenting round, not a redundant one.
            devils = bool(proto["on_unanimity"] == "devils_advocate" and plan and plan.get("unanimous"))
            if devils and (i + 1 < rounds or rounds < cap):
                self._record("manage", None, "host: the panel is unanimous — appointing a devil's advocate next round")
            elif devils:
                self._record("manage", None, "host: the panel is unanimous — round cap reached, the consensus stands")
        # If the FINAL round was the unanimous one, grant ONE extra dissent round — still inside
        # the max_rounds cap, so the bound (≤ max_rounds host plans + N opening calls + 1 memo) holds.
        if devils and rounds < cap:
            self.round_no += 1
            await self.run_thread(thread, devils_advocate=True)
            played += 1
        self.rest()                                  # free consolidation, as after any beat
        ring = [h for h in (self.world.get(i) for i in members_of(thread)) if isinstance(h, Human)]
        rulebook = (thread.get("rulebook") or "").strip()
        cfg = thread.get("manager", {}) or {}
        memo = None
        if self.world.is_live() and cfg.get("model"):
            memo = await self.world.host_memo(cfg, ring, rulebook, self._thread_transcript(ring))
        if not memo:
            memo = self._free_memo(ring, rulebook)
        memo.update(v=len(thread.get("results") or []) + 1, rounds=played, ts=time.time(),
                    names={str(h.id): h.name for h in ring})
        thread.setdefault("results", []).append(memo)
        thread["results"] = thread["results"][-10:]          # keep the last ten versions
        self._record("result", None, f"memo v{memo['v']}: {memo.get('recommendation', '')[:150]}")
        return memo

    def _free_memo(self, ring: list[Human], rulebook: str) -> dict:
        """Offline synthesis: each agent's last utterance IS its final position — honest, free."""
        last: dict[int, str] = {}
        ids = {h.id for h in ring}
        for r in self.log:
            if r.get("kind") == "say" and r.get("frm") in ids:
                last[r["frm"]] = str(r["text"]).split(":", 1)[-1].strip()
        positions = [{"who": h.id, "position": last.get(h.id, "(said nothing)")[:280]} for h in ring]
        return {"question": (rulebook or "the matter at hand")[:200], "positions": positions,
                "dissent": "", "recommendation": "No mediator model was spent; the final positions stand as recorded."}

    # --- direct chat: the user talks to any agent, or to the graph's manager (pinned) ----
    async def chat(self, thread: dict, to: str, text: str) -> dict:
        text = (text or "").strip()[:500]
        chats = thread.setdefault("chats", {})
        key = "manager" if str(to) == "manager" else str(to)
        convo = chats.setdefault(key, [])
        if not text:
            return {"chat": convo[-40:], "to": key}
        convo.append({"role": "user", "text": text, "ts": time.time()})
        from .threads import members_of
        ring = [h for h in (self.world.get(i) for i in members_of(thread)) if isinstance(h, Human)]
        if key == "manager":
            reply = await self._manager_reply(thread, ring, convo)
            convo.append({"role": "manager", "text": reply, "ts": time.time()})
        else:
            agent = self.world.get(int(key)) if key.lstrip("-").isdigit() else None
            # Membership was never checked, so a chat could be addressed to ANY human in the
            # world — one seated in another room, or in no graph at all — and it would think,
            # spend its quota, and file the beat in this scene's log.
            if not isinstance(agent, Human) or agent.id not in members_of(thread):
                convo.pop()
                return {"error": "no such agent in this graph"}
            convo.append({"role": "agent", "text": await self._agent_reply(agent, text), "ts": time.time()})
        chats[key] = convo[-40:]                     # bound the history we keep
        return {"chat": chats[key], "to": key}

    async def _agent_reply(self, agent: Human, text: str) -> str:
        if agent.asleep():
            return f"({agent.name} is resting — out of model quota for now)"
        # kind="ask", not "say". A compiled habit matches on {kind, tone, from_trusted} and
        # nothing else, so an agent that has listened to a few rounds holds a reflex for
        # exactly the shape a user's question used to arrive in — and answered it with the
        # raw debug string "say (i=0.30)" instead of thinking. A question is its own kind of
        # event and must not collide with overheard chatter.
        packet = await self.deliver(
            Signal(kind="ask", from_id=None, sense="hearing", intensity=0.6, stakes=0.6,
                   payload={"text": text, "tone": "neutral"}, domain=self.domain), agent)
        reply = (packet.action or {}).get("text") or packet.understood
        # A reflex has no words worth showing a person; fall back to the persona line rather
        # than surfacing an appraisal's internals.
        if not self.world.is_live() or packet.tier == 1 or not str(reply or "").strip():
            reply = self._free_line(agent, text)
        return str(reply or "…")[:600]

    async def _manager_reply(self, thread: dict, ring: list[Human], convo: list) -> str:
        cfg = thread.get("manager", {}) or {}
        rulebook = (thread.get("rulebook") or "").strip()
        if self.world.is_live() and cfg.get("model"):
            r = await self.world.host_reply(cfg, ring, rulebook, convo, self._thread_transcript(ring))
            if r:
                return r
        names = ", ".join(h.name for h in ring) or "the group"
        if rulebook:
            return f"I'm mediating {names} under: “{rulebook[:140]}”. Tell me how you'd like to steer it, or press step to run a round."
        return f"I'm mediating {names}. Give me a rulebook (set the graph's rules) and I'll run a discussion — or ask me anything about them."

    async def _host_manage(self, thread: dict, ring: list[Human], plan: dict | None = None) -> None:
        """The hidden manager surveys the graph and issues up to `budget` rule-enforcement lines to
        specific agents. Logged as 'manage' beats — a black box to normal users, visible to root.
        The Live composition comes from the single `plan` (run_thread's one spend); free fallback is
        the rulebook lines verbatim. No spend of its own — the whole deliberation is one call."""
        cfg = thread.get("manager", {}) or {}
        budget = max(0, min(int(cfg.get("budget", 2) or 0), 4))            # cost guard: never runaway
        rulebook = (thread.get("rulebook") or thread.get("manager", {}).get("note", "") or "").strip()
        self._record("manage", None, f"host surveys thread {thread['id']} — {len(ring)} agents, {'a rulebook' if rulebook else 'no rulebook'}")
        if not rulebook or budget == 0:
            return
        lines = [ln.strip() for ln in rulebook.splitlines() if ln.strip()][:budget] or [rulebook[:140]]
        composed = (plan or {}).get("enforce")
        for i, ln in enumerate(lines):
            target = ring[i % len(ring)]
            line = (composed[i] if composed and i < len(composed) else ln)[:140]
            self._record("manage", target.id, f"host → {target.name}: {line}", frm=None)

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
                            "wants": h.drives.dominant_goal()[0], "tau": h.tau,
                            "usage": h.usage(),                       # model session usage → the meter/sleep
                            # What it is doing RIGHT NOW, from the central register — so the
                            # canvas can glow the working ones without asking per agent.
                            "activity": _activity_of(self.world.id, h.id)}
                           for h in self.players()],
                "props": [{"id": a.id, "name": a.name, "kind": a.kind, "figure": a.figure,
                           "pos": list(a.pos), "public": a.public, "sealed": bool(a.secret),
                           "slots": a.slots, "seated": a.seated,
                           "shape": getattr(a, "shape", "circle"), "path": getattr(a, "path", [])}
                          for a in self.props_here()],
                "clusters": self.clusters(),
                "threads": self.threads,
                "log": self.log[-60:]}


def _topic_of(thread: dict) -> str:
    """What a thread is ABOUT, in a person's words — set by whoever built the graph. It is
    deliberately NOT the rulebook: a rulebook is an instruction block addressed to the model
    ("You are the platform's own IT crew…"), and using it as the subject of speech put that
    sentence in every bubble. Empty is fine; the rulebook's first line then stands in."""
    return str((thread or {}).get("topic") or "").strip()[:90]


def _subject(topic: str) -> str:
    """The first real sentence of a topic — a rulebook is an instruction block, and slicing its
    first 70 characters produced beats like 'On You are the platform\'s own IT crew planning'."""
    t = " ".join(str(topic or "").split())
    for stop in (". ", "? ", "\n"):
        if stop in t:
            t = t.split(stop)[0]
            break
    return (t[:90] + "…") if len(t) > 90 else (t or "the matter at hand")


def _tone(kind: str) -> str:
    return {"scold": "curt", "praise": "warm", "win": "elated", "lose": "flat"}.get(kind, "neutral")
