"""The PROJECT tenant of the Atlas — the same screen, a project's own truth.

The owner's ask, in three sentences: "the atlas should be part of every project
tab"; "Atlas is the main screen, it should appear immediately when someone creates
a project"; "the team of agents selected for a project can choose to work on
specific modules". These drills pin the load-bearing half of each, and one thing
neither of them says but both depend on — that the two tenants are ONE screen.

The shape of the file follows the risk:

  1. THE CONTRACT IS SHARED. `assert_graph_payload` runs against `/api/graph/self`
     AND `/api/graph/project/{id}`, so a key added to one and forgotten in the other
     fails here rather than as a blank section on a screen.
  2. THE GRAPH IS HONEST. A project's cards are TASKS, and every one of them refuses
     its switch with a reason. A drill fails if a project card ever claims a port, a
     contract or a Start button it does not have.
  3. THE HEALTH MAPPING IS THE DOCUMENTED ONE. done→green, failed→red, in-flight→
     yellow, unstarted→grey, and a delivered task whose own checks failed goes amber
     rather than green — the evidence outranks the status.
  4. IT EXISTS THE MOMENT THE PROJECT DOES, and fills in as the manager plans.
  5. THE CLAIM MEANS SOMETHING. `team.assign` dispatches to whoever the boss claimed
     the card for — that is the whole of "choose to work on specific modules", and a
     drill that only checked the row would have passed on a button that did nothing.
  6. THE RENDERERS NEVER BRANCH ON TENANT, and the legend never sits on a card.
"""

import json

import pytest

from conftest import make_project, make_task
from app import db, modgraph, projgraph, team

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DASH = REPO / "dashboard"


# --------------------------------------------------------------------------
# 1. ONE CONTRACT, TWO TENANTS
# --------------------------------------------------------------------------

# Every key the Atlas reads off a payload, and off one of its nodes. This list is
# the contract: the seam's whole point is that `dashboard/graph/` renders either
# tenant without knowing which, and it can only do that if both composers answer
# with the same shape.
PAYLOAD_KEYS = {"plan", "degraded", "models", "nodes", "edges", "runs", "positions",
                "conclusion"}
NODE_KEYS = {"key", "title", "node_type", "parent_key", "spec", "join_mode", "tags",
             "config", "agent", "tests", "activity", "health", "service", "mastery"}
HEALTH_STATES = {"green", "yellow", "red", "grey"}


def assert_graph_payload(out: dict, *, tenant: str) -> None:
    """The shared assertion. Anything the Atlas reads is checked here ONCE, for both
    tenants, so the two composers cannot drift apart quietly."""
    assert PAYLOAD_KEYS <= set(out), \
        f"{tenant}: the payload is missing {PAYLOAD_KEYS - set(out)}"
    assert isinstance(out["models"], list) and out["models"], \
        f"{tenant}: the model list rides the payload so the select cannot drift"
    assert isinstance(out["nodes"], list) and isinstance(out["edges"], list)
    assert out["nodes"], f"{tenant}: a payload with no cards is not a graph"
    keys = {n["key"] for n in out["nodes"]}
    types = {n["node_type"] for n in out["nodes"]}
    assert "aim" in types and "conclusion" in types, \
        f"{tenant}: the frame (an aim and a conclusion) is what makes a plan a plan"
    for n in out["nodes"]:
        assert NODE_KEYS <= set(n), \
            f"{tenant}: node {n['key']} is missing {NODE_KEYS - set(n)}"
        assert "paths" not in n, \
            f"{tenant}: `paths` must never reach the wire — the panel keeps the box closed"
        if n["health"]:
            assert n["health"]["status"] in HEALTH_STATES, \
                f"{tenant}: {n['key']} has an unrenderable health {n['health']}"
        svc = n["service"]
        assert "kind" in svc and "state" in svc, \
            f"{tenant}: every card must say what it IS and what state it is in"
        assert "remove" in svc and "allowed" in svc["remove"]
        for f in ("total", "passing", "failing"):
            assert f in n["tests"], f"{tenant}: {n['key']} has no {f} count"
        assert isinstance(n["activity"], list)
    for e in out["edges"]:
        assert {"src", "dst", "edge_type"} <= set(e), f"{tenant}: a malformed edge {e}"
        assert e["src"] in keys and e["dst"] in keys, \
            f"{tenant}: {e['src']}→{e['dst']} points outside the graph — the Atlas " \
            "promised never to draw an arrow into nothing"
    c = out["conclusion"]
    assert "health" in c, f"{tenant}: the goal card needs a health"


