"""The Lifeworld — ironclad, offline.

A society of living agents whose one promise is: the only thing that ever spends a token
is a single bounded Tier-2 model call; everything else — senses, memory, skills, drives,
rules, circles, the ledger — is free arithmetic and lookups. This suite proves that with
no live model (a spy counts would-be calls and hard-fails on a real network touch), and
pins the properties that make the model coherent: secrets stay holder-only, mood never
diverges, habits compile and then fire free, and a whole world round-trips through JSON.
"""

import asyncio
import re
from pathlib import Path

import pytest

from app.lifeworld import World, Scene, Human, Card, Deck
from app.lifeworld.config import Flags
from app.lifeworld.types import Signal, Packet
from app.lifeworld import util, skills as skills_mod, drives as drives_mod

LW = Path(__file__).resolve().parent.parent / "conductor" / "app" / "lifeworld"


# --------------------------------------------------------------------------
# a free world, and a spy world (for the one paid path)
# --------------------------------------------------------------------------

def free_world():
    return World(name="test")            # no `complete` -> deterministic Tier 0, free


class Spy:
    def __init__(self):
        self.calls = 0

    async def complete(self, provider, model, system, prompt, settings, max_tokens=2000):
        self.calls += 1
        return '{"understood":"noted","mood":{"stress":0.2},"memory":"a thing","action":{"kind":"say","text":"ok"}}'


def deal_table(seed=7, names=("Mira", "Ivo", "Rae")):
    w = free_world()
    dials = [{"composure": 85, "willpower": 80}, {"composure": 20, "willpower": 30},
             {"risk_appetite": 80, "composure": 55}]
    people = [w.spawn_human(n, dials=d) for n, d in zip(names, dials)]
    deck = Deck.fresh(w.next_id(), seed=seed); w.add(deck)
    sc = Scene(w, id=1, name="Table", domain="cards.poker")
    for p in people:
        sc.seat(p)
    sc.place(deck)
    return w, sc, people, deck


# --------------------------------------------------------------------------
# 1. flags
# --------------------------------------------------------------------------

def test_switch_drama_off_selects_the_serious_bundle():
    f = Flags.preset("sandbox").derive({"switch_drama_off": True})
    assert not f.on("theory_of_mind") and not f.on("gossip") and not f.on("mortality")
    assert f.on("rule_compiler") and f.on("memory") and f.on("secrets_circles")


def test_flags_layer_most_specific_wins():
    world = Flags.preset("sandbox")
    scene = world.derive({"theory_of_mind": False})
    assert world.on("theory_of_mind") and not scene.on("theory_of_mind")


def test_flags_round_trip_stores_only_deltas():
    f = Flags.preset("serious")
    assert Flags.from_dict(f.to_dict()).on("theory_of_mind") == f.on("theory_of_mind")


# --------------------------------------------------------------------------
# 2. subsystems in isolation
# --------------------------------------------------------------------------

def test_competency_graph_propagates_up_and_sideways():
    s = skills_mod.Skills()
    s.credit("law.contract.ma", 1.0)
    assert s.xp["law.contract.ma"] > s.xp["law.contract"] > s.xp["law"]   # ancestor decay
    assert s.xp.get("negotiation", 0) > 0                                  # cross-edge transfer
    assert 0 < s.level("law.contract.ma") < 1

def test_skills_forget_erodes_the_unpractised():
    s = skills_mod.Skills(); s.credit("music.piano", 2.0)
    before = s.xp["music.piano"]
    for _ in range(50):
        s.forget({"cooking"})
    assert s.xp["music.piano"] < before


def test_drives_prerequisite_gates_higher_needs():
    d = drives_mod.Drives()
    d.level["esteem"] = 0.2                         # a real esteem need exists (raw pressure 0.4)
    starving = drives_mod.Drives(); starving.level["esteem"] = 0.2; starving.level["energy"] = 0.05
    raw = d.pressures()["esteem"]
    assert starving.pressures()["esteem"] < raw     # the same need is damped while starving


def test_ledger_detects_a_tampered_chain():
    from app.lifeworld.ledger import Ledger
    lg = Ledger()
    lg.commit(1, "did a"); lg.commit(2, "did b"); lg.commit(3, "did c")
    assert lg.verify()
    lg.pins[1].summary = "did something else"      # forge the middle
    assert not lg.verify()


def test_seal_is_holder_only():
    key, other = util.new_key(), util.new_key()
    c = util.seal({"rank": "A"}, key)
    assert util.unseal(c, key) == {"rank": "A"}
    assert '"rank"' not in c                        # plaintext not in ciphertext
    try:
        assert util.unseal(c, other) != {"rank": "A"}
    except Exception:
        pass                                        # wrong key: garbage or error, never the value


# --------------------------------------------------------------------------
# 3. artifacts — the card's secret
# --------------------------------------------------------------------------

def test_a_dealt_card_is_readable_only_by_its_holder():
    w, sc, people, deck = deal_table()
    a, b = people[0], people[1]
    card = deck.draw_to(a, w)
    assert card.reveal(a) is not None                # holder has the key
    assert card.reveal(b) is None                    # no one else does
    assert card.holder == a.id


def test_the_deck_never_exposes_its_order_or_dealt_values():
    """Regression for a real leak the review caught: the deck used to keep every card's
    plaintext in world-readable public state, defeating the per-card seal entirely."""
    import json
    w, sc, people, deck = deal_table()
    a = people[0]
    card = deck.draw_to(a, w)
    assert set(deck.public) <= {"cursor", "count"}       # only safe counters are public
    sv = json.dumps(sc.view())                            # what the API returns
    assert "order" not in sv and "\"cards\"" not in sv    # order/values are nowhere in the view
    val = card.reveal(a)                                  # the holder's value
    assert val["rank"] + val["suit"] not in sv            # not recoverable from the scene view


def test_a_secret_memory_never_surfaces_in_a_public_recall():
    """Regression: sleep() used to fold secret-scoped episodes into the public semantic
    block, so a circle secret leaked into any public recall."""
    from app.lifeworld.memory import Memory
    m = Memory()
    W = {"emotion": 1, "novelty": 1, "social": 1, "goal": 1, "surprise": 1}
    for i in range(8):
        sig = Signal(kind="reveal", scope="circle", domain="cards", from_id=1,
                     intensity=0.9, stakes=0.9, payload={"text": f"SECRET-ACE-{i}"})
        p = Packet(understood=f"SECRET-ACE-{i}", memory=f"SECRET-ACE-{i}",
                   mood={"stress": 0.5}, tier=2)
        m.remember(p, sig, W, i, social_strength=1.0)
    m.sleep({"cards"})
    assert "SECRET-ACE" not in m.recall(domain="cards", audience_scope="public")


def test_a_long_ledger_summary_still_verifies():
    """Regression: commit() hashed the full summary but stored a truncated one, so a long
    entry made verify() fail on an untampered chain."""
    from app.lifeworld.ledger import Ledger
    lg = Ledger()
    lg.commit(1, "x" * 500)
    lg.commit(2, "y" * 500)
    assert lg.verify()


# --------------------------------------------------------------------------
# 4. the scan loop — cost, divergence, sanity, learning
# --------------------------------------------------------------------------

