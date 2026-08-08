"""The Module Graph drills — the platform's own code as the first graph tenant.

Everything here runs offline with injected fakes, the house style of
test_crew_loop.py: the point of each drill is an invariant that would rot
silently — the seed drifting from the tree it claims to describe, a "new
version" that quietly edited the old one, an advisory gate that grew teeth,
a scheduler-adjacent module sprouting a model call — not a happy path.
"""

import asyncio
import inspect
import subprocess
import time
from pathlib import Path

import pytest

from conftest import graph_rows as _svc_rows, make_project
from app import auth, db, modgraph

REPO = Path(__file__).resolve().parent.parent

GRAPH_TABLES = ("graph_plans", "graph_nodes", "graph_edges", "graph_node_runs",
                "graph_node_tests", "graph_assign")


def _seeded(fresh_db) -> int:
    modgraph.init()
    return modgraph.seed_self_graph()


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
# The seed's own claims about the tree, the pure `derive_group_edges` shape, the
# immutable-version rules and affected-only selection all MOVED to
# services/modgraph/tests with the P5 cutover: they are claims about rows and
# files that another process owns, and nothing outside a service's directory may
# import inside it. What is left here is the SEAM and the SCREEN — the BFF's
# payload, the verify runner that shells out to this checkout's pytest, the
# advisory promise about the CONDUCTOR's own tables, and the operator gates.

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
    tables = [r["name"] for r in db._rows(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    snap = {t: db._rows(f"SELECT * FROM {t}") for t in tables}
    changed = modgraph.update_test_result(pid, "tests/test_knowledge_service.py",
                                          "failing", "1 failed — boom")
    assert changed >= 1
    rows = [t for t in modgraph.tests(pid)
            if t["path"] == "tests/test_knowledge_service.py"]
    assert rows and all(t["status"] == "failing" for t in rows)
    for t in tables:
        assert db._rows(f"SELECT * FROM {t}") == snap[t], f"advisory result wrote to {t}"


# --------------------------------------------------------------------------
# no LLM anywhere near the graph store
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


# --------------------------------------------------------------------------
# the verify endpoint: affected-only through shell.sh, trace + events
# --------------------------------------------------------------------------

def _fake_sh(calls, returncode=0, stdout="9 passed in 0.12s\n", stderr=""):
    def sh(*cmd, cwd=None, timeout=None, stdin=None):
        calls.append({"cmd": [str(c) for c in cmd], "cwd": str(cwd), "timeout": timeout})
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return sh


def test_verify_runs_the_affected_files_and_leaves_a_trace(root_client, monkeypatch):
    from app import shell
    calls = []
    monkeypatch.setattr(shell, "sh", _fake_sh(calls))
    r = root_client.post("/api/graph/self/verify", json={"node": "knowledge"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["node"] == "knowledge"
    assert "tests/test_knowledge_service.py" in out["files"]

    # one bounded pytest run, handed exactly the affected files
    assert len(calls) == 1
    assert calls[0]["timeout"], "the run must carry a timeout"
    assert "pytest" in calls[0]["cmd"] and "-q" in calls[0]["cmd"]
    assert [c for c in calls[0]["cmd"] if c.startswith("tests/")] == out["files"]

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
    monkeypatch.setattr(shell, "sh", _fake_sh(
        calls, returncode=1,
        stdout="FAILED tests/test_knowledge_service.py::test_x - boom\n1 failed, 8 passed in 0.3s\n"))
    r = root_client.post("/api/graph/self/verify", json={"node": "knowledge"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is False
    assert out["statuses"]["tests/test_knowledge_service.py"] == "failing"
    others = [f for f in out["files"] if f != "tests/test_knowledge_service.py"]
    assert others and all(out["statuses"][f] == "passing" for f in others), \
        "a named culprit must not smear the rest of the affected set"
    plan = modgraph.active_plan(0)
    assert modgraph.runs(plan["id"], "knowledge")[-1]["status"] == "failed"
    # advisory: nothing about the platform's work changed
    assert db.list_tasks(0) == []


def test_the_graph_surface_is_an_operator_power(client, make_user):
    """Same gate as the Improve tile: an ordinary signed-in user gets a refusal,
    not a map of the server's own code."""
    uid, other = make_user("bystander")
    assert other.get("/api/graph/self").status_code == 403
    assert client.get("/api/graph/self").status_code == 401


def test_config_validates_the_model_and_layout_merges(root_client):
    from app import providers
    known = sorted(m["id"] for p in providers.PROVIDERS.values() for m in p["models"])
    # A dated id is the first thing people paste ("claude-haiku-4-5-20251001",
    # observed live) — the refusal must TEACH: every valid option, in the detail.
    r = root_client.post("/api/graph/self/node/db/config",
                         json={"model": "claude-haiku-4-5-20251001"})
    assert r.status_code == 400
    for m in known:
        assert m in r.json()["detail"], "the 400 must say the valid options"
    # ...and the payloads carry the same set, so the UI select cannot drift
    assert root_client.get("/api/graph/self").json()["models"] == known
    assert root_client.get("/api/graph/self/node/db").json()["models"] == known
    r = root_client.post("/api/graph/self/node/db/config",
                         json={"model": known[0], "autonomy": "supervised"})
    assert r.status_code == 200 and r.json()["config"]["model"] == known[0]
    plan = modgraph.active_plan(0)
    assert modgraph.get_assign(plan["id"], "db")["model"] == known[0]
    # layout: a drag persists, an undragged node stays ABSENT — never a stored [0,0]
    r = root_client.post("/api/graph/self/layout", json={"positions": {"db": [120, 80]}})
    assert r.status_code == 200
    pos = modgraph.positions(plan["id"])
    assert pos["db"] == [120.0, 80.0] and "routes" not in pos


# --------------------------------------------------------------------------
# the payload carries both levels; a group answers for its children
# --------------------------------------------------------------------------

def test_the_payload_carries_parents_and_rolls_groups_up(root_client, monkeypatch):
    """The canvas derives its two levels client-side from parent_key alone, so
    every node row must carry it — and a group must answer for its children:
    suite totals summed, activity busy exactly when a child is busy."""
    from app import repair
    monkeypatch.setattr(repair, "status", lambda: {"sprint": {"tasks": [{
        "status": "building", "title": "Fix knowledge decay",
        "brief": "conductor/app/knowledge.py drops rows on decay",
        "factor": "correctness"}]}})
    plan = modgraph.active_plan(0)
    modgraph.update_test_result(plan["id"], "tests/test_knowledge_service.py", "failing", "boom")

    r = root_client.get("/api/graph/self")
    assert r.status_code == 200, r.text
    out = r.json()
    by = {n["key"]: n for n in out["nodes"]}
    assert all("parent_key" in n for n in out["nodes"]), \
        "every row must carry parent_key — the canvas clips by level with it"
    assert by["aim"]["parent_key"] == "" and by["conclusion"]["parent_key"] == ""
    assert by["knowledge"]["parent_key"] == "data"

    kids = [n for n in out["nodes"] if n["parent_key"] == "data"]
    assert by["data"]["node_type"] == "group" and kids
    for f in ("total", "passing", "failing", "advisory"):
        assert by["data"]["tests"][f] == sum(k["tests"][f] for k in kids), \
            f"the group's {f} is not the sum of its children's"
    assert by["data"]["tests"]["failing"] >= 1, "the child's red must reach the layer"
    assert by["data"]["tests"]["total"] > by["knowledge"]["tests"]["total"], \
        "db's suites must be in the rollup too"

    # busy child -> busy group; an untouched layer stays quiet
    assert by["knowledge"]["activity"], "the building task names knowledge.py"
    assert by["data"]["activity"] == by["knowledge"]["activity"]
    assert by["frontend"]["activity"] == []


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
    sent = _feed(token, [(0, None, "graph", "graph_verify_started", {"node": "db"})], 1)
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
        (0, None, "graph", "graph_verify_started", {"node": "db"}),
        (pid, None, "system", "marker", {}),
    ], 1)
    assert sent, "the visible project event never arrived"
    assert sent[0]["kind"] == "marker" and sent[0]["project_id"] == pid
    assert all(e["project_id"] != 0 for e in sent), "a pid-0 event leaked to an ordinary user"
