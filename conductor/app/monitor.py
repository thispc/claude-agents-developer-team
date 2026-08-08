"""monitor.py — the platform watching itself, and asking before it acts.

The log pipeline records what happened. Nobody reads 3000 rows. Something has to turn them
into a short list of NOTICES: things a person would want to know, each with the evidence
behind it and, where there is an obvious next move, a PROPOSAL — which is offered, never
taken. Root approves or dismisses.

Two ideas borrowed from `blockers.py`, which solved the same problem for projects:

    Derived on read.  A notice is recomputed every time it is asked for, so one that no
                      longer applies simply stops being reported. Nothing to clean up, and
                      the list can never show a problem that has already gone away.

    Decisions persist. What DOES need remembering is the human's answer. Dismissing a notice
                      silences that fingerprint; approving one records that its action ran.
                      Otherwise the same question is asked every thirty seconds forever.

--- WHAT P3 SPLIT, AND ON WHICH LINE ------------------------------------------------------

The detection moved and the levers did not.

    services/watch  FACTS and DETECTION. The log rows, the error rows, the seven rules that
                    read only those rows, the notices they produce, the decisions store and
                    the standing `auto` flag. It owns no switch and can reach none.

    this module     JUDGMENT and ACTION. `ACTIONS` is still the complete blast radius of
                    pressing Approve, and every entry in it targets conductor-resident
                    machinery: repair.toggle, tuning.set, repair.abort, repair.save_backlog.
                    The last has no HTTP endpoint at all and needs none, because the executor
                    never leaves. Moving any of them would have meant opening a door back
                    into the conductor for a service whose entire job is to watch.

    LOCAL_RULES     the two rules whose evidence is not a log row: "changes waiting for you"
                    reads the review queue, and "stuck in a phase" reads the engine's own
                    clock. They stayed for the same reason the actions did — and that is what
                    erased the platform's last cross-owner kv read, `monitor` reaching into
                    `repair:queue`, a key another module owns.

So `/api/logs/notices` is a COMPOSITION: watch's notices plus the local ones, merged into one
list, deduped by fingerprint, in one sort order. `approve(fp)` resolves the notice from either
source, runs the action HERE, and only then POSTs the decision to watch. From the screen's
side nothing changed; that is the point.

The P3 cutover finished here: the vendored in-process body (_monitor_legacy.py) and the
dual-mode branch are gone, and `WATCH_URL` is REQUIRED — `logs.init()` is the one loud door
for both halves of this seam, and the one that drops the four kv keys the strangler left in
devteam.db (`monitor:decisions` and `monitor:auto` among them). This module has no init of
its own because it has no state of its own left to clean up.

DEGRADED MODE (watch down): the notice list falls back to the LOCAL rules only, and says so —
`degraded: true` and a `banner` sentence the screen shows above the list, so an empty inbox
during an outage cannot be misread as "the platform has been behaving". Approve still works
for a local notice: the action runs here, and only the decision is lost — which means the
notice comes back, which is the honest failure. `auto_on()` reads False when it cannot ask, so
nothing runs unattended on an unconfirmed permission.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Callable

import httpx

from . import config, db, logs

_URL = (os.environ.get("WATCH_URL") or "").strip().rstrip("/")

_TIMEOUT = 2.0
# Tests inject an httpx transport here (one that refuses, for the outage
# drills) — the client code path stays identical. Sync, because every verb
# here is: the notice panel and the engine tick both call them as plain
# functions.
_TRANSPORT: httpx.BaseTransport | None = None
_TOKEN = ""

# Which proposals may run unattended. NOT every action: `pause_repair` and `abort_task`
# stop work that is running, and a rule misfiring at 3am should not be able to silently
# switch the crew off — you would find it off in the morning with no memory of deciding
# that. What is safe to automate is what only ADDS: filing a bug, and nudging a knob that
# is itself capped.
AUTO_SAFE = ("file_task", "set_knob")
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

# How far back a rule looks. Long enough to see a pattern, short enough that a problem
# fixed an hour ago stops shouting. The service has the same number for its own rules;
# this one is what the conductor asks for and what the local rules honour.
WINDOW_S = 6 * 3600

BANNER = ("The watch service is not answering, so this list is only what the conductor "
          "can see for itself — the review queue and a stuck engine. Anything derived "
          "from the logs is missing until it is back.")


def _token() -> str:
    """The service's own token, read from where gen_fleet minted it — the same
    resolution the /svc gateway uses (routes/svc.py)."""
    global _TOKEN
    if not _TOKEN:
        try:
            _TOKEN = (config.ROOT / "data" / "tokens" / "watch.token") \
                .read_text().strip()
        except OSError:
            _TOKEN = ""
    return _TOKEN


def _client() -> httpx.Client:
    return httpx.Client(base_url=_URL, timeout=_TIMEOUT, transport=_TRANSPORT,
                        headers={"X-Service-Token": _token()})


def _degraded(verb: str, err: Exception) -> None:
    """One deduped warn per window, never a raise — the degraded shapes are
    the contract; the log line is how a 3am operator learns which one fired.

    It goes through logs.*, which is a client of the SAME service. That is
    deliberate and it works: the log call echoes to stdout locally whatever
    the service is doing, so the record of the outage survives the outage.
    """
    try:
        logs.log("lifecycle", "watch_degraded",
                 f"watch service unreachable — {verb} degraded "
                 f"({type(err).__name__}: {str(err)[:120]})",
                 level="warn", dedupe_s=300, verb=verb)
    except Exception:
        pass

# --- the local rules ------------------------------------------------------
#
# `_fp` and `_notice` are vendored copies of the service's — same fingerprint
# algorithm, so a notice keeps its identity whichever side produced it, which
# is what lets one decisions store serve both. Two small copies rather than a
# shared import, because the service imports nothing from the conductor and
# the conductor imports nothing from inside a service directory.


def _fp(kind: str, *parts: Any) -> str:
    raw = "|".join([kind, *[str(p) for p in parts]])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


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


def _rule_queue() -> list[dict]:
    """Green work the crew could not land on its own.

    Reads `repair:queue` — the conductor's own key, in the conductor's own process. Before
    P3 this exact read happened in a module that has since become a separate service, and
    it was the platform's last cross-owner kv read: one owner's blob, opened by somebody
    else's code, with no contract between them.
    """
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


def _rule_stuck() -> list[dict]:
    """A phase the engine wrote down and never moved on from.

    The evidence is `repair.state()` — an object in this process, not a log row. That is
    the whole criterion for a rule staying local.
    """
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

LOCAL_RULES: list[Callable[[], list[dict]]] = [_rule_queue, _rule_stuck]

# --- the composition ------------------------------------------------------


def _watch(window_s: int, include_decided: bool) -> dict | None:
    """One call for the whole remote half: its notices, its count, and the
    decisions map the LOCAL notices are filtered against. A second round-trip
    for that map would double the cost of a panel that polls."""
    logs.flush()        # read your own writes: the rows are queued in-process
    try:
        with _client() as c:
            r = c.get("/notices", params={"window_s": int(window_s),
                                          "include_decided": bool(include_decided)})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        _degraded("notices", e)
        return None


def compose(window_s: int = 0, include_decided: bool = False) -> dict:
    """The whole notice inbox, from both sides, in one shape.

    Merged then deduped by fingerprint. The kinds are disjoint today, so a
    collision would mean the two sides had independently derived the same
    notice — in which case showing it twice is the bug, not showing it once.
    """
    w = int(window_s or WINDOW_S)
    remote = _watch(w, include_decided)
    seen: dict[str, dict] = (remote or {}).get("decisions") or {}
    local: list[dict] = []
    for rule in LOCAL_RULES:
        try:
            local.extend(rule())
        except Exception as e:              # a broken rule must not blind the rest
            logs.error("http", "monitor_rule_failed", f"{rule.__name__}: {e}")
    for n in local:
        n["decision"] = seen.get(n["fp"], {})
    if not include_decided:
        local = [n for n in local if n.get("decision", {}).get("state") != "dismissed"]

    out: list[dict] = []
    fps: set[str] = set()
    for n in list((remote or {}).get("notices") or []) + local:
        if n.get("fp") in fps:
            continue
        fps.add(n.get("fp"))
        out.append(n)
    out.sort(key=lambda n: (SEVERITY_RANK.get(n["severity"], 9), -n.get("since", 0)))
    return {
        "notices": out,
        # Counted over the COMPOSED list, not taken from the service: the
        # badge on the screen has to mean "things that need you", and the
        # service can only count its own half.
        "summary": {"total": len(out),
                    "critical": sum(1 for n in out if n["severity"] == "critical"),
                    "warning": sum(1 for n in out if n["severity"] == "warning"),
                    "needs_approval": sum(1 for n in out if n.get("action")
                                          and not n.get("decision", {}).get("state"))},
        "degraded": remote is None,
        "banner": BANNER if remote is None else "",
    }


def scan(window_s: int = WINDOW_S, include_decided: bool = False) -> list[dict]:
    """Every notice that currently applies, worst first, with dismissed ones
    filtered out — both halves."""
    return compose(window_s, include_decided)["notices"]


def summary(window_s: int = WINDOW_S) -> dict:
    return compose(window_s)["summary"]

# --- decisions ------------------------------------------------------------


def decisions() -> dict[str, dict]:
    """Everything the human has said, from the store that keeps it."""
    remote = _watch(WINDOW_S, True)
    return (remote or {}).get("decisions") or {}


def decide(fp: str, state: str, note: str = "") -> dict:
    """Remember what the human said. The ACTION, if there was one, has already
    run here — this is the record, and it is the service's to keep."""
    rec = {"state": state, "ts": time.time(), "note": note[:300]}
    try:
        with _client() as c:
            r = c.post(f"/notices/{fp}/decide", json={"state": state, "note": note[:300]})
            r.raise_for_status()
            got = r.json()
            return {"state": got.get("state"), "ts": got.get("ts"),
                    "note": got.get("note", "")}
    except Exception as e:
        _degraded("decide", e)
        # The decision is lost, so the notice will be asked again. Honest, and
        # strictly better than the alternative: a local record the service
        # would never see would mean the same notice silenced in one process
        # and live in every other.
        return {**rec, "degraded": True}