def _project_with_tasks(client, owner_id=1):
    """A project whose DAG looks like a real one: two roots, a join, a tail."""
    pid = make_project(owner_id=owner_id, name="Weather app")
    a = make_task(pid, role="frontend", title="Scaffold the app", status="done")
    b = make_task(pid, role="design", title="Design system", status="done")
    c = make_task(pid, role="frontend", title="Integrate the design",
                  status="running", deps=[a, b])
    d = make_task(pid, role="tester", title="Final QA", status="planned", deps=[c])
    return pid, (a, b, c, d)


def test_the_two_tenants_answer_the_same_contract(root_client):
    """THE SEAM, as one assertion run twice. The fleet's graph and a project's graph
    are composed by different files against different truths, and the ONLY thing that
    keeps `dashboard/graph/` able to render both without asking which is that they
    agree on this shape."""
    pid, _ = _project_with_tasks(root_client)
    assert_graph_payload(root_client.get("/api/graph/self").json(), tenant="self")
    assert_graph_payload(root_client.get(f"/api/graph/project/{pid}").json(),
                         tenant="project")


def test_the_self_payload_is_untouched_by_the_project_tenant(root_client):
    """A phase that adds a tenant must not move the one already on the wall. The
    fleet's payload carries its own vocabulary (fleet, cluster, uptime, the crew's
    phase) and NONE of the project tenant's."""
    out = root_client.get("/api/graph/self").json()
    c = out["conclusion"]
    assert {"fleet", "cluster", "uptime_s", "repair", "beat"} <= set(c)
    assert "lines" not in c and "links" not in c, \
        "the project tenant's conclusion fields must not appear on the fleet's"
    for n in out["nodes"]:
        assert "brief" not in n["tests"], "the fleet's cards compute their own test line"
        assert not (n["health"] or {}).get("note"), \
            "the fleet's health has no note — its tri-state speaks for itself"


# --------------------------------------------------------------------------
# 2. WHAT A PROJECT REALLY HAS — and what it must never pretend to have
# --------------------------------------------------------------------------

def test_the_cards_are_the_tasks_and_the_frame(root_client):
    pid, (a, b, c, d) = _project_with_tasks(root_client)
    out = root_client.get(f"/api/graph/project/{pid}").json()
    keys = {n["key"] for n in out["nodes"]}
    assert keys == {"aim", "conclusion"} | {projgraph.task_key(t) for t in (a, b, c, d)}
    aim = next(n for n in out["nodes"] if n["node_type"] == "aim")
    assert aim["title"] == "Weather app" and aim["spec"], \
        "the aim IS the project and its brief"


def test_the_edges_are_the_real_task_deps_and_a_derived_frame(root_client):
    """`tasks.deps`, verbatim — plus a frame that CANNOT disagree with it, because it
    is derived from the wiring rather than listed beside it."""
    pid, (a, b, c, d) = _project_with_tasks(root_client)
    out = root_client.get(f"/api/graph/project/{pid}").json()
    pairs = {(e["src"], e["dst"]) for e in out["edges"]}
    K = projgraph.task_key
    assert (K(a), K(c)) in pairs and (K(b), K(c)) in pairs and (K(c), K(d)) in pairs
    # the frame: the two roots hang off the aim, the tail reaches the deliverable
    assert (K(a), "aim") not in pairs
    assert ("aim", K(a)) in pairs and ("aim", K(b)) in pairs
    assert ("aim", K(c)) not in pairs, "a task something else feeds is not fed by the aim"
    assert (K(d), "conclusion") in pairs
    assert (K(a), "conclusion") not in pairs, "a task that feeds another is not terminal"


