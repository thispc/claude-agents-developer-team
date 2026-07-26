"""HTTP surface for the Lifeworld — its own router, so the subsystem stays decoupled from
the projects engine (and out of its route-count gate).

Every mutation loads the whole world, runs the operation, and saves it back. By default a
world loads FREE (the deterministic Tier-0 appraiser), so operating the Studio costs
nothing; pass `?live=1` and the agents genuinely deliberate on your own credentials — the
single bounded spend. The verbs mirror the Scene's: seat, place, interact, greet, say, and
a `round` convenience that deals and lets everyone mingle for the game UI.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import auth
from .lifeworld import store
from .lifeworld.artifact import Deck
from .routes import current_user

router = APIRouter(prefix="/api/lw", tags=["lifeworld"])


# --- bodies ----------------------------------------------------------------

class NewWorld(BaseModel):
    name: str = "a small world"
    preset: str = "sandbox"


class NewHuman(BaseModel):
    name: str = ""
    dials: dict = {}
    senses: list = []


class NewScene(BaseModel):
    name: str = "a scene"
    domain: str = "life"
    flags: dict = {}


class Act(BaseModel):
    human_id: int
    verb: str = "draw"          # draw | flip | greet | say
    target: int | None = None   # an artifact id (interact) or a human id (greet/say)
    text: str = ""
    kind: str = "say"           # for `say`: say | scold | praise | win | lose


# --- helpers ---------------------------------------------------------------

def _own(request: Request, world_id: int):
    u = current_user(request)
    if not store.owns(u["id"], world_id):
        raise HTTPException(404, "no such world of yours")
    return u


def _load(request: Request, world_id: int, live: bool):
    u = _own(request, world_id)
    return store.load(world_id, live=live, settings=auth.get_settings(u) if live else None)


# --- worlds ----------------------------------------------------------------

@router.get("")
def worlds(request: Request) -> dict:
    return {"worlds": store.listing(current_user(request)["id"])}


@router.post("")
def make_world(body: NewWorld, request: Request) -> dict:
    w = store.create(current_user(request)["id"], body.name, body.preset)
    return {"world": {"id": w.id, "name": w.name}}


@router.get("/{world_id}")
def world_view(world_id: int, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    return {"world": {"id": w.id, "name": w.name, "flags": w.flags.to_dict(), "tau": w.tau},
            "people": [h.profile() for h in w.humans()],
            "artifacts": [{"id": a.id, "name": a.name, "kind": a.kind} for a in w.artifacts()],
            "scenes": [s.view() for s in w.scenes.values()]}


@router.delete("/{world_id}")
def drop_world(world_id: int, request: Request) -> dict:
    _own(request, world_id)
    store.delete(world_id)
    return {"ok": True, "deleted": world_id}


# --- cast ------------------------------------------------------------------

@router.post("/{world_id}/human")
def add_human(world_id: int, body: NewHuman, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    h = w.spawn_human(body.name or f"Person {len(w.humans())+1}",
                      dials=body.dials, senses=body.senses or None)
    store.save(w)
    return {"human": h.profile()}


@router.post("/{world_id}/deck")
def add_deck(world_id: int, request: Request, seed: int = 0) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    deck = Deck.fresh(w.next_id(), seed=seed, name="deck of cards")
    w.add(deck)
    store.save(w)
    return {"artifact": {"id": deck.id, "name": deck.name, "kind": "deck",
                         "count": deck.public.get("count")}}


# --- scenes ----------------------------------------------------------------

@router.post("/{world_id}/scene")
def make_scene(world_id: int, body: NewScene, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    s = w.new_scene(body.name, body.domain, body.flags)
    store.save(w)
    return {"scene": s.view()}


@router.post("/{world_id}/scene/{scene_id}/seat")
def seat(world_id: int, scene_id: int, human_id: int, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    s, h = w.scene(scene_id), w.get(human_id)
    if not s or not h:
        raise HTTPException(404, "no such scene or human")
    s.seat(h)
    store.save(w)
    return {"scene": s.view()}


@router.post("/{world_id}/scene/{scene_id}/place")
def place(world_id: int, scene_id: int, artifact_id: int, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    s, a = w.scene(scene_id), w.get(artifact_id)
    if not s or not a:
        raise HTTPException(404, "no such scene or artifact")
    s.place(a)
    store.save(w)
    return {"scene": s.view()}


@router.get("/{world_id}/scene/{scene_id}")
def scene_view(world_id: int, scene_id: int, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    s = w.scene(scene_id)
    if not s:
        raise HTTPException(404, "no such scene")
    return {"scene": s.view()}


# --- the verbs (one scan of scene time each) --------------------------------

@router.post("/{world_id}/scene/{scene_id}/act")
async def act(world_id: int, scene_id: int, body: Act, request: Request, live: int = 0) -> dict:
    w = _load(request, world_id, bool(live))
    s = w.scene(scene_id)
    h = w.get(body.human_id)
    if not s or not h:
        raise HTTPException(404, "no such scene or human")
    if body.verb in ("draw", "flip"):
        await s.interact(h, body.target, body.verb)
    elif body.verb == "greet" and body.target:
        await s.greet(h, w.get(body.target))
    elif body.verb == "say" and body.target:
        await s.say(h, w.get(body.target), body.text, kind=body.kind)
    store.save(w)
    return {"scene": s.view()}


@router.post("/{world_id}/scene/{scene_id}/round")
async def play_round(world_id: int, scene_id: int, request: Request, live: int = 0) -> dict:
    """The game's one-button beat: everyone draws a card, greets their neighbour, then the
    table rests (consolidates). Deterministic and free unless ?live=1."""
    w = _load(request, world_id, bool(live))
    s = w.scene(scene_id)
    if not s:
        raise HTTPException(404, "no such scene")
    deck = next((a for a in (w.get(i) for i in s.props) if a and a.kind == "deck"), None)
    players = s.players()
    for i, h in enumerate(players):
        if deck:
            await s.interact(h, deck.id, "draw")
        if len(players) > 1:
            await s.greet(h, players[(i + 1) % len(players)])
    s.rest()
    store.save(w)
    return {"scene": s.view(), "world_tau": w.tau}


@router.get("/{world_id}/human/{human_id}")
def peek(world_id: int, human_id: int, request: Request) -> dict:
    """A person's full profile, and — the owner's privilege — the values of any sealed
    cards they hold, decrypted with the keys in their own private pool."""
    _own(request, world_id)
    w = store.load(world_id)
    h = w.get(human_id)
    if not h:
        raise HTTPException(404, "no such human")
    hand = [{"id": a.id, "value": a.reveal(h)} for a in w.artifacts()
            if a.kind == "card" and a.holder == human_id]
    return {"human": h.profile(), "narrative": h.narrative, "hand": hand,
            "habits": [{"when": r.match, "confidence": round(r.confidence, 2), "fires": r.fires}
                       for r in h.rules.rules],
            "bonds": {oid: {"trust": round(b.trust, 2), "warmth": round(b.warmth, 2)}
                      for oid, b in h.social.bonds.items()}}
