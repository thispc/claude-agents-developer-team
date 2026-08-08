"""The lifeworld service — a small society of living agents, in its own process.

The P4 extraction, and the hardest one: the conductor's whole lifeworld package
(now `substrate/`, unchanged below `ports.py`), the 35 handler bodies of
`lifeworld_routes.py`, and the `lw_worlds` blob table all moved here. One process
behind one port, owning `data/lifeworld.db` alone. Conductor-side what is left is
a doorway (`lifeworld_routes.py` proxies `/api/lw/*`) and a client
(`lifeworld_client.py`); the package is gone.

WHAT A WORLD IS. A container of registered AGENTS and ARTIFACTS and a set of
ROOMS; a room is a scene with a relatable type (home, office, casino, …) that
sets its look and rules; agents and artifacts are created once and placed into
rooms. Creation is by BRIEF, not by dialing an equalizer: you say who a person is
or what a thing is and a model authors the internals (free deterministic fallback
when there are no credentials). Operating a room is free by default; `?live=1`
lets the agents genuinely deliberate.

THE DEPENDENCY INVERSION — the reason this phase went last but one. Everything
the substrate used to reach UP for is now a client (`substrate/ports.py`):
knowledge is the P1 service, called directly; tuning is the conductor's
allowlisted knob door; the activity register is the conductor's
`/internal/agents` doors; and inference is the conductor's `POST
/internal/complete`. **No credential ever enters this process.** A live world
carries a `settings_ref` — an opaque string the conductor minted — exactly where
the settings dict used to sit, and only the conductor can turn one into a key.

TWO KINDS OF ROUTE.

  `/worlds/…`     the Studio surface, one-for-one with the conductor's
                  `/api/lw/…` paths, which the dashboard hardcodes in fifty-odd
                  places and which therefore MUST NOT MOVE. The conductor keeps
                  them as an authenticated thin proxy: it resolves the session
                  cookie, stamps who the caller is, and forwards with this
                  service's token. The browser never sees a service token and
                  the dashboard never changes origin.

  `/worlds/{id}/crew-…`  the self-repair crew's own verbs — seating, the sprint
                  deliberation, a consult, a green-diff review, a decision and
                  its outcome. These are WHOLE BEHAVIOURS, not accessors, and
                  that is deliberate: each one is a read-modify-write on a world
                  blob, so it has to happen on this side of the wire, under this
                  service's per-world lock. `repair.py` used to import
                  `materialise_manifest` from a route module and perform deep
                  surgery on live `Human` objects; it is now a thin client that
                  keeps its kv record and its adoption semantics and nothing else.

WHO THE CALLER IS. Every request beyond `/health` and `/openapi.json` carries
`X-Service-Token`. WHO the end user is arrives in the conductor's stamp headers
(`X-Lw-Owner`, `X-Lw-Root`, `X-Lw-Settings`, `X-Lw-Source`, `X-Lw-Author`) —
the conductor is the only authenticator, and OWNERSHIP is checked here, against
the `owner_id` column, because that column now lives here. A check made against
a copy of a table you no longer own is not a check.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))       # so `uvicorn app:app` works from any cwd
import helpers                      # vendored per service — never imported across services
import manifest                     # the team-as-a-spec, shared with the crew endpoints
import store                        # this service's own persistence (data/lifeworld.db)
from caller import AUTH, Principal, principal
from manifest import ManifestBody
from substrate import ports
from substrate.artifact import Deck, Prop
from substrate.scene import ROOM_TYPES

SERVICE = os.environ.get("SERVICE_NAME", HERE.name)
SPEC = HERE / "openapi.json"
LEGACY_DB_PATH = Path(os.environ.get("LEGACY_DB_PATH", "devteam.db"))


# --- bodies (moved verbatim from conductor/app/lifeworld_routes.py) ----------

class NewWorld(BaseModel):
    name: str = "a small world"


class NewHuman(BaseModel):
    name: str = ""
    brief: str = ""                         # "a confident young lawyer, sharp but insecure"
    figure: str = ""                        # the chosen figurine/icon
    parents: list[int] = Field(default_factory=list)   # optional: breed from two agents
    model: str = ""                         # the LLM this agent possesses ("" = inherit world default)
    dials: dict = Field(default_factory=dict)   # explicit trait dials (0-100) from the base-DNA questions
    drives: dict = Field(default_factory=dict)  # optional drive setpoints (0-1)


class NewArtifact(BaseModel):
    name: str = ""
    brief: str = ""                         # "a worn deck of cards" / "a round table for 4"
    figure: str = ""
    slots: int = 0                          # >0 = a collating artifact (a table for N)
    shape: str = "circle"                   # circle | rect | path — the collating outline
    path: list[list[float]] = Field(default_factory=list)   # hand-drawn polygon (local coords)
    type: str = ""                          # a library key (a Custom option) → instantiate that spec
    spec: dict | None = None                # a generic-builder spec → validate + instantiate a Composite
    save_as: str = ""                       # with `spec`: also save the spec to this world's custom library


class LibSave(BaseModel):
    name: str = ""
    spec: dict = Field(default_factory=dict)


class Pos(BaseModel):
    id: int
    x: float
    y: float


class SeatSlot(BaseModel):
    slot: int
    human_id: int


class SceneUpdate(BaseModel):
    name: str | None = None                 # rename the scene (its editable title)
    rules: str | None = None                # the free-text note, folded into the rules prompt
    rules_rows: list | None = None          # ordered typed rule rows (AWS-ingress style)


class ThreadEdge(BaseModel):
    a: int
    b: int
    dir: str = "both"                       # both | a2b | b2a
    closed: bool = False


class ThreadUpdate(BaseModel):
    name: str | None = None
    rulebook: str | None = None             # the graph's single free-text rulebook
    manager: dict | None = None             # {model, budget} — the hidden manager
    protocol: dict | None = None            # deliberation policy-as-data


class RefineBody(BaseModel):
    text: str = ""


class ChatBody(BaseModel):
    to: str = "manager"                     # "manager" (pinned) or an agent id in the graph
    text: str = ""


class NewRoom(BaseModel):
    name: str = "a room"
    type: str = "freeplay"                  # home | school | college | office | casino | freeplay


class Act(BaseModel):
    human_id: int
    verb: str = "draw"
    target: int | None = None
    text: str = ""
    kind: str = "say"


# --- helpers ----------------------------------------------------------------

def _own(p: Principal, world_id: int) -> None:
    """404 unless this world belongs to the stamped caller.

    Ownership is enforced HERE because the `owner_id` column is here. The
    conductor authenticates; the service authorises against the row. Same
    message the conductor's `_own` gave, so a client sees no difference.
    """
    if not store.owns(p.owner_id, world_id):
        raise HTTPException(404, "no such world of yours")


def _world(p: Principal, world_id: int, live: bool = False):
    _own(p, world_id)
    w = store.load(world_id, live=bool(live) and p.live_ok,
                   settings_ref=p.settings_ref, source=p.source)
    if w is None:
        raise HTTPException(404, "no such world of yours")
    return w


def _author_creds(p: Principal):
    """The model to author with — the owner's own, through the door — or (None, "") to
    fall back to the free deterministic author. Authoring is a deliberate creation act, so
    it uses the model even when a world is being browsed for free."""
    if p.can_author and p.settings_ref:
        return ports.providers(p.source).complete, p.settings_ref
    return None, ""


def _profile_with_room(w, h) -> dict:
    room = next((s.id for s in w.scenes.values() if h.id in s.seats), None)
    return {**h.profile(), "room": room}


def _artifact_view(a) -> dict:
    return {"id": a.id, "name": a.name, "kind": a.kind, "figure": a.figure,
            "slots": a.slots, "seated": a.seated,
            "shape": getattr(a, "shape", "circle"), "path": getattr(a, "path", []),
            "sealed": bool(a.secret), "public": a.public, "spec": getattr(a, "spec", {})}


def _default_model() -> str:
    return str(store._knob("scene_default_model", "claude-haiku-4-5"))


store.init_store()
store.backfill_from_legacy(LEGACY_DB_PATH)

# openapi_url=None: FastAPI must not serve a live-generated spec at the contract's
# address — the committed file is the contract, and drift between the two is a
# thing the contract tests exist to catch.
app = FastAPI(title=SERVICE, openapi_url=None)

# Missing and forbidden are the SAME answer everywhere a world id appears, so a
# guessed id learns nothing — not even that the row exists. Declared once, on the
# router every world-scoped route hangs off, because a contract that omits the
# answer a caller is most likely to get is not a contract.
COMMON = {404: {"description": "no such world of yours — or no such room, thread, "
                               "agent or artifact inside it"},
          400: {"description": "a malformed request body, or one this world cannot "
                               "accept (an artifact type it does not know, a spec "
                               "with no valid components, an edge between agents "
                               "that are not both seated here)"}}
wr = APIRouter(prefix="/worlds", dependencies=AUTH, responses=COMMON)


@app.get("/health")
def health() -> dict:
    """Readiness, not liveness: ok only when the service could actually answer — its own
    database opens and the worlds table reads.

    `backfilled` rides alongside rather than inside `checks`, because it is not a
    readiness condition: a box with no legacy database to copy from is perfectly
    healthy. It is the conductor's signal that its own `lw_worlds` table is finally
    safe to drop — see store.settled(), and note that dropping it early would lose
    every world on the box.
    """
    def _table_ok() -> bool:
        try:
            helpers.db().execute("SELECT COUNT(*) FROM lw_worlds").fetchone()
            return True
        except Exception:
            return False
    checks = {"db": helpers.db_ok(), "table": _table_ok()}
    return {"ok": all(checks.values()), "service": SERVICE,
            "db": str(helpers.DB_PATH), "checks": checks,
            "backfilled": store.settled()}


@app.get("/openapi.json")
def openapi_spec() -> JSONResponse:
    """The committed contract. Regenerate after changing routes:
    `python app.py --spec > openapi.json` — and let oasdiff judge the diff."""
    return JSONResponse(json.loads(SPEC.read_text()))


# --- worlds & overview -------------------------------------------------------

@app.get("/worlds", dependencies=AUTH)
def worlds(p: Principal = Depends(principal)) -> dict:
    """Every world this owner has. The conductor filters the crew's own world out of the
    Studio list — that is a repair concept and it keeps its kv record, so the filter stays
    where the record is."""
    return {"worlds": store.listing(p.owner_id)}


@app.post("/worlds", dependencies=AUTH,
          responses={400: {"description": "a body that is not JSON at all — the "
                                          "framework's own answer, before any model "
                                          "sees it"}})
def make_world(body: NewWorld, p: Principal = Depends(principal)) -> dict:
    """The one POST that hangs off `app` rather than `wr`, because it has no world
    id to be scoped by — so it does not inherit COMMON and has to say the 400
    itself. (Found by the contract fuzzer during P5, on a spec written in P4: a
    documented answer set that omits the framework's own is a contract with a hole
    in it, whichever phase notices.)"""
    w = store.create(p.owner_id, body.name)       # flags default sandbox internally
    return {"world": {"id": w.id, "name": w.name}}


@wr.get("/{world_id}")
def overview(world_id: int, p: Principal = Depends(principal)) -> dict:
    """The world at a glance — every agent and artifact, and which room each is in, so the
    operator sees the whole society in one place, grouped by room."""
    w = _world(p, world_id)
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


@wr.delete("/{world_id}")
def drop_world(world_id: int, p: Principal = Depends(principal)) -> dict:
    _own(p, world_id)
    store.delete(world_id)
    return {"ok": True, "deleted": world_id}


# --- agents (a registry; created by brief, placed into rooms) ----------------

@wr.get("/{world_id}/agents")
def agents(world_id: int, p: Principal = Depends(principal)) -> dict:
    w = _world(p, world_id)
    return {"agents": [_profile_with_room(w, h) for h in w.humans()]}


@wr.post("/{world_id}/human")
async def add_human(world_id: int, body: NewHuman,
                    p: Principal = Depends(principal)) -> dict:
    from substrate import authoring
    from substrate.psyche import TRAITS
    from substrate.drives import SPEC as DRIVE_SPEC
    from substrate.world import MODEL_WHITELIST
    from substrate.util import clamp01
    _own(p, world_id)
    complete, settings = _author_creds(p)
    # If the owner answered every base-DNA trait, we don't need a model to author the
    # genome — skip that call entirely (spends less); authoring still fills
    # senses/skills deterministically.
    dna = {k: float(v) for k, v in (body.dials or {}).items() if k in TRAITS}
    async with store.lock_for(world_id):
        w = store.load(world_id)
        if w is None:
            raise HTTPException(404, "no such world of yours")
        auth_complete = None if len(dna) >= len(TRAITS) else complete
        spec = await authoring.author_human(
            body.name or f"Person {len(w.humans())+1}", body.brief,
            complete=auth_complete, settings=settings, model=_default_model())
        # breeding: blend two parents' genomes over the authored dials (nature), and
        # carry a slice of their distilled memory (nurture).
        parents = [w.get(pid) for pid in body.parents if w.get(pid)]
        if parents:
            for t in spec["dials"]:
                vals = [par.psyche.traits.get(t, 0.5) * 100 for par in parents]
                spec["dials"][t] = round(sum(vals) / len(vals))
            spec["narrative"] = f"{body.name or 'A child'}, of {' & '.join(x.name for x in parents)}."[:280]
        # The owner's explicit base-DNA answers win over whatever was authored/bred.
        for k, v in dna.items():
            spec["dials"][k] = round(clamp01(v / 100.0 if v > 1 else v) * 100)
        h = w.spawn_human(body.name or f"Person {len(w.humans())+1}", dials=spec["dials"],
                          senses=spec.get("senses") or None, figure=body.figure)
        h.model = body.model if body.model in MODEL_WHITELIST else ""
        for k, v in (body.drives or {}).items():
            if k in DRIVE_SPEC:
                h.drives.level[k] = clamp01(float(v))
        h.narrative = spec.get("narrative", h.narrative)[:280]
        for sk in spec.get("skills", []):
            if isinstance(sk, dict) and sk.get("path"):
                h.skills.credit(sk["path"], float(sk.get("xp", 3.0)))
        if parents:                                          # inherited "upbringing"
            for par in parents:
                for dom, fact in list(par.memory.semantic.items())[:1]:
                    h.memory.semantic.setdefault(dom, fact[:200])
        store.save(w)
    return {"human": h.profile()}


# --- artifacts (a registry; created by brief, placed into rooms) -------------

@wr.get("/{world_id}/artifacts")
def artifacts(world_id: int, p: Principal = Depends(principal)) -> dict:
    w = _world(p, world_id)
    return {"artifacts": [{"id": a.id, "name": a.name, "kind": a.kind,
                           "sealed": bool(a.secret), "public": a.public}
                          for a in w.artifacts()]}


@wr.get("/{world_id}/artifact-lib")
def artifact_lib(world_id: int, p: Principal = Depends(principal)) -> dict:
    """The palette: shipped custom types, this world's saved types, and the vocabulary a
    generic build may use (component kinds + value-table builders)."""
    w = _world(p, world_id)
    from substrate.components import LIBRARY, _COMPONENTS, _BUILDERS
    return {"shipped": LIBRARY, "custom": w.lib_specs,
            "components": sorted(_COMPONENTS), "builders": sorted(_BUILDERS)}


@wr.post("/{world_id}/artifact-lib")
async def artifact_lib_save(world_id: int, body: LibSave,
                            p: Principal = Depends(principal)) -> dict:
    """Save a generic-built spec to this world's custom library for reuse."""
    _own(p, world_id)
    from substrate.components import validate_spec
    vs = validate_spec(body.spec)
    if not vs:
        raise HTTPException(400, "the spec has no valid components")
    async with store.lock_for(world_id):
        w = store.load(world_id)
        w.lib_specs[(body.name.strip()[:40] or vs["type"])] = vs
        store.save(w)
        return {"custom": w.lib_specs}