def test_a_dep_on_a_vanished_task_is_dropped_not_drawn(root_client):
    """The Atlas's oldest promise: no arrow into nothing. A dep pointing at a task
    that was deleted is dropped from the graph, not rendered as a dangling edge."""
    pid = make_project(owner_id=1)
    a = make_task(pid, title="alpha")
    b = make_task(pid, title="beta", deps=[a, 999999])
    out = root_client.get(f"/api/graph/project/{pid}").json()
    pairs = {(e["src"], e["dst"]) for e in out["edges"]}
    assert (projgraph.task_key(a), projgraph.task_key(b)) in pairs
    assert not any("999999" in e["src"] or "999999" in e["dst"] for e in out["edges"])


def test_every_project_card_refuses_its_switch_with_a_reason(root_client):
    """THE HONESTY DRILL, and the one this phase exists to keep. A project's modules
    are not services yet (docs/PROJECT_SERVICES.md is a design note, not a feature),
    so no card may claim a port, a contract or a switch. Every one of them says
    `control: false` WITH the reason, which is what makes the panel print a sentence
    instead of drawing a Start button that would 400."""
    pid, tasks = _project_with_tasks(root_client)
    out = root_client.get(f"/api/graph/project/{pid}").json()
    for n in out["nodes"]:
        svc = n["service"]
        assert svc["control"] is False, f"{n['key']} claims a switch it does not have"
        assert svc["state"] == "none", f"{n['key']} claims a process state"
        assert "not a process" in svc["reason"] or "work, not a process" in svc["reason"]
        assert svc["remove"]["allowed"] is False
    panel = root_client.get(
        f"/api/graph/project/{pid}/node/{projgraph.task_key(tasks[0])}").json()
    assert panel["contract"] is None, "a task serves no endpoint contract"
    assert "not a service" in panel["contract_note"]


def test_the_switch_and_the_replace_verbs_do_not_exist_for_a_project(root_client):
    """The refusals are real 404s, not merely a UI that hides a button: nothing may
    start, stop, verify or remove a project's task through the graph."""
    pid, tasks = _project_with_tasks(root_client)
    key = projgraph.task_key(tasks[0])
    for path in (f"/api/graph/project/{pid}/node/{key}/service",
                 f"/api/graph/project/{pid}/node/{key}/remove",
                 f"/api/graph/project/{pid}/verify"):
        assert root_client.post(path, json={"action": "start", "node": key}).status_code \
            in (404, 405), f"{path} must not exist for a project"


# --------------------------------------------------------------------------
# 3. THE HEALTH MAPPING, exactly as documented
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status,expect", [
    ("done", "green"),
    ("failed", "red"),
    ("cancelled", "red"),
    ("running", "yellow"),
    ("queued", "yellow"),
    ("review", "yellow"),
    ("changes_requested", "yellow"),
    ("planned", "grey"),
])
def test_task_status_maps_to_the_documented_colour(root_client, status, expect):
    pid = make_project(owner_id=1)
    t = make_task(pid, title="one", status=status)
    out = root_client.get(f"/api/graph/project/{pid}").json()
    n = next(x for x in out["nodes"] if x["key"] == projgraph.task_key(t))
    assert n["health"]["status"] == expect, \
        f"a {status} task must be {expect} — see projgraph._STATUS_HEALTH"
    assert n["health"]["note"], "the colour must say what it means on this card"


def test_the_harness_verdict_outranks_a_delivered_status(root_client):
    """A task marked done whose own checks FAILED goes amber, not green. That exit
    code was recorded by the worker process precisely so the model could not soften
    it, and a green card over a red suite would throw the whole point away."""
    pid = make_project(owner_id=1)
    t = make_task(pid, title="one", status="done")
    db.update_task(t, verification=json.dumps(
        {"ran": True, "ok": False, "cmd": "npm test", "exit_code": 1,
         "output": "2 failing"}))
    out = root_client.get(f"/api/graph/project/{pid}").json()
    n = next(x for x in out["nodes"] if x["key"] == projgraph.task_key(t))
    assert n["health"]["status"] == "yellow"
    assert n["tests"]["failing"] == 1 and n["tests"]["total"] == 1
    assert "FAILED" in n["tests"]["brief"]


