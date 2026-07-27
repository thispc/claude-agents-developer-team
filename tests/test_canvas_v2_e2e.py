"""Canvas v2 (SVG/DOM, native hit-testing) driven by a REAL browser with CANVAS_V2=1.

v2 exists because every pixel-canvas engine (Konva/Pixi/Fabric) keeps a SEPARATE hit model
that desyncs from the visible one at devicePixelRatio 2 / panned / negative world-y — the
root of the "can't grab / can't select" bugs. v2 renders tokens and wires as real SVG nodes,
so the browser's own compositor does hit-testing (e.target) and it cannot desync. These tests
reproduce the exact regime that broke v1 (negative-y, DPR 2) and prove the core gestures.

Boots its own conductor on a temp DB with CANVAS_V2=1. SKIPS cleanly without Playwright/chromium.
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
PORT = int(os.environ.get("CANVAS_V2_E2E_PORT", "8145"))
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


pytestmark = pytest.mark.skipif(not _browser_ok(), reason="chromium not installed")


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
    tmp = tempfile.mkdtemp(prefix="devteam-v2-e2e-")
    env = dict(os.environ)
    env.update({
        "DB_PATH": str(Path(tmp) / "c.db"), "WORKSPACES_DIR": str(Path(tmp) / "ws"),
        "PREVIEW_DIR": str(Path(tmp) / "pv"), "DEPLOY_DIR": str(Path(tmp) / "dp"),
        "ROOT_USERNAME": "root", "ROOT_PASSWORD": "rootpass", "WORKER_TOKEN": "wt",
        "LAUNCHER": "local", "PYTHONPATH": str(REPO / "conductor"),
        "ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": "", "GITHUB_TOKEN": "",
        "CANVAS_V2": "1",
    })
    log = open(Path(tmp) / "s.log", "w")
    proc = subprocess.Popen([str(REPO / ".venv/bin/uvicorn"), "app.main:app", "--host", "0.0.0.0", "--port", str(PORT)],
                            cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    assert _wait_port(PORT), "conductor did not start"
    time.sleep(1)
    import httpx
    c = httpx.Client(base_url=BASE, timeout=30)
    c.post("/api/login", json={"username": "root", "password": "rootpass"})
    yield {"client": c, "tmp": tmp}
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def page(server):
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # DPR 2 — the exact regime the Konva hit graph desynced under.
        ctx = browser.new_context(viewport={"width": 1512, "height": 806}, device_scale_factor=2)
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg._lw_errs = errs
        pg.goto(BASE)
        pg.wait_for_selector("#loginScreen", state="visible", timeout=15000)
        pg.fill("#loginForm input[name=username]", "root")
        pg.fill("#loginForm input[name=password]", "rootpass")
        pg.click("#loginForm button[type=submit]")
        pg.wait_for_selector("#loginScreen", state="hidden", timeout=15000)
        pg.wait_for_function("() => !!window.LWCanvas2", timeout=8000)
        yield pg
        browser.close()


def _mk(c, name="V2"):
    wid = c.post("/api/lw", json={"name": name}).json()["world"]["id"]
    rid = c.post(f"/api/lw/{wid}/room", json={"name": "untitled", "type": "freeplay"}).json()["room"]["id"]
    return wid, rid


def _open(page, wid, rid):
    page.evaluate("""() => { for (const id of ['#sdRosterHost','#sdScenesMenu']) { const e=document.querySelector(id); if(e) e.hidden=true; } showLifeworld(); }""")
    page.evaluate("([wid, rid]) => { lwWorldId = wid; return lwOpenScene(rid); }", [wid, rid])
    page.wait_for_function("() => window.LWCanvas2._inst && window.LWCanvas2._inst.tokens.size>0", timeout=8000)
    page.wait_for_timeout(250)


def _scr(page, wx, wy):
    return page.evaluate("([x, y]) => window.LWCanvas2._inst.world.toScreen(x, y)", [wx, wy])


def _tpos(page, tid):
    return page.evaluate("(id) => { const t = window.LWCanvas2._inst.tokens.get(String(id)); return { x: t.x, y: t.y }; }", tid)


def _sel(page):
    return page.evaluate("() => window.LWCanvas2._inst.sel.size")


def _two_connected(c, wid, rid, ya=-577, yb=-560):
    a = c.post(f"/api/lw/{wid}/human", json={"name": "Harvey"}).json()["human"]["id"]
    b = c.post(f"/api/lw/{wid}/human", json={"name": "Mike"}).json()["human"]["id"]
    for h in (a, b):
        c.post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    c.post(f"/api/lw/{wid}/pos", json={"id": a, "x": 355, "y": ya})
    c.post(f"/api/lw/{wid}/pos", json={"id": b, "x": 640, "y": yb})
    return a, b


# --- tests -----------------------------------------------------------------

def test_v2_flag_is_served_and_mounts_svg(server, page):
    assert server["client"].get("/api/me").json().get("canvas_v2") is True
    wid, rid = _mk(server["client"])
    a, _ = _two_connected(server["client"], wid, rid)
    _open(page, wid, rid)
    assert page.evaluate("() => !!document.querySelector('.lw2-host')"), "v2 host not mounted"
    assert page.evaluate("() => document.querySelectorAll('.lw2-token').length") == 2
    assert page.evaluate("() => typeof Konva === 'undefined' || !document.querySelector('#lwKonvaHost canvas')"), "a Konva canvas is present under v2"
    assert not page._lw_errs, f"page errors: {page._lw_errs}"


def test_v2_click_selects_and_stays(server, page):
    wid, rid = _mk(server["client"])
    a, _ = _two_connected(server["client"], wid, rid)
    _open(page, wid, rid)
    p = _tpos(page, a); s = _scr(page, p["x"], p["y"])
    page.mouse.move(s["x"], s["y"]); page.mouse.down(); page.mouse.up()
    page.wait_for_timeout(350)
    assert _sel(page) == 1, "click did not leave the token selected"
    assert page.evaluate("(id)=>document.querySelector(`.lw2-token[data-id=\"${id}\"]`).classList.contains('lw2-sel')", a)


def test_v2_handle_drag_connects(server, page):
    wid, rid = _mk(server["client"])
    a = server["client"].post(f"/api/lw/{wid}/human", json={"name": "A"}).json()["human"]["id"]
    b = server["client"].post(f"/api/lw/{wid}/human", json={"name": "B"}).json()["human"]["id"]
    for h in (a, b):
        server["client"].post(f"/api/lw/{wid}/room/{rid}/seat", params={"human_id": h})
    server["client"].post(f"/api/lw/{wid}/pos", json={"id": a, "x": 355, "y": -577})
    server["client"].post(f"/api/lw/{wid}/pos", json={"id": b, "x": 640, "y": -560})
    _open(page, wid, rid)
    pa = _tpos(page, a); pb = _tpos(page, b)
    sa = _scr(page, pa["x"], pa["y"]); sb = _scr(page, pb["x"], pb["y"])
    page.mouse.click(sa["x"], sa["y"]); page.wait_for_timeout(150)   # select A → handles appear
    reach = page.evaluate("(id)=>{const t=window.LWCanvas2._inst.tokens.get(String(id));return (t.kind==='agent'?36:34)+10;}", a)
    h = _scr(page, pa["x"] + reach, pa["y"])
    page.mouse.move(h["x"], h["y"]); page.mouse.down()
    page.mouse.move(sb["x"], sb["y"], steps=12); page.mouse.up(); page.wait_for_timeout(600)
    assert page.evaluate("() => ((lwRoom && lwRoom.threads) || []).length") >= 1, "handle-drag did not connect"


def test_v2_double_click_graph_and_wire_and_collapse(server, page):
    wid, rid = _mk(server["client"])
    a, b = _two_connected(server["client"], wid, rid)
    server["client"].post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b})
    _open(page, wid, rid)
    pa = _tpos(page, a); pb = _tpos(page, b)
    sa = _scr(page, pa["x"], pa["y"]); sb = _scr(page, pb["x"], pb["y"])
    page.mouse.dblclick(sa["x"], sa["y"]); page.wait_for_timeout(300)
    assert _sel(page) == 2, "double-click did not select the whole graph"
    mid = _scr(page, (pa["x"] + pb["x"]) / 2, (pa["y"] + pb["y"]) / 2)
    page.mouse.click(mid["x"], mid["y"]); page.wait_for_timeout(250)
    assert page.evaluate("() => !!window.LWCanvas2._inst.selEdge"), "single-click on the wire did not select the edge"
    page.mouse.click(sb["x"], sb["y"]); page.wait_for_timeout(200)
    assert _sel(page) == 1, "clicking a member did not collapse to just it"


def test_v2_shows_a_mediated_conversation(server, page):
    """Step a round on a connected thread → the hidden manager mediates and each agent's line
    appears as a speech bubble on the canvas. The whole round is free/deterministic offline."""
    c = server["client"]; wid, rid = _mk(c, "Talk")
    a, b = _two_connected(c, wid, rid)
    th = c.post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b}).json()["thread"]
    c.post(f"/api/lw/{wid}/room/{rid}/thread/{th['id']}",
           json={"rulebook": "debate the most sustainable route from A to B", "manager": {"budget": 2}})
    _open(page, wid, rid)
    # step a round the same way the time-bar does, then let v2 re-render from the new room
    page.evaluate("([wid,rid]) => fetch(`/api/lw/${wid}/room/${rid}/round`, {method:'POST'}).then(r=>r.json()).then(j=>lwRenderRoom(j.room))", [wid, rid])
    page.wait_for_function("() => window.LWCanvas2._inst && document.querySelectorAll('.lw2-bubble').length >= 2", timeout=8000)
    bubbles = page.evaluate("() => [...document.querySelectorAll('.lw2-bubble')].map(b => b.textContent)")
    assert len(bubbles) >= 2, f"expected a speech bubble per speaker, got {bubbles}"
    assert all("route" in t.lower() for t in bubbles), f"bubbles not grounded in the topic: {bubbles}"


def test_v2_drag_moves_and_persists(server, page):
    wid, rid = _mk(server["client"])
    a, _ = _two_connected(server["client"], wid, rid)
    _open(page, wid, rid)
    before = _tpos(page, a); s = _scr(page, before["x"], before["y"])
    page.mouse.move(s["x"], s["y"]); page.mouse.down()
    page.mouse.move(s["x"] - 110, s["y"] + 80, steps=10); page.mouse.up(); page.wait_for_timeout(500)
    after = _tpos(page, a)
    assert abs(after["x"] - before["x"]) > 40, "drag did not move the token"
    # persistence: re-fetch the room from the server and re-mount — the token stays where it was dropped
    page.evaluate("() => lwReloadRoom && lwReloadRoom()")
    page.wait_for_function("() => window.LWCanvas2._inst && window.LWCanvas2._inst.tokens.size>0", timeout=8000)
    page.wait_for_timeout(200)
    reloaded = _tpos(page, a)
    assert abs(reloaded["x"] - after["x"]) < 3, "the moved position did not survive a server round-trip"
