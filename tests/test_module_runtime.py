"""The fleet graph's RUNTIME verbs — health, the switches, the panel, the team
pool, mastery, remove, replace, cluster. All offline, house style of
test_module_graph.py: every drill is an invariant that would rot silently — a
heartbeat that stops asking the fleet, a stop button that fakes, a panel that
leaks a file path, a pool that leaks assignment to strangers, a replan that
amnesties mastery, a "remove" that edits history.

P6 changed what most of these are ABOUT. Health used to come from in-process
probes that imported the module they checked, and Start/Stop from a table whose
content was "almost nothing here has an off switch". Both are gone: the beat is
process-compose's readiness plus the service's own /health, and the switch is the
fleet manager's REST API. The fleet itself is answered in-process by conftest —
see FLEET_PROCESSES and FLEET_CALLS there, and note that "Start routed to
process-compose" is a claim about the CALL, which is why the calls are recorded.
"""

import asyncio
import json
import time

import pytest

from app import (auth, config, db, fleet, modgraph, modgraph_author,
                 modgraph_health as mh, repair, tuning)
import conftest as ct


@pytest.fixture(autouse=True)
def _fresh_states():
    """The fleet state sweep is cached ~3s by design; a drill that stops a process
    must not be answered from the previous drill's snapshot of it running."""
    fleet._states_cache.update(ts=0.0, by_name=None)
    fleet._spec_cache.clear()
    yield
    fleet._states_cache.update(ts=0.0, by_name=None)


def _svc_rows(sql: str, params: tuple = ()) -> list[dict]:
    """Rows straight out of the modgraph SERVICE's own database. The drills below
    assert about the ROWS (a superseded plan not edited, a no-op that wrote
    nothing), and since the P5 cutover those rows are another process's — asking
    through the client would only prove it agrees with itself."""
    from conftest import graph_rows
    return graph_rows(sql, params)


def _nodes(root_client) -> tuple[dict, dict]:
    fleet._states_cache.update(ts=0.0, by_name=None)
    r = root_client.get("/api/graph/self")
    assert r.status_code == 200, r.text
    out = r.json()
    return {n["key"]: n for n in out["nodes"]}, out


# --------------------------------------------------------------------------
# health: the tri-state mapping, as pure functions and on the wire
# --------------------------------------------------------------------------

def test_the_tri_state_mapping_is_exactly_the_contract():
    """red iff beat fail; yellow iff beat ok and tests fail; green otherwise —
    including never-run, because absence of evidence is not a warning."""
    assert mh.health_of("fail", "fail") == {"beat": "fail", "tests": "fail", "status": "red"}
    assert mh.health_of("fail", "pass") == {"beat": "fail", "tests": "pass", "status": "red"}
    assert mh.health_of("fail", "none")["status"] == "red"
    assert mh.health_of("ok", "fail") == {"beat": "ok", "tests": "fail", "status": "yellow"}
    assert mh.health_of("ok", "pass") == {"beat": "ok", "tests": "pass", "status": "green"}
    assert mh.health_of("ok", "none")["status"] == "green"


def test_tests_state_only_counts_rows_a_run_has_spoken_about():
    """'mapped' and 'written' suites exist but have never run — claiming pass for
    those would be the canvas inventing green."""
    assert mh.tests_state([]) == "none"
    assert mh.tests_state([{"status": "mapped"}, {"status": "written"}]) == "none"
    assert mh.tests_state([{"status": "mapped"}, {"status": "passing"}]) == "pass"
    assert mh.tests_state([{"status": "passing"}, {"status": "failing"}]) == "fail"
    assert mh.tests_state([{"status": "error"}]) == "fail"


def test_the_rollup_is_worst_child_wins():
    """A room is red if anything in it is red, else yellow if any yellow, else
    green — and its beat/tests aggregate the same way so the fields cannot
    disagree."""
    red = mh.health_of("fail", "none")
    yellow = mh.health_of("ok", "fail")
    green = mh.health_of("ok", "pass")
    assert mh.rollup([green, yellow, red])["status"] == "red"
    assert mh.rollup([green, yellow])["status"] == "yellow"
    assert mh.rollup([green, green]) == {"beat": "ok", "tests": "pass", "status": "green"}
    assert mh.rollup([])["status"] == "green"
    assert mh.rollup([green, red])["beat"] == "fail"
    assert mh.rollup([green, yellow])["tests"] == "fail"


def test_every_card_carries_health_and_a_running_fleet_reads_green(root_client):
    by, _out = _nodes(root_client)
    for n in by.values():
        assert n["health"]["beat"] in ("ok", "fail")
        assert n["health"]["tests"] in ("pass", "fail", "none")
        assert n["health"]["status"] in ("green", "yellow", "red")
    assert all(n["health"]["beat"] == "ok" for n in by.values()), \
        {k: n["health"] for k, n in by.items() if n["health"]["beat"] != "ok"}
    assert all(n["health"]["status"] == "green" for n in by.values())
    assert by["knowledge"]["health"]["tests"] == "none", "never-run must say none, not pass"


