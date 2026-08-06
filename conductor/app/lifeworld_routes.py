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
    model: str = ""                         # the LLM this agent possesses ("" = inherit world default)
    dials: dict = Field(default_factory=dict)   # explicit trait dials (0-100) from the base-DNA questions; override the authored ones
    drives: dict = Field(default_factory=dict)  # optional drive setpoints (0-1) seeded from the motivation question


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
    rulebook: str | None = None             # the graph's single free-text rulebook (obeyed by its manager)
    manager: dict | None = None             # {model, budget} — the hidden manager
    protocol: dict | None = None            # deliberation policy-as-data (threads.clean_protocol)


class RefineBody(BaseModel):
    text: str = ""


class ChatBody(BaseModel):
    to: str = "manager"                     # "manager" (pinned) or an agent id in the graph
    text: str = ""


class ManifestAgent(BaseModel):
    name: str
    model: str = ""                         # from MODEL_WHITELIST, or "" to inherit
    dials: dict = Field(default_factory=dict)    # trait -> 0-100 (or 0-1); unknown traits ignored
    drives: dict = Field(default_factory=dict)   # drive -> 0-1 setpoints
    brief: str = ""                         # one-line narrative, used verbatim (no authoring spend)
    figure: str = ""


class ManifestBody(BaseModel):
    """A whole team as ONE declarative spec — the substrate's manifest. Applying it materialises
    a real scene (agents, wiring, rules, manager) that the canvas shows like any other; optionally
    runs a deliberation immediately. Deterministic and free to APPLY; a run spends only with ?live=1."""
    name: str = ""                          # the scene's name
    agents: list[ManifestAgent]
    edges: list = Field(default_factory=list)    # [nameA, nameB, dir?] — names from `agents`; dir: both|a2b|b2a
    rules: str = ""                         # the graph's rulebook (what the manager makes happen)
    manager: dict = Field(default_factory=dict)  # {model, budget}
    protocol: dict = Field(default_factory=dict) # deliberation policy (preset/init/anonymize/on_unanimity/max_rounds)
    run: dict = Field(default_factory=dict)      # {rounds: 1-4} → deliberate now and return the memo


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



def _clean_budget(m: dict) -> int:
    """Manager budget from untrusted JSON: junk (\"abc\", Infinity, lists) → the default 2, never a 500."""
    try:
        return max(0, min(int(m.get("budget", 2) or 0), 4))
    except (TypeError, ValueError, OverflowError):
        return 2

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
    from app.lifeworld.psyche import TRAITS
    from app.lifeworld.drives import SPEC as DRIVE_SPEC
    from app.lifeworld.world import MODEL_WHITELIST
    from app.lifeworld.util import clamp01
    _, u = _load(request, world_id)
    w = store.load(world_id)
    complete, settings = _author_creds(u)
    # If the owner answered every base-DNA trait, we don't need a model to author the genome —
    # skip that call entirely (spends less); authoring still fills senses/skills deterministically.
    dna = {k: float(v) for k, v in (body.dials or {}).items() if k in TRAITS}
    auth_complete = None if len(dna) >= len(TRAITS) else complete
    spec = await authoring.author_human(
        body.name or f"Person {len(w.humans())+1}", body.brief,
        complete=auth_complete, settings=settings, model=tuning.get("scene_default_model"))
    # breeding: blend two parents' genomes over the authored dials (nature), and carry a
    # slice of their distilled memory (nurture).
    parents = [w.get(p) for p in body.parents if w.get(p)]
    if parents:
        for t in spec["dials"]:
            vals = [p.psyche.traits.get(t, 0.5) * 100 for p in parents]
            spec["dials"][t] = round(sum(vals) / len(vals))
        spec["narrative"] = f"{body.name or 'A child'}, of {' & '.join(p.name for p in parents)}."[:280]
    # The owner's explicit base-DNA answers win over whatever was authored/bred (nature by choice).
    for k, v in dna.items():
        spec["dials"][k] = round(clamp01(v / 100.0 if v > 1 else v) * 100)
    h = w.spawn_human(body.name or f"Person {len(w.humans())+1}", dials=spec["dials"],
                      senses=spec.get("senses") or None, figure=body.figure)
    h.model = body.model if body.model in MODEL_WHITELIST else ""   # possesses its own mind (or inherits)
    for k, v in (body.drives or {}).items():                       # seed the motivation setpoint(s)
        if k in DRIVE_SPEC:
            h.drives.level[k] = clamp01(float(v))
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


