"""The agent page: the decision tree on the screen, and who may see it.

The decision MODEL moved to services/lifeworld/tests/test_decisions.py with the engine
that owns it (P4). What is here is what the conductor still owns: the dashboard's drawing
of that tree — its layout, its edges, its arrowheads, its colours, its live default — and
the one route gate that matters, which is that a PRIVATE decision is withheld from
everyone but root. That gate lives in the conductor's stamp, not in the substrate.
"""

import asyncio
import json
from pathlib import Path

import pytest


def test_a_private_decision_is_not_shown_to_everyone(root_client, client, make_user, fresh_db):
    """An agent's own reasoning is exactly the kind of thing a scene may have made secret,
    and a detail panel must not be how it leaks.

    Driven entirely over HTTP since P4: the world, the agent and its decisions are the
    service's, so this reaches them the way the browser does. Which is the point — the
    root gate it checks is applied by the conductor's stamp on the way through.
    """
    from app import lifeworld_client as lwc
    from app import repair
    root = repair._root_user()
    wid = asyncio.run(lwc.create_world(root, "w"))
    seat = lwc.seat_crew(wid, [{"id": "correctness", "name": "Correctness", "brief": "b"}],
                         manager={"model": "", "budget": 2}, protocol={},
                         scene_name="table", current_room_id=0)
    hid = seat["agents"]["correctness"]
    lwc.crew_decision(wid, hid, saw="a public matter", understood="x",
                      chose="say something", because={})
    seen = root_client.get(f"/api/lw/{wid}/human/{hid}").json()
    assert len(seen["decisions"]) == 1, "root sees what the agent recorded"
    assert seen["withheld"] == 0
    assert "logs" in seen, "and the backend's own record of them"

    # ...and a stranger cannot see the world at all, let alone its reasoning
    _uid, other = make_user("nosey")
    assert other.get(f"/api/lw/{wid}/human/{hid}").status_code == 404


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


def test_an_agent_that_is_working_says_so():
    """You can still talk to a busy agent — but you should be able to see it is mid-task
    before you interrupt.

    The `usage()["busy"]` arithmetic behind this moved with the engine (its own suite
    covers the window and the sleep); what the CANVAS must do with the flag is here,
    because the canvas is the conductor's.
    """
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


def test_the_legend_dots_are_actually_different_colours():
    """The base rule is `.ag-legend i.ag-k`, so a plain `.ag-k.good` loses on specificity and
    the legend renders four identical hollow dots, explaining nothing."""
    css = (Path(__file__).resolve().parents[1] / "dashboard/style.css").read_text()
    for cls in ("good", "bad", "canon"):
        assert f".ag-legend i.ag-k.{cls}" in css, f"{cls} loses the specificity fight"


def test_the_page_links_a_node_to_what_it_taught():
    from conftest import dashboard_js
    js = dashboard_js()
    ins = js.split("function agInspectHtml", 1)[1].split("\n}", 1)[0]
    assert "from_decisions" in ins, "a node must find the belief it contributed to"
    assert "What it took from this" in ins
    # ...and back the other way: a belief jumps to the decision that produced it
    assert "data-agsig" in js and "why do you think that" in js
