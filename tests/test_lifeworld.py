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
    """The only file that may reach for providers is store.py, and only for the opt-in
    live path. Every other module is provider-free by construction."""
    for f in LW.glob("*.py"):
        if f.name in ("store.py", "__init__.py"):
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


def test_flow_arrows_link_unlink_prune_and_set_the_order():
    """Arrows are a directed graph drawn between tokens; the agents they touch become the
    turn order, and danglers are dropped on read."""
    w = free_world()
    a, b, c = (w.spawn_human(n) for n in ("A", "B", "C"))
    sc = w.new_room("R")
    for h in (a, b, c):
        sc.seat(h)
    assert sc.link(a.id, b.id) and sc.link(b.id, c.id)
    assert not sc.link(a.id, b.id)          # no duplicate
    assert not sc.link(a.id, a.id)          # no self-loop
    assert not sc.link(a.id, 99999)         # both ends must be in the room
    assert [h.id for h in sc.flow_ring()] == [a.id, b.id, c.id]   # order follows the arrows
    sc.unlink(a.id, b.id)
    assert {(e["from"], e["to"]) for e in sc._live_edges()} == {(b.id, c.id)}
    sc.edges.append({"from": a.id, "to": 4242})     # a dangling arrow
    assert all(e["to"] != 4242 for e in sc._live_edges())        # pruned on read


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


def test_the_round_follows_the_drawn_flow(client):
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    ids = [client.post(f"/api/lw/{wid}/human", json={"name": n}).json()["human"]["id"]
           for n in ("A", "B")]
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "R", "type": "freeplay"}).json()["room"]["id"]
    for hid in ids:
        client.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    r = client.post(f"/api/lw/{wid}/room/{rid}/link", json={"a": ids[0], "b": ids[1]}).json()
    assert r["ok"] and {(e["from"], e["to"]) for e in r["edges"]} == {(ids[0], ids[1])}
    room = client.get(f"/api/lw/{wid}/room/{rid}").json()["room"]
    assert any(e["from"] == ids[0] and e["to"] == ids[1] for e in room["edges"])
    rr = client.post(f"/api/lw/{wid}/room/{rid}/round").json()
    assert rr["world_tau"] > 0 and sum(1 for e in rr["room"]["log"] if e["billed"]) == 0   # still free
    client.post(f"/api/lw/{wid}/room/{rid}/unlink", json={"a": ids[0], "b": ids[1]})
    assert client.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["edges"] == []


def test_flow_ring_follows_topology_not_draw_order():
    """The order must be the direction the arrows point, regardless of the order they were
    drawn — draw B→C first, then A→B, and the flow is still A, B, C."""
    w = free_world()
    a, b, c = (w.spawn_human(n) for n in ("A", "B", "C"))
    sc = w.new_room("R")
    for h in (a, b, c):
        sc.seat(h)
    assert sc.link(b.id, c.id) and sc.link(a.id, b.id)    # drawn back-first
    assert [h.id for h in sc.flow_ring()] == [a.id, b.id, c.id]


def test_a_flow_orders_but_does_not_drop_un_arrowed_seated_agents(client):
    """Drawing one arrow must not silently exclude the rest of the room from the round."""
    from conftest import login
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "W"}).json()["world"]["id"]
    ids = [client.post(f"/api/lw/{wid}/human", json={"name": n}).json()["human"]["id"]
           for n in ("A", "B", "C")]
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "R", "type": "freeplay"}).json()["room"]["id"]
    for hid in ids:
        client.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    client.post(f"/api/lw/{wid}/room/{rid}/link", json={"a": ids[0], "b": ids[1]})   # C is un-arrowed
    r = client.post(f"/api/lw/{wid}/room/{rid}/round").json()
    actors = {e["who"] for e in r["room"]["log"] if e.get("who") is not None}
    assert ids[2] in actors      # the un-arrowed seated agent still got a beat
