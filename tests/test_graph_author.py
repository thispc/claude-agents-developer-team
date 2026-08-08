"""The team-authors-the-graph drills — package B of the Module Graph.

The owner's correction was "not hardcoded": the crew's manager authors the plan
and the crew's REAL work lights it up. Everything here runs offline with
injected fakes and asserts call counts — the house style of test_crew_loop.py —
because the invariants worth policing are spend invariants: authoring is ONE
bounded call, an identical answer writes nothing, a second sprint with the same
lineup spends zero, and the graph hooks may never cost a sprint anything.
"""

import asyncio
import inspect
import json
import subprocess
import time
from pathlib import Path

import pytest

from app import auth, config, db, modgraph, modgraph_author, repair, tuning
from app import repair_builder as rb


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from app import launcher
    launcher.COOLDOWN.clear()
    yield
    launcher.COOLDOWN.clear()


@pytest.fixture()
def tmp_repo(tmp_path, monkeypatch):
    from app import selfops
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    g("init", "-b", "main")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / "app.py").write_text("x = 1\n")
    (repo / ".gitignore").write_text(".repair/\n.venv/\n")
    (repo / ".venv").mkdir()
    g("add", "-A"); g("commit", "-m", "init")
    monkeypatch.setattr(selfops, "LIVE_TREE", repo)
    return repo


def _root_user():
    return auth.get_user_by_name(auth.ROOT_USERNAME) or auth.create_user(
        auth.ROOT_USERNAME, "pw-root", is_root=True)


def _go_live(monkeypatch):
    """Live enough for the manager's paths, with zero real spend possible."""
    monkeypatch.setattr(config, "AUTH_CONFIGURED", True)
    monkeypatch.setattr(repair, "_root_settings", lambda: {})


def _task(factor="correctness", title="Fix knowledge decay",
          brief="conductor/app/knowledge.py drops rows on decay"):
    return {"slug": repair._slug(title), "title": title, "factor": factor, "brief": brief,
            "type": "bug", "priority": "p2", "acceptance": [], "branch": "",
            "status": "pending", "worktree": None, "verification": None,
            "landed_sha": None, "attempts": 0, "evidence": ""}


def _author_reply(paths_extra=None):
    """A valid manager answer in the TWO-LEVEL shape: one group over two leaf
    modules. It also probes the server's distrust on purpose: a decoy paths
    list on the group (the real union is computed server-side) and an
    assignment on the group (leaves only — it must be discarded)."""
    return json.dumps({
        "modules": [
            {"key": "aim", "title": "devteam, replanned by its own manager",
             "node_type": "aim", "spec": "the aim", "join": "all_of", "tags": [], "paths": []},
            {"key": "core", "title": "The engine room", "node_type": "group",
             "spec": "the repair layer and what it knows", "join": "all_of",
             "tags": ["backend"],
             "paths": ["conductor/app/main.py"]},        # decoy: must be ignored
            {"key": "engine", "title": "The engine", "node_type": "code",
             "parent": "core",
             "spec": "the repair engine and its builder", "join": "all_of",
             "tags": ["backend"],
             "paths": ["conductor/app/repair.py", "conductor/app/repair_builder.py"]
                      + (paths_extra or [])},
            {"key": "memory", "title": "What the crew knows", "node_type": "code",
             "parent": "core",
             "spec": "the knowledge store", "join": "all_of", "tags": ["backend"],
             "paths": ["conductor/app/knowledge.py"]},
            {"key": "conclusion", "title": "The running platform",
             "node_type": "conclusion", "spec": "the deliverable", "join": "all_of",
             "tags": [], "paths": []},
        ],
        "edges": [
            {"src": "aim", "dst": "core", "type": "depends", "contract": {}},
            {"src": "memory", "dst": "engine", "type": "data",
             "contract": {"rule": "lessons reach builds only via knowledge.recall"}},
            {"src": "core", "dst": "conclusion", "type": "depends", "contract": {}},
        ],
        "assignments": {"engine": "correctness", "memory": "reusability",
                        "core": "speed"},
    })


def _author_fake(reply):
    calls = []

    async def fake(provider, model, system, prompt, settings, max_tokens=2000, source=""):
        calls.append({"system": system, "prompt": prompt})
        return reply
    return fake, calls


def _author_calls(calls):
    return [c for c in calls if c["system"] == modgraph_author.AUTHOR_SYSTEM]


