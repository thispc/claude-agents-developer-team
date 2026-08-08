"""P4 drills — the lifeworld as a service, and everything that had to survive the cut.

The rest of the suite runs the conductor in ROLLBACK mode (`LIFEWORLD_URL` unset,
the in-process package), which is the P4-A fallback and has to stay green on its
own. This file is the one that flips the whole app into CLIENT mode — the real
httpx path onto the mounted service — and asserts the things a process boundary
can quietly break:

  the doors      /internal/complete, /internal/agents/{note,get}: who may knock,
                 and that a model credential never goes out through one
  the proxy      /api/lw/* keeps its paths, its auth and its ownership 404
  the seating    the crew is seated across the wire, and a dead pointer still
                 HEALS BY ADOPTION with the human ids preserved — the drill that
                 protects every knowledge row keyed to one
  the lock       a read-modify-write cannot straddle the boundary
  degraded       every documented shape, including the crew standing down with an
                 honest reason and WAKING when the service comes back
  rollback       unset the URL and the conductor is the monolith again
"""

import asyncio
import json

import httpx
import pytest
from starlette.testclient import TestClient

from app import db, lifeworld_client as lwc, logs, repair
from conftest import (LIFEWORLD_TEST_TOKEN, _LIFEWORLD_URL, lifeworld_service, login)


# --------------------------------------------------------------------------
# fixtures: client mode, and the outage
# --------------------------------------------------------------------------

def _svc_client(*_a, **_k):
    c = TestClient(lifeworld_service.app, base_url=_LIFEWORLD_URL)
    c.headers["X-Service-Token"] = LIFEWORLD_TEST_TOKEN
    return c


@pytest.fixture()
def lw(fresh_db, monkeypatch):
    """The conductor in CLIENT mode against the mounted service.

    Patched rather than set at import on purpose: `lifeworld_client.enabled()` is
    checked at CALL time precisely so the P4-A rollback (`unset LIFEWORLD_URL`) needs
    no restart — which is also what lets one test file drive both modes.
    """
    monkeypatch.setattr(lwc, "_URL", _LIFEWORLD_URL)
    monkeypatch.setattr(lwc, "_TOKEN", LIFEWORLD_TEST_TOKEN)
    monkeypatch.setattr(lwc, "_TRANSPORT",
                        httpx.ASGITransport(app=lifeworld_service.app))
    monkeypatch.setattr(lwc, "_sync_client", _svc_client)
    assert lwc.enabled()
    return lwc


