"""Self-hosted preview of a project's built static site, served under our own URL
at /preview/{project_id}/. We clone/pull the repo's default branch into a preview
dir and serve it read-only — sandboxed static files, no access to the control plane.

Only works for static sites (an index.html at repo root, /docs, or /web). Server
apps can't be previewed this way (they'd need to run) — use the DOKS deploy for those.
"""

import asyncio
import subprocess
from pathlib import Path

from . import config, db, github_client

PREVIEW_DIR = Path(config._env("PREVIEW_DIR", str(config.ROOT / "previews")))
_STATIC_SUBDIRS = ("", "docs", "web", "public", "dist", "build")


def synced_at(project_id: int) -> str:
    """Human-readable freshness of the previewed build, so a stale demo is obvious."""
    import time
    base = PREVIEW_DIR / str(project_id) / "repo" / ".git"
    if not base.exists():
        return ""
    age = int(time.time() - base.stat().st_mtime)
    if age < 90:
        return "built just now"
    if age < 3600:
        return f"built {age // 60} min ago"
    if age < 86400:
        return f"built {age // 3600} h ago"
    return f"built {age // 86400} d ago"


def preview_root(project_id: int) -> Path | None:
    """The served directory for a project, or None if not synced / not static."""
    base = PREVIEW_DIR / str(project_id) / "repo"
    if not base.exists():
        return None
    for sub in _STATIC_SUBDIRS:
        d = base / sub if sub else base
        if (d / "index.html").exists():
            return d
    return None


def _sh(*cmd: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


async def sync(project_id: int) -> tuple[bool, str]:
    """Clone or pull the project's default branch into its preview dir."""
    project = db.get_project(project_id)
    if not project or not project["repo"]:
        return False, "no repo for this project"
    repo = project["repo"]
    if not github_client.enabled(repo):
        return False, "GitHub not configured"
    dest = PREVIEW_DIR / str(project_id) / "repo"
    url = github_client.clone_url(repo, config.GITHUB_TOKEN)

    def _do() -> tuple[bool, str]:
        if dest.exists():
            r = _sh("git", "pull", "--ff-only", cwd=dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            r = _sh("git", "clone", "--depth", "1", url, str(dest))
        if r.returncode != 0:
            return False, f"git failed: {r.stderr[-300:]}"
        return True, "synced"

    ok, note = await asyncio.to_thread(_do)
    if not ok:
        return False, note
    if preview_root(project_id) is None:
        return False, ("synced, but no static index.html found at repo root, /docs, or "
                       "/web — this looks like a server app, not a static site. Use the "
                       "cloud deploy for server apps.")
    return True, "ready"
