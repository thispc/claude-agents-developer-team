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

    def enter_scene_flags(self, scene_overrides: dict | None) -> None:
        self._active_flags = self.flags.derive(scene_overrides)

    # --- the one spend ------------------------------------------------------

    async def appraise(self, human: Human, signal: Signal, ctx: dict) -> Packet:
        from . import appraise as appr
        if self._complete is not None and self.flags_for(human).on("emotions"):
            return await appr.model(human, signal, ctx, settings=self._settings,
                                     complete=self._complete, model_name=self._model_name,
                                     max_tokens=self._utter_tokens)
        return appr.deterministic(human, signal, ctx)

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