def _artifact_view(a) -> dict:
    return {"id": a.id, "name": a.name, "kind": a.kind, "figure": a.figure,
            "slots": a.slots, "seated": a.seated,
            "shape": getattr(a, "shape", "circle"), "path": getattr(a, "path", []),
            "sealed": bool(a.secret), "public": a.public, "spec": getattr(a, "spec", {})}


@router.get("/{world_id}/artifact-lib")
def artifact_lib(world_id: int, request: Request) -> dict:
    """The palette: shipped custom types, this world's saved types, and the vocabulary a generic
    build may use (component kinds + value-table builders)."""
    _own(request, world_id)
    w = store.load(world_id)
    from app.lifeworld.components import LIBRARY, _COMPONENTS, _BUILDERS
    return {"shipped": LIBRARY, "custom": w.lib_specs,
            "components": sorted(_COMPONENTS), "builders": sorted(_BUILDERS)}


@router.post("/{world_id}/artifact-lib")
def artifact_lib_save(world_id: int, body: LibSave, request: Request) -> dict:
    """Save a generic-built spec to this world's custom library for reuse (the save-to-custom loop)."""
    _own(request, world_id)
    w = store.load(world_id)
    from app.lifeworld.components import validate_spec
    vs = validate_spec(body.spec)
    if not vs:
        raise HTTPException(400, "the spec has no valid components")
    w.lib_specs[(body.name.strip()[:40] or vs["type"])] = vs
    store.save(w)
    return {"custom": w.lib_specs}


@router.post("/{world_id}/artifact")
async def add_artifact(world_id: int, body: NewArtifact, request: Request, seed: int = 0) -> dict:
    _, u = _load(request, world_id)
    w = store.load(world_id)
    # Composite path: a Custom library type, or a Generic-built spec. One class, any object, no exec.
    if body.type or body.spec:
        from app.lifeworld.components import LIBRARY, validate_spec
        from app.lifeworld.artifact import Composite
        resolved = validate_spec(body.spec) if body.spec else (LIBRARY.get(body.type) or w.lib_specs.get(body.type))
        if not resolved:
            raise HTTPException(400, "unknown artifact type / empty spec")
        a = Composite.from_spec(w.next_id(), resolved, name=body.name or resolved.get("type", "object"),
                                figure=body.figure or resolved.get("figure", ""), seed=seed)
        if int(body.slots) > 0 and not a.slots:          # allow dropping a composite onto a table footprint
            a.slots = int(body.slots); a.seated = [None] * a.slots
        a.shape = body.shape or "circle"; a.path = [list(p) for p in (body.path or [])]
        if body.save_as and body.spec:
            w.lib_specs[body.save_as[:40]] = resolved    # save-to-custom in one shot
        w.add(a); store.save(w)
        return {"artifact": _artifact_view(a)}
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
    return {"artifact": _artifact_view(a)}


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
    if body.rules_rows is not None:
        from app.lifeworld.scene_rules import validate_rows
        s.rules_rows = validate_rows(body.rules_rows)      # the trust boundary: coerce + whitelist + cap
    store.save(w)
    return {"room": s.view()}


