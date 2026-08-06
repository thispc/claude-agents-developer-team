"""Who hears what.

The arrows on the canvas are the whole point of the graph: they decide the flow of the
conversation. A direction that is drawn but not enforced is worse than no direction at all,
because the operator designs around a guarantee that does not exist.

Every test here was written from a real finding in an audit of the routing paths.
"""

import asyncio
from pathlib import Path

import pytest

from app.lifeworld.world import World


def _pair(dir="both"):
    w = World(name="w")
    s = w.new_room("r", "freeplay")
    a, b = w.spawn_human("A"), w.spawn_human("B")
    s.seat(a); s.seat(b)
    t = s.connect(a.id, b.id, dir)
    return w, s, a, b, t


# ---- direction, on every channel ------------------------------------------

def test_a_one_way_arrow_keeps_the_utterance_out_of_the_listeners_state(fresh_db):
    w, s, a, b, t = _pair("a2b")
    assert s._hears(t, a.id, b.id) is True
    assert s._hears(t, b.id, a.id) is False


def test_a_one_way_arrow_also_keeps_it_out_of_the_listeners_PROMPT(fresh_db):
    """The leak that mattered: state was correctly untouched while the agent's own model call
    was seeded with the very line it was not supposed to have heard. A prompt is where an
    agent's next words come from, so a leak there IS the leak."""
    w, s, a, b, t = _pair("a2b")
    s._record("say", b.id, "B: the secret plan is to sell the company", frm=b.id)
    seen_by_a = s._thread_transcript([a, b], thread=t, for_agent=a.id)
    assert "secret plan" not in seen_by_a, "A must not be told what B said"
    seen_by_b = s._thread_transcript([a, b], thread=t, for_agent=b.id)
    assert "secret plan" in seen_by_b, "B still remembers its own line"


def test_a_chain_does_not_leak_round_the_corner(fresh_db):
    """A—B—C with no A–C edge: C's line must not reach A, even though both are in the ring.
    This is the repo's own default topology, so a leak here is a leak everywhere."""
    w = World(name="w")
    s = w.new_room("r", "freeplay")
    a, b, c = w.spawn_human("A"), w.spawn_human("B"), w.spawn_human("C")
    for h in (a, b, c):
        s.seat(h)
    s.connect(a.id, b.id, "both")
    t = s.connect(b.id, c.id, "both")
    assert s._hears(t, c.id, a.id) is False
    s._record("say", c.id, "C: something only B should hear", frm=c.id)
    assert "only B should hear" not in s._thread_transcript([a, b, c], thread=t, for_agent=a.id)


def test_the_manager_still_sees_everything_because_it_is_the_mediator(fresh_db):
    """Documented, not accidental: the host reads the whole ring on purpose — it is what
    lets one bounded call mediate a round. Without `for_agent` the transcript is ring-wide."""
    w, s, a, b, t = _pair("a2b")
    s._record("say", b.id, "B: a line A cannot hear", frm=b.id)
    assert "cannot hear" in s._thread_transcript([a, b])


def test_the_host_is_told_who_may_reference_whom(fresh_db):
    """It composes each agent's line, so without the adjacency it can write B's content into
    A's mouth and launder direction through the mediator. A prompt is not an enforcement
    boundary — enforcing it would cost one call per agent — but stating it is honest."""
    from pathlib import Path
    from app.lifeworld import world as wmod
    src = Path(wmod.__file__).read_text()
    assert "HEARS (agent id ->" in src
    assert "can_hear" in src and "not an enforcement boundary" in src


# ---- a graph is a closed room ---------------------------------------------

def test_you_cannot_chat_to_an_agent_outside_the_graph(fresh_db):
    """It used to accept any human in the world — one seated in another room, or in no graph
    at all — and that agent would think, spend its quota, and file a beat in this scene."""
    w, s, a, b, t = _pair()
    outsider = w.spawn_human("Outsider")
    s.seat(outsider)                                    # in the room, but not in the graph
    out = asyncio.run(s.chat(t, str(outsider.id), "hello?"))
    assert out.get("error"), "an outsider answered a graph's chat"
    assert not outsider.spends, "and it spent quota doing so"