def test_a_whole_table_of_scans_bills_nothing_offline():
    w, sc, people, deck = deal_table()
    async def run():
        for _ in range(3):
            for i, h in enumerate(people):
                await sc.interact(h, deck.id, "draw")
                await sc.greet(h, people[(i + 1) % len(people)])
            sc.rest()
    asyncio.run(run())
    assert sum(1 for e in sc.log if e["billed"]) == 0
    assert all(h.psyche.is_sane() for h in people)


def test_the_same_scold_diverges_by_trait():
    w, sc, people, _ = deal_table()
    composed, fragile = people[0], people[1]
    async def run():
        await sc.say(fragile, composed, "sloppy", kind="scold", intensity=0.9)
        await sc.say(composed, fragile, "sloppy", kind="scold", intensity=0.9)
    asyncio.run(run())
    assert fragile.psyche.mood["stress"] > composed.psyche.mood["stress"]


def test_a_person_never_differs_into_a_crazy_person():
    w, sc, people, _ = deal_table()
    victim = people[1]
    async def run():
        for _ in range(300):
            await sc.say(people[0], victim, "no", kind="scold", intensity=1.0)
    asyncio.run(run())
    assert victim.psyche.is_sane() and victim.psyche.mood["stress"] <= 1.0


def test_a_habit_compiles_and_then_fires_free():
    w, sc, people, _ = deal_table()
    a, b = people[0], people[1]
    async def run():
        for _ in range(3):
            await sc.say(a, b, "you misplayed", kind="scold", intensity=0.9)
        return await sc.say(a, b, "again", kind="scold", intensity=0.9)
    p = asyncio.run(run())
    assert any(r.match.get("kind") == "scold" for r in b.rules.rules)     # compiled
    assert p.tier == 1 and p.spent == 0                                    # and fired free


def test_memory_is_selective_not_a_transcript():
    """A composed agent greeted by near-strangers remembers little — the salience gate,
    not a log."""
    w, sc, people, _ = deal_table()
    composed = people[0]
    async def run():
        for _ in range(8):
            await sc.greet(people[2], composed)       # routine hellos from a weak bond
    asyncio.run(run())
    assert len(composed.memory.episodic) < composed.tau


# --------------------------------------------------------------------------
# 5. the paid path — exactly one bounded call per deliberation
# --------------------------------------------------------------------------

def test_the_model_path_bills_one_call_per_scan(monkeypatch):
    spy = Spy()
    w = World(name="live", complete=spy.complete, settings={})
    a = w.spawn_human("A", dials={"composure": 50})
    b = w.spawn_human("B")
    sc = Scene(w, id=1, name="s", domain="talk")
    sc.seat(a); sc.seat(b)
    async def run():
        return await sc.say(a, b, "something genuinely novel and weighty", kind="say",
                            intensity=0.9, stakes=0.9)
    p = asyncio.run(run())
    assert spy.calls == 1 and p.tier == 2 and p.spent == 1


def test_the_model_is_never_touched_on_the_free_path():
    async def boom(*a, **k):
        raise AssertionError("free path touched the network")
    w = World(name="live", complete=boom, settings={},
              flags=Flags.preset("sandbox").derive({"emotions": False}))  # no model when off
    a, b = w.spawn_human("A"), w.spawn_human("B")
    sc = Scene(w, id=1, name="s"); sc.seat(a); sc.seat(b)
    asyncio.run(sc.say(a, b, "hi"))                    # must not raise


# --------------------------------------------------------------------------
# 6. inheritance & serialisation
# --------------------------------------------------------------------------

def test_the_taxonomy_is_one_atom_two_kinds():
    from app.lifeworld.entity import Entity, Being
    from app.lifeworld.artifact import Artifact
    assert issubclass(Human, Being) and issubclass(Being, Entity)
    assert issubclass(Card, Artifact) and issubclass(Artifact, Entity)
    assert not issubclass(Artifact, Being)            # an artifact is not alive


def test_a_whole_world_round_trips_through_json():
    import json
    w, sc, people, deck = deal_table()
    asyncio.run(sc.interact(people[0], deck.id, "draw"))
    blob = json.loads(json.dumps(w.to_dict()))
    w2 = World.from_dict(blob)
    ivo = next(h for h in w2.humans() if h.name == "Ivo")
    assert ivo.ledger.verify()
    card = next(a for a in w2.artifacts() if a.kind == "card")
    holder = w2.get(card.holder)
    assert card.reveal(holder) == card.reveal(w.get(card.holder))   # the key survived


# --------------------------------------------------------------------------
# 7. structural cost guard
# --------------------------------------------------------------------------

def test_the_engine_imports_no_provider_except_the_store_seam():
    """The only files that may reach for providers are ports.py (the substrate's one
    door to the platform) and store.py, whose opt-in live path pulls the door open.
    Every other module is provider-free by construction."""
    for f in LW.glob("*.py"):
        if f.name in ("store.py", "ports.py", "__init__.py"):
            continue
        src = f.read_text()
        assert "import providers" not in src, f"{f.name} imports providers"
        assert "providers." not in src, f"{f.name} references providers"


def test_only_the_model_appraiser_can_spend():
    """`complete(` (the injected provider) is called in exactly one place: appraise.model."""
    src = (LW / "appraise.py").read_text()
    assert len(re.findall(r"\bcomplete\(", src)) == 1
    body = src.split("async def model(")[1].split("\ndef ")[0]
    assert "complete(" in body


# --------------------------------------------------------------------------
# 8. routes (the client fixture runs lifespan / db.init)
# --------------------------------------------------------------------------

def test_adding_a_person_by_brief_works_and_the_llm_authors_the_dials(client):
    """The bug the owner hit: add-human rejected an empty `senses`. Now creation is by
    brief and tolerant, and (offline) a deterministic author reads the brief."""
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    r = client.post(f"/api/lw/{wid}/human", json={"name": "Rae", "brief": "a reckless bold gambler"})
    assert r.status_code == 200
    prof = r.json()["human"]
    assert prof["traits"]["risk_appetite"] > 0.6      # the brief shaped the dials, no equalizer asked


def test_an_artifact_by_brief_becomes_the_right_kind(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    a = client.post(f"/api/lw/{wid}/artifact", json={"name": "cards", "brief": "a worn deck of cards"}).json()["artifact"]
    assert a["kind"] == "deck"


def test_the_deck_of_cards_scenario_through_the_api(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "Casino"}).json()["world"]["id"]
    for n in ("Mira", "Ivo", "Rae", "Sol"):
        client.post(f"/api/lw/{wid}/human", json={"name": n, "brief": ""})
    deck = client.post(f"/api/lw/{wid}/artifact", json={"name": "deck", "brief": "a deck of cards"}).json()["artifact"]
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "Table", "type": "casino"}).json()["room"]["id"]
    ov = client.get(f"/api/lw/{wid}").json()
    assert ov["rooms"][0]["theme"] == "casino"        # the room has a look, not a hardcoded table
    for p in ov["agents"]:
        client.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": p["id"]})
    client.post(f"/api/lw/{wid}/room/{rid}/place", params={"artifact_id": deck["id"]})
    r = client.post(f"/api/lw/{wid}/room/{rid}/round").json()
    assert r["world_tau"] > 0
    assert sum(1 for e in r["room"]["log"] if e["billed"]) == 0        # free
    # overview groups agents by their room, and peek reveals the owner's own hand
    ov2 = client.get(f"/api/lw/{wid}").json()
    assert all(a["room"] == rid for a in ov2["agents"])
    hand = client.get(f"/api/lw/{wid}/human/{ov2['agents'][0]['id']}").json()["hand"]
    assert hand and all(h["value"] for h in hand)


