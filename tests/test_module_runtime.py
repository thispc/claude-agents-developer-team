"""The module graph's RUNTIME verbs — health, service, team pool, mastery, remove,
replace, cluster. All offline, house style of test_module_graph.py: every drill is an
invariant that would rot silently — a heartbeat that stops probing, a stop button that
fakes, a pool that leaks assignment to strangers, a replan that amnesties mastery, a
"remove" that edits history.
"""

import asyncio
import json
import time

import pytest

from app import auth, config, db, modgraph, modgraph_author, modgraph_health as mh, repair, tuning


@pytest.fixture(autouse=True)
def _fresh_beats():
    """The heartbeat sweep is cached ~10s by design; a drill that flips a probe must
    not be answered from the previous drill's cache."""
    mh._BEATS.update(ts=0.0, keys=frozenset(), by_key={})
    yield
    mh._BEATS.update(ts=0.0, keys=frozenset(), by_key={})


def _nodes(root_client) -> dict:
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
    """A group is red if any child is red, else yellow if any yellow, else green —
    and its beat/tests aggregate the same way so the fields cannot disagree."""
    red = mh.health_of("fail", "none")
    yellow = mh.health_of("ok", "fail")
    green = mh.health_of("ok", "pass")
    assert mh.rollup([green, yellow, red])["status"] == "red"
    assert mh.rollup([green, yellow])["status"] == "yellow"
    assert mh.rollup([green, green]) == {"beat": "ok", "tests": "pass", "status": "green"}
    assert mh.rollup([])["status"] == "green"
    assert mh.rollup([green, red])["beat"] == "fail"
    assert mh.rollup([green, yellow])["tests"] == "fail"


def test_every_node_carries_health_and_a_healthy_tree_reads_green(root_client):
    by, _out = _nodes(root_client)
    for n in by.values():
        assert n["health"]["beat"] in ("ok", "fail")
        assert n["health"]["tests"] in ("pass", "fail", "none")
        assert n["health"]["status"] in ("green", "yellow", "red")
    # a working checkout with never-run suites: every beat ok, every status green
    assert all(n["health"]["beat"] == "ok" for n in by.values()), \
        {k: n["health"] for k, n in by.items() if n["health"]["beat"] != "ok"}
    assert all(n["health"]["status"] == "green" for n in by.values())
    assert by["knowledge"]["health"]["tests"] == "none", "never-run must say none, not pass"


def test_a_dead_beat_turns_the_node_and_its_layer_red(root_client, monkeypatch):
    """The probe registry is live wiring: a probe that fails (or raises — same thing)
    is a red node, and the rollup carries it to the layer without touching neighbours."""
    monkeypatch.setitem(mh.PROBES, "knowledge", lambda: False)
    mh._BEATS.update(ts=0.0)
    by, _ = _nodes(root_client)
    assert by["knowledge"]["health"] == {"beat": "fail", "tests": "none", "status": "red"}
    assert by["data"]["health"]["status"] == "red", "the layer must roll the red up"
    assert by["db"]["health"]["status"] == "green", "a sibling must not be smeared"
    assert by["backend"]["health"]["status"] == "green"

    def boom():
        raise RuntimeError("probe fell over")
    monkeypatch.setitem(mh.PROBES, "knowledge", boom)
    mh._BEATS.update(ts=0.0)
    by, _ = _nodes(root_client)
    assert by["knowledge"]["health"]["beat"] == "fail", "a raising probe IS a failed beat"


def test_a_red_suite_on_a_live_beat_is_yellow(root_client):
    plan = modgraph.active_plan(0)
    modgraph.update_test_result(plan["id"], "tests/test_knowledge_service.py", "failing", "boom")
    by, _ = _nodes(root_client)
    assert by["knowledge"]["health"] == {"beat": "ok", "tests": "fail", "status": "yellow"}
    assert by["data"]["health"]["status"] == "yellow"
    assert by["frontend"]["health"]["status"] == "green"


