"""Self-repair v2 — the IT crew. A BUTTON: toggled on, the platform improves ITSELF in
sprints, forever; toggled off, it stops.

The crew is real: one Studio persona per enabled FACTOR (simplicity, ui-polish, …) plus the
graph's hidden manager — a visible lifeworld world you can open on the canvas and chat with.
Each sprint: SCOUT the repo (one read-only session) → the crew DELIBERATES the sprint plan
(bounded by construction; the decision memo IS the plan) → BUILD each task in a disposable
worktree (repair_builder) → VERIFY with the platform's own full suite → LAND green squash
commits (or queue them in supervised mode) → RETRO, then rest.

Usage discipline is the manager's first job: a ledger of every session feeds 5-hour and weekly
meters, per-model cooldowns parsed from real "session limit · resets …" errors are the
authoritative signal (launcher.note_rate_limit, restart-surviving), and the crew SLEEPS —
visibly, with a wake time — instead of hitting the wall mid-build.

State lives entirely in kv (no new tables); every transition is persisted BEFORE it runs, so a
restart (including our own auto-restart after landing backend changes) resumes mid-sprint.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path

from . import bus, config, db, launcher, logs, tuning, usage
from . import repair_builder as rb

TICK_SECONDS = 20

# Bumped whenever the built-in personas change. It drives BOTH refreshes a running crew needs:
# factors() re-syncs the kv factor list with DEFAULT_FACTORS, and ensure_team() re-seats the
# world. Without it a persona fix only ever reaches a brand-new install, because the kv copies
# of both are what the live crew actually runs on.
#   2 — real trait/drive vocabulary (the Big Five names this engine never had)
#   3 — the same, actually delivered: bumping is what makes an existing crew take it
TEAM_PERSONAS = 3

# Serializes read-modify-write cycles on the repair:state and repair:ledger kv keys.
# tick() runs on the event loop, but toggle()/set_factors() are plain `def` FastAPI
# handlers that FastAPI runs in a worker thread — a real cross-thread race, not just
# interleaved coroutines — so this has to be a threading.Lock (like db.py's own _lock),
# not an asyncio.Lock, which only serializes within a single event loop.
_STATE_LOCK = threading.Lock()


def hunger(drive: str, deficit: float = 0.35) -> dict:
    """Seed a factor's motivation. A drive is a homeostatic LEVEL, not a wish: pressure is
    setpoint minus level, so the agent that WANTS safety is the one whose safety sits BELOW
    its setpoint. Seeding 0.9 ("very safety-minded") actually says "fully satisfied, wants
    nothing" — which is how six factor agents ended up sharing one default goal."""
    from .lifeworld.drives import SPEC
    return {drive: round(max(0.0, SPEC.get(drive, (0.6, 0, 0))[0] - deficit), 3)}


DEFAULT_FACTORS = [
    # Dials use THIS engine's trait vocabulary (psyche.TRAITS: willpower, risk_appetite,
    # composure, curiosity, sociability, empathy, conscientiousness) and drives.SPEC —
    # anything else is silently dropped by the manifest, which is how six "specialists" ended
    # up as six identical neutral agents saying the same sentence. A specialist must actually
    # differ from its neighbours or the panel is one opinion repeated N times.
    {"id": "correctness", "name": "Correctness", "enabled": True,
     "brief": "bugs, races, broken flows — anything that behaves wrong or could",
     "dials": {"conscientiousness": 95, "risk_appetite": 15, "composure": 80, "curiosity": 55},
     "drives": hunger("safety")},
    {"id": "simplicity", "name": "Simplicity", "enabled": True,
     "brief": "ruthless about steps, options and code that need not exist",
     "dials": {"conscientiousness": 85, "empathy": 25, "willpower": 90, "curiosity": 40},
     "drives": hunger("purpose")},
    {"id": "ui-polish", "name": "UI polish", "enabled": True,
     "brief": "visual coherence, spacing, affordances — the product should feel finished",
     "dials": {"empathy": 90, "curiosity": 80, "conscientiousness": 70, "risk_appetite": 45},
     "drives": hunger("esteem")},
    {"id": "seamlessness", "name": "Seamlessness", "enabled": True,
     "brief": "fewer clicks, fewer surprises — flows that never make the user think about the tool",
     "dials": {"empathy": 95, "sociability": 85, "conscientiousness": 60, "willpower": 45},
     "drives": hunger("social")},
    {"id": "reusability", "name": "Reusability", "enabled": True,
     "brief": "shared helpers over copies; seams others can build on",
     "dials": {"curiosity": 85, "conscientiousness": 80, "composure": 75, "sociability": 35},
     "drives": hunger("curiosity")},
    {"id": "speed", "name": "Speed", "enabled": True,
     "brief": "latency and waste — hot paths, needless work, snappier feedback",
     "dials": {"risk_appetite": 85, "willpower": 85, "composure": 40, "empathy": 30},
     "drives": hunger("energy")},
]

CURRENT_BUILD: asyncio.Task | None = None


# --- state, factors, ledger (kv only) --------------------------------------

def enabled() -> bool:
    return bool(db.kv_get("repair:enabled"))


def _default_state() -> dict:
    return {"phase": "idle", "sprint_no": 0, "task_idx": 0,
            "sleep_until": 0, "sleep_reason": "", "note": ""}


def state() -> dict:
    with _STATE_LOCK:
        return db.kv_get("repair:state") or _default_state()


def set_state(**patch) -> dict:
    with _STATE_LOCK:                    # read-modify-write: inline the read, don't call
        st = db.kv_get("repair:state") or _default_state()   # state() — it would re-lock
        st.update(patch, updated_at=time.time())
        db.kv_set("repair:state", st)
        return st


def factors() -> list[dict]:
    """The factor list, kv-persisted — and re-synced with the built-in personas when those
    change.

    Two different things live in one list. Which factors are ON, and any factor the owner
    ADDED, are the owner's — nothing here may overwrite them. But what a built-in specialist
    IS (its dials, its drive, what it hunts for) belongs to the code, and the kv copy was
    winning: the running crew kept the psyches it was seeded with on day one, so fixing the
    personas only ever reached a fresh install. The stamp makes the refresh happen exactly
    once per persona revision.
    """
    f = db.kv_get("repair:factors")
    if not f:
        f = [dict(x) for x in DEFAULT_FACTORS]
        db.kv_set("repair:factors", f)
        db.kv_set("repair:factors_v", TEAM_PERSONAS)
        return f
    if int(db.kv_get("repair:factors_v") or 0) != TEAM_PERSONAS:
        builtin = {d["id"]: d for d in DEFAULT_FACTORS}
        for row in f:
            d = builtin.get(row.get("id"))
            if d:                                  # keep `enabled`; refresh what the code owns
                row.update(brief=d["brief"], dials=dict(d["dials"]), drives=dict(d["drives"]))
        db.kv_set("repair:factors", f)
        db.kv_set("repair:factors_v", TEAM_PERSONAS)
    return f


def enabled_factors() -> list[dict]:
    return [f for f in factors() if f.get("enabled")]


def set_factors(patch: list[dict] | None = None, add: dict | None = None,
                remove: str | None = None) -> list[dict]:
    fs = factors()
    for p in patch or []:
        for f in fs:
            if f["id"] == p.get("id"):
                f["enabled"] = bool(p.get("enabled"))
    if add and str(add.get("name", "")).strip():
        name = str(add["name"]).strip()[:40]
        fid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "factor"
        if not any(f["id"] == fid for f in fs):
            fs.append({"id": fid, "name": name, "enabled": True,
                       "brief": str(add.get("brief", ""))[:200] or name, "dials": {}})
    if remove:
        fs = [f for f in fs if f["id"] != remove]
    db.kv_set("repair:factors", fs)
    return fs


def ledger_add(kind: str, model: str = "", usd: float = 0.0, n: int = 1) -> None:
    """`n` is how many MODEL CALLS this row stands for. A deliberation is one row but many
    calls (independent init = one per agent, plus a host plan per later round, plus the closing
    memo) — counting it as 1 was why the meter read 5/6 while ~14 calls had gone out."""
    with _STATE_LOCK:
        rows = db.kv_get("repair:ledger") or []
        rows.append({"ts": time.time(), "kind": kind, "model": model,
                     "usd": round(float(usd or 0), 4), "n": max(1, int(n))})
        db.kv_set("repair:ledger", rows[-500:])


def backlog() -> dict:
    """The crew's ranked to-do list, refilled by ONE scout+deliberation and then drained over
    several sprints. Scouting and deliberating every sprint spent ~4 model calls to produce one
    build; draining a backlog spends ~1, so the same quota buys several times the improvements."""
    return db.kv_get("repair:backlog") or {"ts": 0, "digest": "", "tasks": []}


def save_backlog(b: dict) -> None:
    db.kv_set("repair:backlog", b)


def backlog_fresh(b: dict | None = None, now: float | None = None) -> bool:
    b = b if b is not None else backlog()
    now = now or time.time()
    if not b.get("tasks"):
        return False
    return (now - (b.get("ts") or 0)) < max(1, int(tuning.get("repair_backlog_max_age_h"))) * 3600


def _sprint_key(n: int) -> str:
    return f"repair:sprint:{n}"


def sprint(n: int) -> dict | None:
    return db.kv_get(_sprint_key(n))


def save_sprint(rec: dict) -> None:
    db.kv_set(_sprint_key(rec["no"]), rec)


def _clear_error() -> None:
    """A recorded failure must not outlive the phase that recovered from it — a stale banner
    reads as "still broken" forever (sprint 1's dead scout error was still on screen at
    sprint 3, long after the fix)."""
    if db.kv_get("repair:last_error"):
        db.kv_set("repair:last_error", None)


# --- meters + the sleep decision -------------------------------------------

# EVERY model call counts against the window. Metering only the heavy scout/build sessions
# undercounted reality — the plan deliberation and the extraction spend real tokens too, and a
# meter that reads 5/6 while four other calls went out is not "limits visible at all times".
SESSION_KINDS = ("scout", "build", "plan", "extract", "memo", "chat", "consult", "review")


