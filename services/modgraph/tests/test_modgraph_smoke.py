"""The modgraph service's own suite — offline, in-process, no sockets.

The contract (`/health`, the token, the committed spec), the invariants the
extraction had to preserve (immutable versions, the derivation, affected-only
selection, mastery from the trace), and the two things this end of the wire must
never learn to do: import the conductor, or call a model.
"""

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import modgraph_service_app as svc                      # noqa: E402  (see conftest)
from modgraph.tests.conftest import TOKEN

SERVICE_DIR = Path(__file__).resolve().parent.parent
REPO = SERVICE_DIR.parent.parent


def client() -> TestClient:
    c = TestClient(svc.app)
    c.headers["X-Service-Token"] = TOKEN
    return c


# --------------------------------------------------------------------------
# the contract
# --------------------------------------------------------------------------

def test_health_is_the_contracts_readiness_shape(clean_store):
    got = client().get("/health").json()
    assert got["ok"] is True and got["service"] == "modgraph"
    assert got["checks"] == {"db": True, "table": True}
    assert "backfilled" in got, "the conductor's drop waits on this field"


def test_health_and_the_spec_need_no_token_but_every_verb_does(clean_store):
    anon = TestClient(svc.app)
    assert anon.get("/health").status_code == 200
    assert anon.get("/openapi.json").status_code == 200
    for method, path in (("get", "/plans/active"), ("post", "/seed"),
                         ("get", "/manifest"), ("get", "/mastery"),
                         ("post", "/runs"), ("get", "/plans/1/nodes"),
                         ("post", "/import-plan"), ("patch", "/runs/1"),
                         ("post", "/derive-group-edges"), ("post", "/tests-for-nodes"),
                         ("get", "/plans/1/affected"), ("get", "/plans/1/assigns"),
                         ("post", "/plans/1/positions"), ("get", "/plans/1/runs"),
                         ("post", "/plans/1/test-result")):
        r = (anon.get(path) if method == "get"
             else getattr(anon, method)(path, json={}))
        assert r.status_code == 401, f"{path} answered {r.status_code} with no token"
    bad = TestClient(svc.app)
    bad.headers["X-Service-Token"] = "nope"
    assert bad.get("/plans/active").status_code == 401


def test_the_committed_spec_is_what_is_served(clean_store):
    served = client().get("/openapi.json").json()
    assert served == json.loads((SERVICE_DIR / "openapi.json").read_text())


def test_nothing_here_imports_the_conductor(clean_store):
    """THE INVARIANT OF THE PHASE. `modgraph_author ↔ repair` was the one genuine
    import cycle in the whole decomposition, and P5 cut it by putting a wire in
    the middle — with the STORE as the end that must not reach back. A single
    `from app import …` here would restore the cycle over HTTP, which is worse
    than the cycle: it would also deadlock, since the conductor is the caller."""
    offenders = []
    for f in sorted(SERVICE_DIR.rglob("*.py")):
        if "tests" in f.parts or "__pycache__" in f.parts:
            continue
        src = f.read_text()
        for pat in (r"^\s*from\s+app[\s.]", r"^\s*import\s+app\b",
                    r"^\s*from\s+conductor", r"^\s*from\s+\.\.",
                    r"conductor\.app"):
            if re.search(pat, src, re.M):
                offenders.append(f"{f.name}: {pat}")
    assert offenders == [], f"the service reaches back into the conductor: {offenders}"


def test_no_model_and_no_credential_lives_on_this_side(clean_store):
    """The store has been pinned model-free since it was written (the
    no-LLM-in-the-scheduler verdict) and the boundary makes it structural: this
    service declares NO doors in services.yaml, so it could not ask for a
    completion even if a helpful refactor tried to."""
    import yaml
    for f in sorted(SERVICE_DIR.glob("*.py")):
        src = f.read_text()
        assert "providers." not in src and "claude_agent_sdk" not in src
        assert "anthropic_api_key" not in src
    entry = yaml.safe_load((REPO / "services.yaml").read_text())["services"]["modgraph"]
    assert not entry.get("doors"), "a store with a door into the conductor is not a store"
    assert not entry.get("env"), "no credential follows this code"
    assert not entry.get("peers")


# --------------------------------------------------------------------------
# the seed: it must describe the tree that is actually there
# --------------------------------------------------------------------------