def test_the_probes_are_real_and_the_sweep_is_cached(root_client, monkeypatch):
    """shell's beat actually spawns a child; and within the TTL the sweep answers from
    cache — probing on every poll would spend a process spawn per poll for no truth."""
    from app import shell
    calls = []
    real = shell.sh

    def counting(*cmd, **kw):
        calls.append(cmd)
        return real(*cmd, **kw)
    monkeypatch.setattr(shell, "sh", counting)
    mh._BEATS.update(ts=0.0)
    _nodes(root_client)
    spawns = [c for c in calls if c == ("true",)]
    assert spawns, "the shell probe must actually spawn"
    n = len(spawns)
    _nodes(root_client)          # second poll inside the TTL
    assert len([c for c in calls if c == ("true",)]) == n, \
        "a poll inside the TTL must not re-probe"


# --------------------------------------------------------------------------
# service: real switches flip; everything else refuses honestly
# --------------------------------------------------------------------------

def test_the_payload_says_who_can_be_controlled_and_who_cannot(root_client):
    by, _ = _nodes(root_client)
    assert by["repair"]["service"] == {"state": "stopped", "control": True}
    assert by["selfrepair"]["service"] == {"state": "stopped", "control": True}
    assert by["ops"]["service"]["control"] is True         # upkeep_enabled defaults on
    assert by["ops"]["service"]["state"] == "running"
    assert by["lifeworld"]["service"]["state"] == "running"
    # CANVAS_V2 is env-read at import: no honest runtime switch exists
    c = by["canvas"]["service"]
    assert c["state"] == "none" and c["control"] is False and "CANVAS_V2" in c["reason"]
    # compiled-in modules: the exact reason the contract promises
    for key in ("db", "routes", "guards", "shell", "worker", "manager"):
        assert by[key]["service"] == {"state": "none", "control": False,
                                      "reason": mh.DEFAULT_REASON}, key


def test_service_start_stop_flips_the_crews_real_kv(root_client):
    """The repair node's switch IS repair.toggle: the same kv the engine's tick obeys."""
    assert not db.kv_get("repair:enabled")
    r = root_client.post("/api/graph/self/node/repair/service", json={"action": "start"})
    assert r.status_code == 200, r.text
    assert r.json()["service"] == {"state": "running", "control": True}
    assert db.kv_get("repair:enabled") is True, "the REAL knob must flip"
    by, _ = _nodes(root_client)
    assert by["repair"]["service"]["state"] == "running"
    assert by["selfrepair"]["service"]["state"] == "running"
    r = root_client.post("/api/graph/self/node/repair/service", json={"action": "stop"})
    assert r.status_code == 200 and r.json()["service"]["state"] == "stopped"
    assert db.kv_get("repair:enabled") is False


def test_service_flips_the_tuning_knobs_for_ops_and_lifeworld(root_client):
    r = root_client.post("/api/graph/self/node/ops/service", json={"action": "stop"})
    assert r.status_code == 200 and r.json()["service"]["state"] == "stopped"
    assert tuning.get("upkeep_enabled") is False
    root_client.post("/api/graph/self/node/ops/service", json={"action": "start"})
    assert tuning.get("upkeep_enabled") is True
    root_client.post("/api/graph/self/node/lifeworld/service", json={"action": "stop"})
    assert tuning.get("home_life_enabled") is False
    root_client.post("/api/graph/self/node/lifeworld/service", json={"action": "start"})
    assert tuning.get("home_life_enabled") is True


def test_a_module_with_no_switch_400s_with_the_reason_and_never_fakes(root_client):
    r = root_client.post("/api/graph/self/node/db/service", json={"action": "stop"})
    assert r.status_code == 400
    assert r.json()["detail"] == mh.DEFAULT_REASON
    r = root_client.post("/api/graph/self/node/canvas/service", json={"action": "stop"})
    assert r.status_code == 400 and "CANVAS_V2" in r.json()["detail"]
    by, _ = _nodes(root_client)
    assert by["db"]["service"]["state"] == "none", "a refused stop must change nothing"
    assert root_client.post("/api/graph/self/node/db/service",
                            json={"action": "sideways"}).status_code == 400
    assert root_client.post("/api/graph/self/node/nowhere/service",
                            json={"action": "stop"}).status_code == 404


