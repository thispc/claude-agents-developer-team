"""Worker agent entrypoint.

Runs as a k8s Job (or local subprocess). Contract (env vars, set by the conductor's
launcher): TASK_ID, PROJECT_ID, ROLE, TASK_TITLE, TASK_DESCRIPTION, TASK_FEEDBACK,
BRANCH, REPO, GITHUB_TOKEN, GIT_WEB, PROVIDER, MODEL, MAX_TURNS, CONDUCTOR_URL,
WORKER_TOKEN, ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY.

Flow: clone repo -> checkout task branch -> run a headless agent session with the
role prompt -> commit & push -> POST the final report to the conductor.

PROVIDER selects the engine and nothing else. "anthropic" (the default, and what
every project created before this existed sends) is the Claude Agent SDK path,
unchanged. Anything else goes through agentic.py, which drives that provider's
own tool calling around the same clone. Everything downstream of the session —
verification, commit, push, the report — is shared, which is the reason the
conductor never has to ask who did the work.
"""

import asyncio
import json
import fnmatch
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

TASK_ID = int(os.environ["TASK_ID"])
PROJECT_ID = int(os.environ["PROJECT_ID"])
ROLE = os.environ.get("ROLE", "backend")
TITLE = os.environ.get("TASK_TITLE", "")
DESCRIPTION = os.environ.get("TASK_DESCRIPTION", "")
FEEDBACK = os.environ.get("TASK_FEEDBACK", "")
BRANCH = os.environ.get("BRANCH", f"task/{TASK_ID}")
REPO = os.environ.get("REPO", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
# The git host to clone from and push to. Defaults to github.com, so a worker
# that is never told otherwise behaves exactly as it always has.
GIT_WEB = os.environ.get("GIT_WEB", "https://github.com").rstrip("/")
PROVIDER = (os.environ.get("PROVIDER") or "anthropic").strip().lower()
MODEL = os.environ.get("MODEL", "claude-haiku-4-5")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "40"))
CONDUCTOR_URL = os.environ.get("CONDUCTOR_URL", "http://localhost:8000").rstrip("/")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
WORKDIR = Path(os.environ.get("WORKDIR", "/work"))

AGENTS_DIR = Path(os.environ.get("AGENTS_DIR", str(Path(__file__).resolve().parent.parent / "agents")))
SOURCE = f"worker:{ROLE}"


def post(path: str, body: dict) -> None:
    try:
        httpx.post(f"{CONDUCTOR_URL}{path}", json=body,
                   headers={"X-Worker-Token": WORKER_TOKEN}, timeout=15)
    except Exception as e:
        print(f"[worker] failed to reach conductor: {e}", file=sys.stderr)


def fetch(path: str, params: dict) -> dict:
    """Read something from the conductor. Returns {} on any failure — a worker that
    cannot look up context should carry on with less of it, not die holding a task."""
    try:
        r = httpx.get(f"{CONDUCTOR_URL}{path}", params=params,
                      headers={"X-Worker-Token": WORKER_TOKEN}, timeout=15)
        return r.json() if r.status_code == 200 else {}
    except Exception as e:
        print(f"[worker] could not read {path}: {e}", file=sys.stderr)
        return {}


def emit(kind: str, payload: str) -> None:
    post("/internal/events", {"project_id": PROJECT_ID, "task_id": TASK_ID,
                              "source": SOURCE, "kind": kind, "payload": payload[:4000]})


CONTENDER_ID = int(os.environ.get("CONTENDER_ID", "0") or 0)


def report(status: str, text: str, cost: float, verification: dict | None = None) -> None:
    post("/internal/report", {"project_id": PROJECT_ID, "task_id": TASK_ID,
                              "status": status, "report": text[:12000], "cost_usd": cost,
                              "contender_id": CONTENDER_ID,
                              "verification": json.dumps(verification or {})})


VERIFY_TIMEOUT = int(os.environ.get("VERIFY_TIMEOUT", "600"))


# Directories full of other people's tests, or of build output. Walking into them
# is both slow and wrong — a dependency's test suite is not this project's.
_SKIP_DIRS = {"node_modules", "site-packages", "vendor", "dist", "build",
              "__pycache__", "venv", "env", ".tox"}


