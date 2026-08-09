"""A scene: a setting with rules and a goal that shapes what agents do while in it.

This is the general substrate the project/team/manager system is a special case of.
A project is a scene, a task is an artifact, the manager already walks the scene
deciding who does what. The casino table proves the substrate; a software team is
what it ships for. Everything expensive about running one is confined to a single
function, `_utter`, so the whole cost story is auditable in one place.

Four disciplines, each an owner constraint turned into architecture:

- **A secret is un-leakable.** A seat's hand lives only on its own `scene_agents`
  row. `agent_view` — the private-knowledge builder — is the ONLY reader of
  `private_state`, and it reads it for exactly the one seat whose prompt it is
  building. `public_view` never reads it at all; face-down cards render as backs. A
  structural test asserts this shape so a later edit cannot quietly widen it, the
  same guard the credential isolation has.

- **Effects are free; only utterances bill.** Dealing, flipping, computing a hand
  rank, advancing the turn order — all deterministic code in `effects.py`, zero
  model calls. The dealer is code and it imposes a turn order, so only the one agent
  whose turn it is ever thinks, about state the code already prepared. A five-player
  hand is O(turns), never O(agents²).

- **The bill is bounded and visible.** Every model call goes through `_utter`, which
  refuses to spend past the scene's token budget — the match pauses rather than
  running one up. `scenes.utterances` counts the calls, so O(turns) is a number a
  test reads, not a claim.

- **The winner is decided by code.** Showdown collates each live hand with
  `effects.collate` and compares with `effects.beats`. No model is asked who won.
"""

import json
from typing import Any, Callable

from . import bus, db, effects, providers, tuning

# A scene utterance is short by design — a decision and one line of flavour, never
# an essay. The chips are code; the model only supplies the character.
UTTER_SYSTEM = (
    "You are a character seated in a scene. Stay in character, in one or two "
    "sentences. When the scene asks you to act, state your choice plainly in your "
    "first few words (for poker: fold, or stay/call, or raise) and then say one "
    "short line as your character. Never narrate other players' private cards."
)

# Simplified, bounded hold'em economics. The flavour is the model's; every number
# here is code, so a garbled reply still yields a legal, deterministic move.
ANTE = 5
CALL = 10
RAISE = 20


def create(owner_id: int, kind: str = "poker", goal: str = "", title: str = "",
           seed: int = 0) -> dict:
    budget = int(tuning.get("scene_token_budget_default"))
    sid = db.create_scene(owner_id, kind, goal, title, token_budget=budget, seed=seed)
    db.add_scene_event(sid, "phase", f"scene '{title or kind}' created")
    bus.emit(0, None, "system", "scene_created", {"scene": sid, "kind": kind})
    return db.get_scene(sid)


def seat_agent(scene_id: int, home_id: int | None, role: str = "player",
               name: str = "") -> dict | None:
    """Sit an agent down. A home agent brings its own name; a bare code role (the
    dealer) needs none. Seat index is assigned in arrival order."""
    scene = db.get_scene(scene_id)
    if not scene:
        return None
    seats = db.list_scene_agents(scene_id)
    if home_id and not name:
        a = db.get_home_agent(home_id)
        name = (a or {}).get("name", "") or f"Seat {len(seats) + 1}"
    sid = db.add_scene_agent(scene_id, home_id=home_id, seat=len(seats), role=role,
                             name=name)
    db.add_scene_event(scene_id, "phase", f"{name or role} took a seat", seat_id=sid)
    return db.get_scene_agent(sid)


# --- the private-knowledge builder: the one and only reader of a secret --------

def _hand_of(seat: dict) -> list[dict]:
    """Read ONE seat's private hand. This is the sole place `private_state` is ever
    parsed; every other view goes through the public path. Keeping the read in one
    named function is what makes the isolation invariant checkable by a structural
    test rather than only by hope."""
    try:
        return list(json.loads(seat.get("private_state") or "{}").get("hand", []))
    except (ValueError, TypeError):
        return []