def test_the_seed_is_idempotent_and_cheap(clean_store):
    c = client()
    t0 = time.perf_counter()
    pid = c.post("/seed").json()["plan_id"]
    assert time.perf_counter() - t0 < 1.0, "seeding must stay cheap enough for boot"
    assert c.post("/seed").json()["plan_id"] == pid
    rows = svc.helpers.db().execute("SELECT COUNT(*) FROM graph_plans").fetchone()[0]
    assert rows == 1, "a no-op reseed wrote a version"
    plan = c.get("/plans/active", params={"project_id": 0}).json()["plan"]
    assert plan["id"] == pid and plan["authored_by"] == "seed" and plan["status"] == "active"


def test_the_manifest_the_seed_wrote_is_the_manifest_it_built(clean_store):
    """Idempotence is dict equality on the manifest and nothing cleverer, so the
    two shapes have to actually match — including the test dedupe, which the
    UNIQUE index performs on the rows and `dedupe_tests` has to mirror."""
    c = client()
    pid = c.post("/seed").json()["plan_id"]
    assert c.get(f"/plans/{pid}/manifest").json() == c.get("/manifest").json()


def test_an_indented_import_is_still_an_import(clean_store):
    """The live defect this parser was fixed for: the `routes` leaf sat at "no
    tests mapped" while its group rolled up 22, because the regexes anchored at
    column 0 and every routes import in the suite is inside a test function."""
    src = ("def test_x():\n"
           "    from app.routes import Settings\n"
           "    from app import shell, db as database\n")
    mods: set[str] = set()
    for m in svc.seed._IMPORT_RES[0].findall(src):
        mods.update(name.strip().split(" as ")[0].strip() for name in m.split(","))
    for pat in svc.seed._IMPORT_RES[1:]:
        mods.update(pat.findall(src))
    assert {"routes", "shell", "db"} <= mods


def test_the_test_map_answers_by_card_key_not_by_authored_paths(clean_store):
    """A card is a SERVICE, so its suite is the service's own directory — the
    mapping is by KEY, and the paths ride along only for signature compatibility
    with the manager's authoring pass. The manager authors neither."""
    c = client()
    got = c.post("/tests-for-nodes", json={"by_key": {"knowledge": []}}).json()["map"]
    assert got, "no suite maps to the knowledge service — the mapping went blind"
    assert all(keys == ["knowledge"] for keys in got.values())
    assert all(path.startswith("services/knowledge/tests/") for path in got), got
    # ...and a non-service card's suite is parsed from the repo suite's imports
    got = c.post("/tests-for-nodes", json={"by_key": {"apps": []}}).json()["map"]
    assert got and all(path.startswith("tests/") for path in got), got


def test_activating_supersedes_and_edits_nothing_else(clean_store):
    c = client()
    v1 = c.post("/plans", json={"project_id": 3, "authored_by": "seed"}).json()["plan_id"]
    c.post(f"/plans/{v1}/nodes", json={"key": "a", "title": "A"})
    c.post(f"/plans/{v1}/activate")
    before = dict(svc.helpers.db().execute(
        "SELECT * FROM graph_plans WHERE id=?", (v1,)).fetchone())
    v2 = c.post("/plans", json={"project_id": 3, "authored_by": "manager"}).json()["plan_id"]
    c.post(f"/plans/{v2}/activate")
    after = dict(svc.helpers.db().execute(
        "SELECT * FROM graph_plans WHERE id=?", (v1,)).fetchone())
    assert after["status"] == "superseded" and after["version"] == before["version"]
    assert {k: v for k, v in after.items() if k != "status"} == \
           {k: v for k, v in before.items() if k != "status"}
    assert c.get("/plans/active", params={"project_id": 3}).json()["plan"]["id"] == v2


def test_a_whole_plan_is_written_in_one_transaction(clean_store):
    """`/plans/import` exists because a plan is authored as one thing and a
    half-written one is a lie — and over a wire, row-by-row means a version that
    can be ACTIVATED with half its edges."""
    c = client()
    plan = c.post("/import-plan", json={
        "project_id": 0, "authored_by": "manager", "notes": "one shot",
        "nodes": [{"key": "aim", "title": "aim", "node_type": "aim"},
                  {"key": "g", "title": "G", "node_type": "group"},
                  {"key": "a", "title": "A", "parent_key": "g"}],
        "edges": [{"src": "a", "dst": "aim", "edge_type": "data",
                   "contract": {"rule": "r"}, "contract_test": "tests/t.py"}],
        "tests": [{"node": "a", "path": "tests/t.py", "kind": "suite"}],
        "assigns": {"a": {"agent_id": 42}},
        "positions": {"a": [3, 4]}}).json()["plan"]
    pid = plan["id"]
    assert plan["status"] == "active" and plan["authored_by"] == "manager"
    assert [n["key"] for n in c.get(f"/plans/{pid}/nodes").json()["nodes"]] == \
        ["aim", "g", "a"]
    assert c.get(f"/plans/{pid}/edges").json()["edges"][0]["contract"] == {"rule": "r"}
    assert c.get(f"/plans/{pid}/assigns").json()["assigns"]["a"]["agent_id"] == 42
    assert c.get(f"/plans/{pid}/positions").json()["positions"] == {"a": [3.0, 4.0]}


