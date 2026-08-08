"""The Studio over HTTP — `/api/lw/*`, end to end through the doorway.

Since the P4 cutover these paths are a thin authenticated proxy onto
`services/lifeworld` (see conductor/app/lifeworld_routes.py), so every test here now
exercises the whole seam: the session cookie resolved conductor-side, the caller
stamped onto the request, the service authorising against its own `owner_id` column,
and the answer coming back unchanged. That is exactly why they stayed: the paths are
what the dashboard hardcodes, and this file is what stops them moving.

The ENGINE's own tests moved with the engine — services/lifeworld/tests/test_substrate.py
— because nothing outside a service's directory may import inside it, and a suite that
unit-tested another process's objects would be testing a copy of them.
"""

import asyncio
import json
from pathlib import Path

import pytest

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
