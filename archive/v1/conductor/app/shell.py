"""One place the app runs child processes from.

Five modules had grown their own near-identical subprocess wrapper — preview,
sandbox, selfops, rollout, repair_builder — same capture, same text mode, each
with its own defaults, and exactly one of them carrying a lesson the others
never learned. This module keeps that lesson once; the callers keep only their
defaults.

Two verbs, two error contracts, and the difference is deliberate:

- `sh()` NEVER raises for the ordinary failures. subprocess.run raises
  FileNotFoundError for a binary that is not installed, and the conductor's own
  container deliberately carries neither docker nor kubectl — so every
  deployment call became a 500 that read as "the server is broken" when the
  honest answer is "that tool is not here". A missing binary or a timeout comes
  back as a CompletedProcess with returncode 127 and the reason in stderr.
  Callers check returncode; they always had to anyway.

- `git()` DOES raise, exactly as subprocess.run with check=True does. The
  self-repair builder treats a failed git as an exception to classify rather
  than a status to inspect, and git is the one binary the platform cannot run
  without — a raise there is a real emergency, not a condition to soften.

`wait_healthy()` is the third thing every process-spawner reinvented: start a
child on a port, then poll until it answers, it dies, or patience runs out.
"""

import subprocess
import time
from pathlib import Path


def sh(*cmd: str, cwd: Path | str | None = None, timeout: int | None = 120,
       stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run a tool, treating "that tool isn't installed" (and a timeout) as a
    failed run — returncode 127, reason in stderr — never an exception.
    `timeout=None` means no limit, for callers like preview whose npm builds
    legitimately take minutes."""
    try:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, input=stdin,
                              capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: {e}")


def git(*args: str, cwd: Path | str | None = None, check: bool = True,
        timeout: int = 120) -> subprocess.CompletedProcess:
    """Run git. check=True raises CalledProcessError on failure, like
    subprocess.run — see the module docstring for why this one raises."""
    return subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=timeout,
                          check=check)


def wait_healthy(port: int, proc: subprocess.Popen, path: str = "/",
                 timeout: int = 60, ok_status: int | None = None,
                 subject: str = "the app") -> tuple[bool, str]:
    """Poll until the child answers on `port`, or it dies, or we run out of
    patience. Returns (ok, note).

    The one honest difference between its callers lives in `ok_status`:
    deploy counts ANY HTTP answer as alive (a 404 on / still proves the server
    is listening), while the sandbox insists on a 200 from /api/health — a
    candidate build of this very platform must prove it works, not merely that
    it is up.
    """
    import httpx

    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            return False, f"{subject} exited immediately (code {proc.returncode})"
        try:
            r = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=3)
            if ok_status is None:
                return True, f"responding on port {port} (HTTP {r.status_code})"
            if r.status_code == ok_status:
                return True, "healthy"
        except Exception as e:
            last = str(e)
        time.sleep(1)
    if ok_status is None:
        return False, f"no response on port {port} within {timeout}s ({last[:120]})"
    return False, f"no healthy response within {timeout}s"