def _set_hand(seat_id: int, hand: list[dict]) -> None:
    db.update_scene_agent(seat_id, private_state=json.dumps({"hand": hand}))


def _public_seat(seat: dict) -> dict:
    """A seat as everyone may see it: who they are, their chips, whether they folded
    — never their hand. Deliberately does not touch `private_state`."""
    return {"id": seat["id"], "seat": seat["seat"], "role": seat["role"],
            "name": seat["name"], "status": seat["status"], "stack": seat["stack"],
            "committed": seat["committed"], "home_id": seat["home_id"]}


def _render_artifact(art: dict, viewer_seat_id: int | None) -> dict:
    """An artifact as a given viewer may see it. A face-down or held card shows its
    value ONLY to its holder; to anyone else it is a back. A `hidden` artifact — the
    deck, the machinery — never shows its value to anyone. This is the whole
    'an agent can hide what it does not want revealed' property, in code."""
    try:
        state = json.loads(art.get("state") or "{}")
    except (ValueError, TypeError):
        state = {}
    vis = art.get("visibility", "public")
    if vis == "hidden":
        shown = effects.back()                       # the shoe: opaque to everyone
    elif vis in ("facedown", "held") and art.get("holder") != viewer_seat_id:
        shown = effects.back()                       # someone else's secret: a back
    else:
        shown = state
    return {"id": art["id"], "type": art["type"], "visibility": vis,
            "holder": art["holder"], "state": shown}


def public_view(scene_id: int, viewer_seat_id: int | None = None) -> dict:
    """What a spectator (the owner watching the canvas) may see: the scene, the
    seats without their hands, and every artifact rendered so face-down cards are
    backs. Never leaks a hand — the owner peeks as a specific seat via `agent_view`.
    """
    scene = db.get_scene(scene_id)
    if not scene:
        return {}
    try:
        state = json.loads(scene.get("state") or "{}")
    except (ValueError, TypeError):
        state = {}
    seats = [_public_seat(s) for s in db.list_scene_agents(scene_id)]
    # The deck (visibility 'hidden') is the shoe — internal machinery whose order
    # would reveal every future card. It is never enumerated in any view; only cards
    # and pots on the table are. _render_artifact renders it as a back too, defence
    # in depth, but the real guarantee is that it is not here to be rendered.
    arts = [_render_artifact(a, viewer_seat_id) for a in db.list_artifacts(scene_id)
            if a.get("visibility") != "hidden"]
    return {
        "id": scene["id"], "kind": scene["kind"], "title": scene["title"],
        "goal": scene["goal"], "status": scene["status"], "phase": scene["phase"],
        "turn": scene["turn"], "pot": state.get("pot", 0),
        "token_budget": scene["token_budget"], "tokens_spent": scene["tokens_spent"],
        "utterances": scene["utterances"], "layout": _json(scene.get("layout")),
        "seats": seats, "artifacts": arts,
    }


def agent_view(scene_id: int, seat_id: int) -> dict:
    """What ONE seated agent's prompt may contain: the public view PLUS that seat's
    own hand and nothing of anyone else's. The private read is scoped to `seat_id`
    alone — this is the private-knowledge builder the whole secret rests on."""
    view = public_view(scene_id, viewer_seat_id=seat_id)
    seat = db.get_scene_agent(seat_id)
    view["you"] = _public_seat(seat) if seat else {}
    view["your_hand"] = _hand_of(seat) if seat else []   # only THIS seat's secret
    return view


# --- the dealer: free, deterministic ------------------------------------------

def _board(scene: dict) -> list[dict]:
    return _json(scene.get("state")).get("board", [])


