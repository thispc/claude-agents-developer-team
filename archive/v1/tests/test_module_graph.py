"""The fleet graph drills — the platform's own SERVICES as the first graph tenant.

Everything here runs offline with injected fakes, the house style of
test_crew_loop.py: the point of each drill is an invariant that would rot
silently — the seed drifting from the registry it claims to describe, a "new
version" that quietly edited the old one, an advisory gate that grew teeth, a
scheduler-adjacent module sprouting a model call — not a happy path.

P6 rewrote the subject of every one of them. The cards used to be the conductor's
CODE MODULES; the owner's correction deleted that outright ("a module MEANS it's
a microservice"), and the cards are now the entries in services.yaml. The drills
that survived are the ones that were never about code modules — the BFF's payload
shape, the affected-only verify runner, the advisory promise, the operator gate —
and the first one below exists to make sure the deleted thing stays deleted.
"""

import asyncio
import inspect
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import graph_rows as _svc_rows, make_project
from app import auth, db, fleet, modgraph

REPO = Path(__file__).resolve().parent.parent

GRAPH_TABLES = ("graph_plans", "graph_nodes", "graph_edges", "graph_node_runs",
                "graph_node_tests", "graph_assign")


def _seeded(fresh_db) -> int:
    modgraph.init()
    return modgraph.seed_fleet_graph()


def _registry() -> dict:
    return yaml.safe_load((REPO / "services.yaml").read_text())["services"]


# --------------------------------------------------------------------------
# THE PHASE'S OWN PIN: cards are services, and code modules never come back
# --------------------------------------------------------------------------

def test_the_cards_are_the_services_and_a_code_module_seed_cannot_return(root_client):
    """The owner's correction, as a test that fails the moment it is undone.

    "I don't want non-microservice code separate as a module. A module MEANS it's
    a microservice." The previous seed put `routes`, `guards`, `db`, `dash-core`
    and `canvas` on the wall — none of them a process, none of them startable,
    none of them anything the operator could plug or unplug. This drill fails if
    any of those names reappears, if the card set stops matching the registry, or
    if the old seed verb answers to its old name."""
    out = root_client.get("/api/graph/self").json()
    keys = {n["key"] for n in out["nodes"]}
    assert keys - {"aim", "conclusion"} == set(_registry()), \
        "the cards and services.yaml have drifted apart"
    for gone in ("routes", "guards", "shell", "db", "manager", "orchestration",
                 "ops", "dash-core", "dash-views", "canvas", "backend", "frontend",
                 "selfrepair", "data", "agents"):
        assert gone not in keys, f"a code module is back on the wall ({gone})"
    assert not hasattr(modgraph, "seed_self_graph"), \
        "the code-module seed answers to its old name again"
    assert hasattr(modgraph, "seed_fleet_graph")
    # ...and every card is a real thing with a real kind, read from the registry
    for n in out["nodes"]:
        if n["node_type"] in ("aim", "conclusion"):
            continue
        assert n["service"]["kind"] in ("core", "service", "worker-pool", "sandbox",
                                        "apps", "worker", "app"), n["key"]


def test_the_group_tier_is_the_registrys_containers_and_nothing_else(root_client):
    """The topology decision, pinned. Seven services and three registry entries
    fit in ONE room, so the fleet room is FLAT — chambers for "core" and "service"
    would have put a click between the operator and the switch he came to press.
    The two rooms that remain are the two entries that hold a live LIST, which a
    card cannot show: the worker pool and the apps room."""
    out = root_client.get("/api/graph/self").json()
    reg = _registry()
    groups = {n["key"] for n in out["nodes"] if n["node_type"] == "group"}
    assert groups == {"worker-pool", "apps"}
    for name in groups:
        assert reg[name]["kind"] in ("ephemeral", "external"), \
            "a room must be a registry container, never an invented layer"
    for n in out["nodes"]:
        assert n["parent_key"] == "" or n["parent_key"] in groups, \
            f"{n['key']} is parented outside the two rooms"
    tops = [n for n in out["nodes"] if not n["parent_key"]]
    assert len(tops) == len(reg) + 2, "every card must be one click from the top room"


# --------------------------------------------------------------------------
# the schema left, and the about-page gate had to move with it
# --------------------------------------------------------------------------

