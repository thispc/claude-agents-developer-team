"""Source-level pins for the Atlas (dashboard/graph/) — rooms-and-doors module
navigation, the canvas's replacement.

The free-camera canvas was rejected by the owner after live use: pan + zoom +
screen-fixed portal docks made a small graph HARDER to navigate. The Atlas is
the correction, and these tests pin its load-bearing decisions by name:

  1. NO CAMERA — no pan/zoom/drag/pointer-capture anywhere in graph/*.js; one
     room on screen at a time, a CSS grid that fits the viewport;
  2. door chips derive from the payload's EDGES alone, never group adjacency —
     wiring that cannot lie;
  3. chambers (groups) and capillaries (leaves) are visually distinct kinds;
  4. keyboard is first-class: keys live on the screen element, M is the map;
  5. the lessons the canvases taught stay learned: no global singleton wars,
     own aside hosts, reduced motion decided locally and honoured first.

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
    assert 'id="graphRoom"' in html and 'id="graphAside"' in html
    assert 'id="graphWires"' in html, "the wire underlay host is gone"
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


def test_drill_state_lives_in_the_hash():
    """#/graph is the top room, #/graph/<group> is inside that group — the
    address bar, the back button and a pasted link all mean the same room."""
    mod = _read("graph/index.js")
    assert "function open(sourceName, sub)" in mod, "open() must accept the room sub-path"
    assert '"#/graph/" + encodeURIComponent(key)' in mod, "travel must write the hash"
    js = dashboard_js()
    assert 'ModuleGraph.open("self", ' in js, \
        "core.js must pass the hash's sub-path through to the module"


# --------------------------------------------------------------------------
# THE ATLAS RULE: no camera. Ever.
# --------------------------------------------------------------------------

def test_no_pan_zoom_drag_or_pointer_capture_anywhere():
    """The owner's verdict on the canvas stands: free navigation made it worse.
    The Atlas has NO camera — so no pointer capture, no wheel handler, no drag
    gesture, no world transform may exist in any graph module."""
    for rel in ("graph/index.js", "graph/nodes.js", "graph/layout.js"):
        src = _read(rel)
        for banned in ("setPointerCapture", "pointerdown", "pointermove",
                       "pointerup", "wheel", "zoomAt", "panBy", "createWorld",
                       "getView", "setView"):
            assert banned not in src, f"{rel} grew a camera again ({banned})"


def test_the_atlas_imports_no_canvas_engine():
    """world.js/render.js are the canvas engine — the Atlas is DOM + one SVG
    underlay and must not import them (or anything from canvas2/)."""
    for rel in ("graph/index.js", "graph/nodes.js", "graph/layout.js"):
        assert "canvas2" not in _read(rel), f"{rel} imports the canvas engine"


def test_no_position_persistence_and_no_layout_pixels():
    """Positions were the camera's residue. The room is a CSS grid: layout.js
    computes COLUMNS (topo order), never pixel coordinates, and nothing saves
    dragged positions any more."""
    mod = _read("graph/index.js")
    assert "saveLayout" not in mod, "the seam grew its position-persistence verb back"
    lay = _read("graph/layout.js")
    assert "export function columns" in lay
    assert "colW" not in lay and "rowH" not in lay, "layout.js does pixels again"
    body = lay.split("export function columns", 1)[1]
    assert "indeg" in body, "columns must come from a real topological pass"


def test_one_room_on_screen_is_a_css_grid():
    css = _read("graph/graph.css")
    room = css.split(".gr-room {", 1)[1].split("}", 1)[0]
    assert "display: grid" in room
    assert "--gr-cols" in room, "the column count must ride a custom property"
    mod = _read("graph/index.js")
    assert 'setProperty("--gr-cols"' in mod


# --------------------------------------------------------------------------
# wiring that cannot lie: in-room wires from the DOM, door chips from edges
# --------------------------------------------------------------------------

def test_wires_draw_only_in_room_edges_from_dom_anchors():
    mod = _read("graph/index.js")
    paint = mod.split("function paint(", 1)[1].split("\nfunction ", 1)[0]
    assert "roomKeys.has(s) && i.roomKeys.has(d)" in paint, \
        "the SVG underlay may draw in-room edges ONLY"
    dw = mod.split("function drawWires(", 1)[1].split("\nfunction ", 1)[0]
    assert "getBoundingClientRect" in dw, \
        "wire anchors must be READ from the DOM after layout, not simulated"


def test_door_chips_are_built_from_payload_edges_not_group_adjacency():
    """The chip builder walks i.data.edges and resolves the FOREIGN endpoint to
    its owning room + leaf. It must never consult derived group-to-group
    adjacency — the wall's truth is the child edges themselves."""
    mod = _read("graph/index.js")
    dc = mod.split("function doorChips(", 1)[1].split("\nfunction ", 1)[0]
    assert "i.data.edges" in dc.replace("(i.data && i.data.edges)", "i.data.edges"), \
        "door chips must consume the payload's edges"
    assert "parent_key" in dc, "the foreign endpoint must resolve to its owning room"
    ad = mod.split("function attachDoors(", 1)[1].split("\nfunction ", 1)[0]
    assert "textContent" in ad, "chip labels are authored text — textContent only"
    assert "data-door" in ad and "data-target" in ad, \
        "a chip must carry where it travels and what to flash"


