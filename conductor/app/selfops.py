"""The platform working on itself.

devteam appears in its own project list: root can raise an issue against this
repo, the team fixes it on a branch, opens a PR, and — once root approves — the
running instance pulls the merged code and restarts on it. The snake bites its
own tail.

Two properties keep that from being reckless:

- **Workers never touch the live tree.** A worker clones the repo into its own
  workspace exactly like any other project, so a bad edit lands on a branch and
  dies in review. Only `redeploy()` touches the directory this process is
  running from, and only when root asks.
- **Every redeploy is reversible.** The commit we were on is recorded before
  pulling, so a deploy that breaks the platform can be rolled back to a known-good
  revision without git knowledge.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import bus, config, db

# Where this instance is running from — the tree a redeploy updates.
LIVE_TREE = Path(__file__).resolve().parent.parent.parent
STATE_FILE = LIVE_TREE / ".devteam-deploy.json"

SELF_PROJECT_NAME = "devteam (this platform)"


def _sh(*cmd: str, cwd: Path | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd or LIVE_TREE), capture_output=True,
                          text=True, timeout=timeout)


def _state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(**fields: Any) -> None:
    s = _state()
    s.update(fields)
    try:
        STATE_FILE.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


def self_repo() -> str:
    """The GitHub repo this platform's own code lives in."""
    if config.SELF_REPO:
        return config.SELF_REPO
    r = _sh("git", "remote", "get-url", "origin")
    url = r.stdout.strip()
    if not url:
        return ""
    # git@github.com:owner/repo.git  or  https://github.com/owner/repo.git
    tail = url.split("github.com")[-1].lstrip(":/")
    return tail[:-4] if tail.endswith(".git") else tail


