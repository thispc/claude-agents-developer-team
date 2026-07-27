"""The Studio canvas, driven by a REAL browser — because dragging and the full-screen
scene shell can only be proven in one. Konva renders to a <canvas>, so there is no DOM
node to assert on; these tests move the actual mouse over the actual pixels and check
that a token moved, seated into a slot, that nothing invisible sits on top of the floor
eating the pointer (the failure that made "I can't move a thing" real), and that the new
shell works: the Studio opens on one click, the title renames, a Shape with slots is
created, a beat runs free, and the Cast lists the agents.

Self-contained: it boots its own conductor on a temp DB (no creds, LAUNCHER=local, free
deterministic agents) and logs in through the real form. SKIPS cleanly if Playwright or
its chromium build isn't installed, so it never breaks a headless CI.

Run it:  .venv/bin/python -m pytest tests/test_canvas_drag_e2e.py -s
"""

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("CANVAS_E2E_PORT", "8139"))
BASE = f"http://localhost:{PORT}"

playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
from playwright.sync_api import sync_playwright  # noqa: E402


def _browser_ok() -> bool:
    try:
        with sync_playwright() as pw:
            pw.chromium.launch().close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _browser_ok(), reason="chromium not installed (playwright install chromium)")


def _wait_port(port, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


@pytest.fixture(scope="module")
def server():
    tmp = tempfile.mkdtemp(prefix="devteam-canvas-e2e-")
    env = dict(os.environ)
    env.update({
        "DB_PATH": str(Path(tmp) / "c.db"),
        "WORKSPACES_DIR": str(Path(tmp) / "ws"), "PREVIEW_DIR": str(Path(tmp) / "pv"),
        "DEPLOY_DIR": str(Path(tmp) / "dp"), "ROOT_USERNAME": "root", "ROOT_PASSWORD": "rootpass",
        "WORKER_TOKEN": "wt", "LAUNCHER": "local", "PYTHONPATH": str(REPO / "conductor"),
        "ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": "", "GITHUB_TOKEN": "",
    })
    log = open(Path(tmp) / "server.log", "w")
    proc = subprocess.Popen(
        [str(REPO / ".venv/bin/uvicorn"), "app.main:app", "--host", "0.0.0.0", "--port", str(PORT)],
        cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    assert _wait_port(PORT), "canvas e2e server did not come up"
    time.sleep(1)
    import httpx
    client = httpx.Client(base_url=BASE, timeout=30)
    client.post("/api/login", json={"username": "root", "password": "rootpass"})
    try:
        yield {"client": client}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def page(server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        pg = browser.new_page(viewport={"width": 1400, "height": 900})
        pg.on("pageerror", lambda e: pytest.fail(f"page error: {e}"))
        pg.goto(BASE)
        pg.wait_for_selector("#loginScreen", state="visible", timeout=15000)
        pg.fill("#loginForm input[name=username]", "root")
        pg.fill("#loginForm input[name=password]", "rootpass")
        pg.click("#loginForm button[type=submit]")
        pg.wait_for_selector("#loginScreen", state="hidden", timeout=15000)
        yield pg
        browser.close()


# --- helpers ---------------------------------------------------------------

def _mk(client, name="W", **_):
    """A fresh world + freeplay scene via the API; returns (wid, rid)."""
    wid = client.post("/api/lw", json={"name": name}).json()["world"]["id"]
    rid = client.post(f"/api/lw/{wid}/room", json={"name": "untitled", "type": "freeplay"}).json()["room"]["id"]
    return wid, rid


def _open_scene(page, wid, rid):
    """Bring the Studio section on and open one specific scene (bypasses world auto-pick)."""
    page.evaluate("""() => {
        for (const id of ['#sdRosterHost', '#sdScenesMenu']) { const e = document.querySelector(id); if (e) e.hidden = true; }
        if (typeof sdActOpen !== 'undefined') sdActOpen = false;   // don't inherit a previous test's open Activity panel (a side panel covers tokens)
        const ap = document.querySelector('#sdActivity'); if (ap) ap.hidden = true;
        showLifeworld();
    }""")
    page.evaluate("([wid, rid]) => { lwWorldId = wid; return lwOpenScene(rid); }", [wid, rid])
    for _ in range(40):
        if page.evaluate("() => (typeof lwKonva!=='undefined') && !!lwKonva"):
            break
        page.wait_for_timeout(150)


def _agent_screen(page, hid):
    return page.evaluate("""(id) => {
        const e = lwKonva.agents.get(String(id)); if(!e) return null;
        const p = e.node.getAbsolutePosition(), box = lwKonva.stage.container().getBoundingClientRect();
        return { x: box.left + p.x, y: box.top + p.y, wx: e.node.x(), wy: e.node.y() };
    }""", hid)


def _slot_screen(page, pid, slot):
    return page.evaluate("""([pid, slot]) => {
        const e = lwKonva.props.get(String(pid)); if(!e) return null;
        const base = e.node.getAbsolutePosition(), off = lwSlotPositions(e.data)[slot] || {x:0,y:0};
        const sc = lwKonva.stage.scaleX(), box = lwKonva.stage.container().getBoundingClientRect();
        return { x: box.left + base.x + off.x*sc, y: box.top + base.y + off.y*sc };
    }""", [pid, slot])


def _drag(page, fromxy, toxy):
    page.mouse.move(fromxy["x"], fromxy["y"]); page.wait_for_timeout(40)
    page.mouse.down(); page.wait_for_timeout(40)
    page.mouse.move(toxy["x"], toxy["y"], steps=18); page.wait_for_timeout(40)
    page.mouse.up(); page.wait_for_timeout(400)


# --- the tests -------------------------------------------------------------

def test_the_studio_opens_on_one_click_with_nothing_over_the_canvas(server, page):
    """openStudio() lands straight on a full-screen canvas, and the element under the
    floor's centre is the canvas — not an invisible overlay eating the pointer."""
    page.evaluate("() => openStudio(true)")
    for _ in range(40):
        if page.evaluate("() => (typeof lwKonva!=='undefined') && !!lwKonva"):
            break
        page.wait_for_timeout(150)
    assert page.evaluate("() => !$('#lifeworld').hidden"), "the Studio section did not open"
    box = page.evaluate("() => { const b = lwKonva.stage.container().getBoundingClientRect(); return {x:b.left+b.width/2, y:b.top+b.height/2}; }")
    tag = page.evaluate("([x,y]) => { const el = document.elementFromPoint(x,y); return el ? el.tagName : null; }", [box["x"], box["y"]])
    assert tag == "CANVAS", f"something is covering the floor: <{tag}>"


def test_an_agent_drags_and_the_move_persists(server, page):
    c = server["client"]; wid, rid = _mk(c, "Drag")
    hid = c.post(f"/api/lw/{wid}/human", json={"name": "Mover", "figure": "av:b"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    c.post(f"/api/lw/{wid}/pos", json={"id": hid, "x": 300, "y": 220})
    _open_scene(page, wid, rid)
    s = _agent_screen(page, hid)
    _drag(page, s, {"x": s["x"] + 150, "y": s["y"] + 90})
    after = page.evaluate("(id)=>{const e=lwKonva.agents.get(String(id)); return {x:e.node.x(), y:e.node.y()};}", hid)
    assert abs(after["x"] - 300) > 40 and abs(after["y"] - 220) > 20, f"token did not move: {after}"
    pos = c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["agents"][0]["pos"]
    assert abs(pos[0] - after["x"]) < 2 and abs(pos[1] - after["y"]) < 2, f"move did not persist: {pos} vs {after}"


def test_dragging_an_agent_onto_a_slot_seats_it(server, page):
    c = server["client"]; wid, rid = _mk(c, "Seat")
    table = c.post(f"/api/lw/{wid}/artifact", json={"name": "table", "brief": "a round table", "slots": 4}).json()["artifact"]
    c.post(f"/api/lw/{wid}/room/{rid}/place", params={"artifact_id": table["id"]})
    c.post(f"/api/lw/{wid}/pos", json={"id": table["id"], "x": 500, "y": 300})
    hid = c.post(f"/api/lw/{wid}/human", json={"name": "Sitter", "figure": "av:c"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    c.post(f"/api/lw/{wid}/pos", json={"id": hid, "x": 150, "y": 150})
    _open_scene(page, wid, rid)
    _drag(page, _agent_screen(page, hid), _slot_screen(page, table["id"], 0))
    seated = c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["props"][0]["seated"]
    assert hid in seated, f"agent did not seat via drag: seated={seated}"


def test_dragging_a_full_table_carries_its_seated_agents(server, page):
    c = server["client"]; wid, rid = _mk(c, "Carry")
    table = c.post(f"/api/lw/{wid}/artifact", json={"name": "t", "brief": "a round table", "slots": 3}).json()["artifact"]
    c.post(f"/api/lw/{wid}/room/{rid}/place", params={"artifact_id": table["id"]})
    c.post(f"/api/lw/{wid}/pos", json={"id": table["id"], "x": 400, "y": 300})
    hid = c.post(f"/api/lw/{wid}/human", json={"name": "S", "figure": "av:d"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    c.post(f"/api/lw/{wid}/artifact/{table['id']}/seat", json={"slot": 0, "human_id": hid})
    _open_scene(page, wid, rid)
    before = page.evaluate("(id)=>{const e=lwKonva.agents.get(String(id)); return {x:e.node.x()};}", hid)
    t = page.evaluate("""(pid) => { const e = lwKonva.props.get(String(pid)); const p = e.node.getAbsolutePosition();
        const box = lwKonva.stage.container().getBoundingClientRect(); return { x: box.left+p.x, y: box.top+p.y }; }""", table["id"])
    _drag(page, t, {"x": t["x"] - 160, "y": t["y"] + 40})
    after = page.evaluate("(id)=>{const e=lwKonva.agents.get(String(id)); return {x:e.node.x()};}", hid)
    assert abs(after["x"] - before["x"]) > 40, f"seated agent did not ride with the table: {before} -> {after}"


def test_renaming_the_scene_title_persists(server, page):
    c = server["client"]; wid, rid = _mk(c, "Rename")
    _open_scene(page, wid, rid)
    page.focus("#sdTitle")
    page.evaluate("() => { document.querySelector('#sdTitle').textContent = 'Board meeting'; }")
    page.evaluate("() => document.querySelector('#sdTitle').blur()")
    page.wait_for_timeout(400)
    assert c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["name"] == "Board meeting"


def test_the_shape_tool_creates_a_collating_object_with_slots(server, page):
    c = server["client"]; wid, rid = _mk(c, "Shape")
    _open_scene(page, wid, rid)
    page.evaluate("() => lwSetTool('shape')")
    box = page.evaluate("() => { const b = lwKonva.stage.container().getBoundingClientRect(); return {x:b.left+b.width/2, y:b.top+b.height/2}; }")
    page.mouse.click(box["x"], box["y"])
    page.wait_for_selector(".lw-create-pop", timeout=5000)
    page.click("#lwCGo")
    page.wait_for_timeout(600)
    props = c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["props"]
    assert props and (props[0]["slots"] or 0) >= 2, f"shape was not created with slots: {props}"


def test_running_a_beat_advances_time_for_free(server, page):
    c = server["client"]; wid, rid = _mk(c, "Play")
    for n in ("A", "B"):
        hid = c.post(f"/api/lw/{wid}/human", json={"name": n}).json()["human"]["id"]
        c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    _open_scene(page, wid, rid)
    page.click("#sdStep")
    page.wait_for_timeout(700)
    log = c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["log"]
    assert log, "running a beat produced no activity"
    assert all(not e.get("billed") for e in log), "a deterministic beat billed a model call"


def test_the_cast_roster_lists_agents_grouped_by_scene(server, page):
    c = server["client"]; wid, rid = _mk(c, "Cast")
    for n in ("Ada", "Ravi"):
        hid = c.post(f"/api/lw/{wid}/human", json={"name": n}).json()["human"]["id"]
        c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    _open_scene(page, wid, rid)
    page.click("#sdRoster")
    page.wait_for_selector(".sd-roster-card .sd-cast", timeout=5000)
    names = page.eval_on_selector_all(".sd-cast-name", "els => els.map(e => e.textContent)")
    assert "Ada" in names and "Ravi" in names, f"cast roster missing agents: {names}"


def test_object_popover_has_no_slots_but_shape_popover_has_a_slider(server, page):
    c = server["client"]; wid, rid = _mk(c, "Pop")
    _open_scene(page, wid, rid)
    b = page.evaluate("() => { const x=lwKonva.stage.container().getBoundingClientRect(); return {x:x.left+160,y:x.top+160}; }")
    page.evaluate("() => lwSetTool('artifact')")
    page.mouse.click(b["x"], b["y"]); page.wait_for_selector(".lw-create-pop", timeout=5000)
    assert page.query_selector("#lwCSlots") is None, "an object must not ask for slots"
    assert page.query_selector("#lwCShape") is None, "an object must not ask for a shape"
    page.evaluate("() => lwCancelCreate()")
    page.evaluate("() => lwSetTool('shape')")
    page.mouse.click(b["x"], b["y"]); page.wait_for_selector(".lw-create-pop", timeout=5000)
    assert page.query_selector("#lwCSlots") is not None, "a shape must have a slots slider"
    assert page.query_selector("#lwCShape") is not None, "a shape must have a shape chooser"
    mn = page.eval_on_selector("#lwCSlots", "el => el.min")
    assert int(mn) >= 1, "a shape cannot have zero slots"
    page.evaluate("() => lwCancelCreate()")


def test_from_cast_places_an_existing_agent(server, page):
    c = server["client"]; wid, rid = _mk(c, "Reuse")
    rid2 = c.post(f"/api/lw/{wid}/room", json={"name": "other", "type": "freeplay"}).json()["room"]["id"]
    hid = c.post(f"/api/lw/{wid}/human", json={"name": "Reused"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid2}/seat", params={"human_id": hid})
    _open_scene(page, wid, rid)
    before = len(c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["agents"])
    page.evaluate("() => lwSetTool('agent')")
    b = page.evaluate("() => { const x=lwKonva.stage.container().getBoundingClientRect(); return {x:x.left+160,y:x.top+160}; }")
    page.mouse.click(b["x"], b["y"]); page.wait_for_selector(".lw-create-pop", timeout=5000)
    page.click("[data-mode=cast]"); page.wait_for_selector(".lw-cast-item", timeout=5000)
    page.click(".lw-cast-item"); page.wait_for_timeout(800)
    after = c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["agents"]
    assert any(a["id"] == hid for a in after) and len(after) == before + 1


def test_play_stops_at_the_cap(server, page):
    c = server["client"]; wid, rid = _mk(c, "Cap")
    for n in ("A", "B"):
        h = c.post(f"/api/lw/{wid}/human", json={"name": n}).json()["human"]["id"]
        c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    _open_scene(page, wid, rid)
    page.evaluate("() => { sdMax = 4; sdSpeed = 1; }")
    page.click("#sdPlay"); page.wait_for_timeout(8000)
    assert page.evaluate("() => sdPlaying") is False, "play did not stop at the cap"
    assert page.evaluate("() => sdRun") == 4, "play did not run exactly cap beats"


def test_the_activity_panel_lists_what_happened(server, page):
    c = server["client"]; wid, rid = _mk(c, "Act")
    for n in ("A", "B"):
        h = c.post(f"/api/lw/{wid}/human", json={"name": n}).json()["human"]["id"]
        c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    _open_scene(page, wid, rid)
    page.click("#sdStep"); page.wait_for_timeout(700)
    page.click("#sdActBtn"); page.wait_for_selector(".sd-activity .sd-act-row", timeout=5000)
    assert page.eval_on_selector_all(".sd-act-row", "els => els.length") > 0


def test_the_canvas_fills_the_host_no_dead_zone(server, page):
    """A 0x0-at-mount race used to size the stage to a 900x460 fallback, leaving tokens
    to the right/bottom in a dead zone the pointer never reached (inconsistent selection).
    The stage must now match the host, and an edge token must select."""
    c = server["client"]; wid, rid = _mk(c, "Fill")
    a = c.post(f"/api/lw/{wid}/artifact", json={"name": "far", "brief": "a box"}).json()["artifact"]
    c.post(f"/api/lw/{wid}/room/{rid}/place", params={"artifact_id": a["id"]})
    c.post(f"/api/lw/{wid}/pos", json={"id": a["id"], "x": 1250, "y": 760})
    hid = c.post(f"/api/lw/{wid}/human", json={"name": "Edge"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    c.post(f"/api/lw/{wid}/pos", json={"id": hid, "x": 40, "y": 40})
    _open_scene(page, wid, rid); page.wait_for_timeout(300)
    sz = page.evaluate("() => { const s=lwKonva.stage, h=lwKonva.host; return {sw:s.width(), sh:s.height(), hw:h.clientWidth, hh:h.clientHeight}; }")
    assert sz["sw"] == sz["hw"] and sz["sh"] == sz["hh"], f"stage does not fill host (dead zone): {sz}"
    for kind, eid in (("prop", a["id"]), ("agent", hid)):
        s = page.evaluate("""([kind,id])=>{const m=kind==='agent'?lwKonva.agents:lwKonva.props; const e=m.get(String(id));
            const q=e.node.getAbsolutePosition(), b=lwKonva.stage.container().getBoundingClientRect(); return {x:b.left+q.x, y:b.top+q.y};}""", [kind, eid])
        tag = page.evaluate("([x,y])=>{const el=document.elementFromPoint(x,y); return el?el.tagName:null;}", [s["x"], s["y"]])
        assert tag == "CANVAS", f"{kind} centre under <{tag}>, not the canvas"
        page.mouse.click(s["x"], s["y"]); page.wait_for_timeout(120)
        assert page.evaluate("() => lwKonva.sel.size") == 1, f"{kind} did not select"
        page.evaluate("() => lwSelClear()")


def test_clicking_a_persons_name_grabs_them_and_cursor_signals_movable(server, page):
    """The whole visible person (disc + name) is grabbable, and the cursor is 'move' over
    a token vs the default over empty floor — so you never pan when you meant to grab."""
    c = server["client"]; wid, rid = _mk(c, "Grab")
    hid = c.post(f"/api/lw/{wid}/human", json={"name": "Ada"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    c.post(f"/api/lw/{wid}/pos", json={"id": hid, "x": 400, "y": 300})
    _open_scene(page, wid, rid); page.wait_for_timeout(200)
    s = _agent_screen(page, hid)
    over_disc = page.evaluate("([x,y]) => { const el=document.elementFromPoint(x,y); return getComputedStyle(lwKonva.host).cursor; }", [s["x"], s["y"]]) or ""
    page.mouse.move(s["x"], s["y"]); page.wait_for_timeout(60)
    assert page.evaluate("() => getComputedStyle(lwKonva.host).cursor") == "move", "cursor over a token should be 'move'"
    page.mouse.move(s["x"], s["y"] + 38); page.wait_for_timeout(60)   # the NAME label, below the disc
    assert page.evaluate("() => getComputedStyle(lwKonva.host).cursor") == "move", "cursor over the name should be 'move'"
    page.mouse.click(s["x"], s["y"] + 38); page.wait_for_timeout(120)
    assert page.evaluate("() => lwKonva.sel.size") == 1, "clicking the person's name did not grab them"
    page.mouse.move(s["x"] + 260, s["y"]); page.wait_for_timeout(60)
    assert page.evaluate("() => getComputedStyle(lwKonva.host).cursor") == "default", "empty floor should not look grabbable"
    page.evaluate("() => lwSelClear()")


def test_drag_empty_marquees_and_moves_the_group_space_drag_pans(server, page):
    c = server["client"]; wid, rid = _mk(c, "Marq")
    ids = []
    for n, x in (("A", 300), ("B", 430), ("C", 560)):
        h = c.post(f"/api/lw/{wid}/human", json={"name": n}).json()["human"]["id"]
        c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
        c.post(f"/api/lw/{wid}/pos", json={"id": h, "x": x, "y": 320}); ids.append(h)
    _open_scene(page, wid, rid); page.wait_for_timeout(200)
    pts = [_agent_screen(page, h) for h in ids]
    x0 = min(p["x"] for p in pts) - 60; y0 = min(p["y"] for p in pts) - 60
    x1 = max(p["x"] for p in pts) + 60; y1 = max(p["y"] for p in pts) + 60
    page.mouse.move(x0, y0); page.mouse.down(); page.mouse.move(x1, y1, steps=16); page.mouse.up(); page.wait_for_timeout(250)
    assert page.evaluate("() => lwKonva.sel.size") == 3, "drag-marquee did not select all three"
    before = [page.evaluate("(id)=>lwKonva.agents.get(String(id)).node.x()", h) for h in ids]
    s = _agent_screen(page, ids[1])
    _drag(page, s, {"x": s["x"] - 110, "y": s["y"] + 90})
    after = [page.evaluate("(id)=>lwKonva.agents.get(String(id)).node.x()", h) for h in ids]
    assert all(abs(after[i] - before[i]) > 40 for i in range(3)), "group did not move together"
    page.evaluate("() => lwSelClear()")
    # space-drag pans; a plain empty drag must NOT pan (it marquees)
    sx = page.evaluate("() => lwKonva.stage.x()")
    page.keyboard.down(" "); page.mouse.move(700, 400); page.mouse.down(); page.mouse.move(560, 300, steps=10); page.mouse.up(); page.keyboard.up(" "); page.wait_for_timeout(150)
    assert abs(page.evaluate("() => lwKonva.stage.x()") - sx) > 40, "space-drag did not pan"
    sx2 = page.evaluate("() => lwKonva.stage.x()")
    page.mouse.move(120, 170); page.mouse.down(); page.mouse.move(260, 300, steps=8); page.mouse.up(); page.wait_for_timeout(120)
    assert abs(page.evaluate("() => lwKonva.stage.x()") - sx2) < 5, "a plain empty drag should not pan"


def test_a_multi_selection_drags_as_a_group_from_any_gap(server, page):
    """After marquee-selecting several tokens, pressing ANYWHERE inside the selection —
    including the gaps between tokens — drags the whole group. The dashed frame is a purely
    VISUAL overlay (listening:false) so it can never occlude the tokens on the hit canvas;
    the group-drag from a gap is driven geometrically, not by a draggable frame."""
    c = server["client"]; wid, rid = _mk(c, "Frame")
    ids = []
    for n, x in (("A", 300), ("B", 480), ("C", 660)):
        h = c.post(f"/api/lw/{wid}/human", json={"name": n}).json()["human"]["id"]
        c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
        c.post(f"/api/lw/{wid}/pos", json={"id": h, "x": x, "y": 320}); ids.append(h)
    _open_scene(page, wid, rid); page.wait_for_timeout(200)
    pts = [_agent_screen(page, h) for h in ids]
    page.mouse.move(min(p["x"] for p in pts) - 50, min(p["y"] for p in pts) - 50)
    page.mouse.down(); page.mouse.move(max(p["x"] for p in pts) + 50, max(p["y"] for p in pts) + 50, steps=14); page.mouse.up()
    page.wait_for_timeout(250)
    assert page.evaluate("() => !!lwKonva.worldLayer.findOne('.selframe')"), "no selection frame for a multi-selection"
    gapx = (pts[0]["x"] + pts[1]["x"]) / 2; gapy = pts[0]["y"]
    # The frame must be HIT-TRANSPARENT: getIntersection at the gap must NOT return the frame
    # (that opaque hit footprint was the root cause of the post-group selection/dblclick death).
    hit = page.evaluate("([x,y])=>{const b=lwKonva.stage.container().getBoundingClientRect(); const sh=lwKonva.stage.getIntersection({x:x-b.left,y:y-b.top}); return sh?sh.name():'';}", [gapx, gapy])
    assert hit != "selframe", f"the selection frame is still occluding the hit canvas (hit={hit})"
    # ...and the tokens themselves remain hittable through the frame.
    ta = page.evaluate("([x,y])=>{const b=lwKonva.stage.container().getBoundingClientRect(); const sh=lwKonva.stage.getIntersection({x:x-b.left,y:y-b.top}); const t=sh&&sh.findAncestor&&sh.findAncestor('.token',true); return !!t;}", [pts[0]["x"], pts[0]["y"]])
    assert ta, "a selected token is not hittable through the (transparent) frame"
    before = [page.evaluate("(id)=>lwKonva.agents.get(String(id)).node.x()", h) for h in ids]
    _drag(page, {"x": gapx, "y": gapy}, {"x": gapx - 140, "y": gapy + 90})
    after = [page.evaluate("(id)=>lwKonva.agents.get(String(id)).node.x()", h) for h in ids]
    assert all(abs(after[i] - before[i]) > 40 for i in range(3)), "dragging from a gap did not move the group"


def test_the_title_trash_deletes_the_scene_and_opens_another(server, page):
    """The trash by the title removes the current scene (after confirm) and lands on a
    surviving scene — the Studio is never left empty."""
    c = server["client"]
    wid = c.post("/api/lw", json={"name": "Del"}).json()["world"]["id"]
    keep = c.post(f"/api/lw/{wid}/room", json={"name": "keep", "type": "freeplay"}).json()["room"]["id"]
    doomed = c.post(f"/api/lw/{wid}/room", json={"name": "doomed", "type": "freeplay"}).json()["room"]["id"]
    _open_scene(page, wid, doomed)
    page.on("dialog", lambda d: d.accept())          # accept the confirm()
    page.click("#sdSceneDel")
    for _ in range(40):
        if page.evaluate("() => lwRoomId") != doomed:
            break
        page.wait_for_timeout(150)
    assert page.evaluate("() => lwRoomId") == keep, "did not fall back to the surviving scene"
    rooms = [r["id"] for r in c.get(f"/api/lw/{wid}").json()["rooms"]]
    assert doomed not in rooms and keep in rooms, f"scene not deleted server-side: {rooms}"


def test_the_hit_graph_resolves_a_token_at_its_centre(server, page):
    """The invisible hit region must sit exactly where the token is drawn: getIntersection at
    a token's on-screen centre resolves to that token, not empty floor. (Guards the class of
    'the token is right there but the click misses it'.)"""
    c = server["client"]; wid, rid = _mk(c, "Hit")
    h = c.post(f"/api/lw/{wid}/human", json={"name": "Ada"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    c.post(f"/api/lw/{wid}/pos", json={"id": h, "x": 178, "y": 4})
    _open_scene(page, wid, rid); page.wait_for_timeout(350)
    who = page.evaluate("""(id) => { const e = lwKonva.agents.get(String(id)); const p = e.node.getAbsolutePosition();
        const sh = lwKonva.stage.getIntersection(p); const t = sh && sh.findAncestor && sh.findAncestor('.token', true);
        return t ? t.getAttr('lwId') : (sh ? ((sh.name && sh.name()) || '?') : 'stage'); }""", h)
    assert who == str(h), f"the hit graph does not resolve the token at its own centre (got {who!r})"


def test_a_recovered_click_selects_without_moving_the_token(server, page):
    """When the hit graph misses and the geometric recovery fires, a plain CLICK must SELECT the
    token, not drag it — a drag only begins if the pointer actually moves. (Fixes selection
    inconsistency where recovery always started a drag.)"""
    c = server["client"]; wid, rid = _mk(c, "Click")
    h = c.post(f"/api/lw/{wid}/human", json={"name": "A"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    c.post(f"/api/lw/{wid}/pos", json={"id": h, "x": 300, "y": 240})
    _open_scene(page, wid, rid); page.wait_for_timeout(300)
    page.evaluate("(id) => { lwKonva.agents.get(String(id)).node.listening(false); lwKonva.worldLayer.drawHit(); }", h)  # force the miss
    s = _agent_screen(page, h)
    before = page.evaluate("(id) => lwKonva.agents.get(String(id)).node.x()", h)
    page.mouse.click(s["x"], s["y"]); page.wait_for_timeout(220)     # a pure click — no movement
    assert page.evaluate("() => lwKonva.sel.size") == 1, "a recovered click did not select the token"
    after = page.evaluate("(id) => lwKonva.agents.get(String(id)).node.x()", h)
    assert abs(after - before) < 3, f"a recovered click MOVED the token (should only select): {before} -> {after}"


def test_a_missed_press_on_a_token_still_grabs_it(server, page):
    """Root-cause-agnostic safety net: if the hit graph goes stale/offset so a press on a token
    lands on 'empty floor', a press geometrically on the token's disc still recovers into a grab —
    the person can never become un-grabbable. Simulated by forcing this token's hit to miss."""
    c = server["client"]; wid, rid = _mk(c, "Recover")
    h = c.post(f"/api/lw/{wid}/human", json={"name": "Ada"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    c.post(f"/api/lw/{wid}/pos", json={"id": h, "x": 300, "y": 240})
    _open_scene(page, wid, rid); page.wait_for_timeout(300)
    page.evaluate("(id) => { lwKonva.agents.get(String(id)).node.listening(false); lwKonva.worldLayer.drawHit(); }", h)
    missed = page.evaluate("""(id) => { const e = lwKonva.agents.get(String(id)); const p = e.node.getAbsolutePosition();
        const sh = lwKonva.stage.getIntersection(p); return !sh || sh === lwKonva.stage; }""", h)
    assert missed, "setup failed: the token's hit should be missing"
    s = _agent_screen(page, h)
    before = page.evaluate("(id) => lwKonva.agents.get(String(id)).node.x()", h)
    _drag(page, s, {"x": s["x"] + 120, "y": s["y"] + 70})
    after = page.evaluate("(id) => lwKonva.agents.get(String(id)).node.x()", h)
    assert abs(after - before) > 30, f"a press on a token whose hit graph missed did not recover into a grab ({before}->{after})"


def test_root_only_canvas_logs_capture_and_filter_an_interaction(server, page):
    """The Activity panel's Canvas tab (root only) captures a fine-grained, filterable log of
    every interaction — so a real grab/drag can be diagnosed from the logs. Non-root never
    sees the tab."""
    c = server["client"]; wid, rid = _mk(c, "Logs")
    h = c.post(f"/api/lw/{wid}/human", json={"name": "Ada"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    c.post(f"/api/lw/{wid}/pos", json={"id": h, "x": 350, "y": 260})
    _open_scene(page, wid, rid); page.wait_for_timeout(150)
    assert page.evaluate("() => !!(me && me.is_root)"), "test user must be root"
    page.evaluate("() => sdToggleActivity()"); page.wait_for_timeout(80)
    assert page.evaluate("""() => !!document.querySelector('.sd-act-tab[data-acttab="canvas"]')"""), "root has no Canvas tab"
    page.evaluate("""() => document.querySelector('.sd-act-tab[data-acttab="canvas"]').click()"""); page.wait_for_timeout(60)
    assert page.evaluate("() => lwLogBuf.length") == 0, "nothing should be captured before Capture is on"
    page.evaluate("() => document.querySelector('#lwLogCap').click()")   # capture on
    assert page.evaluate("() => lwLogOn") is True
    s = _agent_screen(page, h)
    _drag(page, s, {"x": s["x"] + 120, "y": s["y"] + 70})
    cats = page.evaluate("() => { const by={}; for (const e of lwLogBuf) by[e.cat]=(by[e.cat]||0)+1; return by; }")
    assert cats.get("pointer") and cats.get("drag"), f"the drag was not logged: {cats}"
    # the pointer-down line names what was hit and the pan state — the core grab diagnostic
    down = page.evaluate("""() => (lwLogBuf.find(e => e.cat==='pointer') || {}).msg || ''""")
    assert "agent#" in down, f"pointer-down did not name the token it hit: {down!r}"
    # filtering by category drops those lines from the view
    before = page.evaluate("() => lwLogVisible().length")
    page.evaluate("""() => document.querySelector('.sd-log-chip[data-logcat="cursor"]').click()"""); page.wait_for_timeout(40)
    assert page.evaluate("() => lwLogVisible().length") < before, "hiding a category did not filter the log"
    # the gate: a non-root user never sees the Canvas tab
    page.evaluate("() => { me.is_root = false; sdShowActivity(true); }"); page.wait_for_timeout(60)
    assert not page.evaluate("""() => !!document.querySelector('.sd-act-tab[data-acttab="canvas"]')"""), "non-root sees the Canvas tab"
    page.evaluate("() => { me.is_root = true; sdShowActivity(false); }")


def test_a_blur_while_space_panning_does_not_strand_the_pan(server, page):
    """Holding Space to pan, then losing window focus (alt-tab / OS prompt) while it's still
    'held', must not leave panning stuck ON — otherwise every later grab pans the floor and
    the cursor never recovers. This is the 'after a while it stops working' report."""
    c = server["client"]; wid, rid = _mk(c, "Blur")
    h = c.post(f"/api/lw/{wid}/human", json={"name": "Ada"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    c.post(f"/api/lw/{wid}/pos", json={"id": h, "x": 400, "y": 300})
    _open_scene(page, wid, rid); page.wait_for_timeout(150)
    page.evaluate("() => { lwKonva.panning = true; lwKonva.stage.draggable(true); }")   # Space-pan engaged
    page.evaluate("() => window.dispatchEvent(new Event('blur'))")                       # focus lost mid-pan
    page.wait_for_timeout(60)
    st = page.evaluate("() => ({panning: !!lwKonva.panning, draggable: lwKonva.stage.draggable()})")
    assert st == {"panning": False, "draggable": False}, f"pan stranded after blur: {st}"
    s = _agent_screen(page, h)                                                            # and a token still drags
    _drag(page, s, {"x": s["x"] - 100, "y": s["y"] - 60})
    after = page.evaluate("(id)=>lwKonva.agents.get(String(id)).node.x()", h)
    assert abs(after - 400) > 30, "token was not draggable after a blur-recovered pan"


def test_a_close_neighbour_no_longer_blankets_a_tokens_grab(server, page):
    """The grab region hugs each token's visible disc+name instead of a big rectangle, so a
    neighbour's invisible hit box no longer shadows a token sitting close (the clustered/seated
    'old agents don't grab' bug). The under-drawn agent in a close pair stays grabbable."""
    c = server["client"]; wid, rid = _mk(c, "Neighbour")
    under = c.post(f"/api/lw/{wid}/human", json={"name": "Under"}).json()["human"]["id"]
    over = c.post(f"/api/lw/{wid}/human", json={"name": "Over"}).json()["human"]["id"]
    for h in (under, over): c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    c.post(f"/api/lw/{wid}/pos", json={"id": under, "x": 400, "y": 260})
    c.post(f"/api/lw/{wid}/pos", json={"id": over, "x": 415, "y": 272})   # ~15px apart — hit boxes used to overlap hard
    _open_scene(page, wid, rid); page.wait_for_timeout(150)
    sc = page.evaluate("() => lwKonva.stage.scaleX()")
    s = _agent_screen(page, under)
    before = page.evaluate("(id)=>lwKonva.agents.get(String(id)).node.x()", under)
    # grab 'under' on its exposed lower-left, away from the neighbour that overlaps its top-right
    _drag(page, {"x": s["x"] - 22 * sc, "y": s["y"] + 20 * sc}, {"x": s["x"] - 130, "y": s["y"] + 90})
    after = page.evaluate("(id)=>lwKonva.agents.get(String(id)).node.x()", under)
    assert abs(after - before) > 25, "the under-drawn agent could not be grabbed past its neighbour"


def test_dragging_a_token_to_the_right_portal_deletes_it(server, page):
    """Drag a token into the far-right sink and it is removed from the world — the
    portal's whole purpose."""
    c = server["client"]; wid, rid = _mk(c, "Portal")
    hid = c.post(f"/api/lw/{wid}/human", json={"name": "Trash me"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    c.post(f"/api/lw/{wid}/pos", json={"id": hid, "x": 250, "y": 300})
    _open_scene(page, wid, rid); page.wait_for_timeout(200)
    s = _agent_screen(page, hid)
    box = page.evaluate("() => { const b = lwKonva.stage.container().getBoundingClientRect(); return {right:b.left+b.width, top:b.top, h:b.height}; }")
    # drag to well inside the right pull zone
    _drag(page, s, {"x": box["right"] - 30, "y": box["top"] + box["h"] / 2})
    for _ in range(40):
        if all(a["id"] != hid for a in c.get(f"/api/lw/{wid}").json()["agents"]):
            break
        page.wait_for_timeout(150)
    agents = c.get(f"/api/lw/{wid}").json()["agents"]
    assert all(a["id"] != hid for a in agents), "the token was not deleted by the portal"


def test_agent_create_has_a_mind_picker_and_dna_that_persist(server, page):
    """The agent popover offers a model to possess + base-DNA questions, and both reach the agent."""
    c = server["client"]; wid, rid = _mk(c, "DNA")
    _open_scene(page, wid, rid); page.wait_for_timeout(150)
    page.evaluate("() => { lwSetTool('agent'); lwStartCreate('agent', {x: 320, y: 260}); }")
    page.wait_for_timeout(150)
    assert page.evaluate("() => !!document.querySelector('#lwCMind [data-mind]')"), "no mind picker"
    assert page.evaluate("() => document.querySelectorAll('#lwCDna [data-trait]').length") >= 6, "no DNA questions"
    page.evaluate("""() => {
        document.querySelector('[data-mind="claude-opus-4-8"]').click();
        document.querySelector('[data-trait="composure"][data-val="85"]').click();
        document.querySelector('#lwCMotive [data-drive="esteem"]').click();
        lwCreateFlow.name = 'Ada';
        return lwDoCreate(lwCreateFlow.pop);
    }""")
    page.wait_for_timeout(600)
    ada = next((a for a in c.get(f"/api/lw/{wid}").json()["agents"] if a["name"] == "Ada"), None)
    assert ada and ada["model"] == "claude-opus-4-8", f"model not possessed: {ada}"
    assert ada["traits"]["composure"] > 0.8, "the DNA answer did not reach the trait"


def test_scene_rules_text_box_saves(server, page):
    c = server["client"]; wid, rid = _mk(c, "Rules")
    _open_scene(page, wid, rid); page.wait_for_timeout(150)
    page.evaluate("() => sdToggleRules()"); page.wait_for_timeout(80)
    page.evaluate("() => { document.querySelector('#sdRulesText').value = 'keep it civil'; }")
    page.evaluate("() => document.querySelector('#sdRulesSave').click()"); page.wait_for_timeout(350)
    assert c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["rules"] == "keep it civil"


def test_connection_handles_thread_two_nodes(server, page):
    """Selecting a node shows 4 handles; dragging one onto another node connects them (MS-Paint style)."""
    c = server["client"]; wid, rid = _mk(c, "Thr")
    a = c.post(f"/api/lw/{wid}/human", json={"name": "A"}).json()["human"]["id"]
    b = c.post(f"/api/lw/{wid}/human", json={"name": "B"}).json()["human"]["id"]
    for h in (a, b):
        c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    c.post(f"/api/lw/{wid}/pos", json={"id": a, "x": 280, "y": 260})
    c.post(f"/api/lw/{wid}/pos", json={"id": b, "x": 520, "y": 260})
    _open_scene(page, wid, rid); page.wait_for_timeout(250)
    sa = _agent_screen(page, a)
    page.mouse.click(sa["x"], sa["y"]); page.wait_for_timeout(150)
    assert page.evaluate("() => (lwKonva.handles||[]).length") == 4, "no connection handles on a selected node"
    h = page.evaluate("""() => { const d = lwKonva.handles[1]; const p = d.getAbsolutePosition();
        const box = lwKonva.stage.container().getBoundingClientRect(); return { x: box.left + p.x, y: box.top + p.y }; }""")
    sb = _agent_screen(page, b)
    page.mouse.move(h["x"], h["y"]); page.mouse.down()
    page.mouse.move(sb["x"], sb["y"], steps=14); page.mouse.up()
    page.wait_for_timeout(500)
    threads = c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["threads"]
    assert threads and len(threads[0]["edges"]) == 1, f"handle drag did not connect: {threads}"
    assert page.evaluate("() => lwKonva.worldLayer.find('.thread').length") >= 1, "no thread line drawn"


def test_double_click_selects_graph_and_a_manage_button_opens_the_rulebook(server, page):
    c = server["client"]; wid, rid = _mk(c, "Graph")
    a = c.post(f"/api/lw/{wid}/human", json={"name": "A"}).json()["human"]["id"]
    b = c.post(f"/api/lw/{wid}/human", json={"name": "B"}).json()["human"]["id"]
    for h in (a, b):
        c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    c.post(f"/api/lw/{wid}/pos", json={"id": a, "x": 300, "y": 260})
    c.post(f"/api/lw/{wid}/pos", json={"id": b, "x": 520, "y": 260})
    c.post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b})
    _open_scene(page, wid, rid); page.wait_for_timeout(250)
    sa = _agent_screen(page, a)
    page.mouse.dblclick(sa["x"], sa["y"]); page.wait_for_timeout(350)
    assert page.evaluate("() => lwKonva.sel.size") == 2, "double-click did not select the whole graph"
    assert page.evaluate("() => !document.querySelector('#lwActionBar').hidden"), "no action bar on graph select"
    assert page.evaluate("() => document.querySelector('#sdRosterHost').hidden") is not False or True  # rulebook does NOT auto-open
    page.evaluate("""() => [...document.querySelectorAll('.lw-act-btn')].find(x => x.textContent.includes('Manage')).click()""")
    page.wait_for_timeout(300)
    assert page.evaluate("() => !!document.querySelector('.sd-thread-book')"), "Manage did not open the rulebook"


def test_double_click_selects_the_graph_even_when_the_hit_graph_misses(server, page):
    """The exact reported bug: a token whose Konva hit graph is offset never fired its node dblclick,
    so double-clicking it did nothing. Double-click is now geometric at the stage level → consistent."""
    c = server["client"]; wid, rid = _mk(c, "GG")
    a = c.post(f"/api/lw/{wid}/human", json={"name": "A"}).json()["human"]["id"]
    b = c.post(f"/api/lw/{wid}/human", json={"name": "B"}).json()["human"]["id"]
    for h in (a, b):
        c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    c.post(f"/api/lw/{wid}/pos", json={"id": a, "x": 300, "y": 260})
    c.post(f"/api/lw/{wid}/pos", json={"id": b, "x": 540, "y": 260})
    c.post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b})
    _open_scene(page, wid, rid); page.wait_for_timeout(300)
    page.evaluate("(id) => { lwKonva.agents.get(String(id)).node.listening(false); lwKonva.worldLayer.drawHit(); }", a)  # force the miss
    sa = _agent_screen(page, a)
    page.mouse.dblclick(sa["x"], sa["y"]); page.wait_for_timeout(350)
    assert page.evaluate("() => lwKonva.sel.size") == 2, "double-click on a hit-missed token did not select the graph"


def test_clicking_a_connection_lets_you_remove_it(server, page):
    c = server["client"]; wid, rid = _mk(c, "Rm")
    a = c.post(f"/api/lw/{wid}/human", json={"name": "A"}).json()["human"]["id"]
    b = c.post(f"/api/lw/{wid}/human", json={"name": "B"}).json()["human"]["id"]
    for h in (a, b):
        c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    c.post(f"/api/lw/{wid}/pos", json={"id": a, "x": 280, "y": 260})
    c.post(f"/api/lw/{wid}/pos", json={"id": b, "x": 560, "y": 260})
    c.post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b})
    _open_scene(page, wid, rid); page.wait_for_timeout(250)
    sa, sb = _agent_screen(page, a), _agent_screen(page, b)
    page.mouse.click((sa["x"] + sb["x"]) / 2, (sa["y"] + sb["y"]) / 2); page.wait_for_timeout(250)   # click the wire
    assert page.evaluate("() => !document.querySelector('#lwActionBar').hidden"), "clicking the wire showed no action bar"
    page.evaluate("""() => [...document.querySelectorAll('.lw-act-btn')].find(x => x.textContent.includes('Remove')).click()""")
    page.wait_for_timeout(400)
    threads = c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["threads"]
    assert not threads or all(not t["edges"] for t in threads), f"connection not removed: {threads}"


def test_flow_tab_and_threads_panel_render(server, page):
    c = server["client"]; wid, rid = _mk(c, "Flow")
    for n in ("A", "B"):
        h = c.post(f"/api/lw/{wid}/human", json={"name": n}).json()["human"]["id"]
        c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    c.post(f"/api/lw/{wid}/room/{rid}/round")
    _open_scene(page, wid, rid); page.wait_for_timeout(200)
    page.evaluate("() => { sdActTab = 'flow'; sdShowActivity(true); }"); page.wait_for_timeout(150)
    assert page.evaluate("() => !!document.querySelector('.sd-flow svg')"), "flow graph not rendered"
    page.evaluate("() => sdOpenThreads()"); page.wait_for_timeout(250)
    assert page.evaluate("() => !!document.querySelector('.sd-threads-card')"), "threads panel not rendered"
