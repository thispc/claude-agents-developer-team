"""The Tony Stark drill — a scripted user fiddling through canvas → HQ → module graph →
drill → single-click panel → steer → replan → browser-back out, printing FINDINGS for
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

        print("== canvas → HQ → module graph")
        page.evaluate("location.hash = '#/devteam'")   # the crew's canvas
        page.wait_for_timeout(900)
        page.evaluate("location.hash = '#/hq'")        # Devteam HQ
        page.wait_for_timeout(900)
        page.wait_for_selector("#graphLink:not([hidden])", timeout=8000)
        page.click("#graphLink")
        page.wait_for_selector(".gr-node.gr-group", timeout=10000)
        page.wait_for_timeout(1600)                    # the staged reveal
        if page.evaluate("location.hash") != "#/graph":
            finding(f"opening the graph landed on {page.evaluate('location.hash')}")
        ok("graph open at #/graph")

        st, payload = api(page, "GET", "/api/graph/self")
        leaves = [n for n in payload.get("nodes", [])
                  if n["node_type"] not in ("aim", "conclusion", "group")]
        groups = [n for n in payload.get("nodes", []) if n["node_type"] == "group"]

        print("== every leaf shows its real tests (routes was the liar)")
        for n in leaves:
            if not n["tests"]["total"]:
                finding(f"leaf '{n['key']}' has no mapped suites in the payload")
        routes = next((n for n in leaves if n["key"] == "routes"), None)
        if routes and routes["tests"]["total"]:
            ok(f"routes leaf maps {routes['tests']['total']} suite(s)")

        print("== drill into a group (double-click = the microscope)")
        gkey = (routes or leaves[0])["parent_key"] or groups[0]["key"]
        hist0 = page.evaluate("history.length")
        box = page.evaluate("""(k) => {
            const g = [...document.querySelectorAll('.gr-node.gr-group')]
              .find(n => n.getAttribute('data-key') === k);
            if (!g) return null;
            const r = g.getBoundingClientRect();
            return {x: r.x + r.width/2, y: r.y + r.height/2};
        }""", gkey)
        if not box:
            finding(f"group card '{gkey}' not on stage")
            browser.close()
            return 1
        page.mouse.click(box["x"], box["y"])
        page.wait_for_timeout(120)
        page.mouse.click(box["x"], box["y"])
        page.wait_for_timeout(1500)                    # the flight + reveal
        if page.evaluate("location.hash") != f"#/graph/{gkey}":
            finding(f"drill landed on {page.evaluate('location.hash')}, wanted #/graph/{gkey}")
        hist1 = page.evaluate("history.length")
        if hist1 - hist0 > 1:
            finding(f"one drill minted {hist1 - hist0} history entries (double hash write)")
        else:
            ok("one drill = one history entry")

        print("== single click a leaf: ONE full panel, agent front and center")
        lkey = next((n["key"] for n in leaves if n["parent_key"] == gkey), None)
        lbox = page.evaluate("""(k) => {
            const g = [...document.querySelectorAll('.gr-node')]
              .find(n => n.getAttribute('data-key') === k);
            if (!g) return null;
            const r = g.getBoundingClientRect();
            return {x: r.x + r.width/2, y: r.y + r.height/2};
        }""", lkey)
        if not lbox:
            finding(f"leaf card '{lkey}' not on stage after the drill")
        else:
            page.mouse.click(lbox["x"], lbox["y"])
            page.wait_for_timeout(700)
            aside = page.inner_text("#graphAside")
            for needle, what in (("Tests", "tests section"), ("Trace", "trace section"),
                                 ("Steering", "config section"), ("Edges", "edges section")):
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
            if "no tests mapped" in aside and (next(
                    (n for n in leaves if n["key"] == lkey), {})
                    .get("tests", {}).get("total")):
                finding(f"panel claims 'no tests mapped' on '{lkey}' but the payload maps some")

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
            # The faithful path is the panel's own button when the leaf is
            # unassigned; the API is the fallback when everything is staffed.
            replanned = False
            if page.evaluate("!!document.querySelector('#grStaff')"):
                page.click("#grStaff")
                try:
                    page.wait_for_function(
                        """() => { const o = document.querySelector('#grStaffOut');
                                   return o && /✓|plan|usable|offline/.test(o.textContent); }""",
                        timeout=300_000)
                    outtxt = page.evaluate(
                        "(document.querySelector('#grStaffOut')||{}).textContent || ''")
                    if "✓" in outtxt:
                        ok(f"the panel button replanned: {outtxt.strip()}")
                        replanned = True
                    else:
                        finding(f"the panel's replan button failed: {outtxt.strip()}")
                except Exception:
                    finding("the panel's replan button never reported back")
            if not replanned:
                st, out = api(page, "POST", "/api/graph/self/replan")
                if st != 200:
                    finding(f"replan returned {st}: {out.get('detail')}")
                elif (out.get("plan") or {}).get("authored_by") != "manager":
                    finding(f"replan's plan is not manager-authored: {out}")
                else:
                    replanned = True
            if replanned:
                st2, payload2 = api(page, "GET", "/api/graph/self")
                leaves2 = [n for n in payload2.get("nodes", [])
                           if n["node_type"] not in ("aim", "conclusion", "group")]
                bare = [n["key"] for n in leaves2 if not n.get("agent")]
                noname = [n["key"] for n in leaves2
                          if n.get("agent") and not n["agent"].get("name")]
                if bare:
                    finding(f"after replan these leaves have no agent: {bare}")
                else:
                    ok(f"replan landed — every one of the {len(leaves2)} leaves is staffed")
                if noname:
                    finding(f"assigned agents carry no display name: {noname}")
                # chips render on LEAF cards, so stand on a level that shows some:
                # drill (via the hash, like a pasted link) into a staffed leaf's group
                target = next((n for n in leaves2 if n.get("agent")), None)
                if target:
                    page.evaluate("(h) => { location.hash = h; }",
                                  f"#/graph/{target['parent_key']}")
                    try:
                        page.wait_for_function(
                            "document.querySelectorAll('.gr-node .gr-agent').length > 0",
                            timeout=12_000)
                        chips = page.evaluate(
                            "document.querySelectorAll('.gr-node .gr-agent').length")
                        ok(f"{chips} agent chip(s) drawn on the leaf cards")
                    except Exception:
                        finding("no agent chips on the leaf cards after the replan")

        print("== the back-nav hammer")
        # wherever the replan left us, walk home the way a browser user does —
        # each back must MOVE (a back that leaves the hash unchanged is the bug)
        now = page.evaluate("location.hash")
        for _ in range(4):
            if now == "#/hq":
                break
            before = now
            page.go_back()
            page.wait_for_timeout(800)
            now = page.evaluate("location.hash")
            if now == before:
                finding(f"browser-back from {before} left the hash unchanged")
                break
        if now != "#/hq":
            finding(f"backing out of the graph landed on '{now}', wanted #/hq")
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