def deal(scene_id: int) -> dict:
    """Deal a poker hand — entirely free code. A deterministic deck, two face-down
    hole cards to each player (the secret), five community cards face-up, and an ante
    from every player into the pot. No model is touched; a test deals and reads the
    exact cards from the seed."""
    scene = db.get_scene(scene_id)
    players = [s for s in db.list_scene_agents(scene_id, role="player")]
    if not scene or not players:
        return scene or {}

    db.clear_artifacts(scene_id)
    deck = effects.apply("deck", "fresh", scene["seed"])
    board_cards: list[dict] = []

    # Hole cards: two per player, face-down, added to that player's private hand.
    for p in players:
        cards, deck = effects.apply("deck", "draw", deck, 2)
        hand: list[dict] = []
        for c in cards:
            hand = effects.apply("card", "see", hand, c)
            db.create_artifact(scene_id, "card", c, visibility="facedown", holder=p["id"])
        _set_hand(p["id"], hand)
        commit = min(ANTE, p["stack"])
        db.update_scene_agent(p["id"], status="seated", committed=commit,
                              stack=p["stack"] - commit)

    # Community cards: five, face-up, shared.
    community, deck = effects.apply("deck", "draw", deck, 5)
    for c in community:
        board_cards.append(c)
        db.create_artifact(scene_id, "card", c, visibility="public")

    pot = sum(min(ANTE, p["stack"] + ANTE) for p in players)  # antes just committed
    db.create_artifact(scene_id, "deck", deck, visibility="hidden")
    db.update_scene(scene_id, status="live", phase="bet", turn=0, round=1,
                    state=json.dumps({"board": board_cards, "pot": pot, "acted": []}))
    db.add_scene_event(scene_id, "deal",
                       f"dealt {len(players)} players in; ante {ANTE}", billed=False)
    return db.get_scene(scene_id)


def _live_players(scene_id: int) -> list[dict]:
    return [s for s in db.list_scene_agents(scene_id, role="player")
            if s["status"] != "folded"]


def next_actor(scene_id: int) -> dict | None:
    """Whose turn it is — the first live player, in seat order, who has not yet acted
    this round. Free: the dealer is code and this is the turn order it imposes, the
    reason only one agent ever thinks at a time."""
    scene = db.get_scene(scene_id)
    if not scene or scene["phase"] != "bet":
        return None
    acted = set(_json(scene.get("state")).get("acted", []))
    for p in db.list_scene_agents(scene_id, role="player"):
        if p["status"] != "folded" and p["id"] not in acted:
            return p
    return None


def _decode(text: str) -> tuple[str, int]:
    """Turn a character's line into a legal move. Deterministic and total: any reply,
    including a garbled or empty one, maps to a legal action, so the game never
    depends on the model being well-behaved — only on it being flavourful."""
    low = (text or "").lower()
    if "fold" in low:
        return "fold", 0
    if "raise" in low or "all in" in low or "all-in" in low:
        return "raise", RAISE
    return "call", CALL   # the safe default: stay in for the flat call


# --- the single billing point -------------------------------------------------

def _model_for(seat: dict) -> tuple[str, str]:
    a = db.get_home_agent(seat["home_id"]) if seat.get("home_id") else None
    provider = (a or {}).get("provider") or "anthropic"
    model = (a or {}).get("model") or tuning.get("scene_default_model")
    return provider, model


async def _utter(scene: dict, seat: dict, prompt: str, settings: dict, *,
                 kind: str, max_tokens: int) -> str | None:
    """The ONE place a scene spends a token. Every model call in this module comes
    through here, so the budget, the count and the audit trail have exactly one
    home. Refuses to spend past the scene's token budget — the match pauses, it does
    not quietly run up a bill — and on any provider error records a free note and
    returns None so the caller can fall back to a deterministic move."""
    sid = scene["id"]
    if scene["tokens_spent"] + max_tokens > scene["token_budget"]:
        db.update_scene(sid, status="paused")
        db.add_scene_event(sid, "phase", "token budget reached — scene paused",
                           seat_id=seat["id"], billed=False)
        bus.emit(0, None, "system", "scene_paused", {"scene": sid})
        return None
    provider, model = _model_for(seat)
    try:
        text = await providers.complete(provider, model, UTTER_SYSTEM, prompt,
                                         settings, max_tokens=max_tokens)
    except Exception as e:
        db.add_scene_event(sid, "note", f"{seat['name']} stayed quiet ({str(e)[:80]})",
                           seat_id=seat["id"], billed=False)
        return None
    # Charge the ceiling as a conservative upper bound, exactly as home consolidation
    # does — a bounded call, bounded accounting.
    fresh = db.get_scene(sid)
    db.update_scene(sid, utterances=fresh["utterances"] + 1,
                    tokens_spent=fresh["tokens_spent"] + max_tokens)
    db.add_scene_event(sid, kind, text.strip()[:300], seat_id=seat["id"], billed=True)
    return text