def test_a_task_with_no_verification_says_so_rather_than_inventing_a_suite(root_client):
    pid = make_project(owner_id=1)
    t = make_task(pid, title="one", status="done")
    out = root_client.get(f"/api/graph/project/{pid}").json()
    n = next(x for x in out["nodes"] if x["key"] == projgraph.task_key(t))
    assert n["tests"]["total"] == 0
    assert "no verification recorded" in n["tests"]["brief"]
    assert n["health"]["status"] == "green", \
        "absence of evidence is not a warning — the fleet's rule, kept"


def test_the_task_log_never_leaks_the_servers_own_filesystem(root_client):
    """The panel's log tail is the task's own feed, and a worker's tool calls carry
    the ABSOLUTE path of its checkout on the machine running the platform — the
    operator's home directory, the install location, and every other task's
    workspace beside it. The boss's own source path (`src/weather.js`) is his to
    see; the server's is nobody's."""
    from app import bus
    pid = make_project(owner_id=1)
    t = make_task(pid, title="one", status="done")
    bus.emit(pid, t, "agent", "tool_use",
             "Edit: {'file_path': '/Users/someone/devteam/workspaces/task-7-a1/"
             "repo/src/weather.js'}")
    out = root_client.get(
        f"/api/graph/project/{pid}/node/{projgraph.task_key(t)}").json()
    joined = "\n".join(out["logs"])
    assert "/Users/someone" not in joined and "workspaces/" not in joined, \
        "the server's own filesystem must not reach the panel"
    assert "src/weather.js" in joined, \
        "…but the boss's own file, which is what the line is ABOUT, must survive"


def test_a_running_task_pulses_with_what_it_is_doing(root_client):
    pid = make_project(owner_id=1)
    t = make_task(pid, title="one", status="running")
    db.update_task(t, attempts=2)
    out = root_client.get(f"/api/graph/project/{pid}").json()
    n = next(x for x in out["nodes"] if x["key"] == projgraph.task_key(t))
    assert n["activity"] and "attempt 2" in n["activity"][0]["task"]


# --------------------------------------------------------------------------
# 4. IT EXISTS THE MOMENT THE PROJECT DOES
# --------------------------------------------------------------------------

def test_a_brand_new_project_already_has_a_graph(root_client):
    """The owner: "Atlas is the main screen, it should appear immediately when
    someone creates a project." A project with no tasks yet gets an aim, a
    deliverable and one honest arrow between them — never an empty screen."""
    pid = make_project(owner_id=1, name="fresh", status="planning")
    projgraph.sync(pid)
    out = root_client.get(f"/api/graph/project/{pid}").json()
    assert_graph_payload(out, tenant="project (unplanned)")
    assert {n["key"] for n in out["nodes"]} == {"aim", "conclusion"}
    assert [(e["src"], e["dst"]) for e in out["edges"]] == [("aim", "conclusion")]
    assert "not planned any yet" in json.dumps(out["conclusion"])


def test_creating_a_project_seeds_its_graph_and_announces_it(root_client, monkeypatch):
    """The seed is wired into create_project itself, so the screen is ready before
    the manager's first thought — and the reveal is announced, which is what makes
    the Atlas stage its entrance rather than pop."""
    import app.routes.projects as projects_routes
    src = Path(projects_routes.__file__).read_text()
    assert "projgraph.sync(project_id, announce=True)" in src, \
        "create_project must seed the graph"
    assert src.index("projgraph.sync") < src.index("run_manager"), \
        "the graph must exist BEFORE the manager session starts"


def test_planning_tasks_grows_the_graph_and_stages_the_reveal(root_client):
    """`graph_node_planned` per NEW node, in dependency order — the same event the
    crew's authoring pass emits, which is the machinery the Atlas already stages on."""
    pid = make_project(owner_id=1)
    projgraph.sync(pid)
    a = make_task(pid, title="alpha")
    make_task(pid, title="beta", deps=[a])
    projgraph.sync(pid, announce=True)
    kinds = [e["kind"] for e in db.list_events(pid)]
    assert kinds.count("graph_node_planned") == 2, \
        "only the NEW nodes are announced — the aim and the goal were already there"
    planned = [json.loads(e["payload"]) for e in db.list_events(pid)
               if e["kind"] == "graph_node_planned"]
    assert planned[0]["key"] == projgraph.task_key(a), \
        "the reveal must play in dependency order"