# --------------------------------------------------------------------------
# the team pool: default = the crew; re-point acts on assignment, not sprints
# --------------------------------------------------------------------------

def _second_room(name="night shift"):
    from app.lifeworld import store
    from app.lifeworld_routes import ManifestAgent, ManifestBody, materialise_manifest
    w = store.create(1, name)
    s = materialise_manifest(w, ManifestBody(
        name="bench", agents=[ManifestAgent(name="Alia", brief="steady hands"),
                              ManifestAgent(name="Bo", brief="fresh eyes")]))
    store.save(w)
    return w, s


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
    w, s = _second_room()
    r = root_client.post("/api/graph/self/team", json={"world_id": w.id, "room_id": s.id})
    assert r.status_code == 200, r.text
    out = r.json()
    assert db.kv_get("graph:pool:0") == {"world_id": w.id, "room_id": s.id}
    assert {m["name"] for m in out["members"]} == {"Alia", "Bo"}
    assert all("factor" not in m for m in out["members"]), \
        "factors are a crew concept — a Studio pool has none"
    # DELIBERATELY independent: the crew's sprint team record is untouched
    assert repair.team() == info, "re-pointing the pool must not re-seat the crew"
    # and the new room is what GET now reports
    got = root_client.get("/api/graph/self/team").json()
    assert got["current"]["world_id"] == w.id and got["current"]["room_id"] == s.id
    assert root_client.post("/api/graph/self/team",
                            json={"world_id": 9999, "room_id": 1}).status_code == 404


def test_assignment_validates_pool_membership_and_refuses_groups(root_client):
    info = repair.ensure_team()
    w, s = _second_room("bench world")
    root_client.post("/api/graph/self/team", json={"world_id": w.id, "room_id": s.id})
    members = root_client.get("/api/graph/self/team").json()["members"]
    aid = members[0]["agent_id"]
    # entity ids are world-LOCAL, so a low crew id can collide with a bench id by
    # number — the drill needs a crew specialist whose id is genuinely outside the pool
    pool_ids = {m["agent_id"] for m in members}
    crew_agent = next(h for h in sorted(info["agents"].values(), reverse=True)
                      if h not in pool_ids)
    # a pool member lands on a leaf, into graph_assign
    r = root_client.post("/api/graph/self/node/knowledge/agent", json={"agent_id": aid})
    assert r.status_code == 200, r.text
    assert r.json()["agent"]["name"] == members[0]["name"]
    plan = modgraph.active_plan(0)
    assert modgraph.get_assign(plan["id"], "knowledge")["agent_id"] == aid
    # a crew specialist is NOT in this pool any more — membership is the gate
    r = root_client.post("/api/graph/self/node/db/agent", json={"agent_id": crew_agent})
    assert r.status_code == 400 and "pool" in r.json()["detail"]
    # groups (and the frame) refuse: work lands on leaves
    assert root_client.post("/api/graph/self/node/backend/agent",
                            json={"agent_id": aid}).status_code == 400
    assert root_client.post("/api/graph/self/node/aim/agent",
                            json={"agent_id": aid}).status_code == 400
    assert root_client.post("/api/graph/self/node/nowhere/agent",
                            json={"agent_id": aid}).status_code == 404


# --------------------------------------------------------------------------
# mastery: earned at 3, kept on ties, taken only by exceeding, replan-proof
# --------------------------------------------------------------------------

def _ok_run(pid: int, key: str, agent_id: int, kind: str = "build"):
    rid = modgraph.note_run(pid, key, kind, agent_id=agent_id, status="running")
    modgraph.close_run(rid, "ok", "done")
    return rid


