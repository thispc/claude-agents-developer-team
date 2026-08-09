"""The self-repair crew's verbs — whole behaviours, never accessors.

This module is the hardest cut of P4. Before it, `conductor/app/repair.py`
imported `ManifestAgent`, `ManifestBody` and `materialise_manifest` out of a
ROUTE module, materialised a scene, then performed deep surgery on the live
`Human` objects that came back: carrying memory, decisions, skills and tau across
a re-seat by name, adopting a surviving room when the kv pointer had gone stale,
tidying dead rooms out of the world blob, recording a decision on a specialist,
resolving it later, running the sprint deliberation, brokering a consult between
graph neighbours, and asking one of them to review a green diff.

Every one of those is a READ-MODIFY-WRITE on a world blob, which is why none of
them could stay conductor-side: `store.lock_for` guards a load…await…save cycle,
and a lock you can only take on one side of a wire is not a lock. So the whole
behaviour moved, and `repair.ensure_team` became a thin client that keeps its kv
record and its adoption semantics.

WHAT DID NOT MOVE, on purpose:

  the kv record   `repair:world` is the crew's own pointer and stays with the
                  engine that owns it. This service is told the current pointer
                  and answers with the one that is now true.
  the wording     Every refusal string a build session can read ("you can only
                  consult your graph neighbours — the arrows are the org chart")
                  is composed by the conductor from a machine-readable reason
                  here. The crew's prompts are the engine's voice, not this
                  service's.
  knowledge with  Picking the best-informed neighbour and looking up what it
  the owner's key already concluded happen HERE, against the knowledge service's
                  free local backend, because this process holds no credential.
                  The conductor's own recalls keep the embedding key and still
                  read the same rows — knowledge re-embeds a row locally when the
                  backends differ, so a lesson written with a real embedder is
                  still found, coarser and never absent.
  the ledger,     Metering, the bus, the log lines: all conductor-side, from the
  the bus, logs   facts these endpoints return. A service that emitted its own
                  crew events would be a second writer of the crew's story.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import manifest
import store
from caller import AUTH, Principal, principal
from manifest import ManifestAgent, ManifestBody

# Same shared 404 the Studio routes declare: a world id you do not own and a world
# id that does not exist are one answer.
COMMON = {404: {"description": "no such world of yours — or no such room, thread, "
                               "agent or decision inside it"},
          400: {"description": "a malformed request body"}}
router = APIRouter(dependencies=AUTH, responses=COMMON)
# The staffing pool is not world-scoped and cannot 404: it answers with whatever
# this owner has, which may honestly be nothing.
pool_router = APIRouter(dependencies=AUTH)


# --- bodies ------------------------------------------------------------------

class Factor(BaseModel):
    """One enabled lens of the crew: the persona the engine wants seated."""
    id: str
    name: str
    brief: str = ""
    dials: dict = Field(default_factory=dict)
    drives: dict = Field(default_factory=dict)


class SeatingBody(BaseModel):
    # A crew with no lenses is not a crew. Expressed as schema so it is in the
    # committed contract and FastAPI answers it in FastAPI's own 422 shape.
    factors: list[Factor] = Field(min_length=1)
    manager: dict = Field(default_factory=dict)      # {model, budget}
    protocol: dict = Field(default_factory=dict)
    scene_name: str = ""                             # "sprint table · N lenses"
    world_name: str = "devteam IT crew"              # used only when world_id is 0
    # The engine's CURRENT pointer, so this service can tell an adoption from a
    # rebuild without inventing a second copy of the record.
    current_room_id: int = 0


class ContextBody(BaseModel):
    room_id: int
    thread_id: int
    human_id: int
    cue: str = ""


class DecisionBody(BaseModel):
    human_id: int
    saw: str
    understood: str = ""
    chose: str = ""
    because: dict = Field(default_factory=dict)


class OutcomeBody(BaseModel):
    human_id: int
    decision_id: int
    ok: bool
    says: str = ""


class ConsultBody(BaseModel):
    room_id: int
    thread_id: int
    human_id: int                                    # the asker (the task's specialist)
    question: str
    who: str = ""                                    # a named neighbour, or "" to pick


class ReviewBody(BaseModel):
    room_id: int
    thread_id: int
    human_id: int
    question: str
    cue: str = ""                                    # what to rank the reviewers on


class DeliberateBody(BaseModel):
    room_id: int
    thread_id: int
    topic: str = ""
    rulebook: str = ""
    rounds: int = 2


class ChatNoteBody(BaseModel):
    room_id: int
    thread_id: int
    role: str = "manager"
    text: str = ""


# --- helpers -----------------------------------------------------------------

def _own(p: Principal, world_id: int) -> None:
    if not store.owns(p.owner_id, world_id):
        raise HTTPException(404, "no such world of yours")


def _live(p: Principal, world_id: int):
    """The crew's world with the model door wired in — or free, when the conductor
    stamped no settings reference (offline, no credentials). Never guesses."""
    return store.load(world_id, live=p.live_ok, settings_ref=p.settings_ref,
                      source=p.source or "repair")


def _table(w, room_id: int, thread_id: int):
    s = w.scene(room_id) if w else None
    t = s.thread(thread_id) if s else None
    return s, t


def _persona_of(h) -> dict:
    from substrate.world import _persona
    return _persona(h)


def _neighbours(s, t, me):
    """The agents this one can actually HEAR — a consult's answer has to be able to
    reach the asker, so a neighbour is someone the asker hears. The arrows are the
    org chart, and they are enforced here rather than requested in a prompt."""
    from substrate.threads import members_of
    if s is None or t is None or me is None:
        return []
    return [o for o in (s.world.get(i) for i in members_of(t))
            if o is not None and o.id != me.id and s._hears(t, o.id, me.id)]


async def _best_informed(peers, question: str, world_id: int):
    """Which neighbour has the most relevant PROVEN lesson about this question.
    Free local-backend recall (see the module docstring) — a ranking nuance, never
    a gate: with nothing to go on the caller falls back to the first neighbour."""
    from substrate import ports
    best, score = None, -1.0
    for p_ in peers:
        try:
            hits = await ports.knowledge().recall(
                ports.agent_key_for("lw", world_id, p_.id), question, k=1)
            s_ = hits[0]["score"] if hits else 0.0
        except Exception:
            s_ = 0.0
        if s_ > score:
            best, score = p_, s_
    return best


# --- seating: the crew's table, adopted or rebuilt ---------------------------

@router.post("/worlds/{world_id}/crew-seating")
async def crew_seating(world_id: int, body: SeatingBody,
                       p: Principal = Depends(principal)) -> dict:
    """Seat the crew: one persona per enabled factor plus the hidden manager.

    ADOPT BEFORE REBUILD. The engine's kv record is only a POINTER and it can lose a
    race the world file wins (two servers once each saved their own idea of this
    world), after which the record names a room that is nowhere on disk while the real
    crew sits alive in another. Rebuilding then would orphan everything keyed to the
    living humans' ids — the knowledge rows above all. So a scene that already seats
    exactly these names is ADOPTED and the ids survive.

    A REBUILD KEEPS WHAT WAS EARNED. Toggling one factor re-seats the table, and that
    used to throw away the manager conversation, the deliberation memos and every
    association each specialist had proved. A factor toggle is a change of lineup, not
    amnesia. Carried by NAME, because ids are reassigned by the rebuild; the psyche is
    deliberately NOT carried, since re-seeding it from the dials is the point.

    `world_id` 0 means "the crew has no world yet" — one is created for the caller.
    """
    if world_id:
        _own(p, world_id)
    async with store.lock_for(world_id or 0):
        w = store.load(world_id) if world_id else None
        if w is None:
            w = store.create(p.owner_id, body.world_name)
        names = [f.name for f in body.factors]
        adopted = _adopt(w, body, names)
        if adopted:
            return adopted
        keep_agents, keep_thread = _carry_over(w, body.current_room_id)
        s = manifest.materialise(w, ManifestBody(
            name=body.scene_name or f"sprint table · {len(body.factors)} lenses",
            agents=[ManifestAgent(name=f.name, brief=f.brief, dials=f.dials or {},
                                  drives=f.drives or {}) for f in body.factors],
            edges=[[names[i], names[(i + 1) % len(names)]] for i in range(len(names))]
                  if len(names) > 1 else [],
            rules="", manager=body.manager or {}, protocol=body.protocol or {}))
        _restore(s, keep_agents, keep_thread)
        dropped = _tidy(w, s.id)
        store.save(w)
        return {"world_id": w.id, "room_id": s.id,
                "thread_id": s.threads[0]["id"] if s.threads else 0,
                "agents": {f.id: hid for f, hid in zip(body.factors, s.seats)},
                "outcome": "rebuilt", "dropped_rooms": dropped}


def _adopt(w, body: SeatingBody, names: list[str]) -> dict | None:
    """If some scene in this world already seats exactly the crew's personas (matched by
    NAME — ids are whatever history left), point the record at it instead of rebuilding."""
    want = set(names)
    for s in w.scenes.values():
        players = list(s.players())
        if {h.name for h in players} != want or not s.threads:
            continue
        by_name = {h.name: h.id for h in players}
        return {"world_id": w.id, "room_id": s.id,
                "thread_id": s.threads[0]["id"] if s.threads else 0,
                "agents": {f.id: by_name[f.name] for f in body.factors},
                "outcome": "adopted", "dropped_rooms": 0}
    return None


def _carry_over(w, room_id: int) -> tuple[dict, dict]:
    keep_agents: dict[str, dict] = {}
    keep_thread: dict = {}
    old = w.scene(room_id) if room_id else None
    if old is None:
        return keep_agents, keep_thread
    for h in old.players():
        keep_agents[h.name] = {"memory": h.memory.to_dict(),
                               "decisions": h.decisions.to_dict(),
                               "skills": h.skills.to_dict(), "tau": h.tau}
    t0 = old.threads[0] if old.threads else None
    if t0:
        keep_thread = {"chats": t0.get("chats") or {}, "results": t0.get("results") or []}
    return keep_agents, keep_thread


def _restore(s, keep_agents: dict, keep_thread: dict) -> None:
    from substrate.decisions import DecisionLog
    from substrate.memory import Memory
    from substrate.skills import Skills
    for h in s.players():
        prior = keep_agents.get(h.name)
        if not prior:
            continue
        h.memory = Memory.from_dict(prior.get("memory"))
        h.decisions = DecisionLog.from_dict(prior.get("decisions"))
        h.skills = Skills.from_dict(prior.get("skills"))
        h.tau = int(prior.get("tau") or 0)
    if keep_thread and s.threads:
        s.threads[0]["chats"] = keep_thread.get("chats") or {}
        s.threads[0]["results"] = keep_thread.get("results") or []


def _tidy(w, keep_room: int) -> int:
    """Leave exactly one sprint table behind.

    Every rebuild used to materialise a new scene and simply abandon the old one, so
    the crew's world quietly accumulated a room per persona bump and per factor toggle
    — six of them, five dead, each still holding six agents. It is not only clutter:
    the world is one blob, loaded and saved whole, so the dead rooms were being paid
    for on every tick. This world is entirely ours — nobody authored anything in it by
    hand — so anything outside the current table can go.
    """
    from substrate.human import Human
    dropped = 0
    for sid in [i for i in list(w.scenes) if i != keep_room]:
        w.scenes.pop(sid, None)
        dropped += 1
    seated = set(w.scene(keep_room).seats) if w.scene(keep_room) else set()
    for eid, ent in list(w.entities.items()):
        if isinstance(ent, Human) and eid not in seated:
            w.entities.pop(eid, None)
    return dropped


# --- the specialist: who is building, and what it has proven ----------------

@router.post("/worlds/{world_id}/crew-context")
def crew_context(world_id: int, body: ContextBody,
                 p: Principal = Depends(principal)) -> dict:
    """What makes a build a SPECIALIST's session rather than an anonymous one: who is
    building, what it is like, the association it has already PROVEN about situations
    like this task, and which teammates its arrows let it consult.

    Read-only — a snapshot load, no world lock; only load…mutate…save cycles need one.
    The knowledge-base half of the briefing stays with the conductor, which holds the
    embedding key; this answers the part only the world knows.
    """
    _own(p, world_id)
    from substrate.decisions import signature
    w = store.load(world_id)
    s, t = _table(w, body.room_id, body.thread_id)
    h = w.get(body.human_id) if w else None
    from substrate.human import Human
    if not isinstance(h, Human):
        raise HTTPException(404, "no such crew agent")
    persona = _persona_of(h)
    exact = h.decisions.recall(signature(body.cue, kind="task")) if body.cue else None
    neighbours = [o.name for o in _neighbours(s, t, h)] if (s and t) else []
    return {"name": h.name, "traits": persona.get("traits") or {},
            "wants": persona.get("wants") or "",
            "exact": ({"says": exact.says[:200], "evidence": exact.evidence,
                       "confidence": exact.confidence} if exact is not None else None),
            "neighbours": neighbours}


@router.post("/worlds/{world_id}/crew-decision")
async def crew_decision(world_id: int, body: DecisionBody,
                        p: Principal = Depends(principal)) -> dict:
    """Record, on the specialist that owns this task, the decision to attempt it.

    The crew is the right first tenant for decision memory because its outcomes are not
    a matter of opinion: the suite goes green or it does not. An association trained on
    that is trained on truth, which is the difference between learning and superstition.
    """
    _own(p, world_id)
    from substrate.decisions import signature
    async with store.lock_for(world_id):
        w = store.load(world_id)
        h = w.get(body.human_id) if w else None
        if h is None:
            raise HTTPException(404, "no such crew agent")
        d = h.decisions.record(tau=int(getattr(h, "tau", 0)),
                               sig=signature(body.saw, kind="task"), saw=body.saw,
                               understood=body.understood, chose=body.chose,
                               because=body.because)
        store.save(w)
        return {"decision_id": d.id, "sig": d.sig}


@router.post("/worlds/{world_id}/crew-decision-get")
def crew_decision_get(world_id: int, body: OutcomeBody,
                      p: Principal = Depends(principal)) -> dict:
    """One recorded decision, as it stands. The engine writes a decision and stamps its
    outcome through the two endpoints above and never needs to read one back — but the
    drills do, and a claim that "the outcome landed on the specialist" that can only be
    checked by opening this service's database would not be a claim about the boundary."""
    _own(p, world_id)
    w = store.load(world_id)
    h = w.get(body.human_id) if w else None
    node = h.decisions.get(int(body.decision_id)) if h is not None else None
    if node is None:
        raise HTTPException(404, "no such decision")
    return node.to_dict()