def auto_on() -> bool:
    """Whether the owner has asked for the additive proposals to run unattended.

    False when it cannot be confirmed. Nothing should run unattended on a
    permission nobody could read — and this is asked on every engine tick, so
    it is its own endpoint rather than a full notice scan.
    """
    try:
        with _client() as c:
            r = c.get("/auto")
            r.raise_for_status()
            return bool(r.json().get("auto"))
    except Exception as e:
        _degraded("auto", e)
        return False


def set_auto(on: bool) -> bool:
    try:
        with _client() as c:
            r = c.post("/auto", json={"on": bool(on)})
            r.raise_for_status()
            got = bool(r.json().get("auto"))
    except Exception as e:
        _degraded("set_auto", e)
        return False
    logs.warn("lifecycle", "monitor_auto",
              "unattended approval is ON — additive proposals will run without asking"
              if got else "unattended approval is off — proposals wait for you")
    return got

# --- what an approval is allowed to do ------------------------------------
#
# A fixed, small, reversible set. The rules propose in prose, but the only things that can
# actually happen are these — so reading this list tells you the whole blast radius of
# pressing Approve, without trusting any rule's description of itself.
#
# Every one of them reaches straight into a module in THIS process. That is why the split
# put them here: a detection service that could pause the crew would need a door back into
# the conductor, and `save_backlog` has no HTTP endpoint at all — nor does it need one,
# because the executor never left.


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


