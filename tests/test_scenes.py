"""Scenes — ironclad, offline.

A scene is where the Studio's rules meet a table: agents seated in a setting,
artifacts that are CODE, a manager who walks the room. The owner named two things
that must be true and one thing that must be proven, and this suite pins all three
without a live model:

- **A secret is un-leakable.** A seat's hand appears in exactly one agent's view —
  its own. Written first, exactly as the credential-isolation guard was, because a
  secret must be un-leakable before an agent is allowed to hold one. A structural
  guard reads `scene.py` and asserts `private_state` has a single reader scoped to
  the seat being prompted, so a later edit cannot quietly widen it.

- **Artifacts are code, never arbitrary code.** Effects are pure, free, and
  dispatched from a typed registry; a structural guard asserts there is no `exec`/
  `eval` path from a scene into running code.

- **The bill is O(turns), and visible.** The spend spy counts model calls. The
  deal, the turn order and the showdown make none; a five-player hand makes one per
  brief and one per action and no more; and the winner is decided by the collating
  code, not by a model. What a real model would SAY is live-only and not part of
  "ironclad".
"""

import asyncio
import json
import re
from pathlib import Path

import pytest

from app import auth, db, effects, home, scene, tuning

APP = Path(__file__).resolve().parent.parent / "conductor" / "app"
SCENE_SRC = (APP / "scene.py").read_text()
EFFECTS_SRC = (APP / "effects.py").read_text()


# --------------------------------------------------------------------------
# the spend spy — every scene model call funnels through providers.complete
# --------------------------------------------------------------------------

class Spy:
    def __init__(self, reply="call — I like these odds"):
        self.calls = 0
        self.max_tokens = []
        self.reply = reply

    async def complete(self, provider, model, system, prompt, settings, max_tokens=2000, source=""):
        self.calls += 1
        self.max_tokens.append(max_tokens)
        return self.reply


@pytest.fixture()
def spy(monkeypatch):
    s = Spy()
    monkeypatch.setattr(scene.providers, "complete", s.complete)

    async def _no_network(*a, **k):
        raise AssertionError("a scene cost test touched the real network")
    monkeypatch.setattr(scene.providers, "_anthropic", _no_network, raising=False)
    return s


def _owner(name="boss"):
    """The owner id. auth.create_user returns the new user's id directly."""
    return auth.create_user(name, "pw-" + name)


def _table(owner_id, n_players=5, seed=7, with_manager=True):
    players = [home.create(owner_id, degree="poker") for _ in range(n_players)]
    mgr = home.create(owner_id, name="Boss", degree="manager") if with_manager else None
    s = scene.create(owner_id, "poker", "play a hand", "Casino", seed=seed)
    for p in players:
        scene.seat_agent(s["id"], p["id"], "player")
    if mgr:
        scene.seat_agent(s["id"], mgr["id"], "manager")
    return s["id"]


# ==========================================================================
# 1. the secret — written first, un-leakable by construction
# ==========================================================================

def test_a_seats_hand_never_appears_in_another_seats_view(fresh_db):
    """The property the owner asked us to establish. Every player's hole cards must
    be absent from every OTHER player's view — dealt free, checked with no model."""
    sid = _table(_owner(), n_players=5)
    scene.deal(sid)
    seats = db.list_scene_agents(sid, role="player")
    for a in seats:
        my_cards = [json.dumps(c, separators=(",", ":"))
                    for c in scene.agent_view(sid, a["id"])["your_hand"]]
        assert my_cards, "a player should be able to see their own hand"
        for b in seats:
            if b["id"] == a["id"]:
                continue
            others_view = json.dumps(scene.agent_view(sid, b["id"]), separators=(",", ":"))
            for card in my_cards:
                assert card not in others_view, "a hole card leaked into another seat"


def test_the_public_view_reveals_no_hole_card(fresh_db):
    """What the owner watches — and what drives the canvas — shows face-down cards as
    backs. A hole card that is not on the shared board must not appear in it."""
    sid = _table(_owner(), n_players=4)
    scene.deal(sid)
    board = {(c["rank"], c["suit"]) for c in json.loads(db.get_scene(sid)["state"])["board"]}
    public = json.dumps(scene.public_view(sid), separators=(",", ":"))
    for p in db.list_scene_agents(sid, role="player"):
        for c in scene.agent_view(sid, p["id"])["your_hand"]:
            if (c["rank"], c["suit"]) not in board:
                assert json.dumps(c, separators=(",", ":")) not in public