@router.post("/worlds/{world_id}/crew-outcome")
async def crew_outcome(world_id: int, body: OutcomeBody,
                       p: Principal = Depends(principal)) -> dict:
    """Stamp how it turned out, which is the only thing that moves an association.

    Returns the decision's own cue and signature so the CONDUCTOR can write the
    matching knowledge row with the owner's embedding key — the lesson belongs in the
    knowledge base keyed on the SITUATION, and the key for that stays where the key is.
    """
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        h = w.get(body.human_id) if w else None
        if h is None:
            raise HTTPException(404, "no such crew agent")
        h.decisions.resolve(int(body.decision_id), "good" if body.ok else "bad",
                            says=body.says[:200])
        store.save(w)
        node = h.decisions.get(int(body.decision_id))
        return {"ok": True, "name": h.name,
                "sig": node.sig if node else "", "saw": node.saw if node else ""}


# --- a consult, a review, a deliberation: one bounded call each -------------

@router.post("/worlds/{world_id}/crew-consult")
async def crew_consult(world_id: int, body: ConsultBody,
                       p: Principal = Depends(principal)) -> dict:
    """A build session asking a crew teammate for help.

    NEVER RAISES A DOMAIN FAILURE. Every way this can decline — no table, no
    neighbours, an outsider named, an unreachable answerer — comes back as
    `{"ok": false, "reason": …}` with the facts the conductor needs to phrase it,
    because a broken consult must cost the session a turn and not its life.

    Both agents genuinely perceive the exchange (free), so it lands in their memories
    like any other conversation — collaboration that leaves no trace in the
    participants is theatre. The ask goes on the ROOM record first, because `_hear`
    moves the listener's state but writes no log row, and a consultation invisible on
    the canvas may as well not have happened.
    """
    _own(p, world_id)
    from substrate.decisions import signature
    from substrate.threads import members_of
    from substrate import ports
    question = str(body.question or "").strip()
    async with store.lock_for(world_id):
        w = _live(p, world_id)
        s, t = _table(w, body.room_id, body.thread_id)
        me = w.get(body.human_id) if w else None
        if not (t is not None and me is not None):
            return {"ok": False, "reason": "no_table"}
        peers = _neighbours(s, t, me)
        if not peers:
            return {"ok": False, "reason": "no_peers"}
        nb = _named(peers, body.who)
        if body.who and nb is None:
            return {"ok": False, "reason": "not_a_neighbour",
                    "peers": [x.name for x in peers]}
        if nb is None:
            nb = await _best_informed(peers, question, world_id) or peers[0]
        s._record("say", me.id, f"{me.name}: (consult) {question[:180]}", frm=me.id)
        await s._hear(me, nb, f"(consult) {question[:200]}")
        known = nb.decisions.recall(signature(question))
        recalled = None
        if known is not None:
            recalled = {"says": known.says, "confidence": known.confidence,
                        "seen": known.evidence}
        else:
            hits = await ports.knowledge().recall(
                ports.agent_key_for("lw", world_id, nb.id), question, k=1)
            if hits and hits[0]["score"] >= 0.25:
                recalled = {"says": hits[0]["says"], "confidence": hits[0]["confidence"],
                            "seen": hits[0]["evidence"]}
        ring = [h for h in (w.get(i) for i in members_of(t)) if h is not None]
        answer = await w.agent_reply(
            nb, question,
            transcript=s._thread_transcript(ring, thread=t, for_agent=nb.id),
            recalled=recalled)
        if not answer:
            return {"ok": False, "reason": "unreachable", "who": nb.name}
        s._record("say", nb.id, f"{nb.name}: {answer[:200]}", frm=nb.id)
        await s._hear(nb, me, answer)          # the asker absorbs the answer, free
        store.save(w)
        return {"ok": True, "who": nb.name, "answer": answer,
                "model": w.model_for(nb)}


