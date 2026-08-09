"""Smoke: the contract's guarantees, plus the behaviours this service alone owns.

In-process and offline. The substrate's own depth (scenes, appraisal, decisions,
scene rules) is covered by the conductor suite against the same code until commit
B moves those assertions here; this file proves the SERVICE — readiness, auth, the
caller stamp, ownership, the world lock, the crew's whole-behaviour endpoints, and
the invariant the phase is judged on: no credential is in this process.
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

import lifeworld_service_app as svc            # loaded by conftest under a unique name
from lifeworld.tests.conftest import MODEL_CALLS, REGISTER, SETTINGS_REF, TOKEN

SERVICE_DIR = Path(__file__).resolve().parent.parent
AUTH = {"X-Service-Token": TOKEN}
# The conductor's stamp: an authenticated root caller who may spend.
ROOT = {**AUTH, "X-Lw-Owner": "1", "X-Lw-Root": "1", "X-Lw-Settings": SETTINGS_REF,
        "X-Lw-Source": "repair", "X-Lw-Author": "1"}
# A signed-in non-root caller with no credentials of their own: free mode.
FREE = {**AUTH, "X-Lw-Owner": "2", "X-Lw-Root": "0", "X-Lw-Settings": "",
        "X-Lw-Source": "studio", "X-Lw-Author": "0"}

client = TestClient(svc.app)

FACTORS = [{"id": "correctness", "name": "Correctness", "brief": "bugs and races"},
           {"id": "simplicity", "name": "Simplicity", "brief": "fewer steps"},
           {"id": "speed", "name": "Speed", "brief": "it should be quick"}]


def _seat(headers=ROOT, world_id=0, factors=None, current_room_id=0):
    r = client.post(f"/worlds/{world_id}/crew-seating", headers=headers, json={
        "factors": factors or FACTORS, "manager": {"model": "", "budget": 2},
        "protocol": {"preset": "evidence-2026"}, "scene_name": "sprint table",
        "current_room_id": current_room_id})
    assert r.status_code == 200, r.text
    return r.json()


# --- the contract ------------------------------------------------------------

def test_health_is_the_contracts_readiness_shape(clean_store):
    body = client.get("/health").json()
    assert set(body) >= {"ok", "service", "db", "checks"}
    assert body["ok"] is True and body["checks"] == {"db": True, "table": True}
    assert body["service"] == "lifeworld"


def test_health_and_the_spec_need_no_token_but_every_verb_does(clean_store):
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    for method, path, body in (("get", "/worlds", None),
                               ("post", "/worlds", {"name": "w"}),
                               ("get", "/rooms", None),
                               ("get", "/worlds/1", None),
                               ("post", "/worlds/1/crew-seating",
                                {"factors": FACTORS})):
        call = getattr(client, method)
        r = call(path, json=body) if body is not None else call(path)
        assert r.status_code == 401, f"{path} answered {r.status_code} without a token"
        r = (call(path, json=body, headers={"X-Service-Token": "wrong"}) if body is not None
             else call(path, headers={"X-Service-Token": "wrong"}))
        assert r.status_code == 401, f"{path} accepted a wrong token"


def test_the_committed_spec_is_what_is_served(clean_store):
    served = client.get("/openapi.json").json()
    assert served == json.loads((SERVICE_DIR / "openapi.json").read_text())
    assert "/worlds/{world_id}/crew-seating" in served["paths"]


# --- the caller stamp, and ownership -----------------------------------------

def test_a_request_with_no_caller_stamped_is_refused(clean_store):
    """The conductor is the only authenticator, and it says who on every forwarded
    call. A request with a valid service token and no caller is a bug upstream, not
    an anonymous user — and it must not be answered as one."""
    r = client.get("/worlds", headers=AUTH)
    assert r.status_code == 400 and "X-Lw-Owner" in r.text


def test_ownership_is_enforced_where_the_owner_column_lives(clean_store):
    wid = client.post("/worlds", json={"name": "mine"}, headers=ROOT).json()["world"]["id"]
    assert client.get(f"/worlds/{wid}", headers=ROOT).status_code == 200
    # missing and forbidden are the SAME answer, so a guessed id learns nothing
    assert client.get(f"/worlds/{wid}", headers=FREE).status_code == 404
    assert client.get(f"/worlds/{wid + 900}", headers=ROOT).status_code == 404
    assert client.get("/worlds", headers=FREE).json()["worlds"] == []


def test_root_sees_private_decisions_and_nobody_else_does(clean_store):
    seat = _seat()
    wid, hid = seat["world_id"], seat["agents"]["correctness"]
    r = client.get(f"/worlds/{wid}/human/{hid}", headers=ROOT)
    assert r.status_code == 200 and "decisions" in r.json()
    # the same world, asked for by a non-owner, is not there at all
    assert client.get(f"/worlds/{wid}/human/{hid}", headers=FREE).status_code == 404


# --- no credential is in this process ----------------------------------------

def test_a_live_call_carries_a_reference_and_never_a_key(clean_store):
    """THE INVARIANT. `?live` reaches the model door with a settings REFERENCE in the
    place the settings dict used to sit; nothing in the body could be used to
    authenticate to a provider."""
    wid = client.post("/worlds", json={"name": "w"}, headers=ROOT).json()["world"]["id"]
    room = client.post(f"/worlds/{wid}/room", json={"name": "r"}, headers=ROOT).json()["room"]
    # refine is the simplest route that spends: one call, one string back
    r = client.post(f"/worlds/{wid}/room/{room['id']}/thread/1/refine",
                    json={"text": "mike should ask harvey stuff"}, headers=ROOT)
    assert r.status_code == 200 and "Be concise" in r.json()["text"]
    assert MODEL_CALLS and MODEL_CALLS[-1]["settings_ref"] == SETTINGS_REF
    # Nothing in the body could authenticate to a provider. ("max_tokens" is a
    # size, not a secret — the check names credentials, not the word "token".)
    blob = json.dumps({k: v for k, v in MODEL_CALLS[-1].items() if k != "max_tokens"})
    for secret in ("sk-", "api_key", "_key", "oauth", "Bearer"):
        assert secret not in blob, f"{secret!r} crossed the model door"
    assert MODEL_CALLS[-1]["source"] == "repair", "attribution is explicit on the wire"


def test_no_credentials_ride_the_environment_either(clean_store):
    """SERVICE_CONTRACT rule 4, checked rather than trusted: this service's env is
    PORT, DB_PATH, its own token, addresses, and one peer token. Nothing else."""
    src = (SERVICE_DIR / "substrate" / "ports.py").read_text()
    for banned in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                   "GEMINI_API_KEY", "GITHUB_TOKEN"):
        assert banned not in src
    assert "settings_ref" in src, "the reference is what crosses instead"


def test_the_substrate_reaches_up_through_exactly_one_file(clean_store):
    """The invariant that made this package extractable at all: `from ..` appears
    nowhere under substrate/ except its one door. It used to be checked by the module
    graph against the conductor's copy; it belongs with the code."""
    pkg = SERVICE_DIR / "substrate"
    offenders = [f.name for f in sorted(pkg.glob("*.py"))
                 if "from .." in f.read_text() and f.name != "ports.py"]
    assert offenders == []
    # ...and nothing under substrate/ opens a database
    assert not [f.name for f in sorted(pkg.glob("*.py")) if "sqlite3" in f.read_text()]