async def _run_sprint(monkeypatch, tmp_repo, tasks, prompts, review=False):
    """test_crew_loop's harness: seed the backlog, tick until the engine rests."""
    tuning.set("repair_review", review)
    tuning.set("repair_tasks_per_sprint", len(tasks))

    async def fake_sdk(system_prompt, prompt, cwd, tools, max_turns, model, env, **kw):
        prompts.append({"system": system_prompt, "tools": list(tools), "kw": kw})
        (Path(cwd) / "fixed.txt").write_text(f"done {len(prompts)}\n")
        return "done", 0.0
    monkeypatch.setattr(rb, "_run_sdk", fake_sdk)

    async def fake_verify(wt):
        return {"ran": True, "ok": True, "headline": "42 passed", "failures": []}
    monkeypatch.setattr(rb, "verify", fake_verify)

    repair.save_backlog({"ts": time.time(), "digest": "seeded", "tasks": list(tasks)})
    repair.toggle(True)
    st = repair.state()
    if st.get("phase") == "sleeping":
        repair.set_state(phase="idle", sleep_until=0, sleep_reason="", sleep_kind="",
                         resume_phase="")
    for _ in range(24):
        await repair.tick()
        st = repair.state()
        if st["phase"] == "sleeping":
            break
    repair.toggle(False)
    return repair.sprint(int(db.kv_get("repair:seq") or 0))


# --------------------------------------------------------------------------
# (a) the authoring drill: one call, a manager version, topo reveal, no-op twice
# --------------------------------------------------------------------------

def test_the_manager_authors_the_plan_once_and_identical_is_a_noop(fresh_db, monkeypatch):
    """The seed is superseded by a plan the MANAGER authored, each node assigned
    to a living specialist, revealed in topological order — and the exact same
    answer a second time writes nothing, because a 'new version' with the same
    bytes is history-noise pretending to be work."""
    _root_user(); _go_live(monkeypatch)
    modgraph.init()
    seed_id = modgraph.seed_self_graph()
    info = repair.ensure_team()
    from app import providers
    fake, calls = _author_fake(_author_reply())
    monkeypatch.setattr(providers, "complete", fake)

    pid = asyncio.run(modgraph_author.author_self_plan())
    assert pid and pid != seed_id
    assert len(calls) == 1, "authoring is ONE bounded call"
    plan = modgraph.active_plan(0)
    assert plan["id"] == pid and plan["authored_by"] == "manager"
    assert modgraph.get_plan(seed_id)["status"] == "superseded"
    keys = [n["key"] for n in modgraph.nodes(pid)]
    assert keys == ["aim", "core", "engine", "memory", "conclusion"]

    # the two-level shape, held structurally: the group tops out, the leaves
    # hang from it, and the group's boundary is the COMPUTED union of its
    # children's — the decoy path the manager wrote on the group was ignored
    by = {n["key"]: n for n in modgraph.nodes(pid)}
    assert by["core"]["node_type"] == "group" and by["core"]["parent_key"] == ""
    assert by["engine"]["parent_key"] == "core" and by["memory"]["parent_key"] == "core"
    assert by["core"]["paths"] == sorted(set(by["engine"]["paths"])
                                         | set(by["memory"]["paths"]))
    assert "conductor/app/main.py" not in by["core"]["paths"]

    # assignments: factor -> the specialist's living human id, via the crew
    # record — and on LEAVES only, the one aimed at the group was discarded
    assert modgraph.get_assign(pid, "engine")["agent_id"] == info["agents"]["correctness"]
    assert modgraph.get_assign(pid, "memory")["agent_id"] == info["agents"]["reusability"]
    assert modgraph.get_assign(pid, "core") is None, \
        "an assignment on a group must be discarded — a layer is worked through its children"

    # the test mapping is mechanical, not authored: knowledge tests land on the
    # LEAF `memory`; the group holds no rows of its own — it answers by rollup
    mapped = {t["path"] for t in modgraph.tests(pid, "memory")}
    assert "tests/test_knowledge_service.py" in mapped
    assert modgraph.tests(pid, "core") == []

    # the reveal: one planned event per node in topo order, then the ready flare
    evs = [e for e in db.list_events(0)
           if e["kind"] in ("graph_node_planned", "graph_plan_ready")]
    kinds = [e["kind"] for e in evs]
    assert kinds == ["graph_node_planned"] * len(keys) + ["graph_plan_ready"]
    order = [json.loads(e["payload"])["key"] for e in evs[:-1]]
    idx = {k: i for i, k in enumerate(order)}
    assert order[0] == "aim" and order[-1] == "conclusion"
    assert idx["memory"] < idx["engine"], "an edge's src must reveal before its dst"
    assert (db.kv_get("graph:authored:0") or {}).get("factors") == \
        sorted(f["id"] for f in repair.enabled_factors())

    # identical answer -> no writes, no events, version unchanged; still one call
    n_plans = db._rows("SELECT COUNT(*) AS n FROM graph_plans")[0]["n"]
    calls.clear()
    pid2 = asyncio.run(modgraph_author.author_self_plan())
    assert pid2 == pid and len(calls) == 1
    assert db._rows("SELECT COUNT(*) AS n FROM graph_plans")[0]["n"] == n_plans
    ready = [e for e in db.list_events(0) if e["kind"] == "graph_plan_ready"]
    assert len(ready) == 1, "an unchanged plan must not re-fire the reveal"