@wr.post("/{world_id}/artifact")
async def add_artifact(world_id: int, body: NewArtifact, seed: int = 0,
                       p: Principal = Depends(principal)) -> dict:
    from substrate import authoring
    _own(p, world_id)
    complete, settings = _author_creds(p)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        # Composite path: a Custom library type, or a Generic-built spec. One class, any
        # object, no exec.
        if body.type or body.spec:
            from substrate.components import LIBRARY, validate_spec
            from substrate.artifact import Composite
            resolved = validate_spec(body.spec) if body.spec else \
                (LIBRARY.get(body.type) or w.lib_specs.get(body.type))
            if not resolved:
                raise HTTPException(400, "unknown artifact type / empty spec")
            a = Composite.from_spec(w.next_id(), resolved,
                                    name=body.name or resolved.get("type", "object"),
                                    figure=body.figure or resolved.get("figure", ""), seed=seed)
            if int(body.slots) > 0 and not a.slots:      # drop a composite onto a table footprint
                a.slots = int(body.slots)
                a.seated = [None] * a.slots
            a.shape = body.shape or "circle"
            a.path = [list(x) for x in (body.path or [])]
            if body.save_as and body.spec:
                w.lib_specs[body.save_as[:40]] = resolved    # save-to-custom in one shot
            w.add(a)
            store.save(w)
            return {"artifact": _artifact_view(a)}
        spec = await authoring.author_artifact(body.name or "a thing", body.brief,
                                               complete=complete, settings=settings,
                                               model=_default_model())
        if spec["kind"] == "deck" and int(body.slots) <= 0:
            # a deck only when no slots were asked for — a Shape (slots > 0) is always a
            # collating Prop, even if its brief mentions cards.
            a = Deck.fresh(w.next_id(), seed=seed, name=body.name or "deck of cards")
            a.figure = body.figure or "deck"
        else:
            a = Prop(w.next_id(), name=body.name or "a thing",
                     public=spec.get("public", {}) or {}, figure=body.figure,
                     slots=max(0, int(body.slots)), shape=(body.shape or "circle"),
                     path=[list(x) for x in (body.path or [])])
        w.add(a)
        store.save(w)
    return {"artifact": _artifact_view(a)}


