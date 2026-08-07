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

def test_the_graph_src_seam_exists_with_the_six_verbs():
    mod = _read("graph/index.js")
    assert "DEVTEAM_GRAPH_SRC" in mod
    seam = mod.split("DEVTEAM_GRAPH_SRC = {", 1)[1].split("};", 1)[0]
    for verb in ("fetch:", "verify:", "saveLayout:", "setConfig:", "inspect:", "replan:"):
        assert verb in seam, f"the seam lost its {verb.rstrip(':')} verb"
    assert "/api/graph/self" in seam


# --------------------------------------------------------------------------
# the ONE panel: a single click opens everything, agent front and center
# --------------------------------------------------------------------------

def test_single_click_opens_the_one_full_panel():
    """The owner's verdict on the two-tier aside ("double click ui is very clunky
    and don't know what i need"): ONE panel, opened by a SINGLE click, always
    complete — the lighter card tier is gone by name."""
    mod = _read("graph/index.js")
    assert "function openPanel" in mod and "function renderPanel" in mod
    for gone in ("asideLight", "asideGroup", "openInspector", "renderInspector"):
        assert gone not in mod, f"the two-tier aside is back ({gone})"
    body = mod.split("function selectionChanged(", 1)[1].split("\nfunction ", 1)[0]
    assert "openPanel(i, key)" in body, "a single click must open the full panel"


def test_the_panel_puts_the_agent_front_and_center():
    """"i didnt see the assigned agent": the agent row renders directly under the
    panel's title — the specialist's name when assigned, and when unassigned an
    honest sentence plus the ONE action that fixes it (the manager replan)."""
    mod = _read("graph/index.js")
    assert "function agentRow" in mod
    rp = mod.split("function renderPanel(", 1)[1].split("\nfunction ", 1)[0]
    assert "${agentRow(" in rp, "the panel must render the agent row"
    assert "the specialist working this module" in mod
    assert "the manager staffs modules when it authors the plan" in mod, \
        "the unassigned fallback must say WHO assigns and WHEN"
    assert "Have the manager plan now" in mod
    assert "i.src.replan()" in mod, "the unassigned button must call the replan verb"


def test_double_click_keeps_exactly_one_meaning():
    """Drill, groups only. On a leaf a double-click opens the same single panel a
    click already gives — no inspector hiding behind a second gesture."""
    mod = _read("graph/index.js")
    up = mod.split("async function pointerUp(", 1)[1].split("\nfunction ", 1)[0]
    dbl = up.split("if (g.maybeDouble)", 1)[1].split("} else {", 1)[0]
    assert "drillTo(i, g.key)" in dbl, "double-click on a group is the microscope"
    assert "selectionChanged(i)" in dbl, \
        "a leaf double-click must land on the same panel as a click"


def test_the_model_select_rides_the_servers_own_option_list():
    """The config-400 lesson: a hardcoded option list drifts from what the server
    validates. The payload carries the valid ids; the select builds from them;
    and a rejected model is told what WOULD have worked."""
    mod = _read("graph/index.js")
    assert "d.models" in mod, "the select must prefer the payload's option list"
    py = (REPO / "conductor" / "app" / "routes" / "graph.py").read_text()
    assert "_known_models" in py
    assert '"models": sorted(_known_models())' in py, \
        "both graph payloads must carry the valid model ids"
    assert "valid: " in py, "the 400 detail must SAY the valid options"


def test_node_cards_chip_the_assigned_agent():
    """The chip on the card itself is the at-a-glance answer to "who works this";
    it prefers the specialist's NAME over a bare row id."""
    nodes = _read("graph/nodes.js")
    assert "if (n.agent)" in nodes, "the chip renders when the payload has agent"
    assert "agentInitial" in nodes
    assert "agent.name" in nodes, "the chip must prefer the specialist's name"


# --------------------------------------------------------------------------
# the hierarchical microscope: themes, drill state, motion, HUD
# --------------------------------------------------------------------------