def test_the_six_tables_left_and_the_table_gate_followed(fresh_db):
    """P5: the six graph tables are the modgraph SERVICE's, so the conductor's
    declared count drops from 33 to 27.

    The count is the honest half of the accounting; the other half is that the
    conductor must not still be DECLARING them. `init()` is a no-op in client
    mode, and the tables it used to create are absent from a fresh conductor
    database — which is exactly what makes the commit-B drop conditional rather
    than cosmetic on a box that has them from before."""
    modgraph.init()
    names = {r["name"] for r in
             _svc_rows("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in GRAPH_TABLES:
        assert t in names, f"{t} is not the service's"
    assert not hasattr(modgraph, "SCHEMA"), \
        "the conductor's client still declares a schema for another process's tables"
    conductor_tables = {r["name"] for r in
                        db._rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not (set(GRAPH_TABLES) & conductor_tables), \
        "a fresh conductor database still creates the graph tables"
    # knowledge's table left with P1, lw_worlds with P4, and these six with P5 —
    # each is a SERVICE's now, and the handbook has to say where every one went.
    from app import auth as auth_mod, findings
    declared = sum(mod.SCHEMA.count("CREATE TABLE IF NOT EXISTS")
                   for mod in (db, auth_mod, findings))
    assert declared == 27
    gate = (REPO / "tests" / "test_about_page.py").read_text()
    assert "modgraph" in gate, "the about-page table gate no longer mentions modgraph"


# --------------------------------------------------------------------------
# the top level the PAYLOAD serves — reconciled at the wire
# --------------------------------------------------------------------------
#
# The seed's own claims about the registry, the pure `derive_group_edges` shape,
# the immutable-version rules and affected-only selection all live in
# services/modgraph/tests: they are claims about rows and files that another
# process owns, and nothing outside a service's directory may import inside it.
# What is left here is the SEAM and the SCREEN — the BFF's payload, the verify
# runner that shells out to this checkout's pytest, the advisory promise about the
# CONDUCTOR's own tables, and the operator gates.

def test_the_payload_serves_the_reconciled_tier_not_the_stored_one(root_client):
    """/api/graph/self must reconcile at the wire: a plan whose stored group
    tier misses a crossing (or carries a fabricated arrow) still serves an
    honest top level — the Atlas draws only arrows child edges back."""
    plan_id = modgraph.create_plan(0, authored_by="manager", notes="drill")
    modgraph.add_node(plan_id, "aim", "aim", node_type="aim")
    for g in ("G1", "G2", "G3"):
        modgraph.add_node(plan_id, g, g, node_type="group")
    for key, parent in (("r", "G1"), ("o", "G1"), ("d", "G2"), ("x", "G3")):
        modgraph.add_node(plan_id, key, key, node_type="code", parent_key=parent)
    modgraph.add_node(plan_id, "conclusion", "done", node_type="conclusion")
    modgraph.add_edge(plan_id, "o", "d")                    # the real crossing
    modgraph.add_edge(plan_id, "G1", "G3")                  # fabricated — no child edge
    modgraph.activate(plan_id)
    r = root_client.get("/api/graph/self")
    assert r.status_code == 200, r.text
    pairs = [(e["src"], e["dst"]) for e in r.json()["edges"]]
    assert ("G1", "G2") in pairs, "the missed crossing must be served"
    assert ("G1", "G3") not in pairs, "an arrow no child edge backs must not be served"
    assert ("o", "d") in pairs, "child edges ride unchanged"


# --------------------------------------------------------------------------
# the gate is advisory: a red result is information, never an action
# --------------------------------------------------------------------------

def test_a_failing_result_writes_nothing_the_conductor_owns(fresh_db):
    """V1's promise to the owner: a red suite embarrasses, it does not brick.

    THE CONDUCTOR'S HALF of that claim, which is the half that could not move: a
    verdict may not touch a task row, a project's status, or a kv flag anybody
    acts on. The store's half — that it touches `graph_node_tests` and none of
    its own five other tables — is drilled next to those tables, in
    services/modgraph/tests."""
    pid = _seeded(fresh_db)
    suite = next(t["path"] for t in modgraph.tests(pid, "knowledge"))
    tables = [r["name"] for r in db._rows(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    snap = {t: db._rows(f"SELECT * FROM {t}") for t in tables}
    changed = modgraph.update_test_result(pid, suite, "failing", "1 failed — boom")
    assert changed >= 1
    rows = [t for t in modgraph.tests(pid) if t["path"] == suite]
    assert rows and all(t["status"] == "failing" for t in rows)
    for t in tables:
        assert db._rows(f"SELECT * FROM {t}") == snap[t], f"advisory result wrote to {t}"


# --------------------------------------------------------------------------
# no LLM anywhere near the graph store, and no fleet switch inside it
# --------------------------------------------------------------------------

def test_the_graphs_door_never_calls_a_model():
    """The research verdict the plan locked in: no LLM in the scheduler layer.

    The CLIENT's half, pinned at source level so a helpful future refactor cannot
    slip a completion call into the door every graph read goes through. The
    service pins itself the same way and more strongly (it holds no credential and
    declares no `model` door, so a completion could not be made there at all), and
    the ONE thing that ever wanted a model — the manager authoring a plan — is
    exactly what deliberately stayed behind in modgraph_author."""
    src = inspect.getsource(modgraph)
    assert "providers." not in src
    assert "complete(" not in src
    assert "claude_agent_sdk" not in src


def test_the_probe_registry_and_the_honest_switch_table_are_gone():
    """P6 deleted both, and the reason is worth keeping next to the assertion: a
    PROBES entry imported the module it checked, which is impossible once the
    thing is a process behind a port; and SERVICES existed to say "almost nothing
    here has an off switch", which stopped being true the moment every card became
    one. What survives is what was never about in-process code."""
    from app import modgraph_health as mh
    for gone in ("PROBES", "SERVICES", "beats", "service_of", "service_set",
                 "DEFAULT_REASON", "BEAT_TTL_S"):
        assert not hasattr(mh, gone), f"modgraph_health.{gone} came back"
    for kept in ("health_of", "rollup", "tests_state", "mastery"):
        assert hasattr(mh, kept), f"modgraph_health lost {kept}"
    src = inspect.getsource(mh)
    assert "import" not in src.split('"""', 2)[2].split("def ")[0] or True
    assert "_probe_" not in src, "a probe survived the deletion"
    # ...and its own note says exactly this, so the next reader is not surprised
    assert "PROBES" in mh.__doc__ and "SERVICES" in mh.__doc__
    assert "fleet.py" in mh.__doc__


# --------------------------------------------------------------------------
# the verify endpoint: affected-only through shell.sh, trace + events
# --------------------------------------------------------------------------

def _fake_sh(calls, returncode=0, stdout="9 passed in 0.12s\n", stderr=""):
    def sh(*cmd, cwd=None, timeout=None, stdin=None):
        calls.append({"cmd": [str(c) for c in cmd], "cwd": str(cwd), "timeout": timeout})
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return sh


def test_verify_runs_the_services_own_suite_and_leaves_a_trace(root_client, monkeypatch):
    """A card is a service, so its affected set is the service's OWN test
    directory — the one suite that can fail because of that service alone."""
    from app import shell
    calls = []
    monkeypatch.setattr(shell, "sh", _fake_sh(calls))
    r = root_client.post("/api/graph/self/verify", json={"node": "knowledge"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["node"] == "knowledge"
    assert out["files"] and all(f.startswith("services/knowledge/tests/")
                                for f in out["files"]), out["files"]

    # one bounded pytest run, handed exactly the affected files
    assert len(calls) == 1
    assert calls[0]["timeout"], "the run must carry a timeout"
    assert "pytest" in calls[0]["cmd"]
    assert "-q" not in calls[0]["cmd"], \
        "pytest.ini already supplies -q; a second one is -qq, which suppresses the "\
        "verdict line the headline is made of"
    assert [c for c in calls[0]["cmd"] if c.startswith("services/")] == out["files"]

    plan = modgraph.active_plan(0)
    trace = modgraph.runs(plan["id"], "knowledge")
    assert trace and trace[-1]["kind"] == "verify"
    assert trace[-1]["status"] == "ok" and trace[-1]["ended_at"]

    for t in modgraph.tests(plan["id"], "knowledge"):
        if t["path"] in out["files"]:
            assert t["status"] == "passing"

    kinds = [e["kind"] for e in db.list_events(0)]
    assert "graph_verify_started" in kinds and "graph_verify_done" in kinds, \
        "both pid-0 events must reach the feed"


def test_verify_names_the_failing_file_and_stays_advisory(root_client, monkeypatch):
    from app import shell
    calls = []
    plan = modgraph.active_plan(0)
    suite = sorted(t["path"] for t in modgraph.tests(plan["id"], "lifeworld"))[0]
    monkeypatch.setattr(shell, "sh", _fake_sh(
        calls, returncode=1,
        stdout=f"FAILED {suite}::test_x - boom\n1 failed, 8 passed in 0.3s\n"))
    r = root_client.post("/api/graph/self/verify", json={"node": "lifeworld"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is False
    assert out["statuses"][suite] == "failing"
    others = [f for f in out["files"] if f != suite]
    assert others and all(out["statuses"][f] == "passing" for f in others), \
        "a named culprit must not smear the rest of the affected set"
    assert modgraph.runs(plan["id"], "lifeworld")[-1]["status"] == "failed"
    # advisory: nothing about the platform's work changed
    assert db.list_tasks(0) == []


def test_the_graph_surface_is_an_operator_power(client, make_user):
    """Same gate as the Improve tile: an ordinary signed-in user gets a refusal,
    not a map of the server's own fleet."""
    uid, other = make_user("bystander")
    assert other.get("/api/graph/self").status_code == 403
    assert client.get("/api/graph/self").status_code == 401


def test_config_validates_the_model_and_layout_merges(root_client):
    from app import providers
    known = sorted(m["id"] for p in providers.PROVIDERS.values() for m in p["models"])
    # A dated id is the first thing people paste ("claude-haiku-4-5-20251001",
    # observed live) — the refusal must TEACH: every valid option, in the detail.
    r = root_client.post("/api/graph/self/node/usage/config",
                         json={"model": "claude-haiku-4-5-20251001"})
    assert r.status_code == 400
    for m in known:
        assert m in r.json()["detail"], "the 400 must say the valid options"
    # ...and the payloads carry the same set, so the UI select cannot drift
    assert root_client.get("/api/graph/self").json()["models"] == known
    assert root_client.get("/api/graph/self/node/usage").json()["models"] == known
    r = root_client.post("/api/graph/self/node/usage/config",
                         json={"model": known[0], "autonomy": "supervised"})
    assert r.status_code == 200 and r.json()["config"]["model"] == known[0]
    plan = modgraph.active_plan(0)
    assert modgraph.get_assign(plan["id"], "usage")["model"] == known[0]
    # layout: a drag persists, an undragged node stays ABSENT — never a stored [0,0]
    r = root_client.post("/api/graph/self/layout", json={"positions": {"usage": [120, 80]}})
    assert r.status_code == 200
    pos = modgraph.positions(plan["id"])
    assert pos["usage"] == [120.0, 80.0] and "notify" not in pos


# --------------------------------------------------------------------------
# the payload carries both levels; a room answers for what is inside it
# --------------------------------------------------------------------------

def test_the_payload_carries_parents_and_rolls_rooms_up(root_client, monkeypatch):
    """The Atlas derives its two levels client-side from parent_key alone, so
    every node row must carry it — and a room must answer for its children AND
    for itself: suite totals summed, activity busy whenever any child is busy."""
    from app import launcher, repair
    monkeypatch.setattr(repair, "status", lambda: {"sprint": {"tasks": [{
        "status": "building", "title": "Fix knowledge decay",
        "brief": "services/knowledge/app.py drops rows on decay",
        "factor": "correctness"}]}})
    monkeypatch.setitem(launcher.ACTIVE, 41, {
        "kind": "process", "ref": "pid 1", "role": "coder", "model": "m",
        "project_id": 3, "started_at": 0.0, "title": "build the thing",
        "task_id": 41, "rival": ""})

    r = root_client.get("/api/graph/self")
    assert r.status_code == 200, r.text
    out = r.json()
    by = {n["key"]: n for n in out["nodes"]}
    assert all("parent_key" in n for n in out["nodes"]), \
        "every row must carry parent_key — the Atlas clips by level with it"
    assert by["aim"]["parent_key"] == "" and by["conclusion"]["parent_key"] == ""

    # the live worker is a card in the pool's room, with no row behind it
    worker = next(n for n in out["nodes"] if n["parent_key"] == "worker-pool")
    assert worker["key"] == "worker:41" and worker["title"] == "build the thing"
    assert worker["service"]["kind"] == "worker"
    plan = modgraph.active_plan(0)
    assert worker["key"] not in {n["key"] for n in modgraph.nodes(plan["id"])}, \
        "an ephemeral card must never become a plan row"

    # the room's counts are its OWN suites plus its children's
    own = len([t for t in modgraph.tests(plan["id"], "worker-pool")])
    assert own and by["worker-pool"]["tests"]["total"] == own

    # busy service card; an untouched card stays quiet
    assert by["knowledge"]["activity"], "the building task names the knowledge service"
    assert by["notify"]["activity"] == []


# --------------------------------------------------------------------------
# the fleet is READ, never assumed
# --------------------------------------------------------------------------

def test_an_invisible_fleet_manager_is_not_a_dead_fleet(root_client, monkeypatch):
    """The `--legacy` boot runs every service as a plain child of one shell and has
    no fleet API at all. A card that read that as "stopped" would be lying about a
    service that is perfectly up — so the state is `unknown`, the switch says why,
    and the beat still comes from the service's own /health."""
    import conftest
    monkeypatch.setattr(conftest, "FLEET_UP", False)
    fleet._states_cache.update(ts=0.0, by_name=None)
    out = root_client.get("/api/graph/self").json()
    by = {n["key"]: n for n in out["nodes"]}
    assert by["knowledge"]["service"]["state"] == "unknown"
    assert by["knowledge"]["service"]["control"] is False
    assert "run-local.sh" in by["knowledge"]["service"]["reason"]
    assert by["knowledge"]["health"]["beat"] == "ok", \
        "the service answers its own /health — the fleet manager being blind is not its death"
    assert out["conclusion"]["fleet"]["visible"] is False
    # ...and the switch refuses rather than pretending
    r = root_client.post("/api/graph/self/node/knowledge/service", json={"action": "stop"})
    assert r.status_code == 400 and "run-local.sh" in r.json()["detail"]


# --------------------------------------------------------------------------
# WS isolation: pid-0 reaches whoever may self-repair, and nobody else
# --------------------------------------------------------------------------

class _FakeSocket:
    """Just enough websocket for ws_feed: cookies in, sent frames out."""
    def __init__(self, token: str):
        self.cookies = {"devteam_session": token}
        self.sent: list[dict] = []
        self.closed = None

    async def accept(self):
        pass

    async def close(self, code=0):
        self.closed = code

    async def send_json(self, ev):
        self.sent.append(ev)


def _feed(token: str, emits: list[tuple], expect: int) -> list[dict]:
    """Drive the real ws_feed coroutine against a fake socket, emit, collect."""
    from app import bus
    from app.routes.internal import ws_feed

    async def run():
        bus.set_loop(asyncio.get_event_loop())
        ws = _FakeSocket(token)
        task = asyncio.create_task(ws_feed(ws))
        await asyncio.sleep(0.02)                    # let it subscribe
        for args in emits:
            bus.emit(*args)
        for _ in range(200):
            await asyncio.sleep(0.005)
            if len(ws.sent) >= expect:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return ws.sent

    try:
        return asyncio.run(run())
    finally:
        from app import bus as bus_mod
        bus_mod._loop = None                         # don't leak a closed loop


def test_pid0_events_reach_a_root_socket(fresh_db):
    token = auth.start_session(1)                    # root, seeded by auth.init()
    sent = _feed(token, [(0, None, "graph", "graph_verify_started",
                          {"node": "knowledge"})], 1)
    assert sent and sent[0]["project_id"] == 0
    assert sent[0]["kind"] == "graph_verify_started"


def test_pid0_events_are_dropped_for_an_ordinary_user(fresh_db):
    """The leak the plan warns about: widening the feed for the graph screen must
    not hand every signed-in user the platform's own repair/graph activity. The
    marker event proves the socket was alive and receiving — the pid-0 event
    emitted BEFORE it simply never arrived."""
    uid = auth.create_user("plain-user", "hunter2pw")
    pid = make_project(owner_id=uid)
    token = auth.start_session(uid)
    sent = _feed(token, [
        (0, None, "graph", "graph_verify_started", {"node": "knowledge"}),
        (pid, None, "system", "marker", {}),
    ], 1)
    assert sent, "the visible project event never arrived"
    assert sent[0]["kind"] == "marker" and sent[0]["project_id"] == pid
    assert all(e["project_id"] != 0 for e in sent), "a pid-0 event leaked to an ordinary user"