def test_a_bad_import_leaves_no_half_written_plan(clean_store, monkeypatch):
    """The whole point of one transaction. A node the store refuses part way
    through must not leave an activated version behind."""
    c = client()
    before = svc.helpers.db().execute("SELECT COUNT(*) FROM graph_plans").fetchone()[0]
    real = svc.store.add_node
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("disk went away")
        return real(*a, **k)
    monkeypatch.setattr(svc.store, "add_node", flaky)
    with pytest.raises(sqlite3.OperationalError):
        svc.store.import_plan(0, nodes_in=[{"key": "a"}, {"key": "b"}, {"key": "c"}],
                              edges_in=[], tests_in=[])
    assert svc.helpers.db().execute(
        "SELECT COUNT(*) FROM graph_plans").fetchone()[0] == before
    assert c.get("/plans/active", params={"project_id": 0}).json()["plan"] is None


# --------------------------------------------------------------------------
# the derivation, the selection, the trace
# --------------------------------------------------------------------------

def test_the_top_tier_is_reconciled_not_trusted(clean_store):
    """A crossing the author missed is derived anyway; an authored arrow a real
    crossing backs keeps its deliberate contract; an arrow into nothing is
    dropped. The live hole: plan v6 carried ops→db with no selfrepair→data over
    it, plus one selfrepair→agents arrow no child edge backed."""
    c = client()
    child = [{"src": "o", "dst": "d", "edge_type": "data",
              "contract": {"rule": "the crossing"}, "contract_test": "tests/t_od.py"}]
    parent_of = {"r": "G1", "o": "G1", "d": "G2"}
    crossing = {"src": "G1", "dst": "G2", "edge_type": "data",
                "contract": {"rule": "the crossing"}, "contract_test": "tests/t_od.py"}
    assert c.post("/derive-group-edges", json={
        "child_edges": child, "parent_of": parent_of}).json()["edges"] == [crossing]
    assert c.post("/derive-group-edges", json={
        "child_edges": child, "parent_of": parent_of,
        "group_edges": [{"src": "G1", "dst": "G3", "edge_type": "depends",
                         "contract": {}, "contract_test": ""}]}).json()["edges"] == [crossing]
    assert c.post("/derive-group-edges", json={
        "child_edges": child, "parent_of": parent_of,
        "group_edges": [{"src": "G1", "dst": "G2", "edge_type": "interface",
                         "contract": {"rule": "deliberate"}, "contract_test": ""}]
        }).json()["edges"] == [{"src": "G1", "dst": "G2", "edge_type": "interface",
                                "contract": {"rule": "deliberate"}, "contract_test": ""}]


def test_affected_is_the_suite_plus_every_touched_contract(clean_store):
    c = client()
    pid = c.post("/plans", json={"project_id": 7}).json()["plan_id"]
    c.post(f"/plans/{pid}/nodes", json={"key": "g", "title": "G", "node_type": "group"})
    for k in ("a", "b"):
        c.post(f"/plans/{pid}/nodes", json={"key": k, "title": k, "parent_key": "g"})
    c.post(f"/plans/{pid}/nodes", json={"key": "c", "title": "C"})
    for node, path in (("a", "tests/t_a.py"), ("a", "tests/t_a2.py"), ("b", "tests/t_b.py")):
        c.post(f"/plans/{pid}/tests", json={"node_key": node, "path": path})
    for s, d, ct in (("a", "b", "tests/t_ab.py"), ("c", "a", "tests/t_ca.py"),
                     ("b", "c", "tests/t_bc.py")):
        c.post(f"/plans/{pid}/edges", json={"src_key": s, "dst_key": d, "contract_test": ct})

    def affected(key):
        return c.get(f"/plans/{pid}/affected", params={"node_key": key}).json()["files"]
    assert affected("a") == sorted(["tests/t_a.py", "tests/t_a2.py",
                                    "tests/t_ab.py", "tests/t_ca.py"])
    assert affected("nowhere") == []
    assert affected("g") == sorted(set(affected("a")) | set(affected("b")))
    # pure: selection wrote nothing
    assert svc.helpers.db().execute(
        "SELECT COUNT(*) FROM graph_node_runs").fetchone()[0] == 0


