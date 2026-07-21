"""Run a candidate build of this platform beside the live one, safely.

Self-repair changes the app you are using. A diff tells you the code is plausible;
it does not tell you the app still works. So before `redeploy()` overwrites the
live tree, the candidate is started here: same code, different everything else.

Four isolations, each one deliberate:

- **A git worktree**, not a checkout. The live tree is never switched to another
  branch, so a sandbox can never leave the running platform on the wrong commit.
- **Its own database.** A fresh file, seeded with demo data. The candidate cannot
  read, migrate or corrupt real projects — and a schema migration that destroys
  data does it to a throwaway file, which is precisely the failure worth catching.
- **No credentials.** Every secret is blanked in the child environment. Combined
  with DEMO_MODE the sandbox physically cannot spend a run or reach GitHub.
- **Its own port.** Both run at once, so you compare rather than remember.

Everything lives under `.sandbox/`, and stopping removes the worktree.
"""

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from . import config, db, selfops

SANDBOX_DIR = selfops.LIVE_TREE / ".sandbox"
TREE = SANDBOX_DIR / "tree"
DB = SANDBOX_DIR / "sandbox.db"
PID_FILE = SANDBOX_DIR / "sandbox.json"
LOG = SANDBOX_DIR / "sandbox.log"

PORT_START = int(config._env("SANDBOX_PORT", "8700"))
BOOT_TIMEOUT = 60


def _sh(*cmd: str, cwd: Path | None = None, timeout: int = 120):
    return subprocess.run(cmd, cwd=str(cwd or selfops.LIVE_TREE),
                          capture_output=True, text=True, timeout=timeout)


# Never copied into a sandbox: build output and dependency trees (huge, and
# rebuilt anyway), and anything holding real state or secrets.
SKIP = {".git", ".sandbox", "node_modules", ".venv", "venv", "env", "__pycache__",
        ".pytest_cache", ".mypy_cache", ".tox", "dist", "build", ".next",
        "workspaces", "previews", "deployments", ".claude"}


def snapshot(src: Path, dest: Path) -> tuple[bool, str]:
    """Copy a working tree exactly as it is on disk, uncommitted work included.

    A git worktree can only ever show you a commit. That is the wrong tool for
    "does this change work?", because the change you want to try is usually the
    one still sitting in an agent's workspace — unpushed, sometimes uncommitted.
    Requiring a commit first turns a 10-second check into a round trip, which is
    exactly the loop a QA role needs to be fast.
    """
    if not src.exists():
        return False, f"{src} does not exist"
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src, dest, symlinks=True,
                        ignore=shutil.ignore_patterns(*SKIP, "*.db", "*.db-*", "*.sock"))
    except Exception as e:
        return False, f"could not copy the tree: {e}"
    return True, "copied the working tree as-is"


def _dirty(root: Path) -> int:
    """How many files differ from HEAD — what a snapshot captures and a ref cannot."""
    r = _sh("git", "status", "--porcelain", cwd=root)
    return len([ln for ln in r.stdout.splitlines() if ln.strip()])