def test_you_can_only_thread_agents_who_are_in_the_room(root_client, fresh_db):
    """An id from another room became a full member — speaking, hearing, spending — while
    absent from the room's own agent list. A participant nobody could see."""
    from app.lifeworld import store
    w = store.create(1, "w")
    s1 = w.new_room("here", "freeplay")
    s2 = w.new_room("elsewhere", "freeplay")
    a = w.spawn_human("A"); b = w.spawn_human("B"); far = w.spawn_human("Far")
    s1.seat(a); s1.seat(b); s2.seat(far)
    store.save(w)
    ok = root_client.post(f"/api/lw/{w.id}/room/{s1.id}/thread/connect",
                          json={"a": a.id, "b": b.id})
    assert ok.status_code == 200
    bad = root_client.post(f"/api/lw/{w.id}/room/{s1.id}/thread/connect",
                           json={"a": a.id, "b": far.id})
    assert bad.status_code == 400, "an agent from another room joined the graph"
    same = root_client.post(f"/api/lw/{w.id}/room/{s1.id}/thread/connect",
                            json={"a": a.id, "b": a.id})
    assert same.status_code == 400, "an agent was threaded to itself"


# ---- the arrow you draw is the arrow you get ------------------------------

def test_re_aiming_an_existing_arrow_actually_changes_it(fresh_db):
    """`edge_eq` ignores the direction slot, so "already connected" was read as "nothing to
    do" — and the one-way toggle silently did nothing on any arrow that already existed.
    A large part of why direction never looked like it worked."""
    w, s, a, b, t = _pair("both")
    assert t["edges"] == [[a.id, b.id, "both"]]
    s.connect(a.id, b.id, "a2b")
    assert t["edges"] == [[a.id, b.id, "a2b"]], "the toggle did nothing"
    s.connect(b.id, a.id, "a2b")
    assert t["edges"] == [[b.id, a.id, "a2b"]], "flipping it did nothing"
    assert s._hears(t, b.id, a.id) is True and s._hears(t, a.id, b.id) is False


# ---- talking to one agent, mid-task ---------------------------------------

def test_a_question_gets_an_answer_not_a_stage_direction(fresh_db):
    """An appraisal returns a Packet — a state delta whose one-line action text is a beat in
    a scene, not an answer to a person. Routing a question through it is why talking to an
    agent read like stage directions. Asking is its own act and gets its own prompt."""
    from pathlib import Path
    from app.lifeworld import scene as smod, world as wmod
    src = Path(smod.__file__).read_text()
    reply = src.split("async def _agent_reply", 1)[1].split("\n    async def", 1)[0]
    assert "self.world.agent_reply(" in reply, "live replies must come from the agent's model"
    assert 'kind="ask"' in reply, "and it must still PERCEIVE the question — that moves state"
    wsrc = Path(wmod.__file__).read_text()
    fn = wsrc.split("async def agent_reply", 1)[1].split("\n    async def", 1)[0]
    assert "no stage directions" in fn
    assert "recalled" in fn, "a familiar question should be answered from experience"


def test_the_world_is_locked_across_a_load_and_save(fresh_db):
    """A World is deserialized fresh per request and written back whole, so two overlapping
    cycles are a lost update — the crew's sprint and the operator's chat each erasing the
    other's work depending on who finished last."""
    from pathlib import Path
    from app.lifeworld import store
    assert store.lock_for(7) is store.lock_for(7)
    for mod in ("conductor/app/repair.py", "conductor/app/repair_routes.py"):
        src = Path(__file__).resolve().parents[1].joinpath(mod).read_text()
        assert "store.lock_for(" in src, f"{mod} still races on the world blob"


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


def test_the_canvas_gets_activity_with_the_room(fresh_db):
    """One kv get per repaint, so the canvas can ask without anybody thinking about cost."""
    from app import agents
    from app.lifeworld.world import World
    w = World(id=4, name="w")
    s = w.new_room("r", "freeplay")
    h = w.spawn_human("A")
    s.seat(h)
    agents.note(agents.key_for("lw", 4, h.id), "thinking", "weighing it up")
    view = s.view()
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
    costs money: an agent asleep on its cap looks exactly like one idle for a good reason."""
    from app import agents
    from app.lifeworld import store
    w = store.create(1, "w")
    h = w.spawn_human("A")
    store.save(w)
    agents.note(agents.key_for("lw", w.id, h.id), "building", "the k8s reaper")
    d = root_client.get(f"/api/lw/{w.id}/human/{h.id}").json()
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