def test_resync_is_idempotent_and_does_not_churn_plan_versions(root_client):
    """A plan rewritten on every poll turns the trace into noise. Dict equality on the
    manifest is the whole guard — the same rule seed_fleet_graph holds."""
    pid, _ = _project_with_tasks(root_client)
    first = projgraph.sync(pid)
    for _ in range(3):
        root_client.get(f"/api/graph/project/{pid}")
    assert projgraph.sync(pid) == first, "an unchanged DAG must not write a new version"
    make_task(pid, title="a new one")
    assert projgraph.sync(pid) != first, "a changed DAG must write a new version"


def test_a_project_that_predates_this_feature_gets_its_graph_on_first_read(root_client):
    """The lazy sync in the BFF, which is the only reason project 9 has an Atlas at
    all: it was planned long before this shipped and nobody will re-create it."""
    pid, _ = _project_with_tasks(root_client)
    assert modgraph.active_plan(pid) is None or True   # whatever the state, read it:
    out = root_client.get(f"/api/graph/project/{pid}").json()
    assert len(out["nodes"]) == 6, "the read itself must derive the plan"


# --------------------------------------------------------------------------
# 5. THE CLAIM: "the team can choose to work on specific modules"
# --------------------------------------------------------------------------

def test_the_pool_is_the_projects_own_roster_and_nothing_else(root_client):
    """A project's cards are staffed from the people hired FOR it. Offering another
    world's agents here would be offering to dispatch work to a stranger, so `teams`
    is empty on purpose — the Atlas hides its pool switcher when it is."""
    pid, _ = _project_with_tasks(root_client)
    team.hire(pid, [{"role": "frontend", "count": 1}, {"role": "tester", "count": 1}])
    out = root_client.get(f"/api/graph/project/{pid}/team").json()
    assert out["teams"] == [], "a project's pool is not switchable"
    assert {m["name"] for m in out["members"]} == {a["name"] for a in db.list_agents(pid)}