@wr.post("/{world_id}/pos")
async def move(world_id: int, body: Pos, p: Principal = Depends(principal)) -> dict:
    """Persist a drag — where a token sits on the canvas."""
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        e = w.get(body.id)
        if not e:
            raise HTTPException(404, "no such token")
        e.pos = (body.x, body.y)
        store.save(w)
    return {"ok": True, "id": body.id, "pos": [body.x, body.y]}


@wr.post("/{world_id}/artifact/{artifact_id}/seat")
async def seat_into_slot(world_id: int, artifact_id: int, body: SeatSlot,
                         p: Principal = Depends(principal)) -> dict:
    """Snap an agent into a collating artifact's slot — the magnetic pull, made real."""
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        a, h = w.get(artifact_id), w.get(body.human_id)
        if not a or not h or not getattr(a, "collating", lambda: False)():
            raise HTTPException(404, "no such table or agent, or it doesn't collate")
        ok = a.seat(body.slot, body.human_id)
        store.save(w)
        return {"ok": ok, "seated": a.seated, "cluster": a.cluster()}


@wr.post("/{world_id}/artifact/{artifact_id}/unseat")
async def unseat_from_slot(world_id: int, artifact_id: int, human_id: int,
                           p: Principal = Depends(principal)) -> dict:
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        a = w.get(artifact_id)
        if not a:
            raise HTTPException(404, "no such table")
        a.unseat(human_id)
        store.save(w)
        return {"ok": True, "seated": a.seated}


