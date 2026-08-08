"""The pre-P2 in-process notify body, vendored for the strangler window.

This file exists only between commit A (extract) and commit B (cutover) of the
notify extraction: conductor/app/notify.py is the dual-mode shim, and when
NOTIFY_URL is unset it hands the whole surface to this module — byte-for-byte the
old behaviour, including the `notify_sent` / `notify_seen:*` kv blobs in
devteam.db and the conductor's own GitHub client. Commit B deletes this file and
the fallback branch with it. Nothing imports this module except the shim's
fallback branch; the moved-out body now lives, adapted to two real tables, in
services/notify/app.py. `sprint_digest` did NOT move — it formats the
conductor's own projects and tasks, and its copy in the shim is the live one.

--- the original module docstring follows ---

Getting word out when nobody is looking at the dashboard.

"Unattended" was only half true: the platform kept working without a human, and
told them nothing. Every fault — a crashed manager session, a provider returning
500s, a JavaScript error in the dashboard — landed in the events table, which is
only visible to someone already watching the feed. Walk away for a night and the
first you hear of a problem is that nothing got done.

The channel is GitHub issues, chosen because it costs nothing to add: the token
is already in the cluster, and GitHub already pushes to your email and phone.
Building an email path would mean another credential, another service, another
thing to be down.

Two rules make it survivable rather than a spam machine:

- **One issue per distinct fault.** A crash loop produces the same fingerprint a
  thousand times; it should produce one issue with a count, not a thousand
  issues. That is the difference between a notification and a denial of service
  against your own inbox.
- **A ceiling per hour.** If something goes wrong in a way we did not anticipate,
  the failure mode is silence, not an unbounded write loop against the GitHub API
  with a token that can also push code.
"""

import hashlib
import json
import time
from typing import Any

from . import bus, config, db, github_client

MAX_PER_HOUR = int(config._env("NOTIFY_MAX_PER_HOUR", "6"))
ENABLED = config._env("NOTIFY_GITHUB", "1") != "0"

# fingerprint -> {issue, count, first, last}, in the kv table.
#
# This was in memory "on purpose", reasoning that re-notifying once after a
# restart is cheaper than a schema. That reasoning inverts precisely when it
# matters: the fault most worth deduplicating is the one that kills the process,
# and a crash loop restarts often enough that "once per restart" is not once at
# all. The dedup was absent exactly when it was needed.
_SEEN_PREFIX = "notify_seen:"
_SENT_KEY = "notify_sent"


def _seen(fp: str) -> dict[str, Any] | None:
    return db.kv_get(_SEEN_PREFIX + fp)


def _remember(fp: str, record: dict[str, Any]) -> None:
    db.kv_set(_SEEN_PREFIX + fp, record)


def _sent_times() -> list[float]:
    now = time.time()
    return [t for t in db.kv_get(_SENT_KEY, []) or [] if now - t < 3600]


def _fingerprint(kind: str, detail: str) -> str:
    """What makes two faults 'the same'.

    Deliberately coarse — the first line of the message, not the whole thing.
    Stack traces differ by line numbers and timestamps between otherwise identical
    crashes, and a fingerprint that fine would file a fresh issue for every one.
    """
    head = (detail or "").strip().splitlines()[0][:200] if detail else ""
    return hashlib.sha256(f"{kind}|{head}".encode()).hexdigest()[:12]


def _throttled() -> bool:
    return len(_sent_times()) >= MAX_PER_HOUR


def _repo() -> str:
    from . import selfops
    return config.SELF_REPO or selfops.self_repo()