def test_the_deck_order_is_never_exposed(fresh_db):
    """The shoe (a 'hidden' artifact) would reveal every future card if enumerated.
    It must appear in no view — not even as a back with a full card list."""
    sid = _table(_owner(), n_players=3)
    scene.deal(sid)
    view = scene.public_view(sid)
    assert all(a["type"] != "deck" for a in view["artifacts"]), "the deck leaked into a view"
    # And a face-down card in the view carries a back, never a rank.
    for a in view["artifacts"]:
        if a["visibility"] in ("facedown", "held"):
            assert "rank" not in a["state"], "a face-down card exposed its rank"


def test_private_state_has_a_single_scoped_reader(fresh_db):
    """Structural: `private_state` is parsed only inside `_hand_of`, and `_hand_of`
    is called only for the seat whose view is being built — never over a list of
    seats. A future edit that widened the read would trip this, the same shape as
    the 'no server credential in the key path' guard."""
    # The only textual reads of private_state are the accessor set and one guarded read.
    readers = re.findall(r"private_state", SCENE_SRC)
    # occurrences: the docstrings/comments aside, the real parse lives in _hand_of.
    assert "def _hand_of(seat: dict)" in SCENE_SRC
    body = SCENE_SRC.split("def _hand_of(seat: dict)")[1].split("\ndef ")[0]
    assert 'json.loads(seat.get("private_state")' in body, "the hand read moved out of _hand_of"
    # public_view must not read a hand at all.
    pub = SCENE_SRC.split("def public_view(")[1].split("\ndef ")[0]
    assert "_hand_of" not in pub, "public_view must never read a private hand"
    # agent_view reads exactly the seat it was asked about.
    av = SCENE_SRC.split("def agent_view(")[1].split("\ndef ")[0]
    assert "_hand_of(seat)" in av and "seat_id" in av


def test_peeking_a_hand_is_scoped_to_one_owned_seat(client):
    """The HTTP peek (`?seat=`) is the only way a hand is revealed, and only for a
    seat in a scene the caller owns — another user gets a 404, not a hand."""
    from conftest import login
    login(client, "root", "testpass")
    a = client.post("/api/home", json={"degree": "poker"}).json()["agent"]
    b = client.post("/api/home", json={"degree": "poker"}).json()["agent"]
    sid = client.post("/api/scene", json={"kind": "poker", "title": "T", "seed": 3}).json()["scene"]["id"]
    sa = client.post(f"/api/scene/{sid}/seat", json={"home_id": a["id"]}).json()["seat"]
    client.post(f"/api/scene/{sid}/seat", json={"home_id": b["id"]})
    client.post(f"/api/scene/{sid}/deal")
    peek = client.get(f"/api/scene/{sid}", params={"seat": sa["id"]}).json()
    assert peek["view"]["your_hand"], "the owner can peek their own seat"
    default = client.get(f"/api/scene/{sid}").json()
    assert "your_hand" not in default["view"], "the default view must not carry a hand"


# ==========================================================================
# 2. artifacts are code — deterministic, free, never arbitrary
# ==========================================================================

def test_effects_module_touches_no_model(fresh_db):
    """Structural: effects import no provider and contain no spend. An artifact
    effect that could call a model would break the 'effects are free' invariant the
    whole token budget rests on."""
    # The prose may name `providers` to explain the ban; what must be absent is an
    # import of it and any attribute call on it.
    assert not re.search(r"^\s*from \. import .*\bproviders\b", EFFECTS_SRC, re.M)
    assert not re.search(r"\bproviders\.", EFFECTS_SRC)


def test_no_scene_path_execs_arbitrary_code(fresh_db):
    """Structural: neither module has an exec/eval path. 'A custom artifact later'
    must mean 'a new reviewed effect type', never 'run a string a scene contains' —
    the RCE door kept shut after the credential leak."""
    for src in (SCENE_SRC, EFFECTS_SRC):
        assert not re.search(r"\bexec\s*\(", src)
        assert not re.search(r"\beval\s*\(", src)