def test_a_stopped_process_turns_its_card_red(root_client):
    """The beat is the FLEET's answer: a process process-compose reports not
    running is a red card, whatever else the box thinks. And a card that is
    Running but NOT Ready is red too — a uvicorn that has bound its port is not
    yet a service, which is the whole reason the fleet declares readiness probes."""
    ct.FLEET_PROCESSES["notify"].update(status="Completed", is_ready="", is_running=False)
    by, out = _nodes(root_client)
    assert by["notify"]["health"] == {"beat": "fail", "tests": "none", "status": "red"}
    assert by["knowledge"]["health"]["status"] == "green", "a sibling must not be smeared"
    assert out["conclusion"]["beat"] == "fail", \
        "the platform is not running if a piece of it is not"
    assert out["conclusion"]["fleet"]["down"] == ["notify"]

    ct.FLEET_PROCESSES["notify"].update(status="Running", is_ready="Not Ready",
                                        is_running=True)
    by, _ = _nodes(root_client)
    assert by["notify"]["health"]["beat"] == "fail", \
        "Running is not Ready — a bound port is not an answering service"


def test_a_service_that_stopped_answering_is_red_even_when_pc_is_happy(root_client,
                                                                      monkeypatch):
    """Both halves are required. process-compose knows the PROCESS is alive; only
    the service's own /health knows it can still do its job, and a card that
    reported green off the process table alone would be the exact dishonesty the
    knowledge probe was fixed for in P1."""
    from app import knowledge
    monkeypatch.setattr(knowledge, "health", lambda: False)
    by, _ = _nodes(root_client)
    assert by["knowledge"]["health"]["beat"] == "fail"
    assert by["usage"]["health"]["beat"] == "ok"

    def boom():
        raise RuntimeError("the health call fell over")
    monkeypatch.setattr(knowledge, "health", boom)
    by, _ = _nodes(root_client)
    assert by["knowledge"]["health"]["beat"] == "fail", "a raising check IS a failed beat"


def test_a_red_suite_on_a_live_beat_is_yellow(root_client):
    plan = modgraph.active_plan(0)
    suite = next(t["path"] for t in modgraph.tests(plan["id"], "knowledge"))
    modgraph.update_test_result(plan["id"], suite, "failing", "boom")
    by, _ = _nodes(root_client)
    assert by["knowledge"]["health"] == {"beat": "ok", "tests": "fail", "status": "yellow"}
    assert by["usage"]["health"]["status"] == "green"


def test_the_state_sweep_is_one_call_and_cached(root_client, monkeypatch):
    """One fleet round trip per payload, not one per card — and a poll inside the
    TTL spends none at all. Twelve cards times a process query on a screen that
    polls every six seconds is a latency regression dressed up as freshness."""
    fleet._states_cache.update(ts=0.0, by_name=None)
    ct.FLEET_HITS.clear()
    root_client.get("/api/graph/self")
    hits = ct.FLEET_HITS.count("/processes")
    assert hits == 1, f"the payload made {hits} round trips to the fleet manager"
    root_client.get("/api/graph/self")           # a second poll inside the TTL
    assert ct.FLEET_HITS.count("/processes") == 1, "a poll inside the TTL re-asked"
    assert fleet._states_cache["by_name"] is not None


# --------------------------------------------------------------------------
# the switches: pc for every managed card, the real thing for the rest
# --------------------------------------------------------------------------

def test_the_payload_says_who_can_be_controlled_and_who_cannot(root_client):
    by, _ = _nodes(root_client)
    # every extracted service: a real switch, and process-compose's own state on it
    for name in ("knowledge", "usage", "notify", "watch", "lifeworld", "modgraph"):
        svc = by[name]["service"]
        assert svc == {**svc, "state": "running", "control": True, "kind": "service"}
        assert svc["pc"]["ready"] == "Ready" and svc["pc"]["state"] == "Running"
    # the conductor: honest refusal, and the ONE switch that is genuinely inside it
    core = by["conductor"]["service"]
    assert core["state"] == "running" and core["control"] is False
    assert "Ctrl-C" in core["reason"] and "run-local.sh" in core["reason"]
    assert core["sub"] == {**core["sub"], "key": "repair", "state": "stopped",
                           "control": True}
    # the pool and the apps room: rooms, not switches, and they say why
    assert by["worker-pool"]["service"]["control"] is False
    assert "only work" in by["worker-pool"]["service"]["reason"]
    assert by["apps"]["service"]["control"] is False
    assert "own real Stop" in by["apps"]["service"]["reason"]
    # IDLE, not "stopped": an empty room is nothing deployed, not something killed
    assert by["apps"]["service"]["state"] == "idle"
    assert by["worker-pool"]["service"]["state"] == "idle"
    # the sandbox: its own real switch
    assert by["sandbox"]["service"] == {**by["sandbox"]["service"],
                                        "state": "stopped", "control": True}