def test_an_empty_room_is_refused_before_the_travel():
    """P6 made the rooms LIVE — an empty worker pool is the normal state of a box
    with nothing building. Walking in only to be bounced out costs a wasted click
    AND a duplicated history entry (the bounce replaces the entry it just pushed,
    leaving two identical ones, so the next browser-back appears to do nothing —
    which the Atlas drill's back-nav hammer catches). So the refusal happens
    before the hash is written, and paint()'s bounce stays as the safety net for a
    room that empties while you are standing in it."""
    mod = _read("graph/index.js")
    dt = mod.split("function drillTo(", 1)[1].split("\nfunction ", 1)[0]
    assert "roomNodes(i.data, String(key)).length" in dt, \
        "drillTo must check the room is inhabited before travelling"
    assert dt.index("roomNodes") < dt.index("location.hash"), \
        "the check must come BEFORE the hash write, or the entry is already minted"
    assert "W.toast" in dt, "a refused travel must say why"
    paint = mod.split("\nfunction paint(", 1)[1].split("\nfunction ", 1)[0]
    assert "history.replaceState" in paint, "the safety net must stay"


def test_travel_flashes_the_target_card():
    mod = _read("graph/index.js")
    tr = mod.split("function travel(", 1)[1].split("\nfunction ", 1)[0]
    assert "flashKey" in tr
    fl = mod.split("function flashNow(", 1)[1].split("\nfunction ", 1)[0]
    assert "gr-flash" in fl
    css = _read("graph/graph.css")
    assert "gr-flashglow" in css


# --------------------------------------------------------------------------
# two card kinds, visually unmistakable
# --------------------------------------------------------------------------

def test_chamber_and_capillary_are_distinct_classes():
    nodes = _read("graph/nodes.js")
    assert '"gr-chamber"' in nodes and '"gr-capillary"' in nodes
    assert '"data-kind"' in nodes, "activation branches on the card kind"
    assert "Enter ▸" in nodes, "a chamber must carry its big Enter affordance"
    css = _read("graph/graph.css")
    chamber = css.split(".gr-chamber {", 1)[1].split("}", 1)[0]
    assert "box-shadow" in chamber, "chambers imply depth — the layered doorway look"
    cap = css.split(".gr-capillary {", 1)[1].split("}", 1)[0]
    assert "border-left" in cap, "capillaries carry the distinct accent border"


def test_capillary_puts_the_agent_front_and_center():
    nodes = _read("graph/nodes.js")
    ab = nodes.split("function agentBlock(", 1)[1].split("\nfunction ", 1)[0]
    assert "gr-cagent-avatar" in ab and "gr-cagent-name" in ab
    assert "Unassigned" in ab, "an unstaffed capillary says so honestly"
    assert "mastery" in ab and "★" in ab, "earned mastery shows on the card"


def test_cards_are_dom_with_textcontent_only():
    """Titles, agent names and activity are authored text — createElement +
    textContent, never innerHTML."""
    nodes = _read("graph/nodes.js")
    assert "innerHTML" not in nodes, "innerHTML with node data is an injection waiting to happen"
    assert "textContent" in nodes


def test_chamber_click_enters_capillary_click_opens_the_panel():
    mod = _read("graph/index.js")
    ac = mod.split("function activateCard(", 1)[1].split("\nfunction ", 1)[0]
    assert 'kind === "chamber"' in ac and "drillTo(i, key)" in ac
    assert "openPanel(i, key)" in ac
    assert 'kind === "artifact"' in ac and "asideConclusion" in ac