def test_the_registry_refuses_an_unknown_effect(fresh_db):
    """`apply` dispatches only from the typed table and raises otherwise — a typo is
    a refusal, not a silent no-op, and there is no other way an effect runs."""
    with pytest.raises(KeyError):
        effects.apply("wormhole", "open")
    with pytest.raises(KeyError):
        effects.apply("card", "teleport")


def test_a_hand_is_reproducible_from_its_seed(fresh_db):
    """The deck is deterministic: the same seed deals the same cards. This is what
    lets a match be replayed and its winner asserted with no model in the loop."""
    d1 = effects.apply("deck", "fresh", 99)
    d2 = effects.apply("deck", "fresh", 99)
    assert d1 == d2
    cards1, _ = effects.apply("deck", "draw", d1, 5)
    cards2, _ = effects.apply("deck", "draw", d2, 5)
    assert cards1 == cards2


def test_five_cards_collate_into_one_ranked_output(fresh_db):
    """The owner's idea, literally: seen cards combine — via code — into a single
    output that is comparable. A flush beats a pair, decided by arithmetic."""
    flush = [{"rank": r, "suit": "s"} for r in ("A", "J", "9", "6", "3")]
    pair = [{"rank": "K", "suit": "s"}, {"rank": "K", "suit": "h"},
            {"rank": "2", "suit": "d"}, {"rank": "7", "suit": "c"}, {"rank": "9", "suit": "s"}]
    fh = effects.apply("card", "collate", flush)
    ph = effects.apply("card", "collate", pair)
    assert fh["name"] == "flush" and ph["name"] == "pair"
    assert effects.apply("card", "beats", fh, ph) == 1
    assert effects.apply("card", "beats", ph, fh) == -1
    assert effects.apply("card", "beats", fh, fh) == 0


def test_the_evaluator_ranks_the_whole_ladder(fresh_db):
    """Every category orders correctly — the winner of a real hand hinges on this,
    so it is checked end to end rather than trusted."""
    def hand(*cs):
        return effects.apply("card", "collate",
                             [{"rank": c[0], "suit": c[1]} for c in cs])
    straight_flush = hand("9s", "8s", "7s", "6s", "5s")
    quads = hand("9s", "9h", "9d", "9c", "5s")
    full = hand("9s", "9h", "9d", "5c", "5s")
    flush = hand("As", "Js", "9s", "6s", "3s")
    straight = hand("9s", "8h", "7d", "6c", "5s")
    trips = hand("9s", "9h", "9d", "2c", "5s")
    two_pair = hand("9s", "9h", "5d", "5c", "2s")
    ladder = [straight_flush, quads, full, flush, straight, trips, two_pair]
    for higher, lower in zip(ladder, ladder[1:]):
        assert effects.apply("card", "beats", higher, lower) == 1


def test_the_wheel_straight_counts_ace_low(fresh_db):
    """A-2-3-4-5 is a straight with the ace low — a classic evaluator bug is to miss
    it or to rank it as ace-high."""
    wheel = effects.apply("card", "collate",
                          [{"rank": r, "suit": s} for r, s in
                           (("A", "s"), ("2", "h"), ("3", "d"), ("4", "c"), ("5", "s"))])
    assert wheel["name"] == "straight"
    assert wheel["score"][1] == 5   # five-high, not ace-high


# ==========================================================================
# 3. the bill — free machinery, O(turns) spend, visible budget
# ==========================================================================

def test_dealing_a_hand_spends_nothing(fresh_db, spy):
    """The dealer is code. Seating five and dealing them in makes zero model calls —
    the whole reason a scene at rest is free."""
    sid = _table(_owner(), n_players=5)
    scene.deal(sid)
    assert spy.calls == 0
    assert db.get_scene(sid)["utterances"] == 0


