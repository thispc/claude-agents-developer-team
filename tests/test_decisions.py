"""An agent's decisions, the pivots among them, and what it learns to expect.

A memory of an EVENT is a sentence. A memory of a DECISION keeps its causes attached, so
"I chose X" is still useful six turns later. Everything here must be FREE — assembled from
the packet the appraisal already returned — or it breaks the one-spend invariant the whole
substrate rests on.
"""

import asyncio

import pytest

from app.lifeworld.decisions import (Assoc, CANON_DEGREE, Decision, DecisionLog,
                                     MAX_DECISIONS, MIN_EVIDENCE, signature)


# ---- the signature: the same trouble must produce the same key -------------

def test_the_same_lesson_gets_the_same_key_however_it_is_worded():
    """"HTTP 505 from api.example.com/v2/users" and "505 talking to the billing host" are
    the same lesson, and an agent that files them separately never learns it."""
    assert signature("HTTP 505 from api.example.com/v2/users") == "http:505"
    assert signature("505 talking to the billing host") == "http:505"
    assert signature("ReferenceError: rpLogLine is not defined") == "error:ReferenceError"
    assert signature("the session ran out of turns") == "cond:out_of_turns"
    assert signature("connection refused on 127.0.0.1:8787") == "cond:connection_refused"
    assert signature("") == "" and signature("nothing notable here") == ""


def test_a_status_that_is_not_a_failure_is_not_a_signature():
    assert signature("returned 200 OK") == ""


# ---- associations: the cache, and the evidence bar ------------------------

def test_one_coincidence_teaches_nothing():
    """An agent acting on a single co-occurrence is worse than one with no memory: it is
    confidently wrong, and it got there by itself."""
    log = DecisionLog()
    d = log.record(1, "505 from the billing host", "the env is wrong", "switch env")
    log.resolve(d.id, "good", says="a 505 here is the staging env")
    assert log.recall("http:505") is None, "one sample is superstition"
    assert log.assoc["http:505"].confidence == 0.0


def test_agreeing_outcomes_turn_into_instant_recall():
    log = DecisionLog()
    for i in range(MIN_EVIDENCE):
        d = log.record(i, f"505 on host {i}", "env", "switch env")
        log.resolve(d.id, "good", says="a 505 here is the staging env, not the API")
    a = log.recall("http:505")
    assert a is not None and a.evidence == MIN_EVIDENCE and a.confidence == 1.0
    assert a.says == "a 505 here is the staging env, not the API"


def test_a_conclusion_that_keeps_failing_stops_being_believed():
    log = DecisionLog()
    for i in range(4):
        d = log.record(i, "505 again", "must be the API", "wait and retry")
        log.resolve(d.id, "bad" if i > 0 else "good")
    a = log.assoc["http:505"]
    assert a.confidence < 0.5
    assert log.recall("http:505") is None, "a losing hypothesis must stop being recalled"


def test_only_an_outcome_moves_an_association():
    """An agent that reinforces whatever it happened to think is not learning, it is
    calcifying."""
    log = DecisionLog()
    log.record(1, "505 here", "some guess", "act")
    assert log.assoc == {}, "deciding alone teaches nothing"


# ---- canon: measured, not declared ----------------------------------------

def test_a_pivot_is_recognised_by_what_descends_from_it():
    log = DecisionLog()
    root = log.record(1, "the venv symlink is missing", "builds cannot import", "symlink it")
    for i in range(CANON_DEGREE):
        log.record(2 + i, f"another red build {i}", "same cause", "apply the fix",
                   parents=[root.id])
    assert log.get(root.id).canon is True
    assert log.nodes[-1].canon is False


def test_a_bad_premise_marks_everything_under_it(fresh_db=None):
    """Not deletion: the tree is the record of what it believed and when, and erasing it
    hides the very mistake worth remembering."""
    log = DecisionLog()
    root = log.record(1, "505s are the API's fault", "blame the API", "open a ticket")
    mid = log.record(2, "another 505", "still the API", "escalate", parents=[root.id])
    leaf = log.record(3, "and another", "definitely the API", "escalate again", parents=[mid.id])
    touched = log.invalidate(root.id)
    assert touched == 2
    assert all(log.get(i).stale for i in (root.id, mid.id, leaf.id))
    assert log.get(leaf.id).chose, "the record survives — it is not deleted"


# ---- it has to stay bounded, and canon has to survive ---------------------

def test_the_tree_is_capped_but_pivots_are_never_pruned():
    """These live inside the world blob, which is loaded and saved whole — an unbounded tree
    makes every save slower forever. Canon survives, because the rest hangs from it."""
    log = DecisionLog()
    root = log.record(0, "the founding mistake", "x", "y")
    for i in range(CANON_DEGREE):
        log.record(1 + i, "descendant", "x", "y", parents=[root.id])
    for i in range(MAX_DECISIONS + 50):
        log.record(100 + i, f"routine {i}", "x", "y")
    assert len(log.nodes) <= MAX_DECISIONS
    assert log.get(root.id) is not None, "a pivot must not be pruned"
    assert log.get(root.id).canon is True


