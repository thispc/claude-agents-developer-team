"""P5 drills — the module graph's store as a service, and everything the cut
could quietly break.

The whole conductor suite already runs against the mounted service
(tests/conftest.py sets `MODGRAPH_URL` and points the client at it), so this file
is not about turning that on. It is about the things a process boundary breaks
that no other file would notice:

  the doors      every endpoint's auth, and a store with no doors of its own
  the wire       the derivation reconciled ACROSS it, and mastery counted across
                 plan versions on the far side
  the cycle      nothing in services/modgraph imports the conductor — pinned by
                 grep, because that cycle is what the phase exists to break
  degraded       every documented shape, and the one that matters:
                 **THE CREW KEEPS BUILDING WITH THIS SERVICE STOPPED.** That is
                 the deliberate opposite of P4's honest pause, and the reason is
                 one sentence: the graph is observability, not the substrate.
  the door       MODGRAPH_URL is required, and init() is where that is said
  the drop       the six tables leave the conductor's schema — but only once the
                 service says it has the rows, because what a premature drop
                 costs is the TRACE, and mastery is counted from the trace
"""

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from app import db, logs, modgraph, modgraph_health, repair
from conftest import MODGRAPH_TEST_TOKEN, _MODGRAPH_URL, modgraph_service

REPO = Path(__file__).resolve().parent.parent


def _svc_client(*_a, **_k):
    c = TestClient(modgraph_service.app, base_url=_MODGRAPH_URL)
    c.headers["X-Service-Token"] = MODGRAPH_TEST_TOKEN
    return c


@pytest.fixture()
def mg(fresh_db):
    """The conductor talking to the mounted service — which is simply how the
    suite runs now. Kept as a named fixture so every drill says what it needs,
    and so `mg_down` has something to invert."""
    modgraph.seed_self_graph()
    return modgraph


@pytest.fixture()
def mg_down(fresh_db, monkeypatch):
    """The service, unreachable. Not "returns 500" — GONE, which is what a
    stopped process looks like and what every degraded shape is written for."""
    def _dead(*_a, **_k):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(modgraph, "_client", _dead)
    monkeypatch.setattr(modgraph, "_degraded_read", False)
    return modgraph


def _go_live(monkeypatch):
    from app import auth, config as config_mod
    monkeypatch.setattr(config_mod, "AUTH_CONFIGURED", True)
    auth.save_settings(1, {"anthropic_api_key": "sk-ant-test-not-a-real-key"})


# --------------------------------------------------------------------------
# the doors
# --------------------------------------------------------------------------

def test_every_endpoint_needs_the_service_token(mg):
    """The store holds the operator's own map of their own repository — what the
    modules are, which suites are red, who works what. `/health` and the spec are
    the only unauthenticated things on it."""
    anon = TestClient(modgraph_service.app, base_url=_MODGRAPH_URL)
    assert anon.get("/health").status_code == 200
    assert anon.get("/openapi.json").status_code == 200
    # Enumerated from the COMMITTED spec, not from the app's route table: the
    # contract is the list of things a caller may reach, so it is the list every
    # operation has to be gated on. A route added without a spec entry is caught
    # by the drift test below; one added with one is caught here.
    spec = json.loads((REPO / "services" / "modgraph" / "openapi.json").read_text())
    checked = 0
    for path, ops in spec["paths"].items():
        if path in ("/health", "/openapi.json"):
            continue
        for method in ops:
            concrete = re.sub(r"\{[^}]+\}", "1", path)
            got = anon.request(method.upper(), concrete, json={})
            assert got.status_code == 401, \
                f"{method} {concrete} answered {got.status_code} with no token"
            checked += 1
    assert checked >= 20, f"only {checked} verbs in the contract — did routes move?"
    bad = TestClient(modgraph_service.app, base_url=_MODGRAPH_URL)
    bad.headers["X-Service-Token"] = "nope"
    assert bad.get("/plans/active").status_code == 401


def test_the_store_asks_the_conductor_for_nothing(mg):
    """No doors, no peers, no extra env — and that is not an omission, it is the
    shape that broke the cycle. `modgraph_author ↔ repair` was the one genuine
    import cycle in the decomposition; P5 cut it by keeping the AUTHORING brain in
    the conductor and giving the store nothing to reach back with."""
    import yaml
    entry = yaml.safe_load((REPO / "services.yaml").read_text())["services"]["modgraph"]
    assert not entry.get("doors") and not entry.get("peers") and not entry.get("env")
    topo = json.loads((REPO / "data" / "fleet_topology.json").read_text())["services"]
    assert topo["modgraph"]["port"] == 8886
    assert topo["modgraph"]["doors"] == [] and topo["modgraph"]["knobs"] == []
    assert topo["modgraph"]["public"] is False