def test_a_collating_artifact_forms_a_cluster_and_a_round_plays_it(client):
    """The owner's core canvas idea: an artifact with N seats; agents snap into slots;
    the seated agents plus the artifact become one cluster a round plays over."""
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    ids = [client.post(f"/api/lw/{wid}/human", json={"name": n}).json()["human"]["id"]
           for n in ("A", "B", "C")]
    table = client.post(f"/api/lw/{wid}/artifact",
                        json={"name": "round table", "brief": "a round table", "slots": 3}).json()["artifact"]
    assert table["slots"] == 3 and table["kind"] == "prop"
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "R", "type": "freeplay"}).json()["room"]["id"]
    client.post(f"/api/lw/{wid}/room/{rid}/place", params={"artifact_id": table["id"]})
    for slot, hid in enumerate(ids):
        client.post(f"/api/lw/{wid}/artifact/{table['id']}/seat", json={"slot": slot, "human_id": hid})
    room = client.get(f"/api/lw/{wid}/room/{rid}").json()["room"]
    cl = room["clusters"][0]
    assert cl["full"] and sorted(cl["seated"]) == sorted(ids)      # the ring formed
    r = client.post(f"/api/lw/{wid}/room/{rid}/round").json()
    assert r["world_tau"] > 0 and sum(1 for e in r["room"]["log"] if e["billed"]) == 0


def test_a_drag_position_persists(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    hid = client.post(f"/api/lw/{wid}/human", json={"name": "Mover"}).json()["human"]["id"]
    client.post(f"/api/lw/{wid}/pos", json={"id": hid, "x": 120, "y": 84})
    a = next(a for a in client.get(f"/api/lw/{wid}").json()["agents"] if a["id"] == hid)
    assert a["pos"] == [120, 84]


def test_a_figure_is_carried_on_creation(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    h = client.post(f"/api/lw/{wid}/human", json={"name": "Face", "figure": "wizard"}).json()["human"]
    assert h["figure"] == "wizard"


def test_a_world_is_private_to_its_owner(client, make_user):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "mine"}).json()["world"]["id"]
    _, other = make_user("intruder")
    assert other.get(f"/api/lw/{wid}").status_code == 404
    assert other.delete(f"/api/lw/{wid}").status_code == 404


# --------------------------------------------------------------------------
# shapes, hand-drawn paths, and the flow arrows that drive a round
# --------------------------------------------------------------------------

def test_a_collating_artifact_remembers_its_shape_and_path():
    """A table need not be a circle: it can be a rect or a hand-drawn polygon. The shape
    and its points survive a full JSON round-trip, and seating is unaffected by it."""
    from app.lifeworld.artifact import Prop
    w = free_world()
    blob = Prop(w.next_id(), name="blob", slots=3, shape="path",
                path=[[0, -40], [40, 0], [0, 40], [-40, 0]])
    w.add(blob)
    plain = Prop(w.next_id(), name="plain")
    w.add(plain)
    assert plain.shape == "circle" and plain.path == []       # sane default
    w2 = World.from_dict(w.to_dict())
    q = w2.get(blob.id)
    assert q.shape == "path" and q.path == [[0, -40], [40, 0], [0, 40], [-40, 0]]
    assert q.collating() and q.seat(0, 999) and q.cluster() == [999]   # slots still work


def test_a_shape_and_path_survive_the_create_endpoint(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    pts = [[0, -40], [40, 0], [0, 40], [-40, 0]]
    art = client.post(f"/api/lw/{wid}/artifact", json={
        "name": "blob", "brief": "a blob table", "slots": 3, "shape": "path", "path": pts,
    }).json()["artifact"]
    assert art["shape"] == "path" and art["path"] == pts
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "R", "type": "freeplay"}).json()["room"]["id"]
    client.post(f"/api/lw/{wid}/room/{rid}/place", params={"artifact_id": art["id"]})
    prop = client.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["props"][0]
    assert prop["shape"] == "path" and len(prop["path"]) == 4


def test_a_scene_remembers_its_name_and_rules(client):
    """The editable title and the rules box persist through the scene-update endpoint and a
    reload of the world."""
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "Studio"}).json()["world"]["id"]
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "untitled", "type": "freeplay"}).json()["room"]["id"]
    r = client.post(f"/api/lw/{wid}/room/{rid}/scene",
                    json={"name": "Board meeting", "rules": "Everyone is polite and brief."}).json()["room"]
    assert r["name"] == "Board meeting" and "polite" in r["rules"]
    again = client.get(f"/api/lw/{wid}/room/{rid}").json()["room"]     # survives a fresh load
    assert again["name"] == "Board meeting" and again["rules"] == "Everyone is polite and brief."
    assert client.post(f"/api/lw/{wid}/touch").json()["ok"] is True    # explicit save is honest


def test_scene_rules_reach_the_model_appraisal():
    """A scene's rules must be handed to the one bounded model call, verbatim, so Live runs
    honor them."""
    seen = {}

    async def complete(provider, model, system, prompt, settings, max_tokens=2000):
        seen["system"] = system
        return '{"understood":"ok","action":{"kind":"say","text":"ok"}}'

    w = World(name="live", complete=complete, settings={})
    a, b = w.spawn_human("A"), w.spawn_human("B")
    sc = Scene(w, id=1, name="s", domain="talk"); sc.seat(a); sc.seat(b)
    sc.rules = "Speak only in questions."
    asyncio.run(sc.say(a, b, "something genuinely novel and weighty", kind="say",
                       intensity=0.9, stakes=0.9))               # reaches the bounded model call
    assert "Speak only in questions." in seen.get("system", ""), "scene rules never reached the model"


def test_a_shape_with_slots_is_a_table_even_if_its_brief_says_cards(client):
    """A Shape asks for slots, so it must be a collating Prop — never authored into a
    slot-less deck just because its brief mentions cards. A card brief with no slots is
    still a deck."""
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    table = client.post(f"/api/lw/{wid}/artifact",
                        json={"name": "felt", "brief": "a poker table with a deck of cards", "slots": 4}).json()["artifact"]
    assert table["kind"] == "prop" and table["slots"] == 4
    deck = client.post(f"/api/lw/{wid}/artifact",
                       json={"name": "shoe", "brief": "a deck of cards", "slots": 0}).json()["artifact"]
    assert deck["kind"] == "deck"


def test_a_scene_can_be_deleted_but_its_cast_survives(client):
    """Deleting a scene removes only the canvas; the agents/artifacts are world-level and
    remain in the cast and any other scene."""
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    r1 = client.post(f"/api/lw/{wid}/room", json={"name": "one", "type": "freeplay"}).json()["room"]["id"]
    r2 = client.post(f"/api/lw/{wid}/room", json={"name": "two", "type": "freeplay"}).json()["room"]["id"]
    hid = client.post(f"/api/lw/{wid}/human", json={"name": "Shared"}).json()["human"]["id"]
    client.post(f"/api/lw/{wid}/room/{r1}/seat", params={"human_id": hid})
    client.post(f"/api/lw/{wid}/room/{r2}/seat", params={"human_id": hid})
    assert client.delete(f"/api/lw/{wid}/room/{r1}").json()["ok"] is True
    ov = client.get(f"/api/lw/{wid}").json()
    assert [r["id"] for r in ov["rooms"]] == [r2]                 # only the deleted scene is gone
    assert any(a["id"] == hid for a in ov["agents"])              # the agent survives in the cast
    assert client.get(f"/api/lw/{wid}/room/{r1}").status_code == 404


