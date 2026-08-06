"""Self-repair's hands — everything that touches disk or a model for real.

The engine (repair.py) is pure state machine; THIS module is the executor: it scouts the live
tree read-only, builds in disposable git worktrees under LIVE_TREE/.repair (gitignored, so the
running tree never reads dirty and the workspace pruner never sees them), verifies by running
the platform's own full test suite inside the worktree, and lands green branches as single
squash commits (one commit = one `git revert` rollback handle).

This module is also the TAILSCALE SEAM: repair.py only ever calls scout/build/verify/land/
revert/discard. Running the crew on a remote VM later means swapping this module's internals
for a remote executor — the engine, routes and UI never change.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import bus, config, selfops, tuning

REPAIR_DIR_NAME = ".repair"

# Paths a repair change may NEVER touch. Checked on the branch diff BEFORE verification —
# a violation fails the task outright, whatever the tests say.
PROTECTED = ("devteam.db", ".env", "workspaces/", "previews/", "deployments/",
             ".devteam-deploy.json", "deploy/secrets", ".repair/")


def live_tree() -> Path:
    return selfops.LIVE_TREE


def repair_dir() -> Path:
    d = live_tree() / REPAIR_DIR_NAME
    d.mkdir(exist_ok=True)
    return d


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd or live_tree()),
                          capture_output=True, text=True, timeout=120, check=check)


def _try_git(*args: str, cwd: Path | None = None) -> None:
    try:
        _git(*args, cwd=cwd, check=False)
    except Exception:
        pass


def _cred_env(settings: dict) -> dict:
    """Map owner settings → the env an SDK subprocess needs — exactly providers._anthropic's
    rule: explicit key wins, then the OAuth token, else inherit the ambient CLI login."""
    if settings.get("anthropic_api_key"):
        return {"ANTHROPIC_API_KEY": settings["anthropic_api_key"], "CLAUDE_CODE_OAUTH_TOKEN": ""}
    if settings.get("claude_oauth_token"):
        return {"CLAUDE_CODE_OAUTH_TOKEN": settings["claude_oauth_token"], "ANTHROPIC_API_KEY": ""}
    return {}


async def _run_sdk(system_prompt: str, prompt: str, cwd: Path, tools: list[str],
                   max_turns: int, model: str, env: dict) -> tuple[str, float]:
    """One Claude Agent SDK session (the worker.run_claude_session pattern, minus the teammate
    server). Returns (last text, usd). Raises RuntimeError with the SDK's error text — the
    caller classifies rate limits. Tests monkeypatch THIS function; nothing else spends."""
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                                  TextBlock, query)
    options = ClaudeAgentOptions(
        system_prompt=system_prompt, model=model, max_turns=max_turns, cwd=str(cwd),
        allowed_tools=list(tools), permission_mode="bypassPermissions",
        env={**env, "GIT_TERMINAL_PROMPT": "0"})
    text, usd = "", 0.0
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    text = block.text
        elif isinstance(message, ResultMessage):
            usd = message.total_cost_usd or 0.0
    if not text:
        raise RuntimeError("the session produced no output")
    return text, usd


def _json_block(text: str, opener: str, closer: str):
    try:
        return json.loads(text[text.index(opener):text.rindex(closer) + 1])
    except Exception:
        return None


SCOUT_SYSTEM = (
    "You are the SCOUT of the platform's own IT crew, surveying THIS repository read-only. "
    "You have a LIMITED turn budget — sample strategically (Glob for shape, Grep for smells, "
    "Read only the most promising files); when about half your budget is spent, STOP exploring "
    "and emit your findings. An incomplete survey delivered beats a complete one that never "
    "returns. For each FACTOR below, find 1-3 small, concrete, high-value improvements to this "
    "codebase (the devteam platform you are standing in). Favour changes a single focused "
    "session can finish; never propose touching devteam.db, .env, workspaces/, deploy secrets, "
    "or the test gates' pinned counts. Your FINAL message must be ONLY a JSON array of "
    '{"factor": <factor id>, "title": <short>, "files": [paths], "why": <one line>, '
    '"size": "S"|"M"} — nothing else.')


async def scout(factors: list[dict], settings: dict) -> dict:
    """One read-only survey session over the live tree → candidate improvements per factor."""
    lenses = "\n".join(f"- {f['id']}: {f['name']} — {f['brief']}" for f in factors)
    model = str(tuning.get("repair_builder_model"))
    text, usd = await _run_sdk(
        SCOUT_SYSTEM, f"FACTORS:\n{lenses}\n\nSurvey the repo and return the JSON array.",
        live_tree(), ["Read", "Glob", "Grep"], int(tuning.get("repair_scout_max_turns")),
        model, _cred_env(settings))
    cands = _json_block(text, "[", "]") or []
    ids = {f["id"] for f in factors}
    clean = [{"factor": str(c.get("factor", "")), "title": str(c.get("title", ""))[:140],
              "files": [str(x)[:200] for x in (c.get("files") or [])][:8],
              "why": str(c.get("why", ""))[:240], "size": str(c.get("size", "S"))[:2]}
             for c in cands if isinstance(c, dict) and c.get("factor") in ids and str(c.get("title", "")).strip()]
    digest = "\n".join(f"[{c['factor']}] {c['title']} — {c['why']}" for c in clean[:24])
    return {"digest": digest, "candidates": clean, "usd": usd, "model": model}


def worktree_add(branch: str, name: str) -> Path:
    """A fresh worktree on a fresh branch off main, with the live venv symlinked in so the
    verification step finds a real interpreter (worktrees don't carry gitignored dirs)."""
    wt = repair_dir() / name
    _try_git("worktree", "prune")
    _try_git("worktree", "remove", "--force", str(wt))
    _try_git("branch", "-D", branch)
    _git("worktree", "add", "-b", branch, str(wt), "main")
    venv = live_tree() / ".venv"
    link = wt / ".venv"
    if venv.exists() and not link.exists():
        link.symlink_to(venv)
        # A SYMLINK named .venv is not matched by a ".venv/" gitignore pattern (that matches
        # directories only) — exclude it in the worktree's local ignore file, or the build
        # commit (git add -A) would land a stray symlink onto main.
        import os as _os
        ex = _git("rev-parse", "--git-path", "info/exclude", cwd=wt).stdout.strip()
        exp = Path(ex) if _os.path.isabs(ex) else wt / ex
        exp.parent.mkdir(parents=True, exist_ok=True)
        with open(exp, "a") as fh:
            fh.write("\n.venv\n")
    return wt


def discard(branch: str, wt: str | Path | None) -> None:
    if wt:
        _try_git("worktree", "remove", "--force", str(wt))
    _try_git("worktree", "prune")
    _try_git("branch", "-D", branch)


BUILDER_SYSTEM = (
    "You are a senior engineer on the platform's own IT crew, working in a disposable git "
    "worktree of the devteam repo. Make the SMALLEST correct change that completes the task. "
    "Ground rules: never touch devteam.db, any .env file, workspaces/, previews/, deployments/, "
    "deploy secrets, or .devteam-deploy.json; the app must still import; the dashboard stays "
    "build-free (no new dependencies, no CDN); keep the repo's comment style; the FULL test "
    "suite must pass — run the relevant tests yourself before finishing. Do not commit — the "
    "engine commits for you.")


async def build(task: dict, sprint_no: int, settings: dict, wt: Path | None = None) -> dict:
    """One coding session in a worktree. Returns {branch, worktree, usd}. The engine tracks the
    task dict; a retry passes the existing worktree so evidence accumulates on one branch."""
    branch = task.get("branch") or f"repair/s{sprint_no}-{task['slug']}"
    if wt is None:
        wt = worktree_add(branch, f"s{sprint_no}-{task['slug']}")
    # Stamp the task with what now exists on disk BEFORE the session can throw — the engine
    # holds this same dict and cleans up by it. Without this, a build that dies (max turns,
    # rate limit) leaves an orphaned branch + worktree nobody knows the name of.
    task["branch"], task["worktree"] = branch, str(wt)
    brief = (f"TASK ({task.get('factor', 'improvement')}): {task['title']}\n\n{task.get('brief', '')}\n"
             + (f"\nACCEPTANCE:\n- " + "\n- ".join(task["acceptance"]) if task.get("acceptance") else "")
             + (f"\nPREVIOUS ATTEMPT'S TEST FAILURES (fix these):\n{task.get('evidence', '')}" if task.get("evidence") else ""))
    _, usd = await _run_sdk(BUILDER_SYSTEM, brief, wt,
                            ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
                            int(tuning.get("repair_max_turns")),
                            str(tuning.get("repair_builder_model")), _cred_env(settings))
    _git("add", "-A", cwd=wt)
    r = _git("commit", "-m", f"repair s{sprint_no}: {task['title']}", cwd=wt, check=False)
    if r.returncode != 0 and "nothing to commit" in (r.stdout + r.stderr):
        raise RuntimeError("the session changed nothing")
    return {"branch": branch, "worktree": str(wt), "usd": usd}


def protected_violation(wt: str | Path) -> str | None:
    """The post-hoc guard: any diffed path matching PROTECTED fails the task before verify."""
    r = _git("diff", "--name-only", "main...HEAD", cwd=Path(wt))
    for name in r.stdout.splitlines():
        n = name.strip()
        if n.endswith(".db") or any(p in n for p in PROTECTED):
            return n
    return None


async def verify(wt: str | Path) -> dict:
    """Run the platform's OWN full gate-set inside the worktree, reusing the worker's tested
    verification machinery. The worker module reads env at import and emits over HTTP — so:
    lazy import with harmless env defaults, and rebind its emit to our bus first."""
    for k, v in {"TASK_ID": "0", "PROJECT_ID": "0", "CONDUCTOR_URL": "http://127.0.0.1:0",
                 "WORKER_TOKEN": "", "ROLE": "repair", "BRANCH": "", "REPO": ""}.items():
        os.environ.setdefault(k, v)
    wdir = str(live_tree() / "worker")
    if wdir not in sys.path:
        sys.path.insert(0, wdir)
    w = importlib.import_module("worker")
    w.emit = lambda kind, payload="": bus.emit(0, None, "repair", f"verify_{kind}",
                                               {"detail": str(payload)[:500]})
    try:
        w.VERIFY_TIMEOUT = int(tuning.get("repair_verify_timeout_s"))
    except Exception:
        pass
    return await asyncio.to_thread(w.run_verification, Path(wt))


def land(branch: str, title: str) -> dict:
    """Squash-merge a green branch into the live tree's main. Refuses on a dirty tree (the
    owner may be mid-edit) — the engine queues instead. One commit → one revert handle."""
    if _git("status", "--porcelain").stdout.strip():
        return {"ok": False, "reason": "live tree has uncommitted changes — queued instead"}
    if _git("merge-base", "--is-ancestor", "main", branch, check=False).returncode != 0:
        return {"ok": False, "reason": "main moved since this branch was cut — needs a rebuild"}
    r = _git("merge", "--squash", branch, check=False)
    if r.returncode != 0:
        _try_git("merge", "--abort")
        _try_git("reset", "--hard", "HEAD")
        return {"ok": False, "reason": f"merge conflict: {(r.stderr or r.stdout)[:200]}"}
    c = _git("commit", "-m", f"repair: {title}\n\n[devteam self-repair]", check=False)
    if c.returncode != 0:
        _try_git("reset", "--hard", "HEAD")
        return {"ok": False, "reason": "nothing to land"}
    sha = _git("rev-parse", "HEAD").stdout.strip()
    files = [f for f in _git("diff", "--name-only", "HEAD~1..HEAD").stdout.splitlines() if f.strip()]
    return {"ok": True, "sha": sha, "files": files}


def revert(sha: str) -> dict:
    """Undo one landed repair commit. Safe by construction: every landing is a single squash."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha or ""):
        return {"ok": False, "reason": "not a commit hash"}
    r = _git("revert", "--no-edit", sha, check=False)
    if r.returncode != 0:
        _try_git("revert", "--abort")
        return {"ok": False, "reason": (r.stderr or r.stdout)[:200]}
    return {"ok": True, "sha": _git("rev-parse", "HEAD").stdout.strip()}