# --------------------------------------------------------------------------
# game navigation: keyboard first-class, the Atlas map
# --------------------------------------------------------------------------

def test_key_listeners_live_on_the_screen_not_the_document():
    mod = _read("graph/index.js")
    assert 'document.addEventListener("keydown"' not in mod, \
        "a document-level key listener fires under every other screen"
    assert '.screen.addEventListener("keydown"' in mod
    html = _read("index.html")
    assert 'id="graphScreen" hidden tabindex="-1"' in html, \
        "without tabindex the section cannot receive key events at all"


def test_arrows_enter_escape_and_m_are_wired():
    mod = _read("graph/index.js")
    kd = mod.split("function keyDown(", 1)[1].split("\nfunction ", 1)[0]
    for key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"):
        assert key in kd, f"{key} must move the card focus"
    assert '"Enter"' in kd and "activateCard" in kd
    assert '"Escape"' in kd and "drillTo(i, \"\")" in kd, "Esc climbs out of the room"
    assert "toggleAtlas" in kd, "M must toggle the Atlas map"
    css = _read("graph/graph.css")
    assert ".gr-card.gr-focus" in css, "the focus ring must be visible"


def test_the_atlas_overlay_exists_with_click_to_jump():
    html = _read("index.html")
    assert 'id="graphAtlas"' in html
    mod = _read("graph/index.js")
    assert "function toggleAtlas" in mod and "function renderAtlas" in mod
    ra = mod.split("function renderAtlas(", 1)[1].split("\nfunction ", 1)[0]
    assert "data-jump-room" in ra and "data-jump-node" in ra
    assert "you are here" in ra, "the current room must be marked"
    assert "gr-atlas-dot" in ra, "compact node dots with health colours"


def test_breadcrumb_exists_and_climbs():
    html = _read("index.html")
    assert 'id="graphCrumb"' in html
    mod = _read("graph/index.js")
    assert "renderCrumb" in mod
    assert "gr-crumb-up" in mod, "the aim crumb is the way back up a level"


def test_room_transitions_are_css_and_stand_down_under_reduced_motion():
    mod = _read("graph/index.js")
    nav = mod.split("function navTo(", 1)[1].split("\nfunction ", 1)[0]
    assert "reduceMotion()" in nav
    assert nav.index("reduceMotion()") < nav.index("swapTimer"), \
        "the reduced-motion early-out must come before any transition is armed"
    assert "gr-openit" in nav, "entering = the clicked chamber scales up into the room"
    rev = mod.split("function reveal(", 1)[1].split("\nfunction ", 1)[0]
    assert "reduceMotion()" in rev
    assert rev.index("reduceMotion()") < rev.index("setTimeout(step"), \
        "the reduced-motion early-out must come before the stepper is armed"
    css = _read("graph/graph.css")
    reduce = css.split("prefers-reduced-motion", 1)[1]
    for guard in ("gr-hidden", "gr-openit", "gr-room-enter", "gr-flow", "gr-draw", "gr-flash"):
        assert guard in reduce, f"{guard} does not stand down under reduced motion"


def test_no_raf_loop_anywhere():
    """The canvas kept ONE rAF loop for its camera. The Atlas has no camera, so
    it has none at all — every animation is CSS."""
    for rel in ("graph/index.js", "graph/nodes.js", "graph/layout.js"):
        assert "requestAnimationFrame" not in _read(rel), f"{rel} grew a JS animation loop"


# --------------------------------------------------------------------------
# the canvas blockers that stay learned
# --------------------------------------------------------------------------

def test_no_global_singleton_touching_other_canvases():
    for rel in ("graph/index.js", "graph/nodes.js", "graph/layout.js"):
        assert "LWCanvas2" not in _read(rel), f"{rel} touches the Studio canvas's global"


def test_own_inspector_and_action_hosts():
    for rel in ("graph/index.js", "graph/nodes.js", "graph/layout.js"):
        src = _read(rel)
        assert "lwOverlay" not in src, f"{rel} borrows the Studio overlay"
        assert "sdPortal" not in src, f"{rel} borrows the delete portal"
    assert "graphAside" in _read("graph/index.js"), "the inspector renders in this screen's own aside"


def test_reduced_motion_is_decided_locally():
    mod = _read("graph/index.js")
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in mod, \
        "the module must ask matchMedia itself — lib.js's const is not on window"
    css = _read("graph/graph.css")
    assert "prefers-reduced-motion" in css, "the animations must also stand down in CSS"


