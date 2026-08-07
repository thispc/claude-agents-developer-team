"""The Devteam's Command tab reuses the projects Command tab's language — and adds the one
step a project never needs: updating the running app to the code the crew just landed.

Born from a live complaint: "the approve button click doesn't do anything." It was doing
two somethings, both invisible — the button was DISABLED because main had moved (the reason
buried in a span), and code approved earlier had landed but the process never restarted, so
nothing observable changed. The cure is one visual language (the ui* builders) plus an
update card that says out loud when the app is running stale code.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_JS = (ROOT / "dashboard" / "js" / "projects.js").read_text()
REPAIR_JS = (ROOT / "dashboard" / "js" / "repair.js").read_text()


# ---- the shared builders --------------------------------------------------------------

def test_projects_defines_the_shared_builders_and_uses_them_itself():
    """The builders live in projects.js (loaded before repair.js — order is load-bearing)
    and renderCommand itself must render through them, or 'shared' would be a fiction."""
    for fn in ("function uiAttnCard(", "function uiAskCard(", "function uiAgentCard(",
               "function uiLane("):
        assert fn in PROJECTS_JS, f"missing shared builder: {fn}"
    body = PROJECTS_JS.split("function renderCommand", 1)[1].split("\nfunction ", 1)[0]
    for used in ("uiAttnCard(", "uiAskCard(", "uiAgentCard(", "uiLane("):
        assert used in body, f"renderCommand must build with {used}"


def test_the_devteam_command_tab_speaks_the_same_language():
    panel = REPAIR_JS.split("function rpCommandPanel", 1)[1].split("\nfunction ", 1)[0]
    for used in ("uiAttnCard(", "uiAskCard(", "uiAgentCard(", "uiLane("):
        assert used in panel, f"rpCommandPanel must build with {used}"
    # The queue card must explain a disabled approve instead of failing silently.
    assert "data-approve" in panel and "st.why" in panel
    # A stale branch's PRIMARY action is the rebuild, not a dead approve.
    assert "data-rebuild" in panel
    # The ticket form's pinned anchors survive the rebuild.
    for pin in ("roughIssue", "refineBtn", 'name="sprints"'):
        assert pin in panel, f"ticket form lost its pinned anchor {pin}"


def test_the_update_card_is_wired_to_restart_and_reload():
    panel = REPAIR_JS.split("function rpCommandPanel", 1)[1].split("\nfunction ", 1)[0]
    assert "data-rp-update" in panel and "needs_restart" in panel
    assert "data-rp-reload" in panel and "needs_reload" in panel
    wire = REPAIR_JS.split("function rpWire", 1)[1]
    assert "/api/repair/restart" in wire and "waitForRestart(" in wire
    assert "location.reload()" in wire


# ---- code currency (the backend truth the card renders) -------------------------------

def _fresh_currency(monkeypatch, boot: str | None):
    import sys
    sys.path.insert(0, str(ROOT / "conductor"))
    from app import selfops
    monkeypatch.setattr(selfops, "_BOOT_SHA", boot)
    monkeypatch.setattr(selfops, "_CURRENCY", {"val": None, "ts": 0.0})
    return selfops


def test_current_process_reports_no_update(monkeypatch):
    selfops = _fresh_currency(monkeypatch, None)
    cur = selfops.code_currency()          # mark_boot captures HEAD now → boot == head
    assert cur["behind"] == 0
    assert cur["needs_restart"] is False and cur["needs_reload"] is False


def test_a_moved_main_is_reported_with_the_right_remedy(monkeypatch):
    import subprocess
    old = subprocess.run(["git", "rev-parse", "HEAD~3"], cwd=ROOT,
                         capture_output=True, text=True).stdout.strip()
    selfops = _fresh_currency(monkeypatch, old)
    cur = selfops.code_currency()
    assert cur["behind"] >= 3
    files = cur["files"]
    assert cur["needs_restart"] == any(f.startswith(("conductor/", "worker/")) for f in files)
    assert cur["needs_reload"] == any(f.startswith("dashboard/") for f in files)


def test_status_carries_the_code_block(fresh_db):
    import sys
    sys.path.insert(0, str(ROOT / "conductor"))
    from app import repair
    code = repair.status().get("code") or {}
    for key in ("behind", "needs_restart", "needs_reload"):
        assert key in code, f"status().code must carry {key}"


def test_approve_reports_whether_a_restart_is_due(fresh_db, monkeypatch, root_client):
    """The UI offers the restart in the same breath as 'Landed.' — the poll would say so
    ten seconds later, but the person is looking NOW."""
    import sys
    sys.path.insert(0, str(ROOT / "conductor"))
    from app import db, repair_builder as rb
    db.kv_set("repair:queue", [{"branch": "repair/s1-x", "title": "x", "sprint_no": 1,
                                "slug": "x", "created_at": 0.0}])
    monkeypatch.setattr(rb, "land", lambda branch, title:
                        {"ok": True, "sha": "f" * 40, "files": ["conductor/app/x.py"]})
    monkeypatch.setattr(rb, "discard", lambda branch, wt=None: None)
    r = root_client.post("/api/repair/queue/approve", json={"branch": "repair/s1-x"})
    assert r.status_code == 200
    body = r.json()
    assert body["needs_restart"] is True
    assert body["files"] == ["conductor/app/x.py"]


# ---- Devteam HQ: the project view rendering the crew ----------------------------------

CORE_JS = (ROOT / "dashboard" / "js" / "core.js").read_text()
STUDIO_JS = (ROOT / "dashboard" / "js" / "studio.js").read_text()


def test_as_project_speaks_fluent_project(fresh_db, root_client):
    """The adapter's whole contract: a payload the project screen can render without ONE
    renderer knowing where it came from."""
    r = root_client.get("/api/repair/as-project")
    assert r.status_code == 200
    p = r.json()
    for key in ("id", "name", "status", "summary", "brief", "team", "tasks",
                "manager_model", "autonomy", "repair"):
        assert key in p, f"project payload must carry {key}"
    assert p["id"] == "devteam"
    ok_status = {"planned", "running", "review", "failed", "done", "paused"}
    assert p["status"] in ok_status
    for t in p["tasks"]:
        assert t["status"] in ok_status, f"task status {t['status']} unknown to the board"
        assert t["role"], "every task needs a specialist role"
    import json as _json
    roles = [m["role"] for m in _json.loads(p["team"])]
    assert roles, "the crew must appear as the roster"


def test_the_hq_seam_is_wired_everywhere_it_must_be():
    """One seam (projSrc), consulted at every place the truth's source matters — and
    nowhere else. A renderer that branches on devteam has forked the view again."""
    assert "let projSrc = null" in PROJECTS_JS
    assert "function openDevteamHQ(" in PROJECTS_JS
    for seam in ("projSrc.board()", "projSrc.question()", "projSrc.answer(",
                 "projSrc.chatSend(", "projSrc.showTask(", "projSrc.renderExtras("):
        assert seam in PROJECTS_JS, f"missing seam use: {seam}"
    # HQ's own tabs render with the repair screen's panels — the categorized board survives.
    assert "rpBoardPanel(" in PROJECTS_JS and "rpUsagePanel(" in PROJECTS_JS
    assert "#/hq" in CORE_JS, "the router must know the HQ hash"
    assert "rpWireQueue(" in REPAIR_JS.split("function rpWire(", 1)[1], \
        "the improve screen must reuse the shared queue wiring"
    assert "openDevteamHQ()" in STUDIO_JS, "the canvas devbar must lead to HQ"