async def act(scene_id: int, seat_id: int, settings: dict) -> dict:
    """One player's turn: at most ONE bounded model call for the character's choice,
    then deterministic chips. The move is always legal even if the call is skipped
    (budget) or fails (provider) — `_decode` maps every outcome to a legal action."""
    scene = db.get_scene(scene_id)
    seat = db.get_scene_agent(seat_id)
    if not scene or not seat:
        return scene or {}

    view = agent_view(scene_id, seat_id)
    prompt = _act_prompt(scene, view)
    max_tokens = int(tuning.get("scene_utterance_max_tokens"))
    text = await _utter(scene, seat, prompt, settings, kind="act", max_tokens=max_tokens)
    move, chips = _decode(text or "")     # None (paused/failed) -> the safe default

    if move == "fold":
        db.update_scene_agent(seat_id, status="folded")
    else:
        commit = min(chips, seat["stack"])
        db.update_scene_agent(seat_id, committed=seat["committed"] + commit,
                              stack=seat["stack"] - commit)
        st = _json(db.get_scene(scene_id).get("state"))
        st["pot"] = st.get("pot", 0) + commit
        db.update_scene(scene_id, state=json.dumps(st))

    st = _json(db.get_scene(scene_id).get("state"))
    acted = st.get("acted", [])
    if seat_id not in acted:
        acted.append(seat_id)
    st["acted"] = acted
    db.update_scene(scene_id, state=json.dumps(st))
    return db.get_scene(scene_id)


def showdown(scene_id: int) -> dict:
    """Decide the hand — free code, no model. Collate every live player's seven cards
    into their best five, compare with `effects.beats`, award the pot. On a tie the
    lower seat wins, deterministically, so a replay always names the same winner."""
    scene = db.get_scene(scene_id)
    if not scene:
        return {}
    board = _board(scene)
    live = _live_players(scene_id)
    best_seat: dict | None = None
    best_hand: dict | None = None
    for p in live:
        hand = effects.apply("card", "collate", _hand_of(p) + board)
        if best_hand is None or effects.apply("card", "beats", hand, best_hand) > 0:
            best_seat, best_hand = p, hand

    st = _json(scene.get("state"))
    pot = st.get("pot", 0)
    winner_txt = "no contest"
    if best_seat:
        db.update_scene_agent(best_seat["id"],
                              stack=best_seat["stack"] + pot, status="won")
        winner_txt = f"{best_seat['name']} wins {pot} with {best_hand['name']}"
    st["pot"] = 0
    st["winner"] = best_seat["id"] if best_seat else None
    st["winning_hand"] = best_hand
    db.update_scene(scene_id, status="done", phase="showdown", state=json.dumps(st))
    db.add_scene_event(scene_id, "result", winner_txt, billed=False)
    bus.emit(0, None, "system", "scene_result",
             {"scene": scene_id, "winner": (best_seat or {}).get("name")})
    return db.get_scene(scene_id)


# --- the manager who walks the room, and the whole-hand run -------------------

async def manager_brief(scene_id: int, settings: dict) -> int:
    """The manager visits each player and gives one bounded briefing — the walk the
    owner wants to watch. The walk itself is free (positioned-node movement the UI
    animates from the `brief` events); each visit is one capped call. Returns the
    number of briefings billed, which is exactly the player count."""
    mgr = next((s for s in db.list_scene_agents(scene_id, role="manager")), None)
    if not mgr:
        return 0
    scene = db.get_scene(scene_id)
    briefed = 0
    max_tokens = int(tuning.get("scene_brief_max_tokens"))
    for p in db.list_scene_agents(scene_id, role="player"):
        prompt = (f"You are the manager of this scene. Goal: {scene['goal'] or scene['kind']}. "
                  f"Walk over to {p['name']} and brief them in one short line.")
        scene = db.get_scene(scene_id)      # re-read: budget moves as we go
        text = await _utter(scene, mgr, prompt, settings, kind="brief", max_tokens=max_tokens)
        if text is None:
            break                            # budget reached mid-walk; stop cleanly
        briefed += 1
    return briefed


