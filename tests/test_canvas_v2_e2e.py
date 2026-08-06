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


def _select_graph(page, screen_pt):
    """Select the whole graph a node belongs to, the way a person now does it.

    Double-click used to do this, which meant double-clicking an agent could never open that
    agent. Now: click the node, and the action bar offers to select the rest — the button
    exists because "marquee across it" is not a gesture anyone guesses.
    """
    page.mouse.click(screen_pt["x"], screen_pt["y"])
    page.wait_for_timeout(250)
    page.evaluate("""() => {
      const b = [...document.querySelectorAll('.lw-act-btn')]
        .find(x => x.textContent.includes('Select its graph'));
      if (!b) throw new Error('no "Select its graph" offered for a node in a graph');
      b.click();
    }""")
    page.wait_for_timeout(250)


def test_a_single_click_shows_five_facts_and_cannot_scroll(server, page):
    """The drawer's close button was not buggy — it sat inside the scrolling container and
    scrolled off the top. So the popup cannot scroll at all: five facts and overflow:hidden,
    which makes "no scrolling needed" structural rather than a promise about content."""
    wid, rid = _mk(server["client"])
    a, b = _two_connected(server["client"], wid, rid)
    _open(page, wid, rid)
    pa = _tpos(page, a); sa = _scr(page, pa["x"], pa["y"])
    page.mouse.click(sa["x"], sa["y"]); page.wait_for_timeout(500)
    assert page.evaluate("() => { const e = document.querySelector('#lwPeek'); return !!e && !e.hidden; }"), \
        "a single click showed no popup"
    box = page.evaluate("""() => { const e = document.querySelector('#lwPeek');
        return [e.scrollHeight - e.clientHeight, getComputedStyle(e).overflow]; }""")
    assert box[0] <= 1, f"the popup needs scrolling ({box[0]}px of overflow)"
    assert box[1] == "hidden", f"overflow is {box[1]} — the guarantee has to be structural"
    # ...and its close is reachable, always: Escape, and a ✕ that cannot scroll away
    page.keyboard.press("Escape"); page.wait_for_timeout(250)
    assert page.evaluate("() => document.querySelector('#lwPeek').hidden"), "Escape did not close it"


def test_v2_double_click_opens_the_agent_page(server, page):
    """Root's call: double-click is unambiguously "show me this one". A gesture that means
    two different things depending on invisible state is one people stop trusting."""
    wid, rid = _mk(server["client"])
    a, b = _two_connected(server["client"], wid, rid)
    server["client"].post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b})
    _open(page, wid, rid)
    pa = _tpos(page, a)
    sa = _scr(page, pa["x"], pa["y"])
    page.mouse.dblclick(sa["x"], sa["y"]); page.wait_for_timeout(900)
    # It opens the agent's own PAGE now, not a 340px drawer with the decision graph squeezed
    # into a 340px keyhole behind two nested scrollbars.
    assert page.evaluate("() => !document.querySelector('#agentPage').hidden"), \
        "double-clicking an agent did not open its page"
    assert "#/agent/" in page.evaluate("() => location.hash"), "the page has no address"
    assert page.evaluate("() => document.querySelectorAll('[data-agtab]').length") == 5


def test_v2_select_graph_then_wire_and_collapse(server, page):
    wid, rid = _mk(server["client"])
    a, b = _two_connected(server["client"], wid, rid)
    server["client"].post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b})
    _open(page, wid, rid)
    pa = _tpos(page, a); pb = _tpos(page, b)
    sa = _scr(page, pa["x"], pa["y"]); sb = _scr(page, pb["x"], pb["y"])
    _select_graph(page, sa)
    assert _sel(page) == 2, "the graph was not selected"
    mid = _scr(page, (pa["x"] + pb["x"]) / 2, (pa["y"] + pb["y"]) / 2)
    page.mouse.click(mid["x"], mid["y"]); page.wait_for_timeout(250)
    assert page.evaluate("() => !!window.LWCanvas2._inst.selEdge"), "single-click on the wire did not select the edge"
    page.mouse.click(sb["x"], sb["y"]); page.wait_for_timeout(200)
    assert _sel(page) == 1, "clicking a member did not collapse to just it"