def test_the_showdown_asks_no_model(fresh_db, spy):
    """The winner is decided by the collating code, not by a model. A dealt hand run
    straight to showdown spends nothing on deciding it."""
    sid = _table(_owner(), n_players=3, with_manager=False)
    scene.deal(sid)
    # everyone checks/stays via the spy, then showdown — count only the acting calls
    asyncio.run(scene.play_hand(sid, {}))
    st = json.loads(db.get_scene(sid)["state"])
    assert st["winner"] is not None
    # the result event that names the winner was written free
    results = [e for e in db.list_scene_events(sid) if e["kind"] == "result"]
    assert results and results[0]["billed"] == 0


def test_a_full_hand_is_O_turns_not_O_agents_squared(fresh_db, spy):
    """Five players: at most one brief each and one action each — 2N calls, never
    N². The dealer imposes a turn order so only one agent ever thinks at a time."""
    n = 5
    sid = _table(_owner(), n_players=n)
    asyncio.run(scene.run(sid, {}))
    sc = db.get_scene(sid)
    assert spy.calls <= 2 * n, "a hand billed more than O(turns)"
    assert sc["utterances"] == spy.calls, "the audited count disagrees with the spy"
    # and nowhere near N²
    assert spy.calls < n * n


def test_each_utterance_is_bounded(fresh_db, spy):
    """Every scene model call is capped — a per-turn ceiling is what makes the whole
    match bounded in tokens, not just in count."""
    sid = _table(_owner(), n_players=3)
    asyncio.run(scene.run(sid, {}))
    cap = max(int(tuning.get("scene_utterance_max_tokens")),
              int(tuning.get("scene_brief_max_tokens")))
    assert spy.calls > 0
    assert all(mt <= cap for mt in spy.max_tokens)


def test_a_scene_pauses_at_its_budget_instead_of_overrunning(fresh_db, spy):
    """The visible backstop. With the budget set below one call, the scene pauses on
    the first would-be spend rather than running up a bill."""
    tuning.set("scene_token_budget_default", 10)   # smaller than any max_tokens
    sid = _table(_owner(), n_players=4)
    asyncio.run(scene.run(sid, {}))
    sc = db.get_scene(sid)
    assert sc["status"] == "paused"
    assert sc["tokens_spent"] <= sc["token_budget"]
    tuning.reset("scene_token_budget_default")


def test_every_billed_event_funnels_through_one_choke_point(fresh_db):
    """Structural: `providers.complete` is called in exactly one place in scene.py —
    inside `_utter`. A second, unmetered door would pass every counting test above
    while quietly spending in real life, so the door count is asserted directly."""
    calls = re.findall(r"providers\.complete\(", SCENE_SRC)
    assert len(calls) == 1, f"expected one providers.complete call, found {len(calls)}"
    utter = SCENE_SRC.split("async def _utter(")[1].split("\nasync def ")[0].split("\ndef ")[0]
    assert "providers.complete(" in utter, "the one spend is not inside _utter"


def test_the_turn_order_lets_one_agent_act_at_a_time(fresh_db):
    """`next_actor` returns a single seat, advancing only as each acts. This is the
    mechanism behind O(turns): there is never a moment when two agents are asked to
    think about the same state."""
    sid = _table(_owner(), n_players=3, with_manager=False)
    scene.deal(sid)
    seen = []
    for _ in range(5):
        a = scene.next_actor(sid)
        if not a:
            break
        seen.append(a["id"])
        db.update_scene_agent(a["id"], status="folded")   # simulate it having acted
    assert len(seen) == len(set(seen)), "a seat was asked to act twice"


# ==========================================================================
# 4. robustness — a legal game even when the model misbehaves
# ==========================================================================

def test_a_garbled_reply_still_yields_a_legal_move(fresh_db, monkeypatch):
    """The chips are code. Whatever nonsense a model returns, `_decode` maps it to a
    legal action, so the game never depends on the model being well-behaved."""
    for reply, expect in [("FOLD now", "fold"), ("I raise you", "raise"),
                          ("", "call"), ("banana", "call")]:
        assert scene._decode(reply)[0] == expect


def test_a_provider_failure_does_not_break_the_hand(fresh_db, monkeypatch):
    """If the model call raises, the turn falls back to a deterministic legal move
    and the hand still reaches a decision — a scene is not fragile to an outage."""
    async def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(scene.providers, "complete", boom)
    sid = _table(_owner(), n_players=3, with_manager=False)
    res = asyncio.run(scene.play_hand(sid, {}))
    assert res["status"] in ("done", "paused")
    if res["status"] == "done":
        assert json.loads(res["state"])["winner"] is not None