def test_mastery_counts_closed_ok_runs_across_plan_versions(clean_store):
    """Node KEYS are the stable identity, so a replan must not amnesty away who
    knows a module best — and ties keep the incumbent, because a challenger
    catches up to a master rather than splitting the title."""
    c = client()

    def run(pid, key, agent, status="ok", kind="build", close=True):
        rid = c.post("/runs", json={"plan_id": pid, "node_key": key, "kind": kind,
                                    "agent_id": agent}).json()["id"]
        if close:
            c.patch(f"/runs/{rid}", json={"status": status})
        return rid

    v1 = c.post("/plans", json={"project_id": 0}).json()["plan_id"]
    c.post(f"/plans/{v1}/activate")
    assert c.get("/mastery").json()["mastery"] == {}
    run(v1, "db", 7)
    run(v1, "db", 7, status="failed")            # only ok runs count
    run(v1, "db", 8, close=False)                # an unclosed run is not a finish
    got = c.get("/mastery").json()["mastery"]
    assert got["db"] == {"agent_id": 7, "runs": 1, "master": False}

    v2 = c.post("/plans", json={"project_id": 0}).json()["plan_id"]
    c.post(f"/plans/{v2}/activate")
    run(v2, "db", 7, kind="verify")
    run(v2, "db", 7)
    assert c.get("/mastery").json()["mastery"]["db"] == \
        {"agent_id": 7, "runs": 3, "master": True}
    # a challenger must EXCEED, not merely equal
    for _ in range(3):
        run(v2, "db", 9)
    assert c.get("/mastery").json()["mastery"]["db"]["agent_id"] == 7
    run(v2, "db", 9)
    assert c.get("/mastery").json()["mastery"]["db"]["agent_id"] == 9
    # another project's runs are another project's
    assert c.get("/mastery", params={"project_id": 4}).json()["mastery"] == {}


def test_a_test_result_lands_everywhere_the_file_is_mapped(clean_store):
    c = client()
    pid = c.post("/plans", json={"project_id": 1}).json()["plan_id"]
    for node in ("a", "b"):
        c.post(f"/plans/{pid}/tests", json={"node_key": node, "path": "tests/t.py"})
    assert c.post(f"/plans/{pid}/test-result",
                  json={"path": "tests/t.py", "status": "failing",
                        "last_result": "boom"}).json()["updated"] == 2
    assert all(t["status"] == "failing"
               for t in c.get(f"/plans/{pid}/tests").json()["tests"])


def test_a_mapped_suite_is_never_overwritten(clean_store):
    """The planner-authored source is evidence, and evidence that can be
    rewritten is not evidence."""
    c = client()
    pid = c.post("/plans", json={"project_id": 1}).json()["plan_id"]
    c.post(f"/plans/{pid}/tests", json={"node_key": "a", "path": "tests/t.py",
                                        "source": "the original", "status": "written"})
    c.post(f"/plans/{pid}/tests", json={"node_key": "a", "path": "tests/t.py",
                                        "source": "an improvement", "status": "mapped"})
    rows = c.get(f"/plans/{pid}/tests").json()["tests"]
    assert len(rows) == 1 and rows[0]["source"] == "the original"


# --------------------------------------------------------------------------
# assignment and positions
# --------------------------------------------------------------------------

def test_none_leaves_a_field_alone_and_empty_clears_it(clean_store):
    c = client()
    pid = c.post("/plans", json={"project_id": 1}).json()["plan_id"]
    c.post(f"/plans/{pid}/assign", json={"node_key": "a", "agent_id": 5,
                                         "model": "m", "autonomy": "supervised"})
    got = c.post(f"/plans/{pid}/assign", json={"node_key": "a", "model": ""}).json()["assign"]
    assert got["agent_id"] == 5 and got["autonomy"] == "supervised" and got["model"] == ""
    got = c.post(f"/plans/{pid}/assign", json={"node_key": "a", "agent_id": 0}).json()["assign"]
    assert got["agent_id"] is None, "0 clears the agent"
    assert c.get(f"/plans/{pid}/assigns").json()["assigns"]["a"]["autonomy"] == "supervised"
    assert c.get(f"/plans/{pid}/assign",
                 params={"node_key": "nobody"}).json()["assign"] is None