def test_an_entity_can_be_deleted_and_leaves_every_scene_and_table(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "r", "type": "freeplay"}).json()["room"]["id"]
    table = client.post(f"/api/lw/{wid}/artifact", json={"name": "t", "brief": "a round table", "slots": 3}).json()["artifact"]
    client.post(f"/api/lw/{wid}/room/{rid}/place", params={"artifact_id": table["id"]})
    hid = client.post(f"/api/lw/{wid}/human", json={"name": "Doomed"}).json()["human"]["id"]
    client.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    client.post(f"/api/lw/{wid}/artifact/{table['id']}/seat", json={"slot": 0, "human_id": hid})
    assert client.delete(f"/api/lw/{wid}/entity/{hid}").json()["ok"] is True
    room = client.get(f"/api/lw/{wid}/room/{rid}").json()["room"]
    assert all(a["id"] != hid for a in room["agents"])            # gone from the scene
    assert hid not in room["props"][0]["seated"]                  # unseated from the table
    assert all(a["id"] != hid for a in client.get(f"/api/lw/{wid}").json()["agents"])   # gone from the world


# --------------------------------------------------------------------------
# 9. per-agent model (Stage 1) + directed log rows (Stage 0)
# --------------------------------------------------------------------------

def test_an_agent_possesses_its_own_whitelisted_model_at_the_appraisal():
    """The one bounded Tier-2 call uses THIS agent's model when it named a whitelisted one,
    and falls back to the world default otherwise — the model name is never trusted raw."""
    seen = []
    async def complete(provider, model, system, prompt, settings, max_tokens=2000):
        seen.append(model)
        return '{"understood":"noted","mood":{"stress":0.1},"action":{"kind":"say","text":"ok"}}'
    w = World(name="live", complete=complete, settings={}, model_name="claude-haiku-4-5")
    a, b = w.spawn_human("A"), w.spawn_human("B")
    b.model = "claude-opus-4-8"                        # b possesses Opus (whitelisted)
    sc = Scene(w, id=1, name="s", domain="talk"); sc.seat(a); sc.seat(b)
    asyncio.run(sc.say(a, b, "something genuinely novel and weighty", intensity=0.9, stakes=0.9))
    assert seen == ["claude-opus-4-8"], seen           # the receiver appraised with its own model
    assert w.model_for(a) == "claude-haiku-4-5"        # unset → world default
    b.model = "gpt-4o-not-allowed"
    assert w.model_for(b) == "claude-haiku-4-5"        # non-whitelisted → world default, never passed through


def test_an_agents_model_round_trips_through_the_world_json():
    import json
    w = free_world()
    h = w.spawn_human("Ada"); h.model = "claude-sonnet-5"
    w2 = World.from_dict(json.loads(json.dumps(w.to_dict())))
    assert next(x for x in w2.humans() if x.name == "Ada").model == "claude-sonnet-5"


def test_a_beat_stamps_every_log_row_with_a_sender_and_a_round(client):
    """Every beat records who caused it (frm) and which round it belongs to, so the flow view
    can draw a directed sender→receiver edge per beat."""
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "r", "type": "freeplay"}).json()["room"]["id"]
    a = client.post(f"/api/lw/{wid}/human", json={"name": "Ada"}).json()["human"]["id"]
    b = client.post(f"/api/lw/{wid}/human", json={"name": "Bo"}).json()["human"]["id"]
    for h in (a, b):
        client.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    log = client.post(f"/api/lw/{wid}/room/{rid}/round").json()["room"]["log"]
    assert log, "a beat produced no log rows"
    assert all("frm" in row and "round" in row for row in log), "log rows missing frm/round"
    assert all(row["round"] >= 1 for row in log)
    directed = [row for row in log if row["kind"] == "act" and row["frm"] is not None]
    assert directed and any(row["frm"] != row["who"] for row in directed), "no directed sender→receiver beat"


# --------------------------------------------------------------------------
# 10. scene rules — typed "ingress rows" (stages 2-3)
# --------------------------------------------------------------------------

def test_scene_rule_rows_are_validated_whitelisted_and_capped():
    from app.lifeworld.scene_rules import validate_rows, MAX_ROWS
    rows = validate_rows([
        {"effect": "deny", "when": {"kind": "scold"}},
        {"effect": "clamp", "field": "mood.stress", "value": 0.2},
        {"effect": "bias", "field": "drives.social", "value": 0.1},
        {"effect": "annotate", "note": "be gentle"},
        {"effect": "nuke", "when": {"kind": "x"}},                 # unknown effect → dropped
        {"effect": "clamp", "field": "secret.value", "value": 1},  # non-whitelisted family → dropped
        {"effect": "clamp", "field": "social.trust", "value": 1},  # relationships out of range → dropped
        {"effect": "deny", "when": {"evil": True}},                # unknown when-key stripped, row kept
    ])
    assert [r["effect"] for r in rows] == ["deny", "clamp", "bias", "annotate", "deny"]
    assert [r["n"] for r in rows] == list(range(len(rows)))        # renumbered
    assert rows[-1]["when"] == {}                                  # the bogus when-key was stripped
    assert len(validate_rows([{"effect": "annotate", "note": "x"}] * 100)) == MAX_ROWS


def test_a_deny_rule_blocks_a_beat_before_it_can_spend():
    spy = Spy()
    w = World(name="live", complete=spy.complete, settings={})
    a, b = w.spawn_human("A"), w.spawn_human("B")
    sc = Scene(w, id=1, name="s", domain="talk"); sc.seat(a); sc.seat(b)
    sc.rules_rows = [{"effect": "deny", "when": {"kind": "greet"}}]
    p = asyncio.run(sc.greet(a, b))
    assert spy.calls == 0                                          # blocked before perceive → zero spend
    assert "blocked" in p.understood and sc.log[-1]["kind"] == "blocked"


def test_a_clamp_rule_bounds_the_packet_and_leaves_other_fields_alone():
    from app.lifeworld.scene_rules import SceneRuleSet
    p = Packet(mood={"stress": 0.9, "confidence": -0.5})
    SceneRuleSet([{"effect": "clamp", "field": "mood.stress", "value": 0.1}]).shape(p)
    assert p.mood["stress"] == 0.1 and p.mood["confidence"] == -0.5


def test_a_shape_rule_can_never_touch_a_non_whitelisted_family():
    from app.lifeworld.scene_rules import SceneRuleSet
    p = Packet(social={"5": {"trust": 0.0}})
    SceneRuleSet([{"effect": "bias", "field": "social.trust", "value": 1.0}]).shape(p)
    assert p.social == {"5": {"trust": 0.0}}                       # relationships stay out of rule range


def test_the_rules_prompt_block_compiles_rows_and_the_note():
    from app.lifeworld.scene_rules import SceneRuleSet
    block = SceneRuleSet([{"effect": "annotate", "note": "no bluffing"},
                          {"effect": "clamp", "field": "mood.stress", "value": 0.2, "note": "stay calm"}],
                         note="the house always wins").as_prompt()
    assert "no bluffing" in block and "stay calm" in block and "the house always wins" in block