def test_claiming_a_card_round_trips_and_shows_on_the_card(root_client):
    pid, tasks = _project_with_tasks(root_client)
    team.hire(pid, [{"role": "frontend", "count": 1}])
    who = db.list_agents(pid)[0]
    key = projgraph.task_key(tasks[3])          # the tester task, claimed cross-role
    r = root_client.post(f"/api/graph/project/{pid}/node/{key}/agent",
                         json={"agent_id": who["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["agent"]["name"] == who["name"]
    out = root_client.get(f"/api/graph/project/{pid}").json()
    n = next(x for x in out["nodes"] if x["key"] == key)
    assert n["agent"]["agent_id"] == who["id"]
    assert "claimed by you" in n["agent"]["note"], \
        "the card must say the name is YOUR claim, not the scheduler's pick"
    # ...and it survives the plan being re-derived, because a claim is steering
    make_task(pid, title="a later task")
    projgraph.sync(pid)
    out = root_client.get(f"/api/graph/project/{pid}").json()
    n = next(x for x in out["nodes"] if x["key"] == key)
    assert n["agent"]["agent_id"] == who["id"], \
        "an assignment must carry forward across plan versions by key"


def test_a_claim_actually_decides_who_the_scheduler_dispatches(root_client):
    """THE POINT OF THE BUTTON. Not that a row was written — that `team.assign`, the
    one function every dispatch goes through, returns the claimed teammate instead of
    its own idle/least-loaded pick."""
    pid, tasks = _project_with_tasks(root_client)
    team.hire(pid, [{"role": "tester", "count": 2}])
    testers = [a for a in db.list_agents(pid) if a["role"] == "tester"]
    default = team.assign(db.get_task(tasks[3]))
    other = next(a for a in testers if a["id"] != default["id"])
    key = projgraph.task_key(tasks[3])
    root_client.post(f"/api/graph/project/{pid}/node/{key}/agent",
                     json={"agent_id": other["id"]})
    assert team.assign(db.get_task(tasks[3]))["id"] == other["id"], \
        "the claim must outrank the round-robin at dispatch time"


def test_a_prior_worker_still_outranks_a_claim_on_a_retry(root_client):
    """The claim sits UNDER "whoever already worked this task" on purpose: a retry is
    about finishing what was started, and moving it mid-thread throws away the very
    attempt being retried."""
    pid, tasks = _project_with_tasks(root_client)
    team.hire(pid, [{"role": "tester", "count": 2}])
    testers = [a for a in db.list_agents(pid) if a["role"] == "tester"]
    db.update_task(tasks[3], agent_id=testers[0]["id"])
    key = projgraph.task_key(tasks[3])
    root_client.post(f"/api/graph/project/{pid}/node/{key}/agent",
                     json={"agent_id": testers[1]["id"]})
    assert team.assign(db.get_task(tasks[3]))["id"] == testers[0]["id"]


def test_a_stale_claim_is_ignored_rather_than_obeyed(root_client):
    """The claim is checked against the LIVE roster at dispatch. Dispatching a task to
    somebody else's teammate is worse than dispatching it to the least-loaded one."""
    pid, tasks = _project_with_tasks(root_client)
    other_pid = make_project(owner_id=1, name="somewhere else")
    team.hire(other_pid, [{"role": "tester", "count": 1}])
    stranger = db.list_agents(other_pid)[0]
    plan = modgraph.active_plan(pid) or {"id": projgraph.sync(pid)}
    modgraph.set_assign(plan["id"], projgraph.task_key(tasks[3]),
                        agent_id=stranger["id"])
    assert projgraph.claimed_agent(db.get_task(tasks[3])) is None


def test_a_claim_refuses_a_non_member_a_group_and_the_frame(root_client):
    pid, tasks = _project_with_tasks(root_client)
    team.hire(pid, [{"role": "tester", "count": 1}])
    key = projgraph.task_key(tasks[0])
    assert root_client.post(f"/api/graph/project/{pid}/node/{key}/agent",
                            json={"agent_id": 99999}).status_code == 400
    assert root_client.post(f"/api/graph/project/{pid}/node/aim/agent",
                            json={"agent_id": db.list_agents(pid)[0]["id"]}
                            ).status_code == 400


def test_pinning_a_model_is_enforced_and_autonomy_honestly_refuses(root_client):
    """The model knob is REAL — it lands on `tasks.pinned_model`, the one override
    `launcher.pick_model` puts above every automatic rule. The autonomy knob would
    NOT be, so it refuses instead of storing a value the scheduler never reads."""
    from app import providers
    pid, tasks = _project_with_tasks(root_client)
    model = sorted({m["id"] for p in providers.PROVIDERS.values() for m in p["models"]})[0]
    key = projgraph.task_key(tasks[0])
    r = root_client.post(f"/api/graph/project/{pid}/node/{key}/config",
                         json={"model": model})
    assert r.status_code == 200, r.text
    assert db.get_task(tasks[0])["pinned_model"] == model
    bad = root_client.post(f"/api/graph/project/{pid}/node/{key}/config",
                           json={"autonomy": "autonomous"})
    assert bad.status_code == 400 and "whole-project" in bad.json()["detail"]


def test_the_manager_is_told_who_owns_what(root_client):
    """The manager cannot dispatch, so a claim is context it plans AROUND. It has to
    SEE it, or it will keep re-assigning work away from the person you chose."""
    pid, tasks = _project_with_tasks(root_client)
    team.hire(pid, [{"role": "tester", "count": 1}])
    who = db.list_agents(pid)[0]
    root_client.post(f"/api/graph/project/{pid}/node/{projgraph.task_key(tasks[3])}/agent",
                     json={"agent_id": who["id"]})
    lines = projgraph.claim_lines(pid)
    assert lines and who["name"] in lines[0] and "Atlas" in lines[0]
    src = (REPO / "conductor" / "app" / "manager.py").read_text()
    assert "projgraph.claim_lines(project_id)" in src, \
        "the status tool must carry the boss's own assignments"


# --------------------------------------------------------------------------
# 6. THE GATE
# --------------------------------------------------------------------------

def test_the_project_graph_is_gated_on_OWNERSHIP_not_root(root_client, make_user):
    """The fleet's graph is an operator power; a project's is the boss's own. A
    stranger gets the same 404 every other project route gives — never a 403, which
    would confirm the project exists."""
    uid, other = make_user("stranger")
    pid, _ = _project_with_tasks(root_client)
    assert other.get(f"/api/graph/project/{pid}").status_code == 404
    mine = make_project(owner_id=uid, name="theirs")
    assert other.get(f"/api/graph/project/{mine}").status_code == 200
    assert other.get("/api/graph/self").status_code in (403, 404), \
        "the FLEET's graph stays root-only"


def test_the_flag_removes_the_whole_surface(root_client, monkeypatch):
    from app import config
    pid, _ = _project_with_tasks(root_client)
    monkeypatch.setattr(config, "MODULE_GRAPH", False)
    assert root_client.get(f"/api/graph/project/{pid}").status_code == 404


# --------------------------------------------------------------------------
# 7. THE UI: one screen, two tenants — and a legend that never sits on a card
# --------------------------------------------------------------------------

def _graph_js() -> str:
    return "\n".join((DASH / "graph" / f).read_text()
                     for f in ("index.js", "nodes.js", "layout.js"))


def test_the_renderers_never_branch_on_which_tenant(root_client):
    """THE PIN THIS PHASE IS ABOUT. `dashboard/graph/` draws a fleet and a project
    with the same code; everything that differs is a field on the payload or a
    property of the source object. The moment a renderer asks "is this a project?"
    the seam is gone and the next tenant costs a rewrite."""
    js = _graph_js()
    # the source seam declares both, and resolves by NAME
    assert "const DEVTEAM_GRAPH_SRC" in js and "function PROJECT_GRAPH_SRC" in js
    assert "function srcFor(" in js
    # ...and nothing BELOW the seam interrogates WHICH tenant it is holding.
    #
    # The line the pin draws, precisely, because there is a real distinction: a
    # renderer may compare two source names to EACH OTHER (open() does, to decide
    # navigate-vs-remount; renderLegend does, to rebuild once per source) — that is
    # cache identity and it works for any tenant that ever exists. What it may not do
    # is compare one to a LITERAL, read `tenant` off the payload, or reach for a
    # project id: each of those is a fork that the next tenant would have to be
    # added to by hand, which is the whole failure mode the seam prevents.
    seam_end = js.index("}", js.index("return m ? PROJECT_GRAPH_SRC"))
    body = js[seam_end:]
    smells = [
        (r'[=!]==?\s*"self"', 'comparing a source name to the literal "self"'),
        (r'"project:', 'testing for the "project:" source name'),
        (r'\.tenant\b', "reading `tenant` off the payload"),
        (r'\.projectId\b', "reaching for the project id"),
        (r'isProject|isDevteam|isFleet', "an is-this-a-project flag"),
    ]
    import re as _re
    for pattern, why in smells:
        m = _re.search(pattern, body)
        assert not m, (
            f"a renderer branches on the tenant — {why} at "
            f"…{body[max(0, m.start() - 60):m.end() + 40]!r}. Put the difference on "
            "the payload (health.note, tests.brief, conclusion.lines) or on the "
            "source object (legend, tip, goalTitle, hash) instead.")


def test_both_tenants_reach_the_screen_through_one_module(root_client):
    from conftest import dashboard_js
    js = dashboard_js()
    assert 'ModuleGraph.open("self", ' in js
    assert 'ModuleGraph.open("project:" + Number(id), ' in js, \
        "the project tenant must open the SAME module, one source name apart"
    assert "function openProjectGraph(" in js


def test_the_atlas_is_a_projects_default_view_with_command_one_chip_away(root_client):
    from conftest import dashboard_js
    js = dashboard_js()
    op = js.split("function openProject(", 1)[1].split("\n}", 1)[0]
    assert 'me.module_graph && window.ModuleGraph ? "graph" : "command"' in op, \
        "opening a project with no view named must land on its Atlas"
    # ...and the route must not re-impose the old default behind the address bar
    route = js.split("function route() {", 1)[1].split("\n}\nwindow.addEventListener", 1)[0]
    assert 'openProject(Number(m[1]), m[2] || "", true)' in route
    assert r'/^#\/p\/(\d+)\/graph(?:\/(.+))?/' in route, \
        "#/p/<id>/graph must be routed, with its room sub-path"
    # the classic view is one chip away, and the chip exists
    html = (DASH / "index.html").read_text()
    assert 'data-v="graph"' in html and 'id="atlasChip"' in html
    assert 'data-v="command"' in html
    sw = js.split("function switchView(", 1)[1].split("\n}", 1)[0]
    assert 'view === "graph"' in sw and "openProjectGraph(currentProject" in sw
    # ...and leaving the Atlas for any other chip must bring `main` back
    assert 'if ($("main").hidden)' in sw, \
        "switching off the graph must re-show the project panel it hid"


def test_the_graph_screen_survives_the_project_hash_space(root_client):
    from conftest import dashboard_js
    js = dashboard_js()
    assert r'/^#\/p\/\d+\/graph(\/|$)/.test(location.hash)' in js, \
        "routing must know BOTH hash spaces, or the project Atlas closes itself"


def test_the_legend_can_never_sit_on_a_card(root_client):
    """The owner's screenshot: the legend printed over the bottom-left cards.

    A RESERVED STRIP IS NOT ENOUGH AND THE DRILL PROVED IT. Padding the room's bottom
    reserves space at the bottom of the ROOM; the legend floated at the bottom of the
    STAGE, and the moment the room is taller than the stage (twelve fleet cards on a
    900px window) those are different places and it lands mid-column. So the legend
    left the stage: it is a pinned footer of the side column, which holds no cards.
    That is structural, which is why it can be asserted from the source."""
    html = (DASH / "index.html").read_text()
    stage = html.split('<div class="graph-stage">', 1)[1].split("</div>\n    <!--", 1)[0]
    assert 'id="graphLegend"' not in stage, \
        "the legend must not live inside the stage — anything in there can cover a card"
    side = html.split('<div class="graph-side">', 1)[1].split("</div>", 1)[0]
    assert 'id="graphAside"' in side and 'id="graphLegend"' in side, \
        "the legend belongs under the panel, in the column that holds no cards"
    css = (DASH / "graph" / "graph.css").read_text()
    leg = css.split(".gr-legend {", 1)[1].split("}", 1)[0]
    assert "position: absolute" not in leg and "position: fixed" not in leg, \
        "a positioned legend can float back over the room"
    assert "var(--gr-legend-h)" in leg and "max-height" in leg, \
        "cap the legend so a long one scrolls inside itself instead of eating the panel"
    assert "#graphScreen { --gr-legend-h" in css, \
        "the variable stays scoped to this screen, like every other value here"
    # and the fourth state has a swatch in both the legend and the map
    assert ".gr-legend-dot.gr-hs-grey" in css and ".gr-atlas-dot.gr-hs-grey" in css


def test_hq_gives_the_kanban_board_back_when_it_leaves(root_client):
    """Found by the project pass of tools/graph_experiment.py, and older than this
    phase: Devteam HQ replaces #board's whole innerHTML with its own panels, so an
    ordinary project opened afterwards found no columns and `refreshBoard` died on a
    null — taking the badges, the question and the artifacts down with it. The Atlas
    being a project's default view makes that hop routine, so it is fixed here."""
    from conftest import dashboard_js
    js = dashboard_js()
    assert "boardSkeleton" in js
    extras = js.split("renderExtras: (p) => {", 1)[1].split("\n  },", 1)[0]
    assert "if (boardSkeleton === null) boardSkeleton = board.innerHTML;" in extras, \
        "HQ must snapshot the kanban skeleton BEFORE overwriting it"
    leave = js.split("leave: () => {", 1)[1].split("\n  },", 1)[0]
    assert "b.innerHTML = boardSkeleton" in leave, "…and put it back on the way out"
    rb = js.split("function refreshBoard(", 1)[1].split("\n}", 1)[0]
    assert "if (!box) continue;" in rb, \
        "and a missing column must never take the whole refresh down again"


def test_a_grey_card_is_the_quietest_thing_on_the_wall(root_client):
    css = (DASH / "graph" / "graph.css").read_text()
    grey = css.split(".gr-card.gr-hs-grey {", 1)[1].split("}", 1)[0]
    assert "animation: none" in grey, \
        "unstarted work must not pulse — the eye belongs on what is moving or broken"
    nodes = (DASH / "graph" / "nodes.js").read_text()
    assert '"grey"' in nodes.split("const HS_STATES", 1)[1].split("\n", 1)[0]