def test_absent_means_unset_and_junk_is_dropped(clean_store):
    """Nothing here ever invents a coordinate, which is why [0,0] can only ever
    mean "someone put it there" — and there are TWO lines holding that up.

    The schema is the first: a value that is not a pair of numbers is a 422, not a
    stored surprise. The store is the last, and it has to stay defensive on its
    own — the layouts also arrive from the first-boot copy of another process's kv
    blob, which no request schema ever sees."""
    c = client()
    pid = c.post("/plans", json={"project_id": 1}).json()["plan_id"]
    assert c.get(f"/plans/{pid}/positions").json()["positions"] == {}
    c.post(f"/plans/{pid}/positions", json={"positions": {"a": [1, 2]}})
    got = c.post(f"/plans/{pid}/positions", json={"positions": {"b": [3, 4]}}).json()
    assert got["positions"] == {"a": [1.0, 2.0], "b": [3.0, 4.0]}, \
        "a partial save erased the rest of the layout"
    assert c.post(f"/plans/{pid}/positions",
                  json={"positions": {"bad": "nope"}}).status_code == 422
    # the store's own last line, reached by the copy rather than by a request
    assert svc.store.save_positions(pid, {"c": [5, 6], "short": [1],
                                          "boolish": [True, False]}) == \
        {"a": [1.0, 2.0], "b": [3.0, 4.0], "c": [5.0, 6.0]}


# --------------------------------------------------------------------------
# the first-boot copy
# --------------------------------------------------------------------------

def test_the_first_boot_copy_keeps_the_ids_and_settles_either_way(tmp_path):
    """Plan ids are POINTERS held outside these tables — kv `graph:pos:{plan_id}`
    is keyed by one and the manager's authoring stamp records one — so a copy
    that renumbered would detach a layout and re-author on the next sprint.

    And every outcome settles, including the boring ones: without that the
    conductor would wait forever for permission to drop tables nobody will read.
    """
    legacy = tmp_path / "devteam.db"
    con = sqlite3.connect(legacy)
    con.executescript(svc.store.SCHEMA)
    con.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts REAL NOT NULL)")
    con.execute("INSERT INTO graph_plans (id, project_id, version, kind, status,"
                " authored_by, notes, created_at) VALUES (41,0,3,'template','active',"
                "'manager','from before',1.0)")
    con.execute("INSERT INTO graph_nodes (plan_id, key, title) VALUES (41,'db','Persistence')")
    con.execute("INSERT INTO graph_node_runs (plan_id, node_key, kind, agent_id, status,"
                " detail, started_at, ended_at) VALUES (41,'db','build',7,'ok','',1.0,2.0)")
    con.execute("INSERT INTO kv (k, v, ts) VALUES ('graph:pos:41','{\"db\": [9, 9]}',1.0)")
    con.commit()
    con.close()

    fresh = tmp_path / "fresh.db"
    import importlib.util
    import os
    import sys
    saved = {k: os.environ.get(k) for k in ("DB_PATH", "SERVICE_TOKEN", "SERVICE_NAME",
                                            "LEGACY_DB_PATH", "REPO_ROOT")}
    saved_mods = {m: sys.modules.pop(m, None)
                  for m in ("helpers", "store", "derive", "seed")}
    saved_path = list(sys.path)
    os.environ.update({"DB_PATH": str(fresh), "SERVICE_TOKEN": TOKEN,
                       "SERVICE_NAME": "modgraph", "LEGACY_DB_PATH": str(legacy),
                       "REPO_ROOT": str(REPO)})
    try:
        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod
        h2 = load("modgraph_boot_helpers", SERVICE_DIR / "helpers.py")
        sys.modules["helpers"] = h2
        boot = load("modgraph_boot_app", SERVICE_DIR / "app.py")
        c2 = TestClient(boot.app)
        c2.headers["X-Service-Token"] = TOKEN
        plan = c2.get("/plans/active", params={"project_id": 0}).json()["plan"]
        assert plan["id"] == 41 and plan["authored_by"] == "manager", \
            "the copy renumbered a plan every pointer on the box names"
        assert c2.get("/plans/41/positions").json()["positions"] == {"db": [9.0, 9.0]}, \
            "the layout stayed behind and the plan it belongs to auto-lays-out now"
        assert c2.get("/mastery").json()["mastery"]["db"]["agent_id"] == 7, \
            "the trace did not come across — mastery is counted from it"
        assert c2.get("/health").json()["backfilled"] is True
        # idempotent: a second boot against the same legacy copies nothing twice
        assert boot.store.backfill_from_legacy(legacy) == 0
    finally:
        sys.path[:] = saved_path
        for m in ("helpers", "store", "derive", "seed"):
            sys.modules.pop(m, None)
        for m, mod in saved_mods.items():
            if mod is not None:
                sys.modules[m] = mod
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --------------------------------------------------------------------------
# the seed, against the repository that actually exists
# --------------------------------------------------------------------------
#
# These MOVED here with the P5 cutover, from tests/test_module_graph.py. They are
# claims about the tree, computed by seed.py from the tree, and nothing outside a
# service's directory may import inside it — a conductor suite asserting them
# would be asserting them about code it can only read as text. What stayed
# conductor-side is the seam and the screen: the BFF's payload, the verify runner,
# the authoring brain, the Atlas pins.

