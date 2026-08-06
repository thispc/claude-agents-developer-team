"""The World — the facade the rest of the platform talks to.

It owns the cast (humans and artifacts by id), the flag layer, the id counter, and the
one thing that can spend: `appraise`. By default appraisal is the free deterministic Tier
0; give the world a provider `complete` and settings and it becomes the bounded Tier-2
model call — but the scan loop in `Human` never knows which, because both hide behind the
same `appraise(human, signal, ctx)` awaitable. That is what keeps the whole engine
runnable offline at zero cost and, unchanged, alive with a model.
"""

from __future__ import annotations

from typing import Any, Callable

from .config import Flags
from .entity import Entity
from .human import Human
from .artifact import Artifact
from .types import Packet, Signal

# The only models an agent may possess. A per-agent choice is honoured ONLY if it is on this
# list; anything else falls back to the world default — the model name is never trusted from
# client data straight into a provider call.
MODEL_WHITELIST = frozenset({
    "claude-opus-4-8", "claude-sonnet-5", "claude-fable-5",
    "claude-haiku-4-5-20251001", "claude-haiku-4-5",
})


# How many rulebook lines the host is asked to enforce in one reply. Past this the reply is
# mostly rule echoes, and the round it is supposed to mediate gets squeezed out of the budget.
MAX_RULES = 8


def _persona(h) -> dict:
    """A compact persona so the manager can voice each agent IN CHARACTER — its most defining
    traits (furthest from neutral) and what it wants. Keeps the prompt small but makes the
    author's configuration actually shape the debate (not interchangeable name-only agents)."""
    traits = getattr(getattr(h, "psyche", None), "traits", {}) or {}
    top = sorted(traits.items(), key=lambda kv: -abs(float(kv[1]) - 0.5))[:3]
    try:
        wants = h.drives.dominant_goal()[0]
    except Exception:
        wants = ""
    return {"traits": {k: round(float(v), 2) for k, v in top}, "wants": wants}


