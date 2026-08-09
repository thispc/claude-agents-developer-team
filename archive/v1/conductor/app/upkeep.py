"""The daily pass: look at what went wrong, and act on the worst of it.

Self-repair worked only when a human asked for it, which makes "self-repairing"
an overstatement — the platform could fix itself, but only once someone had
already noticed something was broken and gone to the page. The whole value of the
loop is in the part nobody is present for.

The schedule is deliberately dull. A loop that wakes every few minutes to check
whether it is time yet costs nothing and survives every clock change, daylight
saving shift and restart without any of the reasoning that makes cron-like code
wrong twice a year. The last run is stored, so a restart does not re-run a pass
that already happened, and a machine that was off all night runs once when it
comes back rather than eight times.

What it does, in order and with an explicit stop at each step:

1. **Scan.** Free. Reads recorded events into findings. Always runs.
2. **Rank.** Free. Decides what is worst.
3. **File.** Cheap. The worst finding becomes a ticket, if it clears the bar.
4. **Work.** Expensive, and gated on the same triage rules that decide how much
   independence any change gets — a scheduled repair has no more licence than one
   a human asked for. Off by default.

Step 4 being off by default is not timidity. An unattended process that both
decides what is wrong AND rewrites the code it is running on, with nobody
watching, is a specific and bad idea unless the operator has said otherwise.
"""

import asyncio
import time
from typing import Any

from . import bus, config, db, findings, notify, tuning

# How often the loop wakes to check whether a pass is due. Not how often a pass
# runs — that is `upkeep_interval_hours`.
TICK_SECONDS = 300

LAST_RUN_KEY = "upkeep_last_run"


def due(now: float | None = None) -> bool:
    now = now or time.time()
    if not tuning.get("upkeep_enabled"):
        return False
    interval = float(tuning.get("upkeep_interval_hours")) * 3600
    return (now - float(db.kv_get(LAST_RUN_KEY, 0.0))) >= interval


async def run_once(force: bool = False) -> dict[str, Any]:
    """One upkeep pass. Safe to call by hand — that is how the UI button works."""
    started = time.time()
    result: dict[str, Any] = {"at": started, "filed": None, "started_work": None}

    scanned = findings.scan()
    result["scan"] = {"events_read": scanned["events_read"],
                      "top": [f["title"] for f in scanned["top"][:3]]}

    actionable = findings.worth_a_sprint()
    result["actionable"] = len(actionable)
    db.kv_set(LAST_RUN_KEY, started)

    if not actionable:
        bus.emit(0, None, "system", "upkeep_clean",
                 {"events_read": scanned["events_read"]})
        return result

    worst = actionable[0]
    if not tuning.get("upkeep_files_tickets") and not force:
        # Ranked and visible, but not filed. Someone who wants to know what the
        # platform thinks is wrong should not have to let it act to find out.
        bus.emit(0, None, "system", "upkeep_found",
                 {"title": worst["title"], "score": worst["score"],
                  "note": "not filed — ticket filing is switched off"})
        return result

    filed = await _file(worst)
    result["filed"] = filed
    return result


async def _file(finding: dict) -> dict[str, Any]:
    """Turn the worst finding into a ticket a human can read and act on.

    A ticket, not a sprint. Deciding something is broken and rewriting the running
    code are different acts with different costs, and collapsing them means the
    first mistake in ranking becomes a code change nobody asked for.
    """
    from . import selfops
    body = (
        f"The daily self-check ranked this the most significant thing wrong with "
        f"the platform right now.\n\n"
        f"**Seen {finding['count']} time(s)**, first at "
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(finding['first_seen']))}, "
        f"most recently at "
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(finding['last_seen']))}.\n\n"
        f"**Severity** {finding['severity']} · **score** {finding.get('score', 0)}\n\n"
        f"**What was recorded**\n```\n{finding['detail'][:2000]}\n```\n\n"
        f"_Filed by the scheduled self-check. Recurrences are counted on this "
        f"finding rather than filed again._"
    )
    try:
        res = await notify.report_error(f"self-check/{finding['kind']}", finding["detail"],
                                        {"finding": finding["id"],
                                         "count": finding["count"],
                                         "severity": finding["severity"]})
    except Exception as e:
        return {"sent": False, "reason": str(e)}
    if res.get("issue"):
        findings.set_status(finding["id"], "ticketed", issue_number=res["issue"])
    bus.emit(0, None, "system", "upkeep_filed",
             {"title": finding["title"], "issue": res.get("issue"),
              "score": finding.get("score", 0)})
    return {**res, "finding": finding["id"], "body_preview": body[:200]}


async def loop() -> None:
    """Wake periodically, run a pass when one is due. Never dies.

    A crash here would silently disable self-monitoring, which is the failure mode
    where you find out months later that nothing has been watching. Every error is
    swallowed and reported rather than allowed to end the loop.
    """
    while True:
        try:
            if due():
                await run_once()
        except Exception as e:
            bus.emit(0, None, "system", "upkeep_error", {"detail": str(e)[:300]})
            try:
                await notify.report_error("upkeep", f"the daily self-check failed: {e}")
            except Exception:
                pass
        await asyncio.sleep(TICK_SECONDS)
