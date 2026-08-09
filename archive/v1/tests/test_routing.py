"""Who hears what.

The arrows on the canvas are the whole point of the graph: they decide the flow of the
conversation. A direction that is drawn but not enforced is worse than no direction at all,
because the operator designs around a guarantee that does not exist.

Every test here was written from a real finding in an audit of the routing paths.
"""

import asyncio
from pathlib import Path

import pytest

# The engine's source, for the handful of claims that are ABOUT the source: where a live
# reply comes from, and that the host is told the adjacency. Read as a file rather than
# imported, because nothing outside services/lifeworld/ may import inside it — and a
# claim about a file is honest to check against the file.
SUBSTRATE = Path(__file__).resolve().parents[1] / "services" / "lifeworld" / "substrate"





def test_the_host_is_told_who_may_reference_whom(fresh_db):
    """It composes each agent's line, so without the adjacency it can write B's content into
    A's mouth and launder direction through the mediator. A prompt is not an enforcement
    boundary — enforcing it would cost one call per agent — but stating it is honest."""
    src = (SUBSTRATE / "world.py").read_text()
    assert "HEARS (agent id ->" in src
    assert "can_hear" in src and "not an enforcement boundary" in src


def test_you_can_only_thread_agents_who_are_in_the_room(root_client, fresh_db):
    """An id from another room became a full member — speaking, hearing, spending — while
    absent from the room's own agent list. A participant nobody could see.

    Built over HTTP since P4, because the world is the lifeworld service's. Which makes
    this a better test than it was: the refusal now has to survive the proxy, the caller
    stamp and the service's own validation, which is the whole path a browser takes.
    """
    wid = root_client.post("/api/lw", json={"name": "w"}).json()["world"]["id"]
    r1 = root_client.post(f"/api/lw/{wid}/room", json={"name": "here"}).json()["room"]["id"]
    r2 = root_client.post(f"/api/lw/{wid}/room",
                          json={"name": "elsewhere"}).json()["room"]["id"]
    ids = {}
    for name in ("A", "B", "Far"):
        ids[name] = root_client.post(f"/api/lw/{wid}/human",
                                     json={"name": name}).json()["human"]["id"]
    for name in ("A", "B"):
        root_client.post(f"/api/lw/{wid}/room/{r1}/seat", params={"human_id": ids[name]})
    root_client.post(f"/api/lw/{wid}/room/{r2}/seat", params={"human_id": ids["Far"]})

    ok = root_client.post(f"/api/lw/{wid}/room/{r1}/thread/connect",
                          json={"a": ids["A"], "b": ids["B"]})
    assert ok.status_code == 200, ok.text
    bad = root_client.post(f"/api/lw/{wid}/room/{r1}/thread/connect",
                           json={"a": ids["A"], "b": ids["Far"]})
    assert bad.status_code == 400, "an agent from another room joined the graph"
    same = root_client.post(f"/api/lw/{wid}/room/{r1}/thread/connect",
                            json={"a": ids["A"], "b": ids["A"]})
    assert same.status_code == 400, "an agent was threaded to itself"

# ---- talking to one agent, mid-task ---------------------------------------

def test_a_question_gets_an_answer_not_a_stage_direction(fresh_db):
    """An appraisal returns a Packet — a state delta whose one-line action text is a beat in
    a scene, not an answer to a person. Routing a question through it is why talking to an
    agent read like stage directions. Asking is its own act and gets its own prompt."""
    src = (SUBSTRATE / "scene.py").read_text()
    reply = src.split("async def _agent_reply", 1)[1].split("\n    async def", 1)[0]
    assert "self.world.agent_reply(" in reply, "live replies must come from the agent's model"
    assert 'kind="ask"' in reply, "and it must still PERCEIVE the question — that moves state"
    wsrc = (SUBSTRATE / "world.py").read_text()
    fn = wsrc.split("async def agent_reply", 1)[1].split("\n    async def", 1)[0]
    assert "no stage directions" in fn
    assert "recalled" in fn, "a familiar question should be answered from experience"