def _named(peers, who: str):
    """The NAMED half of picking a neighbour: case-insensitive match, or None — which
    the caller reads as 'refuse' when a name was given and as 'pick by knowledge' when
    none was. Returning peers[0] here for the unnamed case looked harmless and quietly
    disabled the knowledge-scored pick entirely."""
    if not who:
        return None
    wl = who.strip().lower()
    return next((x for x in peers if x.name.lower() == wl), None)


@router.post("/worlds/{world_id}/crew-review")
async def crew_review(world_id: int, body: ReviewBody,
                      p: Principal = Depends(principal)) -> dict:
    """A graph neighbour reads a green diff and answers. VERDICT ONLY — it never edits.
    Two writers on one branch is the mars-rover double-dispatch bug wearing a lab coat.

    The material (the stat AND a bounded slice of the real diff) is built conductor-side,
    where git is; this picks the best-informed neighbour and spends the one bounded call.
    """
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = _live(p, world_id)
        s, t = _table(w, body.room_id, body.thread_id)
        me = w.get(body.human_id) if w else None
        if not (t is not None and me is not None):
            return {"ok": False, "reason": "no_table"}
        peers = _neighbours(s, t, me)
        if not peers:
            return {"ok": False, "reason": "no_peers"}
        reviewer = await _best_informed(peers, body.cue or body.question,
                                        world_id) or peers[0]
        answer = await w.agent_reply(reviewer, body.question)
        store.save(w)
        return {"ok": bool(answer), "who": reviewer.name, "answer": answer or "",
                "reason": "" if answer else "unreachable"}