def test_v2_grouped_selection_click_resolves_via_real_hit_test(server, page):
    """Real-user probe (real mouse, DPR2 — this module's `page` fixture runs device_scale_factor=2):
    with a graph grouped-selected, a click on one member must resolve to THAT token via the
    browser's own compositor (document.elementFromPoint), not an occluding overlay shape (the
    connection handles a single-selection would show sit in the same .lw2-overlay layer that
    painted over tokens in v1's Konva hit graph). Guards the shared hit-test-utils.js helper
    that both the press/select path and the context-menu path now call."""
    wid, rid = _mk(server["client"])
    a, b = _two_connected(server["client"], wid, rid)
    server["client"].post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b})
    _open(page, wid, rid)
    pa = _tpos(page, a); pb = _tpos(page, b)
    sa = _scr(page, pa["x"], pa["y"]); sb = _scr(page, pb["x"], pb["y"])
    _select_graph(page, sa)
    assert _sel(page) == 2, "the graph was not selected"
    # the real-user probe: what does the browser itself resolve at B's screen point, right now?
    hit = page.evaluate(
        "([x, y]) => { const el = document.elementFromPoint(x, y); const t = el && el.closest && el.closest('.lw2-token'); return t ? t.getAttribute('data-id') : null; }",
        [sb["x"], sb["y"]],
    )
    assert hit == str(b), f"elementFromPoint at B's token resolved to {hit!r}, not B — an overlay shape may be occluding it"
    # a real mouse click at that same point must land on B too (same lookup the click handler
    # uses) — the group stays grouped (a plain click on an already-selected member doesn't
    # shrink it), but B must still resolve as hit and stay selected, not fall through to
    # "empty floor" (which would clear the selection entirely).
    page.mouse.click(sb["x"], sb["y"]); page.wait_for_timeout(250)
    assert _sel(page) == 2, "the click on B was not recognized as a token press (selection changed unexpectedly)"
    assert page.evaluate("(id) => document.querySelector(`.lw2-token[data-id=\"${id}\"]`).classList.contains('lw2-sel')", b), \
        "the resolved token did not remain selected"


def test_v2_shows_a_mediated_conversation(server, page):
    """Step a round on a connected thread → the hidden manager mediates and each agent's line
    arrives in the CONVERSATION panel, and the canvas shows only who is working. The whole
    round is free/deterministic offline."""
    c = server["client"]; wid, rid = _mk(c, "Talk")
    a, b = _two_connected(c, wid, rid)
    th = c.post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b}).json()["thread"]
    c.post(f"/api/lw/{wid}/room/{rid}/thread/{th['id']}",
           json={"rulebook": "debate the most sustainable route from A to B", "manager": {"budget": 2}})
    _open(page, wid, rid)
    page.evaluate("([wid,rid]) => fetch(`/api/lw/${wid}/room/${rid}/round`, {method:'POST'}).then(r=>r.json()).then(j=>lwRenderRoom(j.room))", [wid, rid])
    page.wait_for_timeout(1200)
    said = c.get(f"/api/lw/{wid}/room/{rid}").json()["room"]["log"]
    lines = [b["text"] for b in said if b["kind"] == "say"]
    assert len(lines) >= 2, f"expected a line per speaker, got {lines}"
    assert all("route" in t.lower() for t in lines), f"lines not grounded in the topic: {lines}"
    # ...and the canvas is NOT plastered with them: a bubble is reserved for the agent that
    # is working right now, so the picture answers "who is busy" instead of repeating the
    # transcript the panel already holds, scrollable and quotable.
    idle_bubbles = page.evaluate("() => document.querySelectorAll('.lw2-bubble').length")
    assert idle_bubbles == 0, f"speech was popped onto the canvas again ({idle_bubbles} bubbles)"


def test_v2_run_produces_a_decision_memo_card(server, page):
    """The product loop, without a Run button: you TALK to the graph and it deliberates,
    because asking is the act. The decision memo appears with a position per agent."""
    c = server["client"]; wid, rid = _mk(c, "Memo")
    a, b = _two_connected(c, wid, rid)
    th = c.post(f"/api/lw/{wid}/room/{rid}/thread/connect", json={"a": a, "b": b}).json()["thread"]
    c.post(f"/api/lw/{wid}/room/{rid}/thread/{th['id']}",
           json={"rulebook": "debate the most sustainable route from A to B", "manager": {"budget": 2}})
    _open(page, wid, rid)
    p = _tpos(page, a); s = _scr(page, p["x"], p["y"])
    _select_graph(page, s)
    # Run and Chat are gone from the bar: running is what happens when you speak, and the
    # conversation is always on the right. A second way to do one thing is one too many.
    labels = page.evaluate("() => [...document.querySelectorAll('.lw-act-btn')].map(b => b.textContent)")
    assert not any("Run" in t for t in labels), f"a Run button came back: {labels}"
    assert not any("Chat" in t for t in labels), f"a Chat button came back: {labels}"
    # Asking the MANAGER is the deliberation: "decide X" is not a remark, it is the product
    # loop, and cost stays proportional to intent (one agent → one round). Selecting the graph
    # aimed the panel at an agent, so address the room again first — which is what clicking
    # empty floor means.
    page.evaluate("() => { const i = window.LWCanvas2._inst; i.sel.clear(); if (window.sdChatAim) window.sdChatAim(null, 0); }")
    page.fill("#sdChatText", "decide the route and write it up")
    page.click("#sdChatSend")
    page.wait_for_selector(".sd-memo-card", timeout=15000)
    assert page.evaluate("() => document.querySelectorAll('.sd-memo-pos').length") == 2, "expected a final position per agent"
    assert page.evaluate("() => document.querySelector('.sd-memo-rec').textContent.length > 20"), "no recommendation in the memo"


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