def _has_tests(repo_dir: Path, patterns: tuple[str, ...]) -> bool:
    """Any test file, at any depth. The old check globbed one level down, so a
    perfectly ordinary layout like sim/dsp/test_ber_sim.py was invisible and the
    project was reported as having no way to check itself."""
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        if any(fnmatch.fnmatch(f, pat) for f in files for pat in patterns):
            return True
    return False


def _python(repo_dir: Path) -> str:
    """The interpreter that can actually import this project's dependencies.

    Agents routinely create a virtualenv and install into it, so the *system*
    interpreter has none of the project's packages. Running bare `python -m pytest`
    then fails with "No module named pytest" — which the harness would record as a
    FAILED verification and refuse to merge on. A false failure is worse than no
    check at all, because it blocks work that was fine.
    """
    for venv in (".venv", "venv", "env"):
        if (repo_dir / venv / "bin" / "python").exists():
            return f"{venv}/bin/python"
    return "python3"


def detect_verification(repo_dir: Path) -> tuple[str, str] | None:
    """The command that proves this project still works, and how to describe it.

    Deliberately conservative: only returns something when the repo clearly
    declares a way to check itself. A wrong command produces noise, and noise
    trains the manager to ignore the evidence.
    """
    pkg = repo_dir / "package.json"
    if pkg.exists():
        try:
            scripts = (json.loads(pkg.read_text()).get("scripts") or {})
        except Exception:
            scripts = {}
        if "test" in scripts:
            # npm's default placeholder exits 1 and means nothing
            if "no test specified" not in str(scripts.get("test", "")):
                return ("npm test --silent", "npm test")
        if "build" in scripts:
            return ("npm run build", "npm run build")
        if "lint" in scripts:
            return ("npm run lint", "npm run lint")
    if (repo_dir / "pytest.ini").exists() or (repo_dir / "tests").is_dir() or \
            _has_tests(repo_dir, ("test_*.py", "*_test.py")):
        return (f"{_python(repo_dir)} -m pytest -q", "pytest")
    if (repo_dir / "go.mod").exists():
        return ("go test ./...", "go test")
    if (repo_dir / "Cargo.toml").exists():
        return ("cargo test", "cargo test")
    if (repo_dir / "Makefile").exists():
        mk = (repo_dir / "Makefile").read_text(errors="ignore")
        if "\ntest:" in mk or mk.startswith("test:"):
            return ("make test", "make test")

    # Last resort: does the JavaScript even parse?
    #
    # A browser game written as index.html plus game.js declares nothing above,
    # so nothing checked five merged tasks on a real run. `node --check` is not a
    # test and must never be mistaken for one — but a syntax error is exactly the
    # fault that turns a canvas game into a blank screen with a console message
    # nobody sees, and catching that is worth more than catching nothing.
    #
    # Labelled "javascript syntax" precisely so the manager reads it as what it
    # is. A green result here says the file parses, not that the game works.
    js = [f for f in repo_dir.glob("*.js") if f.name != "node_modules"]
    if js and _has(repo_dir, "node"):
        names = " ".join(f'"{f.name}"' for f in sorted(js)[:20])
        return (f"node --check {names}", "javascript syntax")
    return None


def _has(repo_dir: Path, binary: str) -> bool:
    """Whether a command exists here. Checked rather than assumed: proposing a
    check we cannot run would report 'nothing verified this' with an extra step."""
    import shutil
    return shutil.which(binary) is not None


# Which lines in a test run actually name what broke, by toolchain.
#
# The tail of the output is not enough. A long build log pushes the failure names
# out of the window, and "exited 1" tells the manager that something broke without
# telling it what — which is the difference between a grounded judgement and a
# coin flip. Changing only the value function moved published results 7 points;
# changing the search algorithm moved them less than 1. This is the value function.
#
# Deliberately narrow: a pattern that fires on ordinary log noise trains the
# manager to ignore the evidence, which is worse than having none.
FAILURE_PATTERNS = (
    re.compile(r"^FAILED\s+\S+.*$", re.M),                            # pytest short summary
    re.compile(r"^ERROR\s+\S+.*$", re.M),                             # pytest collection error
    re.compile(r"^E\s{3}\w*(?:Error|Exception)\b.*$", re.M),          # pytest assertion detail
    re.compile(r"^\s*[●✕✗]\s+\S.*$", re.M),                           # jest / vitest
    re.compile(r"^\s*--- FAIL: \S+.*$", re.M),                        # go test
    re.compile(r"^error\[E\d+\]:.*$", re.M),                          # rustc
    re.compile(r"^\S+\(\d+,\d+\): error TS\d+:.*$", re.M),            # tsc
)
# "3 failed, 12 passed in 4.2s" / "Tests: 2 failed, 8 passed"
COUNT_PATTERN = re.compile(r"^.*?\b\d+\s+(?:failed|failing)\b.*$", re.M | re.I)