# --- rooms -------------------------------------------------------------------

@wr.get("/{world_id}/room-types")
def room_types(world_id: int, p: Principal = Depends(principal)) -> dict:
    return {"types": [{"type": k, "theme": v["theme"], "blurb": v["blurb"]}
                      for k, v in ROOM_TYPES.items()]}


@wr.post("/{world_id}/room")
async def make_room(world_id: int, body: NewRoom,
                    p: Principal = Depends(principal)) -> dict:
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        s = w.new_room(body.name, body.type)
        store.save(w)
        return {"room": s.view()}


@wr.post("/{world_id}/room/{room_id}/seat")
async def seat(world_id: int, room_id: int, human_id: int,
               p: Principal = Depends(principal)) -> dict:
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        s, h = w.scene(room_id), w.get(human_id)
        if not s or not h:
            raise HTTPException(404, "no such room or agent")
        s.seat(h)
        store.save(w)
        return {"room": s.view()}


@wr.post("/{world_id}/room/{room_id}/place")
async def place(world_id: int, room_id: int, artifact_id: int,
                p: Principal = Depends(principal)) -> dict:
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        s, a = w.scene(room_id), w.get(artifact_id)
        if not s or not a:
            raise HTTPException(404, "no such room or artifact")
        s.place(a)
        store.save(w)
        return {"room": s.view()}


