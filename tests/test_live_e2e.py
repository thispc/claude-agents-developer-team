"""ONE real end-to-end scenario, driven through the UI with a live browser.

This is the only test that spends real tokens and needs real credentials, so it
is opt-in: it SKIPS unless E2E_LIVE=1 and the credentials below are provided.

What it does, exactly as a new user would:
  1. sign up a brand-new user
  2. land in Settings and paste their own Anthropic + GitHub credentials
  3. create a project through the 3-step wizard with a tiny brief
  4. watch the team actually work — poll until a task produces real output
     (a branch/PR/report) or the run finishes

It runs against its own uvicorn instance on a temp database, so it never touches
your real devteam.db, and the new user runs on THEIR OWN credentials (proving the
per-user credential path end to end).

Run it:

    E2E_LIVE=1 \
    E2E_ANTHROPIC_KEY=sk-ant-...      # or E2E_OAUTH_TOKEN=... (one of them) \
    E2E_GITHUB_TOKEN=github_pat_... \
    E2E_GITHUB_REPO=youruser/a-throwaway-repo \
    .venv/bin/python -m pytest tests/test_live_e2e.py -s -v

Optional: E2E_TIMEOUT (seconds, default 600), E2E_PORT (default 8123).
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

LIVE = os.environ.get("E2E_LIVE") == "1"
ANTHROPIC = os.environ.get("E2E_ANTHROPIC_KEY", "")
OAUTH = os.environ.get("E2E_OAUTH_TOKEN", "")
GH_TOKEN = os.environ.get("E2E_GITHUB_TOKEN", "")
GH_REPO = os.environ.get("E2E_GITHUB_REPO", "")
PORT = int(os.environ.get("E2E_PORT", "8123"))
TIMEOUT = int(os.environ.get("E2E_TIMEOUT", "600"))
BASE = f"http://localhost:{PORT}"

pytestmark = pytest.mark.skipif(
    not (LIVE and (ANTHROPIC or OAUTH) and GH_TOKEN and GH_REPO),
    reason="live e2e is opt-in: set E2E_LIVE=1 and E2E_ANTHROPIC_KEY/E2E_OAUTH_TOKEN, "
           "E2E_GITHUB_TOKEN, E2E_GITHUB_REPO",
)


def _wait_port(port, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def live_server():
    """A dedicated conductor on a temp DB, so the test never touches real data."""
    tmp = tempfile.mkdtemp(prefix="devteam-e2e-")
    env = dict(os.environ)
    env.update({
        "DB_PATH": str(Path(tmp) / "e2e.db"),
        "WORKSPACES_DIR": str(Path(tmp) / "workspaces"),
        "PREVIEW_DIR": str(Path(tmp) / "previews"),
        "DEPLOY_DIR": str(Path(tmp) / "deployments"),
        "ROOT_USERNAME": "root",
        "ROOT_PASSWORD": "e2e-root-pass",
        "WORKER_TOKEN": "e2e-worker-token",
        "CONDUCTOR_URL": BASE,        # workers must report back here
        "LAUNCHER": "local",
        "PYTHONPATH": str(REPO / "conductor"),
        # the server itself must NOT carry AI creds — the new user brings their own,
        # which is the whole point. Strip any inherited operator credentials.
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "GITHUB_TOKEN": "",
    })
    log = open(Path(tmp) / "server.log", "w")
    proc = subprocess.Popen(
        [str(REPO / ".venv/bin/uvicorn"), "app.main:app", "--host", "0.0.0.0",
         "--port", str(PORT)],
        cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    assert _wait_port(PORT), "e2e server did not come up"
    time.sleep(1)
    yield {"tmp": tmp, "log": Path(tmp) / "server.log"}
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
    shutil.rmtree(tmp, ignore_errors=True)


def test_new_user_builds_an_app_through_the_ui(live_server):
    from playwright.sync_api import sync_playwright
    import httpx

    username = f"e2e{int(time.time())}"
    brief = ("Create a single static file index.html at the repo root that shows "
             "a heading 'Hello from devteam' and the text 'built by an AI team'. "
             "One file only, no build step, no dependencies. Keep it minimal.")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        console_errors = []
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text)
                if m.type == "error" else None)

        # --- 1. sign up a brand-new user -----------------------------------
        page.goto(BASE, wait_until="networkidle")
        # the login screen is shown for a signed-out visitor
        page.wait_for_selector("#loginScreen", state="visible", timeout=15000)
        page.fill("input[name=username]", username)
        page.fill("input[name=password]", "e2e-user-pass")
        page.click("#signupBtn")
        # signup lands the user straight in Settings
        page.wait_for_selector("#settingsDialog[open]", timeout=15000)

        # --- 2. paste their own credentials --------------------------------
        page.fill("#settingsForm input[name=github_token]", GH_TOKEN)
        if ANTHROPIC:
            page.fill("#settingsForm input[name=anthropic_api_key]", ANTHROPIC)
        else:
            page.fill("#settingsForm input[name=claude_oauth_token]", OAUTH)
        page.click("#settingsForm button[type=submit]")
        page.wait_for_selector("#settingsDialog", state="hidden", timeout=10000)

        # sanity: the server now reports this user has their own credentials
        # (via the same cookie the browser holds)
        cookie = {c["name"]: c["value"] for c in page.context.cookies()}
        me = httpx.get(f"{BASE}/api/me", cookies=cookie, timeout=10).json()
        assert me["signed_in"] and me["has_ai_credentials"], me

        # --- 3. create a project through the wizard ------------------------
        page.click("#newProjectBtn")
        page.wait_for_selector("#newProjectDialog[open]", timeout=10000)
        page.fill("#newProjectForm input[name=name]", "hello-devteam")
        page.fill("#newProjectForm textarea[name=brief]", brief)
        page.fill("#newProjectForm input[name=repo]", GH_REPO)
        page.click("#toRecruitBtn")               # → step 2, calls suggest-team
        # roster appears once the team is suggested
        page.wait_for_selector("#roster .roster-row, #roster input", timeout=60000)
        page.click("#toGoBtn")                    # → step 3
        page.wait_for_selector("#createBtn", state="visible", timeout=10000)
        page.click("#createBtn")                  # hire & start

        # the console switches to the new project
        page.wait_for_selector("main:not([hidden])", timeout=15000)
        time.sleep(2)
        pid = _current_project_id(cookie)
        assert pid, "no project was created"
        print(f"\n[e2e] project {pid} created; watching it work (timeout {TIMEOUT}s)…")

        # --- 4. watch the team actually do something -----------------------
        milestone, detail = _await_milestone(cookie, pid, TIMEOUT)
        # capture evidence regardless of outcome
        shot = Path(live_server["tmp"]).parent / f"e2e-{pid}.png"
        try:
            page.goto(f"{BASE}/#/p/{pid}/board", wait_until="networkidle")
            time.sleep(2)
            page.screenshot(path=str(shot))
            print(f"[e2e] board screenshot: {shot}")
        except Exception:
            pass

        _dump_feed(cookie, pid)
        browser.close()

    assert milestone, (
        f"no real progress within {TIMEOUT}s. Last state: {detail}. "
        f"Check {live_server['log']} for the server/agent logs.")
    print(f"\n[e2e] PASS — reached milestone: {milestone} ({detail})")
    # UI must not have thrown along the way
    assert not console_errors, f"console errors during the flow: {console_errors[:5]}"


# --------------------------------------------------------------------------

def _current_project_id(cookie):
    import httpx
    ps = httpx.get(f"{BASE}/api/projects", cookies=cookie, timeout=10).json()
    return ps[0]["id"] if ps else None


def _await_milestone(cookie, pid, timeout):
    """Poll until the team produces something real, or the run ends.

    A 'milestone' is real evidence work happened: a task reached review/done, a
    branch/PR was produced, or the project finished. Returns (milestone, detail).
    """
    import httpx

    end = time.time() + timeout
    last = ""
    while time.time() < end:
        try:
            proj = httpx.get(f"{BASE}/api/projects/{pid}", cookies=cookie, timeout=10).json()
            tasks = proj.get("tasks", [])
        except Exception as e:
            time.sleep(4); last = f"poll error: {e}"; continue

        statuses = {t["status"] for t in tasks}
        # strongest evidence first
        if any(t.get("pr_number") for t in tasks):
            return "a PR was opened", _summ(tasks)
        if statuses & {"review", "pushed", "done"}:
            return "a task produced reviewable output", _summ(tasks)
        if proj["status"] in ("done", "review"):
            return f"project reached '{proj['status']}'", _summ(tasks)
        if proj["status"] == "failed":
            return "", f"project FAILED — {_summ(tasks)}"

        last = f"project={proj['status']} tasks={_summ(tasks)}"
        print(f"[e2e] … {last}")
        time.sleep(6)
    return "", last


def _summ(tasks):
    return ", ".join(f"#{t.get('seq','?')}:{t['role']}={t['status']}" for t in tasks) or "none yet"


def _dump_feed(cookie, pid):
    import httpx
    try:
        evs = httpx.get(f"{BASE}/api/projects/{pid}/events", cookies=cookie, timeout=10).json()
    except Exception:
        return
    print(f"\n[e2e] last activity for project {pid}:")
    for e in evs[-15:]:
        payload = str(e.get("payload", ""))[:80].replace("\n", " ")
        print(f"    {e['source']:16} {e['kind']:20} {payload}")
