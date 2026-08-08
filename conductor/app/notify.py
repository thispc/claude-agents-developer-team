"""notify.py — the conductor's one door to getting word out when nobody is
looking at the dashboard.

"Unattended" was only half true: the platform kept working without a human, and
told them nothing. Every fault landed in the events table, visible only to
someone already watching the feed. The channel is GitHub issues, because the
token already exists and GitHub already pushes to your email and phone.

Since P2 the dedup, the hourly ceiling and the GitHub call are a SERVICE —
services/notify, its own process on 8883, its own `data/notify.db`, its own
committed contract — and this file is the pure client that reaches it, keeping
every public name the in-process module ever had (report_error / sprint_digest /
status / forget, plus health) so nothing above it had to learn that the notifier
moved.

WHY IT MOVED. "One issue per distinct fault" and "a ceiling per hour" are both
STATE, and that state was two kv blobs rewritten whole (`notify_seen:*`,
`notify_sent`). Two processes filing at once and one of them forgets it filed —
on the exact counter whose job is to stop a flood.

WHAT DID NOT MOVE. `sprint_digest` stays here: it formats the conductor's own
projects and tasks into the text, then sends the finished issue through the
service's generic `POST /issue`. And `_repo()` stays here, because which
repository this platform's own faults belong to is derived from the git remote —
conductor knowledge, so it rides each request rather than being duplicated into a
second env.

The P2 cutover finished here: the vendored in-process body (_notify_legacy.py)
and the dual-mode branch are gone, and `NOTIFY_URL` is now REQUIRED. It is
written into data/env/conductor.env by tools/gen_fleet.py from services.yaml, so
every supported boot path — `./run-local.sh` and `./run-local.sh --legacy` alike
— has it. A conductor started by hand without it fails loudly in init() rather
than pretending it can still tell you when something breaks.

WHY init() AND NOT IMPORT. Importing this module must stay free: routes/__init__
imports it purely so `app.routes.notify.report_error` stays a patchable path.
Boot does call init(), and that is the honest place to refuse.

DEGRADED MODE (service down): every verb answers
`{"sent": False, "reason": "notify service down"}` with one deduped warn —
silence is already this module's designed failure mode, and it is the right one:
a notifier that raises takes down the thing it was reporting on. `status()` says
`degraded: True` so the UI can tell "nothing went wrong" from "the notifier is
broken", and `forget()` returns 0. The verbs degrade for a MISSING url too, by
the same path: init() is the loud door, and a door that also killed every later
call would turn one misconfigured process into a crash loop.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

# github_client is deliberately NOT imported: the GitHub call went with the
# service, and the conductor's copy stays for the things that did not move (PRs,
# merges, branch lookups). Two callers of one client was the coupling; one of
# them now speaks HTTP instead.
from . import bus, config, db, pool

_URL = (os.environ.get("NOTIFY_URL") or "").strip().rstrip("/")

_TIMEOUT = 10.0     # the service may be mid-GitHub-call; it has its own 30s
# Tests inject an httpx transport here (an ASGI transport onto the service app,
# or one that raises) — the client code path stays identical.
_TRANSPORT: httpx.AsyncBaseTransport | None = None
_TOKEN = ""

_DOWN = {"sent": False, "reason": "notify service down"}

_NO_URL = (
    "NOTIFY_URL is not set — the notifier is a service since P2 and the conductor "
    "has no in-process fallback any more. Boot with ./run-local.sh (or "
    "./run-local.sh --legacy), which generates data/env/conductor.env from "
    "services.yaml and starts services/notify on 8883."
)


def _token() -> str:
    """The service's own token, read from where gen_fleet minted it — the same
    resolution the /svc gateway uses (routes/svc.py)."""
    global _TOKEN
    if not _TOKEN:
        try:
            _TOKEN = (config.ROOT / "data" / "tokens" / "notify.token") \
                .read_text().strip()
        except OSError:
            _TOKEN = ""
    return _TOKEN


def _client() -> httpx.AsyncClient:
    """The SHARED async client — one per running event loop, rebuilt when _URL,
    _TRANSPORT or the token changes. Never closed by a caller."""
    return pool.async_client("notify", base_url=_URL, timeout=_TIMEOUT,
                             transport=_TRANSPORT,
                             headers={"X-Service-Token": _token()})


def _sync_client() -> httpx.Client:
    # status/forget keep their historical sync signatures. Shared like the async
    # half; no _TRANSPORT here because a sync client cannot drive an ASGI app —
    # tests swap this whole factory for a TestClient.
    return pool.sync_client("notify:sync", base_url=_URL, timeout=_TIMEOUT,
                            headers={"X-Service-Token": _token()})


def _degraded(verb: str, err: Exception) -> None:
    """One deduped warn per window, never a raise."""
    try:
        from . import logs
        logs.log("lifecycle", "notify_degraded",
                 f"notify service unreachable — {verb} degraded "
                 f"({type(err).__name__}: {str(err)[:120]})",
                 level="warn", dedupe_s=300, verb=verb)
    except Exception:
        pass


def _repo() -> str:
    """Which repository this platform's own faults belong to. Derived from the
    git remote when nothing configures it, which is why it cannot live in the
    service's env — it rides the call."""
    from . import selfops
    return config.SELF_REPO or selfops.self_repo()


# --- the verbs, over the wire -------------------------------------------------