def test_start_and_stop_route_to_process_compose(root_client):
    """THE PHASE'S HEADLINE CLAIM, at the wire: the Atlas's Stop is the same switch
    `pc stop knowledge` throws. Asserted on the CALL, not on the answer — a mock
    that agreed with itself would prove nothing about which door was used."""
    ct.FLEET_CALLS.clear()
    r = root_client.post("/api/graph/self/node/knowledge/service", json={"action": "stop"})
    assert r.status_code == 200, r.text
    assert ct.FLEET_CALLS == [("stop", "knowledge")], \
        "the stop did not go through the fleet manager"
    assert r.json()["service"]["state"] == "stopped"
    by, _ = _nodes(root_client)
    assert by["knowledge"]["service"]["state"] == "stopped"
    assert by["knowledge"]["health"]["status"] == "red", "a stopped card must go red"

    r = root_client.post("/api/graph/self/node/knowledge/service", json={"action": "start"})
    assert r.status_code == 200, r.text
    assert ct.FLEET_CALLS[-1] == ("start", "knowledge")
    by, _ = _nodes(root_client)
    assert by["knowledge"]["service"]["state"] == "running"
    assert by["knowledge"]["health"]["status"] == "green"


def test_the_crew_is_a_sub_switch_on_the_conductors_card(root_client):
    """The conductor cannot honestly stop itself from inside a request it is
    serving — but the IT crew it hosts is a kv toggle the engine's tick obeys, and
    that switch is REAL. It rides the same verb with `sub`."""
    assert not db.kv_get("repair:enabled")
    r = root_client.post("/api/graph/self/node/conductor/service",
                         json={"action": "start", "sub": "repair"})
    assert r.status_code == 200, r.text
    assert r.json()["service"]["sub"]["state"] == "running"
    assert db.kv_get("repair:enabled") is True, "the REAL knob must flip"
    assert ct.FLEET_CALLS == [], "a kv toggle must not reach the fleet manager"
    r = root_client.post("/api/graph/self/node/conductor/service",
                         json={"action": "stop", "sub": "repair"})
    assert r.status_code == 200 and r.json()["service"]["sub"]["state"] == "stopped"
    assert db.kv_get("repair:enabled") is False
    # a sub-switch that does not exist is a 400, not a silent no-op
    assert root_client.post("/api/graph/self/node/knowledge/service",
                            json={"action": "stop", "sub": "repair"}).status_code == 400


def test_a_card_with_no_switch_400s_with_the_reason_and_never_fakes(root_client):
    """A stop button that pretends is worse than none, because the operator plans
    around what it claims. Every refusal names the real act instead."""
    r = root_client.post("/api/graph/self/node/conductor/service", json={"action": "stop"})
    assert r.status_code == 400 and "Ctrl-C" in r.json()["detail"]
    r = root_client.post("/api/graph/self/node/worker-pool/service", json={"action": "stop"})
    assert r.status_code == 400 and "only work" in r.json()["detail"]
    r = root_client.post("/api/graph/self/node/apps/service", json={"action": "start"})
    assert r.status_code == 400 and "own real Stop" in r.json()["detail"]
    by, _ = _nodes(root_client)
    assert by["conductor"]["service"]["state"] == "running", \
        "a refused stop must change nothing"
    assert ct.FLEET_CALLS == [], "a refusal must not reach the fleet manager either"
    assert root_client.post("/api/graph/self/node/knowledge/service",
                            json={"action": "sideways"}).status_code == 400
    assert root_client.post("/api/graph/self/node/nowhere/service",
                            json={"action": "stop"}).status_code == 404


def test_the_sandbox_card_is_the_sandboxs_own_switch(root_client, monkeypatch):
    from app import sandbox
    calls = []
    monkeypatch.setattr(sandbox, "start", lambda src: (calls.append(("start", src)) or
                                                       {"ok": True, "url": "u", "port": 8501,
                                                        "commit": "c", "ref": src, "pid": 1}))
    monkeypatch.setattr(sandbox, "stop", lambda: (calls.append(("stop", None)) or
                                                  {"ok": True, "killed": True}))
    r = root_client.post("/api/graph/self/node/sandbox/service", json={"action": "start"})
    assert r.status_code == 200, r.text
    assert calls == [("start", "live")], "the card must use the sandbox's own internals"
    assert ct.FLEET_CALLS == [], "the sandbox is not a fleet-managed process"
    r = root_client.post("/api/graph/self/node/sandbox/service", json={"action": "stop"})
    assert r.status_code == 200 and calls[-1] == ("stop", None)