def test_the_world_is_locked_across_a_load_and_save(fresh_db):
    """A World is deserialized fresh per request and written back whole, so two overlapping
    cycles are a lost update — the crew's sprint and the operator's chat each erasing the
    other's work depending on who finished last.

    P4 MOVED THE LOCK, and that is the whole point of the phase. The conductor has no
    World to hold a lock over any more, so every read-modify-write happens inside the
    lifeworld service, under ITS per-world lock — which is why `repair` and
    `repair_routes` now ask for whole behaviours (deliberate, consult, chat) instead of
    for the pieces. A lock you can only take on one side of a wire is not a lock, so
    what is asserted here is that NEITHER side straddles it: the conductor holds none,
    and every mutating handler in the service takes one.
    """
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    for mod in ("conductor/app/repair.py", "conductor/app/repair_routes.py"):
        src = repo.joinpath(mod).read_text()
        assert "store.lock_for(" not in src, \
            f"{mod} still holds a world lock the conductor cannot honour across the wire"
        assert "store.load(" not in src and "store.save(" not in src, \
            f"{mod} still does a load/save round trip on a blob it does not own"

    svc = repo / "services" / "lifeworld"
    store_src = (svc / "store.py").read_text()
    assert "def lock_for(" in store_src, "the service must own the per-world lock"
    # Every save is inside a lock. Read structurally rather than grepped, because the
    # failure this prevents is silent and is exactly one forgotten `async with` away —
    # a counted heuristic would pass the day someone adds a handler that saves twice.
    import ast

    def unlocked_saves(path):
        def walk(node, locked, fn):
            out = []
            for n in ast.iter_child_nodes(node):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out += walk(n, locked, n.name)
                    continue
                here = locked or (isinstance(n, ast.AsyncWith) and any(
                    "lock_for" in ast.unparse(i.context_expr) for i in n.items))
                if (isinstance(n, ast.Call) and not locked
                        and "store.save" in ast.unparse(n)):
                    out.append(fn)
                out += walk(n, here, fn)
            return out
        return sorted({f for f in walk(ast.parse(path.read_text()), False, "") if f})

    for f in ("app.py", "crew.py"):
        offenders = unlocked_saves(svc / f)
        assert not offenders, (f"services/lifeworld/{f}: {offenders} write a world blob "
                               "outside the per-world lock — that is the lost update")


def test_a_habit_says_what_it_matches_on_not_object_Object():
    """`Rule.match` is a dict of fields. `String(dict)` is "[object Object]", which is what
    every compiled-habit row has read since the panel was written."""
    from conftest import dashboard_js
    js = dashboard_js()
    assert "function lwHabitWhen" in js
    assert "escapeHtml(String(hb.when))" not in js


def test_the_drawer_is_gone_entirely():
    """Two windows for one agent, a close button that scrolled out of reach, and the decision
    graph squeezed into a 340px keyhole. Everything it rendered lives on the agent's page."""
    from conftest import dashboard_js
    js = dashboard_js()
    assert "function openPersonDrawer" not in js, "the drawer is back"
    assert "lwPeekOpen" not in js, "and so is the popup that replaced it"
    # #lwDetail itself stays — artifacts still use it. Only the person branch is retired.
    assert "openAgentPage" in js, "and something has to open the agent instead"

# ---- the agent register ---------------------------------------------------

def test_the_register_answers_what_any_agent_is_doing(fresh_db):
    """The question had no single answer: it was implied by a usage timestamp here, a task
    status there, a log line somewhere else — and those implications disagree, because a
    worker whose process died still reads as running."""
    from app import agents
    k = agents.key_for("lw", 2, 30)
    agents.note(k, "building", "Fix the k8s reaper", name="Correctness", where="self-repair")
    row = agents.get(k)
    assert row["state"] == "building" and row["busy"] is True
    assert row["what"] == "Fix the k8s reaper"
    assert row["means"], "a state has to mean something a person can read"
    assert [r["key"] for r in agents.roster()] == [k]
    assert agents.summary()["busy"] == 1