@pytest.fixture()
def lw_down(lw, monkeypatch):
    """The service, unreachable. Not "returns 500" — GONE, which is what a stopped
    process looks like and what every degraded shape is written for."""
    class _Dead(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused")

    def _dead_sync(*_a, **_k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(lwc, "_TRANSPORT", _Dead())
    monkeypatch.setattr(lwc, "_sync_client", _dead_sync)
    return lwc


def _root_user():
    from app import auth, config
    import app.config as config_mod
    config_mod.AUTH_CONFIGURED = True
    auth.save_settings(1, {"anthropic_api_key": "sk-ant-test-not-a-real-key"})
    return auth.get_user(1)


def _go_live(monkeypatch):
    from app import config as config_mod
    monkeypatch.setattr(config_mod, "AUTH_CONFIGURED", True)
    from app import auth
    auth.save_settings(1, {"anthropic_api_key": "sk-ant-test-not-a-real-key"})


# --------------------------------------------------------------------------
# the doors
# --------------------------------------------------------------------------

def _door_headers(token: str) -> dict:
    return {"X-Service-Token": token}


def test_every_new_door_refuses_a_missing_or_wrong_token(client):
    """Three doors landed in P4 and each one is a way into the conductor from a
    process that is not a user. None of them may be reachable without a token."""
    calls = [("post", "/internal/complete", {"model": "m", "prompt": "p"}),
             ("post", "/internal/agents/note", {"key": "lw:1:2", "state": "thinking"}),
             ("get", "/internal/agents/lw:1:2", None)]
    for method, path, body in calls:
        r = (client.post(path, json=body) if body is not None else client.get(path))
        assert r.status_code == 401, f"{path} answered {r.status_code} with no token"
        r = (client.post(path, json=body, headers=_door_headers("nope")) if body is not None
             else client.get(path, headers=_door_headers("nope")))
        assert r.status_code == 401, f"{path} accepted a wrong token"


def test_a_service_only_gets_the_doors_its_registry_entry_declared(client, tmp_path,
                                                                   monkeypatch):
    """Being inside the fleet is not a permission. knowledge holds a real, minted
    service token and still may not ask for a model — the 403 names the file to edit."""
    from app.routes import svc
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "knowledge.token").write_text("knowledge-real-token")
    (tokens / "lifeworld.token").write_text("lifeworld-real-token")
    monkeypatch.setattr(svc, "_DATA", tmp_path)
    monkeypatch.setattr(svc, "_services", lambda: {
        "knowledge": {"kind": "service", "managed": True, "url": "http://k",
                      "doors": [], "knobs": []},
        "lifeworld": {"kind": "service", "managed": True, "url": "http://l",
                      "doors": ["model", "agents", "tuning"],
                      "knobs": ["scene_default_model"]}})

    r = client.post("/internal/complete", json={"model": "m", "prompt": "p"},
                    headers=_door_headers("knowledge-real-token"))
    assert r.status_code == 403 and "services.yaml" in r.text

    r = client.post("/internal/agents/note",
                    json={"key": "lw:1:2", "state": "thinking", "what": "x"},
                    headers=_door_headers("lifeworld-real-token"))
    assert r.status_code == 200 and r.json()["state"] == "thinking"

    # the knob allowlist is per-service too, not merely the door
    ok = client.get("/internal/tuning", params={"name": "scene_default_model"},
                    headers=_door_headers("lifeworld-real-token"))
    assert ok.status_code == 200
    nope = client.get("/internal/tuning", params={"name": "repair_builder_model"},
                      headers=_door_headers("lifeworld-real-token"))
    assert nope.status_code == 403 and "knobs:" in nope.text


def test_the_model_door_refuses_a_reference_it_did_not_mint(client, tmp_path, monkeypatch):
    """THE INVARIANT OF THE PHASE. A service names WHOSE quota to spend, and it names
    it with a signed reference — so a service that made one up gets nothing, and a
    service that echoes back the one the conductor gave it gets an answer without ever
    holding a key."""
    from app import auth, providers
    from app.routes import svc
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "lifeworld.token").write_text("lifeworld-real-token")
    monkeypatch.setattr(svc, "_DATA", tmp_path)
    monkeypatch.setattr(svc, "_services", lambda: {
        "lifeworld": {"kind": "service", "managed": True, "url": "http://l",
                      "doors": ["model"], "knobs": []}})
    _root_user()

    seen = {}

    async def fake(provider, model, system, prompt, settings, max_tokens=2000, source=""):
        seen.update(settings=settings, source=source, max_tokens=max_tokens)
        return "an answer"
    monkeypatch.setattr(providers, "complete", fake)

    forged = "user:1.deadbeefdeadbeefdeadbeefdeadbeef"
    r = client.post("/internal/complete",
                    json={"model": "m", "prompt": "p", "settings_ref": forged},
                    headers=_door_headers("lifeworld-real-token"))
    assert r.status_code == 403 and not seen, "a forged reference must resolve to nothing"

    r = client.post("/internal/complete",
                    json={"model": "m", "prompt": "p", "source": "studio",
                          "max_tokens": 99_999,
                          "settings_ref": auth.mint_settings_ref(auth.get_user(1))},
                    headers=_door_headers("lifeworld-real-token"))
    assert r.status_code == 200 and r.json()["text"] == "an answer"
    assert seen["settings"]["anthropic_api_key"] == "sk-ant-test-not-a-real-key", \
        "the door resolves the key HERE — that is the whole point"
    assert seen["source"] == "studio", "attribution is explicit on the wire (P2)"
    assert seen["max_tokens"] <= 4000, "a service must not be able to ask for a window"