def meters(now: float | None = None) -> dict:
    now = now or time.time()
    rows = db.kv_get("repair:ledger") or []
    in5 = [r for r in rows if r["kind"] in SESSION_KINDS and now - r["ts"] < 5 * 3600]
    in7 = [r for r in rows if r["kind"] in SESSION_KINDS and now - r["ts"] < 7 * 86400]
    n5 = sum(int(r.get("n", 1)) for r in in5)
    n7 = sum(int(r.get("n", 1)) for r in in7)
    s5 = [r["ts"] for r in in5]
    w7 = [r["ts"] for r in in7]
    usd5 = round(sum(r.get("usd", 0) or 0 for r in rows if now - r["ts"] < 5 * 3600), 2)
    usd7 = round(sum(r.get("usd", 0) or 0 for r in rows if now - r["ts"] < 7 * 86400), 2)
    cap5 = max(1, int(tuning.get("repair_session_cap")))
    cap7 = max(1, int(tuning.get("repair_weekly_cap")))
    model = str(tuning.get("repair_builder_model"))
    cools = {m: launcher.cooldown_left(m) for m in {model, config.ESCALATION_MODEL}
             if launcher.cooldown_left(m) > 0}
    return {"util": usage.snapshot(now),
            "s5h": {"used": n5, "cap": cap5, "usd": usd5,
                    "wake": (min(s5) + 5 * 3600) if n5 >= cap5 and s5 else 0},
            "w7d": {"used": n7, "cap": cap7, "usd": usd7,
                    "wake": (min(w7) + 7 * 86400) if n7 >= cap7 and w7 else 0},
            "backlog": len(backlog().get("tasks", [])),
            "cooldowns": cools, "model": model}


def phase_cost(phase: str) -> int:
    """Model calls the next phase will spend. A deliberation is the expensive one — one call
    per factor-agent for the independent opening round, a host plan per later round, and the
    closing memo — so asking only 'is there any room left' let a plan blow past the cap."""
    if phase == "plan":
        return len(enabled_factors()) + max(0, int(tuning.get("repair_plan_rounds")) - 1) + 1
    return 1                                        # scout, build: one session each


def headroom(now: float | None = None, need: int = 0) -> tuple[bool, str, float]:
    """(ok, reason, wake_ts) — the manager's sleep decision, in one place.

    Four questions, in order of how authoritative the answer is:

    1. Is a model actually rate-limited? The provider said so in its own words
       (launcher parses "session limit · resets 3pm") — nothing overrides that.
    2. Is the quota IDLE? The crew shares one subscription with the owner, so what it may
       spend is what the owner left unused (usage.verdict). This is the real signal: the
       crew backs off within minutes of a human starting work, and helps itself to a quiet
       night without anyone widening a cap.
    3. Have we spent our whole week?
    4. The session-count backstop, for boxes where cost reporting is absent or zero — a
       count of sessions is a crude proxy for consumption, so it is the LAST word, not the
       first. `need` is what the next phase will cost, making the cap a real ceiling rather
       than a starting gun.
    """
    now = now or time.time()
    m = meters(now)
    if m["cooldowns"]:
        model, left = max(m["cooldowns"].items(), key=lambda kv: kv[1])
        return False, f"{model} is cooling down (limit hit)", now + left
    ok, why, wake = usage.verdict(now)
    if not ok:
        return False, why, wake
    if m["w7d"]["used"] >= m["w7d"]["cap"]:
        return False, "weekly session cap reached", m["w7d"]["wake"]
    # The 5-hour SESSION COUNT is the fallback, not the rule. Where the SDK reports tokens,
    # the measured window above is a better answer to "is there room left" than a hand-set
    # number of calls — and letting the counter veto it is what put the crew to sleep for
    # hours beside a completely idle subscription. With no token signal at all, it is all
    # we have.
    if m["util"]["used_tok"] <= 0 and \
            m["s5h"]["cap"] - m["s5h"]["used"] < max(need, int(tuning.get("repair_headroom_min"))):
        wake = m["s5h"]["wake"] or (now + 1800)
        return False, "session window nearly spent", wake
    return True, "", 0.0


def _sleep(reason: str, until: float, kind: str = "headroom", resume: str = "idle") -> None:
    """kind: 'headroom' (wakes early the moment the window rolls enough) | 'cooldown' (a real
    provider wake time — wait it out) | 'pause' (the deliberate breath between sprints).
    `resume` is the phase to return to: sleeping mid-sprint and waking at 'idle' would start a
    NEW sprint over the top of the planned one, throwing away work already paid for.

    A headroom sleep is re-evaluated on every 20s tick, so this is called over and over while
    nothing changes. Only the CHANGE is worth an event: emitting each time buried the crew's
    actual history under one identical 'sleeping' line every twenty seconds."""
    was = state()
    set_state(phase="sleeping", sleep_until=until, sleep_reason=reason, sleep_kind=kind,
              resume_phase=resume)
    if was.get("phase") != "sleeping" or was.get("sleep_reason") != reason:
        bus.emit(0, None, "repair", "repair_sleeping",
                 {"reason": reason, "until": until, "kind": kind})
        logs.info("sleep", "stand_down", reason, kind=kind, resume=resume,
                  wake_in_s=max(0, int(until - time.time())), was=was.get("phase"))


# --- the button --------------------------------------------------------------

def toggle(on: bool) -> bool:
    db.kv_set("repair:enabled", bool(on))
    if on:
        factors()                                   # seed on first enable
        if state()["phase"] == "sleeping" and not state()["sleep_until"]:
            set_state(phase="idle")
    bus.emit(0, None, "repair", "repair_toggled", {"on": bool(on)})
    return bool(on)


async def abort() -> bool:
    """Cancel the in-flight build (if any) and put the current task out of its misery."""
    global CURRENT_BUILD
    if CURRENT_BUILD and not CURRENT_BUILD.done():
        CURRENT_BUILD.cancel()
        try:
            await CURRENT_BUILD
        except (asyncio.CancelledError, Exception):
            pass
    st = state()
    rec = sprint(st["sprint_no"])
    if rec and st["task_idx"] < len(rec.get("tasks", [])):
        t = rec["tasks"][st["task_idx"]]
        t["status"] = "aborted"
        rb.discard(t.get("branch", ""), t.get("worktree"))
        save_sprint(rec)
    set_state(phase="retro")
    bus.emit(0, None, "repair", "repair_aborted", {})
    return True


# --- credentials -------------------------------------------------------------

def _root_settings() -> dict:
    from . import auth, home
    u = auth.get_user_by_name(auth.ROOT_USERNAME)
    return home.default_settings_for(u["id"]) if u else {}


def _live() -> bool:
    return bool(config.AUTH_CONFIGURED)


# --- the crew (a real, visible Studio world) ---------------------------------

def team() -> dict | None:
    return db.kv_get("repair:world")


def ensure_team():
    """The IT crew as a lifeworld world: one persona per enabled factor + the hidden manager.
    Rebuilt (a fresh scene in the same world) whenever the enabled-factor set changes."""
    from . import auth
    from .lifeworld import store
    from .lifeworld_routes import ManifestAgent, ManifestBody, materialise_manifest
    fs = enabled_factors()
    want = sorted(f["id"] for f in fs)
    info = team()
    if (info and info.get("factor_ids") == want and info.get("personas") == TEAM_PERSONAS
            and _team_room_alive(info)):
        return info
    u = auth.get_user_by_name(auth.ROOT_USERNAME)
    if not u or not fs:
        return None
    w = None
    if info and info.get("world_id"):
        w = store.load(info["world_id"])
    if w is None:
        w = store.create(u["id"], "devteam IT crew")
    # Before materialising a fresh seating, look for one that already exists. The kv record
    # is only a POINTER, and it can lose a race the world file wins (two servers once each
    # saved their own idea of this world) — after which the record names a room that is
    # nowhere on disk while the real crew sits alive in another. Rebuilding then would
    # orphan everything keyed to the living humans' ids (the knowledge rows above all).
    # Adopting the surviving room keeps the ids, so the crew keeps what it has learned.
    adopted = _adopt_crew_room(w, fs, want)
    if adopted:
        return adopted
    # What the crew has EARNED must survive a re-seat. Toggling one factor rebuilds the
    # scene, and until now that quietly threw away the manager conversation, the deliberation
    # memos, and — since agents learn — every association each specialist had proved. A
    # factor toggle is a change of lineup, not amnesia. Carried by NAME, because ids are
    # reassigned by the rebuild; the psyche is deliberately NOT carried, since re-seeding it
    # from the dials is the point of the rebuild.
    keep_agents, keep_thread = {}, {}
    if w is not None:
        old = w.scene(info["room_id"]) if (info and info.get("room_id")) else None
        if old is not None:
            for h in old.players():
                keep_agents[h.name] = {"memory": h.memory.to_dict(),
                                       "decisions": h.decisions.to_dict(),
                                       "skills": h.skills.to_dict(), "tau": h.tau}
            t0 = old.threads[0] if old.threads else None
            if t0:
                keep_thread = {"chats": t0.get("chats") or {},
                               "results": t0.get("results") or []}
    names = [f["name"] for f in fs]
    body = ManifestBody(
        name=f"sprint table · {len(fs)} lenses",
        agents=[ManifestAgent(name=f["name"], brief=f["brief"], dials=f.get("dials") or {},
                              drives=f.get("drives") or {}) for f in fs],
        edges=[[names[i], names[(i + 1) % len(names)]] for i in range(len(names))] if len(names) > 1 else [],
        rules="", manager={"model": str(tuning.get("repair_builder_model")), "budget": 2},
        protocol={"preset": "evidence-2026"})
    s = materialise_manifest(w, body)
    from .lifeworld.decisions import DecisionLog
    from .lifeworld.memory import Memory
    from .lifeworld.skills import Skills
    for h in s.players():
        prior = keep_agents.get(h.name)
        if not prior:
            continue
        h.memory = Memory.from_dict(prior.get("memory"))
        h.decisions = DecisionLog.from_dict(prior.get("decisions"))
        h.skills = Skills.from_dict(prior.get("skills"))
        h.tau = int(prior.get("tau") or 0)
    if keep_thread and s.threads:
        s.threads[0]["chats"] = keep_thread.get("chats") or {}
        s.threads[0]["results"] = keep_thread.get("results") or []
    _tidy_crew_world(w, s.id)
    store.save(w)
    info = {"world_id": w.id, "room_id": s.id, "personas": TEAM_PERSONAS,
            "thread_id": s.threads[0]["id"] if s.threads else 0,
            "agents": {f["id"]: hid for f, hid in zip(fs, s.seats)},
            "factor_ids": want}
    db.kv_set("repair:world", info)
    bus.emit(0, None, "repair", "repair_team_ready", {"world_id": w.id, "agents": len(fs)})
    return info


def _team_room_alive(info: dict) -> bool:
    """The freshness check used to trust the pointer if the WORLD loaded; a deleted or
    never-persisted room then 404'd the canvas and silently un-staffed every build (the
    factor→agent ids resolve to nobody) while the check kept saying fine."""
    try:
        from .lifeworld import store
        w = store.load(info["world_id"])
        return bool(w and w.scene(info.get("room_id")) is not None)
    except Exception:
        return False