def test_mastery_appears_at_three_verified_runs(root_client):
    plan = modgraph.active_plan(0)
    pid = plan["id"]
    _ok_run(pid, "knowledge", 71)
    _ok_run(pid, "knowledge", 71, kind="verify")
    by, _ = _nodes(root_client)
    m = by["knowledge"]["mastery"]
    assert m == {"agent_id": 71, "name": m["name"], "runs": 2, "master": False}
    assert by["db"]["mastery"] is None, "no runs is an honest null, not a zero-run master"
    _ok_run(pid, "knowledge", 71)
    assert _nodes(root_client)[0]["knowledge"]["mastery"]["master"] is True


def test_failed_and_still_open_runs_earn_nothing(root_client):
    plan = modgraph.active_plan(0)
    pid = plan["id"]
    rid = modgraph.note_run(pid, "db", "build", agent_id=71, status="running")
    modgraph.close_run(rid, "failed", "red suite")
    modgraph.note_run(pid, "db", "build", agent_id=71, status="running")   # never closed
    _ok_run(pid, "db", 71, kind="judge")                                    # wrong kind
    assert mh.mastery(0).get("db") is None


def test_a_challenger_must_exceed_the_master_not_merely_match(root_client):
    plan = modgraph.active_plan(0)
    pid = plan["id"]
    for _ in range(3):
        _ok_run(pid, "knowledge", 71)
    for _ in range(3):
        _ok_run(pid, "knowledge", 72)      # the challenger catches up…
    m = mh.mastery(0)["knowledge"]
    assert m["agent_id"] == 71 and m["master"] is True, \
        "a tie keeps the incumbent — later runs do not steal at equal count"
    _ok_run(pid, "knowledge", 72)          # …and now exceeds
    m = mh.mastery(0)["knowledge"]
    assert m["agent_id"] == 72 and m["runs"] == 4 and m["master"] is True


def test_mastery_survives_a_replan_by_node_key(root_client):
    v1 = modgraph.active_plan(0)["id"]
    for _ in range(3):
        _ok_run(v1, "knowledge", 71)
    # a replan: NEW version, same key — the old rows stay, the count carries
    v2 = modgraph.create_plan(0, authored_by="manager", notes="replanned")
    modgraph.add_node(v2, "aim", "aim", node_type="aim")
    modgraph.add_node(v2, "knowledge", "What agents have learned")
    modgraph.add_node(v2, "conclusion", "done", node_type="conclusion")
    modgraph.activate(v2)
    m = mh.mastery(0)["knowledge"]
    assert m == {"agent_id": 71, "runs": 3, "master": True}
    by, _ = _nodes(root_client)
    assert by["knowledge"]["mastery"]["master"] is True
    _ok_run(v2, "knowledge", 71)           # runs on the new version keep adding up
    assert mh.mastery(0)["knowledge"]["runs"] == 4


def test_the_authoring_pass_keeps_the_master_over_the_managers_pick(fresh_db, monkeypatch):
    """Mastery outranks reshuffling: the manager proposes Correctness for `engine`,
    but Speed's agent has three verified runs there — the assignment keeps Speed,
    and says so in the log."""
    auth.get_user_by_name(auth.ROOT_USERNAME) or auth.create_user(
        auth.ROOT_USERNAME, "pw-root", is_root=True)
    monkeypatch.setattr(config, "AUTH_CONFIGURED", True)
    monkeypatch.setattr(repair, "_root_settings", lambda: {})
    modgraph.init()
    seed_id = modgraph.seed_self_graph()
    info = repair.ensure_team()
    master_id = info["agents"]["speed"]
    assert master_id != info["agents"]["correctness"]
    for _ in range(3):
        _ok_run(seed_id, "engine", master_id)      # mastery is keyed by NODE KEY

    reply = json.dumps({
        "modules": [
            {"key": "aim", "title": "aim", "node_type": "aim", "spec": "a",
             "join": "all_of", "tags": [], "paths": []},
            {"key": "core", "title": "Core", "node_type": "group", "spec": "the core",
             "join": "all_of", "tags": [], "paths": []},
            {"key": "engine", "title": "Engine", "node_type": "code", "parent": "core",
             "spec": "the engine", "join": "all_of", "tags": [],
             "paths": ["conductor/app/repair.py"]},
            {"key": "conclusion", "title": "done", "node_type": "conclusion",
             "spec": "c", "join": "all_of", "tags": [], "paths": []},
        ],
        "edges": [],
        "assignments": {"engine": "correctness"},
    })
    from app import providers

    async def fake(provider, model, system, prompt, settings, max_tokens=2000):
        return reply
    monkeypatch.setattr(providers, "complete", fake)
    pid = asyncio.run(modgraph_author.author_self_plan())
    assert pid
    assert modgraph.get_assign(pid, "engine")["agent_id"] == master_id, \
        "the master keeps the node, whatever the manager proposed"
    from app import logs
    assert any(r["event"] == "graph_master_kept" for r in logs.rows())