async def play_hand(scene_id: int, settings: dict) -> dict:
    """Run a full hand autonomously: deal if needed, then advance the turn order,
    one bounded call per acting player, until the betting round is done — then a free
    showdown. This is the match the agents play 'on their own': no human acts, the
    dealer (code) drives, the players (models) choose. O(turns) by construction."""
    scene = db.get_scene(scene_id)
    if not scene:
        return {}
    if scene["phase"] != "bet":
        scene = deal(scene_id)

    guard = 0
    limit = len(db.list_scene_agents(scene_id, role="player")) + 2
    while guard < limit:
        guard += 1
        seat = next_actor(scene_id)
        if not seat:
            break
        scene = db.get_scene(scene_id)
        if scene["status"] == "paused":
            return scene
        await act(scene_id, seat["id"], settings)

    if not next_actor(scene_id) and db.get_scene(scene_id)["status"] != "paused":
        return showdown(scene_id)
    return db.get_scene(scene_id)


async def run(scene_id: int, settings: dict) -> dict:
    """'Ask the manager to run it': the manager briefs the room, then the hand plays
    out. The whole thing is O(players) model calls — one brief and at most one action
    each — and bounded by the scene budget."""
    await manager_brief(scene_id, settings)
    return await play_hand(scene_id, settings)


async def talk(scene_id: int, seat_id: int, message: str, settings: dict) -> str | None:
    """Talk to an agent (or the manager) on the canvas: one bounded, in-character
    reply. The same turn-based, capped exchange as any other utterance — a
    conversation is just a scene with one speaker."""
    scene = db.get_scene(scene_id)
    seat = db.get_scene_agent(seat_id)
    if not scene or not seat:
        return None
    view = agent_view(scene_id, seat_id) if seat["role"] == "player" else public_view(scene_id)
    prompt = (f"Someone at the scene says to you: {message!r}\n\n"
              f"What you can see: {json.dumps(view)[:1500]}\n\nReply in character.")
    max_tokens = int(tuning.get("scene_utterance_max_tokens"))
    return await _utter(scene, seat, prompt, settings, kind="say", max_tokens=max_tokens)


def flip(artifact_id: int) -> dict | None:
    """Flip a card between face-down and face-up — a free effect, the owner's
    'cards can be flipped to reduce visibility' made literal. Only cards flip; a pot
    has no back."""
    art = db.get_artifact(artifact_id)
    if not art or art["type"] != "card":
        return art
    nv = "public" if art["visibility"] in ("facedown", "held") else "facedown"
    db.update_artifact(artifact_id, visibility=nv)
    db.add_scene_event(art["scene_id"], "flip",
                       f"a card turned {'face up' if nv == 'public' else 'face down'}",
                       billed=False)
    return db.get_artifact(artifact_id)


def _act_prompt(scene: dict, view: dict) -> str:
    hand = ", ".join(f"{c['rank']}{c['suit']}" for c in view.get("your_hand", []))
    board = ", ".join(f"{c['rank']}{c['suit']}" for c in _board(scene))
    return (f"Poker. Your hole cards: {hand or '(none)'}. The board: {board or '(none)'}. "
            f"Pot: {view.get('pot', 0)}, your chips: {view.get('you', {}).get('stack', 0)}. "
            f"Decide: fold, call, or raise.")


def _json(blob: Any) -> dict:
    if isinstance(blob, dict):
        return blob
    try:
        return json.loads(blob or "{}")
    except (ValueError, TypeError):
        return {}


def owns(owner_id: int, scene_id: int) -> bool:
    s = db.get_scene(scene_id)
    return bool(s and s["owner_id"] == owner_id)
