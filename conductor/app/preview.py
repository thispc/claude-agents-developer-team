"""Self-hosted preview of a project's built static site, served under our own URL
at /preview/{project_id}/. We clone/pull the repo's default branch into a preview
dir and serve it read-only — sandboxed static files, no access to the control plane.

Only works for static sites (an index.html at repo root, /docs, or /web). Server
apps can't be previewed this way (they'd need to run) — use the DOKS deploy for those.
"""

import asyncio
import shutil
import subprocess
from pathlib import Path

from . import config, db, github_client

PREVIEW_DIR = Path(config._env("PREVIEW_DIR", str(config.ROOT / "previews")))
# Build OUTPUT first, source last.
#
# The order used to start at the repo root, and a bundler's project has an
# index.html there — the one that loads src/main.js as a module. Serving that
# unbuilt is a black screen: the browser cannot resolve the module graph, nothing
# draws, and all you see is the dark fallback background the page sets before its
# script runs. Which is exactly what a real run produced after seventeen tasks
# and $24 of agent time.
_STATIC_SUBDIRS = ("dist", "build", "docs", "web", "public", "")


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


def _build_if_needed(dest) -> tuple[bool, str]:
    """Run the project's build when it declares one.

    A Vite/webpack project is not servable as source: its index.html references
    a module graph the bundler resolves. Previewing the raw checkout produced a
    black screen and no error anyone could see, because the page renders its own
    dark background before the script that fails.

    Only runs when package.json declares a build AND there is no output already.
    Returns (built, error) — an error is fatal to the preview, because serving the
    source instead would show that same black screen and call it success.
    """
    import json as _json
    pkg = dest / "package.json"
    if not pkg.exists():
        return False, ""
    try:
        scripts = (_json.loads(pkg.read_text()).get("scripts") or {})
    except Exception:
        return False, ""
    if "build" not in scripts:
        return False, ""
    if any((dest / d / "index.html").exists() for d in ("dist", "build")):
        return True, ""
    if not shutil.which("npm"):
        return False, ("this project needs `npm run build` before it can be shown, "
                       "and npm is not installed on the server")
    inst = _sh("npm", "install", "--no-audit", "--no-fund", cwd=dest)
    if inst.returncode != 0:
        return False, f"npm install failed: {(inst.stderr or inst.stdout)[-300:]}"
    r = _sh("npm", "run", "build", cwd=dest)
    if r.returncode != 0:
        return False, f"the project's build failed: {(r.stderr or r.stdout)[-400:]}"
    _relativise_assets(dest)
    return True, ""


def _relativise_assets(dest) -> None:
    """Make the built page work when it is not served from the domain root.

    Bundlers emit absolute asset paths by default — `/assets/index-abc.js` — which
    assume the site owns `/`. A preview is mounted at `/preview/<id>/`, so every
    one of those 404s, no script runs, and the page shows the dark background it
    sets before its own code loads. That is precisely the black screen a real run
    produced: the bundle was built, served, and asking for files one directory
    above where they were.

    Rewriting to relative paths in the built output fixes it for any bundler and
    any mount point, without needing to know the base URL at build time or to
    special-case Vite.
    """
    import re as _re
    for out in ("dist", "build"):
        idx = dest / out / "index.html"
        if not idx.exists():
            continue
        html = idx.read_text(errors="ignore")
        fixed = _re.sub(r'((?:src|href)=")/(?!/)', r"\1./", html)
        if fixed != html:
            idx.write_text(fixed)


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

    built, build_note = await asyncio.to_thread(_build_if_needed, dest)
    if not built and build_note:
        return False, build_note
    if preview_root(project_id) is None:
        return False, ("synced, but no static index.html found at repo root, /docs, or "
                       "/web — this looks like a server app, not a static site. Use the "
                       "cloud deploy for server apps.")
    return True, "ready"
