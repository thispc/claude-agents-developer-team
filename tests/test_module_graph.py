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

from conftest import make_project
from app import auth, db, modgraph

REPO = Path(__file__).resolve().parent.parent


def _seeded(fresh_db) -> int:
    modgraph.init()
    return modgraph.seed_self_graph()


# --------------------------------------------------------------------------
# schema + the about-page gate
# --------------------------------------------------------------------------

def test_the_schema_initialises_and_the_table_gate_counts_it(fresh_db):
    """Six tables of our own, knowledge.py-style — zero entries in db.py's
    append-only migration tuple, so the gate that polices the handbook's table
    count must now sum modgraph in or the count it defends is a lie."""
    modgraph.init()
    assert modgraph.SCHEMA.count("CREATE TABLE IF NOT EXISTS") == 6
    names = {r["name"] for r in db._rows("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("graph_plans", "graph_nodes", "graph_edges", "graph_node_runs",
              "graph_node_tests", "graph_assign"):
        assert t in names, f"{t} was not created"
    # knowledge's table moved out with the P1 extraction (the knowledge service
    # owns it now) — the conductor's declared count is 34 without it.
    from app import auth as auth_mod, findings
    declared = sum(mod.SCHEMA.count("CREATE TABLE IF NOT EXISTS")
                   for mod in (db, auth_mod, findings, modgraph))
    assert declared == 34
    gate = (REPO / "tests" / "test_about_page.py").read_text()
    assert "modgraph" in gate, "the about-page table gate does not count modgraph"


# --------------------------------------------------------------------------
# the seed must describe the repository that actually exists
# --------------------------------------------------------------------------

def test_the_seed_matches_reality(fresh_db):
    """A fallback graph that names files which are not there is worse than no
    graph: every claim in it — boundaries, test mapping, contracts, specs — is
    checked against the working tree, because the seed's one job is to be true."""
    t0 = time.perf_counter()
    pid = _seeded(fresh_db)
    assert time.perf_counter() - t0 < 1.0, "seeding must stay cheap enough for boot"
    plan = modgraph.active_plan(0)
    assert plan and plan["id"] == pid and plan["authored_by"] == "seed"
    assert plan["kind"] == "template" and plan["status"] == "active"

    nodes = modgraph.nodes(pid)
    keys = [n["key"] for n in nodes]
    assert keys[0] == "aim" and keys[-1] == "conclusion"
    groups = {n["key"] for n in nodes if n["node_type"] == "group"}
    leaves = {n["key"] for n in nodes if n["node_type"] == "code"}
    assert groups == {"backend", "frontend", "data", "agents", "selfrepair"}
    assert leaves == {"routes", "guards", "shell", "db", "manager",
                      "orchestration", "repair", "lifeworld", "knowledge", "ops",
                      "worker", "dash-core", "dash-views", "canvas"}
    assert set(keys) == groups | leaves | {"aim", "conclusion"}

    # TWO LEVELS, structurally: every top node between aim and conclusion is a
    # GROUP; every module is the child of exactly one group; a group's boundary
    # is precisely the union of its children's — an architecture diagram whose
    # zoomed-out level cannot drift from the modules underneath it.
    by = {n["key"]: n for n in nodes}
    for n in nodes:
        if n["node_type"] in ("aim", "conclusion", "group"):
            assert n["parent_key"] == "", f"{n['key']} must sit at the top level"
        else:
            assert by.get(n["parent_key"], {}).get("node_type") == "group", \
                f"module {n['key']} is not parented to a group"
    for g in sorted(groups):
        kids = [c for c in nodes if c["parent_key"] == g]
        assert kids, f"group {g} has no children"
        assert by[g]["paths"] == sorted({p for c in kids for p in c["paths"]}), \
            f"group {g}'s paths are not the union of its children's"
        assert by[g]["spec"], f"group {g} carries no spec"

    # every boundary entry exists in the repo
    for n in nodes:
        for p in n["paths"]:
            assert (REPO / p).exists(), f"node {n['key']} claims missing path {p}"

    # every mechanically-mapped test file exists
    for t in modgraph.tests(pid):
        assert (REPO / t["path"]).exists(), f"mapped test {t['path']} does not exist"

    # the specs are the modules' real docstrings, not a seed file's memory of them
    specs = {n["key"]: n["spec"] for n in nodes}
    assert "the ownership gates every router leans on" in specs["guards"]
    assert "stored so it can be found again" in specs["knowledge"]
    assert "Native SVG/DOM hit-testing" in specs["canvas"]

    # the ports contract is LITERALLY true: `from ..` only behind the one door
    edges = modgraph.edges(pid)
    ports = next(e for e in edges if e["src_key"] == "lifeworld" and e["dst_key"] == "routes")
    c = ports["contract"]
    assert c["kind"] == "ports"
    offenders = [f.name for f in (REPO / c["package"]).glob("*.py")
                 if c["pattern"] in f.read_text() and f.name != c["door"]]
    assert not offenders, f"the ports contract is no longer true: {offenders}"
    assert c["pattern"] in (REPO / c["package"] / c["door"]).read_text(), \
        "the contract is vacuous — the door itself no longer uses the pattern"

    # the dashboard load-order edges agree with index.html's actual script order
    html = (REPO / "dashboard" / "index.html").read_text()
    for e in edges:
        lc = e["contract"]
        if lc.get("kind") != "load-order":
            continue
        before = html.index(f'src="{lc["before"]}"')
        for name in lc["after"]:
            assert html.index(f'src="{name}"') > before, \
                f"{name} loads before {lc['before']} — the {e['src_key']}→{e['dst_key']} edge lies"


# --------------------------------------------------------------------------
# the top level is derived, never curated
# --------------------------------------------------------------------------

def test_group_edges_are_derived_from_child_edges():
    """Group A → group B iff any child of A touches any child of B, the first
    crossing edge in input order riding up as the representative; an edge
    inside one layer is that layer's private business, and an endpoint with no
    parent derives nothing. Pure: lists in, list out."""
    parent_of = {"a1": "A", "a2": "A", "b1": "B", "c1": "C"}
    child = [
        {"src": "a1", "dst": "a2", "edge_type": "interface",
         "contract": {"rule": "in-house"}, "contract_test": "tests/t_aa.py"},
        {"src": "a1", "dst": "b1", "edge_type": "data",
         "contract": {"rule": "first crossing"}, "contract_test": "tests/t_ab.py"},
        {"src": "a2", "dst": "b1", "edge_type": "interface",
         "contract": {"rule": "second crossing, not the representative"},
         "contract_test": ""},
        {"src": "b1", "dst": "c1", "edge_type": "depends", "contract": {},
         "contract_test": ""},
        {"src": "b1", "dst": "orphan", "edge_type": "depends", "contract": {},
         "contract_test": ""},
    ]
    before = [dict(e) for e in child]
    got = modgraph.derive_group_edges(child, parent_of)
    assert got == [
        {"src": "A", "dst": "B", "edge_type": "data",
         "contract": {"rule": "first crossing"}, "contract_test": "tests/t_ab.py"},
        {"src": "B", "dst": "C", "edge_type": "depends", "contract": {},
         "contract_test": ""},
    ]
    assert child == before, "the derivation must not mutate its input"


def test_a_child_crossing_the_authored_tier_missed_still_rides_up():
    """The live hole, pinned in its exact shape: children r, o in G1; d in G2;
    the child edge o→d MUST yield G1→G2. Plan v6 (manager-authored) carried
    ops→db while its authored top tier had no selfrepair→data over it — and one
    selfrepair→agents arrow no child edge backed. Reconciliation: a missed
    crossing is derived anyway, an authored arrow a real crossing backs keeps
    its deliberate type/contract, and a fabricated arrow is dropped."""
    parent_of = {"r": "G1", "o": "G1", "d": "G2"}
    child = [
        {"src": "r", "dst": "o", "edge_type": "interface",
         "contract": {"rule": "in-house"}, "contract_test": ""},
        {"src": "o", "dst": "d", "edge_type": "data",
         "contract": {"rule": "the crossing"}, "contract_test": "tests/t_od.py"},
    ]
    crossing = {"src": "G1", "dst": "G2", "edge_type": "data",
                "contract": {"rule": "the crossing"}, "contract_test": "tests/t_od.py"}
    # derived bare, the crossing is simply there
    assert modgraph.derive_group_edges(child, parent_of) == [crossing]
    # reconciled against an authored tier that MISSED it and invented G1→G3:
    # the crossing appears anyway; the arrow into nothing does not survive
    authored = [{"src": "G1", "dst": "G3", "edge_type": "depends",
                 "contract": {}, "contract_test": ""}]
    assert modgraph.derive_group_edges(child, parent_of,
                                       group_edges=authored) == [crossing]
    # an authored arrow a real crossing backs keeps its deliberate contract
    authored = [{"src": "G1", "dst": "G2", "edge_type": "interface",
                 "contract": {"rule": "the deliberate rule"}, "contract_test": ""}]
    assert modgraph.derive_group_edges(child, parent_of, group_edges=authored) == [
        {"src": "G1", "dst": "G2", "edge_type": "interface",
         "contract": {"rule": "the deliberate rule"}, "contract_test": ""}]


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


def test_the_seeds_top_level_edges_are_exactly_the_derivation(fresh_db):
    """The stored group-to-group edges are the derivation over the stored child
    edges — no hand-curated arrow at the top level — and the frame holds: the
    aim feeds every layer, every layer feeds the conclusion, and no edge mixes
    a group with a leaf (the canvas clips by level)."""
    pid = _seeded(fresh_db)
    nodes = modgraph.nodes(pid)
    parent_of = {n["key"]: n["parent_key"] for n in nodes if n["parent_key"]}
    groups = {n["key"] for n in nodes if n["node_type"] == "group"}
    edges = [{"src": e["src_key"], "dst": e["dst_key"], "edge_type": e["edge_type"],
              "contract": e["contract"], "contract_test": e["contract_test"]}
             for e in modgraph.edges(pid)]
    child = [e for e in edges if e["src"] in parent_of and e["dst"] in parent_of]
    stored = [e for e in edges if e["src"] in groups and e["dst"] in groups]
    assert stored == modgraph.derive_group_edges(child, parent_of)
    assert stored, "the layers of a working platform cannot be unconnected"
    for g in sorted(groups):
        assert any(e["src"] == "aim" and e["dst"] == g for e in edges)
        assert any(e["src"] == g and e["dst"] == "conclusion" for e in edges)
    for e in edges:
        assert (e["src"] in groups) == (e["dst"] in groups) or \
            e["src"] == "aim" or e["dst"] == "conclusion", \
            f"{e['src']}→{e['dst']} mixes the two levels"


def test_every_leaf_shows_its_real_suites_routes_included(fresh_db):
    """The live defect: the `routes` leaf claimed "no tests mapped" while its
    group rolled up 22, because the import parser anchored at column 0 and every
    routes import in the tests is INDENTED (inside a test function). Every leaf
    whose module the tests exercise must show its suites — routes by name,
    because routes is where it was caught lying."""
    pid = _seeded(fresh_db)
    routes_suites = [t for t in modgraph.tests(pid, "routes") if t["kind"] == "suite"]
    assert routes_suites, "the routes leaf must map its real suites in the seed"

    # the mechanic itself, pinned on a synthetic source: indented imports count
    src = ("def test_x():\n"
           "    from app.routes import Settings\n"
           "    from app import shell\n")
    mods: set[str] = set()
    for m in modgraph._IMPORT_RES[0].findall(src):
        mods.update(name.strip().split(" as ")[0].strip() for name in m.split(","))
    for pat in modgraph._IMPORT_RES[1:]:
        mods.update(pat.findall(src))
    assert {"routes", "shell"} <= mods, \
        "an import four spaces deep is still an import — the parser must see it"


# --------------------------------------------------------------------------
# immutability: drift makes a NEW version; nothing edits history
# --------------------------------------------------------------------------

def test_reseeding_an_unchanged_tree_writes_nothing(fresh_db):
    pid = _seeded(fresh_db)
    counts = {t: db._rows(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
              for t in ("graph_plans", "graph_nodes", "graph_edges", "graph_node_tests")}
    assert modgraph.seed_self_graph() == pid
    for t, n in counts.items():
        assert db._rows(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] == n, f"{t} grew on a no-op reseed"


def test_drift_makes_a_new_version_and_never_touches_the_old_rows(fresh_db, monkeypatch):
    """The whole reason plans are versions: 'what did we believe when this was
    built' must stay answerable. So a drifted tree produces a NEW plan, and the
    only byte that changes on the old one is its status."""
    v1 = _seeded(fresh_db)
    before = {t: db._rows(f"SELECT * FROM {t} WHERE plan_id=? ORDER BY id", (v1,))
              for t in ("graph_nodes", "graph_edges", "graph_node_tests")}
    plan1_before = db._rows("SELECT * FROM graph_plans WHERE id=?", (v1,))[0]

    real = modgraph._self_manifest
    def drifted():
        man = real()
        man["nodes"][1]["spec"] = "the backend layer, freshly reworded"
        return man
    monkeypatch.setattr(modgraph, "_self_manifest", drifted)

    v2 = modgraph.seed_self_graph()
    assert v2 != v1
    plan2 = modgraph.active_plan(0)
    assert plan2["id"] == v2 and plan2["version"] == plan1_before["version"] + 1
    assert plan2["authored_by"] == "seed"
    # v1's graph rows: byte-identical
    for t, rows in before.items():
        assert db._rows(f"SELECT * FROM {t} WHERE plan_id=? ORDER BY id", (v1,)) == rows, \
            f"{t} rows of the superseded plan were edited"
    # v1's plan row: only status moved
    plan1_after = db._rows("SELECT * FROM graph_plans WHERE id=?", (v1,))[0]
    assert plan1_after["status"] == "superseded"
    assert {k: v for k, v in plan1_after.items() if k != "status"} == \
           {k: v for k, v in plan1_before.items() if k != "status"}


def test_the_seed_never_overwrites_a_managers_plan(fresh_db):
    """The fallback is the floor, not the ceiling: the day the crew's manager
    authors a plan, reseeding must leave it in charge."""
    modgraph.init()
    pid = modgraph.create_plan(0, authored_by="manager", notes="the crew's own plan")
    modgraph.add_node(pid, "aim", "the crew's aim", node_type="aim")
    modgraph.activate(pid)
    assert modgraph.seed_self_graph() == pid
    assert modgraph.active_plan(0)["authored_by"] == "manager"


# --------------------------------------------------------------------------
# affected-only selection: a pure function over the rows
# --------------------------------------------------------------------------

def test_affected_selection_is_the_suite_plus_every_touched_contract(fresh_db):
    """Verifying a node runs its own suite plus the contract of every edge it is
    party to — either end, because the other side of an interface is exactly who
    a change here breaks — and nothing else. Verifying a GROUP is exactly the
    union of its children's affected sets."""
    modgraph.init()
    pid = modgraph.create_plan(7, authored_by="test")
    modgraph.add_node(pid, "g", "The AB layer", node_type="group")
    for k in ("a", "b"):
        modgraph.add_node(pid, k, k.upper(), parent_key="g")
    modgraph.add_node(pid, "c", "C")
    modgraph.map_test(pid, "a", "tests/t_a.py")
    modgraph.map_test(pid, "a", "tests/t_a2.py")
    modgraph.map_test(pid, "b", "tests/t_b.py")
    modgraph.add_edge(pid, "a", "b", edge_type="interface", contract_test="tests/t_ab.py")
    modgraph.add_edge(pid, "c", "a", edge_type="data", contract_test="tests/t_ca.py")
    modgraph.add_edge(pid, "b", "c", edge_type="depends", contract_test="tests/t_bc.py")

    runs_before = db._rows("SELECT COUNT(*) AS n FROM graph_node_runs")[0]["n"]
    got = modgraph.affected_tests(pid, "a")
    assert got == sorted(["tests/t_a.py", "tests/t_a2.py", "tests/t_ab.py", "tests/t_ca.py"])
    assert "tests/t_bc.py" not in got, "an edge not touching the node was selected"
    assert "tests/t_b.py" not in got, "another node's suite was selected"
    assert modgraph.affected_tests(pid, "nowhere") == []
    # the group: the union of its children's sets — no more, no less
    assert modgraph.affected_tests(pid, "g") == sorted(
        set(modgraph.affected_tests(pid, "a")) | set(modgraph.affected_tests(pid, "b")))
    assert "tests/t_b.py" in modgraph.affected_tests(pid, "g")
    # pure: selection wrote nothing
    assert db._rows("SELECT COUNT(*) AS n FROM graph_node_runs")[0]["n"] == runs_before


# --------------------------------------------------------------------------
# the gate is advisory: a red result is information, never an action
# --------------------------------------------------------------------------

def test_a_failing_result_writes_only_the_test_rows(fresh_db):
    """V1's promise to the owner: a red suite embarrasses, it does not brick.
    Mechanically that means a failure may touch the test rows and nothing else —
    no task rows, no project status, no kv flag anybody acts on."""
    pid = _seeded(fresh_db)
    tables = [r["name"] for r in db._rows(
        "SELECT name FROM sqlite_master WHERE type='table' AND name != 'graph_node_tests'")]
    snap = {t: db._rows(f"SELECT * FROM {t}") for t in tables}
    changed = modgraph.update_test_result(pid, "tests/test_knowledge_service.py", "failing", "1 failed — boom")
    assert changed >= 1
    rows = [t for t in modgraph.tests(pid) if t["path"] == "tests/test_knowledge_service.py"]
    assert rows and all(t["status"] == "failing" for t in rows)
    for t in tables:
        assert db._rows(f"SELECT * FROM {t}") == snap[t], f"advisory result wrote to {t}"


# --------------------------------------------------------------------------
# no LLM anywhere near the graph store
# --------------------------------------------------------------------------

def test_the_store_and_seed_never_call_a_model():
    """The research verdict the plan locked in: no LLM in the scheduler layer.
    The store, the seed and the selection are deterministic reads of rows and
    files — pinned at source level so a helpful future refactor cannot slip a
    completion call in."""
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