def _adopt_crew_room(w, fs: list[dict], want: list[str]) -> dict | None:
    """If some scene in this world already seats exactly the crew's personas (matched by
    NAME — ids are whatever history left), point the record at it instead of rebuilding."""
    names = {f["name"] for f in fs}
    for s in w.scenes.values():
        players = list(s.players())
        if {h.name for h in players} != names or not s.threads:
            continue
        by_name = {h.name: h.id for h in players}
        info = {"world_id": w.id, "room_id": s.id, "personas": TEAM_PERSONAS,
                "thread_id": s.threads[0]["id"] if s.threads else 0,
                "agents": {f["id"]: by_name[f["name"]] for f in fs},
                "factor_ids": want}
        db.kv_set("repair:world", info)
        logs.info("lifecycle", "crew_room_adopted",
                  f"the team record pointed at a dead room — adopted surviving room {s.id}",
                  room=s.id, world=w.id)
        bus.emit(0, None, "repair", "repair_team_ready", {"world_id": w.id, "agents": len(fs)})
        return info
    return None


def _tidy_crew_world(w, keep_room: int) -> int:
    """Leave exactly one sprint table behind.

    Every rebuild used to materialise a new scene and simply abandon the old one, so the
    crew's world quietly accumulated a room per persona bump and per factor toggle — six of
    them, five dead, each still holding six agents. It is not only clutter: the world is one
    blob, loaded and saved whole, so the dead rooms were being paid for on every tick. This
    world is entirely ours — nobody authored anything in it by hand — so anything outside
    the current table can go.
    """
    from .lifeworld.human import Human
    dropped = 0
    for sid in [i for i in list(w.scenes) if i != keep_room]:
        w.scenes.pop(sid, None)
        dropped += 1
    seated = set(w.scene(keep_room).seats) if w.scene(keep_room) else set()
    for eid, ent in list(w.entities.items()):
        if isinstance(ent, Human) and eid not in seated:
            w.entities.pop(eid, None)
    return dropped


def _crew_world():
    """(world, scene) for the crew, or (None, None). Loaded free — no model, no live flag."""
    info = team()
    if not info:
        return None, None
    try:
        from .lifeworld import store
        w = store.load(info["world_id"])
        return w, (w.scene(info["room_id"]) if w else None)
    except Exception:
        return None, None


def _specialist(factor: str):
    """The agent that owns a factor. Its decisions and what it learns from them belong to
    IT — that is what makes the crew's experience inspectable per specialist rather than
    pooled into one anonymous heap."""
    info = team()
    w, _ = _crew_world()
    if not (info and w):
        return None, None
    hid = (info.get("agents") or {}).get(factor)
    h = w.get(hid) if hid else None
    from .lifeworld.human import Human
    return (w, h) if isinstance(h, Human) else (w, None)


EXPERIENCE_K = 3   # lessons per build prompt: enough to matter, small enough that the
                   # task — not the memoir — is the prompt


def _task_cue(task: dict) -> str:
    """ONE definition of 'the situation this task is'. note_decision writes under this key
    and specialist_context reads under it — recall only works if the writer and the reader
    key the same way, which is exactly the kind of drift a shared function prevents."""
    return str(task.get("evidence") or task.get("brief") or task.get("title") or "")


async def specialist_context(task: dict) -> dict | None:
    """Everything the build needs to be a SPECIALIST's session rather than an anonymous one:
    who is building, what it is like, what it has PROVEN about situations like this task,
    and which teammates its arrows let it consult.

    None → the anonymous build, byte-identical to today (no crew, unknown factor, offline).
    Read-only — a snapshot load, no world lock; only load→mutate→save cycles need one.
    """
    if not _live():
        return None
    ensure_team()                    # a backlog-driven sprint skips _phase_plan, the only
    factor = str(task.get("factor") or "")       # other place the world is guaranteed built
    w, h = _specialist(factor)
    if not h:
        return None
    try:
        from .lifeworld.decisions import signature
        from .lifeworld.threads import members_of
        from .lifeworld.world import _persona
        from . import knowledge
        cue = _task_cue(task)
        p = _persona(h)
        brief = next((f.get("brief", "") for f in enabled_factors() if f["id"] == factor), "")

        # Experience: proven only, no manufactured authority. The exact association first —
        # its bar (conf ≥ .5, evidence ≥ 2) already guards assumptions. Knowledge hits pass
        # at "held up more often than not": one row per outcome means demanding 2 here would
        # silence the store for its first sprints, and a SUGGESTION may be looser than an
        # assumption.
        experience = []
        exact = h.decisions.recall(signature(cue, kind="task"))
        if exact is not None:
            experience.append({"says": exact.says[:200],
                               "label": f"proven x{exact.evidence}, {exact.confidence:.0%}"})
        for hit in await knowledge.recall(reg_key_for(info_world(), h.id), cue,
                                          k=EXPERIENCE_K, settings=_root_settings()):
            if hit["good"] > hit["bad"] and hit["evidence"] >= 1                     and hit["says"][:200] not in [e["says"] for e in experience]:
                experience.append({"says": hit["says"][:200],
                                   "label": f"held up {hit['good']}/{hit['evidence']}"})

        # Teammates via the arrows: a consult's ANSWER must be able to reach the asker, so a
        # neighbour is someone the asker hears.
        info = team()
        s_ = w.scene(info["room_id"]) if info else None
        t_ = s_.thread(info["thread_id"]) if s_ else None
        neighbours = []
        if t_ is not None:
            for pid in members_of(t_):
                other = w.get(pid)
                if pid != h.id and other is not None and s_._hears(t_, pid, h.id):
                    neighbours.append(other.name)

        logs.info("session", "specialist_briefed",
                  f"{h.name} briefed with {len(experience)} proven lesson(s)"
                  + (f" and {len(neighbours)} consultable teammate(s)" if neighbours else ""),
                  factor=factor, task=str(task.get("title", ""))[:80], lessons=len(experience))
        return {"factor": factor, "name": h.name, "brief": brief,
                "traits": p.get("traits") or {}, "wants": p.get("wants") or "",
                "experience": experience[:EXPERIENCE_K + 1], "neighbours": neighbours,
                "consults_cap": int(tuning.get("repair_consults_per_task"))}
    except Exception as e:
        # A briefing failure must never cost the build: anonymous is a working fallback.
        logs.warn("session", "briefing_failed", str(e)[:200], factor=factor)
        return None


def _resolve_peer(peers: list, who: str):
    """The NAMED half of picking a neighbour: case-insensitive match, or None — which the
    caller reads as 'refuse' when a name was given and as 'pick by knowledge' when none was.
    Returning peers[0] here for the unnamed case looked harmless and quietly disabled the
    knowledge-scored pick entirely."""
    if not who:
        return None
    wl = who.strip().lower()
    return next((p for p in peers if p.name.lower() == wl), None)


async def _best_informed_peer(peers: list, question: str, world_id: int):
    from . import knowledge
    best, score = None, -1.0
    for p in peers:
        try:
            hits = await knowledge.recall(reg_key_for(world_id, p.id), question, k=1,
                                          settings=_root_settings())
            s_ = hits[0]["score"] if hits else 0.0
        except Exception:
            s_ = 0.0
        if s_ > score:
            best, score = p, s_
    return best


async def consult(task: dict, question: str, who: str = "") -> str:
    """A build session asking a crew teammate for help. ALWAYS returns advisory text and
    never raises — a broken consult must cost the session a turn, not its life.

    The arrows are enforced here, not requested: only a graph neighbour the asker can hear
    may answer, a named outsider gets a refusal that names the real neighbours, and every
    answered consult is one bounded call on the ANSWERING agent's own model. Both agents
    genuinely perceive the exchange (free), so it lands in their memories like any other
    conversation — collaboration that leaves no trace in the participants is theatre.
    """
    question = str(question or "").strip()
    if not question:
        return "(ask an actual question — include what you tried and the exact error)"
    consults = task.setdefault("consults", [])
    cap = int(tuning.get("repair_consults_per_task"))
    if len(consults) >= cap:
        return (f"(you have used all {cap} consults for this task — "
                "finish with your best judgement)")
    ok, reason, _wake = headroom(need=1)
    if not ok:
        return f"(no quota room for a consult right now: {reason} — carry on alone)"
    info = team()
    if not (_live() and info):
        return "(the crew is not available — use your best judgement)"
    try:
        from .lifeworld import store
        from .lifeworld.threads import members_of
        from .lifeworld.decisions import signature
        from . import knowledge
        async with store.lock_for(info["world_id"]):
            w = store.load(info["world_id"], live=True, settings=_root_settings())
            s_ = w.scene(info["room_id"]) if w else None
            t_ = s_.thread(info["thread_id"]) if s_ else None
            hid = (info.get("agents") or {}).get(str(task.get("factor") or ""))
            me = w.get(hid) if (w and hid) else None
            if not (t_ is not None and me is not None):
                return "(the crew is not available — use your best judgement)"
            peers = [w.get(i) for i in members_of(t_)
                     if i != me.id and w.get(i) is not None and s_._hears(t_, i, me.id)]
            if not peers:
                return "(you have no teammates on the graph — use your best judgement)"
            nb = _resolve_peer(peers, who)
            if who and nb is None:
                names = ", ".join(p.name for p in peers)
                return (f"You can only consult your graph neighbours — the arrows are the "
                        f"org chart. Yours are: {names}.")
            if nb is None:
                nb = await _best_informed_peer(peers, question, info["world_id"]) or peers[0]
            # The ask goes on the ROOM record first — _hear moves the listener's state but
            # writes no log row, and a consultation that leaves no trace in the room is
            # invisible on the canvas. Then the neighbour genuinely hears it: a free Tier-0
            # perceive that files its own memory, exactly as any overheard remark would.
            s_._record("say", me.id, f"{me.name}: (consult) {question[:180]}", frm=me.id)
            await s_._hear(me, nb, f"(consult) {question[:200]}")
            known = nb.decisions.recall(signature(question))
            recalled = None
            if known is not None:
                recalled = {"says": known.says, "confidence": known.confidence,
                            "seen": known.evidence}
            else:
                hits = await knowledge.recall(reg_key_for(info["world_id"], nb.id), question,
                                              k=1, settings=_root_settings())
                if hits and hits[0]["score"] >= 0.25:
                    recalled = {"says": hits[0]["says"], "confidence": hits[0]["confidence"],
                                "seen": hits[0]["evidence"]}
            ring = [h for h in (w.get(i) for i in members_of(t_)) if h is not None]
            answer = await w.agent_reply(
                nb, question,
                transcript=s_._thread_transcript(ring, thread=t_, for_agent=nb.id),
                recalled=recalled)
            if not answer:
                return f"({nb.name} could not be reached — use your best judgement)"
            s_._record("say", nb.id, f"{nb.name}: {answer[:200]}", frm=nb.id)
            await s_._hear(nb, me, answer)          # the asker absorbs the answer, free
            store.save(w)
        consults.append({"who": nb.name, "q": question[:200], "a": answer[:300],
                         "ts": time.time()})
        ledger_add("consult", w.model_for(nb) if hasattr(w, "model_for") else "", 0)
        bus.emit(0, None, "repair", "repair_consult",
                 {"task": str(task.get("title", ""))[:80], "who": nb.name,
                  "q": question[:160]})
        bus.emit(0, None, "repair", "repair_consult_reply",
                 {"who": nb.name, "a": answer[:200]})
        logs.info("session", "consult", f"{task.get('factor', '?')} asked {nb.name}: "
                  f"{question[:120]}", who=nb.name)
        logs.info("session", "consult_reply", answer[:160], who=nb.name)
        return answer
    except Exception as e:
        logs.warn("session", "consult_failed", str(e)[:200])
        return "(the consult failed — use your best judgement)"