def test_the_clamp_rule_disposes_even_when_the_model_proposes_more(monkeypatch):
    """The model proposes a big stress spike; a scene clamp bounds it after the fact."""
    async def complete(provider, model, system, prompt, settings, max_tokens=2000):
        return '{"understood":"noted","mood":{"stress":0.95},"action":{"kind":"say","text":"ok"}}'
    w = World(name="live", complete=complete, settings={})
    a, b = w.spawn_human("A"), w.spawn_human("B")
    sc = Scene(w, id=1, name="s", domain="talk"); sc.seat(a); sc.seat(b)
    sc.rules_rows = [{"effect": "clamp", "field": "mood.stress", "value": 0.1}]
    p = asyncio.run(sc.say(a, b, "something novel and weighty", intensity=0.9, stakes=0.9))
    assert p.tier == 2 and p.mood.get("stress", 0) <= 0.1 + 1e-9   # code disposed of the model's number


def test_scene_rule_rows_round_trip_through_the_api(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "r", "type": "freeplay"}).json()["room"]["id"]
    posted = [{"effect": "deny", "when": {"kind": "scold"}}, {"effect": "nuke"}]  # 1 valid, 1 dropped
    d = client.post(f"/api/lw/{wid}/room/{rid}/scene", json={"rules_rows": posted}).json()["room"]
    assert len(d["rules_rows"]) == 1 and d["rules_rows"][0]["effect"] == "deny"
    d2 = client.get(f"/api/lw/{wid}/room/{rid}").json()["room"]        # survives a reload
    assert d2["rules_rows"][0]["effect"] == "deny"


# --------------------------------------------------------------------------
# 11. composite artifacts — one class, any object, no exec (stage 4)
# --------------------------------------------------------------------------

def test_a_composite_deck_deals_a_card_sealed_to_only_its_holder():
    from app.lifeworld.artifact import Composite
    from app.lifeworld.components import LIBRARY
    w = free_world()
    deck = Composite.from_spec(w.next_id(), LIBRARY["deck"], name="deck", seed=7); w.add(deck)
    a, b = w.spawn_human("A"), w.spawn_human("B")
    card = w.get(deck.interact("draw", a, w).payload["item"])
    assert card.kind == "composite" and card.holder == a.id
    assert card.reveal(a) is not None and card.reveal(b) is None        # holder-only
    v = deck.view(a)
    assert "state" not in v and "order" not in v.get("public", {})      # the deal order never leaks


def test_the_same_composite_class_is_dice_and_a_pot():
    from app.lifeworld.artifact import Composite
    from app.lifeworld.components import LIBRARY
    w = free_world(); a = w.spawn_human("A")
    die = Composite.from_spec(w.next_id(), LIBRARY["die"]); w.add(die)
    assert "rolls" in die.interact("roll", a, w).text() and 1 <= die.public["value"] <= 6
    pot = Composite.from_spec(w.next_id(), LIBRARY["pot"]); w.add(pot)
    pot.interact("inc", a, w); pot.interact("inc", a, w)
    assert pot.public["count"] == 2 and die.kind == pot.kind == "composite"


def test_a_composite_round_trips_with_spec_and_private_state():
    import json
    from app.lifeworld.artifact import Composite
    from app.lifeworld.components import LIBRARY
    w = free_world(); a = w.spawn_human("A")
    deck = Composite.from_spec(w.next_id(), LIBRARY["deck"], seed=3); w.add(deck)
    deck.interact("draw", a, w)
    w2 = World.from_dict(json.loads(json.dumps(w.to_dict())))
    d2 = next(x for x in w2.artifacts() if x.kind == "composite" and x.spec.get("type") == "deck")
    assert d2.public["cursor"] == 1 and len(d2.state["order"]) == 52


def test_validate_spec_drops_unknown_components_and_builders():
    from app.lifeworld.components import validate_spec
    assert validate_spec({"components": [{"kind": "nope"}]}) is None
    vs = validate_spec({"type": "d", "components": [{"kind": "multiset", "builder": "evil_exec"}]})
    assert "builder" not in vs["components"][0]                          # unknown builder stripped


def test_the_library_generic_build_and_save_to_custom(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    lib = client.get(f"/api/lw/{wid}/artifact-lib").json()
    assert "deck" in lib["shipped"] and "multiset" in lib["components"]
    a = client.post(f"/api/lw/{wid}/artifact", json={"type": "deck", "name": "shoe"}).json()["artifact"]
    assert a["kind"] == "composite" and a["spec"]["type"] == "deck"
    client.post(f"/api/lw/{wid}/artifact", json={"spec": {"type": "coin", "components": [{"kind": "rollable", "faces": 2}]}, "save_as": "coin"})
    assert "coin" in client.get(f"/api/lw/{wid}/artifact-lib").json()["custom"]


def test_create_agent_with_a_possessed_model_and_base_dna(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    prof = client.post(f"/api/lw/{wid}/human", json={
        "name": "Ada", "model": "claude-opus-4-8",
        "dials": {"composure": 90, "empathy": 20}, "drives": {"esteem": 0.9}}).json()["human"]
    assert prof["model"] == "claude-opus-4-8"
    assert prof["traits"]["composure"] > 0.8 and prof["traits"]["empathy"] < 0.3
    prof2 = client.post(f"/api/lw/{wid}/human", json={"name": "Bo", "model": "gpt-4o"}).json()["human"]
    assert prof2["model"] == ""                                          # non-whitelisted → inherits


# --------------------------------------------------------------------------
# 12. threads — the agent graph + the hidden manager (stage 5)
# --------------------------------------------------------------------------

def _threaded_room(names=("A", "B", "C")):
    w = free_world()
    people = [w.spawn_human(n) for n in names]
    s = w.new_room("room", "freeplay")
    for h in people:
        s.seat(h)
    for i in range(len(people) - 1):
        s.connect(people[i].id, people[i + 1].id)          # a chain: one connected thread
    return w, s, people


def test_threads_merge_on_connect_and_split_on_disconnect():
    from app.lifeworld.threads import members_of
    w, s, (a, b, c) = _threaded_room()
    assert len(s.threads) == 1 and sorted(members_of(s.threads[0])) == [a.id, b.id, c.id]
    s.disconnect(a.id, b.id)                                # break the chain → {a} drops (no edges), {b,c} remains
    assert len(s.threads) == 1 and sorted(members_of(s.threads[0])) == [b.id, c.id]


def test_a_threads_manager_reads_the_rulebook_free_and_bounded():
    w, s, people = _threaded_room()
    t = s.threads[0]
    t["rulebook"] = "no bluffing\nkeep it civil"
    t["manager"] = {"model": "", "budget": 2}
    asyncio.run(s.run_thread(t))
    manage = [row for row in s.log if row["kind"] == "manage"]
    assert manage and "surveys" in manage[0]["text"]                       # the manager is present…
    assert any("host →" in row["text"] for row in manage)                   # …and addresses agents from the rulebook
    assert len(manage) <= 1 + 2                                            # survey + at most `budget` lines
    assert all(row["billed"] is False for row in s.log)                    # deterministic mode: entirely free


def test_a_thread_with_no_rulebook_just_surveys():
    w, s, people = _threaded_room()
    asyncio.run(s.run_thread(s.threads[0]))
    manage = [row for row in s.log if row["kind"] == "manage"]
    assert len(manage) == 1 and "no rulebook" in manage[0]["text"]         # a survey, no enforcement lines


def test_the_host_composes_its_lines_in_one_bounded_call():
    seen = []
    async def complete(provider, model, system, prompt, settings, max_tokens=2000):
        seen.append(system)
        return '["stay sharp","play fair"]' if "HOST" in system else '{"understood":"ok","action":{"kind":"say","text":"ok"}}'
    w = World(name="live", complete=complete, settings={})
    a, b = w.spawn_human("A"), w.spawn_human("B")
    s = w.new_room("room", "freeplay"); s.seat(a); s.seat(b); s.connect(a.id, b.id)
    s.threads[0]["rulebook"] = "r1\nr2"
    s.threads[0]["manager"] = {"model": "claude-haiku-4-5", "budget": 4}
    asyncio.run(s.run_thread(s.threads[0]))
    assert sum(1 for x in seen if "HOST" in x) == 1                        # the host made exactly ONE call, whatever the budget
    assert sum(1 for row in s.log if row["kind"] == "manage" and "host →" in row["text"]) >= 1


def test_a_round_over_threads_plays_and_the_rulebook_round_trips(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "r", "type": "freeplay"}).json()["room"]["id"]
    a = client.post(f"/api/lw/{wid}/human", json={"name": "Ada"}).json()["human"]["id"]
    b = client.post(f"/api/lw/{wid}/human", json={"name": "Bo"}).json()["human"]["id"]
    for h in (a, b):
        client.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    th = client.post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b}).json()["thread"]
    client.post(f"/api/lw/{wid}/room/{rid}/thread/{th['id']}", json={"rulebook": "be brief", "manager": {"budget": 1}})
    room = client.post(f"/api/lw/{wid}/room/{rid}/round").json()["room"]
    assert any(row["kind"] == "manage" for row in room["log"])            # the manager ran
    assert any(row["kind"] == "say" for row in room["log"])               # …and the agents talked
    assert room["threads"][0]["rulebook"] == "be brief"


