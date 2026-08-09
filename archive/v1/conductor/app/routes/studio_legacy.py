"""The original Studio: globally-persistent agents, scenes, and the artifact
library — the engine the Lifeworld superseded but did not replace.

Kept whole rather than split, because these three surfaces share one ownership
model (everything is private to its owner, enforced the same way the round
table does it) and one secrecy model: a scene's face-down card is code, sealed
so that even the owner sees a back until they choose to look.
"""

import json

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .. import artifact_lib, auth, db, home, memory, scene, tuning
from .base import _owned, current_user, owned_project, router


class NewHomeAgent(BaseModel):
    name: str = ""
    degree: str = ""
    persona: str = ""
    provider: str = "anthropic"
    model: str = ""
    traits: dict = {}            # personality dials set when creating the agent


class HomeAgentPatch(BaseModel):
    name: str | None = None
    degree: str | None = None
    persona: str | None = None
    provider: str | None = None
    model: str | None = None
    model_locked: bool | None = None
    status: str | None = None
    traits: dict | None = None


def _own_home(request: Request, home_id: int) -> dict:
    """The agent, if it belongs to the caller. A Studio is private to its owner —
    an agent must never be listed for, edited by, or deployed by anyone else, the
    same scoping the round table already enforces."""
    return _owned(request, db.get_home_agent, home_id, "agent in your Studio")


@router.get("/api/home")
def home_list(request: Request) -> dict:
    """The caller's Studio: identity, lifetime counters, current model, mood."""
    u = current_user(request)
    return {"agents": home.describe(u["id"]), "budget": home.budget_state()}


@router.post("/api/home")
def home_create(body: NewHomeAgent, request: Request) -> dict:
    u = current_user(request)
    a = home.create(u["id"], body.name, body.degree, body.persona,
                    body.provider, body.model, traits=body.traits)
    return {"agent": a}


@router.get("/api/home/{home_id}")
def home_get(home_id: int, request: Request) -> dict:
    _own_home(request, home_id)
    return {
        "agent": db.get_home_agent(home_id),
        "memory": db.get_memory(home_id),
        "episodes": db.unconsolidated(home_id)[-20:],
        "evolution": db.list_evolution(home_id),
        "instances": db.home_instances(home_id),
    }