def test_a_provider_failure_comes_back_as_a_gateway_error(client, tmp_path, monkeypatch):
    """502, not 500: the failure is upstream of this box, and every substrate caller
    already has a free fallback for a raising `complete`."""
    from app import auth, providers
    from app.routes import svc
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "lifeworld.token").write_text("t")
    monkeypatch.setattr(svc, "_DATA", tmp_path)
    monkeypatch.setattr(svc, "_services", lambda: {
        "lifeworld": {"kind": "service", "managed": True, "url": "http://l",
                      "doors": ["model"], "knobs": []}})
    _root_user()

    async def boom(*a, **k):
        raise providers.ProviderError("anthropic returned 529: overloaded")
    monkeypatch.setattr(providers, "complete", boom)

    r = client.post("/internal/complete",
                    json={"model": "m", "prompt": "p",
                          "settings_ref": auth.mint_settings_ref(auth.get_user(1))},
                    headers=_door_headers("t"))
    assert r.status_code == 502 and "overloaded" in r.text


def test_the_service_env_carries_no_model_credential():
    """The registry is where a credential could quietly follow the code. notify's
    GITHUB_TOKEN is the fleet's one exception and it is not one of these."""
    import yaml
    from pathlib import Path
    reg = yaml.safe_load((Path(__file__).resolve().parents[1] / "services.yaml").read_text())
    lw_entry = reg["services"]["lifeworld"]
    assert not lw_entry.get("env"), \
        "the lifeworld declares no extra env — inference goes through the model door"
    assert "model" in lw_entry["doors"] and "knowledge" in lw_entry["peers"]


# --------------------------------------------------------------------------
# the proxy: /api/lw/* must not move
# --------------------------------------------------------------------------

def test_the_studio_paths_are_proxied_and_still_need_a_session(lw, client):
    """Fifty-odd hardcoded paths across dashboard/js address these. The service moved;
    they did not, and an anonymous caller still gets 401 BEFORE a byte is forwarded."""
    assert client.get("/api/lw").status_code == 401
    assert client.post("/api/lw", json={"name": "w"}).status_code == 401
    login(client, "root", "testpass")
    r = client.post("/api/lw", json={"name": "a small world"})
    assert r.status_code == 200, r.text
    wid = r.json()["world"]["id"]
    assert client.get(f"/api/lw/{wid}").json()["world"]["name"] == "a small world"
    assert client.get("/api/lw").json()["worlds"][0]["id"] == wid
    # the room verbs round-trip through the proxy exactly as they did in-process
    room = client.post(f"/api/lw/{wid}/room", json={"name": "office", "type": "office"})
    assert room.status_code == 200 and room.json()["room"]["type"] == "office"


def test_ownership_is_still_a_404_across_the_wire(lw, client, make_user):
    """The ownership row moved into the service, so the CHECK moved with it — a check
    against a table you no longer own is a check against a stale copy. What must not
    change is the answer: missing and forbidden are both 404, so a guessed id learns
    nothing."""
    login(client, "root", "testpass")
    wid = client.post("/api/lw", json={"name": "root's world"}).json()["world"]["id"]
    _uid, other = make_user("stranger")
    assert other.get(f"/api/lw/{wid}").status_code == 404
    assert other.post(f"/api/lw/{wid}/room", json={"name": "x"}).status_code == 404


def test_the_agent_panel_is_composed_not_forwarded(lw, root_client):
    """Root's copy carries the backend's own log rows for that agent — which belong to
    the WATCH service and are root-only. A lifeworld that fetched them would be a
    second, undeclared caller of watch with none of watch's gates."""
    from conftest import clear_logs
    clear_logs()
    wid = root_client.post("/api/lw", json={"name": "w"}).json()["world"]["id"]
    hid = root_client.post(f"/api/lw/{wid}/human",
                           json={"name": "Ada", "dials": {}}).json()["human"]["id"]
    logs.info("session", "something", "Ada did a thing")
    out = root_client.get(f"/api/lw/{wid}/human/{hid}").json()
    assert "logs" in out and any("Ada" in json.dumps(r) for r in out["logs"])
    assert "name" not in out, "the name was only ever the key for the log lookup"


