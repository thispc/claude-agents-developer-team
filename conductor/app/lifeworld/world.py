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


class World:
    def __init__(self, id: int = 0, name: str = "world", flags: Flags | None = None,
                 complete: Callable | None = None, settings: dict | None = None,
                 model_name: str = "claude-haiku-4-5", utter_tokens: int = 200):
        self.id = id
        self.name = name
        self.flags = flags or Flags.preset("sandbox")
        self.entities: dict[int, Entity] = {}
        self.scenes: dict[int, Any] = {}      # id -> Scene (holds a back-ref to this world)
        self._active_flags = self.flags
        self._scene_rules = ""                # compiled rules prompt of the scene currently delivering
        self._ruleset = None                  # the SceneRuleSet currently active (gate + shape)
        self._seq = 0
        # the one spend, injected — the engine never imports providers itself
        self._complete = complete
        self._settings = settings or {}
        self._model_name = model_name
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

    async def appraise(self, human: Human, signal: Signal, ctx: dict) -> Packet:
        from . import appraise as appr
        if self._complete is not None and self.flags_for(human).on("emotions"):
            packet = await appr.model(human, signal, ctx, settings=self._settings,
                                      complete=self._complete, model_name=self.model_for(human),
                                      max_tokens=self._utter_tokens, rules=self._scene_rules)
        else:
            packet = appr.deterministic(human, signal, ctx)
        # The code disposes: scene SHAPE rules clamp/bias the deltas AFTER the appraiser, so a rule
        # bites even when the (free) reflex or the (paid) model ignored it.
        return self.ruleset().shape(packet, signal, ctx)

    # --- persistence hook ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "seq": self._seq, "tau": self.tau,
                "flags": self.flags.to_dict(),
                "entities": [e.to_dict() for e in self.entities.values()],
                "scenes": [s.to_state() for s in self.scenes.values()]}

    @classmethod
    def from_dict(cls, d: dict[str, Any], **runtime) -> "World":
        from .scene import Scene
        w = cls(id=d.get("id", 0), name=d.get("name", "world"),
                flags=Flags.from_dict(d.get("flags")), **runtime)
        w._seq = d.get("seq", 0)
        w.tau = d.get("tau", 0)
        for row in d.get("entities", []):
            w.entities[row["id"]] = Entity.load(row)
        for sd in d.get("scenes", []):
            w.scenes[sd["id"]] = Scene.from_state(w, sd)
        return w