async def report_error(kind: str, detail: str, context: dict | None = None) -> dict:
    """File (or count) a platform fault as a GitHub issue.

    Returns what it did, so callers can log it — but never raises. A notifier that
    can break the thing it is reporting on is worse than no notifier; with the
    service down that promise is kept by answering
    `{"sent": False, "reason": "notify service down"}`.
    """
    if not detail:
        return {"sent": False, "reason": "disabled"}
    try:
        c = _client()
        r = await c.post("/error", json={
            "kind": str(kind or "?")[:120], "detail": str(detail)[:20000],
            "context": context or {}, "repo": _repo()[:200]})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _degraded("report_error", e)
        return dict(_DOWN)


async def sprint_digest(project_id: int, sprint: int) -> dict:
    """What a finished sprint actually produced — the thing worth waking up to.

    Filed even when the sprint went well, because "six sprints ran overnight and
    here is what came out" is the entire point of asking for six sprints. A digest
    only on failure would mean silence is ambiguous.

    Stays conductor-side and always will: every line of it is a JOIN over projects
    and tasks, and handing a service a reader on those would undo the isolation
    the extraction bought. It composes the text here and posts the finished issue
    through the service's generic door.
    """
    p = db.get_project(project_id)
    if not p:
        return {"sent": False, "reason": "no such project"}
    tasks = [t for t in db.list_tasks(project_id) if t["sprint"] == sprint]
    done = [t for t in tasks if t["status"] == "done"]
    failed = [t for t in tasks if t["status"] == "failed"]
    stuck = [t for t in tasks if t["status"] not in ("done", "failed")]

    def _line(t):
        v = json.loads(t["verification"] or "{}")
        mark = "✅" if v.get("ok") else ("❌" if v.get("ran") else "⚠️ unverified")
        pr = f" (PR #{t['pr_number']})" if t["pr_number"] else ""
        return f"- {mark} **#{t['seq']} {t['title']}** — {t['role']}{pr}"

    body = (
        f"Sprint {sprint} of {p['sprints']} finished on **{p['name']}**.\n\n"
        f"**Shipped ({len(done)})**\n" + ("\n".join(_line(t) for t in done) or "_nothing_") +
        (f"\n\n**Failed ({len(failed)})**\n" + "\n".join(_line(t) for t in failed) if failed else "") +
        (f"\n\n**Still open ({len(stuck)})**\n" + "\n".join(_line(t) for t in stuck) if stuck else "") +
        f"\n\n**Cost** {p['runs_used']} of {p['max_runs']} agent runs used.\n\n"
        f"_⚠️ unverified means the project declares no test command, so nothing "
        f"checked that work beyond the agent's own account of it._"
    )
    repo = p["repo"] or _repo()
    if not repo:
        return {"sent": False, "reason": "no repo to report to"}
    title = f"Sprint {sprint} digest: {len(done)} shipped, {len(failed)} failed"
    try:
        c = _client()
        r = await c.post("/issue", json={"repo": repo[:200], "title": title[:300],
                                         "body": body[:20000]})
        r.raise_for_status()
        out = r.json()
    except Exception as e:
        _degraded("sprint_digest", e)
        return dict(_DOWN)
    if out.get("issue"):
        # The digest's own bus event stays conductor-side, because the digest
        # does: it is about a PROJECT, and the service was handed text, not a
        # project id.
        bus.emit(project_id, None, "system", "digest_filed",
                 {"issue": out["issue"], "sprint": sprint})
    return out


def status() -> dict[str, Any]:
    """What has been reported, for the UI — so 'no notifications' is provably
    'nothing went wrong' rather than 'the notifier is broken'. The repo is added
    here because it is the conductor that knows it."""
    try:
        c = _sync_client()
        r = c.get("/status")
        r.raise_for_status()
        return {**r.json(), "repo": _repo()}
    except Exception as e:
        _degraded("status", e)
        return {"enabled": False, "repo": _repo(), "max_per_hour": 0,
                "sent_last_hour": 0, "distinct_faults": [], "degraded": True}


def forget(fingerprint: str | None = None) -> int:
    """Stop counting a fault as already-reported, so the next occurrence files a
    fresh issue. The operational use is after a fix ships: the old issue is closed
    and a recurrence should be loud again, not silently added to a stale count."""
    try:
        c = _sync_client()
        r = c.post("/forget", json={"fingerprint": fingerprint or ""})
        r.raise_for_status()
        return int(r.json().get("removed") or 0)
    except Exception as e:
        _degraded("forget", e)
        return 0


def health() -> bool:
    """Is the notifier actually answering? Its own /health — the endpoint
    process-compose probes — asked through this door rather than around it."""
    try:
        c = _sync_client()
        r = c.get("/health")
        return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception as e:
        _degraded("health", e)
        return False


# --- lifecycle ----------------------------------------------------------------

def init() -> None:
    """Refuse to boot without the notifier, and finish the strangler.

    The conductor keeps no notification state any more. Two kv shapes are dropped
    here — in the module that used to write them:

      notify_seen:<fp>  one record per distinct fault: count, first/last seen, and
                        the issue it was filed as. The service copied these into
                        its own `notify_seen` table on first boot (read-only
                        ATTACH, marked once), because without them the extraction
                        itself would have been a notification storm — every
                        already-filed fault looking new again.
      notify_sent       the rolling hour of send timestamps. Deliberately NOT
                        copied: it is a rate-limiter's sliding window, it empties
                        within the hour anyway, and starting the service's own
                        counter at zero errs toward being able to tell you
                        something.

    Commit A kept both so a rollback — unset the URL — could find them again.
    With the fallback deleted they are a second, staler copy of state another
    process owns, and a reader who found them would reasonably conclude the
    dedup still happens here.
    """
    if not _URL:
        raise RuntimeError(_NO_URL)
    db.kv_delete("notify_sent")
    for key in list(db.kv_prefix("notify_seen:")):
        db.kv_delete(key)
