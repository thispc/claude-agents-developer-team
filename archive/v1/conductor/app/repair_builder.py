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

from . import bus, config, logs, selfops, shell, tuning

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
    # Kept under this name and signature (tests and repair.py reach for rb._git);
    # live_tree() is resolved at call time so tests can repoint selfops.LIVE_TREE.
    return shell.git(*args, cwd=cwd or live_tree(), check=check)


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
                   max_turns: int, model: str, env: dict, *,
                   mcp_servers: dict | None = None) -> tuple[str, float]:
    """One Claude Agent SDK session (the worker.run_claude_session pattern, minus the teammate
    server). Returns (last text, usd). Raises RuntimeError with the SDK's error text — the
    caller classifies rate limits. Tests monkeypatch THIS function; nothing else spends."""
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                                  TextBlock, query)
    kw = {"mcp_servers": mcp_servers} if mcp_servers else {}
    options = ClaudeAgentOptions(
        system_prompt=system_prompt, model=model, max_turns=max_turns, cwd=str(cwd),
        allowed_tools=list(tools), permission_mode="bypassPermissions",
        env={**env, "GIT_TERMINAL_PROMPT": "0"}, **kw)
    text, usd = "", 0.0
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    text = block.text
        elif isinstance(message, ResultMessage):
            from . import usage
            usd = usage.note_result("repair", model, message)
            # The SDK's own wrapper says "returned an error result: success", which is both
            # alarming and empty. Everything useful is on the message — say that instead.
            if getattr(message, "is_error", False):
                detail = (str(getattr(message, "result", "") or "")[:300]
                          or "; ".join(str(x)[:120] for x in (getattr(message, "errors", None) or []))
                          or f"stop_reason={getattr(message, 'stop_reason', '?')}"
                          f" api_status={getattr(message, 'api_error_status', '?')}")
                raise RuntimeError(f"the session ended in error after "
                                   f"{getattr(message, 'num_turns', '?')} turns: {detail}")
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
    logs.info("git", "worktree_added", f"isolated build tree for {branch}", branch=branch)
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
    "worktree of the devteam repo. EVERY path you read or write is RELATIVE to your working "
    "directory: never use an absolute path, never cd out of it, and never touch the checkout "
    "this worktree was made from — that is the operator's live tree, they are working in it "
    "right now, and edits there land in whatever they commit next. "
    "Make the SMALLEST correct change that completes the task. "
    "Ground rules: never touch devteam.db, any .env file, workspaces/, previews/, deployments/, "
    "deploy secrets, or .devteam-deploy.json; the app must still import; the dashboard stays "
    "build-free (no new dependencies, no CDN); keep the repo's comment style; the FULL test "
    "suite must pass — run the relevant tests yourself before finishing. Do not commit — the "
    "engine commits for you. If you start any long-running process while testing (a server, "
    "a watcher), kill it before you finish — sprint 42's build left a uvicorn running for two "
    "days after its worktree was deleted.\n"
    "IF THE TASK IS BIGGER THAN ONE SESSION, do not attempt all of it. Ten of the last "
    "eighteen failures were sessions that ran out of turns with nothing to show, which costs "
    "the same as finishing and delivers nothing. Pick the smallest coherent FIRST SLICE that "
    "leaves the tree green, do that, and end your last message with 'NEXT: <what remains>'. "
    "A landed slice plus an honest note beats an ambitious diff that never lands.")


def builder_system(crew: dict | None) -> str:
    """BUILDER_SYSTEM as a function of WHO is building.

    None → the anonymous prompt, byte-identical to before — the fallback for a missing
    factor, an unbuilt crew, or offline. With a crew dict, the specialist block goes FIRST:
    identity is the frame the ground rules are read through, not a footnote to them.

    The EXPERIENCE block is omitted entirely when there is none — a specialist with no
    record gets no manufactured authority — and every lesson carries its own numbers, so
    the model can weigh a 1/1 suggestion differently from a 5/5 law.
    """
    if not crew:
        return BUILDER_SYSTEM
    head = []
    traits = ", ".join(f"{k} {v:.2f}" for k, v in list((crew.get("traits") or {}).items())[:3])
    head.append(f"You are {crew.get('name', 'a specialist')}, the crew's {crew.get('factor', '')} "
                f"specialist — {crew.get('brief', '')}."
                + (f" Defining traits: {traits};" if traits else "")
                + (f" you most want {crew['wants']}." if crew.get("wants") else ""))
    if crew.get("experience"):
        head.append("\nYOUR EXPERIENCE (your own record on situations like this task):")
        for e in crew["experience"]:
            head.append(f"- [{e.get('label', '?')}] {e.get('says', '')}")
        head.append("Let it guide the change; if a lesson does not apply, one line in your "
                    "final message saying why.")
    if crew.get("neighbours") and crew.get("ask"):
        names = ", ".join(crew["neighbours"])
        head.append(f"\nTEAMMATES: use the ask_teammate tool to consult {names} — your graph "
                    f"neighbours; the arrows are the org chart, nobody else can hear you. You "
                    f"have {crew.get('consults_cap', 2)} consults; spend them on a real "
                    f"blocker, not reassurance.")
    return "\n".join(head) + "\n\n" + BUILDER_SYSTEM