def extract_failures(output: str, limit: int = 12) -> list[str]:
    """The specific things that broke, deduped, in the order the patterns match."""
    seen: set[str] = set()
    out: list[str] = []
    for pat in FAILURE_PATTERNS:
        for line in pat.findall(output or ""):
            norm = " ".join(line.split())[:220]
            if norm and norm not in seen:
                seen.add(norm)
                out.append(norm)
                if len(out) >= limit:
                    return out
    return out


def failure_headline(output: str) -> str:
    """The tool's own count line, e.g. '3 failed, 12 passed in 4.21s'."""
    found = COUNT_PATTERN.findall(output or "")
    return " ".join(found[-1].split())[:200] if found else ""


def run_verification(repo_dir: Path) -> dict:
    """Run the project's own checks and report the RAW result.

    This runs in the worker process, not in the agent session, precisely so the
    model cannot summarise, soften or invent the outcome. A manager judging prose
    is judging a claim; a manager judging an exit code is judging evidence.
    """
    found = detect_verification(repo_dir)
    if not found:
        return {"ran": False, "reason": "this project declares no test/build command"}
    cmd, label = found

    # Nothing new to check? Reuse the result rather than paying for it twice.
    #
    # A retry after a rate limit or a session limit re-clones the same branch and
    # re-runs the same suite over the same commit, which cannot produce a different
    # answer and can cost minutes on a shared node. Deliberately narrow: only a
    # PASSING result is reused, and only for the exact commit it was recorded
    # against. A failing result is always re-run, because the whole reason a retry
    # exists is that someone is trying to fix it.
    head = current_commit(repo_dir)
    prior = json.loads(os.environ.get("PRIOR_VERIFICATION", "") or "{}")
    if head and prior.get("ok") and prior.get("commit") == head and prior.get("cmd") == label:
        emit("verify_reused", f"{label} already passed on {head[:8]}; not re-running")
        return {**prior, "reused": True}

    emit("verifying", f"running {label}")
    try:
        r = subprocess.run(cmd, cwd=repo_dir, shell=True, capture_output=True,
                           text=True, timeout=VERIFY_TIMEOUT)
        out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        ok = r.returncode == 0
        res = {"ran": True, "ok": ok, "cmd": label,
               "exit_code": r.returncode, "output": out[-3000:],
               # Which commit this result is about. Without it a stored verification
               # is a claim about "the branch", and the branch moves.
               "commit": current_commit(repo_dir)}
        if not ok:
            # Extracted from the WHOLE output, not the tail we keep — the names of
            # what failed are often scrolled past by the time the run ends.
            res["failures"] = extract_failures(out)
            res["headline"] = failure_headline(out)
        return res
    except subprocess.TimeoutExpired:
        return {"ran": True, "ok": False, "cmd": label, "exit_code": -1,
                "output": f"{label} timed out after {VERIFY_TIMEOUT}s"}
    except Exception as e:
        return {"ran": False, "reason": f"could not run {label}: {e}"}