@wr.post("/{world_id}/room/{room_id}/scene")
async def update_scene(world_id: int, room_id: int, body: SceneUpdate,
                       p: Principal = Depends(principal)) -> dict:
    """Rename a scene or set its standing rules — the editable title and the rules box."""
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        s = w.scene(room_id)
        if not s:
            raise HTTPException(404, "no such room")
        if body.name is not None:
            s.name = body.name.strip()[:120]
        if body.rules is not None:
            s.rules = body.rules[:2000]
        if body.rules_rows is not None:
            from substrate.scene_rules import validate_rows
            s.rules_rows = validate_rows(body.rules_rows)   # coerce + whitelist + cap
        store.save(w)
        return {"room": s.view()}


@wr.post("/{world_id}/room/{room_id}/thread/connect")
async def thread_connect(world_id: int, room_id: int, body: ThreadEdge,
                         p: Principal = Depends(principal)) -> dict:
    """Thread two agents together (drawn on the canvas) — creates, extends, or merges."""
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        s = w.scene(room_id)
        if not s:
            raise HTTPException(404, "no such room")
        # Validated: this used to accept any two integers. An id belonging to an agent
        # seated in a DIFFERENT room became a full member of this graph — speaking,
        # hearing and spending — while being absent from the room's own agent list.
        from substrate.human import Human
        for who in (body.a, body.b):
            ent = w.get(who)
            if not isinstance(ent, Human) or who not in s.seats:
                raise HTTPException(400, "you can only connect agents seated in this room")
        if body.a == body.b:
            raise HTTPException(400, "an agent cannot be threaded to itself")
        t = s.connect(body.a, body.b, dir=body.dir, closed=body.closed)
        store.save(w)
        return {"thread": t, "threads": s.threads}