def head() -> dict[str, str]:
    return {
        "commit": _sh("git", "rev-parse", "--short", "HEAD").stdout.strip(),
        "subject": _sh("git", "log", "-1", "--pretty=%s").stdout.strip(),
        "branch": _sh("git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
        "dirty": "yes" if _sh("git", "status", "--porcelain").stdout.strip() else "no",
    }


def ensure_project(owner_id: int) -> int:
    """The platform's own project row — created once, reused forever."""
    for p in db.list_projects():
        if p["is_self"]:
            return p["id"]
    pid = db.create_project(
        SELF_PROJECT_NAME,
        "This is the devteam platform itself. Issues raised here are defects and "
        "improvements in the very app you are running: fix them in this repo, on a "
        "branch, with a PR. Be conservative — a bad change breaks the platform for "
        "everyone, including this team.",
        self_repo(), config.PROJECT_BUDGET_USD, config.MAX_CONCURRENT_WORKERS,
        max_runs=config.MAX_AGENT_RUNS, owner_id=owner_id,
    )
    db.set_project_self(pid, True)
    return pid


SEVERITY_NOTE = {
    "bug": "This is a defect in the running platform.",
    "improvement": "This is an enhancement to the platform.",
    "urgent": "This is breaking the platform right now — fix it first, minimally, "
              "and do not refactor anything unrelated.",
}


def issue_brief(title: str, body: str, severity: str) -> str:
    """The brief the manager receives. Defensive about self-modification, because
    the repo being changed is the one running the session."""
    return (
        f"ISSUE RAISED AGAINST THIS PLATFORM: {title}\n\n"
        f"{SEVERITY_NOTE.get(severity, '')}\n\n{body}\n\n"
        "Plan and assign the fix. Pass these ground rules to whoever works on it:\n"
        "- This repository IS the platform running your session. Change the minimum "
        "needed to fix the issue; unrelated refactors are rejected.\n"
        "- Never edit or delete `devteam.db`, `.env`, or anything under `workspaces/`.\n"
        "- The app must still start: `python -c \"import sys; sys.path.insert(0,'conductor'); "
        "import app.main\"` must succeed before you finish.\n"
        "- If you touch the dashboard, keep it dependency-free — no build step, no CDN.\n"
        "- Say exactly which files you changed and how you verified the fix."
    )


async def file_issue(project_id: int, title: str, body: str, severity: str) -> dict[str, Any]:
    """Hand an issue about the platform to the manager as work to plan.

    Deliberately does NOT create a task directly. A raw task would be dispatched
    by the scheduler the moment it exists — no plan, no review, no decision about
    whether the fix needs one worker or three. Routing it through the manager is
    what makes this the same loop as every other project, applied to ourselves.
    """
    from . import github_client

    p = db.get_project(project_id) or {}
    issue_no = None
    if github_client.enabled(p.get("repo", "")):
        try:
            issue_no = await github_client.create_issue(
                p["repo"], f"[self] {title}",
                f"{body}\n\n_raised from the devteam dashboard_")
        except Exception:
            pass

    brief = issue_brief(title, body, severity)
    if issue_no:
        brief += f"\n\nTracking GitHub issue #{issue_no} on {p.get('repo')}."
    db.add_directive(project_id, brief)
    db.set_project_status(project_id, "running", "")
    bus.emit(project_id, None, "boss", "self_issue_raised",
             {"title": title, "severity": severity, "issue": issue_no})
    return {"issue": issue_no, "queued": True}


def can_redeploy() -> dict[str, Any]:
    """Whether it is safe to pull new code into the running instance."""
    h = head()
    reasons = []
    if h["dirty"] == "yes":
        reasons.append("the live tree has uncommitted changes that a pull would clobber")
    # Only genuinely live runs count. A task left 'running' by a session that died
    # is a zombie — counting those would block every future deploy forever.
    from . import launcher, scheduler
    now = time.time()
    live = 0
    for proj in db.list_projects():
        for t in db.list_tasks(proj["id"]):
            if t["status"] not in ("running", "queued"):
                continue
            tracked = any(str(k).startswith(str(t["id"])) for k in launcher.ACTIVE)
            fresh = now - t["updated_at"] < scheduler.STUCK_SECONDS
            if tracked or fresh:
                live += 1
    if live:
        reasons.append(f"{live} agent(s) are mid-run and would be killed by the restart")
    return {"ok": not reasons, "reasons": reasons, "head": h,
            "last_deploy": _state(), "repo": self_repo()}


def redeploy(force: bool = False) -> dict[str, Any]:
    """Pull the merged code and restart this process onto it.

    The commit we are leaving is recorded first, so `rollback()` can always get
    back to a version that was known to boot.
    """
    check = can_redeploy()
    if not check["ok"] and not force:
        return {"ok": False, "error": "; ".join(check["reasons"]), **check}

    previous = head()
    fetch = _sh("git", "fetch", "origin")
    if fetch.returncode != 0:
        return {"ok": False, "error": f"git fetch failed: {fetch.stderr[-300:]}"}
    pull = _sh("git", "pull", "--ff-only", "origin", previous["branch"])
    if pull.returncode != 0:
        return {"ok": False,
                "error": f"git pull failed (not a fast-forward?): {pull.stderr[-300:]}"}

    new = head()
    if new["commit"] == previous["commit"]:
        return {"ok": False, "error": "already running the latest commit; nothing to deploy",
                "head": new}

    # Refuse to restart onto code that cannot even be imported — that would take
    # the platform down with no way back through the UI.
    check_import = _sh(sys.executable, "-c",
                       "import sys; sys.path.insert(0, 'conductor'); import app.main",
                       timeout=90)
    if check_import.returncode != 0:
        _sh("git", "reset", "--hard", previous["commit"])
        return {"ok": False, "reverted": True,
                "error": "the new code failed to import, so it was NOT deployed; "
                         f"reverted to {previous['commit']}. "
                         f"{check_import.stderr.strip()[-400:]}"}

    _save_state(rollback_to=previous["commit"], rollback_subject=previous["subject"],
                deployed=new["commit"], deployed_subject=new["subject"], at=time.time())
    return {"ok": True, "from": previous, "to": new, "restarting": True}


def rollback() -> dict[str, Any]:
    prev = _state().get("rollback_to")
    if not prev:
        return {"ok": False, "error": "no previous deploy recorded to roll back to"}
    r = _sh("git", "reset", "--hard", prev)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-300:]}
    _save_state(rolled_back_from=head()["commit"], at=time.time())
    return {"ok": True, "to": head(), "restarting": True}


def restart_process() -> None:
    """Replace this process with a fresh one on the new code.

    exec keeps the same PID and the same listening socket setup as the original
    launch, so whatever supervises the process (shell, systemd, k8s) is unaffected.
    """
    os.execv(sys.executable, [sys.executable] + sys.argv)