# --- the crew's whole behaviours ---------------------------------------------

def test_seating_creates_a_world_when_the_engine_has_none(clean_store):
    seat = _seat()
    assert seat["outcome"] == "rebuilt"
    assert set(seat["agents"]) == {"correctness", "simplicity", "speed"}
    assert seat["room_id"] and seat["thread_id"]
    view = client.get(f"/worlds/{seat['world_id']}/room/{seat['room_id']}",
                      headers=ROOT).json()["room"]
    assert {a["name"] for a in view["agents"]} == {"Correctness", "Simplicity", "Speed"}


def test_seating_again_adopts_the_same_room_with_the_same_ids(clean_store):
    """The ids are what every knowledge row hangs off. A second seating must find the
    surviving room by NAME and keep them, never re-seat."""
    first = _seat()
    again = _seat(world_id=first["world_id"])
    assert again["outcome"] == "adopted"
    assert again["room_id"] == first["room_id"]
    assert again["agents"] == first["agents"]


def test_a_lineup_change_re_seats_carries_what_was_earned_and_tidies(clean_store):
    first = _seat()
    wid, hid = first["world_id"], first["agents"]["correctness"]
    d = client.post(f"/worlds/{wid}/crew-decision", headers=ROOT,
                    json={"human_id": hid, "saw": "a flaky test", "understood": "u",
                          "chose": "fix", "because": {}}).json()
    client.post(f"/worlds/{wid}/crew-outcome", headers=ROOT,
                json={"human_id": hid, "decision_id": d["decision_id"], "ok": True,
                      "says": "it held"})
    client.post(f"/worlds/{wid}/crew-chat-note", headers=ROOT,
                json={"room_id": first["room_id"], "thread_id": first["thread_id"],
                      "role": "manager", "text": "sprint 1 retro"})

    two = _seat(world_id=wid, factors=FACTORS[:2], current_room_id=first["room_id"])
    assert two["outcome"] == "rebuilt" and two["room_id"] != first["room_id"]
    assert set(two["agents"]) == {"correctness", "simplicity"}
    # what the crew earned, carried by NAME across the re-seat
    node = client.post(f"/worlds/{wid}/crew-decision-get", headers=ROOT,
                       json={"human_id": two["agents"]["correctness"],
                             "decision_id": d["decision_id"], "ok": True, "says": ""})
    assert node.status_code == 200 and node.json()["outcome"] == "good"
    view = client.get(f"/worlds/{wid}/room/{two['room_id']}", headers=ROOT).json()["room"]
    assert any("retro" in m["text"] for m in view["threads"][0]["chats"]["manager"])
    # ...and exactly one table is left behind: the world is one blob, paid for whole
    assert [r["room_id"] for r in client.get("/rooms", headers=ROOT).json()["rooms"]] \
        == [two["room_id"]]