class World:
    def __init__(self, id: int = 0, name: str = "world", flags: Flags | None = None,
                 complete: Callable | None = None, settings: dict | None = None,
                 model_name: str = "claude-haiku-4-5", utter_tokens: int = 200):
        self.id = id
        self.name = name
        self.flags = flags or Flags.preset("sandbox")
        self.entities: dict[int, Entity] = {}
        self.scenes: dict[int, Any] = {}      # id -> Scene (holds a back-ref to this world)
        self.lib_specs: dict[str, dict] = {}  # user-saved artifact TYPE specs (the "save to custom" library)
        self._active_flags = self.flags
        self._scene_rules = ""                # compiled rules prompt of the scene currently delivering
        self._ruleset = None                  # the SceneRuleSet currently active (gate + shape)
        self._seq = 0
        # the one spend, injected — the engine never imports providers itself
        self._complete = complete
        self._settings = settings or {}
        self._model_name = model_name
        self.host_error = ""                  # why the last mediated call produced nothing
        self._utter_tokens = utter_tokens
        self.tau = 0                          # world clock: total scans across everyone

    # --- cast ---------------------------------------------------------------

    def next_id(self) -> int:
        self._seq += 1
        return self._seq

    def add(self, entity: Entity) -> Entity:
        self.entities[entity.id] = entity
        self._seq = max(self._seq, entity.id)
        return entity

    def get(self, id: int) -> Entity | None:
        return self.entities.get(id)

    def spawn_human(self, name: str, *, dials: dict | None = None,
                    senses: list | None = None, figure: str = "") -> Human:
        h = Human.newborn(self.next_id(), name, dials=dials, senses=senses, figure=figure)
        return self.add(h)                    # type: ignore[return-value]

    def new_room(self, name: str, type: str = "freeplay"):
        """A room is a scene with a relatable type that sets its domain, flags and look."""
        from .scene import Scene, resolve_room
        spec = resolve_room(type)
        s = Scene(self, self.next_id(), name=name, domain=spec["domain"],
                  flag_overrides=dict(spec["flags"]), type=type, theme=spec["theme"])
        self.scenes[s.id] = s
        return s

    def scene(self, id: int):
        return self.scenes.get(id)

    def humans(self) -> list[Human]:
        return [e for e in self.entities.values() if isinstance(e, Human)]

    def artifacts(self) -> list[Artifact]:
        return [e for e in self.entities.values() if isinstance(e, Artifact)]

    # --- flags --------------------------------------------------------------

    def flags_for(self, entity: Entity) -> Flags:
        return self._active_flags.derive(getattr(entity, "flag_overrides", None))

    def enter_scene_flags(self, scene_overrides: dict | None, rules: str = "",
                          rules_rows: list | None = None) -> None:
        from .scene_rules import SceneRuleSet
        self._active_flags = self.flags.derive(scene_overrides)
        self._ruleset = SceneRuleSet(rules_rows or [], note=rules or "")
        self._scene_rules = self._ruleset.as_prompt()   # the compiled block the model is handed

    def ruleset(self):
        from .scene_rules import SceneRuleSet
        return self._ruleset if self._ruleset is not None else SceneRuleSet([], "")

    # --- the one spend ------------------------------------------------------

    def model_for(self, human: Human) -> str:
        """The model this agent possesses if it named a whitelisted one, else the world default."""
        m = getattr(human, "model", "") or ""
        return m if m in MODEL_WHITELIST else self._model_name

    def is_live(self) -> bool:
        return self._complete is not None

    async def agent_position(self, human: Human, topic: str, transcript: str, rules: str = "") -> str | None:
        """ONE bounded call on the AGENT'S OWN model — its independent position, in its own voice.
        This is the diverse-initialization round the debate literature demands: genuinely separate
        samples from (possibly different-vendor) models, not one model ghost-writing a panel.
        Spend is noted against the agent's session. None → the free deterministic stance line."""
        if self._complete is None:
            return None
        import json
        p = _persona(human)
        sys = (f"You are {human.name}. Your defining traits: {json.dumps(p['traits'])}; what you most "
               f"want: {p['wants'] or 'a good outcome'}. State YOUR OWN position on the TOPIC in 1-2 "
               "sentences, in your own voice, committing to a concrete stance. Plain text only.")
        if rules and rules.strip():
            sys += " Standing rules: " + rules.strip()[:300]
        prompt = (f"TOPIC: {topic}\nSAID IN EARLIER ROUNDS (may be empty):\n{transcript[:800] or '(nothing yet)'}\n"
                  "Your position (1-2 sentences):")
        try:
            raw = await self._complete("anthropic", self.model_for(human), sys, prompt, self._settings, max_tokens=120)
            text = (raw or "").strip()[:200]
            if not text:
                return None
            human.note_spend()
            return text
        except Exception:
            return None

    async def host_plan(self, cfg: dict, ring, rulebook: str, transcript: str,
                        devils_advocate: bool = False, want_unanimous: bool = False) -> dict | None:
        """The thread manager's ONE bounded spend per deliberation. In a SINGLE call it BOTH enforces
        the rulebook AND mediates the round, returning {"enforce": [line per rule], "round": [{"who":
        id, "text": line} per agent]} or None (Live only; free fallback is deterministic). One call
        whatever the ring size or budget — the code then disposes (records + free broadcast), so a whole
        deliberation costs at most this single call. Keeps the O(turns)/one-spend invariant + DO credit."""
        if self._complete is None or not ring:
            return None
        import json
        model = cfg.get("model") if cfg.get("model") in MODEL_WHITELIST else self._model_name
        roster = [{"id": h.id, "name": h.name, **_persona(h)} for h in ring]   # give the host each agent's real substance
        # A rulebook is prose written for the model, and splitting it into lines can yield
        # dozens of "rules" — each of which the host is then asked to echo a line for. That is
        # what silently blew the reply past its token budget: the JSON truncated, the parse
        # failed, and the round fell back to canned lines with no explanation anywhere.
        rules = [ln.strip() for ln in (rulebook or "").splitlines() if ln.strip()][:MAX_RULES]
        topic = (rulebook or "the matter at hand").strip()
        # The unanimity clause is only sent when the thread's protocol actually consumes it — so a
        # classic thread's prompt stays exactly what it always was (prompt-level backward compat).
        keys = '{"enforce": [...], "round": [...], "unanimous": <bool>}' if want_unanimous else '{"enforce": [...], "round": [...]}'
        sys = ("You are the silent HOST mediating a table of agents. Each agent in AGENTS has a persona "
               "(its traits and what it wants) — VOICE EACH ONE IN CHARACTER, let their personas actually "
               "shape and DIVIDE their positions (don't make them agree by default). Do TWO things and "
               f"return ONE JSON object {keys}: "
               "(1) enforce — for each RULE, one short firm line to the table, same order; (2) round — "
               "compose ONE short line (that agent's own voice, 1-2 sentences) for EACH agent to say this "
               "round, in an order you choose that advances the TOPIC. round is an array of {\"who\": "
               "<agent id int>, \"text\": <line>}. "
               + ("Set unanimous=true ONLY if, after this round, every agent effectively holds the same "
                  "position. " if want_unanimous else "")
               + "Return ONLY that JSON object.")
        if devils_advocate:
            sys += (" IMPORTANT: the panel was UNANIMOUS last round. This round, appoint exactly ONE agent "
                    "to argue the strongest opposing case (in character), and let the others answer it — "
                    "surface what the consensus might be missing.")
        prompt = (f"AGENTS: {json.dumps(roster)}\nTOPIC: {json.dumps(topic)}\nRULES: {json.dumps(rules)}\n"
                  f"TRANSCRIPT SO FAR:\n{transcript[:1000] or '(nothing yet)'}\nReturn only the JSON object.")
        try:
            # Budget for what we actually asked for: a line per agent AND a line per rule.
            budget = 110 * max(1, len(ring)) + 60 * len(rules) + 160
            raw = await self._complete("anthropic", model, sys, prompt, self._settings, max_tokens=budget)
            obj = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
            enforce = [str(x)[:140] for x in (obj.get("enforce") or []) if str(x).strip()] or None
            ids = {h.id for h in ring}
            rnd = []
            for x in (obj.get("round") or []):
                try:
                    who = int(x.get("who"))
                except (TypeError, ValueError, AttributeError):
                    continue
                text = str(x.get("text", "")).strip()
                if who in ids and text:
                    rnd.append({"who": who, "text": text[:200]})
            # Trust boundary: the model may return unanimous as the STRING "false"/"no" — bool() of
            # any non-empty string is True, which would spend a spurious devil's-advocate round.
            u = obj.get("unanimous") if want_unanimous else None
            unanimous = u is True or (isinstance(u, str) and u.strip().lower() == "true")
            self.host_error = ""
            return {"enforce": enforce, "round": rnd or None, "unanimous": unanimous}
        except Exception as e:
            # Keep returning None (the caller has a free fallback), but say WHY. A mediated
            # round that quietly degrades to canned lines looks like the feature not working.
            self.host_error = f"{type(e).__name__}: {str(e)[:160]}"
            return None

    async def host_memo(self, cfg: dict, ring, rulebook: str, transcript: str) -> dict | None:
        """The manager's closing synthesis — ONE bounded call that turns a finished deliberation
        into the DECISION MEMO: each agent's final position, the live dissent, a recommendation.
        None → the free deterministic memo (last stances verbatim). This is the 'fine result' a
        run produces; the whole run stays bounded by construction (host plans per round, plus one
        opening call per agent under independent init, plus this single closing call)."""
        if self._complete is None or not ring:
            return None
        import json
        model = cfg.get("model") if cfg.get("model") in MODEL_WHITELIST else self._model_name
        roster = [{"id": h.id, "name": h.name} for h in ring]
        sys = ("You are the HOST closing a finished deliberation. From the TRANSCRIPT, write the decision "
               "memo as ONE JSON object: {\"question\": <what was deliberated, one line>, \"positions\": "
               "[{\"who\": <agent id int>, \"position\": <that agent's FINAL stance, one line>}], "
               "\"dissent\": <the sharpest unresolved disagreement, one line, or \"\">, "
               "\"recommendation\": <your call as mediator, 1-2 lines, concrete>}. Return ONLY the JSON.")
        prompt = (f"AGENTS: {json.dumps(roster)}\nDELIBERATED UNDER: {json.dumps(rulebook or '(no rules set)')}\n"
                  f"TRANSCRIPT:\n{transcript[:2400] or '(nothing was said)'}\nReturn only the JSON object.")
        try:
            raw = await self._complete("anthropic", model, sys, prompt, self._settings, max_tokens=90 * max(1, len(ring)) + 160)
            obj = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
            ids = {h.id for h in ring}
            positions = []
            for x in (obj.get("positions") or []):
                try:
                    who = int(x.get("who"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if who in ids and str(x.get("position", "")).strip():
                    positions.append({"who": who, "position": str(x["position"])[:280]})
            if not positions:
                return None
            return {"question": str(obj.get("question", ""))[:200], "positions": positions,
                    "dissent": str(obj.get("dissent", ""))[:280],
                    "recommendation": str(obj.get("recommendation", ""))[:400]}
        except Exception:
            return None

    async def host_reply(self, cfg: dict, ring, rulebook: str, convo: list, transcript: str = "") -> str | None:
        """The manager answering the USER in the graph's chat — one bounded call per user message.
        It speaks as the mediator: it can explain what the agents ALREADY discussed (the DISCUSSION
        transcript), how it's holding them to the rules, or take the user's direction. None → free
        deterministic fallback."""
        if self._complete is None:
            return None
        import json
        model = cfg.get("model") if cfg.get("model") in MODEL_WHITELIST else self._model_name
        sys = ("You are the HOST/manager mediating a small group of agents that ALREADY EXIST and may "
               "have already discussed the topic (see DISCUSSION). Answer the USER's latest message "
               "helpfully and briefly (2-4 sentences), in your own voice as the mediator — summarise or "
               "explain what the agents said, how you're holding them to the rules, or take the user's "
               "direction. Never offer to create the agents; they are already here. Plain text only.")
        hist = "\n".join(f"{m.get('role')}: {m.get('text')}" for m in convo[-10:])
        prompt = (f"AGENTS: {json.dumps([h.name for h in ring])}\nRULES: {json.dumps(rulebook or '(none set)')}\n"
                  f"DISCUSSION SO FAR:\n{transcript[:1200] or '(they have not spoken yet)'}\n"
                  f"CHAT WITH USER:\n{hist}\nReply as the manager (plain text).")
        try:
            raw = await self._complete("anthropic", model, sys, prompt, self._settings, max_tokens=220)
            return (raw or "").strip()[:600] or None
        except Exception:
            return None

    async def appraise(self, human: Human, signal: Signal, ctx: dict, free: bool = False) -> Packet:
        from . import appraise as appr
        # `free` forces Tier 0 even in Live mode — used for the conversation BROADCAST (a listener
        # absorbing an utterance) so a round never spends beyond the manager's single mediation call.
        if (not free) and self._complete is not None and self.flags_for(human).on("emotions"):
            packet = await appr.model(human, signal, ctx, settings=self._settings,
                                      complete=self._complete, model_name=self.model_for(human),
                                      max_tokens=self._utter_tokens, rules=self._scene_rules)
            if getattr(packet, "spent", 0):
                human.note_spend()               # a real model-use counts against this agent's session
        else:
            packet = appr.deterministic(human, signal, ctx)
        # The code disposes: scene SHAPE rules clamp/bias the deltas AFTER the appraiser, so a rule
        # bites even when the (free) reflex or the (paid) model ignored it.
        return self.ruleset().shape(packet, signal, ctx)

    # --- persistence hook ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "seq": self._seq, "tau": self.tau,
                "flags": self.flags.to_dict(), "lib_specs": self.lib_specs,
                "entities": [e.to_dict() for e in self.entities.values()],
                "scenes": [s.to_state() for s in self.scenes.values()]}

    @classmethod
    def from_dict(cls, d: dict[str, Any], **runtime) -> "World":
        from .scene import Scene
        w = cls(id=d.get("id", 0), name=d.get("name", "world"),
                flags=Flags.from_dict(d.get("flags")), **runtime)
        w._seq = d.get("seq", 0)
        w.tau = d.get("tau", 0)
        w.lib_specs = dict(d.get("lib_specs", {}) or {})
        for row in d.get("entities", []):
            w.entities[row["id"]] = Entity.load(row)
        for sd in d.get("scenes", []):
            w.scenes[sd["id"]] = Scene.from_state(w, sd)
        return w