@router.post("/worlds/{world_id}/crew-deliberate")
async def crew_deliberate(world_id: int, body: DeliberateBody,
                          p: Principal = Depends(principal)) -> dict:
    """The sprint plan: the crew deliberates and the decision memo IS the plan.

    Held across load…save, which is the whole reason it is one endpoint: the crew chat
    route does the same round trip on this world, and without the lock whichever
    finished last silently erased the other — a sprint's memo, or the operator's
    conversation.

    `independent` comes back so the conductor can meter what actually went out:
    independent init spends one call per agent for the opening round, a host plan per
    later round, and one closing memo.
    """
    _own(p, world_id)
    from substrate.threads import members_of, protocol_of
    async with store.lock_for(world_id):
        w = _live(p, world_id)
        s, t = _table(w, body.room_id, body.thread_id)
        if t is None:
            return {"ok": False, "reason": "no_table", "memo": None}
        if body.topic:
            t["topic"] = body.topic
        if body.rulebook:
            t["rulebook"] = body.rulebook[:2000]
        memo = await s.run_deliberation(t, rounds=max(1, int(body.rounds)))
        store.save(w)
        return {"ok": True, "memo": memo,
                "independent": protocol_of(t).get("init") == "independent",
                "ring": len(members_of(t))}


@router.post("/worlds/{world_id}/crew-chat-note")
async def crew_chat_note(world_id: int, body: ChatNoteBody,
                         p: Principal = Depends(principal)) -> dict:
    """Drop a line into the crew thread's manager chat, so 'chat with the manager' can
    discuss what just happened (the sprint retro)."""
    _own(p, world_id)
    async with store.lock_for(world_id):
        w = store.load(world_id)
        _s, t = _table(w, body.room_id, body.thread_id)
        if t is None:
            return {"ok": False, "reason": "no_table"}
        convo = t.setdefault("chats", {}).setdefault(body.role or "manager", [])
        convo.append({"role": body.role or "manager", "text": body.text,
                      "ts": time.time()})
        t["chats"][body.role or "manager"] = convo[-40:]
        store.save(w)
        return {"ok": True, "kept": len(t["chats"][body.role or "manager"])}