# --------------------------------------------------------------------------
# remove: a NEW version minus the node; history untouched; refusals honest
# --------------------------------------------------------------------------

def test_remove_makes_a_new_version_and_never_edits_the_old(root_client):
    v1 = modgraph.active_plan(0)["id"]
    before = {t: db._rows(f"SELECT * FROM {t} WHERE plan_id=? ORDER BY id", (v1,))
              for t in ("graph_nodes", "graph_edges", "graph_node_tests")}
    modgraph.set_assign(v1, "db", agent_id=77)
    modgraph.save_positions(v1, {"db": [10, 20], "knowledge": [30, 40]})

    r = root_client.post("/api/graph/self/node/knowledge/remove")
    assert r.status_code == 200, r.text
    out = r.json()
    v2 = out["plan"]["id"]
    assert v2 != v1 and out["removed"] == "knowledge"
    assert out["plan"]["authored_by"] == "root", \
        "a human removal must outrank the seed, or the next boot resurrects the node"
    plan2 = modgraph.active_plan(0)
    assert plan2["id"] == v2
    keys = {n["key"] for n in modgraph.nodes(v2)}
    assert "knowledge" not in keys and "db" in keys
    # edges through the node dropped, everything else carried
    for e in modgraph.edges(v2):
        assert "knowledge" not in (e["src_key"], e["dst_key"])
    assert any(e["src_key"] == "db" and e["dst_key"] == "routes"
               for e in modgraph.edges(v2))
    # the parent layer's boundary follows the children that REMAIN
    by = {n["key"]: n for n in modgraph.nodes(v2)}
    assert by["data"]["paths"] == by["db"]["paths"]
    # no test rows for the gone node; the others carried
    assert modgraph.tests(v2, "knowledge") == []
    assert modgraph.tests(v2, "db"), "surviving suites must carry"
    # steering carried by key; the removed node's position dropped
    assert modgraph.get_assign(v2, "db")["agent_id"] == 77
    pos = modgraph.positions(v2)
    assert pos.get("db") == [10.0, 20.0] and "knowledge" not in pos
    # v1: byte-identical rows, only the plan's status moved
    for t, rows in before.items():
        assert db._rows(f"SELECT * FROM {t} WHERE plan_id=? ORDER BY id", (v1,)) == rows, \
            f"{t} rows of the superseded plan were edited"
    assert modgraph.get_plan(v1)["status"] == "superseded"
    # the flare and the note the manager will read next authoring
    assert any(e["kind"] == "graph_node_removed" for e in db.list_events(0))
    notes = db.kv_get("graph:notes:0") or []
    assert notes and "knowledge" in notes[-1]["note"]


def test_remove_refuses_the_frame_and_a_layer_with_children(root_client):
    for key, why in (("aim", "frame"), ("conclusion", "frame"), ("data", "children")):
        r = root_client.post(f"/api/graph/self/node/{key}/remove")
        assert r.status_code == 400, key
    assert "children" in root_client.post("/api/graph/self/node/data/remove").json()["detail"]
    assert root_client.post("/api/graph/self/node/nowhere/remove").status_code == 404
    # nothing changed: the seed plan is still the active one
    assert modgraph.active_plan(0)["authored_by"] == "seed"


