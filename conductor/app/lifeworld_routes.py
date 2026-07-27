"""HTTP surface for the Lifeworld — its own router, decoupled from the projects engine.

A world is a container of registered AGENTS and ARTIFACTS and a set of ROOMS; a room is a
scene with a relatable type (home, office, casino, …) that sets its look and rules; agents
and artifacts are created once and placed into rooms. Creation is by BRIEF, not by dialing
an equalizer: you say who a person is or what a thing is, and a model authors the internals
(free deterministic fallback when there are no credentials). Operating a room is free by
default; `?live=1` lets the agents genuinely deliberate on your own credentials.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import auth, providers, tuning
from .lifeworld import store, authoring
from .lifeworld.artifact import Deck, Prop
from .lifeworld.scene import ROOM_TYPES
from .routes import current_user

router = APIRouter(prefix="/api/lw", tags=["lifeworld"])


# --- bodies ----------------------------------------------------------------

class NewWorld(BaseModel):
    name: str = "a small world"


class NewHuman(BaseModel):
    name: str = ""
    brief: str = ""                         # "a confident young lawyer, sharp but insecure"
    figure: str = ""                        # the chosen figurine/icon
    parents: list[int] = Field(default_factory=list)   # optional: breed from two agents


class NewArtifact(BaseModel):
    name: str = ""
    brief: str = ""                         # "a worn deck of cards" / "a round table for 4"
    figure: str = ""
    slots: int = 0                          # >0 = a collating artifact (a table for N)
    shape: str = "circle"                   # circle | rect | path — the collating outline
    path: list[list[float]] = Field(default_factory=list)   # hand-drawn polygon (local coords)


class Pos(BaseModel):
    id: int
    x: float
    y: float


class SeatSlot(BaseModel):
    slot: int
    human_id: int


class SceneUpdate(BaseModel):
    name: str | None = None                 # rename the scene (its editable title)
    rules: str | None = None                # the standing rules obeyed on every run


class NewRoom(BaseModel):
    name: str = "a room"
    type: str = "freeplay"                  # home | school | college | office | casino | freeplay


class Act(BaseModel):
    human_id: int
    verb: str = "draw"
    target: int | None = None
    text: str = ""
    kind: str = "say"


# --- helpers ---------------------------------------------------------------

def _own(request: Request, world_id: int):
    u = current_user(request)
    if not store.owns(u["id"], world_id):
        raise HTTPException(404, "no such world of yours")
    return u


def _load(request: Request, world_id: int, live: bool = False):
    u = _own(request, world_id)
    return store.load(world_id, live=live, settings=auth.get_settings(u) if live else None), u


def _author_creds(user: dict):
    """The model to author with — the owner's own — or (None, {}) to fall back to the free
    deterministic author. Authoring is a deliberate creation act, so it uses the model even
    when a world is being browsed for free."""
    if auth.has_own_ai_credentials(user):
        return providers.complete, auth.get_settings(user)
    return None, {}


def _profile_with_room(w, h) -> dict:
    room = next((s.id for s in w.scenes.values() if h.id in s.seats), None)
    return {**h.profile(), "room": room}


# --- worlds & overview -----------------------------------------------------

@router.get("")
def worlds(request: Request) -> dict:
    return {"worlds": store.listing(current_user(request)["id"])}


@router.post("")
def make_world(body: NewWorld, request: Request) -> dict:
    w = store.create(current_user(request)["id"], body.name)      # flags default sandbox internally
    return {"world": {"id": w.id, "name": w.name}}


@router.get("/{world_id}")
def overview(world_id: int, request: Request) -> dict:
    """The world at a glance — every agent and artifact, and which room each is in, so the
    operator sees the whole society in one place, grouped by room."""
    _own(request, world_id)
    w = store.load(world_id)
    prop_room = {}
    for s in w.scenes.values():
        for pid in s.props:
            prop_room[pid] = s.id
    return {
        "world": {"id": w.id, "name": w.name, "tau": w.tau},
        "rooms": [{**s.view(), "blurb": ROOM_TYPES.get(s.type, {}).get("blurb", "")}
                  for s in w.scenes.values()],
        "agents": [_profile_with_room(w, h) for h in w.humans()],
        "artifacts": [{"id": a.id, "name": a.name, "kind": a.kind, "sealed": bool(a.secret),
                       "public": a.public, "room": prop_room.get(a.id)} for a in w.artifacts()],
        "room_types": [{"type": k, "theme": v["theme"], "blurb": v["blurb"]}
                       for k, v in ROOM_TYPES.items()],
    }


@router.delete("/{world_id}")
def drop_world(world_id: int, request: Request) -> dict:
    _own(request, world_id)
    store.delete(world_id)
    return {"ok": True, "deleted": world_id}


# --- agents (a registry; created by brief, placed into rooms) ---------------

@router.get("/{world_id}/agents")
def agents(world_id: int, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    return {"agents": [_profile_with_room(w, h) for h in w.humans()]}


@router.post("/{world_id}/human")
async def add_human(world_id: int, body: NewHuman, request: Request) -> dict:
    _, u = _load(request, world_id)
    w = store.load(world_id)
    complete, settings = _author_creds(u)
    spec = await authoring.author_human(
        body.name or f"Person {len(w.humans())+1}", body.brief,
        complete=complete, settings=settings, model=tuning.get("scene_default_model"))
    # breeding: blend two parents' genomes over the authored dials (nature), and carry a
    # slice of their distilled memory (nurture).
    parents = [w.get(p) for p in body.parents if w.get(p)]
    if parents:
        for t in spec["dials"]:
            vals = [p.psyche.traits.get(t, 0.5) * 100 for p in parents]
            spec["dials"][t] = round(sum(vals) / len(vals))
        spec["narrative"] = f"{body.name or 'A child'}, of {' & '.join(p.name for p in parents)}."[:280]
    h = w.spawn_human(body.name or f"Person {len(w.humans())+1}", dials=spec["dials"],
                      senses=spec.get("senses") or None, figure=body.figure)
    h.narrative = spec.get("narrative", h.narrative)[:280]
    for sk in spec.get("skills", []):
        if isinstance(sk, dict) and sk.get("path"):
            h.skills.credit(sk["path"], float(sk.get("xp", 3.0)))
    if parents:                                          # inherited "upbringing"
        for p in parents:
            for dom, fact in list(p.memory.semantic.items())[:1]:
                h.memory.semantic.setdefault(dom, fact[:200])
    store.save(w)
    return {"human": h.profile()}


# --- artifacts (a registry; created by brief, placed into rooms) ------------

@router.get("/{world_id}/artifacts")
def artifacts(world_id: int, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    return {"artifacts": [{"id": a.id, "name": a.name, "kind": a.kind,
                           "sealed": bool(a.secret), "public": a.public} for a in w.artifacts()]}


@router.post("/{world_id}/artifact")
async def add_artifact(world_id: int, body: NewArtifact, request: Request, seed: int = 0) -> dict:
    _, u = _load(request, world_id)
    w = store.load(world_id)
    complete, settings = _author_creds(u)
    spec = await authoring.author_artifact(body.name or "a thing", body.brief,
                                           complete=complete, settings=settings,
                                           model=tuning.get("scene_default_model"))
    if spec["kind"] == "deck" and int(body.slots) <= 0:
        # a deck only when no slots were asked for — a Shape (slots > 0) is always a
        # collating Prop, even if its brief mentions cards.
        a = Deck.fresh(w.next_id(), seed=seed, name=body.name or "deck of cards")
        a.figure = body.figure or "deck"
    else:
        a = Prop(w.next_id(), name=body.name or "a thing", public=spec.get("public", {}) or {},
                 figure=body.figure, slots=max(0, int(body.slots)),
                 shape=(body.shape or "circle"), path=[list(p) for p in (body.path or [])])
    w.add(a)
    store.save(w)
    return {"artifact": {"id": a.id, "name": a.name, "kind": a.kind, "figure": a.figure,
                         "slots": a.slots, "seated": a.seated,
                         "shape": getattr(a, "shape", "circle"), "path": getattr(a, "path", []),
                         "sealed": bool(a.secret), "public": a.public}}


@router.post("/{world_id}/pos")
def move(world_id: int, body: Pos, request: Request) -> dict:
    """Persist a drag — where a token sits on the canvas."""
    _own(request, world_id)
    w = store.load(world_id)
    e = w.get(body.id)
    if not e:
        raise HTTPException(404, "no such token")
    e.pos = (body.x, body.y)
    store.save(w)
    return {"ok": True, "id": body.id, "pos": [body.x, body.y]}


@router.post("/{world_id}/artifact/{artifact_id}/seat")
def seat_into_slot(world_id: int, artifact_id: int, body: SeatSlot, request: Request) -> dict:
    """Snap an agent into a collating artifact's slot — the magnetic pull, made real. The
    seated agents plus the artifact are then one cluster."""
    _own(request, world_id)
    w = store.load(world_id)
    a, h = w.get(artifact_id), w.get(body.human_id)
    if not a or not h or not getattr(a, "collating", lambda: False)():
        raise HTTPException(404, "no such table or agent, or it doesn't collate")
    ok = a.seat(body.slot, body.human_id)
    store.save(w)
    return {"ok": ok, "seated": a.seated, "cluster": a.cluster()}


@router.post("/{world_id}/artifact/{artifact_id}/unseat")
def unseat_from_slot(world_id: int, artifact_id: int, human_id: int, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    a = w.get(artifact_id)
    if not a:
        raise HTTPException(404, "no such table")
    a.unseat(human_id)
    store.save(w)
    return {"ok": True, "seated": a.seated}


# --- rooms ------------------------------------------------------------------

@router.get("/{world_id}/room-types")
def room_types(request: Request) -> dict:
    current_user(request)
    return {"types": [{"type": k, "theme": v["theme"], "blurb": v["blurb"]}
                      for k, v in ROOM_TYPES.items()]}


@router.post("/{world_id}/room")
def make_room(world_id: int, body: NewRoom, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    s = w.new_room(body.name, body.type)
    store.save(w)
    return {"room": s.view()}


@router.post("/{world_id}/room/{room_id}/seat")
def seat(world_id: int, room_id: int, human_id: int, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    s, h = w.scene(room_id), w.get(human_id)
    if not s or not h:
        raise HTTPException(404, "no such room or agent")
    s.seat(h)
    store.save(w)
    return {"room": s.view()}


@router.post("/{world_id}/room/{room_id}/place")
def place(world_id: int, room_id: int, artifact_id: int, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    s, a = w.scene(room_id), w.get(artifact_id)
    if not s or not a:
        raise HTTPException(404, "no such room or artifact")
    s.place(a)
    store.save(w)
    return {"room": s.view()}


@router.post("/{world_id}/room/{room_id}/scene")
def update_scene(world_id: int, room_id: int, body: SceneUpdate, request: Request) -> dict:
    """Rename a scene or set its standing rules — the editable title and the rules box."""
    _own(request, world_id)
    w = store.load(world_id)
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    if body.name is not None:
        s.name = body.name.strip()[:120]
    if body.rules is not None:
        s.rules = body.rules[:2000]
    store.save(w)
    return {"room": s.view()}


@router.post("/{world_id}/touch")
def touch(world_id: int, request: Request) -> dict:
    """An explicit save — re-persist the world so Cmd+S has something honest to confirm."""
    _own(request, world_id)
    w = store.load(world_id)
    store.save(w)
    return {"ok": True}


@router.delete("/{world_id}/room/{room_id}")
def delete_room(world_id: int, room_id: int, request: Request) -> dict:
    """Delete a scene (its canvas). Agents and artifacts are world-level, so they survive
    in the cast and any other scene they're in — only this scene goes."""
    _own(request, world_id)
    w = store.load(world_id)
    w.scenes.pop(room_id, None)
    store.save(w)
    return {"ok": True, "deleted": room_id}