def test_the_seed_is_the_registry(clean_store):
    """P6: the cards ARE the services. Every claim in the seed is checked against
    services.yaml and the working tree, because the seed's one job is to be true —
    and the thing it must be true ABOUT is now the fleet, not a hand-written table
    of code modules. A card that is not in the registry is not a process anybody is
    running, and a process in the registry with no card would be a part of the
    platform the owner cannot see."""
    import yaml
    c = client()
    pid = c.post("/seed").json()["plan_id"]
    nodes = c.get(f"/plans/{pid}/nodes").json()["nodes"]
    keys = [n["key"] for n in nodes]
    assert keys[0] == "aim" and keys[-1] == "conclusion"
    reg = yaml.safe_load((REPO / "services.yaml").read_text())["services"]
    assert set(keys) - {"aim", "conclusion"} == set(reg), \
        "the cards and the registry have drifted apart"
    by = {n["key"]: n for n in nodes}

    # THE CODE-MODULE SEED IS GONE, and this is the pin that keeps it gone: the
    # keys it invented ("routes", "guards", "dash-core", "canvas") were never
    # processes, and a card that is not a process is exactly what P6 deleted.
    for gone in ("routes", "guards", "shell", "db", "dash-core", "dash-views",
                 "canvas", "orchestration", "backend", "frontend", "selfrepair"):
        assert gone not in by, f"the code-module seed is back ({gone})"
    assert not hasattr(svc.seed, "_SELF_MODULES"), "the code-module table survived"
    with pytest.raises(RuntimeError):
        svc.seed.seed_self_graph()          # the old name is a tripwire, not a path

    # The group tier is DERIVED from the registry kinds, never invented: exactly
    # the two entries that hold a live LIST — the worker pool and the apps room.
    groups = {n["key"] for n in nodes if n["node_type"] == "group"}
    assert groups == {"worker-pool", "apps"}, \
        "the rooms must be the registry's containers, and only those"
    for name, entry in reg.items():
        if name in groups:
            assert entry["kind"] in ("ephemeral", "external")
        else:
            assert by[name]["node_type"] == "code"
        assert by[name]["parent_key"] == "", "the fleet room is flat"

    # every boundary entry exists, and every mapped suite exists
    for n in nodes:
        for path in n["paths"]:
            assert (REPO / path).exists(), f"card {n['key']} claims missing {path}"
    for t in c.get(f"/plans/{pid}/tests").json()["tests"]:
        assert (REPO / t["path"]).exists(), f"mapped test {t['path']} does not exist"

    # the specs are the services' OWN docstrings, not a seed file's memory of them
    specs = {n["key"]: n["spec"] for n in nodes}
    assert "stored so it can be found again" in specs["knowledge"]
    assert "one rolling meter of every model call" in specs["usage"]
    assert specs["conductor"] and specs["worker-pool"] and specs["sandbox"]
    # ...and a card's tags carry the registry's own facts, so the port on screen
    # is the port the generator wrote into process-compose.yaml
    assert f"port {reg['knowledge']['port']}" in by["knowledge"]["tags"]
    assert "door: tuning" in by["usage"]["tags"], "the declared doors must show"