def test_a_consult_only_reaches_a_graph_neighbour(clean_store):
    """The arrows are the org chart, enforced here rather than requested in a prompt.
    The refusal is machine-readable: the sentence a build session reads is the
    conductor's to compose."""
    seat = _seat()
    wid, me = seat["world_id"], seat["agents"]["correctness"]
    body = {"room_id": seat["room_id"], "thread_id": seat["thread_id"],
            "human_id": me, "question": "why does the import fail?"}
    out = client.post(f"/worlds/{wid}/crew-consult", headers=ROOT,
                      json={**body, "who": "Nobody"}).json()
    assert out == {"ok": False, "reason": "not_a_neighbour",
                   "peers": out["peers"]} and set(out["peers"]) <= {"Simplicity", "Speed"}
    assert MODEL_CALLS == [], "a refusal must be free"

    out = client.post(f"/worlds/{wid}/crew-consult", headers=ROOT, json=body).json()
    assert out["ok"] and out["who"] in ("Simplicity", "Speed")
    assert len(MODEL_CALLS) == 1, "one consult = one bounded call"
    view = client.get(f"/worlds/{wid}/room/{seat['room_id']}", headers=ROOT).json()["room"]
    assert any("(consult)" in r["text"] for r in view["log"]), "the ask must reach the room"


def test_a_free_caller_gets_the_deterministic_substrate(clean_store):
    """No settings reference means no credentials — the world loads FREE and the model
    door is never knocked on. Refusing instead would take the Studio offline for
    everyone who has not pasted a key."""
    seat = _seat()
    wid = seat["world_id"]
    # the crew's world is root's, so ask as root but with the free stamp
    free_root = {**ROOT, "X-Lw-Settings": "", "X-Lw-Author": "0"}
    out = client.post(f"/worlds/{wid}/crew-deliberate", headers=free_root,
                      json={"room_id": seat["room_id"], "thread_id": seat["thread_id"],
                            "topic": "what next", "rulebook": "be brief",
                            "rounds": 1}).json()
    assert out["ok"] and out["memo"], "a free deliberation still produces a memo"
    assert MODEL_CALLS == [], "and it spends nothing"


def test_the_deliberation_reports_what_it_cost_to_make(clean_store):
    """The conductor meters the crew's spend, so the endpoint returns the two facts a
    meter needs: whether the protocol opened with one call per agent, and how many
    agents were at the table."""
    seat = _seat()
    out = client.post(f"/worlds/{seat['world_id']}/crew-deliberate", headers=ROOT,
                      json={"room_id": seat["room_id"], "thread_id": seat["thread_id"],
                            "topic": "t", "rulebook": "r", "rounds": 2}).json()
    assert out["ok"] and out["ring"] == 3
    assert out["independent"] is True, "the evidence-2026 preset opens independently"