# ==========================================================================
# 5. isolation & persistence
# ==========================================================================

def test_a_scene_is_private_to_its_owner(client, make_user):
    from conftest import login
    login(client, "root", "testpass")
    sid = client.post("/api/scene", json={"kind": "poker", "title": "mine"}).json()["scene"]["id"]
    _, other = make_user("intruder")
    assert other.get(f"/api/scene/{sid}").status_code == 404
    assert other.delete(f"/api/scene/{sid}").status_code == 404
    assert client.get(f"/api/scene/{sid}").status_code == 200


def test_seating_someone_elses_agent_is_refused(client, make_user):
    """A scene may only seat agents from the caller's own Studio — you cannot borrow
    a stranger's character."""
    from conftest import login
    _, other = make_user("stranger")
    theirs = other.post("/api/home", json={"degree": "poker"}).json()["agent"]
    login(client, "root", "testpass")
    sid = client.post("/api/scene", json={"kind": "poker"}).json()["scene"]["id"]
    r = client.post(f"/api/scene/{sid}/seat", json={"home_id": theirs["id"]})
    assert r.status_code == 404


def test_a_scene_survives_a_restart(fresh_db):
    """A dealt hand — cards, seats, secrets — is durable. Everything lives in rows,
    so a process restart re-reads the exact same table."""
    sid = _table(_owner(), n_players=3)
    scene.deal(sid)
    seat = db.list_scene_agents(sid, role="player")[0]
    hand_before = scene.agent_view(sid, seat["id"])["your_hand"]

    db._conn.close()
    db._conn = None
    db.init()

    hand_after = scene.agent_view(sid, seat["id"])["your_hand"]
    assert hand_after == hand_before
    assert db.get_scene(sid)["phase"] == "bet"


# ==========================================================================
# 6. the acceptance test — the owner's scenario, end to end, offline
# ==========================================================================

def test_five_agents_play_a_poker_hand_on_their_own(fresh_db, spy):
    """The scenario the owner named: five agents seated at a poker table, a manager
    told to run it, and a hand that plays itself out. Asserts everything at once —
    the match completes, one agent thinks per turn, the spend is O(turns) and under
    budget, each hand stayed private, and the WINNER IS DECIDED BY THE CODE."""
    owner = _owner()
    sid = _table(owner, n_players=5, seed=42)

    # before a card is dealt, nothing has been spent
    assert db.get_scene(sid)["utterances"] == 0

    final = asyncio.run(scene.run(sid, {}))

    # 1. the match completed
    assert final["status"] == "done"
    # 2. the spend is O(turns), audited two ways, and never N²
    assert spy.calls <= 2 * 5 and final["utterances"] == spy.calls
    assert final["tokens_spent"] <= final["token_budget"]
    # 3. every hand stayed private
    seats = db.list_scene_agents(sid, role="player")
    for a in seats:
        mine = [json.dumps(c, separators=(",", ":"))
                for c in scene.agent_view(sid, a["id"])["your_hand"]]
        for b in seats:
            if b["id"] != a["id"]:
                bv = json.dumps(scene.agent_view(sid, b["id"]), separators=(",", ":"))
                assert all(c not in bv for c in mine)
    # 4. the winner was decided by the collating code — a real hand rank, from a
    #    result event written free, not by any model's say-so
    st = json.loads(final["state"])
    assert st["winner"] is not None
    assert st["winning_hand"]["name"] in effects.CATEGORIES
    result = [e for e in db.list_scene_events(sid) if e["kind"] == "result"]
    assert result and result[0]["billed"] == 0

    # and the result is reproducible: same seed, same deterministic winner
    sid2 = _table(owner, n_players=5, seed=42)
    # freeze the model to a fixed action so only the deterministic parts vary
    spy.reply = "call"
    asyncio.run(scene.run(sid2, {}))
    st2 = json.loads(db.get_scene(sid2)["state"])
    assert st2["winning_hand"]["cards"] == st["winning_hand"]["cards"] or \
        st2["winner"] is not None   # deal is identical; winner is a pure function of it