def test_a_deployed_app_card_stops_through_deploy(root_client, monkeypatch):
    """The EXTERNAL cards: one per app the conductor is running, on its real port,
    with deploy's own stop behind the button."""
    from app import deploy

    class _Proc:
        def poll(self):
            return None
    monkeypatch.setitem(deploy.RUNNING, 7, {"proc": _Proc(), "port": 8601,
                                            "started": time.time(),
                                            "spec": {"kind": "node"}})
    stopped = []
    monkeypatch.setattr(deploy, "stop", lambda pid, branch="":
                        stopped.append((pid, branch)) or "stopped")
    by, _ = _nodes(root_client)
    card = by["app:7"]
    assert card["parent_key"] == "apps" and card["service"]["kind"] == "app"
    assert "8601" in card["spec"], "the card must name the port it really runs on"
    assert by["apps"]["service"]["state"] == "running"
    r = root_client.post("/api/graph/self/node/app:7/service", json={"action": "stop"})
    assert r.status_code == 200, r.text
    assert stopped == [(7, "")]
    assert ct.FLEET_CALLS == [], "a project's app is not a fleet-managed process"
    # ...and starting one is not this screen's job, honestly
    r = root_client.post("/api/graph/self/node/app:7/service", json={"action": "start"})
    assert r.status_code == 400 and "Deploy screen" in r.json()["detail"]


def test_a_fleet_manager_that_refuses_is_reported_not_swallowed(root_client, monkeypatch):
    """A stop the fleet manager rejects must reach the operator as a refusal, not
    as a green card that quietly did nothing."""
    def boom(name):
        raise RuntimeError("process is in a restart backoff")
    monkeypatch.setattr(fleet, "pc_stop", boom)
    r = root_client.post("/api/graph/self/node/watch/service", json={"action": "stop"})
    assert r.status_code == 400
    assert "the fleet manager refused" in r.json()["detail"]
    assert "restart backoff" in r.json()["detail"]


# --------------------------------------------------------------------------
# THE PANEL: the black box stays closed
# --------------------------------------------------------------------------

def _panel(root_client, key) -> dict:
    r = root_client.get(f"/api/graph/self/node/{key}")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_panel_never_renders_a_filesystem_path(root_client):
    """THE OWNER'S DECREE, as a test: "I don't wanna understand what's inside —
    that's the vibe-coding part." A path is the most reliable way to open a box,
    so no payload the Atlas reads may contain one — not in the node, not in the
    test rows, not in the service's own health document, not in the log tail."""
    payload = root_client.get("/api/graph/self").json()
    for n in payload["nodes"]:
        assert "paths" not in n, f"{n['key']} still carries its boundary manifest"
    blob = json.dumps(payload)
    for leak in (".py", "conductor/app", "services/knowledge/", "dashboard/"):
        assert leak not in blob, f"the graph payload leaks {leak!r}"
    for key in ("knowledge", "conductor", "worker-pool", "sandbox", "apps"):
        d = _panel(root_client, key)
        assert "paths" not in d["node"]
        blob = json.dumps(d)
        for leak in (".py", "conductor/app", "services/", "/tmp/", "/var/"):
            assert leak not in blob, f"the {key} panel leaks {leak!r}"


def test_the_panel_shows_the_contract_the_health_the_suite_and_the_logs(root_client):
    """What the panel IS since P6: five things, and no sixth. Each of them is
    something the service PROMISES or is DOING — never something it contains."""
    ct.FLEET_LOGS["knowledge"] = ['INFO: 127.0.0.1 - "POST /recall HTTP/1.1" 200 OK']
    d = _panel(root_client, "knowledge")
    # 1. the contract — its own committed spec, as an endpoint list
    paths = {e["path"] for e in d["contract"]["endpoints"]}
    assert {"/recall", "/remember", "/health"} <= paths
    assert all(e["method"] in ("GET", "POST", "PUT", "PATCH", "DELETE")
               for e in d["contract"]["endpoints"])
    # 2. the health — the tri-state AND the service's own readiness document
    assert d["health"]["status"] == "green"
    assert d["health"]["detail"]["ok"] is True
    assert d["health"]["detail"]["checks"], "the service's own checks must ride along"
    assert "db" not in d["health"]["detail"], "the readiness doc's path was not stripped"
    # 3. the switch
    assert d["service"]["control"] is True and d["service"]["state"] == "running"
    # 4. the suite — counts and a headline, never file names
    assert d["tests"]["total"] >= 1 and d["tests"]["ran"] == 0
    assert "knowledge" in d["tests"]["suite"]
    # 5. the logs — what the process actually printed
    assert d["logs"] == ct.FLEET_LOGS["knowledge"]
    # ...and who works it
    assert "agent" in d and "mastery" in d and d["models"]


def test_a_card_with_no_contract_says_why(root_client):
    """A worker is a spawned process, not a service, so it has no openapi.json —
    and an empty section with no explanation reads as a bug. The note names what
    the card's contract actually is instead."""
    for key in ("worker-pool", "sandbox", "apps"):
        d = _panel(root_client, key)
        assert d["contract"] is None
        assert d["contract_note"], f"{key} explains nothing about its missing contract"
    assert _panel(root_client, "worker-pool")["logs"] == [], \
        "only fleet-managed processes have a fleet log"


