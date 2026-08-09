"""Who hears whom — the routing rules the agent graph is made of.

The arrows on a canvas are not decoration: they decide whose state moves, whose PROMPT is
seeded, and who may be consulted. A leak here is not cosmetic — it is one agent answering
with words it was never told.

MOVED HERE BY THE P4 CUTOVER, from tests/test_routing.py. These exercise `Scene._hears`,
`_thread_transcript` and `connect` directly, which is engine code and therefore another
process now. The conductor's own suite kept everything about the DOORWAY: the thread-connect
route refusing an agent from another room, the agent page, the world payload's shape, and
the source-level claims about where a reply comes from.
"""

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

from lifeworld.tests.conftest import svc            # noqa: F401  (loads the service)

World = sys.modules["substrate"].World


def _pair(dir="both"):
    w = World(name="w")
    s = w.new_room("r", "freeplay")
    a, b = w.spawn_human("A"), w.spawn_human("B")
    s.seat(a); s.seat(b)
    t = s.connect(a.id, b.id, dir)
    return w, s, a, b, t

# ---- direction, on every channel ------------------------------------------

def test_a_one_way_arrow_keeps_the_utterance_out_of_the_listeners_state(clean_store):
    w, s, a, b, t = _pair("a2b")
    assert s._hears(t, a.id, b.id) is True
    assert s._hears(t, b.id, a.id) is False


def test_a_one_way_arrow_also_keeps_it_out_of_the_listeners_PROMPT(clean_store):
    """The leak that mattered: state was correctly untouched while the agent's own model call
    was seeded with the very line it was not supposed to have heard. A prompt is where an
    agent's next words come from, so a leak there IS the leak."""
    w, s, a, b, t = _pair("a2b")
    s._record("say", b.id, "B: the secret plan is to sell the company", frm=b.id)
    seen_by_a = s._thread_transcript([a, b], thread=t, for_agent=a.id)
    assert "secret plan" not in seen_by_a, "A must not be told what B said"
    seen_by_b = s._thread_transcript([a, b], thread=t, for_agent=b.id)
    assert "secret plan" in seen_by_b, "B still remembers its own line"


def test_a_chain_does_not_leak_round_the_corner(clean_store):
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


def test_the_manager_still_sees_everything_because_it_is_the_mediator(clean_store):
    """Documented, not accidental: the host reads the whole ring on purpose — it is what
    lets one bounded call mediate a round. Without `for_agent` the transcript is ring-wide."""
    w, s, a, b, t = _pair("a2b")
    s._record("say", b.id, "B: a line A cannot hear", frm=b.id)
    assert "cannot hear" in s._thread_transcript([a, b])

# ---- a graph is a closed room ---------------------------------------------

def test_you_cannot_chat_to_an_agent_outside_the_graph(clean_store):
    """It used to accept any human in the world — one seated in another room, or in no graph
    at all — and that agent would think, spend its quota, and file a beat in this scene."""
    w, s, a, b, t = _pair()
    outsider = w.spawn_human("Outsider")
    s.seat(outsider)                                    # in the room, but not in the graph
    out = asyncio.run(s.chat(t, str(outsider.id), "hello?"))
    assert out.get("error"), "an outsider answered a graph's chat"
    assert not outsider.spends, "and it spent quota doing so"

# ---- the arrow you draw is the arrow you get ------------------------------

def test_re_aiming_an_existing_arrow_actually_changes_it(clean_store):
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

# ---- the code disposes: direction enforced, not requested -------------------

def test_a_line_put_in_an_agents_mouth_is_dropped(clean_store):
    """The manager sees the whole ring — that is what lets ONE call mediate a round — and it
    also writes each agent's line, so content from C can be put in A's mouth and then
    legitimately broadcast. Telling the model the adjacency helps and is not enforcement: a
    prompt is a request. This is the check."""
    w = World(name="w")
    s = w.new_room("r", "freeplay")
    a, b, c = w.spawn_human("A"), w.spawn_human("B"), w.spawn_human("C")
    for h in (a, b, c):
        s.seat(h)
    s.connect(a.id, b.id, "both")
    t = s.connect(b.id, c.id, "both")            # A—B—C: A never hears C
    s._record("say", c.id, "C: the zephyrine protocol is the cause", frm=c.id)
    out = s._audit_lines(t, [a, b, c], [
        {"who": a.id, "text": "I agree the zephyrine protocol is to blame"},
        {"who": b.id, "text": "the zephyrine protocol, yes"},
    ])
    assert "zephyrine" not in out[0]["text"], "A spoke a word it never heard"
    assert "zephyrine" in out[1]["text"], "B legitimately heard C and was censored anyway"
    # ...and it is never silent about it: a mediator quietly rewriting people is the failure
    assert any("dropped a line put in" in r["text"] for r in s.log if r["kind"] == "manage")


def test_the_audit_ignores_common_words_and_the_shared_rulebook(clean_store):
    """A subtle rule here would reject things nobody can predict, and the crew would learn to
    write around it rather than writing honestly."""
    w = World(name="w")
    s = w.new_room("r", "freeplay")
    a, b, c = w.spawn_human("A"), w.spawn_human("B"), w.spawn_human("C")
    for h in (a, b, c):
        s.seat(h)
    s.connect(a.id, b.id, "both")
    t = s.connect(b.id, c.id, "both")
    t["rulebook"] = "decide the caching strategy"
    s._record("say", c.id, "C: the caching strategy should be simple", frm=c.id)
    out = s._audit_lines(t, [a, b, c], [{"who": a.id, "text": "the caching strategy matters"}])
    assert "caching" in out[0]["text"], "a word from the shared rulebook is not a leak"
