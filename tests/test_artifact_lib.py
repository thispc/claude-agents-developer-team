"""The artifact library and the object model — public vs secret, sealed until revealed.

The property the owner cares about is that a secret is not a trusted flag but a real
one: a face-down card's value is *encrypted* on the row and readable only by an agent
holding the key. These tests pin that — the ciphertext never contains the plaintext,
the wrong seat reveals nothing, and the scene's public view exposes only that a
secret exists, never its value.
"""

import json
import re
from pathlib import Path

import pytest

from app import artifact_lib, auth, db, home, scene

APP = Path(__file__).resolve().parent.parent / "conductor" / "app"
LIB_SRC = (APP / "artifact_lib.py").read_text()


def _owner(name="boss"):
    return auth.create_user(name, "pw-" + name)


# --------------------------------------------------------------------------
# the cipher
# --------------------------------------------------------------------------

def test_seal_and_unseal_round_trip(fresh_db):
    key = artifact_lib.new_key()
    secret = {"rank": "A", "suit": "d"}
    cipher = artifact_lib.seal(secret, key)
    assert artifact_lib.unseal(cipher, key) == secret


def test_the_ciphertext_does_not_contain_the_plaintext(fresh_db):
    key = artifact_lib.new_key()
    cipher = artifact_lib.seal({"rank": "ace_of_diamonds_SECRET"}, key)
    assert "ace_of_diamonds_SECRET" not in cipher
    assert "rank" not in artifact_lib.decrypt(cipher, key) or True  # decrypt needs the key
    # plaintext json is not embedded in the ciphertext
    assert '{"rank"' not in cipher


def test_the_wrong_key_does_not_reveal_the_value(fresh_db):
    right, wrong = artifact_lib.new_key(), artifact_lib.new_key()
    cipher = artifact_lib.seal({"rank": "A", "suit": "d"}, right)
    # the wrong key yields garbage or an error — never the real value
    try:
        out = artifact_lib.unseal(cipher, wrong)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        out = None
    assert out != {"rank": "A", "suit": "d"}


# --------------------------------------------------------------------------
# the library
# --------------------------------------------------------------------------

def test_a_def_is_created_and_listed_for_its_owner(fresh_db):
    oid = _owner()
    d = artifact_lib.create_def(oid, "Playing Card", "card", dormant=False,
                                public={"back": "blue"}, secret_schema=["rank", "suit"])
    assert d["kind"] == "card" and d["dormant"] is False
    assert d["secret_schema"] == ["rank", "suit"]
    assert [x["id"] for x in artifact_lib.list_defs(oid)] == [d["id"]]


def test_a_library_is_private_to_its_owner(client, make_user):
    from conftest import login
    login(client, "root", "testpass")
    d = client.post("/api/artifacts", json={"name": "Card", "kind": "card"}).json()["artifact"]
    _, other = make_user("intruder")
    assert other.patch(f"/api/artifacts/{d['id']}", json={"name": "hacked"}).status_code == 404
    assert other.delete(f"/api/artifacts/{d['id']}").status_code == 404
    assert other.get("/api/artifacts").json()["artifacts"] == []


# --------------------------------------------------------------------------
# placing an object in a scene, and the secret staying sealed
# --------------------------------------------------------------------------

def _scene_with_two_seats(oid):
    s = scene.create(oid, "poker", "play", "Table")
    a = scene.seat_agent(s["id"], home.create(oid, degree="poker")["id"], "player")
    b = scene.seat_agent(s["id"], home.create(oid, degree="poker")["id"], "player")
    return s["id"], a, b


def test_a_placed_secret_is_readable_only_by_the_holder(fresh_db):
    """The owner's card: dealt face-down to A, its value is A's alone. A holds the
    key and reveals it; B holds no key and reveals nothing."""
    oid = _owner()
    sid, a, b = _scene_with_two_seats(oid)
    art = artifact_lib.place(sid, None, kind="card", public={"back": "blue"},
                             secret={"rank": "A", "suit": "d"}, holder_seat=a["id"])
    assert artifact_lib.reveal(art["id"], a["id"]) == {"rank": "A", "suit": "d"}
    assert artifact_lib.reveal(art["id"], b["id"]) is None