def test_the_pool_endpoints_answer_for_the_module_graph(clean_store):
    seat = _seat()
    m = client.get(f"/worlds/{seat['world_id']}/room/{seat['room_id']}/members",
                   headers=ROOT).json()
    assert {x["name"] for x in m["members"]} == {"Correctness", "Simplicity", "Speed"}
    assert client.get(f"/worlds/{seat['world_id']}/room/9999/members",
                      headers=ROOT).status_code == 404, \
        "a gone room must be distinguishable from one that seats nobody"
    rooms = client.get("/rooms", headers=ROOT).json()["rooms"]
    assert rooms and rooms[0]["agents"] == 3


def test_the_register_is_the_conductors_and_this_service_only_writes_to_it(clean_store):
    """A lifeworld agent has to show up beside workers and crew on ONE board, so the
    substrate notes through the conductor's door rather than keeping its own."""
    seat = _seat()
    body = {"room_id": seat["room_id"], "thread_id": seat["thread_id"],
            "human_id": seat["agents"]["correctness"], "question": "a question"}
    client.post(f"/worlds/{seat['world_id']}/crew-consult", headers=ROOT, json=body)
    assert any(k.startswith(f"lw:{seat['world_id']}:") for k in REGISTER), \
        "the answering agent never appeared on the board"


# --- persistence -------------------------------------------------------------

def test_the_first_boot_copy_keeps_the_row_ids(tmp_path):
    """Every world id in the platform is a POINTER at one — repair:world,
    graph:pool:0, a project's team. A copy that renumbered them would silently
    un-staff the crew and re-point every project's team at nothing."""
    legacy = tmp_path / "legacy.db"
    con = sqlite3.connect(legacy)
    con.execute("CREATE TABLE lw_worlds (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " owner_id INTEGER NOT NULL, name TEXT NOT NULL DEFAULT '',"
                " data TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,"
                " updated_at REAL NOT NULL)")
    con.execute("INSERT INTO lw_worlds (id, owner_id, name, data, created_at, updated_at)"
                " VALUES (41, 1, 'the crew', '{}', ?, ?)", (time.time(), time.time()))
    con.commit()
    con.close()

    store = svc.store
    fresh = tmp_path / "fresh.db"
    saved = svc.helpers._conn, svc.helpers.DB_PATH
    try:
        svc.helpers._conn = None
        svc.helpers.DB_PATH = fresh
        store.init_store()
        assert store.backfill_from_legacy(legacy) == 1
        row = store.get_row(41)
        assert row and row["name"] == "the crew", "the id must survive the copy"
        assert store.backfill_from_legacy(legacy) == 0, "and it happens exactly once"
    finally:
        svc.helpers._conn, svc.helpers.DB_PATH = saved


def test_the_world_lock_is_per_world_and_stable(clean_store):
    assert svc.store.lock_for(7) is svc.store.lock_for(7)
    assert svc.store.lock_for(7) is not svc.store.lock_for(8)


def test_concurrent_writers_on_one_world_do_not_lose_each_other(clean_store):
    """The lost update this lock exists for: a World is deserialized fresh per request
    and written back WHOLE, so two overlapping load…await…save cycles erase each other.
    Conductor-side there is nothing left to hold a lock over, which is why the whole
    behaviour had to move here."""
    seat = _seat()
    wid = seat["world_id"]

    async def race():
        import httpx as _httpx
        transport = _httpx.ASGITransport(app=svc.app)
        async with _httpx.AsyncClient(transport=transport,
                                      base_url="http://lifeworld.test") as c:
            await asyncio.gather(*[
                c.post(f"/worlds/{wid}/crew-chat-note", headers=ROOT,
                       json={"room_id": seat["room_id"], "thread_id": seat["thread_id"],
                             "role": "manager", "text": f"line {i}"})
                for i in range(8)])

    asyncio.run(race())
    view = client.get(f"/worlds/{wid}/room/{seat['room_id']}", headers=ROOT).json()["room"]
    texts = sorted(m["text"] for m in view["threads"][0]["chats"]["manager"])
    assert texts == [f"line {i}" for i in range(8)], f"a write was lost: {texts}"