def test_a_claim_left_by_a_dead_process_expires(fresh_db):
    """An entry is a CLAIM that work is in flight. Claims made by processes that then die
    must not glow forever, and nothing has to remember to clean up after a crash — which is
    the only kind of cleanup that works."""
    import time as _t
    from app import agents, db as _db
    k = agents.key_for("lw", 1, 1)
    agents.note(k, "thinking", "answering you")
    rows = _db.kv_get(agents.KEY)
    rows[k]["ts"] = _t.time() - 99999
    _db.kv_set(agents.KEY, rows)
    assert agents.get(k)["state"] == "idle" and agents.get(k)["stale"] is True


def test_work_that_raises_does_not_leave_an_agent_thinking(fresh_db):
    from app import agents
    k = agents.key_for("lw", 1, 2)
    with pytest.raises(ValueError):
        with agents.working(k, "thinking", "a call that blows up"):
            raise ValueError("boom")
    assert agents.get(k)["busy"] is False


def test_the_register_is_root_only(client, fresh_db):
    from conftest import _signup
    _signup(client, "nosy")
    client.post("/api/login", json={"username": "nosy", "password": "hunter2pw"})
    assert client.get("/api/logs/agents").status_code == 403


def test_the_canvas_gets_activity_with_the_room(root_client, fresh_db):
    """One register read per repaint, so the canvas can ask without anybody thinking about
    cost — and, since P4, one register for the whole platform even though the agents live
    in another process. The conductor writes the board; the substrate reads it back through
    `GET /internal/agents/{key}` while composing the room view.
    """
    from app import agents
    wid = root_client.post("/api/lw", json={"name": "w"}).json()["world"]["id"]
    rid = root_client.post(f"/api/lw/{wid}/room", json={"name": "r"}).json()["room"]["id"]
    hid = root_client.post(f"/api/lw/{wid}/human", json={"name": "A"}).json()["human"]["id"]
    root_client.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    agents.note(agents.key_for("lw", wid, hid), "thinking", "weighing it up")
    view = root_client.get(f"/api/lw/{wid}/room/{rid}").json()["room"]
    assert view["agents"][0]["activity"]["busy"] is True
    assert view["agents"][0]["activity"]["what"] == "weighing it up"


def test_a_bubble_says_what_an_agent_is_doing_not_what_it_said():
    """Six bubbles of transcript on top of a graph is unreadable as a picture and redundant
    with the panel, where the words can be scrolled and quoted — and it left no way to answer
    the question a canvas is actually good at: which of these is working?"""
    src = (Path(__file__).resolve().parents[1] / "dashboard/canvas2/index.js").read_text()
    assert "function showActivity" in src and "function showSpeech" not in src
    fn = src.split("function showActivity", 1)[1].split("\n}", 1)[0]
    assert "act.busy" in fn, "a bubble must be reserved for an agent that is working"
    assert "room.log" not in fn, "it must not be reading the transcript any more"


def test_the_agent_page_answers_what_it_is_doing_first(root_client, fresh_db):
    """The first question anyone opens an agent to ask, and the one with a wrong answer that
    costs money: an agent asleep on its cap looks exactly like one idle for a good reason.

    This is now an end-to-end test of the register ACROSS TWO PROCESSES: the conductor
    owns the board, the substrate reads it back through `GET /internal/agents/{key}` while
    rendering the panel, and the answer comes home through the proxy. One board, whoever
    asks — which is the whole reason the register did not move with the agents.
    """
    from app import agents
    wid = root_client.post("/api/lw", json={"name": "w"}).json()["world"]["id"]
    hid = root_client.post(f"/api/lw/{wid}/human",
                           json={"name": "A"}).json()["human"]["id"]
    agents.note(agents.key_for("lw", wid, hid), "building", "the k8s reaper")
    d = root_client.get(f"/api/lw/{wid}/human/{hid}").json()
    assert d["activity"]["busy"] is True and d["activity"]["what"] == "the k8s reaper"
    assert "usage" in d and "withheld" in d