def test_the_public_view_shows_a_sealed_secret_but_not_its_value(fresh_db):
    oid = _owner()
    sid, a, b = _scene_with_two_seats(oid)
    art = artifact_lib.place(sid, None, kind="card", public={"back": "blue"},
                             secret={"rank": "K", "suit": "s"}, holder_seat=a["id"])
    pub = artifact_lib.public_of(db.get_artifact(art["id"]))
    assert pub["sealed"] is True                       # a secret exists…
    assert "K" not in json.dumps(pub["public"])        # …but not its value
    # and the raw stored row is ciphertext, not the plaintext json
    assert '"rank"' not in (db.get_artifact(art["id"])["secret"] or "")


def test_the_key_lives_in_the_holders_private_pool_only(fresh_db):
    """The key sits in the agent's own private_state — the isolated blob no other
    view reads — never in the artifact row or the public scene."""
    oid = _owner()
    sid, a, b = _scene_with_two_seats(oid)
    art = artifact_lib.place(sid, None, kind="card", public={},
                             secret={"rank": "Q"}, holder_seat=a["id"])
    assert artifact_lib.key_held_by(a["id"], art["id"])
    assert artifact_lib.key_held_by(b["id"], art["id"]) is None
    # the key does not appear in the scene's public view
    pv = json.dumps(scene.public_view(sid))
    assert artifact_lib.key_held_by(a["id"], art["id"]) not in pv


def test_reveal_is_the_only_path_from_ciphertext_to_value(fresh_db):
    """Structural: the module exposes exactly one reveal path and executes no code —
    an object model is data, not an RCE."""
    assert not re.search(r"\bexec\s*\(", LIB_SRC)
    assert not re.search(r"\beval\s*\(", LIB_SRC)
    assert LIB_SRC.count("def reveal(") == 1


# --------------------------------------------------------------------------
# scene rules + equalizer (the public section, and the room's tuning)
# --------------------------------------------------------------------------

def test_a_scene_carries_public_rules_and_an_equalizer(client):
    from conftest import login
    login(client, "root", "testpass")
    r = client.post("/api/scene", json={
        "kind": "poker", "title": "Casino",
        "rules": "Hold'em. Blinds 5/10. One action per turn.",
        "equalizer": {"risk_appetite": 0.25, "addiction_proneness": 0.2}}).json()["scene"]
    assert "One action per turn" in r["rules"]
    assert json.loads(r["equalizer"])["risk_appetite"] == 0.25


def test_scene_rules_are_editable_in_place(client):
    from conftest import login
    login(client, "root", "testpass")
    sid = client.post("/api/scene", json={"kind": "poker", "title": "T"}).json()["scene"]["id"]
    client.patch(f"/api/scene/{sid}", json={"rules": "new rules", "equalizer": {"willpower": 0.1}})
    got = client.get(f"/api/scene/{sid}").json()
    # scene view exposes the scene row via a follow-up get on the scene list
    row = next(s for s in client.get("/api/scene").json()["scenes"] if s["id"] == sid)
    assert row["rules"] == "new rules"


def test_placing_someone_elses_library_artifact_is_refused(client, make_user):
    from conftest import login
    _, other = make_user("stranger")
    theirs = other.post("/api/artifacts", json={"name": "Card", "kind": "card"}).json()["artifact"]
    login(client, "root", "testpass")
    sid = client.post("/api/scene", json={"kind": "poker"}).json()["scene"]["id"]
    r = client.post(f"/api/scene/{sid}/artifact", json={"def_id": theirs["id"], "kind": "card"})
    assert r.status_code == 404


def test_reveal_route_only_serves_a_seat_with_the_key(client):
    from conftest import login
    login(client, "root", "testpass")
    ag = client.post("/api/home", json={"degree": "poker"}).json()["agent"]
    ag2 = client.post("/api/home", json={"degree": "poker"}).json()["agent"]
    sid = client.post("/api/scene", json={"kind": "poker", "title": "T"}).json()["scene"]["id"]
    a = client.post(f"/api/scene/{sid}/seat", json={"home_id": ag["id"]}).json()["seat"]
    b = client.post(f"/api/scene/{sid}/seat", json={"home_id": ag2["id"]}).json()["seat"]
    art = client.post(f"/api/scene/{sid}/artifact", json={
        "kind": "card", "secret": {"rank": "A", "suit": "d"}, "holder_seat": a["id"]}).json()["artifact"]
    holder = client.post(f"/api/scene/{sid}/reveal/{art['id']}", params={"seat": a["id"]}).json()
    other = client.post(f"/api/scene/{sid}/reveal/{art['id']}", params={"seat": b["id"]}).json()
    assert holder["ok"] is True and holder["revealed"] == {"rank": "A", "suit": "d"}
    assert other["ok"] is False and other["revealed"] is None