@wr.post("/{world_id}/room/{room_id}/thread/disconnect")
async def thread_disconnect(world_id: int, room_id: int, body: ThreadEdge,
                            p: Principal = Depends(principal)) -> dict:
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        s = w.scene(room_id)
        if not s:
            raise HTTPException(404, "no such room")
        s.disconnect(body.a, body.b)
        store.save(w)
        return {"threads": s.threads}


@wr.post("/{world_id}/room/{room_id}/thread/{tid}")
async def thread_update(world_id: int, room_id: int, tid: int, body: ThreadUpdate,
                        p: Principal = Depends(principal)) -> dict:
    """Set a thread's name, its own rule table, or its hidden manager."""
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        s = w.scene(room_id)
        if not s:
            raise HTTPException(404, "no such room")
        t = s.thread(tid)
        if not t:
            raise HTTPException(404, "no such thread")
        if body.name is not None:
            t["name"] = body.name.strip()[:60]
        if body.rulebook is not None:
            t["rulebook"] = body.rulebook[:2000]
        if body.manager is not None:
            from substrate.world import MODEL_WHITELIST
            m = body.manager or {}
            t["manager"] = {"model": (m.get("model") if m.get("model") in MODEL_WHITELIST else ""),
                            "budget": manifest.clean_budget(m)}
        if body.protocol is not None:
            from substrate.threads import clean_protocol
            t["protocol"] = clean_protocol(body.protocol)
        store.save(w)
        return {"thread": t}


@wr.post("/{world_id}/room/{room_id}/thread/{tid}/refine")
async def thread_refine(world_id: int, room_id: int, tid: int, body: RefineBody,
                        p: Principal = Depends(principal)) -> dict:
    """Polish a graph's rulebook with the LLM (offline → unchanged). Never applied until
    the owner saves it — this only proposes."""
    _own(p, world_id)
    complete, settings = _author_creds(p)
    text = (body.text or "").strip()
    if not text:
        return {"text": ""}
    if complete is None:
        return {"text": text}                      # offline: unchanged (no spend)
    sys_prompt = (
        "You are a text formatter. You receive a DRAFT of rules for a group of agents — it "
        "may be informal, terse, or a single sentence like 'I want mike to ask harvey stuff'. "
        "Your ONLY job is to rewrite that draft as a clean, numbered rulebook of clear, "
        "enforceable directives. ALWAYS rewrite whatever you are given — never ask a "
        "question, never request clarification, never add preamble, commentary, or a closing "
        "line, never refuse. If the draft is one instruction, express it as one or two "
        "concrete rules. Output ONLY the numbered rules.")
    prompt = f"DRAFT:\n{text}\n\nRewrite the DRAFT as a numbered rulebook. Output only the rules."
    try:
        raw = (await complete("anthropic", _default_model(), sys_prompt, prompt,
                              settings, max_tokens=350) or "").strip()
        low = raw.lower()
        # guard: if the model ignored the instruction and asked for input, keep the draft
        if not raw or any(q in low for q in ("could you", "please provide", "i need the",
                                             "i'm ready to help", "clarification")):
            return {"text": text}
        return {"text": raw[:2000]}
    except Exception:
        return {"text": text}


@wr.delete("/{world_id}/room/{room_id}/thread/{tid}")
async def thread_delete(world_id: int, room_id: int, tid: int,
                        p: Principal = Depends(principal)) -> dict:
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        s = w.scene(room_id)
        if not s:
            raise HTTPException(404, "no such room")
        s.threads = [t for t in s.threads if t["id"] != tid]
        store.save(w)
        return {"threads": s.threads}


@wr.get("/{world_id}/room/{room_id}/thread/{tid}/chat")
def thread_chat_history(world_id: int, room_id: int, tid: int,
                        p: Principal = Depends(principal)) -> dict:
    """The saved chats for a graph: {peer_id | "manager": [{role, text, ts}, …]}."""
    w = _world(p, world_id)
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    t = s.thread(tid)
    if not t:
        raise HTTPException(404, "no such thread")
    return {"chats": t.get("chats", {})}


@wr.post("/{world_id}/room/{room_id}/thread/{tid}/chat")
async def thread_chat(world_id: int, room_id: int, tid: int, body: ChatBody,
                      live: int = 0, p: Principal = Depends(principal)) -> dict:
    """Send a message to an agent in the graph, or to the graph's manager (pinned). One
    bounded model call per message in Live mode (deterministic offline).

    THE LOCK IS NEW AND IT MATTERS. Conductor-side, only the self-repair chat route took
    it; the Studio's own chat did the same load→await→save round trip without one, so a
    sprint deliberation landing mid-chat erased whichever finished first. The conductor
    cannot hold this lock any more — it has no World to hold one over — so every
    read-modify-write in this file takes it here, which is the only place it can be taken.
    """
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id, live=bool(live) and p.live_ok,
                       settings_ref=p.settings_ref, source=p.source)
        s = w.scene(room_id) if w else None
        if not s:
            raise HTTPException(404, "no such room")
        t = s.thread(tid)
        if not t:
            raise HTTPException(404, "no such thread")
        res = await s.chat(t, body.to, body.text)
        store.save(w)
    return res