def _free_port() -> int:
    import socket
    for p in range(PORT_START, PORT_START + 100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("no free port for the sandbox")


def _state() -> dict[str, Any]:
    try:
        return json.loads(PID_FILE.read_text())
    except Exception:
        return {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def status() -> dict[str, Any]:
    """What the sandbox is doing right now, adopted across conductor restarts."""
    st = _state()
    if not st or not st.get("pid"):
        return {"running": False}
    if not _alive(st["pid"]):
        return {"running": False, "died": True, "ref": st.get("ref"),
                "log_tail": tail_log()}
    return {"running": True, **st}


def tail_log(lines: int = 40) -> str:
    try:
        return "\n".join(LOG.read_text(errors="ignore").splitlines()[-lines:])
    except Exception:
        return ""


def branches() -> list[dict[str, str]]:
    """Branches a sandbox could be started from — self-repair work, newest first."""
    r = _sh("git", "for-each-ref", "--sort=-committerdate", "--count=40",
            "--format=%(refname:short)\t%(committerdate:relative)\t%(contents:subject)",
            "refs/heads/", "refs/remotes/origin/")
    out, seen = [], set()
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name = parts[0].replace("origin/", "")
        if name in seen or name == "HEAD":
            continue
        seen.add(name)
        out.append({"ref": parts[0], "name": name, "when": parts[1], "subject": parts[2][:90]})
    return out


def _child_env(port: int) -> dict[str, str]:
    """The candidate's environment: everything it needs, nothing it could misuse.

    Secrets are blanked rather than omitted — the child inherits os.environ, so an
    unset variable is the operator's real one. That distinction has already caused
    one credential leak in this codebase; it is not repeated here.
    """
    blank = {k: "" for k in (
        "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY", "GEMINI_KEY",
        "OPENAI_API_KEY", "GITHUB_TOKEN", "GITHUB_REPO", "DIGITALOCEAN_API_TOKEN",
        "APPS_DOMAIN", "SELF_REPO", "SELFREPAIR_USERS")}
    # HOME is redirected, not just CLAUDE_CONFIG_DIR. The candidate is code you
    # have not run before — possibly an OLD commit that predates DEMO_MODE and so
    # honours none of the guards below. Isolation therefore has to hold from the
    # parent's side alone: config._has_cli_login() probes ~/.claude and the macOS
    # keychain, both reached through HOME, so a sandbox pointed at an old branch
    # could otherwise still find the operator's subscription login.
    home = SANDBOX_DIR / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        **blank,
        "HOME": str(home),
        "DEMO_MODE": "1",
        # The SHAPE of the parent's auth, never the secret behind it. Without this
        # the candidate reports auth "none" and the dashboard renders dollars where
        # the live build renders agent runs — a difference in the thing under test.
        "DEMO_AUTH_MODE": config.auth_mode(),
        "DB_PATH": str(DB),
        "PORT": str(port),
        "LAUNCHER": "local",
        "WORKSPACES_DIR": str(SANDBOX_DIR / "workspaces"),
        "CLAUDE_CONFIG_DIR": str(SANDBOX_DIR / "claude-cfg"),
        "CONDUCTOR_URL": f"http://127.0.0.1:{port}",
        "WORKER_TOKEN": "sandbox-token",
        "ROOT_USERNAME": "root",
        "ROOT_PASSWORD": "sandbox",
        "PYTHONPATH": str(TREE / "conductor"),
    }


def _wait_healthy(port: int, proc: subprocess.Popen) -> tuple[bool, str]:
    deadline = time.time() + BOOT_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            return False, f"the candidate exited immediately (code {proc.returncode})"
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=3)
            if r.status_code == 200:
                return True, "healthy"
        except Exception:
            pass
        time.sleep(1)
    return False, f"no healthy response within {BOOT_TIMEOUT}s"


def _safe_ref(ref: str) -> bool:
    return bool(ref) and all(c.isalnum() or c in "/-_." for c in ref) and ".." not in ref