def note_decision(task: dict, saw: str) -> None:
    """Record, on the specialist that owns this task, the decision to attempt it.

    The crew is the right first tenant for this because its outcomes are not a matter of
    opinion: the suite goes green or it does not. An association trained on that is trained
    on truth, which is the difference between learning and superstition.
    """
    factor = str(task.get("factor") or "")
    w, h = _specialist(factor)
    if not h:
        return
    try:
        from .lifeworld.decisions import signature
        d = h.decisions.record(
            tau=int(getattr(h, "tau", 0)), sig=signature(saw, kind="task"), saw=saw,
            understood=str(task.get("brief", ""))[:200],
            chose=f"build: {task.get('title', '')}",
            because={"factor": factor, "type": task.get("type"),
                     "priority": task.get("priority"), "attempt": task.get("attempts")})
        task["decision_id"] = d.id
        from .lifeworld import store
        store.save(w)
    except Exception:
        pass


def _note_activity(task: dict, state: str, what: str) -> None:
    """Put the specialist that owns this task on the board. The crew's agents are the ones
    doing the longest-running work on the platform, so they are exactly what a monitor is
    for — and until now a build in flight was visible only as a phase string in kv."""
    from . import agents as reg
    info, factor = team(), str(task.get("factor") or "")
    hid = ((info or {}).get("agents") or {}).get(factor)
    if not (info and hid):
        return
    reg.note(reg.key_for("lw", info["world_id"], hid), state, what,
             name=factor, where="self-repair", kind="agent")


def _release_activity(task: dict) -> None:
    from . import agents as reg
    info, factor = team(), str(task.get("factor") or "")
    hid = ((info or {}).get("agents") or {}).get(factor)
    if info and hid:
        reg.done(reg.key_for("lw", info["world_id"], hid))


def reg_key_for(world_id: int, human_id: int) -> str:
    from .agents import key_for
    return key_for("lw", world_id, human_id)


# --- the module graph: real work lights the node that owns it -----------------
#
# The canvas highlight follows the sprint, not a simulation: when a specialist's
# build session starts, the node whose boundary manifest owns the task gets a
# trace row and a pid-0 event; when the session ends, the run closes and the
# glow goes out. Everything here is best-effort by construction — the graph is a
# map of the work, and a map that can crash the expedition is worse than no map.

_GRAPH_FILE_RE = re.compile(r"\b[\w./-]+\.(?:py|js|css|html|md|sh|json|yml|yaml)\b")


def _graph_specialist_id(task: dict) -> int | None:
    hid = ((team() or {}).get("agents") or {}).get(str(task.get("factor") or ""))
    return int(hid) if hid else None


def _graph_node_for(task: dict) -> tuple[int, str] | None:
    """(plan_id, node_key) of the active devteam plan node that owns this task.

    Matched the same way the /api/graph/self activity payload matches: the
    task's own file mentions (task['files'] when the planner said, else paths
    named in title/brief/evidence) prefix-matched to each node's boundary
    manifest. When the task names no files at all, the specialist's ASSIGNED
    node (graph_assign, written by the manager's authoring pass) answers.

    LEAVES only: a group's boundary is the union of its children's, so letting
    groups into the match would swallow every build their children should own —
    runs land on modules, the payload rolls them up to the layer."""
    from . import modgraph
    plan = modgraph.active_plan(0)
    if not plan:
        return None
    pid = int(plan["id"])
    nodes = [n for n in modgraph.nodes(pid) if n["node_type"] != "group"]
    files = [str(f) for f in (task.get("files") or [])]
    if not files:
        text = " ".join(str(task.get(k) or "") for k in ("title", "brief", "evidence"))
        files = _GRAPH_FILE_RE.findall(text)
    for n in nodes:
        if any(f == p or (p.endswith("/") and f.startswith(p))
               for f in files for p in n["paths"]):
            return pid, n["key"]
    hid = _graph_specialist_id(task)
    if hid:
        for n in nodes:
            a = modgraph.get_assign(pid, n["key"])
            if a and a.get("agent_id") == hid:
                return pid, n["key"]
    return None


def _graph_build_started(task: dict) -> None:
    """Open a running build row on the owning node and light it on the canvas."""
    try:
        from . import modgraph
        own = _graph_node_for(task)
        if not own:
            return
        pid, key = own
        rid = modgraph.note_run(pid, key, "build", agent_id=_graph_specialist_id(task),
                                status="running",
                                detail=json.dumps({"title": str(task.get("title") or "")[:140]}))
        task["graph_node"] = {"plan": pid, "key": key}
        task["graph_run"] = rid
        bus.emit(0, None, "graph", "graph_node_active",
                 {"key": key, "agent": str(task.get("factor") or ""),
                  "task": str(task.get("title") or "")[:140]})
    except Exception as e:
        logs.warn("sprint", "graph_hook_failed", str(e)[:200], where="build_started")


def _graph_build_ended(task: dict, ok: bool, detail: str = "") -> None:
    """Close the open build run (success or death) and put the glow out."""
    try:
        from . import modgraph
        rid = task.pop("graph_run", None)
        key = (task.get("graph_node") or {}).get("key")
        if rid:
            modgraph.close_run(int(rid), "ok" if ok else "failed", detail=detail[:200])
        if key:
            bus.emit(0, None, "graph", "graph_node_idle", {"key": key})
    except Exception as e:
        logs.warn("sprint", "graph_hook_failed", str(e)[:200], where="build_ended")


def _graph_verified(task: dict, ok: bool, headline: str = "") -> None:
    """One closed verify row on the owning node: what the suite said, on the map."""
    try:
        from . import modgraph
        gn = task.get("graph_node")
        if not gn:
            own = _graph_node_for(task)
            if not own:
                return
            gn = {"plan": own[0], "key": own[1]}
        rid = modgraph.note_run(int(gn["plan"]), gn["key"], "verify",
                                agent_id=_graph_specialist_id(task),
                                status="ok" if ok else "failed", detail=headline[:200])
        modgraph.close_run(rid, "ok" if ok else "failed", detail=headline[:200])
    except Exception as e:
        logs.warn("sprint", "graph_hook_failed", str(e)[:200], where="verified")


def info_world() -> int:
    return int((team() or {}).get("world_id") or 0)


async def note_outcome(task: dict, ok: bool, says: str = "") -> None:
    """Stamp how it turned out, which is the only thing that moves an association."""
    did = task.get("decision_id")
    factor = str(task.get("factor") or "")
    if not did:
        return
    w, h = _specialist(factor)
    if not h:
        return
    try:
        h.decisions.resolve(int(did), "good" if ok else "bad", says=says[:200])
        from .lifeworld import store
        store.save(w)
    except Exception:
        pass
    # ...and into the knowledge base, keyed on the SITUATION, so the next task that looks
    # like this one finds it without needing the same exact signature.
    try:
        from . import knowledge
        node = h.decisions.get(int(did))
        if node and says:
            await knowledge.remember(
                reg_key_for(info_world(), h.id), cue=node.saw or task.get("title", ""),
                says=says[:400], sig=node.sig, kind="belief",
                payload={"task": task.get("title", ""), "factor": task.get("factor", "")},
                good=1 if ok else 0, bad=0 if ok else 1, settings=_root_settings())
    except Exception:
        pass


def team_usage() -> list[dict]:
    info = team()
    if not info:
        return []
    try:
        from .lifeworld import store
        w = store.load(info["world_id"])
        out = []
        for fid, hid in (info.get("agents") or {}).items():
            h = w.get(hid) if w else None
            if h is not None:
                out.append({"factor": fid, "name": h.name, "usage": h.usage()})
        return out
    except Exception:
        return []


# --- the sprint pipeline -----------------------------------------------------

async def tick() -> None:
    if not enabled() or not state():
        return
    st = state()
    if st["phase"] == "sleeping":
        # A headroom sleep is a GUESS at when the window rolls — if the meters recover sooner
        # (or a cooldown expires), wake now rather than sitting out a stale wake time. A pause
        # between sprints is deliberate, so it waits out its clock.
        ok, why, wake = headroom()
        early = st.get("sleep_kind", "headroom") != "pause" and ok
        if not early and time.time() < (st.get("sleep_until") or 0):
            # Still asleep — but say WHY it is still asleep, not why it fell asleep. The
            # reason is written once and then never revisited, so the banner could read
            # "used its share of this window" beside a meter showing the window 100% idle.
            if why and why != st.get("sleep_reason") and st.get("sleep_kind") != "pause":
                set_state(sleep_reason=why, sleep_until=max(wake, time.time() + TICK_SECONDS))
            return
        st = set_state(phase=st.get("resume_phase") or "idle", sleep_until=0,
                       sleep_reason="", sleep_kind="", resume_phase="")
    if st["phase"] == "restarting":                 # we came back up — the restart happened
        st = set_state(phase="idle")
    try:
        from . import monitor
        for did in await monitor.sweep():           # unattended approvals, if root asked for them
            bus.emit(0, None, "repair", "repair_auto_approved", did)
    except Exception as e:
        logs.warn("lifecycle", "monitor_sweep_failed", str(e)[:200])
    await advance(st)


async def advance(st: dict) -> None:
    with usage.attributed("repair"):
        await _advance(st)


def _phase_log(phase: str, st: dict) -> None:
    """One line per transition. The phase machine is the thing you most want a history of
    when a sprint went somewhere unexpected, and until now it existed only as the CURRENT
    value of one kv key — by the time anyone looked, it had moved on."""
    logs.info("sprint", f"phase_{phase}", f"entering {phase}",
              sprint=st.get("sprint_no"), task=st.get("task_idx"))