@router.get("/worlds/{world_id}/crew-usage")
def crew_usage(world_id: int, room_id: int = Query(0),
               p: Principal = Depends(principal)) -> dict:
    """Each seated agent's own model-session usage — what the Improve screen shows
    beside the crew. Keyed by agent id; the factor each one owns is the engine's record
    to apply, not this service's to know."""
    _own(p, world_id)
    w = store.load(world_id)
    s = w.scene(room_id) if (w and room_id) else None
    people = s.players() if s is not None else (w.humans() if w else [])
    return {"agents": [{"agent_id": h.id, "name": h.name, "usage": h.usage()}
                       for h in people]}


# --- rooms as staffing pools (the module graph's assignment pool) ------------

@router.get("/worlds/{world_id}/room/{room_id}/members")
def room_members(world_id: int, room_id: int,
                 p: Principal = Depends(principal)) -> dict:
    """One room as a pool: {world_id, room_id, name, members}.

    404 rather than an empty pool when the room cannot be loaded, so a caller can tell
    "this room is gone" from "this room seats nobody" — the module graph falls back to
    the crew's own table on the first and shows an empty pool on the second.
    """
    _own(p, world_id)
    w = store.load(world_id)
    s = w.scene(room_id) if w else None
    if s is None:
        raise HTTPException(404, "no such room")
    return {"world_id": int(world_id), "room_id": int(room_id),
            "name": f"{w.name} · {s.name}" if s.name else w.name,
            "members": [{"agent_id": h.id, "name": h.name} for h in s.players()]}


@pool_router.get("/rooms")
def rooms(extra: str = Query(""), p: Principal = Depends(principal)) -> dict:
    """Every room that could staff a module: all of this owner's, plus any world id in
    `extra`.

    `extra` exists for exactly one caller — the module graph's team picker, which must
    also offer the crew's own world (owned by root, and reachable by any account the
    operator granted self-repair to). That route is root/self-repair gated conductor-side
    and passes only the crew's world id; this service does not own that decision and
    does not pretend to.
    """
    wids = [int(row["id"]) for row in store.listing(p.owner_id)]
    for raw in (extra or "").split(","):
        raw = raw.strip()
        if raw.isdigit() and int(raw) not in wids:
            wids.append(int(raw))
    out = []
    for wid in wids:
        try:
            w = store.load(wid)
        except Exception:
            continue
        if not w:
            continue
        for s in w.scenes.values():
            players = s.players()
            if not players:
                continue                    # a room that seats nobody staffs nothing
            out.append({"world_id": w.id, "room_id": s.id,
                        "name": f"{w.name} · {s.name}" if s.name else w.name,
                        "agents": len(players)})
    return {"rooms": out}
