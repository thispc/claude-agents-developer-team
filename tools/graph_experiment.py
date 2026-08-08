"""The Atlas drill — a scripted user fiddling through canvas → HQ → the Atlas →
rooms → door chips → keyboard → the map → verbs → THE START/STOP ROUND TRIP → back
out, printing FINDINGS for anything that misbehaves. Dependency-light: playwright
(in the venv) and stdlib only.

SINCE P6 THE CARDS ARE SERVICES, and the headline step is the one that could not
exist before: it STOPS a real process through the Atlas, watches the fleet manager
report it stopped and the card go red, STARTS it again, and watches it come back
Ready and green — while checking, in the middle, that the platform serving this
very drill is unaffected. A stop button that pretends is worse than no stop button,
and this is the only way to find out which one shipped.

Run with the fleet up:  ./run-local.sh  then  python tools/graph_experiment.py
Environment knobs:  BASE (default http://127.0.0.1:8787), USERNAME/PASSWORD
(default root/devteam), SKIP_REPLAN=1 to skip the one step that spends a real
model call, SKIP_FLEET=1 to skip the start/stop round trip (it needs the fleet
manager — the `--legacy` boot has none). Exit code 1 when any FINDING was recorded.
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
SKIP_FLEET = os.environ.get("SKIP_FLEET", "") == "1"
# The card the round trip stops. `notify` is the safest process in the fleet to
# take away for four seconds: nothing blocks on it, its shim degrades to
# {"sent": false} and the only thing lost while it is down is a GitHub issue
# nobody filed. Never the conductor (it serves this drill) and never modgraph
# (the Atlas would go blind mid-drill and prove nothing).
ROUND_TRIP_CARD = os.environ.get("ROUND_TRIP_CARD", "notify")

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

        print("== every card is a SERVICE, with its own suite and its own switch")
        EPHEMERAL = ("worker", "app")
        for n in nodes:
            if n["node_type"] in ("aim", "conclusion"):
                continue
            svc = n.get("service") or {}
            if not svc.get("kind"):
                finding(f"card '{n['key']}' says nothing about what it is")
                continue
            if svc["kind"] in EPHEMERAL:
                continue                      # a live process, not a plan row
            if not n["tests"]["total"]:
                finding(f"card '{n['key']}' has no mapped suite in the payload")
            if svc.get("control") is False and not svc.get("reason"):
                finding(f"card '{n['key']}' refuses control without saying why")
            if svc.get("state") in (None, ""):
                finding(f"card '{n['key']}' reports no state at all")
        # the code-module seed must stay deleted
        for gone in ("routes", "guards", "db", "dash-core", "canvas", "backend"):
            if gone in {n["key"] for n in nodes}:
                finding(f"a CODE MODULE is on the wall again: '{gone}'")
        fleet_line = (payload.get("conclusion") or {}).get("fleet") or {}
        if not fleet_line:
            finding("the Artifact carries no fleet line")
        elif fleet_line.get("visible") and fleet_line.get("down"):
            finding(f"the fleet reports these down: {fleet_line['down']}")
        else:
            ok(f"{len(nodes)} cards, all services; fleet {fleet_line.get('running')}/"
               f"{fleet_line.get('declared')} up")

        # The rooms this drill walks. Since P6 the top room is FLAT — the only
        # chambers are the two registry containers that hold a live list — so the
        # walk enters whichever of them has something inside it, and falls back to
        # the first chamber when the box is idle (an empty room is a real state and
        # the Atlas bounces out of it, which is itself worth exercising).
        by_key = {n["key"]: n for n in nodes}
        occupied = [g for g in groups
                    if any(parent.get(n["key"]) == g["key"] for n in nodes)]
        gkey = (occupied or groups or [{"key": ""}])[0]["key"]
        dkey = ""

        want = sorted(n["key"] for n in nodes if parent.get(n["key"]) == gkey)
        entered = False
        if not want:
            ok(f"room '{gkey}' is empty right now (nothing is running in it) — "
               "the Atlas bounces back to the top room, which is the honest answer")
        else:
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
                finding(f"entering landed on {page.evaluate('location.hash')}, "
                        f"wanted #/graph/{gkey}")
            hist1 = page.evaluate("history.length")
            if hist1 - hist0 > 1:
                finding(f"one enter minted {hist1 - hist0} history entries "
                        "(double hash write)")
            else:
                ok("one enter = one history entry")
            shown = sorted(room_keys(page))
            if shown != want:
                finding(f"room '{gkey}' shows {shown}, wanted exactly its children {want}")
            else:
                ok(f"room '{gkey}' shows exactly its {len(want)} children")
            entered = True

        # The panel drill runs on a SERVICE card, in whichever room we are in.
        lkey = want[0] if want else next(
            (n["key"] for n in leaves
             if (n.get("service") or {}).get("kind") == "service"), None)
        print("== single click a capillary: ONE full panel, the black box closed")
        lbox = card_center(page, lkey)
        if not lbox:
            finding(f"capillary card '{lkey}' not in the room")
        else:
            page.mouse.click(lbox["x"], lbox["y"])
            page.wait_for_timeout(700)
            aside = page.inner_text("#graphAside")
            for needle, what in (("Contract", "Contract section"),
                                 ("Health", "Health & switch section"),
                                 ("Test suite", "Test-suite section"),
                                 ("Agent", "Agent section"),
                                 ("Logs", "Logs section"),
                                 ("Trace", "trace section"),
                                 ("Steering", "config section"),
                                 ("Edges", "edges section")):
                if needle not in aside:
                    finding(f"single-click panel is missing its {what}")
            # THE OWNER'S DECREE, checked on the rendered text: the panel must not
            # show one file, module or line of what is inside a service.
            import re as _re
            leaks = _re.findall(r"[\w./-]+\.(?:py|js|css|html)\b", aside)
            if leaks:
                finding(f"the panel leaked file paths: {sorted(set(leaks))[:4]}")
            else:
                ok("panel: no file path anywhere in it — the box stays closed")
            if not page.evaluate("!!document.querySelector('#graphAside .gr-endpoint')") \
                    and "serves no contract" not in aside:
                finding("the panel shows neither a contract nor a reason it has none")
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
            for bid, what in (("grRepStack", "Contract Replace"),
                              ("grRepTests", "Test-suite Replace"),
                              ("grAgentPick", "agent picker [Change agent]"),
                              ("grSecHealth", "Health & switch section")):
                if not page.evaluate(f"!!document.querySelector('#{bid}')"):
                    finding(f"panel is missing the {what} button")
            page.evaluate("document.getElementById('graphScreen').focus()")
            page.keyboard.press("Escape")              # close the panel, stay in the room
            page.wait_for_timeout(300)
            here = f"#/graph/{gkey}" if entered else "#/graph"
            if page.evaluate("location.hash") != here:
                finding("Esc with the panel open must close the panel, not leave the room")

        print("== the door chip that cannot lie: a real cross-room dependency")
        # In the fleet's FLAT top room the only edge that genuinely crosses a wall
        # is a live worker's report line into the conductor, so this step exercises
        # what is actually there and says so when there is nothing — a drill that
        # invented a crossing would be checking the drill, not the Atlas.
        room_of = {n["key"]: (parent.get(n["key"]) or "") for n in nodes}
        crossing = next(((e["src"], e["dst"]) for e in edges
                         if room_of.get(e["src"]) != room_of.get(e["dst"])
                         and e["src"] in room_of and e["dst"] in room_of), None)
        if not crossing:
            ok("no dependency crosses a room wall in this fleet — nothing to draw, "
               "and the Atlas draws nothing (door chips come from edges alone)")
            stray = page.evaluate(
                "document.querySelectorAll('#graphRoom .gr-door').length")
            if stray:
                finding(f"{stray} door chip(s) drawn with no cross-room edge behind them")
        else:
            src, dst = crossing
            src_room, dst_room = room_of[src], room_of[dst]
            page.evaluate("(h) => { location.hash = h; }",
                          f"#/graph/{src_room}" if src_room else "#/graph")
            page.wait_for_timeout(1100)
            door = page.evaluate("""(args) => {
                const [okey, dkey] = args;
                const card = [...document.querySelectorAll('#graphRoom .gr-card')]
                  .find(c => c.getAttribute('data-key') === okey);
                if (!card) return {err: 'no ' + okey + ' card'};
                const doors = [...card.querySelectorAll('.gr-door')];
                const hit = doors.find(d => d.getAttribute('data-door') === dkey);
                if (!hit) return {err: 'no door to ' + (dkey || 'the top room'),
                                  have: doors.map(d => d.getAttribute('data-door'))};
                const r = hit.getBoundingClientRect();
                return {x: r.x + r.width / 2, y: r.y + r.height / 2,
                        label: hit.textContent.trim()};
            }""", [src, dst_room])
            if door.get("err"):
                finding(f"{src} card door chip: {door['err']} (have {door.get('have')})")
            else:
                ok(f"door chip on {src}: \u201c{door['label']}\u201d")
                page.mouse.click(door["x"], door["y"])
                page.wait_for_timeout(900)
                want_hash = f"#/graph/{dst_room}" if dst_room else "#/graph"
                if page.evaluate("location.hash") != want_hash:
                    finding(f"the door landed on {page.evaluate('location.hash')}, "
                            f"wanted {want_hash}")
                flashed = page.evaluate("""(k) => {
                    const c = [...document.querySelectorAll('#graphRoom .gr-card')]
                      .find(n => n.getAttribute('data-key') === k);
                    return c ? c.classList.contains('gr-flash') : null;
                }""", dst)
                if flashed is None:
                    finding(f"'{dst}' card is not in the room the door landed in")
                elif not flashed:
                    finding(f"the door landed but '{dst}' did not flash-highlight")
                else:
                    ok(f"travelled to {dst_room or 'the top room'}, {dst} flash-highlighted")
            page.evaluate("(h) => { location.hash = h; }", "#/graph")
            page.wait_for_timeout(900)

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
            # An EMPTY room is a real state — the Atlas walks in, finds nothing and
            # replaces the history entry rather than stranding you in a blank room.
            empty = not any(parent.get(n["key"]) == target for n in nodes)
            page.keyboard.press("Enter")
            page.wait_for_timeout(900)
            landed = page.evaluate("location.hash")
            if empty:
                if landed != "#/graph":
                    finding(f"Enter on the empty room '{target}' stranded us at {landed}")
                else:
                    ok(f"Enter on the empty room '{target}' bounced back honestly")
            elif landed != f"#/graph/{target}":
                finding(f"Enter on chamber '{target}' landed on {landed}")
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
        page.evaluate("(h) => { location.hash = h; }",
                      f"#/graph/{gkey}" if entered else "#/graph")
        page.wait_for_timeout(1100)
        lbox = card_center(page, lkey)
        if not lbox:
            finding(f"card '{lkey}' lost after returning to its room")
        else:
            page.mouse.click(lbox["x"], lbox["y"], button="right")
            page.wait_for_timeout(350)
            labels = page.evaluate(
                "[...document.querySelectorAll('.ctx-menu .ctx-item')]"
                ".map(b => b.textContent.trim())")
            for verb in ("Start", "Stop", "Peek", "Test", "Remove", "Replace"):
                if not any(str(lbl).startswith(verb) for lbl in labels):
                    finding(f"right-click menu is missing the {verb} verb (got {labels})")
            # Remove on a SERVICE must be greyed with its reason, not offered as a
            # click that always fails — the fleet's membership is a repository file.
            rm = page.evaluate("""() => {
                const b = [...document.querySelectorAll('.ctx-menu .ctx-item')]
                  .find(x => x.textContent.trim() === 'Remove');
                return b ? {disabled: b.disabled || b.classList.contains('disabled'),
                            title: b.title || ''} : null;
            }""")
            svc_kind = ((by_key.get(lkey) or {}).get("service") or {}).get("kind")
            if svc_kind in ("core", "service"):
                if not rm or not rm["disabled"]:
                    finding("Remove is offered on a service card — it is a services.yaml edit")
                elif "services.yaml" not in (rm["title"] or ""):
                    finding(f"Remove is greyed but says nothing useful: {rm}")
                else:
                    ok("Remove on a service is greyed with the registry reason")
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

        # ------------------------------------------------------------------
        # THE HEADLINE DRILL: Start/Stop round-trips through real process state
        # ------------------------------------------------------------------
        if SKIP_FLEET:
            print("== start/stop round trip skipped (SKIP_FLEET=1)")
        else:
            print(f"== START/STOP ROUND TRIP on a real process: {ROUND_TRIP_CARD}")
            card = ROUND_TRIP_CARD

            def card_now(key):
                _st, pl = api(page, "GET", "/api/graph/self")
                return next((n for n in pl.get("nodes", []) if n["key"] == key), {})

            def card_ui(key):
                return page.evaluate("""(k) => {
                    const c = [...document.querySelectorAll('#graphRoom .gr-card')]
                      .find(n => n.getAttribute('data-key') === k);
                    if (!c) return null;
                    const chip = c.querySelector('.gr-state');
                    return {cls: c.getAttribute('class'),
                            chip: chip ? chip.textContent.trim() : ''};
                }""", key)

            def settle(key, want, tries=40):
                """Wait for the FLEET to report the state, not for a timer to expire.

                Coming back up means READY, not merely running: the readiness probe
                runs every five seconds, and a uvicorn that has bound its port is
                not yet a service — which is the entire reason the fleet declares
                probes and the entire reason this drill waits for one."""
                for _ in range(tries):
                    n = card_now(key)
                    svc = n.get("service") or {}
                    if svc.get("state") == want and (
                            want != "running" or (svc.get("pc") or {}).get("ready") == "Ready"):
                        return n
                    page.wait_for_timeout(500)
                return card_now(key)

            page.evaluate("(h) => { location.hash = h; }", "#/graph")
            page.wait_for_timeout(1200)
            before = card_now(card)
            svc0 = before.get("service") or {}
            if svc0.get("state") != "running" or svc0.get("control") is not True:
                finding(f"'{card}' is not a running, controllable card to drill "
                        f"({svc0}) — is the fleet up? (./run-local.sh)")
            elif not (svc0.get("pc") or {}).get("ready"):
                finding(f"'{card}' carries no process-compose state — the Atlas is not "
                        "reading the fleet manager at all")
            else:
                ok(f"{card}: running · pc {svc0['pc']['state']}/{svc0['pc']['ready']}")

                # --- STOP, through the Atlas's own verb ---------------------
                st, out = api(page, "POST",
                              f"/api/graph/self/node/{card}/service", {"action": "stop"})
                if st != 200:
                    finding(f"stopping '{card}' answered {st}: {out.get('detail')}")
                else:
                    n = settle(card, "stopped")
                    svc = n.get("service") or {}
                    if svc.get("state") != "stopped":
                        finding(f"after Stop the fleet still reports '{card}' "
                                f"{svc.get('state')} — the button did not stop a process")
                    elif (svc.get("pc") or {}).get("running"):
                        finding(f"process-compose still reports '{card}' running")
                    else:
                        ok(f"{card} stopped — pc says {svc['pc']['state']!r}")
                    if (n.get("health") or {}).get("status") != "red":
                        finding(f"a stopped '{card}' is not RED on the card "
                                f"({(n.get('health') or {}).get('status')})")
                    else:
                        ok(f"{card}'s card went red")
                    # ...and the app it was cut out of is unharmed. This is the
                    # half that makes the drill worth running: an isolated service
                    # is one you can take away.
                    page.evaluate("location.reload()")
                    page.wait_for_selector("#graphRoom .gr-card", timeout=15000)
                    page.wait_for_timeout(1400)
                    if page.evaluate("location.hash") not in ("#/graph", ""):
                        finding("the Atlas did not survive a reload with a service down")
                    ui = card_ui(card)
                    if not ui:
                        finding(f"'{card}' vanished from the room while stopped")
                    elif "gr-hs-red" not in (ui["cls"] or ""):
                        finding(f"the stopped card does not carry the red class ({ui})")
                    elif "stopped" not in (ui["chip"] or ""):
                        finding(f"the stopped card's state chip says {ui['chip']!r}")
                    else:
                        ok(f"on screen: {ui['chip']!r}, red — and the platform is fine")
                    st_h, _ = api(page, "GET", "/api/health")
                    if st_h != 200:
                        finding("stopping one service took the conductor with it")
                    # The reload above emptied this tab's history, and the back-nav
                    # hammer at the end of the drill walks it. Re-enter the Atlas the
                    # way a person does, so the trail it hammers is a real one.
                    page.evaluate("location.hash = '#/hq'")
                    page.wait_for_timeout(900)
                    page.wait_for_selector("#graphLink:not([hidden])", timeout=8000)
                    page.click("#graphLink")
                    page.wait_for_selector("#graphRoom .gr-card", timeout=10000)
                    page.wait_for_timeout(1200)

                # --- START, and back to green -------------------------------
                st, out = api(page, "POST",
                              f"/api/graph/self/node/{card}/service", {"action": "start"})
                if st != 200:
                    finding(f"starting '{card}' answered {st}: {out.get('detail')}")
                else:
                    n = settle(card, "running")
                    svc = n.get("service") or {}
                    if svc.get("state") != "running":
                        finding(f"'{card}' did not come back up ({svc.get('state')})")
                    elif (svc.get("pc") or {}).get("ready") != "Ready":
                        finding(f"'{card}' is running but the fleet never called it "
                                f"Ready ({(svc.get('pc') or {}).get('ready')})")
                    else:
                        ok(f"{card} started — pc says Running/Ready again")
                    for _ in range(20):        # the beat also asks its own /health
                        if (card_now(card).get("health") or {}).get("status") == "green":
                            break
                        page.wait_for_timeout(500)
                    h = (card_now(card).get("health") or {}).get("status")
                    if h != "green":
                        finding(f"'{card}' is back but its card is still {h}")
                    else:
                        ok(f"{card}'s card is green again — full round trip clean")

            # the honest refusals, at the wire: the conductor cannot stop itself,
            # and the pool has no switch at all
            st, out = api(page, "POST", "/api/graph/self/node/conductor/service",
                          {"action": "stop"})
            if st != 400 or "Ctrl-C" not in str(out.get("detail", "")):
                finding(f"stopping the conductor answered {st} {out.get('detail')!r} — "
                        "it must refuse, and say where the real switch is")
            else:
                ok("the conductor refuses its own Stop and names the real switch")
            st, out = api(page, "POST", "/api/graph/self/node/worker-pool/service",
                          {"action": "stop"})
            if st != 400:
                finding(f"the worker pool offered a Stop it does not have ({st})")
            else:
                ok("the worker pool refuses honestly — there is no switch, only work")
            # ...and the crew's sub-switch on the conductor's card IS real
            st, out = api(page, "POST", "/api/graph/self/node/conductor/service",
                          {"action": "stop", "sub": "repair"})
            if st != 200:
                finding(f"the IT crew sub-switch answered {st}: {out.get('detail')}")
            else:
                sub = ((out.get("service") or {}).get("sub") or {})
                if sub.get("state") != "stopped":
                    finding(f"the crew sub-switch did not take: {sub}")
                else:
                    ok("the IT crew sub-switch on the conductor's card is real")

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
                _st3, payload3 = api(page, "GET", "/api/graph/self")
                after = {n["key"] for n in payload3.get("nodes", [])}
                before_keys = {n["key"] for n in nodes}
                if after - before_keys:
                    finding("the manager INVENTED cards the fleet does not run: "
                            f"{sorted(after - before_keys)}")
                if before_keys - after:
                    finding("the manager DROPPED cards the fleet is running: "
                            f"{sorted(before_keys - after)}")
                st2, payload2 = api(page, "GET", "/api/graph/self")
                leaves2 = [n for n in payload2.get("nodes", [])
                           if n["node_type"] not in ("aim", "conclusion", "group")
                           and (n.get("service") or {}).get("kind")
                           not in ("worker", "app")]
                bare = [n["key"] for n in leaves2 if not n.get("agent")]
                if bare:
                    finding(f"after replan these leaves have no agent: {bare}")
                else:
                    ok(f"replan landed — every one of the {len(leaves2)} leaves is staffed")
                target2 = next((n for n in leaves2 if n.get("agent")), None)
                if target2:
                    page.evaluate("(h) => { location.hash = h; }",
                                  f"#/graph/{target2['parent_key']}"
                                  if target2.get("parent_key") else "#/graph")
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