async def _advance(st: dict) -> None:
    """One persisted phase transition. Everything it spends is billed to the crew — the
    deliberation goes through the same `providers.complete` a Studio seat uses, and filed as
    the owner's it would read as 'someone else is using the quota', putting the crew to sleep
    on its own footsteps."""
    phase = st["phase"]
    _phase_log(phase, st)
    if phase == "idle":
        # a sprint that must scout+plan needs room for the whole plan, not just the scout
        ok, reason, wake = headroom(need=1 if backlog_fresh() else phase_cost("plan") + 1)
        if not ok:
            return _sleep(reason, wake)
        n = int(db.kv_get("repair:seq") or 0) + 1
        rec = {"no": n, "started_at": time.time(), "scout": {}, "memo": None,
               "tasks": [], "retro": "", "landed": 0, "failed": 0, "landed_files": []}
        b = backlog()
        if backlog_fresh(b):                         # straight to work — no scout, no deliberation
            take = max(1, int(tuning.get("repair_tasks_per_sprint")))
            rec["tasks"], b["tasks"] = b["tasks"][:take], b["tasks"][take:]
            rec["scout"] = {"digest": b.get("digest", ""), "candidates": [], "from_backlog": True}
            save_backlog(b)
            save_sprint(rec)
            set_state(phase="build", sprint_no=n, task_idx=0, note="")
            bus.emit(0, None, "repair", "repair_sprint_started",
                     {"sprint": n, "from_backlog": True, "left": len(b["tasks"])})
            return
        save_sprint(rec)
        set_state(phase="scout", sprint_no=n, task_idx=0, note="")
        bus.emit(0, None, "repair", "repair_sprint_started", {"sprint": n})
        return

    rec = sprint(st["sprint_no"])
    if rec is None:                                  # state points at a missing sprint — reset
        set_state(phase="idle")
        return

    if phase == "scout":
        await _phase_scout(st, rec)
    elif phase == "plan":
        await _phase_plan(st, rec)
    elif phase == "build":
        await _phase_build(st, rec)
    elif phase == "verify":
        await _phase_verify(st, rec)
    elif phase == "land":
        await _phase_land(st, rec)
    elif phase == "retro":
        await _phase_retro(st, rec)


def _rate_limited(err: str) -> bool:
    if launcher.looks_rate_limited(err):
        launcher.note_rate_limit(str(tuning.get("repair_builder_model")), err)
        ok, reason, wake = headroom()
        _sleep(reason or "provider limit hit", wake or time.time() + 300, kind="cooldown",
               resume=state().get("phase") if state().get("phase") != "sleeping" else "idle")
        return True
    return False


async def _phase_scout(st: dict, rec: dict) -> None:
    """Survey the repo in ONE read-only session and come back with candidate improvements,
    one per enabled factor. Budget-aware by construction: it samples rather than reads
    everything, because a scout that spends its whole turn allowance exploring has nothing
    left to report. Offline it produces a deterministic empty digest and the sprint still
    completes, free."""
    if not _live():
        rec["scout"] = {"digest": "(offline — no credentials; sprint runs deterministically)",
                        "candidates": []}
    else:
        try:
            out = await rb.scout(enabled_factors(), _root_settings())
            ledger_add("scout", out.get("model", ""), out.get("usd", 0))
            rec["scout"] = {"digest": out["digest"], "candidates": out["candidates"]}
        except Exception as e:
            ledger_add("scout", str(tuning.get("repair_builder_model")), 0)   # a failed session still burned quota
            if _rate_limited(str(e)):
                return
            db.kv_set("repair:last_error", {"ts": time.time(), "phase": "scout", "detail": str(e)[:400]})
            rec["scout"] = {"digest": f"(scout failed: {str(e)[:120]})", "candidates": []}
    save_sprint(rec)
    if rec["scout"]["candidates"]:
        _clear_error()
    set_state(phase="plan")
    bus.emit(0, None, "repair", "repair_scouted", {"sprint": rec["no"],
                                                   "candidates": len(rec["scout"]["candidates"])})


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:32] or "task"


def _tasks_from_candidates(cands: list[dict], n: int) -> list[dict]:
    out = []
    for c in cands[:n]:
        out.append({"slug": _slug(c["title"]), "title": c["title"], "factor": c["factor"],
                    "brief": f"{c['why']}\nLikely files: {', '.join(c.get('files') or [])}",
                    "acceptance": [], "branch": "", "status": "pending", "worktree": None,
                    "verification": None, "landed_sha": None, "attempts": 0, "evidence": ""})
    return [_typed(t) for t in out]


# What KIND of change this is, and how much it matters. Two different questions that were
# being answered by one undifferentiated list: "the crew did 7 things" tells you nothing,
# while "4 bugs (one P1), 2 features, an idea" is a status you can act on. Kept to five types
# a person would actually use — a taxonomy nobody can apply consistently is worse than none.
TASK_TYPES = {
    "bug":      "something behaves wrong, or could",
    "feature":  "something the platform could not do before",
    "refactor": "the same behaviour, in a shape that is easier to live with",
    "chore":    "upkeep — deletions, renames, dependency and config work",
    "idea":     "worth trying, not yet worth committing to",
}
PRIORITIES = {"p1": "drop everything", "p2": "this sprint", "p3": "when there is room"}

def _recent_too_big(limit: int = 5) -> list[str]:
    """Titles of tasks that ran out of turns lately — the crew's own evidence about what it
    consistently over-scopes."""
    out = []
    for rec in sorted(db.kv_prefix("repair:sprint:").values(),
                      key=lambda r: r.get("no", 0), reverse=True)[:8]:
        for t in rec.get("tasks", []):
            if t.get("too_big") and t.get("title"):
                out.append(str(t["title"])[:100])
    return out[:limit]


PLAN_EXTRACT_SYSTEM = (
    "You turn a sprint-planning memo into build tasks. Each task must be finishable by ONE "
    "focused engineer in roughly 30 tool calls — a narrow, surgical change to a handful of "
    "files. Split or shrink anything broader (a repo-wide refactor is not a task, it is a "
    "programme; take its first slice instead). Return ONLY a JSON array of "
    '{"title": <short imperative>, "factor": <factor id>, "type": <bug|feature|refactor|'
    'chore|idea>, "priority": <p1|p2|p3>, "brief": <2-4 sentences of exactly '
    'what to change and where>, "acceptance": [<checkable outcomes>]} — at most N items, '
    "highest value first. Be honest about type and priority: p1 means the platform is "
    "broken or losing data for someone right now, and most work is not p1. "
    "A task that names more than about three files, or says 'extract', 'migrate' or "
    "'refactor X into Y' about a whole module, is a PROGRAMME and will die unfinished — "
    "emit its first concrete slice instead.")


# A task that names half a module is a programme. The planner is TOLD this and still
# proposes them — ten of eighteen failures — so the code checks rather than hoping.
_PROGRAMME = re.compile(
    r"\b(extract|migrate|refactor|rewrite|redesign|consolidat\w*|split)\b.{0,60}"
    r"\b(all|every|entire|whole|across|module|codebase|routes\.py|app\.js)\b", re.I)


def too_ambitious(task: dict) -> str:
    """Why this task will die unfinished, or "" if it looks like one session's work.

    Two mechanical signals, both learned from the failures: it names more files than one
    focused change touches, or it is phrased as a programme rather than an edit. Neither is
    clever, and that is the point — a subtle rule here would reject things nobody can predict,
    and the crew would learn to write around it instead of writing smaller.
    """
    text = f"{task.get('title', '')} {task.get('brief', '')}"
    files = set(re.findall(r"\b[\w./-]+\.(?:py|js|css|html|md|sh|json|yml|yaml)\b", text))
    if len(files) > 4:
        return f"names {len(files)} files — one session changes a handful"
    if _PROGRAMME.search(text):
        return "phrased as a programme, not an edit"
    return ""


def classify(title: str, brief: str = "", factor: str = "") -> tuple[str, str]:
    """(type, priority) from the words, for when the model did not say.

    A deterministic fallback rather than a default of "chore/p3": offline sprints, older
    backlog rows and any model that ignores the schema all still get a sensible label, and
    the board stays readable instead of showing one grey blob. Deliberately shallow — it
    reads like the guess it is, and the model's own answer always wins.
    """
    t = f"{title} {brief}".lower()
    if any(w in t for w in ("race", "crash", "leak", "wrong", "broken", "fails", "bug",
                            "silently", "never reconcil", "orphan", "stale", "unsandboxed",
                            "swallow", "hides", "blanket except", "loses", "double")):
        kind = "bug"
    elif any(w in t for w in ("add ", "support ", "introduce", "expose", "surface ", "show ")):
        kind = "feature"
    elif any(w in t for w in ("extract", "dedupe", "one helper", "reuse", "consolidat",
                              "rename", "simplif")):
        kind = "refactor"
    elif any(w in t for w in ("delete", "remove", "clean up", "tidy", "upgrade", "bump")):
        kind = "chore"
    else:
        kind = "idea" if factor == "" else "chore"
    if kind == "bug" and any(w in t for w in ("data loss", "credential", "secret", "unsandboxed",
                                              "security", "corrupt", "cannot start")):
        return kind, "p1"
    return kind, {"bug": "p2", "feature": "p3", "refactor": "p3", "chore": "p3", "idea": "p3"}[kind]


def _typed(task: dict) -> dict:
    """Stamp a task with a type and priority, trusting the model when it gave usable ones."""
    kind = str(task.get("type") or "").lower().strip()
    prio = str(task.get("priority") or "").lower().strip()
    if kind not in TASK_TYPES or prio not in PRIORITIES:
        gk, gp = classify(task.get("title", ""), task.get("brief", ""), task.get("factor", ""))
        kind = kind if kind in TASK_TYPES else gk
        prio = prio if prio in PRIORITIES else gp
    task["type"], task["priority"] = kind, prio
    return task