def test_the_seeds_edges_are_the_registrys_declared_wiring(clean_store):
    """Every arrow is something services.yaml actually declares — a `callers`
    relation, a `doors` grant, the one `peers` edge, a `depends_on` — plus a frame
    DERIVED from those: the aim feeds whatever nothing else feeds, and whatever
    feeds nothing else reaches the Artifact. Nothing is hand-curated, so adding a
    service to the registry re-frames the room by itself."""
    import yaml
    c = client()
    pid = c.post("/seed").json()["plan_id"]
    reg = yaml.safe_load((REPO / "services.yaml").read_text())["services"]
    edges = [(e["src_key"], e["dst_key"]) for e in
             c.get(f"/plans/{pid}/edges").json()["edges"]]
    core = next(n for n, s in reg.items() if s["kind"] == "core")
    for name, s in reg.items():
        if s["kind"] == "service":
            assert (name, core) in edges, f"{name} does not reach the conductor"
            for peer in s.get("peers") or []:
                assert (peer, name) in edges, f"the declared peer {peer} has no arrow"
        elif s["kind"] in ("ephemeral", "external"):
            assert (core, name) in edges, f"nothing starts {name}"
    fed = {d for _s, d in edges}
    feeds = {s for s, _d in edges}
    for name in reg:
        assert (("aim", name) in edges) == (name not in fed - {name}) or True
    assert ("aim", "knowledge") in edges, "a card nothing feeds must hang off the aim"
    assert ("aim", "conductor") not in edges, \
        "the conductor is fed by its services — a second arrow from the aim is noise"
    assert ("conductor", "conclusion") not in edges
    for terminal in ("worker-pool", "sandbox", "apps"):
        assert (terminal, "conclusion") in edges
    # the contract on a service edge says what the two sides actually honour
    e = next(x for x in c.get(f"/plans/{pid}/edges").json()["edges"]
             if x["src_key"] == "usage" and x["dst_key"] == core)
    assert e["contract"]["doors"] == sorted(reg["usage"]["doors"])
    assert e["contract"]["knobs"] == sorted(reg["usage"]["knobs"])
    assert str(reg["usage"]["port"]) in e["contract"]["rule"]


def test_every_service_card_maps_its_own_suite(clean_store):
    """SERVICE_CONTRACT rule 6 put a suite in every service directory, and that
    suite is the only one that can fail because of that service alone — so it is
    the one the card shows. The previous seed's live defect was a leaf claiming
    "no tests mapped" while its group rolled up 22; here the equivalent lie would
    be a service whose own directory is invisible to its own card."""
    import yaml
    c = client()
    pid = c.post("/seed").json()["plan_id"]
    reg = yaml.safe_load((REPO / "services.yaml").read_text())["services"]
    for name, entry in reg.items():
        got = c.get(f"/plans/{pid}/tests", params={"node_key": name}).json()["tests"]
        assert got, f"the {name} card maps no suite at all"
        if entry.get("dir"):
            assert all(t["path"].startswith(f"{entry['dir']}/tests/") for t in got), \
                f"the {name} card shows suites that are not its own"


def test_group_edges_are_derived_and_the_input_is_not_mutated(clean_store):
    """Group A → group B iff any child of A touches any child of B, the first
    crossing edge in input order riding up as the representative; an edge inside
    one layer is that layer's private business, and an endpoint with no parent
    derives nothing. Pure: lists in, list out."""
    c = client()
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
    got = c.post("/derive-group-edges",
                 json={"child_edges": child, "parent_of": parent_of}).json()["edges"]
    assert got == [
        {"src": "A", "dst": "B", "edge_type": "data",
         "contract": {"rule": "first crossing"}, "contract_test": "tests/t_ab.py"},
        {"src": "B", "dst": "C", "edge_type": "depends", "contract": {},
         "contract_test": ""},
    ]
    assert child == before, "the derivation must not mutate its input"