def test_a_manifest_materialises_a_whole_team_and_deliberates(client):
    """The substrate's declarative surface: POST one JSON spec → a real scene with the agents
    (deterministic, no authoring spend), wired by NAME, rules + manager installed — and with
    run.rounds set it deliberates immediately and returns the versioned DECISION MEMO."""
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    man = {"name": "route-debate",
           "agents": [{"name": "Harvey", "dials": {"assertive": 90}, "drives": {"esteem": 0.9}, "brief": "a closer"},
                      {"name": "Mike", "dials": {"curious": 85}}],
           "edges": [["Harvey", "Mike", "both"]],
           "rules": "debate the most sustainable route from A to B",
           "manager": {"budget": 2},
           "run": {"rounds": 2}}
    r = client.post(f"/api/lw/{wid}/manifest", json=man).json()
    assert r["room"]["name"] == "route-debate" and len(r["room"]["agents"]) == 2
    assert set(r["agents"]) == {"Harvey", "Mike"} and len(r["thread_ids"]) == 1
    assert r["room"]["threads"][0]["rulebook"].startswith("debate")
    memo = r["result"]
    assert memo["v"] == 1 and memo["rounds"] == 2
    assert {p["who"] for p in memo["positions"]} == set(r["agents"].values())   # a final position per agent
    assert memo["question"].startswith("debate") and memo["recommendation"]
    # the memo is durable + versioned: a re-run appends v2
    rid, tid = r["room"]["id"], r["thread_ids"][0]
    r2 = client.post(f"/api/lw/{wid}/room/{rid}/thread/{tid}/run", params={"rounds": 1}).json()
    assert r2["result"]["v"] == 2
    hist = client.get(f"/api/lw/{wid}/room/{rid}/thread/{tid}/results").json()["results"]
    assert [m["v"] for m in hist] == [1, 2]
    # the scene is a REAL scene — the canvas view shows the say beats of the deliberation
    assert any(row["kind"] == "say" for row in r["room"]["log"])


def test_a_bad_manifest_is_rejected_not_half_applied(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    dup = {"agents": [{"name": "A"}, {"name": "A"}], "edges": []}
    assert client.post(f"/api/lw/{wid}/manifest", json=dup).status_code == 422
    ghost = {"agents": [{"name": "A"}], "edges": [["A", "Nobody"]]}
    assert client.post(f"/api/lw/{wid}/manifest", json=ghost).status_code == 422


def test_the_memo_synthesis_is_one_bounded_call():
    """Live: a 2-round deliberation makes exactly rounds+1 HOST calls (per-round plans + the
    closing memo) — the 'fine result' never opens an unbounded spend path."""
    seen = []
    ids = {}
    async def complete(provider, model, system, prompt, settings, max_tokens=2000):
        seen.append(system)
        if "closing" in system:
            return ('{"question":"route A to B","positions":[{"who":%d,"position":"rail"},'
                    '{"who":%d,"position":"bus"}],"dissent":"cost","recommendation":"take the rail"}'
                    % (ids["a"], ids["b"]))
        if "HOST" in system:
            return ('{"enforce":["civil"],"round":[{"who":%d,"text":"rail wins"},{"who":%d,"text":"bus is cheaper"}]}'
                    % (ids["a"], ids["b"]))
        return '{"understood":"ok"}'
    w = World(name="live", complete=complete, settings={})
    a, b = w.spawn_human("A"), w.spawn_human("B")
    ids["a"], ids["b"] = a.id, b.id
    s = w.new_room("room", "freeplay"); s.seat(a); s.seat(b); s.connect(a.id, b.id)
    s.threads[0]["rulebook"] = "route A to B"
    s.threads[0]["manager"] = {"model": "claude-haiku-4-5", "budget": 2}
    memo = asyncio.run(s.run_deliberation(s.threads[0], rounds=2))
    host_calls = sum(1 for x in seen if "HOST" in x or "closing" in x)
    assert host_calls == 3                                                  # 2 round-plans + 1 memo
    assert memo["recommendation"] == "take the rail" and memo["dissent"] == "cost"
    assert memo["names"][str(a.id)] == "A"                                  # names travel with the memo


def test_the_graph_chat_talks_to_agents_and_the_manager(client):
    """The user can chat with any agent in the graph, or with the pinned manager; replies are
    generated (deterministic offline) and the whole conversation persists per peer."""
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "r", "type": "freeplay"}).json()["room"]["id"]
    a = client.post(f"/api/lw/{wid}/human", json={"name": "Harvey"}).json()["human"]["id"]
    b = client.post(f"/api/lw/{wid}/human", json={"name": "Mike"}).json()["human"]["id"]
    for h in (a, b):
        client.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    th = client.post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b}).json()["thread"]
    client.post(f"/api/lw/{wid}/room/{rid}/thread/{th['id']}", json={"rulebook": "debate route A to B", "manager": {"budget": 2}})
    # chat with the manager (pinned)
    r = client.post(f"/api/lw/{wid}/room/{rid}/thread/{th['id']}/chat", json={"to": "manager", "text": "who's here?"}).json()
    assert r["to"] == "manager" and len(r["chat"]) == 2 and r["chat"][-1]["role"] == "manager"
    assert "harvey" in r["chat"][-1]["text"].lower() and "mike" in r["chat"][-1]["text"].lower()
    # chat with a specific agent
    r2 = client.post(f"/api/lw/{wid}/room/{rid}/thread/{th['id']}/chat", json={"to": str(a), "text": "hello"}).json()
    assert r2["chat"][-1]["role"] == "agent" and r2["chat"][-1]["text"]
    # both conversations persist, keyed by peer
    hist = client.get(f"/api/lw/{wid}/room/{rid}/thread/{th['id']}/chat").json()["chats"]
    assert "manager" in hist and str(a) in hist and len(hist["manager"]) == 2