async def _phase_plan(st: dict, rec: dict) -> None:
    """The crew deliberates, and the decision memo IS the plan. One bounded round per
    configured round plus one opening call per agent, then a single extraction call turns
    the memo into concrete tasks. Plans several sprints ahead into the backlog: planning is
    the expensive phase, so it is amortised rather than repeated every sprint."""
    ok, reason, wake = headroom(need=phase_cost("plan"))
    if not ok:
        return _sleep(reason, wake, resume="plan")
    n_tasks = max(1, int(tuning.get("repair_backlog_size")))    # plan a BACKLOG, not one sprint
    memo = None
    try:
        info = ensure_team()
        if info:
            from .lifeworld import store
            # Held across load…save: the chat route does the same round trip on this world,
            # and without the lock whichever finished last silently erased the other — a
            # sprint's memo, or the operator's conversation.
            async with store.lock_for(info["world_id"]):
              w = store.load(info["world_id"], live=_live(), settings=_root_settings() if _live() else None)
              s = w.scene(info["room_id"]) if w else None
              thread = s.thread(info["thread_id"]) if s else None
              if thread is not None:
                  lenses = "\n".join(f"- {f['id']}: {f['brief']}" for f in enabled_factors())
                  thread["topic"] = f"sprint {rec['no']}: what the devteam platform needs next"
                  thread["rulebook"] = (
                      "You are the platform's own IT crew planning the NEXT FEW SPRINTS on the "
                      "devteam codebase — one shared backlog you will work through in order.\n"
                      f"Lenses:\n{lenses}\nScout findings:\n{rec['scout']['digest'] or '(none)'}\n"
                      f"Decide the top {n_tasks} improvements, best first; the recommendation must "
                      "name each task's title, factor, target files and acceptance checks.")[:2000]
                  too_big = _recent_too_big()
                  if too_big:
                      # The crew kept proposing programme-sized work — "extract a router from
                      # routes.py" — and each one died on turns. Showing it its own oversized
                      # tasks is cheaper and more specific than any amount of prompt adjectives.
                      thread["rulebook"] += (
                          "\nThese were planned before and DIED because one engineer could not "
                          "finish them in a single session — do not propose work of this size "
                          "again; take a first slice instead:\n- " + "\n- ".join(too_big[:5]))
                  rounds = int(tuning.get("repair_plan_rounds"))
                  memo = await s.run_deliberation(thread, rounds=rounds)
                  store.save(w)
                  # independent init spends one call per agent for the opening round, a host plan
                  # per later round, and one closing memo — meter what actually went out.
                  from .lifeworld.threads import protocol_of
                  per_agent = len(ring_names) if (ring_names := [f for f in enabled_factors()]) and \
                      protocol_of(thread).get("init") == "independent" else 1
                  ledger_add("plan", str(tuning.get("repair_builder_model")), 0,
                             n=per_agent + max(0, rounds - 1) + 1)
                  # What each specialist actually argued, and what the manager concluded. The
                  # deliberation lived only in the Studio world's scene log, so from the Improve
                  # screen the plan appeared out of nowhere — the crew's reasoning is the most
                  # interesting thing it produces and it was the one thing not on screen.
                  names = (memo or {}).get("names") or {}
                  bus.emit(0, None, "repair", "repair_deliberated", {
                      "sprint": rec["no"],
                      "positions": [{"who": names.get(str(p.get("who")), "") or p.get("name", ""),
                                     "text": str(p.get("position") or p.get("text") or "")[:400]}
                                    for p in ((memo or {}).get("positions") or [])][:12],
                      "recommendation": str((memo or {}).get("recommendation") or "")[:1200]})
    except Exception as e:
        db.kv_set("repair:last_error", {"ts": time.time(), "phase": "plan", "detail": str(e)[:400]})
    rec["memo"] = memo
    tasks: list[dict] = []
    if _live() and memo:
        try:
            from . import providers
            raw = await providers.complete(
                "anthropic", str(tuning.get("repair_builder_model")),
                PLAN_EXTRACT_SYSTEM.replace("N", str(n_tasks)),
                f"MEMO:\n{memo.get('recommendation', '')}\n"
                + "\n".join(p.get("position", "") for p in memo.get("positions", []))
                + f"\nSCOUT:\n{rec['scout']['digest']}",
                _root_settings(), max_tokens=800)
            ledger_add("extract", str(tuning.get("repair_builder_model")), 0)
            arr = rb._json_block(raw, "[", "]") or []
            dropped = []
            for t in list(arr):
                why = too_ambitious(t) if isinstance(t, dict) else "not a task"
                if why:
                    dropped.append(f"{str(t.get('title', '?'))[:60]} ({why})")
                    arr.remove(t)
            if dropped:
                # Said out loud, and fed back into the next plan. A silent filter would look
                # like the crew planning less, rather than planning smaller.
                logs.warn("sprint", "task_too_ambitious",
                          f"dropped {len(dropped)} over-scoped task(s) before they could burn a "
                          f"session: {'; '.join(dropped[:3])}", sprint=rec["no"])
            for t in arr[:n_tasks]:
                if isinstance(t, dict) and str(t.get("title", "")).strip():
                    tasks.append(_typed({
                                  "type": t.get("type"), "priority": t.get("priority"),
                                  "slug": _slug(str(t["title"])), "title": str(t["title"])[:140],
                                  "factor": str(t.get("factor", ""))[:24],
                                  "brief": str(t.get("brief", ""))[:800],
                                  "acceptance": [str(a)[:200] for a in (t.get("acceptance") or [])][:6],
                                  "branch": "", "status": "pending", "worktree": None,
                                  "verification": None, "landed_sha": None, "attempts": 0,
                                  "evidence": ""}))
        except Exception as e:
            if _rate_limited(str(e)):
                rec["memo"] = memo
                save_sprint(rec)
                return
    if not tasks:
        tasks = _tasks_from_candidates(rec["scout"]["candidates"], n_tasks)
    # The plan fills the BACKLOG; this sprint takes only its share and leaves the rest for the
    # next sprints to drain without paying for another scout+deliberation.
    take = max(1, int(tuning.get("repair_tasks_per_sprint")))
    rec["tasks"], rest = tasks[:take], tasks[take:]
    save_backlog({"ts": time.time(), "digest": rec["scout"].get("digest", ""), "tasks": rest})
    tasks = rec["tasks"]
    save_sprint(rec)
    if not tasks:
        rec["retro"] = "nothing worth doing surfaced this sprint"
        save_sprint(rec)
        set_state(phase="retro")
    else:
        set_state(phase="build", task_idx=0)
    bus.emit(0, None, "repair", "repair_planned", {"sprint": rec["no"], "tasks": len(tasks)})
    # The manager owns the MODULE GRAPH too: after the deliberation, before the build,
    # ONE bounded call authors the devteam plan the canvas reveals (modgraph_author) —
    # once per LINEUP, not per sprint (the kv stamp is the staleness check), and never
    # allowed to cost the sprint anything but this guarded attempt.
    try:
        from . import modgraph_author
        if modgraph_author.should_author() and headroom(need=1)[0]:
            await modgraph_author.author_self_plan()
    except Exception as e:
        logs.warn("sprint", "graph_author_failed", str(e)[:200])


def _task(st: dict, rec: dict) -> dict | None:
    i = st["task_idx"]
    return rec["tasks"][i] if i < len(rec["tasks"]) else None


async def _phase_build(st: dict, rec: dict) -> None:
    """One coding session per task, in a disposable git worktree, tracked as a cancellable
    task so the abort button means something. The engine commits, not the model. A session
    that dies on turns is marked too big and is never retried — only test failures are."""
    global CURRENT_BUILD
    t = _task(st, rec)
    if t is None:
        set_state(phase="retro")
        return
    # Before anything else, including any question of credentials or quota: has this task
    # already had its chances? A restart resumes the sprint — correct — but starts a FRESH
    # session for the same task, so without a ceiling an interrupted task is an open tab on
    # the quota rather than a failure. Sprint 14 reached five attempts that way, one per
    # restart, each a whole session spent with nothing to show.
    if int(t.get("attempts") or 0) >= int(tuning.get("repair_max_attempts")):
        t["status"] = "failed"
        t["error"] = f"gave up after {t['attempts']} sessions — it never finished one"
        rec["failed"] += 1
        rb.discard(t.get("branch", ""), t.get("worktree"))
        save_sprint(rec)
        set_state(task_idx=st["task_idx"] + 1)
        logs.warn("session", "gave_up", f"{t['title']} — {t['error']}", task=t["title"],
                  attempts=t["attempts"])
        bus.emit(0, None, "repair", "repair_task_failed",
                 {"task": t["title"], "why": t["error"]})
        return
    if not _live():                                 # offline: nothing can build — mark and move on
        t["status"] = "failed"
        t["verification"] = {"ran": False, "ok": False, "headline": "offline — no credentials to build"}
        rec["failed"] += 1
        save_sprint(rec)
        set_state(task_idx=st["task_idx"] + 1)
        return
    ok, reason, wake = headroom(need=phase_cost("build"))
    if not ok:
        return _sleep(reason, wake, resume="build")      # come back to THIS task, not a new sprint
    t["status"] = "building"
    t["attempts"] += 1
    save_sprint(rec)
    bus.emit(0, None, "repair", "repair_building", {"sprint": rec["no"], "task": t["title"]})
    logs.info("session", "build_started", t["title"], sprint=rec["no"],
              factor=t.get("factor"), attempt=t.get("attempts"))
    # A backlog-driven sprint skips _phase_plan, which was the only place the crew world was
    # guaranteed built — so on a fresh box the first build's note_decision found no team and
    # silently recorded nothing. The specialists must exist before anything is filed on them.
    ensure_team()
    note_decision(t, _task_cue(t))
    _note_activity(t, "building", t["title"])
    _graph_build_started(t)                          # the module graph lights the node
    crew = await specialist_context(t)
    if crew and crew.get("neighbours"):
        import functools
        crew["ask"] = functools.partial(consult, t)
    try:
        wt = t.get("worktree")
        CURRENT_BUILD = asyncio.ensure_future(
            rb.build(t, rec["no"], _root_settings(), wt=None if wt is None else Path(wt),
                     crew=crew))
        out = await CURRENT_BUILD
        CURRENT_BUILD = None
        t["branch"], t["worktree"] = out["branch"], out["worktree"]
        ledger_add("build", str(tuning.get("repair_builder_model")), out.get("usd", 0))
        _graph_build_ended(t, True)
        t["status"] = "verifying"
        save_sprint(rec)
        set_state(phase="verify")
    except asyncio.CancelledError:
        CURRENT_BUILD = None
        _graph_build_ended(t, False, "aborted")
        return                                      # abort() already rewrote the state
    except Exception as e:
        CURRENT_BUILD = None
        ledger_add("build", str(tuning.get("repair_builder_model")), 0)       # a failed session still burned quota
        _graph_build_ended(t, False, str(e)[:200])
        logs.error("session", "build_died", str(e)[:300], task=t["title"],
                   attempt=t.get("attempts"), kind=type(e).__name__)
        if _rate_limited(str(e)):
            t["status"] = "pending"                 # resume this same task after the sleep
            t["attempts"] -= 1
            save_sprint(rec)
            return
        t["status"] = "failed"
        t["error"] = str(e)[:400]
        # A session that ran out of TURNS is too big, not unlucky — re-running the same prompt
        # buys the same death (sprint 3 spent 128 minutes proving it). Mark it so verify's
        # retry path can never pick it up, and tell the crew to re-scope it next time.
        if "maximum number of turns" in str(e).lower():
            t["too_big"] = True
            t["error"] = "too big for one session — needs re-scoping into a smaller slice"
        rec["failed"] += 1
        if t.get("branch"):
            rb.discard(t["branch"], t.get("worktree"))
        save_sprint(rec)
        set_state(task_idx=st["task_idx"] + 1)