@wr.post("/{world_id}/room/{room_id}/thread/{tid}/run")
async def thread_run(world_id: int, room_id: int, tid: int, rounds: int = 2,
                     live: int = 0, p: Principal = Depends(principal)) -> dict:
    """The reconciliation loop: deliberate `rounds` rounds (protocol-capped) over this
    graph, then the manager synthesizes the DECISION MEMO — versioned on the thread,
    returned here. Free by default. ?live=1 spends BOUNDED calls: at most rounds+1 under
    the classic protocol; under independent init add one call per agent for the opening
    round (N + rounds + 1), and a unanimous final round may add one dissent round — always
    within the max_rounds cap, O(N+rounds)."""
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id, live=bool(live) and p.live_ok,
                       settings_ref=p.settings_ref, source=p.source)
        s = w.scene(room_id) if w else None
        if not s:
            raise HTTPException(404, "no such room")
        t = s.thread(tid)
        if not t:
            raise HTTPException(404, "no such thread")
        memo = await s.run_deliberation(t, rounds)
        store.save(w)
        return {"result": memo, "room": s.view()}


@wr.get("/{world_id}/room/{room_id}/thread/{tid}/results")
def thread_results(world_id: int, room_id: int, tid: int,
                   p: Principal = Depends(principal)) -> dict:
    """Every kept decision memo for this graph, oldest→newest (v1, v2, …)."""
    w = _world(p, world_id)
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    t = s.thread(tid)
    if not t:
        raise HTTPException(404, "no such thread")
    return {"results": t.get("results", [])}


@wr.post("/{world_id}/manifest")
async def apply_manifest(world_id: int, body: ManifestBody, live: int = 0,
                         p: Principal = Depends(principal)) -> dict:
    """Declare a whole team as one spec and materialise it: a new scene with the agents
    (built deterministically from their dials — applying never spends), wired per `edges`,
    rules + manager installed. The canvas shows it like any hand-built scene — the Studio
    is one client of this API. If run.rounds is set, deliberates immediately."""
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id, live=bool(live) and p.live_ok,
                       settings_ref=p.settings_ref, source=p.source)
        s = manifest.materialise(w, body)
        by_name = {(w.get(hid).name): hid for hid in s.seats}
        result = None
        rounds = int((body.run or {}).get("rounds", 0) or 0)
        if rounds and s.threads:
            result = await s.run_deliberation(s.threads[0], rounds)
        store.save(w)
        return {"room": s.view(), "agents": by_name,
                "thread_ids": [t["id"] for t in s.threads], "result": result}


@wr.post("/{world_id}/touch")
async def touch(world_id: int, p: Principal = Depends(principal)) -> dict:
    """An explicit save — re-persist the world so Cmd+S has something honest to confirm."""
    _own(p, world_id)
    async with store.lock_for(world_id):
        store.save(store.load(world_id))
    return {"ok": True}


@wr.delete("/{world_id}/room/{room_id}")
async def delete_room(world_id: int, room_id: int,
                      p: Principal = Depends(principal)) -> dict:
    """Delete a scene (its canvas). Agents and artifacts are world-level, so they survive
    in the cast and any other scene they're in — only this scene goes."""
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        w.scenes.pop(room_id, None)
        store.save(w)
    return {"ok": True, "deleted": room_id}


@wr.delete("/{world_id}/entity/{entity_id}")
async def delete_entity(world_id: int, entity_id: int,
                        p: Principal = Depends(principal)) -> dict:
    """Delete an agent or artifact from the world entirely — dropped from the cast,
    removed from every scene, and unseated from any table it sat at."""
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        w.entities.pop(entity_id, None)
        for s in w.scenes.values():
            s.seats = [i for i in s.seats if i != entity_id]
            s.props = [i for i in s.props if i != entity_id]
        for a in w.artifacts():             # pop the deleted agent out of any table's ring
            if entity_id in (getattr(a, "seated", None) or []):
                a.unseat(entity_id)
        store.save(w)
    return {"ok": True, "deleted": entity_id}


@wr.get("/{world_id}/room/{room_id}")
def room_view(world_id: int, room_id: int, p: Principal = Depends(principal)) -> dict:
    w = _world(p, world_id)
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    return {"room": s.view()}