def consult_server(ask):
    """The one door out of a build session: ask_teammate, answered by a LIVE crew agent.

    Imitates the projects worker's helper server (worker.py) with two deliberate
    differences: no HTTP hop — the conductor holds the crew world in this very process —
    and no separate consult model, because the answerer speaks on ITS OWN model, which is
    the lifeworld's point. `ask` is a callable the ENGINE injects (functools.partial over
    repair.consult with the task bound), which keeps this module free of a circular import:
    rb is the hands, and the engine hands the hands a phone.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool("ask_teammate",
          "Ask a crew teammate for help when you are stuck or unsure. Include what you "
          "tried and the exact error — they cannot see your screen. Leave `who` empty to "
          "reach the neighbour most likely to know. Only your graph neighbours can hear you.",
          {"question": str, "who": str})
    async def ask_teammate(args):
        text = await ask(str(args.get("question", ""))[:4000], str(args.get("who", ""))[:60])
        return {"content": [{"type": "text", "text": text}]}

    return create_sdk_mcp_server(name="crew", version="1.0.0", tools=[ask_teammate])


async def build(task: dict, sprint_no: int, settings: dict, wt: Path | None = None,
                crew: dict | None = None) -> dict:
    """One coding session in a worktree. Returns {branch, worktree, usd}. The engine tracks the
    task dict; a retry passes the existing worktree so evidence accumulates on one branch."""
    branch = task.get("branch") or f"repair/s{sprint_no}-{task['slug']}"
    if wt is None:
        wt = worktree_add(branch, f"s{sprint_no}-{task['slug']}")
    # Stamp the task with what now exists on disk BEFORE the session can throw — the engine
    # holds this same dict and cleans up by it. Without this, a build that dies (max turns,
    # rate limit) leaves an orphaned branch + worktree nobody knows the name of.
    task["branch"], task["worktree"] = branch, str(wt)
    before = _live_fingerprint()
    brief = (f"TASK ({task.get('factor', 'improvement')}): {task['title']}\n\n{task.get('brief', '')}\n"
             + (f"\nACCEPTANCE:\n- " + "\n- ".join(task["acceptance"]) if task.get("acceptance") else "")
             + (f"\nFEEDBACK ON THE PREVIOUS ATTEMPT (address it):\n{task.get('evidence', '')}" if task.get("evidence") else ""))
    tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
    extra = {}
    if crew and crew.get("neighbours") and crew.get("ask"):
        tools.append("mcp__crew__ask_teammate")
        extra["mcp_servers"] = {"crew": consult_server(crew["ask"])}
    _, usd = await _run_sdk(builder_system(crew), brief, wt, tools,
                            int(tuning.get("repair_max_turns")),
                            str(tuning.get("repair_builder_model")), _cred_env(settings),
                            **extra)
    stray, theirs = _escaped(before, wt)
    if theirs:
        # Not the session's doing, and not ours to undo. Worth saying out loud, because it is
        # also why a landing may queue instead of land a minute from now.
        logs.info("sandbox", "live_tree_moved",
                  "the live checkout changed while the session ran — not attributable to it",
                  files=", ".join(theirs[:6]), n=len(theirs))
    if stray:
        # Sprint 11 did exactly this: the session edited the OPERATOR'S checkout instead of its
        # worktree, and since the operator was committing at the time, half-finished unverified
        # work rode into main inside an unrelated commit. Fail loudly and name the files. We do
        # NOT revert them — that tree belongs to a person who may have their own work in it.
        logs.error("sandbox", "escape",
                   "a build session wrote into the live checkout the same files it changed "
                   "in its worktree", files=", ".join(stray[:6]), n=len(stray))
        raise RuntimeError(
            "the session wrote outside its worktree, into the live checkout: "
            + ", ".join(stray[:6]) + (" …" if len(stray) > 6 else "")
            + " — nothing was reverted; check `git status` before committing")
    _git("add", "-A", cwd=wt)
    r = _git("commit", "-m", f"repair s{sprint_no}: {task['title']}", cwd=wt, check=False)
    if r.returncode != 0 and "nothing to commit" in (r.stdout + r.stderr):
        # On a FIRST build, no changes is a dead session. On a retry, the branch may already
        # hold green work — and a session that read the feedback, examined its own change and
        # stood by it will legitimately write nothing. Failing that threw away landed-ready
        # work on the review gate's first live outing: the builder's defence was treated as
        # death. The suite ahead is still the hard gate either way.
        prior = _git("rev-list", "--count", "main..HEAD", cwd=wt, check=False).stdout.strip()
        if prior.isdigit() and int(prior) > 0:
            logs.info("session", "stood_by_change",
                      f"the session changed nothing but the branch holds {prior} commit(s) — "
                      "treating it as the builder standing by its work", task=task["title"])
            return {"branch": branch, "worktree": str(wt), "usd": usd, "stood_by": True}
        raise RuntimeError("the session changed nothing")
    return {"branch": branch, "worktree": str(wt), "usd": usd}


def _live_fingerprint() -> dict[str, str]:
    """What the operator's own checkout looks like right now: path -> porcelain status code.

    The worktree is the sandbox, but nothing enforces it — a session with Bash can write
    anywhere. So we photograph the live tree before the session and compare after; that is
    the only honest way to find out whether the sandbox held."""
    out: dict[str, str] = {}
    try:
        # -uall: plain --porcelain collapses new files into "conductor/", and the operator
        # needs to know WHICH file appeared in their tree, not which directory.
        r = _git("status", "--porcelain", "-uall", check=False)
        for ln in r.stdout.splitlines():
            if len(ln) > 3:
                out[ln[3:].strip()] = ln[:2]
    except Exception:
        pass
    return out


def _escaped(before: dict[str, str], wt: Path | None = None) -> tuple[list[str], list[str]]:
    """(wrote_outside, changed_by_someone_else) — the live-tree paths that moved during the
    session, split by whether the SESSION is plausibly responsible.

    The first version of this blamed the session for every change, which is wrong the moment
    a human is editing the same checkout: sprints 13 and 17 were failed for files the
    operator was working on at that instant, and the crew's finished work was thrown away
    for it. The evidence that separates them is cheap — a session that wrote a file outside
    its worktree almost always wrote it INSIDE too, because the file is what it was working
    on. Overlap with the worktree's own diff is therefore attributable; anything else is
    somebody else's edit and is reported, not punished.
    """
    after = _live_fingerprint()
    moved = sorted(p for p, code in after.items()
                   if not p.startswith(".repair/") and before.get(p) != code)
    mine: set[str] = set()
    if wt:
        r = _git("diff", "--name-only", "main...HEAD", cwd=wt, check=False)
        mine = {ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()}
        r2 = _git("status", "--porcelain", "-uall", cwd=wt, check=False)
        mine |= {ln[3:].strip() for ln in (r2.stdout or "").splitlines() if len(ln) > 3}
    return [p for p in moved if p in mine], [p for p in moved if p not in mine]


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


def landable(branch: str) -> dict:
    """Can this branch land RIGHT NOW, and if not, what would fix it — asked without doing
    anything. The queue used to offer Approve unconditionally, so pressing it was the only
    way to discover that the answer was no, and the failure arrived as a toast that vanished.
    Same two gates `land` enforces, in the same order."""
    if _git("status", "--porcelain").stdout.strip():
        return {"ok": False, "why": "your own tree has uncommitted changes",
                "fix": "commit or stash your work, then approve", "remedy": ""}
    if _git("merge-base", "--is-ancestor", "main", branch, check=False).returncode != 0:
        return {"ok": False, "why": "main moved on since this branch was cut",
                "fix": "rebuild it onto today's main and re-run the suite",
                "remedy": "rebuild"}
    return {"ok": True, "why": "", "fix": "", "remedy": ""}


def rebuild(branch: str, wt: str | Path | None) -> dict:
    """Bring a stale branch onto today's main so it can land.

    A branch cut yesterday is not wrong, it is just behind — and "needs a rebuild" with no
    way to rebuild it is a dead end. Rebase rather than merge, so the branch stays ONE commit
    and landing stays one squash and one revert handle. A conflict is reported, not guessed
    at: that is a decision for whoever wrote the conflicting code.
    """
    wtp = Path(wt) if wt else None
    if not wtp or not wtp.exists():
        return {"ok": False, "reason": "its worktree is gone — discard it and let the crew redo the task"}
    _try_git("rebase", "--abort", cwd=wtp)
    r = _git("rebase", "main", cwd=wtp, check=False)
    if r.returncode != 0:
        _try_git("rebase", "--abort", cwd=wtp)
        return {"ok": False, "reason": f"conflicts with main: {(r.stderr or r.stdout)[:200]}"}
    return {"ok": True, "branch": branch, "worktree": str(wtp)}


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
