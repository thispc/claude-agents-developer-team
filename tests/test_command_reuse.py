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