async def report_error(kind: str, detail: str, context: dict | None = None) -> dict:
    """File (or count) a platform fault as a GitHub issue.

    Returns what it did, so callers can log it — but never raises. A notifier that
    can break the thing it is reporting on is worse than no notifier.
    """
    if not ENABLED or not detail:
        return {"sent": False, "reason": "disabled"}
    fp = _fingerprint(kind, detail)
    seen = _seen(fp)
    if seen:
        # Same fault again: count it, do not file another issue. Comment only on
        # milestones, or a busy loop becomes a busy comment thread.
        seen["count"] += 1
        seen["last"] = time.time()
        _remember(fp, seen)
        if seen["count"] in (10, 100, 1000) and seen.get("issue"):
            try:
                await github_client.comment_issue(
                    _repo(), seen["issue"],
                    f"Still happening — {seen['count']} times since "
                    f"{time.strftime('%H:%M', time.localtime(seen['first']))}.")
            except Exception:
                pass
        return {"sent": False, "reason": "already reported", "count": seen["count"],
                "issue": seen.get("issue")}

    record = {"count": 1, "first": time.time(), "last": time.time(), "issue": None}
    _remember(fp, record)
    if _throttled():
        return {"sent": False, "reason": f"more than {MAX_PER_HOUR} in an hour; "
                                         f"holding off so this cannot flood"}
    repo = _repo()
    if not repo or not github_client.enabled(repo):
        return {"sent": False, "reason": "no GitHub repo configured to report to"}

    ctx = context or {}
    body = (
        f"The platform reported a fault while running unattended.\n\n"
        f"**What happened**\n```\n{detail[:2000]}\n```\n\n"
        f"**Where**\n" +
        "".join(f"- {k}: `{v}`\n" for k, v in ctx.items()) +
        f"- fingerprint: `{fp}`\n"
        f"- at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"_Filed automatically. Repeats of the same fault are counted on this "
        f"issue rather than filed again._"
    )
    try:
        n = await github_client.create_issue(repo, f"[{kind}] {detail.strip().splitlines()[0][:90]}", body)
        record["issue"] = n
        _remember(fp, record)
        db.kv_set(_SENT_KEY, _sent_times() + [time.time()])
        bus.emit(0, None, "system", "notified", {"issue": n, "kind": kind})
        return {"sent": True, "issue": n}
    except Exception as e:
        return {"sent": False, "reason": f"could not file the issue: {e}"}


async def sprint_digest(project_id: int, sprint: int) -> dict:
    """What a finished sprint actually produced — the thing worth waking up to.

    Filed even when the sprint went well, because "six sprints ran overnight and
    here is what came out" is the entire point of asking for six sprints. A digest
    only on failure would mean silence is ambiguous.
    """
    if not ENABLED:
        return {"sent": False, "reason": "disabled"}
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
    if not repo or not github_client.enabled(repo):
        return {"sent": False, "reason": "no repo to report to"}
    try:
        n = await github_client.create_issue(
            repo, f"Sprint {sprint} digest: {len(done)} shipped, {len(failed)} failed", body)
        bus.emit(project_id, None, "system", "digest_filed", {"issue": n, "sprint": sprint})
        return {"sent": True, "issue": n}
    except Exception as e:
        return {"sent": False, "reason": str(e)}


def status() -> dict[str, Any]:
    """What has been reported, for the UI — so 'no notifications' is provably
    'nothing went wrong' rather than 'the notifier is broken'."""
    return {
        "enabled": ENABLED,
        "repo": _repo(),
        "max_per_hour": MAX_PER_HOUR,
        "sent_last_hour": len(_sent_times()),
        "distinct_faults": [
            {"kind_hash": fp[len(_SEEN_PREFIX):], "count": v["count"], "issue": v["issue"],
             "last": v["last"]}
            for fp, v in sorted(db.kv_prefix(_SEEN_PREFIX).items(),
                                key=lambda kv: -kv[1].get("last", 0))[:50]],
    }


def forget(fingerprint: str | None = None) -> int:
    """Stop counting a fault as already-reported, so the next occurrence files a
    fresh issue. The operational use is after a fix ships: the old issue is closed
    and a recurrence should be loud again, not silently added to a stale count.
    """
    if fingerprint:
        db.kv_delete(_SEEN_PREFIX + fingerprint)
        return 1
    keys = list(db.kv_prefix(_SEEN_PREFIX))
    for k in keys:
        db.kv_delete(k)
    return len(keys)