async def _phase_verify(st: dict, rec: dict) -> None:
    """Run the platform's OWN full test suite inside the worktree — the same gates a human's
    change must pass, including all the repo's self-protecting ones. Red once buys a retry
    with the failures as evidence; red twice fails the task and the branch is discarded."""
    t = _task(st, rec)
    if t is None or not t.get("worktree"):
        set_state(phase="build")
        return
    bad = rb.protected_violation(t["worktree"])
    if bad:
        t["status"] = "failed"
        t["error"] = f"touched a protected path: {bad}"
        logs.error("sandbox", "protected_path",
                   f"a build touched {bad} — refused before it could be verified",
                   task=t["title"], path=bad)
        rec["failed"] += 1
        rb.discard(t["branch"], t["worktree"])
        save_sprint(rec)
        set_state(phase="build", task_idx=st["task_idx"] + 1)
        bus.emit(0, None, "repair", "repair_task_failed", {"task": t["title"], "why": t["error"]})
        logs.warn("session", "build_failed", f"{t['title']} — {t['error']}",
                  sprint=rec["no"], factor=t.get("factor"))
        return
    _note_activity(t, "verifying", t["title"])
    res = await rb.verify(t["worktree"])
    t["verification"] = res
    # What the suite said is the outcome that teaches. A red headline is also the situation
    # this specialist will recognise next time it sees one like it.
    _release_activity(t)
    _graph_verified(t, bool(res.get("ok")), str(res.get("headline") or ""))
    await note_outcome(t, bool(res.get("ok")),
                 says=("the change held up" if res.get("ok")
                       else f"this kind of change breaks: {res.get('headline', '')}"))
    logs.log("verify", "suite_green" if res.get("ok") else "suite_red",
             res.get("headline", "") or ("the suite passed" if res.get("ok") else "the suite failed"),
             level="info" if res.get("ok") else "warn",
             task=t["title"], attempt=t.get("attempts"),
             failures=len(res.get("failures") or []))
    if res.get("ok"):
        # The suite proves the change WORKS; a neighbour judges whether it is the RIGHT
        # change — different questions. One bounded call, one send-back per task, and the
        # send-back rides the exact plumbing a red suite uses: evidence, same branch.
        verdict = await _review_green(t, rec)
        if verdict is not None:
            t["sent_back"] = True
            t["evidence"] = f"a reviewer ({verdict['who']}) sent it back: {verdict['feedback']}"
            t["status"] = "pending"
            save_sprint(rec)
            set_state(phase="build")               # same task_idx → retry with the feedback
            bus.emit(0, None, "repair", "repair_review",
                     {"task": t["title"], "verdict": "send_back", "who": verdict["who"],
                      "feedback": verdict["feedback"][:200]})
            logs.info("verify", "review_sent_back",
                      f"{verdict['who']} sent back: {t['title']} — {verdict['feedback'][:120]}",
                      task=t["title"], who=verdict["who"])
            return
        t["status"] = "green"
        save_sprint(rec)
        set_state(phase="land")
        return
    if not t.get("too_big") and t["attempts"] <= int(tuning.get("repair_fix_attempts")):
        t["evidence"] = "\n".join(res.get("failures") or [])[:1500] or res.get("headline", "")
        logs.info("session", "build_retry", f"{t['title']} — retrying with the failures as evidence",
                  task=t["title"], attempt=t.get("attempts"))
        t["status"] = "pending"
        save_sprint(rec)
        set_state(phase="build")                    # same task_idx → retry with evidence
        return
    t["status"] = "failed"
    rec["failed"] += 1
    rb.discard(t["branch"], t["worktree"])
    save_sprint(rec)
    set_state(phase="build", task_idx=st["task_idx"] + 1)
    bus.emit(0, None, "repair", "repair_task_failed",
             {"task": t["title"], "why": res.get("headline", "tests failed")})
    logs.warn("verify", "task_abandoned",
              f"{t['title']} — {res.get('headline', 'tests failed')}",
              task=t["title"], too_big=bool(t.get("too_big")), attempts=t.get("attempts"))


async def _review_green(task: dict, rec: dict) -> dict | None:
    """A graph neighbour reads the green diff. None = approve; {who, feedback} = send back.

    FAIL-OPEN everywhere: knob off, already sent back once, no crew, no neighbours, the
    reviewer unreachable, or a reply that does not parse — all approve. A green suite must
    never be blocked by machinery being absent or by a reviewer's formatting. The reviewer
    only VERDICTS; it never edits — two writers on one branch is the mars-rover
    double-dispatch bug wearing a lab coat.
    """
    if not bool(tuning.get("repair_review")) or task.get("sent_back"):
        return None
    info = team()
    if not (_live() and info and task.get("worktree")):
        return None
    try:
        from .lifeworld import store
        from .lifeworld.threads import members_of
        # The stat AND a bounded slice of the real diff. The gate's first live reviewer sent
        # a change back with "need the actual diff to verify" — file names and line counts
        # cannot show whether error handling was fixed or merely relocated, and a reviewer
        # that cannot see the change can only ever complain about not seeing it.
        stat = rb._git("diff", "--stat", "main...HEAD",
                       cwd=Path(task["worktree"]), check=False).stdout[-800:]
        body = rb._git("diff", "main...HEAD",
                       cwd=Path(task["worktree"]), check=False).stdout[:3500]
        diff = f"{stat}\n{body}"
        head = ((task.get("verification") or {}).get("headline") or "")[:200]
        accept = "; ".join(str(a) for a in (task.get("acceptance") or []))[:400]
        q = (f"Your teammate finished a change and the full suite is GREEN. Task: "
             f"{task.get('title', '')}. Acceptance: {accept or '(none stated)'}. "
             f"Suite: {head or 'green'}.\nDiff stat:\n{diff}\n"
             "Is this the RIGHT change — scoped to the task, no collateral edits, nothing "
             "suspicious in what it touched? Reply with exactly APPROVE, or "
             "SEND_BACK: <one concrete, actionable complaint>.")
        async with store.lock_for(info["world_id"]):
            w = store.load(info["world_id"], live=True, settings=_root_settings())
            s_ = w.scene(info["room_id"]) if w else None
            t_ = s_.thread(info["thread_id"]) if s_ else None
            hid = (info.get("agents") or {}).get(str(task.get("factor") or ""))
            me = w.get(hid) if (w and hid) else None
            if not (t_ is not None and me is not None):
                return None
            peers = [w.get(i) for i in members_of(t_)
                     if i != me.id and w.get(i) is not None and s_._hears(t_, i, me.id)]
            if not peers:
                return None
            reviewer = await _best_informed_peer(peers, _task_cue(task),
                                                 info["world_id"]) or peers[0]
            answer = await w.agent_reply(reviewer, q)
            store.save(w)
        ledger_add("review", "", 0)
        if not answer:
            return None
        text = answer.strip()
        if text.upper().startswith("SEND_BACK"):
            feedback = text.split(":", 1)[1].strip() if ":" in text else text[9:].strip()
            if feedback:
                return {"who": reviewer.name, "feedback": feedback[:400]}
        bus.emit(0, None, "repair", "repair_review",
                 {"task": task.get("title", ""), "verdict": "approve", "who": reviewer.name})
        logs.info("verify", "review_approved", f"{reviewer.name} approved: {task.get('title', '')}",
                  who=reviewer.name)
        return None
    except Exception as e:
        logs.warn("verify", "review_failed", str(e)[:200])
        return None


async def _phase_land(st: dict, rec: dict) -> None:
    """Squash-merge the green branch to main, so one task is one commit and one commit is one
    `git revert` handle. Refuses if the diff touches a protected path, and queues for review
    instead of landing when the live tree is dirty (that work is the owner's) or when
    supervised mode is on."""
    t = _task(st, rec)
    if t is None:
        set_state(phase="retro")
        return
    if bool(tuning.get("repair_supervised")):
        q = db.kv_get("repair:queue") or []
        q.append({"branch": t["branch"], "sprint_no": rec["no"], "slug": t["slug"],
                  "title": t["title"], "verification": t.get("verification"),
                  "worktree": t.get("worktree"), "created_at": time.time()})
        db.kv_set("repair:queue", q)
        t["status"] = "queued"
        bus.emit(0, None, "repair", "repair_queued", {"task": t["title"]})
    else:
        out = rb.land(t["branch"], t["title"])
        if out.get("ok"):
            t["status"], t["landed_sha"] = "landed", out["sha"]
            rec["landed"] += 1
            rec["landed_files"] = sorted(set(rec.get("landed_files", []) + out.get("files", [])))
            rb.discard(t["branch"], t["worktree"])
            _clear_error()                           # something landed — the last failure is history
            bus.emit(0, None, "repair", "repair_landed", {"task": t["title"], "sha": out["sha"]})
            logs.info("git", "landed", t["title"], sha=str(out["sha"])[:8],
                      files=len(out.get("files") or []))
        else:                                        # dirty tree / moved main → queue, don't lose it
            q = db.kv_get("repair:queue") or []
            q.append({"branch": t["branch"], "sprint_no": rec["no"], "slug": t["slug"],
                      "title": t["title"], "verification": t.get("verification"),
                      "worktree": t.get("worktree"), "created_at": time.time(),
                      "note": out.get("reason", "")})
            db.kv_set("repair:queue", q)
            t["status"] = "queued"
            bus.emit(0, None, "repair", "repair_queued", {"task": t["title"], "why": out.get("reason")})
        logs.info("git", "queued_not_landed", f"{t['title']} — {out.get('reason', '')}",
                  task=t["title"], reason=out.get("reason"))
    save_sprint(rec)
    set_state(phase="build", task_idx=st["task_idx"] + 1)


async def _phase_retro(st: dict, rec: dict) -> None:
    """Close the sprint: a one-line record of what landed and what did not, then either
    restart the server (when backend files changed, so the platform runs what it just wrote)
    or rest until the next sprint is due."""
    rec["finished_at"] = time.time()
    if not rec.get("retro"):
        rec["retro"] = (f"sprint {rec['no']}: {rec['landed']} landed, {rec['failed']} failed, "
                        f"{sum(1 for t in rec['tasks'] if t['status'] == 'queued')} queued")
    save_sprint(rec)
    db.kv_set("repair:seq", rec["no"])
    _tell_crew(rec)
    bus.emit(0, None, "repair", "repair_sprint_done",
             {"sprint": rec["no"], "landed": rec["landed"], "failed": rec["failed"]})
    backend_touched = any(f.startswith(("conductor/", "worker/")) for f in rec.get("landed_files", []))
    if backend_touched and bool(tuning.get("repair_auto_restart")):
        set_state(phase="restarting", note="applying landed backend changes")
        bus.emit(0, None, "repair", "repair_restarting", {"sprint": rec["no"]})
        await asyncio.sleep(1)                       # let the websocket frame flush
        from . import selfops
        selfops.restart_process()
        return
    ok, reason, wake = headroom()
    if not ok:
        return _sleep(reason, wake)
    _sleep("resting between sprints", time.time() + int(tuning.get("repair_sprint_pause_s")), kind="pause")