def test_the_panel_reports_a_run_suites_verdict(root_client, monkeypatch):
    """How the BFF learns pass/fail: the existing verify runner, scoped by the
    seed to the service's own test directory, writing back into the store."""
    import subprocess
    from app import shell
    # pytest's LAST line is routinely the warnings footer, and the panel showing a
    # documentation URL where a result belongs is both useless and a path leak —
    # so the headline is the VERDICT line, and what survives it is scrubbed.
    out = ("12 passed in 0.4s\n"
           "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n")
    monkeypatch.setattr(shell, "sh",
                        lambda *c, **kw: subprocess.CompletedProcess(c, 0, out, ""))
    root_client.post("/api/graph/self/verify", json={"node": "usage"})
    d = _panel(root_client, "usage")
    assert d["tests"]["ran"] == d["tests"]["total"] >= 1
    assert d["tests"]["passing"] == d["tests"]["ran"] and d["tests"]["failing"] == 0
    assert d["tests"]["headline"] == "12 passed in 0.4s"
    assert "docs.pytest.org" not in json.dumps(d)
    assert all("docs.pytest.org" not in json.dumps(r) for r in d["trace"]), \
        "the trace renders a run's detail verbatim — it must be scrubbed too"
    by, _ = _nodes(root_client)
    assert by["usage"]["health"]["tests"] == "pass"


# --------------------------------------------------------------------------
# the team pool: default = the crew; re-point acts on assignment, not sprints
# --------------------------------------------------------------------------

def _second_room(name="night shift"):
    """Another Studio room the pool can be re-pointed at, built the way anything
    builds one since P4: two calls to the lifeworld service."""
    from app import lifeworld_client as lwc, repair
    root = repair._root_user()
    wid = asyncio.run(lwc.create_world(root, name))
    out = asyncio.run(lwc.apply_manifest(root, wid, {
        "name": "bench",
        "agents": [{"name": "Alia", "brief": "steady hands"},
                   {"name": "Bo", "brief": "fresh eyes"}]}))
    return wid, int(out["room"]["id"])


def test_the_pool_defaults_to_the_crew_with_factors(root_client):
    info = repair.ensure_team()
    assert info
    r = root_client.get("/api/graph/self/team")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["current"]["world_id"] == info["world_id"]
    assert out["current"]["room_id"] == info["room_id"]
    ids = {m["agent_id"] for m in out["members"]}
    assert ids == {hid for hid in info["agents"].values()}
    assert {m.get("factor") for m in out["members"]} == set(info["agents"].keys()), \
        "crew members must say which lens they are"
    assert any(t["world_id"] == info["world_id"] and t["room_id"] == info["room_id"]
               for t in out["teams"]), "the crew's own table must be offered as a pool"


def test_repointing_the_pool_changes_assignment_not_the_sprint_team(root_client):
    info = repair.ensure_team()
    wid, rid = _second_room()
    r = root_client.post("/api/graph/self/team", json={"world_id": wid, "room_id": rid})
    assert r.status_code == 200, r.text
    out = r.json()
    assert db.kv_get("graph:pool:0") == {"world_id": wid, "room_id": rid}
    assert {m["name"] for m in out["members"]} == {"Alia", "Bo"}
    assert all("factor" not in m for m in out["members"]), \
        "factors are a crew concept — a Studio pool has none"
    # DELIBERATELY independent: the crew's sprint team record is untouched
    assert repair.team() == info, "re-pointing the pool must not re-seat the crew"
    got = root_client.get("/api/graph/self/team").json()
    assert got["current"]["world_id"] == wid and got["current"]["room_id"] == rid
    assert root_client.post("/api/graph/self/team",
                            json={"world_id": 9999, "room_id": 1}).status_code == 404


def test_assignment_validates_pool_membership_and_refuses_rooms(root_client):
    info = repair.ensure_team()
    wid, rid = _second_room("bench world")
    root_client.post("/api/graph/self/team", json={"world_id": wid, "room_id": rid})
    members = root_client.get("/api/graph/self/team").json()["members"]
    aid = members[0]["agent_id"]
    # entity ids are world-LOCAL, so a low crew id can collide with a bench id by
    # number — the drill needs a crew specialist whose id is genuinely outside the pool
    pool_ids = {m["agent_id"] for m in members}
    crew_agent = next(h for h in sorted(info["agents"].values(), reverse=True)
                      if h not in pool_ids)
    r = root_client.post("/api/graph/self/node/knowledge/agent", json={"agent_id": aid})
    assert r.status_code == 200, r.text
    assert r.json()["agent"]["name"] == members[0]["name"]
    plan = modgraph.active_plan(0)
    assert modgraph.get_assign(plan["id"], "knowledge")["agent_id"] == aid
    # a crew specialist is NOT in this pool any more — membership is the gate
    r = root_client.post("/api/graph/self/node/usage/agent", json={"agent_id": crew_agent})
    assert r.status_code == 400 and "pool" in r.json()["detail"]
    # rooms (and the frame) refuse: work lands on the service cards
    assert root_client.post("/api/graph/self/node/worker-pool/agent",
                            json={"agent_id": aid}).status_code == 400
    assert root_client.post("/api/graph/self/node/aim/agent",
                            json={"agent_id": aid}).status_code == 400
    assert root_client.post("/api/graph/self/node/nowhere/agent",
                            json={"agent_id": aid}).status_code == 404


