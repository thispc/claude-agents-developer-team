"""An agent's decisions, the pivots among them, and what it learns to expect.

A memory of an EVENT is a sentence. A memory of a DECISION keeps its causes attached, so
"I chose X" is still useful six turns later. Everything here must be FREE — assembled from
the packet the appraisal already returned — or it breaks the one-spend invariant the whole
substrate rests on.
"""

import asyncio
from pathlib import Path

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


# ---- the DAG is laid out, not listed --------------------------------------

def test_the_tree_is_drawn_as_a_graph_with_edges():
    """A chronological list answers "what happened"; a DAG answers "what led to what", which
    is the only question a tree is for."""
    from conftest import dashboard_js
    js = dashboard_js()
    assert "function lwLayoutDag" in js and "lw-dedge" in js
    assert "<svg class=\"lw-dag\"" in js, "it has to actually draw a graph"
    assert "Math.max(...ps.map" in js, "depth must come from the deepest parent"
    # no library, no build step — the whole app loads nothing external
    assert "cdn." not in js and "import(" not in js


def test_the_layout_cannot_hang_on_corrupt_parentage():
    """Depth is computed iteratively rather than recursively, so a cycle from a corrupted
    world blob stops improving instead of blowing the stack."""
    from conftest import dashboard_js
    body = dashboard_js().split("function lwLayoutDag", 1)[1].split("\n}", 1)[0]
    assert "for (let pass = 0; pass <" in body, "the depth pass must be bounded"


def test_the_node_data_is_not_serialised_into_the_page():
    """A <script type="application/json"> block is RAW TEXT: escaping it corrupts the JSON
    and not escaping it is an injection. Neither is needed when the renderer and the click
    handler are ten lines apart."""
    from conftest import dashboard_js
    js = dashboard_js()
    tree = js.split("function lwTreeHtml", 1)[1].split("\nfunction ", 1)[0]
    assert "<script" not in tree, "the renderer must not serialise nodes into the page"
    assert "JSON.stringify" not in tree
    assert "let lwDagRows" in js, "the click handler reads them from memory instead"


# ---- gestures: double-click means "show me this one" ----------------------

def test_double_click_always_opens_the_thing_you_clicked():
    """It used to select the whole graph instead, which meant double-clicking an agent could
    never open that agent — the graph swallowed the gesture. A gesture that means two
    different things depending on invisible state is one people stop trusting."""
    src = (Path(__file__).resolve().parents[1] / "dashboard/canvas2/index.js").read_text()
    dbl = src.split("function fireDoubleClick", 1)[1].split("\n}", 1)[0]
    assert "openAgentPage" in dbl, "double-click must open the agent's own page"
    assert "graphSelect" not in src, "the old double-click-selects-graph path is back"


def test_a_graph_offers_its_actions_when_it_is_actually_selected():
    src = (Path(__file__).resolve().parents[1] / "dashboard/canvas2/index.js").read_text()
    assert "function selectedThread" in src and "function offerGraphActions" in src
    # covering the WHOLE graph, not merely touching it: a partial selection is usually
    # somebody halfway through a marquee
    sel = src.split("function selectedThread", 1)[1].split("\n}", 1)[0]
    assert "members.size !== inst.sel.size" in sel
    # and it is offered from both a completed marquee and a plain click
    assert src.count("offerGraphActions(inst)") >= 3


def test_selecting_one_node_tells_you_it_belongs_to_something():
    """"Marquee across it" is not a gesture anyone guesses, and a capability nobody can find
    is one that does not exist."""
    src = (Path(__file__).resolve().parents[1] / "dashboard/canvas2/index.js").read_text()
    assert "Select its graph" in src


# ---- the canvas has to make direction and activity legible -----------------

def _c2(name):
    return (Path(__file__).resolve().parents[1] / "dashboard/canvas2" / name).read_text()


def test_a_wire_stops_short_of_its_tokens_so_the_arrowhead_is_visible():
    """Wires ran centre to centre, which drew the arrowhead UNDERNEATH the circle: the
    direction was rendered and then hidden, which is why it was never readable."""
    src = _c2("render.js")
    assert "export function wireEnds" in src
    assert "WIRE_GAP = SIZES.AGENT_R" in src
    ends = src.split("export function wireEnds", 1)[1].split("\n}", 1)[0]
    assert "Math.hypot" in ends and "Math.min(WIRE_GAP" in ends, "short wires must not invert"


def test_two_way_is_two_heads_not_the_absence_of_one():
    """Head-versus-no-head is nearly invisible at any real zoom, and conversation flow
    depends on reading it correctly. The difference has to be a thing that is THERE."""
    src = _c2("render.js")
    wire = src.split("export function wireNode", 1)[1].split("\n}", 1)[0]
    assert 'dir === "a2b" || dir === "both"' in wire
    assert 'dir === "b2a" || dir === "both"' in wire