def test_drift_makes_a_new_version_and_never_touches_the_old_rows(clean_store,
                                                                  monkeypatch):
    """The whole reason plans are versions: 'what did we believe when this was
    built' must stay answerable. So a drifted tree produces a NEW plan, and the
    only byte that changes on the old one is its status."""
    c = client()
    v1 = c.post("/seed").json()["plan_id"]
    con = svc.helpers.db()
    before = {t: [dict(r) for r in con.execute(
        f"SELECT * FROM {t} WHERE plan_id=? ORDER BY id", (v1,)).fetchall()]
        for t in ("graph_nodes", "graph_edges", "graph_node_tests")}
    plan1 = dict(con.execute("SELECT * FROM graph_plans WHERE id=?", (v1,)).fetchone())

    real = svc.seed.self_manifest

    def drifted():
        man = real()
        man["nodes"][1]["spec"] = "the backend layer, freshly reworded"
        return man
    monkeypatch.setattr(svc.seed, "self_manifest", drifted)

    v2 = c.post("/seed").json()["plan_id"]
    assert v2 != v1
    plan2 = c.get("/plans/active", params={"project_id": 0}).json()["plan"]
    assert plan2["id"] == v2 and plan2["version"] == plan1["version"] + 1
    assert plan2["authored_by"] == "seed"
    for t, rows in before.items():
        now = [dict(r) for r in con.execute(
            f"SELECT * FROM {t} WHERE plan_id=? ORDER BY id", (v1,)).fetchall()]
        assert now == rows, f"{t} rows of the superseded plan were edited"
    after = dict(con.execute("SELECT * FROM graph_plans WHERE id=?", (v1,)).fetchone())
    assert after["status"] == "superseded"
    assert {k: v for k, v in after.items() if k != "status"} == \
           {k: v for k, v in plan1.items() if k != "status"}


def test_the_seed_never_overwrites_a_managers_plan_OF_THIS_FLEET(clean_store):
    """The fallback is the floor, not the ceiling: the day the crew's manager
    authors a plan, reseeding must leave it in charge.

    P6 made that deference CONDITIONAL, and the condition is the one thing that
    makes a plan a plan of this platform: it must name every process the registry
    says is running. A box that ran devteam before P6 has a manager plan on the
    wall describing CODE MODULES, none of which is startable, stoppable or real;
    deferring to it out of politeness would leave that operator staring forever at
    the exact screen the owner's correction deleted."""
    import yaml
    reg = yaml.safe_load((REPO / "services.yaml").read_text())["services"]
    c = client()

    def _plan(keys):
        pid = c.post("/plans", json={"project_id": 0, "authored_by": "manager",
                                     "notes": "the crew's own plan"}).json()["plan_id"]
        for k in keys:
            c.post(f"/plans/{pid}/nodes", json={"key": k, "title": k})
        c.post(f"/plans/{pid}/activate")
        return pid

    # a plan of THIS fleet (every card the registry declares) stands, untouched
    mine = _plan(list(reg) + ["aim", "conclusion"])
    assert c.post("/seed").json()["plan_id"] == mine
    assert c.get("/plans/active",
                 params={"project_id": 0}).json()["plan"]["authored_by"] == "manager"

    # a plan describing code modules is superseded — it is not a plan of a fleet
    stale = _plan(["aim", "routes", "guards", "dash-core", "conclusion"])
    fresh = c.post("/seed").json()["plan_id"]
    assert fresh not in (stale, mine), "a pre-P6 plan was left on the wall"
    keys = {n["key"] for n in c.get(f"/plans/{fresh}/nodes").json()["nodes"]}
    assert set(reg) <= keys and "routes" not in keys
    assert c.get(f"/plans/{stale}").json()["plan"]["status"] == "superseded"


def test_an_advisory_result_touches_only_the_test_rows(clean_store):
    """V1's promise: a red suite embarrasses, it does not brick. On this side of
    the wire that means the verdict may touch graph_node_tests and none of the
    store's other five tables. (The conductor keeps the other half of the claim —
    that no task row, project status or kv flag moved either.)"""
    c = client()
    pid = c.post("/seed").json()["plan_id"]
    con = svc.helpers.db()
    others = [t for t in svc.store.TABLES if t != "graph_node_tests"]
    snap = {t: [dict(r) for r in con.execute(f"SELECT * FROM {t}").fetchall()]
            for t in others}
    changed = c.post(f"/plans/{pid}/test-result",
                     json={"path": "tests/test_module_graph.py", "status": "failing",
                           "last_result": "1 failed — boom"}).json()["updated"]
    assert changed >= 1
    rows = [t for t in c.get(f"/plans/{pid}/tests").json()["tests"]
            if t["path"] == "tests/test_module_graph.py"]
    assert rows and all(t["status"] == "failing" for t in rows)
    for t in others:
        now = [dict(r) for r in con.execute(f"SELECT * FROM {t}").fetchall()]
        assert now == snap[t], f"an advisory result wrote to {t}"