def test_the_poll_pauses_while_the_panel_or_map_is_open():
    """agent.js's lesson: repainting under the reader blanks the inspector
    mid-sentence — and a repaint under the open map would be worse."""
    mod = _read("graph/index.js")
    assert "inst.inspectKey || inst.transition || inst.atlasOpen" in mod


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
# the source seam: renderers stay source-agnostic
# --------------------------------------------------------------------------

def test_the_graph_src_seam_keeps_its_verbs():
    mod = _read("graph/index.js")
    assert "DEVTEAM_GRAPH_SRC" in mod
    seam = mod.split("DEVTEAM_GRAPH_SRC = {", 1)[1].split("};", 1)[0]
    for verb in ("fetch:", "verify:", "setConfig:", "inspect:", "replan:",
                 "service:", "setAgent:", "removeNode:", "replaceAspect:",
                 "team:", "setTeam:", "cluster:"):
        assert verb in seam, f"the seam lost its {verb.rstrip(':')} verb"
    assert "/api/graph/self" in seam


# --------------------------------------------------------------------------
# the ONE panel — kept verbatim from the canvas, because it was good
# --------------------------------------------------------------------------

def test_single_click_on_a_capillary_opens_the_one_full_panel():
    mod = _read("graph/index.js")
    assert "function openPanel" in mod and "function renderPanel" in mod
    for gone in ("asideLight", "asideGroup", "openInspector", "renderInspector"):
        assert gone not in mod, f"the two-tier aside is back ({gone})"


def test_the_panel_has_three_replaceable_sections():
    mod = _read("graph/index.js")
    rp = mod.split("function renderPanel(", 1)[1].split("\nfunction ", 1)[0]
    for header in ("Contract", "Test suite", "Agent"):
        assert header in rp, f"the panel lost its {header} section"
    for bid in ("grSecStack", "grSecTests", "grSecAgent",
                "grRepStack", "grRepTests", "grAgentPick"):
        assert bid in rp, f"the panel lost {bid}"
    wp = mod.split("function wirePanel(", 1)[1].split("\nfunction ", 1)[0]
    assert 'replaceDialog(i, key, "stack"' in wp and 'replaceDialog(i, key, "tests"' in wp
    assert "agentPicker(i, key)" in wp, "the agent section's replace IS the picker"


def test_the_panel_puts_the_agent_front_and_center():
    mod = _read("graph/index.js")
    assert "function agentRow" in mod
    rp = mod.split("function renderPanel(", 1)[1].split("\nfunction ", 1)[0]
    assert "${agentRow(" in rp, "the panel must render the agent row"
    assert "the specialist working this service" in mod
    assert "the manager staffs modules when it authors the plan" in mod, \
        "the unassigned fallback must say WHO assigns and WHEN"
    assert "Have the manager plan now" in mod
    assert "i.src.replan()" in mod, "the unassigned button must call the replan verb"


def test_the_model_select_rides_the_servers_own_option_list():
    mod = _read("graph/index.js")
    assert "d.models" in mod, "the select must prefer the payload's option list"
    py = (REPO / "conductor" / "app" / "routes" / "graph.py").read_text()
    assert "_known_models" in py
    assert '"models": sorted(_known_models())' in py, \
        "both graph payloads must carry the valid model ids"
    assert "valid: " in py, "the 400 detail must SAY the valid options"


def test_the_panel_shows_the_contract_not_a_derived_tech_stack():
    """P6 replaced the "Tech stack" section outright. It derived a language mix
    from the node's FILE LIST — which is exactly what the owner said he did not
    want to see, and which the payload no longer carries at all. What renders in
    its place is the service's own committed openapi.json as an endpoint list: the
    one honest answer to "what is this thing" that is not a description of its
    insides."""
    mod = _read("graph/index.js")
    for gone in ("techStack", "EXT_KIND", "nodePaths", "Python / FastAPI",
                 "JavaScript / vanilla", "gr-stack-chip"):
        assert gone not in mod, f"the file-derived stack section is back ({gone})"
    cr = mod.split("function contractRows(", 1)[1].split("\nfunction ", 1)[0]
    assert "d.contract" in cr and "endpoints" in cr
    assert "e.method" in cr and "e.path" in cr, "the endpoint list is the contract"
    assert "contract_note" in cr, \
        "a card with no contract of its own must say why, not render an empty box"