@router.post("/{world_id}/room/{room_id}/thread/connect")
def thread_connect(world_id: int, room_id: int, body: ThreadEdge, request: Request) -> dict:
    """Thread two agents together (drawn on the canvas) — creates, extends, or merges a thread."""
    _own(request, world_id)
    w = store.load(world_id)
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    # Validated: this used to accept any two integers. An id belonging to an agent seated in
    # a DIFFERENT room became a full member of this graph — speaking, hearing and spending —
    # while being absent from the room's own agent list, so it was a participant nobody could
    # see. A graph may only wire people who are actually in the room.
    from .lifeworld.human import Human
    for who in (body.a, body.b):
        ent = w.get(who)
        if not isinstance(ent, Human) or who not in s.seats:
            raise HTTPException(400, "you can only connect agents seated in this room")
    if body.a == body.b:
        raise HTTPException(400, "an agent cannot be threaded to itself")
    t = s.connect(body.a, body.b, dir=body.dir, closed=body.closed)
    store.save(w)
    return {"thread": t, "threads": s.threads}


@router.post("/{world_id}/room/{room_id}/thread/disconnect")
def thread_disconnect(world_id: int, room_id: int, body: ThreadEdge, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    s.disconnect(body.a, body.b)
    store.save(w)
    return {"threads": s.threads}


@router.post("/{world_id}/room/{room_id}/thread/{tid}")
def thread_update(world_id: int, room_id: int, tid: int, body: ThreadUpdate, request: Request) -> dict:
    """Set a thread's name, its own rule table, or its hidden manager (model / budget / note)."""
    _own(request, world_id)
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
        from app.lifeworld.world import MODEL_WHITELIST
        m = body.manager or {}
        t["manager"] = {"model": (m.get("model") if m.get("model") in MODEL_WHITELIST else ""),
                        "budget": _clean_budget(m)}
    if body.protocol is not None:
        from app.lifeworld.threads import clean_protocol
        t["protocol"] = clean_protocol(body.protocol)
    store.save(w)
    return {"thread": t}


@router.post("/{world_id}/room/{room_id}/thread/{tid}/refine")
async def thread_refine(world_id: int, room_id: int, tid: int, body: RefineBody, request: Request) -> dict:
    """Polish a graph's rulebook with the LLM (offline → a light deterministic tidy). Never applied
    until the owner saves it — this only proposes."""
    _, u = _load(request, world_id)
    complete, settings = _author_creds(u)
    text = (body.text or "").strip()
    if not text:
        return {"text": ""}
    if complete is None:
        return {"text": text}                      # offline: unchanged (no spend)
    sys = ("You are a text formatter. You receive a DRAFT of rules for a group of agents — it may be "
           "informal, terse, or a single sentence like 'I want mike to ask harvey stuff'. Your ONLY job "
           "is to rewrite that draft as a clean, numbered rulebook of clear, enforceable directives. "
           "ALWAYS rewrite whatever you are given — never ask a question, never request clarification, "
           "never add preamble, commentary, or a closing line, never refuse. If the draft is one "
           "instruction, express it as one or two concrete rules. Output ONLY the numbered rules.")
    prompt = f"DRAFT:\n{text}\n\nRewrite the DRAFT as a numbered rulebook. Output only the rules."
    try:
        raw = (await complete("anthropic", tuning.get("scene_default_model"), sys, prompt, settings, max_tokens=350) or "").strip()
        low = raw.lower()
        # guard: if the model ignored the instruction and asked for input, keep the draft unchanged
        if not raw or any(p in low for p in ("could you", "please provide", "i need the", "i'm ready to help", "clarification")):
            return {"text": text}
        return {"text": raw[:2000]}
    except Exception:
        return {"text": text}


@router.delete("/{world_id}/room/{room_id}/thread/{tid}")
def thread_delete(world_id: int, room_id: int, tid: int, request: Request) -> dict:
    _own(request, world_id)
    w = store.load(world_id)
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    s.threads = [t for t in s.threads if t["id"] != tid]
    store.save(w)
    return {"threads": s.threads}


@router.get("/{world_id}/room/{room_id}/thread/{tid}/chat")
def thread_chat_history(world_id: int, room_id: int, tid: int, request: Request) -> dict:
    """The saved chats for a graph: {peer_id | "manager": [{role, text, ts}, …]}."""
    _own(request, world_id)
    w = store.load(world_id)
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    t = s.thread(tid)
    if not t:
        raise HTTPException(404, "no such thread")
    return {"chats": t.get("chats", {})}


@router.post("/{world_id}/room/{room_id}/thread/{tid}/chat")
async def thread_chat(world_id: int, room_id: int, tid: int, body: ChatBody, request: Request) -> dict:
    """Send a message to an agent in the graph, or to the graph's manager (pinned). One bounded
    model call per message in Live mode (deterministic offline); the reply is appended and saved."""
    _own(request, world_id)
    w = store.load(world_id)
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    t = s.thread(tid)
    if not t:
        raise HTTPException(404, "no such thread")
    res = await s.chat(t, body.to, body.text)
    store.save(w)
    return res


@router.post("/{world_id}/room/{room_id}/thread/{tid}/run")
async def thread_run(world_id: int, room_id: int, tid: int, request: Request,
                     rounds: int = 2, live: int = 0) -> dict:
    """The reconciliation loop: deliberate `rounds` rounds (protocol-capped) over this graph, then
    the manager synthesizes the DECISION MEMO — versioned on the thread, returned here. Free by
    default. ?live=1 spends BOUNDED calls: at most rounds+1 under the classic protocol; under
    independent init add one call per agent for the opening round (N + rounds + 1), and a
    unanimous final round may add one dissent round — always within the max_rounds cap, O(N+rounds)."""
    w, _ = _load(request, world_id, live=bool(live))
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    t = s.thread(tid)
    if not t:
        raise HTTPException(404, "no such thread")
    memo = await s.run_deliberation(t, rounds)
    store.save(w)
    return {"result": memo, "room": s.view()}


@router.get("/{world_id}/room/{room_id}/thread/{tid}/results")
def thread_results(world_id: int, room_id: int, tid: int, request: Request) -> dict:
    """Every kept decision memo for this graph, oldest→newest (v1, v2, …) — compare re-runs."""
    _own(request, world_id)
    w = store.load(world_id)
    s = w.scene(room_id)
    if not s:
        raise HTTPException(404, "no such room")
    t = s.thread(tid)
    if not t:
        raise HTTPException(404, "no such thread")
    return {"results": t.get("results", [])}


def materialise_manifest(w, body: ManifestBody):
    """The manifest's body as a plain function (no HTTP): spawn the agents deterministically
    from their dials, wire the edges by name, install rules + manager + protocol. Returns the
    new Scene. Shared by the route below and by background engines (self-repair's IT crew).
    Raises HTTPException on a bad spec — callers with no request context catch it like any error."""
    import math
    from app.lifeworld.psyche import TRAITS
    from app.lifeworld.drives import SPEC as DRIVE_SPEC
    from app.lifeworld.world import MODEL_WHITELIST
    from app.lifeworld.util import clamp01
    if not body.agents or len(body.agents) > 12:
        raise HTTPException(422, "a manifest needs 1-12 agents")
    names = [a.name.strip()[:60] for a in body.agents]
    if len(set(names)) != len(names) or not all(names):
        raise HTTPException(422, "agent names must be present and unique (edges address by name)")
    s = w.new_room((body.name or "manifest").strip()[:60], "freeplay")
    by_name: dict[str, int] = {}
    n = len(body.agents)
    for i, spec in enumerate(body.agents):
        dials = {}
        for k, v in (spec.dials or {}).items():
            if k in TRAITS:
                dials[k] = round(clamp01(float(v) / 100.0 if float(v) > 1 else float(v)) * 100)
        h = w.spawn_human(names[i], dials=dials or None, figure=spec.figure)
        h.model = spec.model if spec.model in MODEL_WHITELIST else ""
        for k, v in (spec.drives or {}).items():
            if k in DRIVE_SPEC:
                h.drives.level[k] = clamp01(float(v))
        if spec.brief.strip():
            h.narrative = spec.brief.strip()[:280]
        s.seat(h)
        ang = -math.pi / 2 + (i / max(1, n)) * math.tau        # a ring, top-first — reads instantly on the canvas
        h.pos = (420 + math.cos(ang) * (90 + 24 * n), 300 + math.sin(ang) * (70 + 18 * n))
        by_name[names[i]] = h.id
    for e in body.edges:
        if not (isinstance(e, (list, tuple)) and len(e) >= 2):
            raise HTTPException(422, f"bad edge {e!r} — use [nameA, nameB, dir?]")
        a, b = by_name.get(str(e[0])), by_name.get(str(e[1]))
        if a is None or b is None or a == b:
            raise HTTPException(422, f"edge {e!r} names an unknown (or same) agent")
        d = str(e[2]) if len(e) > 2 else "both"
        if d not in ("both", "a2b", "b2a"):
            raise HTTPException(422, f"edge dir must be both|a2b|b2a, got {d!r}")
        s.connect(a, b, dir=d)
    from app.lifeworld.threads import clean_protocol
    for t in s.threads:                                        # the graph's brief + its manager + protocol
        if body.rules:
            t["rulebook"] = body.rules[:2000]
        m = body.manager or {}
        t["manager"] = {"model": (m.get("model") if m.get("model") in MODEL_WHITELIST else ""),
                        "budget": _clean_budget(m)}
        t["protocol"] = clean_protocol(body.protocol)
    return s


@router.post("/{world_id}/manifest")
async def apply_manifest(world_id: int, body: ManifestBody, request: Request, live: int = 0) -> dict:
    """Declare a whole team as one spec and materialise it: a new scene with the agents (built
    deterministically from their dials — applying never spends), wired per `edges`, rules + manager
    installed. The canvas shows it like any hand-built scene — the Studio is one client of this API.
    If run.rounds is set, deliberates immediately and returns the decision memo."""
    w, _ = _load(request, world_id, live=bool(live))
    s = materialise_manifest(w, body)
    by_name = {(w.get(hid).name): hid for hid in s.seats}
    result = None
    rounds = int((body.run or {}).get("rounds", 0) or 0)
    if rounds and s.threads:
        result = await s.run_deliberation(s.threads[0], rounds)
    store.save(w)
    return {"room": s.view(), "agents": by_name,
            "thread_ids": [t["id"] for t in s.threads], "result": result}


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
    s.round_no += 1                      # one beat = one round; every log row it emits is stamped with it
    if s.threads:                        # threads present → each thread's Host plays its members per its rules
        for t in list(s.threads):
            await s.run_thread(t)
        s.rest()
        store.save(w)
        return {"room": s.view(), "world_tau": w.tau}
    from app.lifeworld.components import has_component
    def _drawable(a):                    # the old Deck OR a Composite carrying a multiset (deals cards)
        return a.kind == "deck" or (a.kind == "composite" and has_component(getattr(a, "spec", {}), "multiset"))
    deck = next((a for a in s.props_here() if _drawable(a)), None)

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
    # The decision tree, and what this agent has learned to expect. Private decisions are
    # withheld from everyone but root: an agent's own reasoning is exactly the kind of thing
    # a scene may have made secret, and a detail panel must not be the way it leaks.
    from . import auth, logs
    u = auth.user_for_token(request.cookies.get("devteam_session")) or {}
    is_root = bool(u.get("is_root"))
    nodes = [n.to_dict() for n in h.decisions.nodes
             if is_root or n.scope != "private"]
    assoc = sorted((a.to_dict() for a in h.decisions.assoc.values()),
                   key=lambda a: (-a["confidence"], -a["evidence"]))
    out = {"human": h.profile(), "narrative": h.narrative, "hand": hand,
           "habits": [{"when": r.match, "confidence": round(r.confidence, 2), "fires": r.fires}
                      for r in h.rules.rules],
           "bonds": {oid: {"trust": round(b.trust, 2), "warmth": round(b.warmth, 2)}
                     for oid, b in h.social.bonds.items()},
           "decisions": nodes[-60:], "associations": assoc,
           "canon": [n["id"] for n in nodes if n.get("canon")]}
    if is_root:
        # The backend's own record of this agent, from the log pipeline. Root only — logs
        # name file paths, branch names and the shape of the operator's work.
        out["logs"] = logs.recent(q=h.name, limit=40) if h.name else []
    return out