def test_invented_paths_are_dropped_with_a_log(fresh_db, monkeypatch):
    """The manager may regroup and rename; it does not get to invent files. A
    paths entry that is not in the tree is dropped and said out loud."""
    _root_user(); _go_live(monkeypatch)
    modgraph.init(); modgraph.seed_self_graph(); repair.ensure_team()
    from app import providers
    fake, calls = _author_fake(_author_reply(paths_extra=["conductor/app/imaginary.py"]))
    monkeypatch.setattr(providers, "complete", fake)
    pid = asyncio.run(modgraph_author.author_self_plan())
    node = next(n for n in modgraph.nodes(pid) if n["key"] == "engine")
    assert "conductor/app/imaginary.py" not in node["paths"]
    assert "conductor/app/repair.py" in node["paths"], "valid siblings must survive the drop"
    from app import logs
    assert any(r["event"] == "graph_paths_dropped" for r in logs.rows())


def test_a_parent_that_is_not_a_group_is_cleared(fresh_db, monkeypatch):
    """Two levels means TWO: a leaf claiming another leaf as its parent, or a
    group claiming a parent of its own, flattens to top level rather than
    minting a hierarchy the canvas has no idea how to clip — and the group's
    computed union then covers only the children that remain."""
    _root_user(); _go_live(monkeypatch)
    modgraph.init(); modgraph.seed_self_graph(); repair.ensure_team()
    reply = json.loads(_author_reply())
    assert reply["modules"][1]["key"] == "core" and reply["modules"][3]["key"] == "memory"
    reply["modules"][1]["parent"] = "aim"          # a group must not nest
    reply["modules"][3]["parent"] = "engine"       # a leaf is nobody's parent
    from app import providers
    fake, _calls = _author_fake(json.dumps(reply))
    monkeypatch.setattr(providers, "complete", fake)
    pid = asyncio.run(modgraph_author.author_self_plan())
    by = {n["key"]: n for n in modgraph.nodes(pid)}
    assert by["core"]["parent_key"] == ""
    assert by["memory"]["parent_key"] == ""
    assert by["engine"]["parent_key"] == "core", "the valid parent must survive"
    assert by["core"]["paths"] == sorted(by["engine"]["paths"]), \
        "the union must follow the children that actually remain"


def test_assignments_by_factor_name_map_back_to_ids(fresh_db, monkeypatch):
    """Validator loosening, pinned: the roster is shown to the model as
    "id: Name — brief", and it answers with the NAME often enough that dropping
    those threw away real assignments. A name (or a differently-cased id) maps
    back to its id; a value matching nothing on the roster still dies."""
    _root_user(); _go_live(monkeypatch)
    modgraph.init(); modgraph.seed_self_graph()
    info = repair.ensure_team()
    reply = json.loads(_author_reply())
    reply["assignments"] = {"engine": "Correctness",       # the display name
                            "memory": "REUSABILITY",       # a shouted id
                            "core": "Speed"}               # on a group: still discarded
    from app import providers
    fake, _calls = _author_fake(json.dumps(reply))
    monkeypatch.setattr(providers, "complete", fake)
    pid = asyncio.run(modgraph_author.author_self_plan())
    assert modgraph.get_assign(pid, "engine")["agent_id"] == info["agents"]["correctness"]
    assert modgraph.get_assign(pid, "memory")["agent_id"] == info["agents"]["reusability"]
    assert modgraph.get_assign(pid, "core") is None, \
        "leaves-only still holds — a name that maps is not a licence to staff a layer"

    # junk is junk: a value on nobody's roster is discarded, not guessed at
    # (the spec is reworded so the answer differs — an identical answer would be
    # the no-op path and never re-write assignments at all)
    reply["assignments"] = {"engine": "not-a-lens"}
    reply["modules"][2]["spec"] = "the repair engine and its builder, reworded"
    fake2, _c2 = _author_fake(json.dumps(reply))
    monkeypatch.setattr(providers, "complete", fake2)
    pid2 = asyncio.run(modgraph_author.author_self_plan())
    assert pid2 != pid, "the reworded answer must be a new version"
    assert modgraph.get_assign(pid2, "engine") is None