def test_mastery_badge_is_earned_never_asserted():
    mod = _read("graph/index.js")
    ml = mod.split("function masteryLine(", 1)[1].split("\nfunction ", 1)[0]
    assert "★ Master of this module" in ml
    assert "working toward mastery" in ml and "/3 runs" in ml
    assert "if (!m || m.runs == null)" in ml, "mastery is optional — absence renders nothing"


def test_agent_picker_and_team_selector():
    html = _read("index.html")
    assert 'id="graphTeam"' in html
    assert "independent of the sprint crew" in html, \
        "the selector's one-line explanation tooltip is gone"
    mod = _read("graph/index.js")
    lt = mod.split("async function loadTeam(", 1)[1].split("\nfunction ", 1)[0]
    assert "sel.hidden = true" in lt, \
        "no endpoint → no dropdown; a dead selector would be a lie"
    assert "i.src.setTeam" in lt
    ap = mod.split("async function agentPicker(", 1)[1].split("\nfunction ", 1)[0]
    assert "i.src.team()" in ap, "the picker is fed by GET /api/graph/self/team"
    assert "i.src.setAgent(key, m.id)" in ap
    assert "textContent" in ap, "member names are free text — textContent only"


# --------------------------------------------------------------------------
# the right-click verb menu — rebound to cards, all six verbs intact
# --------------------------------------------------------------------------

def test_context_menu_lists_the_six_verbs():
    mod = _read("graph/index.js")
    assert '.addEventListener("contextmenu"' in mod
    assert 'removeEventListener("contextmenu"' in mod, "the listener must die with the instance"
    cm = mod.split("function contextMenu(", 1)[1].split("\nfunction ", 1)[0]
    assert "preventDefault" in cm, "the native menu must be suppressed inside the stage"
    assert ".gr-card" in cm, "the menu must resolve the card under the click"
    nm = mod.split("function nodeMenu(", 1)[1].split("\nfunction ", 1)[0]
    for verb in ('"Start"', '"Stop"', '"Peek"', '"Test"', '"Remove"', '"Replace ▸"'):
        assert f"label: {verb}" in nm, f"the card menu lost its {verb} verb"
    # Start/Stop honour the service contract: disabled + the honest reason as tooltip
    assert "control === false" in nm and "svc.reason" in nm
    # ...and the SUB-SWITCH: the conductor cannot stop itself from inside a request
    # it is serving, but the IT crew it hosts is a real toggle and gets its own pair.
    assert "svc.sub" in nm and "sub.key" in nm, "the card's sub-switch is gone"
    # Remove is honest about what a card IS now — a services.yaml edit, so the verb
    # is greyed with the reason rather than offering a click that always fails.
    assert "rm.allowed === false" in nm and "rm.reason" in nm, \
        "Remove must disable itself with the registry reason"
    lm = mod.split("function levelMenu(", 1)[1].split("\nfunction ", 1)[0]
    for entry in ("Open the Atlas map", "Toggle theme", "Back to overview"):
        assert entry in lm, f"the room menu lost its {entry} entry"


# --------------------------------------------------------------------------
# P6: the cards ARE services — the three pins the phase is judged on
# --------------------------------------------------------------------------

def test_the_atlas_renders_services_not_code_modules():
    """The owner's correction, pinned on the screen side. A card carries a live
    STATE read from the fleet (running / stopped / idle / unknown), and `unknown`
    is a state of its own because the `--legacy` boot has no fleet manager to ask
    — a card that printed "stopped" there would be lying about a service that is
    perfectly up."""
    nodes = _read("graph/nodes.js")
    sc = nodes.split("function stateChip(", 1)[1].split("\nfunction ", 1)[0]
    assert "n.service" in sc and "gr-state-" in sc
    assert '"none"' in sc, "a card with no switch must render no chip at all"
    for state in ("running", "stopped", "idle", "unknown"):
        assert state in nodes, f"the {state} state has no chip text"
    fill = nodes.split("function fill(", 1)[1].split("\nfunction ", 1)[0]
    assert "stateChip(n)" in fill, "the chip must be on the card, not only in the panel"
    css = _read("graph/graph.css")
    for cls in (".gr-state-running", ".gr-state-stopped", ".gr-state-unknown"):
        assert cls in css, f"{cls} is gone"