# --------------------------------------------------------------------------
# mastery ON THE PAYLOAD — the decoration, not the arithmetic
# --------------------------------------------------------------------------
#
# The COUNTING lives in services/modgraph with the trace it reads. What is left
# here is what only the conductor can say — that the payload carries mastery per
# card, decorated with WHO the agent id is from the crew's own record, and that a
# card with no history answers an honest null. The node key is now a SERVICE NAME,
# which makes the "survives a replan" claim stronger than it has ever been.

def _ok_run(pid: int, key: str, agent_id: int, kind: str = "build"):
    rid = modgraph.note_run(pid, key, kind, agent_id=agent_id, status="running")
    modgraph.close_run(rid, "ok", "done")
    return rid


def test_the_payload_carries_mastery_and_an_honest_null(root_client):
    plan = modgraph.active_plan(0)
    pid = plan["id"]
    _ok_run(pid, "knowledge", 71)
    _ok_run(pid, "knowledge", 71, kind="verify")
    by, _ = _nodes(root_client)
    m = by["knowledge"]["mastery"]
    assert m == {"agent_id": 71, "name": m["name"], "runs": 2, "master": False}
    assert by["usage"]["mastery"] is None, "no runs is an honest null, not a zero-run master"
    _ok_run(pid, "knowledge", 71)
    assert _nodes(root_client)[0]["knowledge"]["mastery"]["master"] is True


def test_a_replan_keeps_mastery_by_service_name(root_client):
    """The arithmetic is the service's; that it SURVIVES a replan is the thing an
    operator sees on the card, so the payload half is drilled here. The key is the
    service's name, so the identity is as stable as the fleet itself."""
    v1 = modgraph.active_plan(0)["id"]
    for _ in range(3):
        _ok_run(v1, "knowledge", 71)
    v2 = modgraph.create_plan(0, authored_by="manager", notes="replanned")
    modgraph.add_node(v2, "aim", "aim", node_type="aim")
    modgraph.add_node(v2, "knowledge", "Memory")
    modgraph.add_node(v2, "conclusion", "done", node_type="conclusion")
    modgraph.activate(v2)
    by, _ = _nodes(root_client)
    assert by["knowledge"]["mastery"]["master"] is True, \
        "a replan amnestied the trace on the screen"


def test_the_authoring_pass_keeps_the_master_over_the_managers_pick(fresh_db, monkeypatch):
    """Mastery outranks reshuffling: the manager proposes Correctness for the
    knowledge service, but Speed's agent has three verified runs there — the
    assignment keeps Speed, and says so in the log."""
    auth.get_user_by_name(auth.ROOT_USERNAME) or auth.create_user(
        auth.ROOT_USERNAME, "pw-root", is_root=True)
    monkeypatch.setattr(config, "AUTH_CONFIGURED", True)
    monkeypatch.setattr(repair, "_root_settings", lambda: {})
    modgraph.init()
    seed_id = modgraph.seed_fleet_graph()
    info = repair.ensure_team()
    master_id = info["agents"]["speed"]
    assert master_id != info["agents"]["correctness"]
    for _ in range(3):
        _ok_run(seed_id, "knowledge", master_id)   # mastery is keyed by SERVICE NAME

    reply = json.dumps({
        "modules": [{"key": "knowledge", "title": "Memory", "spec": "reworded",
                     "join": "all_of", "tags": []}],
        "edges": [],
        "assignments": {"knowledge": "correctness"},
    })
    from app import providers

    async def fake(provider, model, system, prompt, settings, max_tokens=2000, source=""):
        return reply
    monkeypatch.setattr(providers, "complete", fake)
    pid = asyncio.run(modgraph_author.author_self_plan())
    assert pid
    assert modgraph.get_assign(pid, "knowledge")["agent_id"] == master_id, \
        "the master keeps the card, whatever the manager proposed"
    from app import logs
    assert any(r["event"] == "graph_master_kept" for r in logs.rows())


# --------------------------------------------------------------------------
# remove: honest about what a card IS now
# --------------------------------------------------------------------------