# --- the verbs (one scan of room time each) ---------------------------------

@wr.post("/{world_id}/room/{room_id}/act")
async def act(world_id: int, room_id: int, body: Act, live: int = 0,
              p: Principal = Depends(principal)) -> dict:
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id, live=bool(live) and p.live_ok,
                       settings_ref=p.settings_ref, source=p.source)
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


@wr.post("/{world_id}/room/{room_id}/round")
async def play_round(world_id: int, room_id: int, live: int = 0,
                     p: Principal = Depends(principal)) -> dict:
    """The one-button beat: everyone greets a neighbour and, if a deck is present, draws a
    card; then the room rests (consolidates). Free unless ?live=1."""
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id, live=bool(live) and p.live_ok,
                       settings_ref=p.settings_ref, source=p.source)
        s = w.scene(room_id)
        if not s:
            raise HTTPException(404, "no such room")
        s.round_no += 1                  # one beat = one round; every log row is stamped
        if s.threads:                    # threads present → each thread's Host plays its members
            for t in list(s.threads):
                await s.run_thread(t)
            s.rest()
            store.save(w)
            return {"room": s.view(), "world_tau": w.tau}
        from substrate.components import has_component

        def _drawable(a):        # the old Deck OR a Composite carrying a multiset
            return a.kind == "deck" or (a.kind == "composite"
                                        and has_component(getattr(a, "spec", {}), "multiset"))
        deck = next((a for a in s.props_here() if _drawable(a)), None)

        async def play(ring):
            for i, h in enumerate(ring):
                if deck:
                    await s.interact(h, deck.id, "draw")
                if len(ring) > 1:
                    await s.greet(h, ring[(i + 1) % len(ring)])

        # Prefer clusters: each collating artifact's seated agents act as a ring. If no
        # cluster has formed yet, fall back to everyone in the room so a round still does
        # something.
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


@wr.get("/{world_id}/human/{human_id}")
def peek(world_id: int, human_id: int, p: Principal = Depends(principal)) -> dict:
    """One agent's detail panel.

    The backend's own log rows for this agent are NOT here: they belong to the watch
    service and the conductor composes them onto this answer for a root caller. A service
    that fetched another service's rows to decorate its own would be a second, undeclared
    caller of watch with none of watch's gates.
    """
    w = _world(p, world_id)
    h = w.get(human_id)
    if not h:
        raise HTTPException(404, "no such agent")
    hand = [{"id": a.id, "value": a.reveal(h)} for a in w.artifacts()
            if a.kind == "card" and a.holder == human_id]
    # The decision tree, and what this agent has learned to expect. Private decisions are
    # withheld from everyone but root: an agent's own reasoning is exactly the kind of
    # thing a scene may have made secret, and a detail panel must not be the way it leaks.
    is_root = p.is_root
    nodes = [n.to_dict() for n in h.decisions.nodes if is_root or n.scope != "private"]
    assoc = sorted((a.to_dict() for a in h.decisions.assoc.values()),
                   key=lambda a: (-a["confidence"], -a["evidence"]))
    # What it is doing right now — the first question anyone opens an agent to ask, and
    # the one with a wrong answer that costs money.
    from substrate.scene import _activity_of
    return {"human": h.profile(), "narrative": h.narrative, "hand": hand,
            "activity": _activity_of(world_id, h.id), "usage": h.usage(),
            "withheld": 0 if is_root else sum(1 for n in h.decisions.nodes
                                              if n.scope == "private"),
            "habits": [{"when": r.match, "confidence": round(r.confidence, 2),
                        "fires": r.fires} for r in h.rules.rules],
            "bonds": {oid: {"trust": round(b.trust, 2), "warmth": round(b.warmth, 2)}
                      for oid, b in h.social.bonds.items()},
            "decisions": nodes[-60:], "associations": assoc,
            "name": h.name,
            "canon": [n["id"] for n in nodes if n.get("canon")]}


# --- the crew's own verbs (whole behaviours, never accessors) ----------------
#
# Mounted from crew.py rather than written here because they are a different KIND
# of endpoint: the Studio routes above are one canvas action each, while every
# route in crew.py is a whole behaviour the self-repair engine used to perform by
# hand on live objects. Same app, same token, same lock.

import crew  # noqa: E402

app.include_router(wr)
app.include_router(crew.router)
app.include_router(crew.pool_router)


if __name__ == "__main__":
    if "--spec" in sys.argv:
        # The one honest way to update the contract: print what the code actually serves,
        # commit it, and let the tests + oasdiff hold you to it.
        print(json.dumps(app.openapi(), indent=2))
    else:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8885")))