def current_commit(repo_dir: Path) -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir,
                           capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def sh(*cmd: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def setup_repo() -> Path:
    repo_dir = WORKDIR / "repo"
    if not REPO:
        repo_dir.mkdir(parents=True, exist_ok=True)
        sh("git", "init", "-b", "main", cwd=repo_dir)
        return repo_dir
    url = f"https://x-access-token:{GITHUB_TOKEN}@{GIT_WEB.split('://', 1)[-1]}/{REPO}.git"
    r = sh("git", "clone", url, str(repo_dir))
    if r.returncode != 0:
        raise RuntimeError(f"git clone failed: {r.stderr[-500:]}")
    sh("git", "config", "user.email", "devteam-bot@users.noreply.github.com", cwd=repo_dir)
    sh("git", "config", "user.name", f"devteam {ROLE} agent", cwd=repo_dir)
    # Large pushes over HTTP can fail with "RPC failed; HTTP 400 ... unexpected
    # disconnect" unless the buffer is raised.
    sh("git", "config", "http.postBuffer", "524288000", cwd=repo_dir)
    sh("git", "config", "http.version", "HTTP/1.1", cwd=repo_dir)
    # Reuse the branch on re-runs (request_changes), else create it.
    if sh("git", "checkout", BRANCH, cwd=repo_dir).returncode != 0:
        sh("git", "checkout", "-b", BRANCH, cwd=repo_dir)
    return repo_dir


def commit_and_push(repo_dir: Path) -> tuple[bool, str]:
    sh("git", "add", "-A", cwd=repo_dir)
    committed = sh("git", "commit", "-m", f"[{ROLE}] {TITLE} (task {TASK_ID})", cwd=repo_dir)
    if committed.returncode != 0 and "nothing to commit" in (committed.stdout + committed.stderr):
        # Re-run may have committed mid-session via bash; still try pushing.
        pass
    if not REPO:
        return True, "no remote configured; changes remain in workspace"
    # Push can fail transiently (HTTP 400 / disconnect) — retry before giving up,
    # otherwise completed work gets thrown away.
    last_err = ""
    for attempt in range(4):
        pushed = sh("git", "push", "-u", "origin", BRANCH, cwd=repo_dir)
        if pushed.returncode == 0:
            return True, f"pushed branch {BRANCH}"
        last_err = (pushed.stderr or pushed.stdout)[-400:]
        low = last_err.lower()
        if "up-to-date" in low or "up to date" in low:
            return True, f"branch {BRANCH} already up to date"

        # Non-fast-forward: someone else advanced this branch while we worked (a
        # re-dispatch, or a rival on the same ref). Throwing the work away is the
        # worst answer — rebase our commits on top of theirs and push again.
        if "fast-forward" in low or "fetch first" in low or "rejected" in low:
            emit("push_retry", f"attempt {attempt + 1}: remote moved ahead; rebasing")
            sh("git", "fetch", "origin", BRANCH, cwd=repo_dir)
            reb = sh("git", "rebase", f"origin/{BRANCH}", cwd=repo_dir)
            if reb.returncode != 0:
                # Conflicting histories — keep ours rather than lose the session.
                sh("git", "rebase", "--abort", cwd=repo_dir)
                emit("push_retry", "rebase conflicted; keeping our version")
                forced = sh("git", "push", "--force-with-lease", "-u", "origin", BRANCH,
                            cwd=repo_dir)
                if forced.returncode == 0:
                    return True, f"pushed branch {BRANCH} (force-with-lease after conflict)"
                last_err = (forced.stderr or forced.stdout)[-400:]
            continue          # retry the plain push immediately after a clean rebase

        emit("push_retry", f"attempt {attempt + 1} failed: {last_err[-200:]}")
        # Jittered backoff. Rivals in a contest finish within seconds of each other,
        # so a fixed 3/6/9 schedule marches them straight back into the same
        # collision on every retry — which is how a transient ref lock became three
        # observed push failures on one project.
        time.sleep(3 * (attempt + 1) * (0.5 + random.random()))
    return False, f"git push failed after 4 attempts: {last_err}"


CONSULT_MODEL = os.environ.get("CONSULT_MODEL", "claude-sonnet-5")


def build_helper_server():
    """Lets a worker ask a more capable teammate for help instead of grinding alone.

    This is the collaboration primitive: a cheap model doing the work can escalate a
    *question* (not the whole task) to a stronger model, get a concrete answer, and
    carry on. Two heads on one task, without two agents fighting over the same files.
    """

    @tool("ask_teammate",
          "Ask a named teammate for help when you are stuck, unsure of an approach, "
          "or hitting the same error repeatedly. Include everything they need: what "
          "you're trying to do, what you tried, the exact error, and the relevant code. "
          "They cannot see your screen. Leave `who` empty to reach whoever on the team "
          "has done the most work. Returns their advice.",
          {"question": str, "context": str, "who": str})
    async def ask_teammate(args: dict) -> dict:
        question = str(args.get("question", ""))[:4000]
        ctx = str(args.get("context", ""))[:8000]
        who = str(args.get("who", ""))[:60]

        # Who is actually being asked, and what they have already built. Without
        # this the "teammate" was a clever stranger: it had no idea what anyone on
        # the project had done, so its advice regularly contradicted a decision
        # another agent made an hour earlier — and the asker had no way to tell.
        mate = fetch("/internal/teammate", {"project_id": PROJECT_ID, "name": who,
                                            "exclude_task": TASK_ID})
        name = mate.get("name") or "a senior teammate"
        emit("consult", f"asked {name}: {question[:200]}")

        identity = (
            f"You are {mate['name']}, the {mate['role']} on this team."
            + (f"\n\n{mate['persona']}" if mate.get("persona") else "")
            + (f"\n\nWhat you have built on this project:\n{mate['notes']}"
               if mate.get("notes") else "")
            + ("\n\nYour recent work:\n" + "\n\n".join(
                f"- {w['title']}: {w['report'][:600]}" for w in mate.get("work") or [])
               if mate.get("work") else "")
            if mate.get("found") else
            "You are a senior engineer on this team."
        )
        prompt = (
            f"{identity}\n\n"
            f"A {ROLE} on your team is stuck and asked for your help.\n\n"
            f"THEIR TASK: {TITLE}\n\nTHEIR QUESTION:\n{question}\n\n"
            f"WHAT THEY'VE GOT (code / errors / attempts):\n{ctx}\n\n"
            "Give a direct, concrete answer they can act on immediately: the specific fix, "
            "the exact code, or the precise next step. No pleasantries, no restating their "
            "problem. If they're on the wrong track, say so plainly and give the right one. "
            "If their question touches something you built, say what you decided and why, "
            "so they build on it instead of around it."
        )
        answer = ""
        try:
            async for msg in query(prompt=prompt, options=ClaudeAgentOptions(
                    model=CONSULT_MODEL, max_turns=1,
                    disallowed_tools=["Bash", "Write", "Edit", "Task", "TodoWrite"],
                    permission_mode="bypassPermissions")):
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            answer += b.text
        except Exception as e:
            answer = f"(your teammate could not be reached: {e}; use your best judgement)"
        emit("consult_reply", f"{name}: {answer[:1400]}")
        return {"content": [{"type": "text", "text": answer or "(no answer)"}]}

    return create_sdk_mcp_server(name="team", version="1.0.0", tools=[ask_teammate])


def build_prompt() -> str:
    parts = [f"# Task {TASK_ID}: {TITLE}", "", DESCRIPTION]
    handoff = os.environ.get("HANDOFF_CONTEXT", "")
    if handoff:
        parts += ["", "## Handover from your teammates", handoff]
    if FEEDBACK:
        parts += ["", "## Review feedback on your previous attempt (address all of it):",
                  FEEDBACK]
    prior = os.environ.get("PRIOR_ATTEMPT", "")
    if prior:
        parts += ["", "## Where the previous attempt got to", prior]
    parts += ["", "Work inside the current directory (the repository checkout). "
              "When you are done, end with a final summary message as instructed."]
    return "\n".join(parts)


async def run_claude_session(system_prompt: str, repo_dir: Path) -> tuple[str, float, str | None]:
    """The original Claude Agent SDK session, lifted out of run() unchanged.

    bypassPermissions inside a disposable clone is the deliberate posture: an agent
    that has to ask permission cannot build anything unattended, and there is no
    human at the other end of the prompt.
    """
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=MODEL,
        max_turns=MAX_TURNS,
        cwd=str(repo_dir),
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep",
                       "mcp__team__ask_teammate"],
        mcp_servers={"team": build_helper_server()},
        permission_mode="bypassPermissions",
        env={"GIT_TERMINAL_PROMPT": "0"},
    )

    last_text = ""
    cost = 0.0
    error: str | None = None
    try:
        async for message in query(prompt=build_prompt(), options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        last_text = block.text
                        emit("message", block.text)
                    elif isinstance(block, ToolUseBlock):
                        summary = str(block.input or {})[:300]
                        emit("tool_use", f"{block.name}: {summary}")
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
    except Exception as e:
        error = str(e)
    return last_text, cost, error


PROVIDER_KEY_ENV = {"openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY"}


async def run_agentic_session(system_prompt: str, repo_dir: Path) -> tuple[str, float, str | None]:
    """The same job on OpenAI or Gemini, through their own function calling.

    Returns the same triple as the Claude path so everything after it — verify,
    commit, push, report — cannot tell the difference. A missing key is returned as
    an error rather than raised, so the caller still salvages and pushes whatever
    is in the clone (nothing, in that case) and the boss gets a report saying why.
    """
    import agentic

    key = os.environ.get(PROVIDER_KEY_ENV.get(PROVIDER, ""), "")
    if not key:
        return "", 0.0, (f"no {PROVIDER} credentials reached this worker — add the key "
                         f"in Settings, or move this teammate to a provider you have")
    spend = {"usd": 0.0}
    try:
        last_text, cost = await agentic.run_session(
            provider=PROVIDER, model=MODEL, key=key,
            system=system_prompt + agentic.TOOL_BRIEFING,
            prompt=build_prompt(), repo_dir=repo_dir, max_turns=MAX_TURNS,
            emit=emit, spend=spend)
        return last_text, cost, None
    except Exception as e:
        # The turn limit and a rate limit both arrive here, and their wording is
        # load-bearing: the launcher reads the report to tell a capacity death from
        # a quality failure, and to give a turn-starved retry a bigger budget.
        return "", spend["usd"], str(e)


async def run() -> None:
    emit("agent_status", "running")
    try:
        repo_dir = setup_repo()
    except Exception as e:
        report("failed", f"could not prepare repository: {e}", 0.0)
        return

    role_file = AGENTS_DIR / f"{ROLE}.md"
    if role_file.exists():
        system_prompt = role_file.read_text()
    else:
        # Custom role recruited by the boss with no dedicated prompt file — build a
        # capable generic one so ad-hoc roles (designer, devops, "phd researcher") work.
        system_prompt = (
            f"You are a senior {ROLE} on an autonomous software team. You work alone on one "
            f"task in a fresh clone of the repository; a manager reviews your branch afterwards.\n"
            "- Do exactly what the task describes, to a professional standard. The file paths and "
            "contracts in the task are binding — other team members build against your output.\n"
            "- Actually make it work: run, test, or verify what you produce before finishing.\n"
            "- Commit and push are handled FOR YOU after you finish; leave the working tree in "
            "its final state. Do NOT run `git push`, and do NOT open a pull request (no `gh pr "
            "create`) — the platform opens it and tracks its number. Opening one yourself is "
            "wasted effort and hides the PR from your manager.\n"
            "- End with a short summary of what you did, files touched, how to verify, and anything "
            "you could not do. If you hit work outside your scope, add an `ESCALATION:` section "
            "listing recommended follow-up tasks (role, what, why) for the manager to consider."
        )

    # Who this agent is, appended rather than substituted: the role prompt is the
    # part that has been tuned and tested, and a persona modifies it instead of
    # replacing it. A persona able to override "verify your work before you
    # finish" would be a way to talk the platform out of its own safeguards.
    system_prompt += os.environ.get("AGENT_CONTEXT", "")

    if PROVIDER == "anthropic":
        last_text, cost, error = await run_claude_session(system_prompt, repo_dir)
    else:
        last_text, cost, error = await run_agentic_session(system_prompt, repo_dir)

    if error:
        # Salvage: commit and push anything already written before giving up, so a
        # crash near the end doesn't throw away a session's worth of real work.
        saved, push_note = commit_and_push(repo_dir)
        note = f"(work in progress was pushed: {push_note})" if saved else \
               f"(nothing could be pushed: {push_note})"
        report("failed",
               f"agent session error: {error}\n\n{note}\n\nlast message:\n{last_text}",
               cost)
        return

    # Verify BEFORE reporting, so the manager receives evidence rather than a claim.
    verification = run_verification(repo_dir)
    if verification.get("ran"):
        emit("verified", f"{verification['cmd']} exited {verification['exit_code']}")

    ok, push_note = commit_and_push(repo_dir)
    status = "pushed" if ok else "failed"
    report(status, f"{last_text}\n\n---\n{push_note}", cost, verification)
    emit("agent_status", "finished")


if __name__ == "__main__":
    asyncio.run(run())
