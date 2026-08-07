"""Source-level pins for the module-graph canvas (dashboard/graph/).

The graph screen is the FOURTH canvas this dashboard has grown, and the first
three each taught a lesson the hard way. These tests pin those lessons by name so
they cannot silently un-learn themselves:

  1. one engine — world.js is imported, never copied;
  2. no global singleton wars — the graph never touches the Studio canvas's global;
  3. key listeners on the screen element, never the document;
  4. its own inspector/action hosts — no borrowed overlays from other screens;
  5. reduced motion decided locally and honoured BEFORE any animation is armed.

Classic-script assertions go through conftest.dashboard_js() (index.html's load
order); the ES-module files are read directly — they are not in that bundle.
"""

from pathlib import Path

from conftest import dashboard_js

REPO = Path(__file__).resolve().parents[1]
DASH = REPO / "dashboard"


def _read(rel: str) -> str:
    return (DASH / rel).read_text()


# --------------------------------------------------------------------------
# wiring: the screen exists, is routed, and loads its module + css
# --------------------------------------------------------------------------

def test_graph_screen_is_wired_into_the_shell():
    html = _read("index.html")
    assert 'id="graphScreen"' in html
    assert '<script type="module" src="graph/index.js"></script>' in html
    assert '<link rel="stylesheet" href="graph/graph.css">' in html
    # its own hosts, inside the section — the module never borrows another screen's
    assert 'id="graphCanvas"' in html and 'id="graphAside"' in html
    # the HQ chip that opens it
    assert 'id="graphLink"' in html


def test_graph_screen_is_a_screen_and_a_route():
    js = dashboard_js()
    # a SCREENS entry, so every other screen's hideScreens() puts it away
    screens = js.split("const SCREENS = [", 1)[1].split("];", 1)[0]
    assert '"#graphScreen"' in screens
    # a route of its own, resolved before the HQ route
    assert 'location.hash.startsWith("#/graph")' in js
    assert "openModuleGraph(true)" in js
    route_body = js.split("function route() {", 1)[1].split("\n}", 1)[0]
    assert route_body.index('startsWith("#/graph")') < route_body.index("#\\/hq"), \
        "#/graph must be routed before the #/hq branch"
    # routing away destroys the instance — timers and listeners must not outlive it
    assert "window.ModuleGraph.close()" in js


def test_open_module_graph_gates_on_the_flag_and_falls_back_to_hq():
    js = dashboard_js()
    body = js.split("function openModuleGraph(", 1)[1].split("\nfunction ", 1)[0]
    assert "me.module_graph" in body, "the flag gate is gone"
    assert "openDevteamHQ" in body, "flag off must land on Devteam HQ, not a blank screen"


def test_the_hq_chip_only_shows_in_devteam_mode_with_the_flag():
    js = dashboard_js()
    assert "projSrc && me && me.module_graph" in js, \
        "the #graphLink chip must hide for ordinary projects and when the flag is off"


# --------------------------------------------------------------------------
# the ws bridge: classic scripts own the socket; the module hears DOM events
# --------------------------------------------------------------------------

def test_connect_ws_bridges_graph_and_repair_events_to_the_module():
    js = (DASH / "js" / "projects.js").read_text()
    assert 'new CustomEvent("graph-event"' in js, "the bridge is gone"
    assert 'startsWith("graph_")' in js
    body = js.split("function connectWs()", 1)[1].split("\n}", 1)[0]
    assert "graph-event" in body, "the bridge must live inside connectWs's onmessage"
    # ...and the module listens for exactly that event
    mod = _read("graph/index.js")
    assert 'document.addEventListener("graph-event"' in mod


# --------------------------------------------------------------------------
# one engine: world.js imported, never copied
# --------------------------------------------------------------------------

def test_graph_imports_the_canvas2_engine_rather_than_copying_it():
    mod = _read("graph/index.js")
    assert 'from "../canvas2/world.js"' in mod and "createWorld" in mod
    assert "function createWorld" not in mod, "a copied engine drifts; import it"
    assert 'from "../canvas2/render.js"' in mod, \
        "wireNode/setWireEnds/setPos/speechBubble come from render.js"
    nodes = _read("graph/nodes.js")
    assert 'from "../canvas2/world.js"' in nodes and "svgEl" in nodes
    assert "function svgEl" not in nodes