def test_a_non_root_agent_panel_withholds_private_decisions(lw, client, make_user):
    """Root-only detail is decided from the conductor's stamp, not from anything the
    service could be talked into believing."""
    uid, mine = make_user("owner1")
    wid = mine.post("/api/lw", json={"name": "w"}).json()["world"]["id"]
    hid = mine.post(f"/api/lw/{wid}/human", json={"name": "Bo"}).json()["human"]["id"]
    out = mine.get(f"/api/lw/{wid}/human/{hid}").json()
    assert out["withheld"] == 0 and "logs" not in out


# --------------------------------------------------------------------------
# the crew, across the wire
# --------------------------------------------------------------------------

def test_the_crew_is_seated_by_the_service_and_the_record_stays_here(lw, monkeypatch):
    """`repair.ensure_team` is a ~30-line client now. What it keeps is the kv record —
    a POINTER, and the only thing that says which room the crew is in."""
    _go_live(monkeypatch)
    info = repair.ensure_team()
    assert info and info["world_id"] and info["room_id"] and info["thread_id"]
    assert set(info["agents"]) == {f["id"] for f in repair.enabled_factors()}
    assert db.kv_get("repair:world") == info, "the record is the conductor's"
    # and the world really is in the SERVICE's database, not the conductor's
    rows = lifeworld_service.helpers.db().execute(
        "SELECT id FROM lw_worlds").fetchall()
    assert [r[0] for r in rows] == [info["world_id"]]
    assert db._rows("SELECT COUNT(*) AS n FROM lw_worlds")[0]["n"] == 0, \
        "the conductor's table must stay empty in client mode"


def test_a_second_call_is_free_and_changes_nothing(lw, monkeypatch):
    _go_live(monkeypatch)
    first = repair.ensure_team()
    again = repair.ensure_team()
    assert again == first


def test_a_dead_pointer_still_heals_by_adoption_across_the_boundary(lw, monkeypatch):
    """THE DRILL THIS PHASE IS JUDGED ON.

    The kv record is a pointer and it can lose a race the world file wins. Rebuilding
    on a dead pointer would re-seat new human ids and orphan every knowledge row keyed
    to the living ones. Now the pointer is in one process and the room is in another,
    and the ids that must be preserved cross the wire in a JSON body — so the heal is
    drilled again, end to end, in client mode.
    """
    _go_live(monkeypatch)
    info = repair.ensure_team()
    old_ids = sorted(m["agent_id"] for m in repair.crew_members())
    assert old_ids

    db.kv_set("repair:world", dict(info, room_id=info["room_id"] + 900,
                                   agents={k: v + 900 for k, v in info["agents"].items()}))
    assert repair._specialist("correctness")[1] is None, "the corruption must un-staff"

    healed = repair.ensure_team()
    assert healed["room_id"] == info["room_id"], "adopt the surviving room, never rebuild"
    assert sorted(m["agent_id"] for m in repair.crew_members()) == old_ids, \
        "adoption must keep the human ids — every knowledge row hangs off one"
    assert repair._specialist("correctness")[1] is not None
    assert any(r["event"] == "crew_room_adopted" for r in logs.rows())