def test_nothing_in_the_service_imports_the_conductor(mg):
    """THE CYCLE, pinned. The store is the end that must not reach back — and an
    HTTP call back into the conductor would be worse than the import cycle, since
    the conductor is the caller and would be waiting on itself."""
    offenders = []
    for f in sorted((REPO / "services" / "modgraph").rglob("*.py")):
        if "tests" in f.parts or "__pycache__" in f.parts:
            continue
        src = f.read_text()
        # `CONDUCTOR_URL` is deliberately NOT in this list as a bare word: the
        # seed quotes it inside a CONTRACT describing the worker's env, which is
        # a sentence about the repository, not a reach into it. What is banned is
        # reading it — the address of the process this one must never call.
        for pat in (r"^\s*from\s+app[\s.]", r"^\s*import\s+app\b",
                    r"^\s*from\s+conductor", r"conductor\.app",
                    r"environ.*CONDUCTOR_URL", r"getenv.*CONDUCTOR_URL"):
            if re.search(pat, src, re.M):
                offenders.append(f"{f.name}: {pat}")
    assert offenders == [], f"the store reaches back into the conductor: {offenders}"


def test_the_authoring_brain_stayed_and_still_needs_the_conductor(mg):
    """The other half of the same claim: modgraph_author did NOT move, and the
    reason is visible in what it imports. A test that only checked the service
    would pass on a day someone moved the brain too."""
    src = (REPO / "conductor" / "app" / "modgraph_author.py").read_text()
    assert "providers" in src and "repair" in src and "tuning" in src
    assert (REPO / "conductor" / "app" / "modgraph_author.py").exists()
    assert not (REPO / "services" / "modgraph" / "author.py").exists()


# --------------------------------------------------------------------------
# the derivation and the mastery JOIN, across the wire
# --------------------------------------------------------------------------

def test_the_reconciliation_survives_the_boundary(mg, root_client):
    """`derive_group_edges` crossed WITH the rows because it is graph domain, not
    presentation: the seed calls it to build the plan it writes and the payload
    calls it again to reconcile what a manager authored. Both ends must give the
    same answer, or the stored plan and the rendered graph tell different stories
    about the same repository."""
    plan_id = modgraph.create_plan(0, authored_by="manager", notes="drill")
    modgraph.add_node(plan_id, "aim", "aim", node_type="aim")
    for g in ("G1", "G2", "G3"):
        modgraph.add_node(plan_id, g, g, node_type="group")
    for key, parent in (("r", "G1"), ("o", "G1"), ("d", "G2"), ("x", "G3")):
        modgraph.add_node(plan_id, key, key, node_type="code", parent_key=parent)
    modgraph.add_node(plan_id, "conclusion", "done", node_type="conclusion")
    modgraph.add_edge(plan_id, "o", "d", edge_type="data",
                      contract={"rule": "the crossing"})
    modgraph.add_edge(plan_id, "G1", "G3")            # fabricated — no child edge
    modgraph.activate(plan_id)

    pairs = [(e["src"], e["dst"])
             for e in root_client.get("/api/graph/self").json()["edges"]]
    assert ("G1", "G2") in pairs, "the missed crossing must be served"
    assert ("G1", "G3") not in pairs, "an arrow no child edge backs must not be served"
    # ...and the shim's own call agrees, so the BFF and the seed cannot diverge
    direct = modgraph.derive_group_edges(
        [{"src": "o", "dst": "d", "edge_type": "data",
          "contract": {"rule": "the crossing"}, "contract_test": ""}],
        {"r": "G1", "o": "G1", "d": "G2", "x": "G3"})
    assert [(e["src"], e["dst"]) for e in direct] == [("G1", "G2")]