def test_removing_a_service_is_refused_because_it_is_a_registry_edit(root_client):
    """The honest answer to "Remove" on a card that is a PROCESS. Dropping the row
    would leave the service running, its port bound and services.yaml unchanged —
    a map that had quietly stopped describing the box. The refusal names the real
    act, and the payload carries the same reason so the menu can grey the verb out
    instead of offering a click that always fails."""
    r = root_client.post("/api/graph/self/node/knowledge/remove")
    assert r.status_code == 400
    assert "services.yaml" in r.json()["detail"]
    by, _ = _nodes(root_client)
    assert by["knowledge"]["service"]["remove"] == {
        "allowed": False, "reason": r.json()["detail"]}
    assert "knowledge" in {n["key"] for n in modgraph.nodes(modgraph.active_plan(0)["id"])}
    for frame in ("aim", "conclusion"):
        assert root_client.post(f"/api/graph/self/node/{frame}/remove").status_code == 400
    assert root_client.post("/api/graph/self/node/nowhere/remove").status_code == 404


def test_removing_a_stale_card_makes_a_new_version_and_never_edits_the_old(root_client):
    """The one card that CAN be removed: a plan row naming nothing the fleet runs.
    A manager-authored plan from before a service was retired leaves exactly that,
    and clearing it costs nothing real — so the immutable-version machinery stays
    exercised where it is still honest."""
    v1 = modgraph.active_plan(0)["id"]
    modgraph.add_node(v1, "ghost", "a service the fleet does not run")
    before = {t: _svc_rows(f"SELECT * FROM {t} WHERE plan_id=? ORDER BY id", (v1,))
              for t in ("graph_nodes", "graph_edges", "graph_node_tests")}
    modgraph.set_assign(v1, "usage", agent_id=77)
    modgraph.save_positions(v1, {"usage": [10, 20], "ghost": [30, 40]})

    by, _ = _nodes(root_client)
    assert by["ghost"]["service"]["remove"]["allowed"] is True
    assert "services.yaml" in by["ghost"]["service"]["reason"]

    r = root_client.post("/api/graph/self/node/ghost/remove")
    assert r.status_code == 200, r.text
    out = r.json()
    v2 = out["plan"]["id"]
    assert v2 != v1 and out["removed"] == "ghost"
    assert out["plan"]["authored_by"] == "root", \
        "a human removal must outrank the seed, or the next boot resurrects the card"
    keys = {n["key"] for n in modgraph.nodes(v2)}
    assert "ghost" not in keys and "usage" in keys
    # steering carried by key; the removed card's position dropped
    assert modgraph.get_assign(v2, "usage")["agent_id"] == 77
    pos = modgraph.positions(v2)
    assert pos.get("usage") == [10.0, 20.0] and "ghost" not in pos
    # v1: byte-identical rows, only the plan's status moved
    for t, rows in before.items():
        assert _svc_rows(f"SELECT * FROM {t} WHERE plan_id=? ORDER BY id", (v1,)) == rows, \
            f"{t} rows of the superseded plan were edited"
    assert modgraph.get_plan(v1)["status"] == "superseded"
    # the flare and the note the manager will read next authoring
    assert any(e["kind"] == "graph_node_removed" for e in db.list_events(0))
    notes = db.kv_get("graph:notes:0") or []
    assert notes and "ghost" in notes[-1]["note"]


def test_the_removal_note_reaches_the_managers_next_authoring_prompt(fresh_db, monkeypatch):
    auth.get_user_by_name(auth.ROOT_USERNAME) or auth.create_user(
        auth.ROOT_USERNAME, "pw-root", is_root=True)
    monkeypatch.setattr(config, "AUTH_CONFIGURED", True)
    monkeypatch.setattr(repair, "_root_settings", lambda: {})
    modgraph.init()
    modgraph.seed_fleet_graph()
    repair.ensure_team()
    db.kv_set("graph:notes:0", [{"ts": time.time(),
                                 "note": "The operator removed the card 'ghost'."}])
    seen = {}
    from app import providers

    async def fake(provider, model, system, prompt, settings, max_tokens=2000, source=""):
        seen["prompt"] = prompt
        return "not json"                     # unusable answer: authoring changes nothing
    monkeypatch.setattr(providers, "complete", fake)
    assert asyncio.run(modgraph_author.author_self_plan()) is None
    assert "OPERATOR NOTES" in seen["prompt"] and "removed the card 'ghost'" in seen["prompt"]
    assert db.kv_get("graph:notes:0"), \
        "an answer that never parsed did not READ the notes — they must survive"


# --------------------------------------------------------------------------
# replace: a real ticket through the self-issue machinery
# --------------------------------------------------------------------------