def test_a_factor_toggle_re_seats_and_keeps_what_was_earned(lw, monkeypatch):
    """A factor toggle is a change of lineup, not amnesia: the memos, the manager
    conversation and every association a specialist proved are carried by NAME across
    the rebuild — inside the service, in one call, under its world lock."""
    _go_live(monkeypatch)
    info = repair.ensure_team()
    hid = info["agents"]["correctness"]
    got = lwc.crew_decision(
        info["world_id"], hid, saw="a flaky test in the suite", understood="u",
        chose="fix it", because={})
    assert got and got["decision_id"]
    asyncio.run(lwc.crew_outcome(
        info["world_id"], hid, got["decision_id"], True, "it held up"))
    lwc.crew_chat_note(info["world_id"], info["room_id"],
                                           info["thread_id"], "sprint 1 retro")

    fs = repair.factors()
    for f in fs:
        if f["id"] == "speed":
            f["enabled"] = False
    db.kv_set("repair:factors", fs)
    after = repair.ensure_team()
    assert after["room_id"] != info["room_id"], "a lineup change is a re-seat"
    assert "speed" not in after["agents"]
    node = repair.decision_node("correctness", got["decision_id"])
    assert node and node["outcome"] == "good", "the specialist's decision log survived"
    view = lwc.room_view(after["world_id"], after["room_id"])
    assert any("retro" in m["text"] for m in view["threads"][0]["chats"]["manager"]), \
        "the manager conversation survived the re-seat"


def test_the_room_seats_exactly_one_table_after_a_re_seat(lw, monkeypatch):
    """The world is one blob, loaded and saved whole, so a dead room is paid for on
    every tick. `_tidy` moved into the service with the seating it belongs to."""
    _go_live(monkeypatch)
    first = repair.ensure_team()
    fs = repair.factors()
    for f in fs:
        if f["id"] == "speed":
            f["enabled"] = False
    db.kv_set("repair:factors", fs)
    after = repair.ensure_team()
    rooms = lwc.rooms(repair._root_user(), [after["world_id"]])
    assert [r["room_id"] for r in rooms] == [after["room_id"]], \
        f"the crew's world kept a dead room: {rooms}"
    assert first["room_id"] not in [r["room_id"] for r in rooms]


def test_a_consult_reaches_a_neighbour_through_the_service(lw, monkeypatch):
    """One bounded call on the ANSWERING agent's model, the ask and the answer both on
    the room record, and the arrows still the org chart — all of it now one endpoint,
    because all of it is one read-modify-write on a world blob."""
    _go_live(monkeypatch)
    from app import providers
    calls = []

    async def fake(provider, model, system, prompt, settings, max_tokens=2000, source=""):
        calls.append({"system": system, "settings": settings, "source": source})
        return "symlink the venv — we proved this in sprint 3"
    monkeypatch.setattr(providers, "complete", fake)

    info = repair.ensure_team()
    task = {"factor": "correctness", "title": "t", "brief": "b"}
    out = asyncio.run(repair.consult(task, "the worktree build dies with ImportError, why?"))
    assert "symlink" in out
    assert len(calls) == 1, "one consult = one bounded call"
    assert calls[0]["settings"]["anthropic_api_key"] == "sk-ant-test-not-a-real-key", \
        "the key was resolved at the door, in the conductor"
    assert task["consults"][0]["who"] in ("Simplicity", "Speed")

    log = lwc.room_view(info["world_id"], info["room_id"])["log"]
    assert any("(consult)" in r["text"] for r in log), "the ask never reached the room"
    assert any("symlink" in r["text"] for r in log), "nor did the answer"


def test_naming_an_outsider_is_refused_in_the_engines_own_words(lw, monkeypatch):
    """The service answers a machine-readable reason; the sentence a build session
    reads is composed conductor-side, because that sentence is the engine's voice."""
    _go_live(monkeypatch)
    from app import providers
    calls = []

    async def fake(*a, **k):
        calls.append(1)
        return "should never be called"
    monkeypatch.setattr(providers, "complete", fake)

    repair.ensure_team()
    task = {"factor": "correctness", "title": "t"}
    out = asyncio.run(repair.consult(task, "how do I fix this import?", who="Seamlessness"))
    assert "You can only consult your graph neighbours" in out
    assert "Simplicity" in out and "Speed" in out
    assert calls == [], "a refusal must be free"
    assert task.get("consults", []) == [], "and uncounted"


