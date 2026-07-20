"""Worker agent entrypoint.

Runs as a k8s Job (or local subprocess). Contract (env vars, set by the conductor's
launcher): TASK_ID, PROJECT_ID, ROLE, TASK_TITLE, TASK_DESCRIPTION, TASK_FEEDBACK,
BRANCH, REPO, GITHUB_TOKEN, MODEL, MAX_TURNS, CONDUCTOR_URL, WORKER_TOKEN,
ANTHROPIC_API_KEY.

Flow: clone repo -> checkout task branch -> run a headless Claude session with the
role prompt -> commit & push -> POST the final report to the conductor.
"""

import asyncio
import os
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
    query,
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


def emit(kind: str, payload: str) -> None:
    post("/internal/events", {"project_id": PROJECT_ID, "task_id": TASK_ID,
                              "source": SOURCE, "kind": kind, "payload": payload[:4000]})


def report(status: str, text: str, cost: float) -> None:
    post("/internal/report", {"project_id": PROJECT_ID, "task_id": TASK_ID,
                              "status": status, "report": text[:12000], "cost_usd": cost})


def sh(*cmd: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def setup_repo() -> Path:
    repo_dir = WORKDIR / "repo"
    if not REPO:
        repo_dir.mkdir(parents=True, exist_ok=True)
        sh("git", "init", "-b", "main", cwd=repo_dir)
        return repo_dir
    url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{REPO}.git"
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
    for attempt in range(3):
        pushed = sh("git", "push", "-u", "origin", BRANCH, cwd=repo_dir)
        if pushed.returncode == 0:
            return True, f"pushed branch {BRANCH}"
        last_err = (pushed.stderr or pushed.stdout)[-400:]
        if "up-to-date" in last_err or "up to date" in last_err:
            return True, f"branch {BRANCH} already up to date"
        emit("push_retry", f"attempt {attempt + 1} failed: {last_err[-200:]}")
        time.sleep(3 * (attempt + 1))
    return False, f"git push failed after 3 attempts: {last_err}"


def build_prompt() -> str:
    parts = [f"# Task {TASK_ID}: {TITLE}", "", DESCRIPTION]
    handoff = os.environ.get("HANDOFF_CONTEXT", "")
    if handoff:
        parts += ["", "## Handover from your teammates", handoff]
    if FEEDBACK:
        parts += ["", "## Review feedback on your previous attempt (address all of it):",
                  FEEDBACK]
    parts += ["", "Work inside the current directory (the repository checkout). "
              "When you are done, end with a final summary message as instructed."]
    return "\n".join(parts)


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
            "- Commit is handled for you after you finish; leave the working tree in its final state.\n"
            "- End with a short summary of what you did, files touched, how to verify, and anything "
            "you could not do. If you hit work outside your scope, add an `ESCALATION:` section "
            "listing recommended follow-up tasks (role, what, why) for the manager to consider."
        )

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=MODEL,
        max_turns=MAX_TURNS,
        cwd=str(repo_dir),
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
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

    if error:
        report("failed", f"agent session error: {error}\n\nlast message:\n{last_text}", cost)
        return

    ok, push_note = commit_and_push(repo_dir)
    status = "pushed" if ok else "failed"
    report(status, f"{last_text}\n\n---\n{push_note}", cost)
    emit("agent_status", "finished")


if __name__ == "__main__":
    asyncio.run(run())