def test_the_agent_page_exists_and_is_addressable():
    from conftest import dashboard_js
    js = dashboard_js()
    assert "async function openAgentPage" in js
    assert "#/agent/" in js, "it needs an address, or you cannot link to it"
    # ONE page, no tab strip: a tab strip is a filing cabinet, and this screen is for
    # exploring how an agent got where it is.
    assert "AG_TABS" not in js, "the tabs came back"
    assert "function agInspectHtml" in js and "function agKnowledgeHtml" in js
    for f in ("all", "pivots", "learned", "bad"):
        assert f'id: "{f}"' in js.split("const AG_FILTERS = [", 1)[1].split("];", 1)[0]
    # the graph gets the page — the entire reason it exists
    css = (Path(__file__).resolve().parents[1] / "dashboard/style.css").read_text()
    assert "#agentPage .lw-dagwrap { max-height: none;" in css


def test_one_helper_hides_the_screens():
    """Six functions each carried their own copy of the hide-list, which is how a new screen
    ends up visible underneath another one — and it did, twice, in one edit."""
    from conftest import dashboard_js
    js = dashboard_js()
    assert "function hideScreens" in js and '"#agentPage"' in js

# ---- the shape of the product ---------------------------------------------

def test_the_devteam_is_not_listed_among_your_teams(root_client, fresh_db):
    """It is not a team you assembled; it is the one that works on this platform, and it has
    its own door. Mixing it into the same list invites someone to reorganise the crew that is
    mid-sprint."""
    from app import repair
    mine = root_client.post("/api/lw", json={"name": "my team"}).json()["world"]["id"]
    repair.ensure_team()
    dev = (repair.team() or {}).get("world_id")
    listed = [w["id"] for w in root_client.get("/api/lw").json()["worlds"]]
    assert mine in listed and dev not in listed
    assert root_client.get("/api/lw").json()["devteam_world"] == dev
    # ...but it is reachable when explicitly asked for, which is what its own door does
    both = [w["id"] for w in root_client.get("/api/lw?include_devteam=1").json()["worlds"]]
    assert dev in both


def test_a_project_can_name_the_team_that_staffs_it(fresh_db):
    """Not a new requirement — with nothing chosen the manager hires per task exactly as
    before. It is the option to arrange the people BEFORE the work."""
    from conftest import make_project
    from app import db
    pid = make_project(owner_id=1, name="thing")
    db.set_team(pid, 4, 9)
    p = db.get_project(pid)
    assert p["team_world"] == 4 and p["team_room"] == 9


def test_migrations_are_appended_never_inserted():
    """The tuple is indexed by PRAGMA user_version, so a statement inserted anywhere but the
    end shifts every one after it: a database at version N skips the new statements and
    re-runs old ones."""
    src = Path(__file__).resolve().parents[1].joinpath("conductor/app/db.py").read_text()
    block = src.split("migrations = (", 1)[1].split("\n    )", 1)[0]
    assert "APPEND ONLY BELOW" in block
    tail = block.split("APPEND ONLY BELOW", 1)[1]
    assert "team_world" in tail, "the newest migration is not at the end"


def test_the_landing_doors_say_what_is_behind_them():
    """A landing page that describes its own features is a brochure. The numbers under each
    door are the difference between "arrange your agents" and "3 teams, one working"."""
    from conftest import dashboard_js
    js = dashboard_js()
    assert "async function paintHomeStats" in js
    for stat in ("#statProjects", "#statTeams", "#statDevteam"):
        assert stat in js
    html = (Path(__file__).resolve().parents[1] / "dashboard/index.html").read_text()
    for name in (">Projects<", ">Teams<", ">Devteam<"):
        assert name in html, f"the landing page has no {name} door"