def test_a_specialist_is_briefed_from_both_stores(lw, monkeypatch):
    """The lifeworld half (who it is, what it proved) comes from the service; the
    knowledge half stays conductor-side, because the embedding key does."""
    _go_live(monkeypatch)
    from app import knowledge
    info = repair.ensure_team()
    hid = info["agents"]["correctness"]
    asyncio.run(knowledge.remember(
        repair.reg_key_for(info["world_id"], hid),
        "a fresh worktree fails with ImportError",
        "symlink .venv into every worktree", good=2))
    ctx = asyncio.run(repair.specialist_context(
        {"factor": "correctness", "title": "Worktree ImportError",
         "brief": "a fresh worktree fails with ImportError"}))
    assert ctx and ctx["name"] == "Correctness"
    assert ctx["traits"], "the persona came from the substrate"
    assert any("symlink" in e["says"] for e in ctx["experience"]), \
        "the knowledge hit came from the conductor's own recall"
    assert set(ctx["neighbours"]) == {"Simplicity", "Speed"}


def test_the_module_graphs_pool_reads_the_room_through_the_service(lw, root_client,
                                                                   monkeypatch):
    _go_live(monkeypatch)
    info = repair.ensure_team()
    out = root_client.get("/api/graph/self/team").json()
    assert out["current"]["room_id"] == info["room_id"]
    assert {m["name"] for m in out["members"]} >= {"Correctness"}
    assert any(m.get("factor") == "correctness" for m in out["members"]), \
        "the factor decoration is the crew's record, applied conductor-side"
    assert out["degraded"] is False


# --------------------------------------------------------------------------
# the lock cannot straddle the boundary
# --------------------------------------------------------------------------

def test_two_overlapping_writers_do_not_erase_each_other(lw, monkeypatch):
    """The crew's sprint and the operator's chat both do load → await → save on one
    world. Conductor-side there is no longer a lock to hold, so the service holds it —
    and this drills that it actually serialises, with the awaits interleaved."""
    _go_live(monkeypatch)
    info = repair.ensure_team()
    wid, rid, tid = info["world_id"], info["room_id"], info["thread_id"]

    # crew_chat_note is sync (its callers are); drive the same endpoint concurrently
    # through the async client so the two really do interleave inside the service.
    async def note(text):
        return await lwc._post(f"/worlds/{wid}/crew-chat-note",
                               {"room_id": rid, "thread_id": tid, "role": "manager",
                                "text": text}, headers=lwc.root_stamp())

    async def race():
        return await asyncio.gather(*[note(f"line {i}") for i in range(6)])

    asyncio.run(race())
    view = lwc.room_view(wid, rid)
    texts = [m["text"] for m in view["threads"][0]["chats"]["manager"]]
    assert sorted(texts) == [f"line {i}" for i in range(6)], \
        f"a concurrent write was lost: {texts}"


# --------------------------------------------------------------------------
# degraded: every documented shape
# --------------------------------------------------------------------------

def test_the_studio_says_the_substrate_is_down_instead_of_showing_an_empty_world(
        lw_down, root_client):
    """503 with a readable reason. Rendering an empty world would be worse than an
    error: the next save would persist it."""
    r = root_client.get("/api/lw/1")
    assert r.status_code == 503
    assert "lifeworld service is not answering" in r.json()["detail"]
    assert r.json()["degraded"] is True
    r = root_client.post("/api/lw/1/room", json={"name": "x"})
    assert r.status_code == 503


