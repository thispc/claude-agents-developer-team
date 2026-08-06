"""monitor.py — the platform watching itself, and asking before it acts.

The log pipeline records what happened. Nobody reads 3000 rows. This turns them into a short
list of NOTICES: things a person would want to know, each with the evidence behind it and,
where there is an obvious next move, a PROPOSAL — which is offered, never taken. Root approves
or dismisses.

Two ideas borrowed from `blockers.py`, which solved the same problem for projects:

    Derived on read.  A notice is recomputed from the logs every time it is asked for, so one
                      that no longer applies simply stops being reported. Nothing to clean up,
                      and the list can never show a problem that has already gone away.

    Decisions persist. What DOES need remembering is the human's answer. Dismissing a notice
                      silences that fingerprint; approving one records that its action ran.
                      Otherwise the same question is asked every thirty seconds forever.

The proposal is the point. "14 build sessions died on turns" is an observation; "…so lower
repair_max_turns to 35 — approve?" is something you can act on without going to read the code.
Nothing here ever acts on its own: the model (and the rules) propose, the human disposes, and
the only things a proposal may do are listed in ACTIONS — a fixed, small, reversible set.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable

from . import db, logs

DECISIONS_KEY = "monitor:decisions"
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

# How far back a rule looks. Long enough to see a pattern, short enough that a problem fixed
# an hour ago stops shouting.
WINDOW_S = 6 * 3600


def _fp(kind: str, *parts: Any) -> str:
    raw = "|".join([kind, *[str(p) for p in parts]])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def decisions() -> dict[str, dict]:
    d = db.kv_get(DECISIONS_KEY) or {}
    return d if isinstance(d, dict) else {}


def decide(fp: str, state: str, note: str = "") -> dict:
    d = decisions()
    d[fp] = {"state": state, "ts": time.time(), "note": note[:300]}
    db.kv_set(DECISIONS_KEY, d)
    return d[fp]


def _notice(kind: str, severity: str, title: str, detail: str, *, fp_parts: tuple = (),
            evidence: list | None = None, proposal: str = "", action: str = "",
            params: dict | None = None, count: int = 1, since: float = 0.0) -> dict:
    return {"fp": _fp(kind, *fp_parts), "kind": kind, "severity": severity,
            "title": title, "detail": detail,
            "evidence": [{"ts": r.get("ts"), "level": r.get("level"),
                          "cat": r.get("cat"), "event": r.get("event"),
                          "msg": r.get("msg", "")[:200]} for r in (evidence or [])[-5:]],
            "proposal": proposal, "action": action, "params": params or {},
            "count": count, "since": since}


# --- the rules -------------------------------------------------------------
#
# Each takes the window's log rows and returns notices. They are deliberately dumb and
# readable: a rule nobody can check is a rule nobody trusts, and every one of these came from
# a failure that actually happened on this box.

def _rule_sandbox(rows: list[dict]) -> list[dict]:
    hits = [r for r in rows if r.get("cat") == "sandbox"]
    if not hits:
        return []
    files = sorted({str(r.get("files") or r.get("path") or "") for r in hits if r.get("files") or r.get("path")})
    return [_notice(
        "sandbox", "critical",
        f"{len(hits)} isolation breach{'es' if len(hits) > 1 else ''}",
        "A build session touched something outside its worktree, or a protected path. "
        "Nothing was reverted — that tree may hold your own work — so the files are still "
        "there to look at: " + (", ".join(files)[:300] or "see the evidence below"),
        fp_parts=("|".join(files),), evidence=hits, count=len(hits),
        since=min(float(r.get("ts", 0)) for r in hits),
        proposal="Stop self-repair until you have looked at those files.",
        action="pause_repair")]


# A live zombie heartbeats every tick; a dead one stops. Anything older than this is a
# problem that has already been solved, and reporting it is how a monitor loses its
# credibility — the whole promise of deriving on read is that what you see is still true.
ZOMBIE_FRESH_S = 300


def _rule_zombie(rows: list[dict]) -> list[dict]:
    fresh = time.time() - ZOMBIE_FRESH_S
    hits = [r for r in rows if r.get("event") == "lease_held_elsewhere"
            and float(r.get("ts", 0)) >= fresh]
    if not hits:
        return []
    holder = hits[-1].get("holder")
    n = sum(int(r.get("repeats") or 1) for r in hits)
    return [_notice(
        "zombie", "critical", f"another process (pid {holder}) is driving the engine",
        f"A server that no longer answers requests is still ticking the self-repair engine "
        f"against this database — {n} times in the window. It is running whatever code it "
        f"started with. Kill it: `kill -9 {holder}`.",
        fp_parts=(holder,), evidence=hits, count=n,
        since=min(float(r.get("ts", 0)) for r in hits),
        proposal="", action="")]


def _rule_build_deaths(rows: list[dict]) -> list[dict]:
    hits = [r for r in rows if r.get("event") == "build_died"]
    if len(hits) < 2:
        return []
    turns = [r for r in hits if "turn" in str(r.get("msg", "")).lower()]
    if len(turns) >= 2:
        from . import tuning
        now = int(tuning.get("repair_max_turns"))
        return [_notice(
            "build_turns", "warning", f"{len(turns)} build sessions ran out of turns",
            "Tasks are being planned bigger than one session can finish, so the quota is "
            "spent and nothing lands.",
            fp_parts=(len(turns) // 3,), evidence=turns, count=len(turns),
            since=min(float(r.get("ts", 0)) for r in turns),
            proposal=f"Ask the crew for smaller slices — drop repair_tasks_per_sprint to 1 "
                     f"so each sprint plans less at once (turns stay at {now}).",
            action="set_knob", params={"name": "repair_tasks_per_sprint", "value": 1})]
    return [_notice(
        "build_deaths", "warning", f"{len(hits)} build sessions died",
        "Sessions are failing before they produce anything. The last one said: "
        + str(hits[-1].get("msg", ""))[:200],
        fp_parts=(len(hits) // 3,), evidence=hits, count=len(hits),
        since=min(float(r.get("ts", 0)) for r in hits),
        proposal="Pause self-repair until the cause is understood — every dead session "
                 "still spends quota.",
        action="pause_repair")]


def _rule_red_streak(rows: list[dict]) -> list[dict]:
    verdicts = [r for r in rows if r.get("cat") == "verify" and r.get("event") in ("suite_green", "suite_red")]
    tail: list[dict] = []
    for r in reversed(verdicts):
        if r.get("event") == "suite_green":
            break
        tail.append(r)
    if len(tail) < 3:
        return []
    return [_notice(
        "red_streak", "warning", f"the suite has been red {len(tail)} times running",
        "No task has verified green recently. Either the crew is picking work it cannot "
        "finish, or something on main is broken for everyone.",
        fp_parts=(len(tail) // 3,), evidence=list(reversed(tail)), count=len(tail),
        since=min(float(r.get("ts", 0)) for r in tail),
        proposal="Pause self-repair and run the suite yourself.",
        action="pause_repair")]


def _rule_errors(rows: list[dict]) -> list[dict]:
    """Anything logged at error level that no other rule has a better story for."""
    claimed = {"sandbox", "session"}
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        if r.get("level") != "error" or r.get("cat") in claimed:
            continue
        groups.setdefault((r.get("cat"), r.get("event")), []).append(r)
    out = []
    for (cat, event), hits in groups.items():
        out.append(_notice(
            "errors", "warning" if len(hits) < 3 else "critical",
            f"{len(hits)}× {cat}/{event}",
            str(hits[-1].get("msg", ""))[:300] or "see the evidence below",
            fp_parts=(cat, event, len(hits) // 3), evidence=hits, count=len(hits),
            since=min(float(r.get("ts", 0)) for r in hits),
            proposal="", action=""))
    return out


def _rule_quota(rows: list[dict]) -> list[dict]:
    hits = [r for r in rows if r.get("event") == "rate_limited"]
    if not hits:
        return []
    models = sorted({str(r.get("model") or "?") for r in hits})
    return [_notice(
        "quota", "info", f"hit the limit on {', '.join(models)}",
        "The crew backed off and will wake when the provider says so — nothing to do, but "
        "it explains a quiet period.",
        fp_parts=(",".join(models), len(hits) // 3), evidence=hits, count=len(hits),
        since=min(float(r.get("ts", 0)) for r in hits),
        proposal="", action="")]


def _rule_queue(_rows: list[dict]) -> list[dict]:
    q = db.kv_get("repair:queue") or []
    if not q:
        return []
    oldest = min(float(x.get("created_at") or 0) for x in q)
    if time.time() - oldest < 3600:
        return []
    return [_notice(
        "queue", "warning", f"{len(q)} change{'s' if len(q) > 1 else ''} waiting for you",
        "Green work the crew could not land on its own — usually because your own tree was "
        "dirty at the time. It is finished and sitting on a branch: "
        + ", ".join(str(x.get("title", ""))[:60] for x in q[:3]),
        fp_parts=(len(q),), count=len(q), since=oldest,
        proposal="Open the Board tab and approve or discard each one.", action="")]


def _rule_stuck(_rows: list[dict]) -> list[dict]:
    from . import repair
    if not repair.enabled():
        return []
    st = repair.state()
    age = time.time() - float(st.get("updated_at") or 0)
    if st.get("phase") in ("sleeping", "idle") or age < 3600:
        return []
    return [_notice(
        "stuck", "warning", f"stuck in {st.get('phase')} for {int(age // 60)} minutes",
        "The engine writes its phase down before every transition, so a phase this old means "
        "the work in it never finished or never failed.",
        fp_parts=(st.get("phase"),), since=float(st.get("updated_at") or 0),
        proposal="Abort the task in flight so the sprint can move on.",
        action="abort_task")]


RULES: list[Callable[[list[dict]], list[dict]]] = [
    _rule_sandbox, _rule_zombie, _rule_build_deaths, _rule_red_streak,
    _rule_errors, _rule_quota, _rule_queue, _rule_stuck,
]


def scan(window_s: int = WINDOW_S, include_decided: bool = False) -> list[dict]:
    """Every notice that currently applies, worst first, with dismissed ones filtered out."""
    since = time.time() - max(300, int(window_s))
    rows = [r for r in logs.rows() if float(r.get("ts", 0)) >= since]
    out: list[dict] = []
    for rule in RULES:
        try:
            out.extend(rule(rows))
        except Exception as e:                     # a broken rule must not blind the rest
            logs.error("http", "monitor_rule_failed", f"{rule.__name__}: {e}")
    seen = decisions()
    for n in out:
        n["decision"] = seen.get(n["fp"], {})
    if not include_decided:
        out = [n for n in out if n.get("decision", {}).get("state") != "dismissed"]
    out.sort(key=lambda n: (SEVERITY_RANK.get(n["severity"], 9), -n.get("since", 0)))
    return out


def summary(window_s: int = WINDOW_S) -> dict:
    ns = scan(window_s)
    return {"total": len(ns),
            "critical": sum(1 for n in ns if n["severity"] == "critical"),
            "warning": sum(1 for n in ns if n["severity"] == "warning"),
            "needs_approval": sum(1 for n in ns if n.get("action")
                                  and not n.get("decision", {}).get("state"))}


# --- what an approval is allowed to do -------------------------------------
#
# A fixed, small, reversible set. The rules propose in prose, but the only things that can
# actually happen are these — so reading this list tells you the whole blast radius of
# pressing Approve, without trusting any rule's description of itself.

async def _act_pause_repair(_p: dict) -> str:
    from . import repair
    repair.toggle(False)
    return "self-repair is off"


async def _act_set_knob(p: dict) -> str:
    from . import tuning
    name, value = str(p.get("name")), p.get("value")
    tuning.set(name, value)
    return f"{name} is now {value}"


async def _act_abort_task(_p: dict) -> str:
    from . import repair
    await repair.abort()
    return "the task in flight was aborted"


ACTIONS: dict[str, Callable] = {
    "pause_repair": _act_pause_repair,
    "set_knob": _act_set_knob,
    "abort_task": _act_abort_task,
}


async def approve(fp: str) -> dict:
    """Run one notice's proposal. Unknown or action-less notices are simply acknowledged —
    plenty of notices are worth reading and have nothing to press."""
    n = next((x for x in scan(include_decided=True) if x["fp"] == fp), None)
    if not n:
        return {"ok": False, "reason": "that notice no longer applies"}
    fn = ACTIONS.get(n.get("action") or "")
    if not fn:
        decide(fp, "acknowledged")
        return {"ok": True, "did": "acknowledged"}
    did = await fn(n.get("params") or {})
    decide(fp, "approved", did)
    logs.warn("lifecycle", "monitor_action", f"root approved: {n['title']} → {did}",
              fp=fp, action=n.get("action"))
    return {"ok": True, "did": did}
