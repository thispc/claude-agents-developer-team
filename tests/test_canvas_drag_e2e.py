"""The Lifeworld canvas, driven by a REAL browser — because dragging can only be
proven in one. Konva renders to a <canvas>, so there is no DOM node to assert on;
these tests move the actual mouse over the actual pixels and check that a token
moved, seated into a slot, and that nothing invisible is sitting on top of the
floor eating the pointer (the failure mode that made "I can't move a thing" real).

Self-contained: it boots its own conductor on a temp DB (no creds, LAUNCHER=local,
free deterministic agents) and logs in through the real form. It SKIPS cleanly if
Playwright or its chromium build isn't installed, so it never breaks a headless CI.

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

def _open_room(page, wid, rid):
    page.evaluate("(wid) => { location.hash = '#/lifeworld/' + wid; }", wid)
    page.wait_for_timeout(700)
    page.evaluate("(rid) => openRoom(rid)", rid)
    for _ in range(40):
        if page.evaluate("() => (typeof lwKonva!=='undefined') && !!lwKonva && lwKonva.agents.size + lwKonva.props.size > 0"):
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

def test_the_canvas_is_the_topmost_thing_nothing_invisible_covers_it(server, page):
    """The bug that made dragging impossible: an invisible element over the floor eats
    every pointer event. Assert the element under the canvas centre IS the canvas."""
    c = server["client"]
    wid = c.post("/api/lw", json={"name": "Cover"}).json()["world"]["id"]
    rid = c.post(f"/api/lw/{wid}/room", json={"name": "R", "type": "freeplay"}).json()["room"]["id"]
    hid = c.post(f"/api/lw/{wid}/human", json={"name": "A", "figure": "av:a"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    c.post(f"/api/lw/{wid}/pos", json={"id": hid, "x": 300, "y": 220})
    _open_room(page, wid, rid)
    s = _agent_screen(page, hid)
    tag = page.evaluate("([x,y]) => { const el = document.elementFromPoint(x,y); return el ? el.tagName : null; }", [s["x"], s["y"]])
    assert tag == "CANVAS", f"something is covering the floor at the token: <{tag}> (drag would be dead)"


def test_an_agent_drags_and_the_move_persists(server, page):
    c = server["client"]
    wid = c.post("/api/lw", json={"name": "Drag"}).json()["world"]["id"]
    rid = c.post(f"/api/lw/{wid}/room", json={"name": "R", "type": "freeplay"}).json()["room"]["id"]
    hid = c.post(f"/api/lw/{wid}/human", json={"name": "Mover", "figure": "av:b"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    c.post(f"/api/lw/{wid}/pos", json={"id": hid, "x": 300, "y": 220})
    _open_room(page, wid, rid)
    s = _agent_screen(page, hid)
    _drag(page, s, {"x": s["x"] + 150, "y": s["y"] + 90})
    after = page.evaluate("(id)=>{const e=lwKonva.agents.get(String(id)); return {x:e.node.x(), y:e.node.y()};}", hid)
    assert abs(after["x"] - 300) > 40 and abs(after["y"] - 220) > 20, f"token did not move on canvas: {after}"
    pos = c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["agents"][0]["pos"]
    assert abs(pos[0] - after["x"]) < 2 and abs(pos[1] - after["y"]) < 2, f"move did not persist: {pos} vs {after}"


def test_dragging_an_agent_onto_a_slot_seats_it(server, page):
    """Collation: drop an agent near a table's free socket and it magnetically seats."""
    c = server["client"]
    wid = c.post("/api/lw", json={"name": "Seat"}).json()["world"]["id"]
    rid = c.post(f"/api/lw/{wid}/room", json={"name": "R", "type": "freeplay"}).json()["room"]["id"]
    table = c.post(f"/api/lw/{wid}/artifact", json={"name": "table", "brief": "a round table", "slots": 4}).json()["artifact"]
    c.post(f"/api/lw/{wid}/room/{rid}/place", params={"artifact_id": table["id"]})
    c.post(f"/api/lw/{wid}/pos", json={"id": table["id"], "x": 500, "y": 300})
    hid = c.post(f"/api/lw/{wid}/human", json={"name": "Sitter", "figure": "av:c"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    c.post(f"/api/lw/{wid}/pos", json={"id": hid, "x": 150, "y": 150})
    _open_room(page, wid, rid)
    a, slot = _agent_screen(page, hid), _slot_screen(page, table["id"], 0)
    _drag(page, a, slot)
    seated = c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["props"][0]["seated"]
    assert hid in seated, f"agent did not seat via drag: seated={seated}"


def test_dragging_a_full_table_carries_its_seated_agents(server, page):
    c = server["client"]
    wid = c.post("/api/lw", json={"name": "Carry"}).json()["world"]["id"]
    rid = c.post(f"/api/lw/{wid}/room", json={"name": "R", "type": "freeplay"}).json()["room"]["id"]
    table = c.post(f"/api/lw/{wid}/artifact", json={"name": "t", "brief": "a round table", "slots": 3}).json()["artifact"]
    c.post(f"/api/lw/{wid}/room/{rid}/place", params={"artifact_id": table["id"]})
    c.post(f"/api/lw/{wid}/pos", json={"id": table["id"], "x": 400, "y": 300})
    hid = c.post(f"/api/lw/{wid}/human", json={"name": "S", "figure": "av:d"}).json()["human"]["id"]
    c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": hid})
    c.post(f"/api/lw/{wid}/artifact/{table['id']}/seat", json={"slot": 0, "human_id": hid})
    _open_room(page, wid, rid)
    before = page.evaluate("(id)=>{const e=lwKonva.agents.get(String(id)); return {x:e.node.x(), y:e.node.y()};}", hid)
    t = page.evaluate("""(pid) => { const e = lwKonva.props.get(String(pid)); const p = e.node.getAbsolutePosition();
        const box = lwKonva.stage.container().getBoundingClientRect(); return { x: box.left+p.x, y: box.top+p.y }; }""", table["id"])
    _drag(page, t, {"x": t["x"] - 160, "y": t["y"] + 40})
    after = page.evaluate("(id)=>{const e=lwKonva.agents.get(String(id)); return {x:e.node.x(), y:e.node.y()};}", hid)
    assert abs(after["x"] - before["x"]) > 40, f"seated agent did not ride with the table: {before} -> {after}"