def test_the_crew_stands_down_with_an_honest_reason_and_wakes_on_recovery(
        lw, monkeypatch):
    """PAUSING IS THE HONEST BEHAVIOUR. A crew that kept sprinting without its
    specialists would still be spending, just anonymously — and nothing would learn
    from the outcome. The sleep is bounded so recovery needs no restart, and it
    resumes the phase it was in rather than starting a fresh sprint over the top."""
    _go_live(monkeypatch)
    repair.toggle(True)
    repair.ensure_team()

    class _Dead(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connection refused")

    def _dead_sync(*_a, **_k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(lwc, "_TRANSPORT", _Dead())
    monkeypatch.setattr(lwc, "_sync_client", _dead_sync)
    db.kv_set("repair:world", None)          # a pointer it now cannot re-establish
    assert repair.ensure_team() is None, "seat_crew degrades to None"

    asyncio.run(repair.advance(repair.state()))
    st = repair.state()
    assert st["phase"] == "sleeping" and st["sleep_reason"] == repair.LIFEWORLD_DOWN
    assert 0 < st["sleep_until"] - __import__("time").time() <= repair.LIFEWORLD_WAKE_S, \
        "an unbounded sleep would need a restart to clear"
    assert any(r["event"] == "crew_paused" for r in logs.rows())

    # ...and it wakes, with no restart, the moment the service answers again.
    monkeypatch.setattr(lwc, "_TRANSPORT",
                        httpx.ASGITransport(app=lifeworld_service.app))
    monkeypatch.setattr(lwc, "_sync_client", _svc_client)
    repair.set_state(sleep_until=1)          # its clock has run out; the reason has not
    asyncio.run(repair.tick())
    assert repair.state()["phase"] != "sleeping", "the crew never woke"
    assert repair.team(), "and it re-seated itself on the way back up"


def test_every_crew_verb_degrades_without_blocking_the_work(lw_down, monkeypatch):
    """A learning system that blocks the work in order to learn is worse than one that
    misses a lesson. Each of these is a no-op with a log line, never a raise."""
    _go_live(monkeypatch)
    db.kv_set("repair:world", {"world_id": 1, "room_id": 1, "thread_id": 1,
                               "agents": {"correctness": 2}, "factor_ids": [],
                               "personas": repair.TEAM_PERSONAS})
    assert repair.crew_members() == []
    assert repair._specialist("correctness")[1] is None
    assert repair.team_usage() == []
    assert repair.decision_node("correctness", 1) is None
    task = {"factor": "correctness", "title": "t"}
    repair.note_decision(task, "something broke")
    assert "decision_id" not in task, "no id invented when the store is unreachable"
    asyncio.run(repair.note_outcome(dict(task, decision_id=1), True, "worked"))
    out = asyncio.run(repair.consult(task, "a question about the failure"))
    assert "use your best judgement" in out
    assert asyncio.run(repair.specialist_context(task)) is None
    assert any(r["event"] == "lifeworld_degraded" for r in logs.rows())


def test_the_assignment_pool_says_unavailable_rather_than_empty(lw_down, root_client,
                                                               monkeypatch):
    """An empty list because the substrate is down is not the same fact as an empty
    list because nobody has built a team, and a picker that cannot tell them apart
    invites someone to build a second crew."""
    _go_live(monkeypatch)
    db.kv_set("repair:world", {"world_id": 1, "room_id": 1, "thread_id": 1,
                               "agents": {}, "factor_ids": [],
                               "personas": repair.TEAM_PERSONAS})
    out = root_client.get("/api/graph/self/team").json()
    assert out["degraded"] is True and out["teams"] == []
    assert out["current"]["room_id"] == 0 and "not yet materialised" in out["current"]["name"]


def test_the_module_graph_card_goes_red_when_the_service_is_down(lw_down):
    from app import modgraph_health
    assert modgraph_health._probe_lifeworld() is False


# --------------------------------------------------------------------------
# rollback
# --------------------------------------------------------------------------

def test_unsetting_the_url_puts_the_conductor_back_in_the_monolith(fresh_db, monkeypatch):
    """The P4-A rollback, drilled rather than asserted in prose: with no URL the
    package runs in-process, the world lands in the CONDUCTOR's table, and the crew
    seats itself exactly as it did before the extraction."""
    monkeypatch.setattr(lwc, "_URL", "")
    monkeypatch.setattr(lwc, "_TRANSPORT", None)
    assert not lwc.enabled()
    _go_live(monkeypatch)
    info = repair.ensure_team()
    assert info and info["room_id"]
    assert db._rows("SELECT COUNT(*) AS n FROM lw_worlds")[0]["n"] == 1, \
        "rollback must write the conductor's own table"
    assert lwc.health() is True, "in-process, it is up iff this process is"
    from app.lifeworld import store
    w = store.load(info["world_id"])
    assert w and w.scene(info["room_id"]) is not None