def test_mastery_is_counted_on_the_far_side_across_plan_versions(mg):
    """The one JOIN in the feature (`graph_node_runs ⋈ graph_plans`) stayed a
    JOIN, because both tables went. Node keys are the stable identity, so a
    replan must not amnesty away who knows a module best — drilled here across
    the wire, where a naive port would have had to compose it."""
    v1 = modgraph.active_plan(0)["id"]

    def closed(pid, key, agent, kind="build"):
        rid = modgraph.note_run(pid, key, kind, agent_id=agent)
        modgraph.close_run(rid, "ok", "done")

    for _ in range(2):
        closed(v1, "db", 71)
    assert modgraph_health.mastery(0)["db"] == {"agent_id": 71, "runs": 2, "master": False}

    v2 = modgraph.create_plan(0, authored_by="manager")
    modgraph.add_node(v2, "db", "Persistence")
    modgraph.activate(v2)
    closed(v2, "db", 71, kind="verify")
    got = modgraph_health.mastery(0)["db"]
    assert got == {"agent_id": 71, "runs": 3, "master": True}, \
        "a replan amnestied the trace — mastery must count by node KEY"


def test_affected_selection_crosses_and_the_runner_did_not(mg, root_client, monkeypatch):
    """The service says WHICH files; the conductor runs them, in its own checkout,
    and posts back only the verdict. A store that spawned pytest in another
    process's working tree would have imported that tree's whole world."""
    import subprocess
    from app import shell
    calls = []

    def sh(*cmd, cwd=None, timeout=None, stdin=None):
        calls.append({"cmd": [str(c) for c in cmd], "cwd": str(cwd)})
        return subprocess.CompletedProcess(cmd, 0, "9 passed in 0.12s\n", "")
    monkeypatch.setattr(shell, "sh", sh)

    r = root_client.post("/api/graph/self/verify", json={"node": "knowledge"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["files"] == modgraph.affected_tests(
        modgraph.active_plan(0)["id"], "knowledge"), \
        "the runner ran a different set from the one the store selected"
    assert len(calls) == 1 and str(REPO) == calls[0]["cwd"], \
        "the suite must run in the conductor's own checkout"
    # ...and the verdict landed in the SERVICE's rows
    rows = modgraph_service.helpers.db().execute(
        "SELECT status FROM graph_node_tests WHERE path=?",
        ("tests/test_knowledge_service.py",)).fetchall()
    assert rows and all(r[0] == "passing" for r in rows)
    trace = modgraph.runs(modgraph.active_plan(0)["id"], "knowledge")
    assert trace[-1]["kind"] == "verify" and trace[-1]["status"] == "ok"


def test_a_whole_plan_crosses_in_one_call(mg, root_client, monkeypatch):
    """Both bulk writers use `/plans/import`, so a rebuilt version can never be
    ACTIVATED with half its edges. Counted at the wire, because "one call" is the
    claim and fifty round trips is what it replaced."""
    posts = []
    real = modgraph._post

    def counting(path, payload=None, **kw):
        posts.append(path)
        return real(path, payload, **kw)
    monkeypatch.setattr(modgraph, "_post", counting)

    r = root_client.post("/api/graph/self/node/knowledge/remove")
    assert r.status_code == 200, r.text
    assert posts.count("/import-plan") == 1
    assert not any(p.endswith("/nodes") or p.endswith("/edges") for p in posts), \
        "the rebuild went back to writing rows one at a time"
    v2 = r.json()["plan"]["id"]
    assert "knowledge" not in {n["key"] for n in modgraph.nodes(v2)}
    assert modgraph.get_plan(v2)["status"] == "active"


# --------------------------------------------------------------------------
# degraded: every documented shape
# --------------------------------------------------------------------------

def test_the_trace_verbs_are_a_gap_and_never_a_raise(mg_down):
    """A trace gap is recorded as a gap. `note_run` returning 0 is LOAD-BEARING:
    the engine stores what it gets and closes it later, and a fabricated id would
    close somebody else's row when the service came back."""
    assert modgraph.note_run(1, "db", "build", agent_id=7) == 0
    modgraph.close_run(99, "ok", "done")                 # no raise
    assert modgraph.update_test_result(1, "tests/t.py", "failing") == 0
    assert any(r["event"] == "modgraph_degraded" for r in logs.rows())


def test_reads_are_empty_and_say_so(mg_down):
    """Empty-because-unreadable and empty-because-that-is-the-graph must never
    look the same, which is the entire job of `degraded()`."""
    assert modgraph.active_plan(0) is None
    assert modgraph.degraded() is True
    assert modgraph.nodes(1) == [] and modgraph.edges(1) == []
    assert modgraph.tests(1) == [] and modgraph.runs(1) == []
    assert modgraph.positions(1) == {} and modgraph.assigns(1) == {}
    assert modgraph.get_assign(1, "db") is None
    assert modgraph.affected_tests(1, "db") == []
    assert modgraph.self_manifest() == {}
    assert modgraph.seed_self_graph() == 0
    assert modgraph.health() is False


def test_mastery_degrades_to_none_not_to_nobody_has_earned_anything(mg_down):
    """NONE, NOT {}. The authoring pass's "a master keeps its module" rule reads
    this, and an outage that read as "nobody has earned anything" would let one
    reshuffle undo every earned continuity on the box."""
    assert modgraph.mastery(0) is None
    assert modgraph_health.mastery(0) is None


def test_the_atlas_says_unavailable_instead_of_drawing_an_empty_repository(
        mg_down, root_client):
    """200 with an honest banner, not a 503 — the opposite call from the Studio's,
    and for a reason: nothing the Atlas draws is ever saved back, so the only
    failure mode is what the operator is told. A screen that still shows the
    crew's phase and the uptime beats a red toast with nothing behind it."""
    r = root_client.get("/api/graph/self")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["degraded"] is True and out["plan"] is None
    assert "not answering" in out["reason"] and "keeps building" in out["reason"]
    assert out["nodes"] == [] and out["edges"] == [] and out["runs"] == []
    assert out["conclusion"]["health"] == "unknown"
    assert out["models"], "the model list is the conductor's and must survive"


def test_the_verb_endpoints_refuse_honestly_rather_than_pretending(mg_down, root_client):
    for path, body in (("/api/graph/self/node/db/config", {"model": ""}),
                       ("/api/graph/self/verify", {"node": "db"}),
                       ("/api/graph/self/layout", {"positions": {"db": [1, 2]}})):
        r = root_client.post(path, json=body)
        assert r.status_code == 503, f"{path} answered {r.status_code}"
        assert "not answering" in r.json()["detail"]
    assert root_client.get("/api/graph/self/node/db").status_code == 503


# --------------------------------------------------------------------------
# THE DRILL THIS PHASE IS JUDGED ON
# --------------------------------------------------------------------------

def test_the_crew_keeps_building_when_the_graph_is_down(mg_down, monkeypatch):
    """THE JUDGEMENT CALL OF P5, drilled explicitly.

    When the LIFEWORLD is down the crew PAUSES, and that is honest: a crew
    without its specialists would still be spending, just anonymously, and
    nothing would learn from the outcome. When the MODULE GRAPH is down the crew
    must NOT pause. The code still gets written, the tests still run, the commit
    still lands; all that is lost is the record of which module it happened on.
    A platform that stopped improving itself because its map was unavailable
    would have the priority exactly backwards.

    So: every graph hook on the build path is exercised with the store gone, and
    the sprint tick must not sleep, must not raise, and must not name the graph
    as a reason for anything.
    """
    _go_live(monkeypatch)
    repair.toggle(True)
    task = {"factor": "correctness", "title": "Fix the decay in knowledge.py",
            "brief": "conductor/app/knowledge.py drops rows", "files": []}

    # the three hooks, in the order a build fires them
    repair._graph_build_started(task)
    assert "graph_run" not in task and "graph_node" not in task, \
        "a run id was invented for a store that never answered"
    repair._graph_build_ended(task, True, "landed")
    repair._graph_verified(task, True, "9 passed")
    assert repair._graph_node_for(task) is None

    # nothing anywhere claimed the build failed, and nothing raised
    rows = logs.rows()
    assert not any(r["event"] == "graph_hook_failed" for r in rows), \
        "a gap in the map was reported as a broken hook"
    assert any(r["event"] == "modgraph_degraded" for r in rows), \
        "the gap must still be RECORDED — it is a gap, not a silence"

    # and the tick does not stand the crew down over it
    asyncio.run(repair.advance(repair.state()))
    st = repair.state()
    assert st.get("sleep_reason") != "modgraph down"
    assert "graph" not in str(st.get("sleep_reason") or "").lower(), \
        f"the crew stood down because its MAP was unavailable: {st.get('sleep_reason')}"


def test_authoring_refuses_before_it_spends_when_the_store_is_unreachable(
        mg_down, monkeypatch):
    """One authoring pass is a REAL model call on the owner's quota. With the
    store unreachable it would author a decomposition of an empty inventory and
    then fail to write it — so it refuses first, and leaves the staleness stamp
    alone so the next sprint tries again."""
    _go_live(monkeypatch)
    from app import modgraph_author, providers
    calls = []

    async def fake(*a, **k):
        calls.append(1)
        return "{}"
    monkeypatch.setattr(providers, "complete", fake)
    monkeypatch.setattr(repair, "_live", lambda: True)
    monkeypatch.setattr(repair, "ensure_team", lambda: {"agents": {}})

    assert asyncio.run(modgraph_author.author_self_plan()) is None
    assert calls == [], "a model call was spent on a plan with nowhere to land"
    assert modgraph_author.should_author() is True, "the retry was stamped away"
    assert any(r["event"] == "graph_author_skipped" for r in logs.rows())


# --------------------------------------------------------------------------
# the cutover itself: a loud door, and a drop that waits
# --------------------------------------------------------------------------
#
# `test_unsetting_the_url_puts_the_conductor_back_in_the_monolith` lived here
# between the two commits and is GONE, along with the vendored body it exercised.
# The rollback after the cutover is `git revert`, not an environment variable, and
# the service's own database survives it either way because the rows were copied
# out and never moved.

def test_the_conductor_refuses_to_boot_without_the_service(monkeypatch):
    """One clear message naming run-local.sh, at the honest moment. IMPORTING this
    module must stay free — tools, tests and the seed do it without ever reaching
    the store — so the refusal lives in init(), not at import."""
    monkeypatch.setattr(modgraph, "_URL", "")
    with pytest.raises(RuntimeError) as e:
        modgraph.init()
    assert "MODGRAPH_URL" in str(e.value) and "run-local.sh" in str(e.value)


def test_nothing_reaches_for_the_deleted_fallback():
    """The file is gone; what matters is that no code still reaches for it. (The
    client names it in a docstring as history — a comment cannot import.)"""
    assert not (REPO / "conductor" / "app" / "_modgraph_legacy.py").exists()
    importers = [str(f) for f in (REPO / "conductor").rglob("*.py")
                 if re.search(r"^\s*(from|import).*_modgraph_legacy", f.read_text(), re.M)]
    assert importers == [], f"_modgraph_legacy still has importers: {importers}"


def test_the_client_has_one_mode_and_no_branch_left_to_take():
    """A `sys.modules` swap surviving the cutover would mean a conductor that
    silently kept its own six tables on a box where MODGRAPH_URL happened to be
    unset — green tests, and a graph nobody else could see."""
    src = (REPO / "conductor" / "app" / "modgraph.py").read_text()
    assert "sys.modules[__name__]" not in src
    assert not hasattr(modgraph, "SCHEMA"), "the client declares a schema again"
    assert modgraph.__name__ == "app.modgraph"


def test_the_six_tables_are_dropped_only_once_the_service_has_copied(fresh_db,
                                                                     monkeypatch):
    """NOTHING ORDERS THE TWO PROCESSES. The fleet starts the conductor and the
    service together, so init() can easily run before the service has attached —
    and what a premature drop costs is THE TRACE, which is the one part that
    cannot be rebuilt.

    Plans, nodes and edges regenerate from the working tree in under a second and
    the manager re-authors on the next lineup change. `graph_node_runs` does not:
    it is every build and verify any specialist has closed, and MASTERY IS COUNTED
    FROM IT, never stored. Drop it early and every module silently loses its
    master, so the next authoring pass reshuffles specialists off the modules they
    had earned — with nothing anywhere reporting the loss. `graph_assign` goes the
    same way: the operator's own steering. So the drop asks first.
    """
    db._execute("CREATE TABLE IF NOT EXISTS graph_node_runs (id INTEGER PRIMARY KEY,"
                " plan_id INTEGER NOT NULL, node_key TEXT NOT NULL, kind TEXT,"
                " task_id INTEGER, agent_id INTEGER, status TEXT, detail TEXT,"
                " started_at REAL NOT NULL, ended_at REAL)")
    db._execute("CREATE TABLE IF NOT EXISTS graph_assign (id INTEGER PRIMARY KEY,"
                " plan_id INTEGER NOT NULL, node_key TEXT NOT NULL, agent_id INTEGER,"
                " home_id INTEGER, model TEXT, autonomy TEXT)")
    db._execute("INSERT INTO graph_node_runs (id, plan_id, node_key, kind, agent_id,"
                " status, detail, started_at, ended_at)"
                " VALUES (1, 9, 'db', 'build', 71, 'ok', '', 1.0, 2.0)")
    db.kv_set("graph:pos:9", {"db": [1, 2]})
    # Three graph kv keys are the CONDUCTOR's own and must survive: the operator's
    # directives to the manager, the authoring staleness stamp, and the assignment
    # pool pointer. Only the layouts moved, because only they are keyed to a plan
    # id in another process's database.
    db.kv_set("graph:notes:0", [{"ts": 1.0, "note": "do not reintroduce 'canvas'"}])
    db.kv_set("graph:authored:0", {"factors": ["speed"], "plan_id": 9})
    db.kv_set("graph:pool:0", {"world_id": 2, "room_id": 29})

    def _not_settled(*_a, **_k):
        return _FakeHealth({"ok": True, "backfilled": False})
    monkeypatch.setattr(modgraph, "_client", _not_settled)
    modgraph.init()
    assert db._rows("SELECT COUNT(*) AS n FROM graph_node_runs")[0]["n"] == 1, \
        "the trace was dropped before the service had it — mastery cannot be rebuilt"
    assert db.kv_get("graph:pos:9") is not None

    # ...and an unreachable service is not fatal either: a conductor that refused
    # to boot because a PEER was still starting is how a fleet takes itself down
    # in a ring.
    def _dead(*_a, **_k):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(modgraph, "_client", _dead)
    modgraph.init()
    assert db._rows("SELECT COUNT(*) AS n FROM graph_node_runs")[0]["n"] == 1

    # Once it says it is settled — which includes "there was nothing to copy" —
    # all six go, and the layouts with them: a `graph:pos:{plan_id}` left behind
    # names a plan id in another process's database, which is not a layout.
    monkeypatch.setattr(modgraph, "_client", _svc_client)
    modgraph.init()
    names = {r["name"] for r in
             db._rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not (names & set(modgraph._LEGACY_TABLES)), \
        f"the conductor still declares {sorted(names & set(modgraph._LEGACY_TABLES))}"
    assert db.kv_get("graph:pos:9") is None
    assert modgraph._LEGACY_KV_PREFIX == "graph:pos:"
    # ...and the conductor's own graph kv is untouched. A prefix sweep that took
    # `graph:notes:0` with it would silently un-say an explicit human directive —
    # the manager would reintroduce the module the operator removed.
    assert db.kv_get("graph:notes:0")[0]["note"] == "do not reintroduce 'canvas'"
    assert db.kv_get("graph:authored:0")["factors"] == ["speed"]
    assert db.kv_get("graph:pool:0")["room_id"] == 29


def test_health_reports_the_first_boot_decision_the_drop_will_wait_on(mg):
    """Commit B drops the six tables ONLY once this is true, because nothing
    orders the two processes — and what a premature drop costs here is the
    TRACE, from which mastery is counted. `backfilled` means "the first-boot
    decision has been made, either way", including "there was nothing to copy"."""
    got = _svc_client().get("/health").json()
    assert got["ok"] is True and got["backfilled"] is True
    marker = json.loads(modgraph_service.helpers.kv_get("backfilled_from"))
    assert marker["tables"] == list(modgraph_service.store.TABLES)
    assert marker["reason"], "a settled marker with no reason explains nothing at 3am"


def test_the_committed_spec_is_what_the_service_serves():
    """oasdiff gates the diff between commits; this gates the diff between the
    file and the code that is supposed to implement it."""
    served = TestClient(modgraph_service.app).get("/openapi.json").json()
    assert served == json.loads(
        (REPO / "services" / "modgraph" / "openapi.json").read_text())


def test_the_registry_generates_this_services_env_and_token(tmp_path):
    import shutil
    sys.path.insert(0, str(REPO / "tools"))
    import gen_fleet
    shutil.copy(REPO / "services.yaml", tmp_path / "services.yaml")
    gen_fleet.generate(tmp_path, {})
    assert "MODGRAPH_URL=http://127.0.0.1:8886" in \
        (tmp_path / "data/env/conductor.env").read_text()
    env = (tmp_path / "data/env/modgraph.env").read_text()
    assert "PORT=8886" in env and "DB_PATH=data/modgraph.db" in env
    assert (tmp_path / "data/tokens/modgraph.token").exists()
    compose = (tmp_path / "process-compose.yaml").read_text()
    assert "services/modgraph" in compose and "8886" in compose


class _FakeHealth:
    """The two lines of an httpx client the drop path actually uses."""

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, _path, **_kw):
        return httpx.Response(200, json=self.payload)