# --------------------------------------------------------------------------
# the four canvas blockers, each pinned by name
# --------------------------------------------------------------------------

def test_blocker_1_no_global_singleton_touching_other_canvases():
    """Mounting the graph must not be able to kill the Studio's canvas: the graph
    keeps its one instance module-private and never writes the global the Studio
    canvas reads."""
    for rel in ("graph/index.js", "graph/nodes.js", "graph/layout.js"):
        assert "LWCanvas2" not in _read(rel), f"{rel} touches the Studio canvas's global"


def test_blocker_2_key_listeners_live_on_the_screen_not_the_document():
    mod = _read("graph/index.js")
    assert 'document.addEventListener("keydown"' not in mod, \
        "a document-level key listener fires under every other screen"
    assert '.screen.addEventListener("keydown"' in mod
    html = _read("index.html")
    assert 'id="graphScreen" hidden tabindex="-1"' in html, \
        "without tabindex the section cannot receive key events at all"


def test_blocker_3_own_inspector_and_action_hosts():
    for rel in ("graph/index.js", "graph/nodes.js", "graph/layout.js"):
        src = _read(rel)
        assert "#lwOverlay" not in src and "lwOverlay" not in src, f"{rel} borrows the Studio overlay"
        assert "#sdPortal" not in src and "sdPortal" not in src, f"{rel} borrows the delete portal"
    assert "graphAside" in _read("graph/index.js"), "the inspector renders in this screen's own aside"


def test_blocker_4_reduced_motion_is_decided_locally():
    mod = _read("graph/index.js")
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in mod, \
        "the module must ask matchMedia itself — lib.js's const is not on window"
    css = _read("graph/graph.css")
    assert "prefers-reduced-motion" in css, "the glow animation must also stand down in CSS"


# --------------------------------------------------------------------------
# the staged reveal honours reduced motion — checked BEFORE the stepper runs
# --------------------------------------------------------------------------

def test_reveal_checks_reduced_motion_before_stepping():
    mod = _read("graph/index.js")
    body = mod.split("function reveal(", 1)[1]
    assert "reduceMotion()" in body
    assert body.index("reduceMotion()") < body.index("setTimeout(step"), \
        "the reduced-motion early-out must come before the stepper is armed"


def test_the_poll_pauses_while_something_is_selected():
    """agent.js's lesson: repainting under the cursor clears the selection and
    blanks the inspector mid-sentence."""
    mod = _read("graph/index.js")
    assert "inst.sel.size || inst.inspectKey" in mod


# --------------------------------------------------------------------------
# nodes.js: svgEl only — titles can never become markup
# --------------------------------------------------------------------------

def test_node_cards_are_svg_only_and_titles_go_through_textcontent():
    nodes = _read("graph/nodes.js")
    assert "innerHTML" not in nodes, "innerHTML with node data is an injection waiting to happen"
    assert "text: trim(n.title" in nodes, "the title must pass through svgEl's text attribute"
    # svgEl's `text` attribute IS textContent — that contract is what the pin above leans on
    world = _read("canvas2/world.js")
    fn = world.split("export function svgEl", 1)[1].split("export function", 1)[0]
    assert "textContent" in fn


# --------------------------------------------------------------------------
# the source seam: renderers stay source-agnostic
# --------------------------------------------------------------------------

def test_the_graph_src_seam_exists_with_the_five_verbs():
    mod = _read("graph/index.js")
    assert "DEVTEAM_GRAPH_SRC" in mod
    seam = mod.split("DEVTEAM_GRAPH_SRC = {", 1)[1].split("};", 1)[0]
    for verb in ("fetch:", "verify:", "saveLayout:", "setConfig:", "inspect:"):
        assert verb in seam, f"the seam lost its {verb.rstrip(':')} verb"
    assert "/api/graph/self" in seam