def test_start_and_stop_are_real_verbs_on_every_card():
    """The seam's `service` verb carries the sub-switch, the panel offers the pair
    beside the health, and a card with no switch shows the honest reason instead of
    a dead button."""
    mod = _read("graph/index.js")
    seam = mod.split("DEVTEAM_GRAPH_SRC = {", 1)[1].split("};", 1)[0]
    assert "service: (key, action, sub)" in seam, "the seam lost the sub-switch"
    assert 'JSON.stringify({ action, sub: sub || "" })' in seam
    cr = mod.split("function controlRows(", 1)[1].split("\nfunction ", 1)[0]
    assert "grSvcStart" in cr and "grSvcStop" in cr
    assert "s.control" in cr and "s.reason" in cr, \
        "a card with no switch must render the reason, never a dead button"
    assert "grSubStart" in cr and "grSubStop" in cr, "the sub-switch pair is gone"
    assert "process-compose:" in cr, "the card must show the fleet's own verdict"
    wp = mod.split("function wirePanel(", 1)[1].split("\nfunction ", 1)[0]
    assert 'switchIt(i.aside.querySelector("#grSvcStop"), "#grSvcOut", "stop", "")' in wp
    assert '"repair"' in wp, "the crew's sub-switch is not wired"
    assert "refetch(i)" in wp, "the card's glow must repaint from the fresh truth"


def test_the_panel_never_renders_a_filesystem_path():
    """THE BLACK BOX STAYS CLOSED — the owner's decree, on the screen side: "I
    don't wanna understand what's inside — that's the vibe-coding part."

    The BFF strips `paths` from every payload, so the panel could not render one
    even if it tried; this pin is the other half, against the panel trying. The
    test-suite section shows COUNTS and a headline (it used to print every mapped
    file name), and nothing anywhere reads a path field off a node."""
    mod = _read("graph/index.js")
    rp = mod.split("function renderPanel(", 1)[1].split("\nfunction ", 1)[0]
    for leak in ("n.paths", "live.paths", "tt.path", "e.contract_test"):
        assert leak not in rp, f"the panel renders {leak}"
    assert "t.suite" in rp and "t.passing" in rp, \
        "the suite section must be counts and a headline, never file names"
    assert "esc(tt.path)" not in mod, "the per-file test rows are back"
    for sec in ("grSecHealth", "grSecLogs"):
        assert sec in rp, f"the panel lost its {sec} section"
    lr = mod.split("function logRows(", 1)[1].split("\nfunction ", 1)[0]
    assert "d.logs" in lr and "gr-logs" in lr, "the log tail is gone"
    hr = mod.split("function healthRows(", 1)[1].split("\nfunction ", 1)[0]
    assert "det.ok" in hr and "checks" in hr, \
        "the service's own readiness document must show, checks and all"


def test_remove_confirms_and_replace_files_a_ticket():
    mod = _read("graph/index.js")
    rm = mod.split("async function removeNodeFlow(", 1)[1].split("\nfunction ", 1)[0]
    assert "W.confirm" in rm, "Remove without a confirm is a foot-gun"
    assert "refetch(i)" in rm, "the room must repaint after a remove"
    rd = mod.split("function replaceDialog(", 1)[1].split("\nasync function ", 1)[0]
    assert "ticket filed" in rd
    assert "openProject" in rd, "the filed ticket must link via openProject"
    assert 'aspect' in rd and "replaceAspect" in rd


# --------------------------------------------------------------------------
# themes — two looks, scoped, persisted
# --------------------------------------------------------------------------

def test_two_themes_scoped_to_graphscreen_with_persistence():
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


# --------------------------------------------------------------------------
# tri-state health glow + the legend
# --------------------------------------------------------------------------

def test_tri_state_health_classes_with_reduced_motion_stand_down():
    nodes = _read("graph/nodes.js")
    assert '"gr-hs-" + s' in nodes
    assert "tests failing, heartbeat fine" in nodes and "heartbeat failing" in nodes, \
        "the yellow/red tooltips must say exactly what is wrong"
    css = _read("graph/graph.css")
    for cls in (".gr-hs-green", ".gr-hs-yellow", ".gr-hs-red"):
        assert cls in css, f"{cls} is gone"
    for anim in ("gr-breathe", "gr-amber", "gr-strobe"):
        assert anim in css, f"the {anim} animation is gone"
    reduce = css.split("prefers-reduced-motion", 1)[1]
    assert ".gr-card.gr-hs-green, .gr-card.gr-hs-yellow, .gr-card.gr-hs-red { animation: none; }" \
        in reduce, "tri-state must collapse to static colored borders under reduced motion"
    mod = _read("graph/index.js")
    dg = mod.split("function deriveGroupHealth(", 1)[1].split("\nfunction ", 1)[0]
    assert "Math.max" in dg, "a group rolls the WORST of its children"
    assert "n.health && n.health.status" in dg, \
        "a payload-rolled group health must win over the client's derivation"