@router.delete("/{world_id}/entity/{entity_id}")
def delete_entity(world_id: int, entity_id: int, request: Request) -> dict:
    """Delete an agent or artifact from the world entirely — dropped from the cast, removed
    from every scene, and unseated from any table it sat at."""
    _own(request, world_id)
    w = store.load(world_id)
    w.entities.pop(entity_id, None)
    for s in w.scenes.values():
        s.seats = [i for i in s.seats if i != entity_id]
        s.props = [i for i in s.props if i != entity_id]
    for a in w.artifacts():                       # pop the deleted agent out of any table's ring
        if entity_id in (getattr(a, "seated", None) or []):
            a.unseat(entity_id)
    store.save(w)
    return {"ok": True, "deleted": entity_id}


@router.get("/{world_id}/room/{room_id}")
def room_view(world_id: int, room_id: int, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    return {"room": s.view()}


# --- the verbs (one scan of room time each) ---------------------------------

@router.post("/{world_id}/room/{room_id}/act")
async def act(world_id: int, room_id: int, body: Act, request: Request, live: int = 0) -> dict:
    w, _u = _load(request, world_id, bool(live))
    s, h = w.scene(room_id), w.get(body.human_id)
    if not s or not h:
        raise HTTPException(404, "no such room or agent")
    if body.verb in ("draw", "flip"):
        await s.interact(h, body.target, body.verb)
    elif body.verb == "greet" and body.target:
        await s.greet(h, w.get(body.target))
    elif body.verb == "say" and body.target:
        await s.say(h, w.get(body.target), body.text, kind=body.kind)
    store.save(w)
    return {"room": s.view()}


@router.post("/{world_id}/room/{room_id}/round")
async def play_round(world_id: int, room_id: int, request: Request, live: int = 0) -> dict:
    """The one-button beat: everyone greets a neighbour and, if a deck is present, draws a
    card; then the room rests (consolidates). Free unless ?live=1."""
    w, _u = _load(request, world_id, bool(live))
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    deck = next((a for a in s.props_here() if a.kind == "deck"), None)

    async def play(ring):
        for i, h in enumerate(ring):
            if deck:
                await s.interact(h, deck.id, "draw")
            if len(ring) > 1:
                await s.greet(h, ring[(i + 1) % len(ring)])

    # Prefer clusters: each collating artifact's seated agents act as a ring. If no cluster
    # has formed yet, fall back to everyone in the room so a round still does something.
    rings = [[w.get(hid) for hid in a.cluster()]
             for a in s.props_here()
             if getattr(a, "collating", lambda: False)() and a.cluster()]
    if rings:
        for ring in rings:
            await play([h for h in ring if h])
    else:
        await play(s.players())
    s.rest()
    store.save(w)
    return {"room": s.view(), "world_tau": w.tau}


@router.get("/{world_id}/human/{human_id}")
def peek(world_id: int, human_id: int, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    h = w.get(human_id)
    if not h:
        raise HTTPException(404, "no such agent")
    hand = [{"id": a.id, "value": a.reveal(h)} for a in w.artifacts()
            if a.kind == "card" and a.holder == human_id]
    return {"human": h.profile(), "narrative": h.narrative, "hand": hand,
            "habits": [{"when": r.match, "confidence": round(r.confidence, 2), "fires": r.fires}
                       for r in h.rules.rules],
            "bonds": {oid: {"trust": round(b.trust, 2), "warmth": round(b.warmth, 2)}
                      for oid, b in h.social.bonds.items()}}
