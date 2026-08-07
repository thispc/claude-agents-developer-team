"""The Atlas drill — a scripted user fiddling through canvas → HQ → the Atlas →
rooms → door chips → keyboard → the map → verbs → back out, printing FINDINGS for
anything that misbehaves. Dependency-light: playwright (in the venv) and stdlib only.

Run with the dev server up:  python tools/graph_experiment.py
Environment knobs:  BASE (default http://127.0.0.1:8787), USERNAME/PASSWORD
(default root/devteam), SKIP_REPLAN=1 to skip the one step that spends a real
model call. Exit code 1 when any FINDING was recorded.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8787")
USERNAME = os.environ.get("USERNAME", "root")
PASSWORD = os.environ.get("PASSWORD", "devteam")
SKIP_REPLAN = os.environ.get("SKIP_REPLAN", "") == "1"

FINDINGS: list[str] = []


def finding(msg: str) -> None:
    FINDINGS.append(msg)
    print(f"  FINDING: {msg}")


def ok(msg: str) -> None:
    print(f"  ok: {msg}")


def visible_screens(page) -> list[str]:
    return page.evaluate("""() => {
        const ids = ['#home', 'main', '#plan', '#studio', '#scenes', '#lifeworld',
                     '#aboutPage', '#selfPage', '#agentPage', '#graphScreen'];
        return ids.filter(s => { const e = document.querySelector(s);
                                 return e && !e.hidden; });
    }""")


def room_keys(page) -> list[str]:
    return page.evaluate(
        "[...document.querySelectorAll('#graphRoom .gr-card')]"
        ".map(c => c.getAttribute('data-key'))")


def card_center(page, key):
    return page.evaluate("""(k) => {
        const c = [...document.querySelectorAll('#graphRoom .gr-card')]
          .find(n => n.getAttribute('data-key') === k);
        if (!c) return null;
        const r = c.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + Math.min(24, r.height / 2)};
    }""", key)


def api(page, method: str, path: str, body: dict | None = None):
    """The page context's own cookies ride along — same session as the UI.
    The timeout is generous because the replan step IS a bounded model call."""
    req = page.context.request
    if method == "GET":
        r = req.get(BASE + path, timeout=30_000)
    else:
        r = req.post(BASE + path, data=json.dumps(body or {}),
                     headers={"Content-Type": "application/json"},
                     timeout=300_000)
    try:
        return r.status, r.json()
    except Exception:
        return r.status, {}


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: finding(f"page error: {e}"))

        print("== sign in")
        page.goto(BASE)
        page.fill('#loginForm input[name="username"]', USERNAME)
        page.fill('#loginForm input[name="password"]', PASSWORD)
        page.click('#loginForm button[type="submit"]')
        page.wait_for_selector("header:not([hidden])")
        ok("signed in")

        print("== canvas → HQ → the Atlas")
        page.evaluate("location.hash = '#/devteam'")   # the crew's canvas
        page.wait_for_timeout(900)
        page.evaluate("location.hash = '#/hq'")        # Devteam HQ
        page.wait_for_timeout(900)
        page.wait_for_selector("#graphLink:not([hidden])", timeout=8000)
        page.click("#graphLink")
        page.wait_for_selector("#graphRoom .gr-card.gr-chamber", timeout=10000)
        page.wait_for_timeout(1600)                    # the staged reveal + wire sweep
        if page.evaluate("location.hash") != "#/graph":
            finding(f"opening the Atlas landed on {page.evaluate('location.hash')}")
        ok("the Atlas open at #/graph")

        st, payload = api(page, "GET", "/api/graph/self")
        nodes = payload.get("nodes", [])
        leaves = [n for n in nodes
                  if n["node_type"] not in ("aim", "conclusion", "group")]
        groups = [n for n in nodes if n["node_type"] == "group"]
        parent = {n["key"]: n.get("parent_key") or "" for n in nodes}

        print("== the top tier cannot miss a crossing (the reconciliation invariant)")
        edges = payload.get("edges", [])
        pairs = {(e["src"], e["dst"]) for e in edges}
        missed = []
        for e in edges:
            ga, gb = parent.get(e["src"], ""), parent.get(e["dst"], "")
            if ga and gb and ga != gb and (ga, gb) not in pairs:
                missed.append(f"{e['src']}→{e['dst']} has no {ga}→{gb} above it")
        if missed:
            finding("the payload's group tier misses crossings: " + "; ".join(missed[:4]))
        else:
            ok("every child crossing has its group arrow in the payload")

        print("== the top ROOM: everything on screen, no camera to lose")
        fit = page.evaluate("""() => {
            const stage = document.querySelector('.graph-stage').getBoundingClientRect();
            const cards = [...document.querySelectorAll('#graphRoom .gr-card')];
            const out = cards.filter(c => {
                const r = c.getBoundingClientRect();
                return r.left < stage.left - 2 || r.right > stage.right + 2;
            }).map(c => c.getAttribute('data-key'));
            const st = document.querySelector('.graph-stage');
            return {out, hscroll: st.scrollWidth > st.clientWidth + 2,
                    n: cards.length};
        }""")
        if fit["out"]:
            finding(f"cards overflow the room horizontally: {fit['out']}")
        if fit["hscroll"]:
            finding("the room scrolls horizontally — it must always fit the viewport width")
        top_keys = set(room_keys(page))
        want_top = {n["key"] for n in nodes if n["node_type"] == "group"} \
            | {n["key"] for n in nodes if n["node_type"] == "aim"} \
            | ({n["key"] for n in leaves if not parent.get(n["key"])} if groups else
               {n["key"] for n in leaves})
        conclusion = next((n for n in nodes if n["node_type"] == "conclusion"), None)
        if conclusion:
            want_top.add(conclusion["key"])
        if top_keys != want_top:
            finding(f"top room shows {sorted(top_keys)}, wanted {sorted(want_top)}")
        else:
            ok(f"top room renders all {fit['n']} cards in-viewport")
        artifact_last = page.evaluate("""() => {
            const cols = [...document.querySelectorAll('#graphRoom .gr-col')];
            const last = cols[cols.length - 1];
            return !!(last && last.classList.contains('gr-col-artifact')
                      && last.querySelector('.gr-artifact'));
        }""")
        if conclusion and not artifact_last:
            finding("the Artifact is not the top room's last column card")
        wires = page.evaluate("document.querySelectorAll('#graphWires path.gr-wire').length")
        if edges and not wires:
            finding("no wires drawn in the top room despite in-room edges")
        else:
            ok(f"{wires} in-room wires drawn, the Artifact holds the last column")

        print("== every leaf shows its real tests (routes was the liar)")
        for n in leaves:
            if not n["tests"]["total"]:
                finding(f"leaf '{n['key']}' has no mapped suites in the payload")
        ok("leaf suite mapping checked")

        # The named rooms this drill walks: ops lives in one, db in another.
        ops = next((n for n in leaves if n["key"] == "ops"), None)
        db = next((n for n in leaves if n["key"] == "db"), None)
        gkey = (ops and parent.get("ops")) or (groups and groups[0]["key"]) or ""
        dkey = (db and parent.get("db")) or ""

        print(f"== enter a chamber (click = travel): {gkey}")
        hist0 = page.evaluate("history.length")
        box = card_center(page, gkey)
        if not box:
            finding(f"chamber card '{gkey}' not in the top room")
            browser.close()
            return 1
        page.mouse.click(box["x"], box["y"])
        page.wait_for_timeout(1200)                    # swap + stagger
        if page.evaluate("location.hash") != f"#/graph/{gkey}":
            finding(f"entering landed on {page.evaluate('location.hash')}, wanted #/graph/{gkey}")
        hist1 = page.evaluate("history.length")
        if hist1 - hist0 > 1:
            finding(f"one enter minted {hist1 - hist0} history entries (double hash write)")
        else:
            ok("one enter = one history entry")
        shown = sorted(room_keys(page))
        want = sorted(n["key"] for n in leaves if parent.get(n["key"]) == gkey)
        if shown != want:
            finding(f"room '{gkey}' shows {shown}, wanted exactly its children {want}")
        else:
            ok(f"room '{gkey}' shows exactly its {len(want)} children")

        lkey = (ops and ops["key"] in want and "ops") or (want[0] if want else None)
        print("== single click a capillary: ONE full panel, agent front and center")
        lbox = card_center(page, lkey)
        if not lbox:
            finding(f"capillary card '{lkey}' not in the room")
        else:
            page.mouse.click(lbox["x"], lbox["y"])
            page.wait_for_timeout(700)
            aside = page.inner_text("#graphAside")
            for needle, what in (("Tech stack", "Tech-stack section"),
                                 ("Test suite", "Test-suite section"),
                                 ("Agent", "Agent section"),
                                 ("Trace", "trace section"),
                                 ("Steering", "config section"),
                                 ("Edges", "edges section")):
                if needle not in aside:
                    finding(f"single-click panel is missing its {what}")
            has_agent = page.evaluate(
                "!!document.querySelector('#graphAside .gr-agent-row')")
            if not has_agent:
                finding("the panel shows no agent row at all")
            elif "Unassigned" in aside:
                if "Have the manager plan now" not in aside:
                    finding("unassigned panel lacks the [Have the manager plan now] button")
                else:
                    ok("panel: honest Unassigned + the replan button")
            else:
                ok("panel: an assigned specialist is shown")
            for bid, what in (("grRepStack", "Tech-stack Replace"),
                              ("grRepTests", "Test-suite Replace"),
                              ("grAgentPick", "agent picker [Change agent]")):
                if not page.evaluate(f"!!document.querySelector('#{bid}')"):
                    finding(f"panel is missing the {what} button")
            page.evaluate("document.getElementById('graphScreen').focus()")
            page.keyboard.press("Escape")              # close the panel, stay in the room
            page.wait_for_timeout(300)
            if page.evaluate("location.hash") != f"#/graph/{gkey}":
                finding("Esc with the panel open must close the panel, not leave the room")

        print("== the door chip that cannot lie: ops → Data › db")
        if not (ops and db and dkey):
            finding("payload has no ops→db shape to drill (ops/db/parents missing)")
        else:
            door = page.evaluate("""(args) => {
                const [okey, dkey] = args;
                const card = [...document.querySelectorAll('#graphRoom .gr-card')]
                  .find(c => c.getAttribute('data-key') === okey);
                if (!card) return {err: 'no ops card'};
                const doors = [...card.querySelectorAll('.gr-door')];
                const hit = doors.find(d => d.getAttribute('data-door') === dkey);
                if (!hit) return {err: 'no door to ' + dkey,
                                  have: doors.map(d => d.getAttribute('data-door'))};
                const r = hit.getBoundingClientRect();
                return {x: r.x + r.width / 2, y: r.y + r.height / 2,
                        label: hit.textContent.trim()};
            }""", [ops["key"], dkey])
            if door.get("err"):
                finding(f"ops card door chip: {door['err']} (have {door.get('have')})")
            else:
                ok(f"door chip on ops: “{door['label']}”")
                page.mouse.click(door["x"], door["y"])
                page.wait_for_timeout(900)
                if page.evaluate("location.hash") != f"#/graph/{dkey}":
                    finding(f"the door landed on {page.evaluate('location.hash')}, "
                            f"wanted #/graph/{dkey}")
                flashed = page.evaluate("""(k) => {
                    const c = [...document.querySelectorAll('#graphRoom .gr-card')]
                      .find(n => n.getAttribute('data-key') === k);
                    return c ? c.classList.contains('gr-flash') : null;
                }""", db["key"])
                if flashed is None:
                    finding(f"'{db['key']}' card is not in the {dkey} room after the door")
                elif not flashed:
                    finding(f"the door landed but '{db['key']}' did not flash-highlight")
                else:
                    ok(f"travelled to {dkey}, {db['key']} flash-highlighted")

        print("== keyboard: Esc climbs out, arrows + Enter walk back in")
        page.evaluate("document.getElementById('graphScreen').focus()")
        page.keyboard.press("Escape")
        page.wait_for_timeout(700)
        if page.evaluate("location.hash") != "#/graph":
            finding(f"Esc from a room landed on {page.evaluate('location.hash')}, wanted #/graph")
        target = None
        for _ in range(24):                            # walk the focus to a chamber
            focused = page.evaluate("""() => {
                const c = document.querySelector('#graphRoom .gr-card.gr-focus');
                return c ? [c.getAttribute('data-key'), c.getAttribute('data-kind')] : null;
            }""")
            if focused and focused[1] == "chamber":
                target = focused[0]
                break
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(60)
            focused2 = page.evaluate(
                "(document.querySelector('#graphRoom .gr-card.gr-focus')||{getAttribute:()=>null}).getAttribute && "
                "(document.querySelector('#graphRoom .gr-card.gr-focus')||{}).getAttribute('data-key')")
            if focused and focused2 == focused[0]:
                page.keyboard.press("ArrowDown")       # stuck at the right edge — drop a row
                page.wait_for_timeout(60)
        if not target:
            finding("arrow keys never reached a chamber card (no visible focus walk)")
        else:
            page.keyboard.press("Enter")
            page.wait_for_timeout(900)
            if page.evaluate("location.hash") != f"#/graph/{target}":
                finding(f"Enter on chamber '{target}' landed on {page.evaluate('location.hash')}")
            else:
                ok(f"Enter entered '{target}'")
            page.keyboard.press("Escape")
            page.wait_for_timeout(700)
            if page.evaluate("location.hash") != "#/graph":
                finding("Esc did not climb back out to #/graph")
            else:
                ok("Enter/Esc round-trip clean")

        print("== M opens the Atlas map")
        page.evaluate("document.getElementById('graphScreen').focus()")
        page.keyboard.press("m")
        page.wait_for_timeout(400)
        atlas_open = page.evaluate(
            "(e => !!e && !e.hidden)(document.querySelector('#graphAtlas'))")
        if not atlas_open:
            finding("M did not open the Atlas map overlay")
        else:
            rooms_n = page.evaluate(
                "document.querySelectorAll('#graphAtlas .gr-atlas-room').length")
            if rooms_n < len(groups) + 1:
                finding(f"the map shows {rooms_n} rooms, wanted {len(groups) + 1}")
            here = page.evaluate(
                "!!document.querySelector('#graphAtlas .gr-atlas-here')")
            if not here:
                finding("the map does not mark the current room")
            else:
                ok(f"the map: {rooms_n} rooms, current room marked")
            page.keyboard.press("m")
            page.wait_for_timeout(300)
            if page.evaluate(
                    "(e => !!e && !e.hidden)(document.querySelector('#graphAtlas'))"):
                finding("M did not close the Atlas map again")

        print("== right-click: the six verbs, and Test fires")
        page.evaluate("(h) => { location.hash = h; }", f"#/graph/{gkey}")
        page.wait_for_timeout(1100)
        lbox = card_center(page, lkey)
        if not lbox:
            finding(f"capillary '{lkey}' lost after re-entering {gkey}")
        else:
            page.mouse.click(lbox["x"], lbox["y"], button="right")
            page.wait_for_timeout(350)
            labels = page.evaluate(
                "[...document.querySelectorAll('.ctx-menu .ctx-item')]"
                ".map(b => b.textContent.trim())")
            for verb in ("Start", "Stop", "Peek", "Test", "Remove", "Replace"):
                if not any(str(lbl).startswith(verb) for lbl in labels):
                    finding(f"right-click menu is missing the {verb} verb (got {labels})")
            with page.expect_response("**/api/graph/self/verify",
                                      timeout=300_000) as rinfo:
                page.evaluate("""() => {
                    const b = [...document.querySelectorAll('.ctx-menu .ctx-item')]
                      .find(x => x.textContent.trim() === 'Test');
                    if (b) b.click();
                }""")
            if rinfo.value.status != 200:
                finding(f"the Test verb's verify POST returned {rinfo.value.status}")
            else:
                ok("Test verb fired the affected-only verify")
            page.wait_for_timeout(1800)             # the refetch repaints the ring
            ring = page.evaluate("""(k) => {
                const c = [...document.querySelectorAll('#graphRoom .gr-card')]
                  .find(n => n.getAttribute('data-key') === k);
                const r = c && c.querySelector('.gr-ring');
                return r ? (r.getAttribute('class') || '') : '';
            }""", lkey) or ""
            if not ring or "gr-ring-none" in ring:
                finding(f"after the Test verb the ring did not update ({ring!r})")
            else:
                ok(f"ring updated: {ring.split()[-1]}")

        print("== the team selector is honest about its backend")
        st_t, _ = api(page, "GET", "/api/graph/self/team")
        sel_visible = page.evaluate(
            "(e => !!e && !e.hidden)(document.querySelector('#graphTeam'))")
        if st_t == 200 and not sel_visible:
            finding("the team endpoint answers but the header selector is hidden")
        elif st_t != 200 and sel_visible:
            finding("the selector shows with no team endpoint behind it — a dead dropdown")
        else:
            ok(f"team selector matches its backend (endpoint {st_t}, "
               f"{'shown' if sel_visible else 'hidden'})")

        print("== steer: the config POST and its refusal")
        st, out = api(page, "POST", f"/api/graph/self/node/{lkey}/config",
                      {"model": "claude-haiku-4-5-20251001"})
        if st != 400:
            finding(f"dated model id gave {st}, expected a teaching 400")
        elif "valid:" not in str(out.get("detail", "")):
            finding(f"the 400 does not say the valid options: {out.get('detail')}")
        else:
            ok("dated id refused WITH the valid options in the detail")
        models = payload.get("models") or []
        if not models:
            finding("the graph payload carries no models list for the select")
        else:
            st, out = api(page, "POST", f"/api/graph/self/node/{lkey}/config",
                          {"model": models[0]})
            if st != 200:
                finding(f"a payload-listed model id was refused: {st} {out}")
            else:
                ok(f"config accepts the payload's own ids ({models[0]})")
            api(page, "POST", f"/api/graph/self/node/{lkey}/config", {"model": ""})

        if SKIP_REPLAN:
            print("== replan skipped (SKIP_REPLAN=1)")
        else:
            print("== replan: the manager authors the plan (spends ONE model call)")
            st, out = api(page, "POST", "/api/graph/self/replan")
            if st != 200:
                finding(f"replan returned {st}: {out.get('detail')}")
            elif (out.get("plan") or {}).get("authored_by") != "manager":
                finding(f"replan's plan is not manager-authored: {out}")
            else:
                st2, payload2 = api(page, "GET", "/api/graph/self")
                leaves2 = [n for n in payload2.get("nodes", [])
                           if n["node_type"] not in ("aim", "conclusion", "group")]
                bare = [n["key"] for n in leaves2 if not n.get("agent")]
                if bare:
                    finding(f"after replan these leaves have no agent: {bare}")
                else:
                    ok(f"replan landed — every one of the {len(leaves2)} leaves is staffed")
                target2 = next((n for n in leaves2 if n.get("agent")), None)
                if target2:
                    page.evaluate("(h) => { location.hash = h; }",
                                  f"#/graph/{target2['parent_key']}")
                    try:
                        page.wait_for_function(
                            "document.querySelectorAll('#graphRoom .gr-cagent-avatar').length > 0",
                            timeout=12_000)
                        chips = page.evaluate(
                            "document.querySelectorAll('#graphRoom .gr-cagent-avatar').length")
                        ok(f"{chips} agent chip(s) drawn on the capillary cards")
                    except Exception:
                        finding("no agent chips on the capillary cards after the replan")

        print("== the back-nav hammer")
        # wherever the drill left us, walk home the way a browser user does —
        # each back must MOVE (a back that leaves the hash unchanged is the bug)
        now = page.evaluate("location.hash")
        for _ in range(14):
            if now == "#/hq":
                break
            before = now
            page.go_back()
            page.wait_for_timeout(700)
            now = page.evaluate("location.hash")
            if now == before:
                finding(f"browser-back from {before} left the hash unchanged")
                break
        if now != "#/hq":
            finding(f"backing out of the Atlas landed on '{now}', wanted #/hq")
        vis = visible_screens(page)
        if vis != ["main"]:
            finding(f"at #/hq the visible screens are {vis}, wanted ['main'] only")
        else:
            ok("back-nav lands on #/hq with ONLY main visible")

        browser.close()

    print()
    if FINDINGS:
        print(f"{len(FINDINGS)} FINDING(S):")
        for f in FINDINGS:
            print(f"  - {f}")
        return 1
    print("no findings — the experiment is clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