def test_two_themes_scoped_to_graphscreen_with_persistence():
    """Blueprint (this screen's default HUD) and paper (the app's own look) are
    one data attribute apart; every colour resolves through --gr-* custom
    properties scoped to #graphScreen, so neither theme can leak app-wide."""
    css = _read("graph/graph.css")
    assert '#graphScreen[data-gtheme="blueprint"]' in css
    assert '#graphScreen[data-gtheme="paper"]' in css
    assert "--gr-" in css
    assert ":root" not in css, "graph theme variables must stay scoped to #graphScreen"
    mod = _read("graph/index.js")
    assert 'THEME_KEY = "gr:theme"' in mod
    assert "localStorage.setItem(THEME_KEY" in mod, "the chosen theme must persist"
    assert "dataset.gtheme" in mod
    assert '"blueprint"' in mod and '"paper"' in mod
    html = _read("index.html")
    assert 'id="graphTheme"' in html, "the theme toggle lives in the graph bar"


def test_breadcrumb_exists_and_climbs():
    html = _read("index.html")
    assert 'id="graphCrumb"' in html
    mod = _read("graph/index.js")
    assert "renderCrumb" in mod
    assert "gr-crumb-up" in mod, "the aim crumb is the way back up a level"


def test_drill_state_lives_in_the_hash():
    """#/graph is the architecture, #/graph/<group> is inside that group — the
    address bar, the back button and a pasted link all mean the same place."""
    mod = _read("graph/index.js")
    assert "function open(sourceName, sub)" in mod, "open() must accept the drill sub-path"
    assert '"#/graph/" + encodeURIComponent(key)' in mod, "drilling must write the hash"
    js = dashboard_js()
    assert 'ModuleGraph.open("self", ' in js, \
        "core.js must pass the hash's sub-path through to the module"


def test_motion_is_css_with_reduced_motion_guards():
    """Dash-flow shimmer, wire draw-in, hover lift and the selection pulse are
    all CSS (transform/opacity/dash only) — and every one of them stands down
    under prefers-reduced-motion."""
    css = _read("graph/graph.css")
    for needle in ("gr-dashflow", "gr-drawin", "gr-pulse", "stroke-dashoffset"):
        assert needle in css, f"the {needle} animation is gone"
    assert ":hover .gr-cardg" in css and "scale(1.03)" in css, "the hover lift is gone"
    reduce = css.split("prefers-reduced-motion", 1)[1]
    for guard in ("gr-flow", "gr-draw", "gr-sel", ":hover .gr-cardg", "gr-busy"):
        assert guard in reduce, f"{guard} does not stand down under reduced motion"


def test_the_raf_loop_lives_only_in_the_camera():
    """Efficiency pin: the ONE requestAnimationFrame loop is the camera flight
    (flyTo) and nothing else — at rest, no JS animation loop runs at all."""
    mod = _read("graph/index.js")
    before, after = mod.split("function flyTo", 1)
    assert "requestAnimationFrame" not in before, "an rAF loop outside the camera"
    fly_body = after.split("\nfunction ", 1)[0]
    assert "requestAnimationFrame" in fly_body
    assert "requestAnimationFrame" not in after.split("\nfunction ", 1)[1], \
        "an rAF loop outside the camera"
    for rel in ("graph/nodes.js", "graph/layout.js"):
        assert "requestAnimationFrame" not in _read(rel)


def test_minimap_present_with_click_to_jump():
    html = _read("index.html")
    assert 'id="graphMini"' in html
    mod = _read("graph/index.js")
    assert "renderMini" in mod and "miniViewport" in mod
    assert "onpointerdown" in mod.split("function renderMini", 1)[1].split("\nfunction ", 1)[0], \
        "the minimap must jump the camera on click"


def test_edge_tooltip_escapes_the_contract():
    """Contracts are planner-authored JSON riding into a floating div — every
    interpolated field goes through esc() or it is an injection."""
    html = _read("index.html")
    assert 'id="graphTip"' in html
    mod = _read("graph/index.js")
    body = mod.split("function showTip", 1)[1].split("\nfunction ", 1)[0]
    assert "esc(JSON.stringify(ed.contract))" in body
    assert "esc(ed.edge_type" in body and "esc(ed.contract_test)" in body
    assert "esc(s)" in body and "esc(d)" in body