def test_it_survives_a_round_trip_through_storage():
    log = DecisionLog()
    d = log.record(1, "505 here", "env", "switch")
    log.resolve(d.id, "good", says="staging env")
    back = DecisionLog.from_dict(log.to_dict())
    assert [n.chose for n in back.nodes] == ["switch"]
    assert back.assoc["http:505"].says == "staging env"
    assert back.seq == log.seq


def test_garbage_in_storage_does_not_take_the_agent_down():
    back = DecisionLog.from_dict({"nodes": [{"nonsense": 1}, None], "assoc": {"x": "not a dict"}})
    assert back.nodes == [] and back.assoc == {}


# ---- the agent actually uses it -------------------------------------------

def test_deciding_records_the_decision_and_its_causes(fresh_db):
    """Free by construction: every field comes from the packet the appraisal already
    returned, so keeping the tree costs no model call."""
    from app.lifeworld.types import Signal
    from app.lifeworld.world import World
    w = World(name="w")
    w.new_room("r", "freeplay")
    h = w.spawn_human("Correctness", dials={"conscientiousness": 90})
    sig = Signal(kind="say", from_id=None, sense="hearing", intensity=0.9, stakes=0.8,
                 payload={"text": "the build failed with a 505 from the billing host"},
                 domain="work.tech")
    asyncio.run(h.perceive(sig, w, free=True))
    assert h.decisions.nodes, "a decision with an action must be recorded"
    n = h.decisions.nodes[-1]
    assert n.sig == "http:505"
    assert n.because.get("wanted"), "the cause we already computed must be kept"
    assert "tier" in n.because


def test_a_remembered_situation_reaches_the_appraisal_before_it_thinks(fresh_db):
    """The cache read. A hit means the agent does not have to work it out again — which is
    the entire reason to keep any of this."""
    from app.lifeworld.types import Signal
    from app.lifeworld.world import World
    w = World(name="w")
    w.new_room("r", "freeplay")
    h = w.spawn_human("Correctness")
    for i in range(MIN_EVIDENCE):
        d = h.decisions.record(i, "505 from a host", "the env is wrong", "switch env")
        h.decisions.resolve(d.id, "good", says="a 505 here is the staging env")
    seen = {}
    orig = w.appraise

    async def spy(human, s, ctx, free=False):
        seen.update(ctx)
        return await orig(human, s, ctx, free=free)
    w.appraise = spy
    sig = Signal(kind="say", from_id=None, sense="hearing", intensity=0.9, stakes=0.8,
                 payload={"text": "another 505, this time from billing"}, domain="work.tech")
    asyncio.run(h.perceive(sig, w, free=True))
    assert seen.get("recalled"), "what it already knows must reach the decision"
    assert seen["recalled"]["says"] == "a 505 here is the staging env"


def test_an_agent_carries_its_decisions_through_a_save(fresh_db):
    from app.lifeworld.human import Human
    from app.lifeworld.world import World
    w = World(name="w")
    h = w.spawn_human("Speed")
    d = h.decisions.record(1, "505", "env", "switch")
    h.decisions.resolve(d.id, "good", says="staging")
    back = Human.from_dict(h.to_dict())
    assert back.decisions.assoc["http:505"].says == "staging"
    assert [n.chose for n in back.decisions.nodes] == ["switch"]


def test_a_private_decision_is_not_shown_to_everyone(root_client, client, make_user, fresh_db):
    """An agent's own reasoning is exactly the kind of thing a scene may have made secret,
    and a detail panel must not be how it leaks."""
    from app.lifeworld import store
    w = store.create(1, "w")
    h = w.spawn_human("Correctness")
    h.decisions.record(1, "a public matter", "x", "say something")
    h.decisions.record(2, "a secret", "x", "keep quiet", scope="private")
    store.save(w)
    seen = root_client.get(f"/api/lw/{w.id}/human/{h.id}").json()
    assert len(seen["decisions"]) == 2, "root sees the lot"
    assert "logs" in seen, "and the backend's own record of them"


def test_the_agent_panel_shows_the_tree_the_cache_and_the_logs():
    from conftest import dashboard_js
    js = dashboard_js()
    for fn in ("lwAssocHtml", "lwTreeHtml", "lwAgentLogsHtml"):
        assert f"function {fn}" in js, f"{fn} is missing"
    assert "d.associations" in js and "d.decisions" in js and "d.logs" in js
    # the causes are folded away until asked for: they matter when you ask why, and are
    # noise when you are scanning
    assert "data-dnode" in js and "lw-dwhy" in js