def test_a_seat_completion_offers_no_tools_at_all():
    """The replan-409 root cause, pinned at source: with only disallowed_tools the
    CLI still advertised its remaining built-ins and the box's own MCP servers,
    and the model sometimes spent its single turn on a tool call — dying as
    error_max_turns instead of answering. tools=[] plus strict_mcp_config makes
    "one bounded completion" structural, not a hope."""
    from app import providers
    src = inspect.getsource(providers._anthropic)
    assert "max_turns=1" in src
    assert "tools=[]" in src, "a seat must advertise NO tools"
    assert "strict_mcp_config=True" in src, \
        "the box's own MCP config must never leak into a seat"


def test_offline_authors_nothing(fresh_db, monkeypatch):
    """The runs-offline-free invariant: no credentials -> None, zero calls, zero
    writes, the seed stays in charge, no staleness stamp is minted."""
    monkeypatch.setattr(config, "AUTH_CONFIGURED", False)
    modgraph.init()
    seed_id = modgraph.seed_self_graph()
    from app import providers
    fake, calls = _author_fake(_author_reply())
    monkeypatch.setattr(providers, "complete", fake)
    assert asyncio.run(modgraph_author.author_self_plan()) is None
    assert calls == []
    plan = modgraph.active_plan(0)
    assert plan["id"] == seed_id and plan["authored_by"] == "seed"
    assert db.kv_get("graph:authored:0") is None


def test_the_replan_endpoint_refuses_offline_with_a_reason(root_client, monkeypatch):
    # Pinned, not assumed: a developer machine's `claude` CLI login makes
    # AUTH_CONFIGURED true for a reason this test is not about (conftest's rule).
    monkeypatch.setattr(config, "AUTH_CONFIGURED", False)
    r = root_client.post("/api/graph/self/replan")
    assert r.status_code == 409
    assert "offline" in r.json()["detail"]


# --------------------------------------------------------------------------
# (b) the engine hook: the specialist's real build lights the owning node
# --------------------------------------------------------------------------

def test_a_build_lights_the_node_that_owns_its_paths(fresh_db, tmp_repo, monkeypatch):
    """The highlight source is real work: a sprint task whose brief names
    conductor/app/knowledge.py opens a running build row on the `knowledge`
    node, closes it ok when the session ends, and adds a verify row when the
    suite speaks — with graph_node_active/idle riding the pid-0 feed."""
    _root_user(); _go_live(monkeypatch)
    modgraph.init(); modgraph.seed_self_graph()
    prompts = []
    rec = asyncio.run(_run_sprint(monkeypatch, tmp_repo, [_task()], prompts))
    assert rec and rec["tasks"][0]["status"] == "landed", rec and rec["tasks"]

    plan = modgraph.active_plan(0)
    rows = modgraph.runs(plan["id"], "knowledge")
    builds = [r for r in rows if r["kind"] == "build"]
    assert builds, "the build never reached the graph trace"
    assert builds[-1]["status"] == "ok" and builds[-1]["ended_at"], \
        "the run must go running -> closed ok"
    assert builds[-1]["agent_id"] == repair.team()["agents"]["correctness"], \
        "the run must name the specialist that did the work"
    verifies = [r for r in rows if r["kind"] == "verify"]
    assert verifies and verifies[-1]["status"] == "ok", "green verify must add its row"

    evs = {e["kind"]: e for e in db.list_events(0)}
    assert "graph_node_active" in evs and "graph_node_idle" in evs
    assert json.loads(evs["graph_node_active"]["payload"])["key"] == "knowledge"
    assert json.loads(evs["graph_node_idle"]["payload"])["key"] == "knowledge"