def test_health_legend_bottom_left():
    html = _read("index.html")
    assert 'id="graphLegend"' in html
    mod = _read("graph/index.js")
    lg = mod.split("function renderLegend(", 1)[1].split("\nfunction ", 1)[0]
    for line in ("calm glow", "tests failing, heartbeat fine", "heartbeat itself is failing"):
        assert line in lg, "the legend must explain each state in one line"
    css = _read("graph/graph.css")
    leg = css.split(".gr-legend {", 1)[1].split("}", 1)[0]
    assert "left: 14px" in leg and "bottom: 14px" in leg


# --------------------------------------------------------------------------
# the Artifact: last column of the top room, a header chip everywhere else
# --------------------------------------------------------------------------

def test_conclusion_is_the_artifact_card_and_the_goal_chip():
    mod = _read("graph/index.js")
    rn = mod.split("function roomNodes(", 1)[1].split("\nfunction ", 1)[0]
    assert 'n.node_type !== "conclusion"' in rn, \
        "the conclusion must never be an ordinary room card"
    gt = mod.split("function goalTitle(", 1)[1].split("\nfunction ", 1)[0]
    assert "The Artifact — the running platform" in gt
    assert "/artifact/i" in gt, \
        "override the display client-side ONLY when the backend has not renamed it"
    paint = mod.split("function paint(", 1)[1].split("\nfunction ", 1)[0]
    assert "gr-col-artifact" in paint and "buildArtifactCard" in paint, \
        "the Artifact is the top room's always-last column"
    gc = mod.split("function renderGoalChip(", 1)[1].split("\nfunction ", 1)[0]
    assert "!i.level" in gc, "the chip shows in sub-rooms; the top room has the card"
    assert 'drillTo(i, "")' in gc, "the chip links home"
    html = _read("index.html")
    assert 'id="graphGoal"' in html


def test_cluster_iframe_is_sandboxed_and_never_the_live_app():
    mod = _read("graph/index.js")
    assert 'sandbox="allow-same-origin allow-scripts"' in mod
    cs = mod.split("function clusterSafeUrl(", 1)[1].split("\nfunction ", 1)[0]
    assert "url.origin === location.origin" in cs and 'url.pathname === "/"' in cs, \
        "the app's own root must be refused — never iframe the live app itself"
    ch = mod.split("function clusterHtml(", 1)[1].split("\nfunction ", 1)[0]
    assert "cl.available" in ch and "cl.reason" in ch, \
        "unavailability must show the honest reason"
    assert "grClusterStart" in ch and "grClusterStop" in ch
    gp = mod.split("function asideConclusion(", 1)[1].split("\nfunction ", 1)[0]
    assert "uptime" in gp and "boot_sha" in gp and "Mini cluster" in gp


def test_an_unreadable_store_says_so_instead_of_drawing_an_empty_repository():
    """P5: the graph's rows are another process's, and when that process is not
    answering `/api/graph/self` returns 200 with `degraded: true`, no plan and no
    nodes. Painting that as a bare room would read as "this repository has no
    modules", which is the one thing it does not mean — so the room says it, and
    says the reason the payload gave (which names what is still true: the crew
    keeps building; only the map is gone)."""
    mod = _read("graph/index.js")
    assert "function paintUnavailable(" in mod
    pu = mod.split("function paintUnavailable(", 1)[1].split("\nfunction ", 1)[0]
    assert "data.degraded" in pu and "(data.nodes || []).length" in pu, \
        "the banner must key off the payload's own flag, not off emptiness alone"
    assert "gr-unavailable" in pu and "data.reason" in pu, \
        "the honest reason comes from the backend, never from a hardcoded guess"
    paint = mod.split("\nfunction paint(", 1)[1].split("\nfunction ", 1)[0]
    assert "paintUnavailable(i, data)" in paint.split("\n", 3)[1], \
        "the check must come FIRST — the normal path assumes a plan"
    assert ".gr-unavailable {" in _read("graph/graph.css")
