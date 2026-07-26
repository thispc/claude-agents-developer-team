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
    page.evaluate("() => showLifeworld()")
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
