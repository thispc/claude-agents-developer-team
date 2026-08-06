"""Who hears what.

The arrows on the canvas are the whole point of the graph: they decide the flow of the
conversation. A direction that is drawn but not enforced is worse than no direction at all,
because the operator designs around a guarantee that does not exist.

Every test here was written from a real finding in an audit of the routing paths.
"""

import asyncio

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

def test_a_question_is_never_answered_by_a_reflex(fresh_db):
    """A habit matches on {kind, tone, from_trusted} and nothing else, so an agent that had
    listened to a few rounds held a reflex for exactly the shape a user's question arrived
    in — and replied with the raw appraisal string "say (i=0.30)"."""
    from pathlib import Path
    from app.lifeworld import scene as smod
    src = Path(smod.__file__).read_text()
    reply = src.split("async def _agent_reply", 1)[1].split("\n    async def", 1)[0]
    assert 'kind="ask"' in reply, "a question must not arrive shaped like overheard chatter"
    assert "packet.tier == 1" in reply, "and a reflex's internals must never reach a person"


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


def test_the_resume_pin_count_is_treated_as_the_number_it_is():
    """`resume.pins` is an int; the drawer did `Array.isArray(...) ? ... : []` and then
    rendered the empty list — so no pinned achievement has ever appeared."""
    from conftest import dashboard_js
    js = dashboard_js()
    assert "typeof resume.pins === \"number\"" in js
    assert "pins.map((p)" not in js