@router.patch("/api/home/{home_id}")
def home_patch(home_id: int, body: HomeAgentPatch, request: Request) -> dict:
    _own_home(request, home_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "model_locked" in fields:
        fields["model_locked"] = 1 if fields["model_locked"] else 0
    if "traits" in fields:
        fields["traits"] = json.dumps(fields["traits"])
    if fields:
        db.update_home_agent(home_id, **fields)
    return {"agent": db.get_home_agent(home_id)}


@router.delete("/api/home/{home_id}")
def home_delete(home_id: int, request: Request) -> dict:
    """Archive by default. A hard delete would orphan the `runs` and `agents` rows
    that reference this identity; archiving keeps the history readable."""
    _own_home(request, home_id)
    db.update_home_agent(home_id, status="archived")
    return {"ok": True, "archived": home_id}


@router.post("/api/home/{home_id}/use")
def home_use(home_id: int, project_id: int, request: Request) -> dict:
    """Deploy this agent into one of the caller's projects."""
    _own_home(request, home_id)
    owned_project(project_id, request)
    inst = home.use(home_id, project_id)
    return {"instance": inst}


@router.get("/api/home/{home_id}/memory")
def home_memory(home_id: int, request: Request) -> dict:
    _own_home(request, home_id)
    return {"memory": db.get_memory(home_id), "blob": memory.current_blob(home_id),
            "episodes": db.unconsolidated(home_id)}


@router.post("/api/home/{home_id}/consolidate")
async def home_consolidate(home_id: int, request: Request) -> dict:
    """Fold this agent's memory now instead of waiting for the tick. Bills once —
    the manual button, like forcing a self-check."""
    u = current_user(request)
    _own_home(request, home_id)
    res = await memory.consolidate(home_id, auth.get_settings(u),
                                   allow_spend=home._may_spend())
    return res


@router.get("/api/home/{home_id}/evolution")
def home_evolution(home_id: int, request: Request) -> dict:
    _own_home(request, home_id)
    return {"evolution": db.list_evolution(home_id)}


@router.get("/api/home/budget")
def home_budget(request: Request) -> dict:
    """Today's background spend against the daily ceiling — so at-rest cost of ~0
    is visible, not just promised."""
    current_user(request)
    return home.budget_state()


# --- scenes: a small world where artifacts (code) shape what agents do ---

class NewScene(BaseModel):
    kind: str = "poker"
    goal: str = ""
    title: str = ""
    seed: int = 0
    rules: str = ""              # the public rules everyone in the scene can read
    equalizer: dict = {}         # trait biases that tune every agent placed in the scene


class ScenePatch(BaseModel):
    title: str | None = None
    goal: str | None = None
    rules: str | None = None
    equalizer: dict | None = None
    status: str | None = None


class SeatBody(BaseModel):
    home_id: int | None = None
    role: str = "player"
    name: str = ""


class TalkBody(BaseModel):
    seat_id: int
    message: str


def _own_scene(request: Request, scene_id: int) -> dict:
    """The scene, if it belongs to the caller. A scene is private to its owner, the
    same scoping the Studio and the round table enforce."""
    return _owned(request, db.get_scene, scene_id, "scene of yours")


@router.get("/api/scene")
def scene_list(request: Request) -> dict:
    u = current_user(request)
    return {"scenes": db.list_scenes(u["id"]),
            "enabled": bool(tuning.get("scene_enabled"))}


@router.post("/api/scene")
def scene_create(body: NewScene, request: Request) -> dict:
    u = current_user(request)
    if not tuning.get("scene_enabled"):
        raise HTTPException(403, "scenes are disabled on this instance")
    s = scene.create(u["id"], body.kind, body.goal, body.title, seed=body.seed)
    if body.rules or body.equalizer:
        db.update_scene(s["id"], rules=body.rules, equalizer=json.dumps(body.equalizer))
        s = db.get_scene(s["id"])
    return {"scene": s}


@router.patch("/api/scene/{scene_id}")
def scene_patch(scene_id: int, body: ScenePatch, request: Request) -> dict:
    """Edit a scene's public rules, its equalizer, or its framing in place."""
    _own_scene(request, scene_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "equalizer" in fields:
        fields["equalizer"] = json.dumps(fields["equalizer"])
    if fields:
        db.update_scene(scene_id, **fields)
    return {"scene": db.get_scene(scene_id)}


@router.get("/api/scene/{scene_id}")
def scene_get(scene_id: int, request: Request, seat: int | None = None) -> dict:
    """The scene as the owner watches it. `?seat=<id>` peeks as one seated agent —
    the ONLY way a hand is ever revealed, and only for a seat the caller owns. The
    default view renders face-down cards as backs, so a secret stays a secret even
    to the owner until they choose to look."""
    _own_scene(request, scene_id)
    if seat is not None:
        s = db.get_scene_agent(seat)
        if not s or s["scene_id"] != scene_id:
            raise HTTPException(404, "no such seat in this scene")
        view = scene.agent_view(scene_id, seat)
    else:
        view = scene.public_view(scene_id)
    return {"view": view, "events": db.list_scene_events(scene_id)}


@router.delete("/api/scene/{scene_id}")
def scene_delete(scene_id: int, request: Request) -> dict:
    _own_scene(request, scene_id)
    db.delete_scene(scene_id)
    return {"ok": True, "deleted": scene_id}


@router.post("/api/scene/{scene_id}/seat")
def scene_seat(scene_id: int, body: SeatBody, request: Request) -> dict:
    _own_scene(request, scene_id)
    if body.home_id and not home.owns(current_user(request)["id"], body.home_id):
        raise HTTPException(404, "no such agent in your Studio")
    seat = scene.seat_agent(scene_id, body.home_id, body.role, body.name)
    return {"seat": seat}


@router.post("/api/scene/{scene_id}/deal")
def scene_deal(scene_id: int, request: Request) -> dict:
    """Deal a hand — free code, no model. Separated from playing so the owner can
    watch the deal before anyone spends a token acting on it."""
    _own_scene(request, scene_id)
    return {"scene": scene.deal(scene_id)}


@router.post("/api/scene/{scene_id}/play")
async def scene_play(scene_id: int, request: Request) -> dict:
    """Let the seated agents play the hand out on their own — bounded, O(players)."""
    u = current_user(request)
    _own_scene(request, scene_id)
    res = await scene.play_hand(scene_id, auth.get_settings(u))
    return {"scene": res, "events": db.list_scene_events(scene_id)}


@router.post("/api/scene/{scene_id}/run")
async def scene_run(scene_id: int, request: Request) -> dict:
    """Ask the manager to run it: brief the room, then play the hand out."""
    u = current_user(request)
    _own_scene(request, scene_id)
    res = await scene.run(scene_id, auth.get_settings(u))
    return {"scene": res, "events": db.list_scene_events(scene_id)}


@router.post("/api/scene/{scene_id}/talk")
async def scene_talk(scene_id: int, body: TalkBody, request: Request) -> dict:
    u = current_user(request)
    _own_scene(request, scene_id)
    reply = await scene.talk(scene_id, body.seat_id, body.message, auth.get_settings(u))
    return {"reply": reply, "events": db.list_scene_events(scene_id)}


@router.post("/api/scene/{scene_id}/flip/{artifact_id}")
def scene_flip(scene_id: int, artifact_id: int, request: Request) -> dict:
    """Flip a card face-up or face-down — a free effect. Reducing visibility is the
    owner's own analogy made real: a card is code, and a face-down card hides its
    value from everyone but its holder."""
    _own_scene(request, scene_id)
    art = db.get_artifact(artifact_id)
    if not art or art["scene_id"] != scene_id:
        raise HTTPException(404, "no such artifact in this scene")
    return {"artifact": scene.flip(artifact_id)}


# --- the artifact library: reusable objects, and the public/secret model ---

class NewArtifactDef(BaseModel):
    name: str = ""
    kind: str = "prop"
    dormant: bool = True
    public: dict = {}
    secret_schema: list = []
    description: str = ""


class ArtifactDefPatch(BaseModel):
    name: str | None = None
    kind: str | None = None
    dormant: bool | None = None
    public: dict | None = None
    secret_schema: list | None = None
    description: str | None = None


class PlaceArtifact(BaseModel):
    def_id: int | None = None
    kind: str = "prop"
    public: dict = {}
    secret: dict = {}
    holder_seat: int | None = None
    dormant: bool = True


def _own_def(request: Request, def_id: int) -> dict:
    return _owned(request, db.get_artifact_def, def_id, "artifact in your library")


@router.get("/api/artifacts")
def artifact_lib_list(request: Request) -> dict:
    """The caller's artifact library — the reusable objects in the Studio's Artifacts
    tab."""
    u = current_user(request)
    return {"artifacts": artifact_lib.list_defs(u["id"])}


@router.post("/api/artifacts")
def artifact_lib_create(body: NewArtifactDef, request: Request) -> dict:
    u = current_user(request)
    d = artifact_lib.create_def(u["id"], body.name, body.kind, dormant=body.dormant,
                                public=body.public, secret_schema=body.secret_schema,
                                description=body.description)
    return {"artifact": d}


@router.patch("/api/artifacts/{def_id}")
def artifact_lib_patch(def_id: int, body: ArtifactDefPatch, request: Request) -> dict:
    _own_def(request, def_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "dormant" in fields:
        fields["dormant"] = 1 if fields["dormant"] else 0
    for j in ("public", "secret_schema"):
        if j in fields:
            fields[j] = json.dumps(fields[j])
    if fields:
        db.update_artifact_def(def_id, **fields)
    return {"artifact": artifact_lib.get_def(def_id)}


@router.delete("/api/artifacts/{def_id}")
def artifact_lib_delete(def_id: int, request: Request) -> dict:
    _own_def(request, def_id)
    db.delete_artifact_def(def_id)
    return {"ok": True, "deleted": def_id}


@router.post("/api/scene/{scene_id}/artifact")
def scene_place_artifact(scene_id: int, body: PlaceArtifact, request: Request) -> dict:
    """Drop an artifact into a scene. If it carries a secret, the value is sealed and
    the key handed to the holder seat — from here it is unreadable to anyone else."""
    _own_scene(request, scene_id)
    if body.def_id is not None and not artifact_lib.owns_def(current_user(request)["id"], body.def_id):
        raise HTTPException(404, "no such artifact in your library")
    art = artifact_lib.place(scene_id, body.def_id, kind=body.kind, public=body.public,
                             secret=body.secret, holder_seat=body.holder_seat,
                             dormant=body.dormant)
    return {"artifact": artifact_lib.public_of(art)}


@router.post("/api/scene/{scene_id}/reveal/{artifact_id}")
def scene_reveal(scene_id: int, artifact_id: int, seat: int, request: Request) -> dict:
    """Reveal a sealed secret to a seat that holds its key — the interaction that
    decrypts. Without the key this returns nothing; the value is never exposed to a
    seat that was not given it."""
    _own_scene(request, scene_id)
    art = db.get_artifact(artifact_id)
    if not art or art["scene_id"] != scene_id:
        raise HTTPException(404, "no such artifact in this scene")
    values = artifact_lib.reveal(artifact_id, seat)
    return {"revealed": values, "ok": values is not None}