def _tell_crew(rec: dict) -> None:
    """Drop the retro into the crew thread's chat so 'chat with the manager' can discuss it."""
    try:
        info = team()
        if not info:
            return
        from .lifeworld import store
        w = store.load(info["world_id"])
        s = w.scene(info["room_id"]) if w else None
        thread = s.thread(info["thread_id"]) if s else None
        if thread is None:
            return
        convo = thread.setdefault("chats", {}).setdefault("manager", [])
        convo.append({"role": "manager", "text": rec["retro"], "ts": time.time()})
        thread["chats"]["manager"] = convo[-40:]
        store.save(w)
    except Exception:
        pass


# --- the loop ----------------------------------------------------------------

def claim_lease() -> None:
    """Take the lease unconditionally, at startup.

    `_hold_lease` correctly refuses to run two engines against one database — but it picks the
    wrong winner. A server that has been told to stop, and has already released the port, can
    keep its event loop alive and keep heartbeating; the NEW server, the one that just bound
    the port, then stands down and the crew silently does nothing while a zombie drives it on
    stale code. Binding the port is the only unforgeable proof of being the current conductor,
    so the process that managed it claims the engine, and the old holder stands down on its
    next tick instead of the other way round.
    """
    import os
    prev = (db.kv_get("repair:lease") or {}).get("pid")
    db.kv_set("repair:lease", {"pid": os.getpid(), "ts": time.time()})
    logs.info("lifecycle", "lease_claimed",
              "this process owns the self-repair engine (it bound the port)",
              pid=os.getpid(), previous=prev)


def _hold_lease() -> bool:
    """ONE engine per database. The crew's own threading.Lock (landed by sprint 2) serializes
    writers inside a process; this lease serializes across PROCESSES — a stray old server whose
    loop still ticks against the same devteam.db caused two engines to run one sprint (double
    plans, orphaned green branches). The lease is a kv row {pid, ts} heartbeaten every tick;
    another live holder → we stand down this tick. A crashed holder expires in 3 ticks."""
    import os
    now = time.time()
    lease = db.kv_get("repair:lease") or {}
    if lease.get("pid") != os.getpid() and now - (lease.get("ts") or 0) < TICK_SECONDS * 3:
        # Every 20s forever if a zombie holds it, so damped: the fact matters, the repetition
        # does not, and 180 identical lines an hour would bury everything else.
        logs.log("lifecycle", "lease_held_elsewhere",
                 "another process is driving the engine — standing down this tick",
                 level="warn", dedupe_s=600, holder=lease.get("pid"), me=os.getpid())
        return False
    db.kv_set("repair:lease", {"pid": os.getpid(), "ts": now})
    return True


async def loop() -> None:
    """The never-die background loop (upkeep.py's contract): errors are recorded and
    reported, never fatal — a self-repair engine that can crash itself is a joke."""
    usage.backfill_repair()          # one-shot: the crew's own history into the shared meter
    try:
        from . import knowledge
        seeded = await knowledge.backfill_from_sprints(_root_settings())
        if seeded:
            logs.info("lifecycle", "knowledge_seeded",
                      f"{seeded} lessons imported from sprints that already ran", n=seeded)
    except Exception:
        pass
    while True:
        try:
            if not _hold_lease():
                await asyncio.sleep(TICK_SECONDS)
                continue
            await tick()
        except Exception as e:
            phase = state().get("phase", "?")
            db.kv_set("repair:last_error", {"ts": time.time(), "phase": phase,
                                            "detail": str(e)[:400]})
            logs.error("sprint", "phase_failed", str(e)[:300], phase=phase,
                       kind=type(e).__name__)
            try:
                bus.emit(0, None, "repair", "engine_error", {"detail": str(e)[:200]})
            except Exception:
                pass
        await asyncio.sleep(TICK_SECONDS)


def landed(limit: int = 30) -> list[dict]:
    """Everything the crew has actually shipped, newest first. One task is one squashed
    commit, so this is the honest answer to "what has it done to my repo" — self-repair
    lands on main rather than opening pull requests, and the commit IS the reviewable unit."""
    out = []
    for rec in sorted(db.kv_prefix("repair:sprint:").values(),
                      key=lambda r: r.get("no", 0), reverse=True):
        for t in rec.get("tasks", []):
            if t.get("landed_sha"):
                out.append({"sha": t["landed_sha"], "title": t.get("title", ""),
                            "factor": t.get("factor", ""), "sprint": rec.get("no"),
                            "at": rec.get("finished_at") or rec.get("started_at") or 0})
    return out[:limit]


def tickets(limit: int = 12) -> list[dict]:
    """Work you handed the crew from the Command tab. Those become ordinary projects — which
    is right, they are ordinary work — but that also meant they vanished from this screen the
    moment they were filed, with no way back to them."""
    from . import selfops
    try:
        pid = selfops.find_project()
        rows = [] if not pid else db.list_tasks(pid)
        return [{"id": t["id"], "seq": t.get("seq"), "title": t.get("title", ""),
                 "status": t.get("status"), "sprint": t.get("sprint"),
                 "project_id": pid} for t in rows][-limit:][::-1]
    except Exception:
        return []


def _typed_sprint(rec: dict | None) -> dict | None:
    if rec:
        rec["tasks"] = [_typed(t) for t in rec.get("tasks", [])]
    return rec


def as_project() -> dict:
    """The devteam shaped EXACTLY like a project payload, so Devteam HQ is the project
    screen rendering a different truth — one view, two sources, zero forked renderers.

    The mapping is meaning-first, not cosmetic: sprint tasks become task rows whose ROLE is
    the specialist persona that owns them (the crew, not an ad-hoc spawn); the review queue
    becomes tasks in review; the backlog is planned work; the monitor's top actionable
    notice becomes the manager's pending question, so approving it IS answering the ask
    card. The raw status() rides along under `repair` for the HQ-only tabs (board with its
    categories, notices, usage)."""
    from . import monitor
    d = status()
    fs = {f["id"]: f for f in d.get("factors") or []}

    def name_of(fid: str) -> str:
        return (fs.get(fid) or {}).get("name") or (fid or "crew").replace("-", " ").title()

    model = str(tuning.get("repair_builder_model"))
    sp = d.get("sprint") or {}
    phase = (d.get("state") or {}).get("phase") or ""
    tasks = []
    for i, t in enumerate(sp.get("tasks") or []):
        st = {"pending": "running" if phase in ("build", "verify") else "planned",
              "landed": "done", "failed": "failed", "queued": "review"}.get(t.get("status"), "planned")
        tasks.append({"id": f"s{sp.get('no', 0)}-{i}", "role": name_of(t.get("factor")),
                      "title": t.get("title") or "", "description": t.get("brief") or "",
                      "status": st, "attempts": int(t.get("attempts") or 1), "deps": "[]",
                      "model": model, "type": t.get("type"), "priority": t.get("priority")})
    qs = d.get("queue_state") or {}
    for q in d.get("queue") or []:
        st = qs.get(q.get("branch")) or {}
        tasks.append({"id": f"q-{q.get('slug', '')}", "role": "finished change",
                      "title": q.get("title") or "",
                      "description": (f"{st.get('why')} — {st.get('fix')}" if not st.get("ok", True)
                                      else "green and ready to land"),
                      "status": "review", "attempts": 1, "deps": "[]",
                      "model": model, "branch": q.get("branch")})
    for i, t in enumerate(d.get("backlog") or []):
        tasks.append({"id": f"b-{i}", "role": name_of(t.get("factor")),
                      "title": t.get("title") or "", "description": t.get("brief") or "",
                      "status": "planned", "attempts": 0, "deps": "[]",
                      "model": model, "type": t.get("type"), "priority": t.get("priority")})
    actionable = [n for n in monitor.scan() if n.get("action")]
    question = None
    if actionable:
        n = actionable[0]
        question = {"id": n["fp"], "topic": "decision",
                    "question": f"{n['title']} — {n.get('proposal') or n.get('detail') or ''}",
                    "options": [{"label": f"Approve — {n['action'].replace('_', ' ')}"},
                                {"label": "Dismiss this notice"}]}
    queue = d.get("queue") or []
    crit = (d.get("notices") or {}).get("critical")
    if not d.get("enabled"):
        p_status, summary = "paused", "The crew is switched off."
    elif queue or crit:
        p_status = "review"
        summary = (f"{len(queue)} finished change(s) wait for your approve or discard."
                   if queue else "The monitor raised a critical notice.")
    else:
        p_status, summary = "running", (d.get("state") or {}).get("sleep_reason") or ""
    roster = [{"role": f["name"], "model": model, "provider": "anthropic"}
              for f in d.get("factors") or [] if f.get("enabled")]
    m = (d.get("meters") or {}).get("s5h") or {}
    u = (d.get("meters") or {}).get("util") or {}
    badge = (f"{round(float(u.get('frac') or 0) * 100)}% of this token window used"
             if u.get("budget_tok") else "")
    info = team() or {}
    return {"id": "devteam", "name": "Devteam — the IT crew", "status": p_status,
            "summary": summary,
            "brief": "Keeps this platform healthy: scouts, deliberates, builds, verifies "
                     "and lands — in sprints, on its own repository.",
            "team": json.dumps(roster), "tasks": tasks, "manager_model": model,
            "autonomy": "autonomous", "cost_usd": 0.0, "budget_usd": 0.0,
            "runs_used": m.get("used", 0), "max_runs": m.get("cap", 0),
            # Pre-formatted meter for the header badge: the crew's honest constraint is the
            # shared token window, not the project pipeline's run counter.
            "badge_text": badge,
            "badge_title": "The crew shares your subscription's token window and only "
                           "spends what you are not using. Details on the Usage tab.",
            "repo": (d.get("head") or {}).get("repo") or "",
            "team_world": info.get("world_id"), "team_room": info.get("room_id"),
            # sprints=1 on purpose: the project widget would paint one fabricated "SHIPPED"
            # row per sprint number. The devteam's real history lives on the Board tab.
            "sprint": 1, "sprints": 1,
            "question": question, "repair": d}


def status() -> dict:
    """Everything the Repair screen renders, in one payload."""
    from . import monitor, selfops
    st = state()
    n = st.get("sprint_no") or int(db.kv_get("repair:seq") or 0)
    return {"enabled": enabled(), "state": st, "meters": {**meters(), "team": team_usage()},
            "notices": monitor.summary(), "landed": [_typed(t) for t in landed()],
            "tickets": tickets(), "types": TASK_TYPES, "priorities": PRIORITIES,
            "queue_state": {q["branch"]: rb.landable(q["branch"])
                            for q in (db.kv_get("repair:queue") or [])},
            "factors": factors(), "sprint": _typed_sprint(sprint(n)),
            "queue": db.kv_get("repair:queue") or [],
            "backlog": [_typed(t) for t in backlog().get("tasks", [])],
            "world": team(), "head": selfops.head(), "code": selfops.code_currency(),
            "last_error": db.kv_get("repair:last_error"),
            "supervised": bool(tuning.get("repair_supervised"))}