def test_the_removal_note_reaches_the_managers_next_authoring_prompt(fresh_db, monkeypatch):
    auth.get_user_by_name(auth.ROOT_USERNAME) or auth.create_user(
        auth.ROOT_USERNAME, "pw-root", is_root=True)
    monkeypatch.setattr(config, "AUTH_CONFIGURED", True)
    monkeypatch.setattr(repair, "_root_settings", lambda: {})
    modgraph.init()
    modgraph.seed_self_graph()
    repair.ensure_team()
    db.kv_set("graph:notes:0", [{"ts": time.time(),
                                 "note": "The operator removed the module 'knowledge'."}])
    seen = {}
    from app import providers

    async def fake(provider, model, system, prompt, settings, max_tokens=2000):
        seen["prompt"] = prompt
        return "not json"                     # unusable answer: authoring changes nothing
    monkeypatch.setattr(providers, "complete", fake)
    assert asyncio.run(modgraph_author.author_self_plan()) is None
    assert "OPERATOR NOTES" in seen["prompt"] and "removed the module 'knowledge'" in seen["prompt"]
    assert db.kv_get("graph:notes:0"), \
        "an answer that never parsed did not READ the notes — they must survive"


# --------------------------------------------------------------------------
# replace: a real ticket through the self-issue machinery
# --------------------------------------------------------------------------

def test_replace_files_a_ticket_on_the_platforms_own_project(root_client):
    r = root_client.post("/api/graph/self/node/db/replace",
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
    assert "[graph] Replace the tests of module 'db'" in text
    assert "the suite is flaky" in text
    assert any(e["kind"] == "self_issue_raised" for e in db.list_events(proj))
    assert any(e["kind"] == "graph_replace_filed" for e in db.list_events(0))


def test_replace_validates_the_aspect_and_points_agent_elsewhere(root_client):
    r = root_client.post("/api/graph/self/node/db/replace",
                         json={"aspect": "agent", "note": ""})
    assert r.status_code == 400
    assert "/agent" in r.json()["detail"] or "agent endpoint" in r.json()["detail"]
    assert root_client.post("/api/graph/self/node/nowhere/replace",
                            json={"aspect": "tests", "note": ""}).status_code == 404


# --------------------------------------------------------------------------
# the conclusion's vitals and the cluster verbs
# --------------------------------------------------------------------------

def test_the_conclusion_carries_the_platforms_vitals(root_client, monkeypatch):
    from app import sandbox
    monkeypatch.setattr(sandbox, "status", lambda: {"running": False})
    _, out = _nodes(root_client)
    c = out["conclusion"]
    assert c["beat"] == "ok"
    assert isinstance(c["uptime_s"], int) and c["uptime_s"] >= 0
    assert c["boot_sha"] and c["head_sha"], "the shas are the honest code identity"
    assert c["cluster"] == {"available": True, "running": False}
    monkeypatch.setattr(sandbox, "status",
                        lambda: {"running": True, "url": "http://127.0.0.1:8500/"})
    _, out = _nodes(root_client)
    assert out["conclusion"]["cluster"] == {"available": True, "running": True,
                                            "url": "http://127.0.0.1:8500/"}


def test_a_failing_leaf_fails_the_conclusions_beat(root_client, monkeypatch):
    monkeypatch.setitem(mh.PROBES, "shell", lambda: False)
    mh._BEATS.update(ts=0.0)
    _, out = _nodes(root_client)
    assert out["conclusion"]["beat"] == "fail", \
        "the platform is not running if a piece of it is not"


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
            ("post", "/api/graph/self/node/db/service", {"action": "start"}),
            ("post", "/api/graph/self/node/db/agent", {"agent_id": 1}),
            ("post", "/api/graph/self/node/db/remove", None),
            ("post", "/api/graph/self/node/db/replace", {"aspect": "tests", "note": ""}),
            ("post", "/api/graph/self/cluster", {"action": "start"})):
        r = getattr(other, method)(path, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, f"{path} answered {r.status_code} for a bystander"