def sources() -> list[dict[str, Any]]:
    """Everywhere a candidate build can come from, most immediate first."""
    out: list[dict[str, Any]] = [{
        "id": "live", "kind": "live", "label": "This working tree — as it is now",
        "detail": (f"{_dirty(selfops.LIVE_TREE)} uncommitted change(s) included"
                   if _dirty(selfops.LIVE_TREE) else "currently identical to HEAD"),
    }]
    # A worker's checkout: the fastest thing to try, because it is where an agent's
    # work actually lives before it is pushed anywhere.
    ws = config.WORKSPACES_DIR
    if ws.exists():
        import re as _re
        for d in sorted(ws.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
            repo = d / "repo"
            if not repo.is_dir():
                continue
            m = _re.match(r"task-(\d+)", d.name)
            t = db.get_task(int(m.group(1))) if m else None
            if not t:
                continue
            out.append({"id": f"workspace:{d.name}", "kind": "workspace", "path": str(repo),
                        "label": f"Agent workspace — #{t['seq']} {t['role']}",
                        "detail": f"{t['title'][:60]} · {_dirty(repo)} uncommitted"})
            if len(out) > 8:
                break
    for b in branches()[:12]:
        out.append({"id": f"ref:{b['ref']}", "kind": "ref", "ref": b["ref"],
                    "label": f"Branch — {b['name']}",
                    "detail": f"{b['subject']} ({b['when']})"})
    return out


def start(source: str) -> dict[str, Any]:
    """Boot a candidate build. `source` is an id from sources().

    Three shapes, and the first two are the point: you should not have to commit
    anything to find out whether a change works.
    """
    stop()
    SANDBOX_DIR.mkdir(exist_ok=True)
    shutil.rmtree(TREE, ignore_errors=True)
    _sh("git", "worktree", "prune")

    kind, _, rest = source.partition(":")
    if kind == "live":
        ok, note = snapshot(selfops.LIVE_TREE, TREE)
        if not ok:
            return {"ok": False, "error": note}
        origin, dirty = "this working tree", _dirty(selfops.LIVE_TREE)
    elif kind == "workspace":
        if "/" in rest or ".." in rest:
            return {"ok": False, "error": f"refusing {rest!r} as a workspace name"}
        src = config.WORKSPACES_DIR / rest / "repo"
        ok, note = snapshot(src, TREE)
        if not ok:
            return {"ok": False, "error": note}
        origin, dirty = f"agent workspace {rest}", _dirty(src)
    elif kind == "ref":
        if not _safe_ref(rest):
            return {"ok": False, "error": f"refusing to use {rest!r} as a git ref"}
        r = _sh("git", "worktree", "add", "--detach", str(TREE), rest)
        if r.returncode != 0:
            return {"ok": False, "error": f"could not check out {rest}: {r.stderr[-300:]}"}
        origin, dirty = f"branch {rest}", 0
    else:
        return {"ok": False, "error": f"unknown source {source!r}"}

    if not (TREE / "conductor" / "app" / "main.py").exists():
        return {"ok": False, "error": "that source does not look like a devteam checkout "
                                      "— conductor/app/main.py is missing"}
    head = _sh("git", "rev-parse", "--short", "HEAD", cwd=TREE).stdout.strip() or "(no git)"
    subject = _sh("git", "log", "-1", "--pretty=%s", cwd=TREE).stdout.strip() or origin
    ref = source

    DB.unlink(missing_ok=True)          # a fresh database every time, by design
    port = _free_port()
    env = _child_env(port)
    python = str(selfops.LIVE_TREE / ".venv" / "bin" / "python")
    if not Path(python).exists():
        python = "python3"
    LOG.parent.mkdir(exist_ok=True)
    logf = open(LOG, "w")
    proc = subprocess.Popen(
        [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(TREE / "conductor"), env=env, stdout=logf, stderr=subprocess.STDOUT,
        start_new_session=True)

    ok, note = _wait_healthy(port, proc)
    if not ok:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        return {"ok": False, "error": note, "log_tail": tail_log(), "ref": source}

    st = {"pid": proc.pid, "port": port, "ref": ref, "commit": head,
          "subject": subject, "origin": origin, "dirty": dirty,
          "url": f"http://127.0.0.1:{port}/", "started_at": time.time()}
    PID_FILE.write_text(json.dumps(st, indent=2))
    return {"ok": True, **st}


def stop() -> dict[str, Any]:
    """Kill the sandbox and remove its worktree. Safe to call when none is running."""
    st = _state()
    killed = False
    if st.get("pid") and _alive(st["pid"]):
        try:
            os.killpg(os.getpgid(st["pid"]), signal.SIGTERM)
            for _ in range(20):
                if not _alive(st["pid"]):
                    break
                time.sleep(0.2)
            if _alive(st["pid"]):
                os.killpg(os.getpgid(st["pid"]), signal.SIGKILL)
            killed = True
        except Exception:
            pass
    PID_FILE.unlink(missing_ok=True)
    if TREE.exists():
        _sh("git", "worktree", "remove", "--force", str(TREE))
        shutil.rmtree(TREE, ignore_errors=True)
    _sh("git", "worktree", "prune")
    return {"ok": True, "killed": killed}