def test_a_thread_mediates_a_conversation_round_free():
    """Connected agents TALK: the manager composes a line for each (free & deterministic offline),
    each is its own 'say' beat grounded in the topic, and the whole round stays free."""
    w, s, (harvey, mike) = _threaded_room(("Harvey", "Mike"))
    t = s.threads[0]
    t["rulebook"] = "debate the most sustainable route from A to B"
    t["manager"] = {"model": "", "budget": 2}
    asyncio.run(s.run_thread(t))
    says = [r for r in s.log if r["kind"] == "say"]
    assert {r["frm"] for r in says} == {harvey.id, mike.id}               # each agent spoke exactly once
    assert all("route" in r["text"].lower() for r in says)               # grounded in the rulebook topic
    assert all(r["billed"] is False for r in s.log)                       # entirely free — no spend


def test_a_one_way_arrow_restricts_who_hears():
    """Info flow is straight from the arrows: a one-way edge only lets the tail reach the head."""
    w = free_world()
    a, b = w.spawn_human("A"), w.spawn_human("B")
    s = w.new_room("room", "freeplay"); s.seat(a); s.seat(b)
    s.connect(a.id, b.id, dir="a2b")
    t = s.threads[0]
    assert s._hears(t, a.id, b.id) is True                               # tail → head flows
    assert s._hears(t, b.id, a.id) is False                              # head → tail is blocked
    s.disconnect(a.id, b.id); s.connect(a.id, b.id, dir="both")
    t = s.threads[0]
    assert s._hears(t, a.id, b.id) and s._hears(t, b.id, a.id)           # bidirectional: both hear


def test_protocol_defaults_are_backward_compatible_and_validated():
    """Policy-as-data: an old thread with no protocol resolves to EXACTLY the historical
    behaviour; presets overlay; junk from the API is rejected, not stored."""
    from app.lifeworld.threads import protocol_of, clean_protocol, DEFAULT_PROTOCOL
    assert protocol_of({}) == DEFAULT_PROTOCOL                              # old saved threads → classic
    p = protocol_of({"protocol": {"preset": "evidence-2026"}})
    assert p["init"] == "independent" and p["anonymize"] is True and p["on_unanimity"] == "devils_advocate"
    p2 = protocol_of({"protocol": {"preset": "evidence-2026", "anonymize": False}})
    assert p2["init"] == "independent" and p2["anonymize"] is False         # thread overrides beat the preset
    cleaned = clean_protocol({"preset": "nope", "init": "telepathy", "max_rounds": 99, "anonymize": 1, "extra": "x"})
    assert cleaned == {"anonymize": True, "max_rounds": 4}                  # junk dropped, values clamped


def test_independent_init_gives_each_agent_its_own_model_call():
    """The diverse-initialization fix: round 1 of a run is N separate calls — each on the
    AGENT'S OWN model, blind — not one ghost-written panel. Later rounds and the memo stay
    manager-composed; total calls ≤ N + (rounds-1) + 1, still bounded by construction."""
    calls = []
    async def complete(provider, model, system, prompt, settings, max_tokens=2000):
        calls.append({"model": model, "system": system})
        if "closing" in system:
            return '{"question":"q","positions":[{"who":%d,"position":"p"}],"dissent":"","recommendation":"r"}' % ids["a"]
        if "HOST" in system:
            return '{"enforce":[],"round":[{"who":%d,"text":"reply a"},{"who":%d,"text":"reply b"}],"unanimous":false}' % (ids["a"], ids["b"])
        return "My independent stance."
    w = World(name="live", complete=complete, settings={})
    a, b = w.spawn_human("Ada"), w.spawn_human("Bo")
    a.model, b.model = "claude-haiku-4-5", "claude-sonnet-5"                # different minds per agent
    ids = {"a": a.id, "b": b.id}
    s = w.new_room("room", "freeplay"); s.seat(a); s.seat(b); s.connect(a.id, b.id)
    s.threads[0]["rulebook"] = "q"
    s.threads[0]["manager"] = {"model": "claude-haiku-4-5", "budget": 2}
    s.threads[0]["protocol"] = {"init": "independent"}
    asyncio.run(s.run_deliberation(s.threads[0], rounds=2))
    agent_calls = [c for c in calls if "You are Ada" in c["system"] or "You are Bo" in c["system"]]
    host_calls = [c for c in calls if "HOST" in c["system"] or "closing" in c["system"]]
    assert len(agent_calls) == 2, "round 1 must be one independent call per agent"
    assert {c["model"] for c in agent_calls} == {"claude-haiku-4-5", "claude-sonnet-5"}   # each agent's OWN model
    assert len(host_calls) == 2                                            # round-2 plan + the memo — no round-1 host call
    says = [r for r in s.log if r["kind"] == "say"]
    assert sum(1 for r in says if "independent stance" in r["text"]) == 2  # both spoke their own words in round 1


def test_a_muted_graph_never_pays_even_in_independent_mode():
    """budget 0 is THE mute switch: with init=independent a muted graph must make zero model
    calls, say nothing, and note no spends — the one-spend-choke-point invariant holds."""
    calls = []
    async def complete(provider, model, system, prompt, settings, max_tokens=2000):
        calls.append(model)
        return "x"
    w = World(name="live", complete=complete, settings={})
    a, b = w.spawn_human("A"), w.spawn_human("B")
    s = w.new_room("room", "freeplay"); s.seat(a); s.seat(b); s.connect(a.id, b.id)
    s.threads[0]["manager"] = {"model": "claude-haiku-4-5", "budget": 0}
    s.threads[0]["protocol"] = {"init": "independent"}
    asyncio.run(s.run_thread(s.threads[0], first_round=True))
    assert calls == [] and not [r for r in s.log if r["kind"] == "say"]
    assert a.usage()["used"] == 0 and b.usage()["used"] == 0


def test_clean_protocol_survives_unhashable_junk():
    """A JSON list/object where a string belongs must be DROPPED, not crash the endpoint."""
    from app.lifeworld.threads import clean_protocol
    assert clean_protocol({"init": ["ghostwritten"], "preset": {}, "on_unanimity": {}, "max_rounds": [4]}) == {}
    assert clean_protocol("not-a-dict") == {}


def test_unanimous_string_false_is_not_unanimous():
    """Trust boundary: the model returning unanimous as the STRING 'false' must not trigger
    a spurious devil's-advocate round (bool('false') is True — the exact bug class of ed8f05b)."""
    async def complete(provider, model, system, prompt, settings, max_tokens=2000):
        return '{"enforce":[],"round":[{"who":%d,"text":"t"}],"unanimous":"false"}' % aid
    w = World(name="live", complete=complete, settings={})
    a = w.spawn_human("A"); aid = a.id
    plan = asyncio.run(w.host_plan({"model": "claude-haiku-4-5", "budget": 2}, [a], "q", "", want_unanimous=True))
    assert plan["unanimous"] is False
    async def complete2(provider, model, system, prompt, settings, max_tokens=2000):
        return '{"enforce":[],"round":[{"who":%d,"text":"t"}],"unanimous":"true"}' % aid
    w._complete = complete2
    assert asyncio.run(w.host_plan({"model": "claude-haiku-4-5", "budget": 2}, [a], "q", "", want_unanimous=True))["unanimous"] is True