async def _act_file_task(p: dict) -> str:
    """Put a task at the FRONT of the crew's backlog, so the next sprint picks it up.

    The front, not the back: a notice reaches a human because something is actually broken,
    and burying it under six planned improvements would answer "queue it as a bug" with
    "eventually". It goes in as a proper typed task, so it looks exactly like planned work
    on the board — because from the crew's side it is."""
    from . import repair
    b = repair.backlog()
    task = repair._typed({
        "slug": repair._slug(str(p.get("title", "reported defect"))),
        "title": str(p.get("title", "reported defect"))[:140],
        "type": p.get("type"), "priority": p.get("priority"),
        "factor": str(p.get("factor", "correctness"))[:24],
        "brief": str(p.get("brief", ""))[:800], "acceptance": [],
        "branch": "", "status": "pending", "worktree": None, "verification": None,
        "landed_sha": None, "attempts": 0, "evidence": "", "from_monitor": True})
    b["tasks"] = [task] + [t for t in b.get("tasks", []) if t.get("slug") != task["slug"]]
    b["ts"] = b.get("ts") or time.time()
    repair.save_backlog(b)
    return f"queued for the next sprint: {task['title'][:60]}"

ACTIONS: dict[str, Callable] = {
    "pause_repair": _act_pause_repair,
    "set_knob": _act_set_knob,
    "abort_task": _act_abort_task,
    "file_task": _act_file_task,
}


async def sweep() -> list[dict]:
    """Run the proposals that are safe to run unattended, if the owner has asked for that.

    Called from the engine's own tick, so it happens whether or not anyone has the screen
    open — which is the point of the setting. Every action still goes through `approve`, so
    it is recorded, logged and visible in exactly the same place a hand-pressed one is:
    "the platform did it while I was asleep" must never mean "and left no trace".
    """
    if not auto_on():
        return []
    done = []
    for n in scan():
        if n.get("action") not in AUTO_SAFE or n.get("decision", {}).get("state"):
            continue
        out = await approve(n["fp"])
        if out.get("ok"):
            done.append({"title": n["title"], "did": out.get("did", "")})
    return done


async def approve(fp: str) -> dict:
    """Run one notice's proposal. Unknown or action-less notices are simply acknowledged —
    plenty of notices are worth reading and have nothing to press.

    The notice is resolved from the COMPOSED list, so it does not matter which side derived
    it. The action runs here either way; only the decision travels.
    """
    n = next((x for x in scan(include_decided=True) if x["fp"] == fp), None)
    if not n:
        return {"ok": False, "reason": "that notice no longer applies"}
    fn = ACTIONS.get(n.get("action") or "")
    if not fn:
        decide(fp, "acknowledged")
        return {"ok": True, "did": "acknowledged"}
    did = await fn(n.get("params") or {})
    # The order matters and is the whole shape of the split: DO the thing with
    # the conductor's own machinery, then tell the store what was decided. A
    # remote decide that fails after a successful action is a notice you will
    # be asked about again — never an action that did not happen.
    decide(fp, "approved", did)
    logs.warn("lifecycle", "monitor_action", f"root approved: {n['title']} → {did}",
              fp=fp, action=n.get("action"))
    return {"ok": True, "did": did}


def health() -> bool:
    """Is the watch service actually answering? Its own /health, asked through
    this door rather than around it."""
    try:
        with _client() as c:
            r = c.get("/health")
            return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False