def test_an_agent_that_is_working_says_so(fresh_db):
    """You can still talk to a busy agent — but you should be able to see it is mid-task
    before you interrupt."""
    import time as _t
    from app.lifeworld.world import World
    w = World(name="w")
    h = w.spawn_human("Correctness")
    assert h.usage()["busy"] is False
    h.spends.append(_t.time())
    assert h.usage()["busy"] is True, "a call just now means it is thinking now"
    h.spends[-1] = _t.time() - 600
    assert h.usage()["busy"] is False, "and it stops glowing when the round ends"
    src = _c2("render.js")
    assert "lw2-busyring" in src and "lw2-busy" in src


def test_the_busy_ring_respects_reduced_motion():
    css = (Path(__file__).resolve().parents[1] / "dashboard/style.css").read_text()
    block = css.split(".lw2-busyring", 1)[1][:600]
    assert "prefers-reduced-motion: no-preference" in block, \
        "the animation must be opt-out, not opt-in"


def test_live_is_the_default_wherever_the_platform_can_actually_think():
    """Six agents producing free stance lines look identical to six agents with nothing to
    say, and that is exactly the report that came back. Deterministic stays — it is what
    makes the suite free and an unconfigured install usable — but it is the fallback."""
    from conftest import dashboard_js
    js = dashboard_js()
    assert "async function lwDefaultLive" in js
    fn = js.split("async function lwDefaultLive", 1)[1].split("\n}", 1)[0]
    assert '/api/health' in fn and 'h.auth' in fn
    assert "lwDefaultLive()" in js, "and it has to actually run at boot"


def test_a_reflex_does_not_write_a_debugger_string_into_the_tree(fresh_db):
    """A Tier-0 packet's `understood` is "say (i=0.30)" — the appraiser telling itself how
    intense the signal was, not something the agent decided. Recording it made a decision
    tree that read like a debugger."""
    from app.lifeworld.types import Signal
    from app.lifeworld.world import World
    w = World(name="w"); w.new_room("r", "freeplay")
    h = w.spawn_human("A")
    sig = Signal(kind="say", from_id=None, sense="hearing", intensity=0.9, stakes=0.8,
                 payload={"text": "the build failed with ImportError"}, domain="work.tech")
    asyncio.run(h.perceive(sig, w, free=True))
    n = h.decisions.nodes[-1]
    assert "i=0." not in n.understood and "i=0." not in n.chose
    assert "ImportError" in n.understood, "it should record what it reacted TO"


def test_the_legend_dots_are_actually_different_colours():
    """The base rule is `.ag-legend i.ag-k`, so a plain `.ag-k.good` loses on specificity and
    the legend renders four identical hollow dots, explaining nothing."""
    css = (Path(__file__).resolve().parents[1] / "dashboard/style.css").read_text()
    for cls in ("good", "bad", "canon"):
        assert f".ag-legend i.ag-k.{cls}" in css, f"{cls} loses the specificity fight"


# ---- the tree and the cache are connected --------------------------------

def test_a_belief_remembers_which_decisions_built_it(fresh_db):
    """A conclusion you cannot trace is a number to take on faith, and "why do you think
    that?" is the first question anyone asks a knowledge base."""
    log = DecisionLog()
    a = log.record(1, "build failed ImportError", "the venv symlink is missing", "symlink it")
    log.resolve(a.id, "good", says="an ImportError here means the venv symlink")
    b = log.record(2, "another ImportError", "same cause", "apply the fix", parents=[a.id])
    log.resolve(b.id, "good", says="an ImportError here means the venv symlink")
    assoc = log.assoc["error:ImportError"]
    assert assoc.from_decisions == [a.id, b.id]
    assert log.taught_by(a.id) is assoc
    assert log.taught_by(999) is None


def test_recall_returns_the_path_somebody_already_walked(fresh_db):
    """Meeting a familiar situation should not mean thinking it through from nothing; it
    should mean walking a path somebody already walked, with the outcomes attached."""
    log = DecisionLog()
    root = log.record(1, "ImportError in the build", "the venv symlink", "symlink it")
    log.resolve(root.id, "good", says="an ImportError here means the venv symlink")
    kid = log.record(2, "ImportError again", "same", "apply the fix", parents=[root.id])
    log.resolve(kid.id, "good", says="an ImportError here means the venv symlink")
    out = log.recall_path("error:ImportError")
    assert out["known"] is True and out["confidence"] == 1.0
    assert [n["chose"] for n in out["path"]] == ["symlink it", "apply the fix"]
    assert log.recall_path("error:NeverSeen")["known"] is False


def test_the_page_links_a_node_to_what_it_taught():
    from conftest import dashboard_js
    js = dashboard_js()
    ins = js.split("function agInspectHtml", 1)[1].split("\n}", 1)[0]
    assert "from_decisions" in ins, "a node must find the belief it contributed to"
    assert "What it took from this" in ins
    # ...and back the other way: a belief jumps to the decision that produced it
    assert "data-agsig" in js and "why do you think that" in js