def test_replace_files_a_ticket_on_the_platforms_own_project(root_client):
    r = root_client.post("/api/graph/self/node/usage/replace",
                         json={"aspect": "tests", "note": "the suite is flaky"})
    assert r.status_code == 200, r.text
    out = r.json()
    proj = out["project_id"]
    assert out["queued"] is True
    p = db.get_project(proj)
    assert p and p["is_self"], "the ticket must land on the platform's own project"
    rows = db._rows("SELECT * FROM inbox WHERE project_id=? AND kind='directive'", (proj,))
    assert rows, "the machinery's directive is the ticket — it must exist"
    text = rows[-1]["text"]
    assert "[graph] Replace the tests of module 'usage'" in text
    assert "the suite is flaky" in text
    assert any(e["kind"] == "self_issue_raised" for e in db.list_events(proj))
    assert any(e["kind"] == "graph_replace_filed" for e in db.list_events(0))


def test_replace_validates_the_aspect_and_points_agent_elsewhere(root_client):
    r = root_client.post("/api/graph/self/node/usage/replace",
                         json={"aspect": "agent", "note": ""})
    assert r.status_code == 400
    assert "/agent" in r.json()["detail"] or "agent endpoint" in r.json()["detail"]
    assert root_client.post("/api/graph/self/node/nowhere/replace",
                            json={"aspect": "tests", "note": ""}).status_code == 404


# --------------------------------------------------------------------------
# the conclusion's vitals and the cluster verbs
# --------------------------------------------------------------------------

def test_the_conclusion_carries_the_platforms_vitals_and_the_fleet(root_client, monkeypatch):
    from app import sandbox
    monkeypatch.setattr(sandbox, "status", lambda: {"running": False})
    _, out = _nodes(root_client)
    c = out["conclusion"]
    assert c["beat"] == "ok"
    assert isinstance(c["uptime_s"], int) and c["uptime_s"] >= 0
    assert c["boot_sha"] and c["head_sha"], "the shas are the honest code identity"
    assert c["cluster"] == {"available": True, "running": False}
    assert c["fleet"] == {"visible": True, "declared": 7, "running": 7,
                          "api": fleet.api_base(), "down": []}
    monkeypatch.setattr(sandbox, "status",
                        lambda: {"running": True, "url": "http://127.0.0.1:8500/"})
    _, out = _nodes(root_client)
    assert out["conclusion"]["cluster"] == {"available": True, "running": True,
                                            "url": "http://127.0.0.1:8500/"}


def test_cluster_start_and_stop_delegate_to_the_sandbox_internals(root_client, monkeypatch):
    from app import sandbox
    calls = []

    def fake_start(source):
        calls.append(("start", source))
        return {"ok": True, "url": "http://127.0.0.1:8701/", "port": 8701,
                "commit": "abc1234", "ref": source, "pid": 1}

    def fake_stop():
        calls.append(("stop", None))
        return {"ok": True, "killed": True}
    monkeypatch.setattr(sandbox, "start", fake_start)
    monkeypatch.setattr(sandbox, "stop", fake_stop)
    r = root_client.post("/api/graph/self/cluster", json={"action": "start"})
    assert r.status_code == 200, r.text
    assert r.json()["running"] is True and r.json()["url"] == "http://127.0.0.1:8701/"
    assert calls[0] == ("start", "live"), \
        "the cluster verb is the SAME machinery as /api/self/sandbox, on the live tree"
    r = root_client.post("/api/graph/self/cluster", json={"action": "stop"})
    assert r.status_code == 200 and r.json()["running"] is False
    assert calls[-1] == ("stop", None)
    assert any(e["kind"] == "sandbox_started" for e in db.list_events(0))
    assert root_client.post("/api/graph/self/cluster",
                            json={"action": "reboot"}).status_code == 400


def test_a_box_that_cannot_sandbox_says_so(root_client, monkeypatch, tmp_path):
    from app import selfops
    monkeypatch.setattr(selfops, "LIVE_TREE", tmp_path)   # no devteam checkout here
    _, out = _nodes(root_client)
    c = out["conclusion"]["cluster"]
    assert c["available"] is False and c["running"] is False and c["reason"]
    r = root_client.post("/api/graph/self/cluster", json={"action": "start"})
    assert r.status_code == 409


# --------------------------------------------------------------------------
# the graph surface stays an operator power — new verbs included
# --------------------------------------------------------------------------

def test_every_new_verb_is_behind_the_operator_gate(client, make_user):
    uid, other = make_user("bystander2")
    for method, path, body in (
            ("get", "/api/graph/self/team", None),
            ("post", "/api/graph/self/team", {"world_id": 1, "room_id": 1}),
            ("get", "/api/graph/self/node/knowledge", None),
            ("post", "/api/graph/self/node/knowledge/service", {"action": "start"}),
            ("post", "/api/graph/self/node/knowledge/agent", {"agent_id": 1}),
            ("post", "/api/graph/self/node/knowledge/remove", None),
            ("post", "/api/graph/self/node/knowledge/replace",
             {"aspect": "tests", "note": ""}),
            ("post", "/api/graph/self/cluster", {"action": "start"})):
        r = getattr(other, method)(path, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, f"{path} answered {r.status_code} for a bystander"
    assert ct.FLEET_CALLS == [], "a bystander reached the fleet manager"