def test_the_devteam_door_opens_the_team_not_the_console():
    """Hiding the crew from the Teams list was right, but it left the canvas unreachable: the
    door said Devteam and led to a console, and the actual team — the ring of six, the arrows,
    the manager — could not be looked at from anywhere. Seeing the arrangement is the point of
    the door; the sprint board is what the arrangement produced."""
    from conftest import dashboard_js
    js = dashboard_js()
    assert "async function openDevteam" in js
    assert '$("#modeImprove").addEventListener("click", () => openDevteam())' in js
    assert 'startsWith("#/devteam")' in js, "it needs an address of its own"
    # ...and HQ (the crew rendered as a project) is a button ON that canvas, with the
    # engine room one step further and a way back from the console to the team.
    assert "sdDevteamBar" in js and "Devteam HQ" in js
    assert "sdDevEngine" in js, "the engine room must stay reachable from the canvas"
    assert 'id="rpOpenTeam"' in js, "the console must lead back to the team"

# ---- a task that will die unfinished never gets a session -------------------

def test_programme_sized_tasks_are_rejected_at_the_door(fresh_db):
    """Ten of eighteen failures were sessions that ran out of turns. The planner is told to
    take a slice and still proposes programmes, so the code checks rather than hoping."""
    from app import repair
    assert repair.too_ambitious({"title": "Extract first domain router from routes.py",
                                 "brief": "move the projects endpoints out"})
    assert repair.too_ambitious({"title": "Tidy up",
                                 "brief": "delete a.py b.py c.py d.py e.py f.py"})
    assert not repair.too_ambitious({"title": "Fix the check-then-bind race in port allocation",
                                     "brief": "launcher.py binds after checking; hold the socket"})


def test_dropping_an_over_scoped_task_is_said_out_loud(fresh_db):
    """A silent filter would look like the crew planning LESS, rather than planning smaller."""
    src = Path(__file__).resolve().parents[1].joinpath("conductor/app/repair.py").read_text()
    assert "task_too_ambitious" in src
    assert "before they could burn a" in src
    assert "A silent filter would look" in src

# ---- the store starts full, not empty --------------------------------------

def test_the_knowledge_base_is_seeded_from_sprints_that_already_ran(fresh_db):
    """A knowledge base is useless the day you build it and useful a month later — which makes
    the first month the hard part. Thirty-odd sprints of outcomes were already recorded."""
    import asyncio
    from app import db, knowledge
    db.kv_set("repair:sprint:1", {"no": 1, "tasks": [
        {"title": "Extract the router from routes.py", "status": "failed",
         "error": "too big for one session — needs re-scoping into a smaller slice"},
        {"title": "Fix a port race", "status": "landed"},
        {"title": "Something still open", "status": "building"}]})
    n = asyncio.run(knowledge.backfill_from_sprints())
    assert n == 2, "an unfinished task has taught nothing and must not be imported"
    hits = asyncio.run(knowledge.recall("global", "extracting a router out of routes.py", k=1))
    assert hits and "too big" in hits[0]["says"]
    assert asyncio.run(knowledge.backfill_from_sprints()) == 0, "it must run once"


def test_the_team_picker_reads_rooms_from_the_right_level():
    """`rooms` sits at the TOP of the world payload, not under `world`. Reading the wrong
    level produced an empty list silently, so the picker offered no teams at all and the
    whole feature looked unimplemented — found only by opening the wizard for real."""
    from conftest import dashboard_js
    js = dashboard_js()
    fn = js.split("async function fillTeamPick", 1)[1].split("\n}", 1)[0]
    assert ".rooms || []" in fn and ".world?.rooms" not in fn


def test_a_world_payload_really_does_put_rooms_at_the_top(root_client, fresh_db):
    """Pinned against the server, so the client and the shape cannot drift apart again."""
    wid = root_client.post("/api/lw", json={"name": "w"}).json()["world"]["id"]
    root_client.post(f"/api/lw/{wid}/room", json={"name": "a team"})
    body = root_client.get(f"/api/lw/{wid}").json()
    assert "rooms" in body and body["rooms"], "rooms moved; fillTeamPick reads the top level"
    assert "rooms" not in (body.get("world") or {})