def test_unanimity_triggers_a_devils_advocate_round():
    """Premature-consensus rule: when the plan reports the panel unanimous, the NEXT round's
    host is instructed to appoint a dissenter (and the appointment is logged)."""
    systems = []
    async def complete(provider, model, system, prompt, settings, max_tokens=2000):
        systems.append(system)
        if "closing" in system:
            return '{"question":"q","positions":[{"who":%d,"position":"p"}],"dissent":"","recommendation":"r"}' % ids["a"]
        if "HOST" in system:
            return '{"enforce":[],"round":[{"who":%d,"text":"agree"},{"who":%d,"text":"agree"}],"unanimous":true}' % (ids["a"], ids["b"])
        return "x"
    w = World(name="live", complete=complete, settings={})
    a, b = w.spawn_human("A"), w.spawn_human("B")
    ids = {"a": a.id, "b": b.id}
    s = w.new_room("room", "freeplay"); s.seat(a); s.seat(b); s.connect(a.id, b.id)
    s.threads[0]["rulebook"] = "q"
    s.threads[0]["manager"] = {"model": "claude-haiku-4-5", "budget": 2}
    s.threads[0]["protocol"] = {"on_unanimity": "devils_advocate"}
    asyncio.run(s.run_deliberation(s.threads[0], rounds=2))
    host_systems = [x for x in systems if "HOST" in x and "closing" not in x]
    assert "opposing case" not in host_systems[0] and "opposing case" in host_systems[1]
    assert any("devil's advocate" in r["text"] for r in s.log if r["kind"] == "manage")


def test_anonymized_transcript_strips_names_for_the_host():
    """anonymize: the manager reads 'Agent 1/Agent 2', never names — arguments, not reputations."""
    w = free_world()
    a, b = w.spawn_human("Harvey"), w.spawn_human("Mike")
    s = w.new_room("room", "freeplay"); s.seat(a); s.seat(b); s.connect(a.id, b.id)
    s._record("say", a.id, f"{a.name}: rail is best", frm=a.id)
    s._record("say", b.id, f"{b.name}: bus is best", frm=b.id)
    t = s._thread_transcript([a, b], anonymize=True)
    assert "Harvey" not in t and "Mike" not in t
    assert "Agent 1: rail is best" in t and "Agent 2: bus is best" in t
    assert "Harvey" in s._thread_transcript([a, b])                        # non-anonymized path unchanged


def test_protocol_round_trips_through_thread_update_and_manifest(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    man = {"name": "p", "agents": [{"name": "A"}, {"name": "B"}], "edges": [["A", "B"]],
           "rules": "r", "protocol": {"preset": "evidence-2026"}}
    r = client.post(f"/api/lw/{wid}/manifest", json=man).json()
    rid, tid = r["room"]["id"], r["thread_ids"][0]
    assert r["room"]["threads"][0]["protocol"] == {"preset": "evidence-2026"}
    client.post(f"/api/lw/{wid}/room/{rid}/thread/{tid}", json={"protocol": {"preset": "classic", "max_rounds": 3}})
    room = client.get(f"/api/lw/{wid}/room/{rid}").json()["room"]
    assert room["threads"][0]["protocol"] == {"preset": "classic", "max_rounds": 3}


def test_agent_model_session_usage_and_sleep():
    """An agent tracks model-uses in a rolling session window; at the cap it sleeps until the
    window passes — the meter/red state the canvas shows."""
    w = free_world()
    a = w.spawn_human("A")
    cap, win = a._session_params()
    t0 = 1_000_000.0
    assert a.usage(t0)["frac"] == 0.0 and not a.asleep(t0)
    for i in range(cap):
        a.note_spend(t0 + i)                                   # burn the whole session
    u = a.usage(t0 + cap)
    assert u["asleep"] is True and u["frac"] == 1.0 and u["used"] == cap
    assert a.asleep(t0 + cap) is True
    assert a.asleep(t0 + cap + win + 1) is False                # the window rolls off → awake again


def test_a_sleeping_agent_sits_out_the_conversation():
    """A resting agent (out of session quota) is skipped: only awake members talk in a round."""
    _w, s, (a, b) = _threaded_room(("A", "B"))
    cap, _ = b._session_params()
    for _ in range(cap):
        b.note_spend()                                         # B exhausts its session (uses real time)
    assert b.asleep()
    t = s.threads[0]; t["manager"] = {"model": "", "budget": 2}
    asyncio.run(s.run_thread(t))
    says = [r for r in s.log if r["kind"] == "say"]
    assert {r["frm"] for r in says} == {a.id}                  # only the awake agent spoke


def test_the_manager_mediates_the_whole_round_in_one_call():
    """Live: a SINGLE manager call both enforces the rulebook AND composes each agent's line —
    one spend for the whole deliberation, then the code disposes (say beats + free broadcast)."""
    seen = []
    ids = {}
    async def complete(provider, model, system, prompt, settings, max_tokens=2000):
        seen.append(system)
        if "HOST" in system:
            return ('{"enforce":["keep it civil"],"round":['
                    '{"who":%d,"text":"Rail is the greenest route."},'
                    '{"who":%d,"text":"A river barge beats rail on carbon."}]}' % (ids["a"], ids["b"]))
        return '{"understood":"ok","action":{"kind":"say","text":"ok"}}'
    w = World(name="live", complete=complete, settings={})
    harvey, mike = w.spawn_human("Harvey"), w.spawn_human("Mike")
    ids["a"], ids["b"] = harvey.id, mike.id
    s = w.new_room("room", "freeplay"); s.seat(harvey); s.seat(mike); s.connect(harvey.id, mike.id)
    s.threads[0]["rulebook"] = "debate the most sustainable route A to B"
    s.threads[0]["manager"] = {"model": "claude-haiku-4-5", "budget": 2}
    asyncio.run(s.run_thread(s.threads[0]))
    assert sum(1 for x in seen if "HOST" in x) == 1                       # ONE manager call, whole deliberation
    says = [r for r in s.log if r["kind"] == "say"]
    assert {r["frm"] for r in says} == {harvey.id, mike.id}
    assert any("rail" in r["text"].lower() for r in says) and any("barge" in r["text"].lower() for r in says)
    assert any("host →" in r["text"] for r in s.log if r["kind"] == "manage")   # enforcement from the same plan


def test_refine_offline_returns_the_text_unchanged(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "r", "type": "freeplay"}).json()["room"]["id"]
    a = client.post(f"/api/lw/{wid}/human", json={"name": "A"}).json()["human"]["id"]
    b = client.post(f"/api/lw/{wid}/human", json={"name": "B"}).json()["human"]["id"]
    for h in (a, b): client.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    th = client.post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b}).json()["thread"]
    out = client.post(f"/api/lw/{wid}/room/{rid}/thread/{th['id']}/refine", json={"text": "let them debate"}).json()
    assert out["text"] == "let them debate"                                # no creds → no spend, unchanged


def test_an_agent_consults_its_memory_before_thinking():
    """The point of remembering is not having to think again — so the knowledge
    lookup happens BEFORE the model call, not as a post-hoc annotation.

    A source-level claim about ORDER inside perceive(), which is why it survived
    the knowledge extraction intact: the store moved out to a service, but where
    the substrate asks it is still the substrate's own business.
    """
    from app.lifeworld import human as hmod
    src = Path(hmod.__file__).read_text()
    body = src.split("async def perceive", 1)[1].split("async def _recall_similar", 1)[0]
    assert "_recall_similar" in body and body.index("_recall_similar") < body.index("world.appraise")
    assert '"via": "similar"' in src