def test_a_build_death_closes_the_run_failed_and_never_raises(fresh_db, tmp_repo,
                                                              monkeypatch):
    """The graph must NEVER break a sprint: a session that dies closes its run
    as failed, the glow goes out, and the engine walks on to its rest."""
    _root_user(); _go_live(monkeypatch)
    modgraph.init(); modgraph.seed_self_graph()
    tuning.set("repair_review", False)
    tuning.set("repair_tasks_per_sprint", 1)

    async def dying_sdk(*a, **kw):
        raise RuntimeError("boom, the SDK fell over mid-build")
    monkeypatch.setattr(rb, "_run_sdk", dying_sdk)

    async def run():
        repair.save_backlog({"ts": time.time(), "digest": "d", "tasks": [_task()]})
        repair.toggle(True)
        if repair.state().get("phase") == "sleeping":
            repair.set_state(phase="idle", sleep_until=0, sleep_reason="",
                             sleep_kind="", resume_phase="")
        for _ in range(24):
            await repair.tick()
            if repair.state()["phase"] == "sleeping":
                break
        repair.toggle(False)
        return repair.sprint(int(db.kv_get("repair:seq") or 0))

    rec = asyncio.run(run())
    assert rec["tasks"][0]["status"] == "failed"
    assert repair.state()["phase"] == "sleeping", "the sprint must end at rest, not crash"
    plan = modgraph.active_plan(0)
    builds = [r for r in modgraph.runs(plan["id"], "knowledge") if r["kind"] == "build"]
    assert builds and builds[-1]["status"] == "failed" and builds[-1]["ended_at"], \
        "a dead session must close its run as failed, not leave it running forever"
    assert any(e["kind"] == "graph_node_idle" for e in db.list_events(0)), \
        "the glow must go out even on a death"


# --------------------------------------------------------------------------
# (c) staleness: one authoring call per LINEUP, not per sprint
# --------------------------------------------------------------------------

def test_a_second_sprint_with_the_same_factors_spends_no_authoring_call(fresh_db,
                                                                        monkeypatch):
    """The engine's trigger rides the kv stamp: the first plan phase authors the
    graph (one call); the next sprint with an unchanged roster spends zero; a
    changed lineup makes it stale again."""
    _root_user(); _go_live(monkeypatch)
    modgraph.init(); modgraph.seed_self_graph()
    # keep the plan phase free of the lifeworld: no deliberation, no extraction —
    # the only completion in the whole sprint is the authoring call (or not).
    monkeypatch.setattr(repair, "ensure_team", lambda: None)

    async def fake_scout(factors, settings):
        return {"digest": "quiet", "candidates": [], "model": "", "usd": 0}
    monkeypatch.setattr(rb, "scout", fake_scout)
    from app import providers
    fake, calls = _author_fake(_author_reply())
    monkeypatch.setattr(providers, "complete", fake)

    async def one_sprint():
        repair.toggle(True)
        if repair.state().get("phase") == "sleeping":
            repair.set_state(phase="idle", sleep_until=0, sleep_reason="",
                             sleep_kind="", resume_phase="")
        for _ in range(24):
            await repair.tick()
            if repair.state()["phase"] == "sleeping":
                break
        repair.toggle(False)

    asyncio.run(one_sprint())
    assert len(_author_calls(calls)) == 1, "the first plan phase authors exactly once"
    assert modgraph.active_plan(0)["authored_by"] == "manager"

    repair.save_backlog({"ts": 0, "digest": "", "tasks": []})    # force scout+plan again
    calls.clear()
    asyncio.run(one_sprint())
    assert _author_calls(calls) == [], \
        "a second sprint with the same factors must spend zero authoring calls"

    repair.set_factors(patch=[{"id": "speed", "enabled": False}])
    assert modgraph_author.should_author() is True, "a changed lineup is stale again"


# --------------------------------------------------------------------------
# (d) the no-LLM pin holds: authorship lives OUTSIDE the store
# --------------------------------------------------------------------------

def test_authorship_lives_outside_the_store_and_inside_the_plan_phase():
    """The research verdict stays locked in: modgraph.py is still a model-free
    store; the one completion lives in modgraph_author; and the engine's trigger
    sits where the plan says — the END of the plan phase, staleness-checked."""
    src = inspect.getsource(modgraph)
    assert "providers." not in src
    assert "complete(" not in src
    assert "claude_agent_sdk" not in src
    asrc = inspect.getsource(modgraph_author)
    assert "providers.complete" in asrc, "the ONE bounded call lives in the author"
    psrc = inspect.getsource(repair._phase_plan)
    assert "modgraph_author" in psrc and "should_author" in psrc, \
        "the engine must author at the end of its plan phase, staleness-checked"
    bsrc = inspect.getsource(repair._phase_build)
    assert "_graph_build_started" in bsrc and "_graph_build_ended" in bsrc
    assert "_graph_verified" in inspect.getsource(repair._phase_verify)
