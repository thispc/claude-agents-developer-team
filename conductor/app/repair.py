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
import re
import threading
import time
from pathlib import Path

from . import bus, config, db, launcher, tuning
from . import repair_builder as rb

TICK_SECONDS = 20

# Serializes read-modify-write cycles on the repair:state and repair:ledger kv keys.
# tick() runs on the event loop, but toggle()/set_factors() are plain `def` FastAPI
# handlers that FastAPI runs in a worker thread — a real cross-thread race, not just
# interleaved coroutines — so this has to be a threading.Lock (like db.py's own _lock),
# not an asyncio.Lock, which only serializes within a single event loop.
_STATE_LOCK = threading.Lock()

DEFAULT_FACTORS = [
    {"id": "correctness", "name": "Correctness", "enabled": True,
     "brief": "bugs, races, broken flows — anything that behaves wrong or could",
     "dials": {"conscientiousness": 92, "openness": 40}},
    {"id": "simplicity", "name": "Simplicity", "enabled": True,
     "brief": "ruthless about steps, options and code that need not exist",
     "dials": {"conscientiousness": 88, "agreeableness": 25}},
    {"id": "ui-polish", "name": "UI polish", "enabled": True,
     "brief": "visual coherence, spacing, affordances — the product should feel finished",
     "dials": {"openness": 85, "conscientiousness": 70}},
    {"id": "seamlessness", "name": "Seamlessness", "enabled": True,
     "brief": "fewer clicks, fewer surprises — flows that never make the user think about the tool",
     "dials": {"agreeableness": 80, "openness": 70}},
    {"id": "reusability", "name": "Reusability", "enabled": True,
     "brief": "shared helpers over copies; seams others can build on",
     "dials": {"conscientiousness": 80, "openness": 60}},
    {"id": "speed", "name": "Speed", "enabled": True,
     "brief": "latency and waste — hot paths, needless work, snappier feedback",
     "dials": {"extraversion": 70, "conscientiousness": 75}},
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
    f = db.kv_get("repair:factors")
    if not f:
        f = [dict(x) for x in DEFAULT_FACTORS]
        db.kv_set("repair:factors", f)
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


def ledger_add(kind: str, model: str = "", usd: float = 0.0) -> None:
    with _STATE_LOCK:
        rows = db.kv_get("repair:ledger") or []
        rows.append({"ts": time.time(), "kind": kind, "model": model, "usd": round(float(usd or 0), 4)})
        db.kv_set("repair:ledger", rows[-500:])


def _sprint_key(n: int) -> str:
    return f"repair:sprint:{n}"


def sprint(n: int) -> dict | None:
    return db.kv_get(_sprint_key(n))


def save_sprint(rec: dict) -> None:
    db.kv_set(_sprint_key(rec["no"]), rec)


# --- meters + the sleep decision -------------------------------------------

SESSION_KINDS = ("scout", "build")


def meters(now: float | None = None) -> dict:
    now = now or time.time()
    rows = db.kv_get("repair:ledger") or []
    s5 = [r["ts"] for r in rows if r["kind"] in SESSION_KINDS and now - r["ts"] < 5 * 3600]
    w7 = [r["ts"] for r in rows if r["kind"] in SESSION_KINDS and now - r["ts"] < 7 * 86400]
    cap5 = max(1, int(tuning.get("repair_session_cap")))
    cap7 = max(1, int(tuning.get("repair_weekly_cap")))
    model = str(tuning.get("repair_builder_model"))
    cools = {m: launcher.cooldown_left(m) for m in {model, config.ESCALATION_MODEL}
             if launcher.cooldown_left(m) > 0}
    return {"s5h": {"used": len(s5), "cap": cap5,
                    "wake": (min(s5) + 5 * 3600) if len(s5) >= cap5 and s5 else 0},
            "w7d": {"used": len(w7), "cap": cap7,
                    "wake": (min(w7) + 7 * 86400) if len(w7) >= cap7 and w7 else 0},
            "cooldowns": cools, "model": model}


def headroom(now: float | None = None) -> tuple[bool, str, float]:
    """(ok, reason, wake_ts) — the manager's sleep decision, in one place."""
    now = now or time.time()
    m = meters(now)
    if m["cooldowns"]:
        model, left = max(m["cooldowns"].items(), key=lambda kv: kv[1])
        return False, f"{model} is cooling down (limit hit)", now + left
    if m["w7d"]["used"] >= m["w7d"]["cap"]:
        return False, "weekly session cap reached", m["w7d"]["wake"]
    if m["s5h"]["cap"] - m["s5h"]["used"] < max(0, int(tuning.get("repair_headroom_min"))):
        wake = m["s5h"]["wake"] or (now + 1800)
        return False, "session window nearly spent", wake
    return True, "", 0.0


def _sleep(reason: str, until: float) -> None:
    set_state(phase="sleeping", sleep_until=until, sleep_reason=reason)
    bus.emit(0, None, "repair", "repair_sleeping", {"reason": reason, "until": until})


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
    if info and info.get("factor_ids") == want and store.load(info["world_id"]):
        return info
    u = auth.get_user_by_name(auth.ROOT_USERNAME)
    if not u or not fs:
        return None
    w = None
    if info and info.get("world_id"):
        w = store.load(info["world_id"])
    if w is None:
        w = store.create(u["id"], "devteam IT crew")
    names = [f["name"] for f in fs]
    body = ManifestBody(
        name=f"sprint table · {len(fs)} lenses",
        agents=[ManifestAgent(name=f["name"], brief=f["brief"], dials=f.get("dials") or {})
                for f in fs],
        edges=[[names[i], names[(i + 1) % len(names)]] for i in range(len(names))] if len(names) > 1 else [],
        rules="", manager={"model": str(tuning.get("repair_builder_model")), "budget": 2},
        protocol={"preset": "evidence-2026"})
    s = materialise_manifest(w, body)
    store.save(w)
    info = {"world_id": w.id, "room_id": s.id,
            "thread_id": s.threads[0]["id"] if s.threads else 0,
            "agents": {f["id"]: hid for f, hid in zip(fs, s.seats)},
            "factor_ids": want}
    db.kv_set("repair:world", info)
    bus.emit(0, None, "repair", "repair_team_ready", {"world_id": w.id, "agents": len(fs)})
    return info


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
        if time.time() < (st.get("sleep_until") or 0):
            return
        st = set_state(phase="idle", sleep_until=0, sleep_reason="")
    if st["phase"] == "restarting":                 # we came back up — the restart happened
        st = set_state(phase="idle")
    await advance(st)


async def advance(st: dict) -> None:
    phase = st["phase"]
    if phase == "idle":
        ok, reason, wake = headroom()
        if not ok:
            return _sleep(reason, wake)
        n = int(db.kv_get("repair:seq") or 0) + 1
        save_sprint({"no": n, "started_at": time.time(), "scout": {}, "memo": None,
                     "tasks": [], "retro": "", "landed": 0, "failed": 0, "landed_files": []})
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
        _sleep(reason or "provider limit hit", wake or time.time() + 300)
        return True
    return False


async def _phase_scout(st: dict, rec: dict) -> None:
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
    return out


PLAN_EXTRACT_SYSTEM = (
    "You turn a sprint-planning memo into build tasks. Return ONLY a JSON array of "
    '{"title": <short imperative>, "factor": <factor id>, "brief": <2-4 sentences of exactly '
    'what to change and where>, "acceptance": [<checkable outcomes>]} — at most N items, '
    "highest value first.")


async def _phase_plan(st: dict, rec: dict) -> None:
    n_tasks = max(1, int(tuning.get("repair_tasks_per_sprint")))
    memo = None
    try:
        info = ensure_team()
        if info:
            from .lifeworld import store
            w = store.load(info["world_id"], live=_live(), settings=_root_settings() if _live() else None)
            s = w.scene(info["room_id"]) if w else None
            thread = s.thread(info["thread_id"]) if s else None
            if thread is not None:
                lenses = "\n".join(f"- {f['id']}: {f['brief']}" for f in enabled_factors())
                thread["rulebook"] = (
                    "You are the platform's own IT crew planning ONE sprint on the devteam codebase.\n"
                    f"Lenses:\n{lenses}\nScout findings:\n{rec['scout']['digest'] or '(none)'}\n"
                    f"Decide the top {n_tasks} improvements for this sprint; the recommendation "
                    "must name each task's title, factor, target files and acceptance checks.")[:2000]
                memo = await s.run_deliberation(thread, rounds=int(tuning.get("repair_plan_rounds")))
                store.save(w)
                ledger_add("plan", str(tuning.get("repair_builder_model")), 0)
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
            for t in arr[:n_tasks]:
                if isinstance(t, dict) and str(t.get("title", "")).strip():
                    tasks.append({"slug": _slug(str(t["title"])), "title": str(t["title"])[:140],
                                  "factor": str(t.get("factor", ""))[:24],
                                  "brief": str(t.get("brief", ""))[:800],
                                  "acceptance": [str(a)[:200] for a in (t.get("acceptance") or [])][:6],
                                  "branch": "", "status": "pending", "worktree": None,
                                  "verification": None, "landed_sha": None, "attempts": 0,
                                  "evidence": ""})
        except Exception as e:
            if _rate_limited(str(e)):
                rec["memo"] = memo
                save_sprint(rec)
                return
    if not tasks:
        tasks = _tasks_from_candidates(rec["scout"]["candidates"], n_tasks)
    rec["tasks"] = tasks
    save_sprint(rec)
    if not tasks:
        rec["retro"] = "nothing worth doing surfaced this sprint"
        save_sprint(rec)
        set_state(phase="retro")
    else:
        set_state(phase="build", task_idx=0)
    bus.emit(0, None, "repair", "repair_planned", {"sprint": rec["no"], "tasks": len(tasks)})


def _task(st: dict, rec: dict) -> dict | None:
    i = st["task_idx"]
    return rec["tasks"][i] if i < len(rec["tasks"]) else None


async def _phase_build(st: dict, rec: dict) -> None:
    global CURRENT_BUILD
    t = _task(st, rec)
    if t is None:
        set_state(phase="retro")
        return
    if not _live():                                 # offline: nothing can build — mark and move on
        t["status"] = "failed"
        t["verification"] = {"ran": False, "ok": False, "headline": "offline — no credentials to build"}
        rec["failed"] += 1
        save_sprint(rec)
        set_state(task_idx=st["task_idx"] + 1)
        return
    ok, reason, wake = headroom()
    if not ok:
        return _sleep(reason, wake)
    t["status"] = "building"
    t["attempts"] += 1
    save_sprint(rec)
    bus.emit(0, None, "repair", "repair_building", {"sprint": rec["no"], "task": t["title"]})
    try:
        wt = t.get("worktree")
        CURRENT_BUILD = asyncio.ensure_future(
            rb.build(t, rec["no"], _root_settings(), wt=None if wt is None else Path(wt)))
        out = await CURRENT_BUILD
        CURRENT_BUILD = None
        t["branch"], t["worktree"] = out["branch"], out["worktree"]
        ledger_add("build", str(tuning.get("repair_builder_model")), out.get("usd", 0))
        t["status"] = "verifying"
        save_sprint(rec)
        set_state(phase="verify")
    except asyncio.CancelledError:
        CURRENT_BUILD = None
        return                                      # abort() already rewrote the state
    except Exception as e:
        CURRENT_BUILD = None
        ledger_add("build", str(tuning.get("repair_builder_model")), 0)       # a failed session still burned quota
        if _rate_limited(str(e)):
            t["status"] = "pending"                 # resume this same task after the sleep
            t["attempts"] -= 1
            save_sprint(rec)
            return
        t["status"] = "failed"
        t["error"] = str(e)[:400]
        rec["failed"] += 1
        if t.get("branch"):
            rb.discard(t["branch"], t.get("worktree"))
        save_sprint(rec)
        set_state(task_idx=st["task_idx"] + 1)


async def _phase_verify(st: dict, rec: dict) -> None:
    t = _task(st, rec)
    if t is None or not t.get("worktree"):
        set_state(phase="build")
        return
    bad = rb.protected_violation(t["worktree"])
    if bad:
        t["status"] = "failed"
        t["error"] = f"touched a protected path: {bad}"
        rec["failed"] += 1
        rb.discard(t["branch"], t["worktree"])
        save_sprint(rec)
        set_state(phase="build", task_idx=st["task_idx"] + 1)
        bus.emit(0, None, "repair", "repair_task_failed", {"task": t["title"], "why": t["error"]})
        return
    res = await rb.verify(t["worktree"])
    t["verification"] = res
    if res.get("ok"):
        t["status"] = "green"
        save_sprint(rec)
        set_state(phase="land")
        return
    if t["attempts"] <= int(tuning.get("repair_fix_attempts")):
        t["evidence"] = "\n".join(res.get("failures") or [])[:1500] or res.get("headline", "")
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


async def _phase_land(st: dict, rec: dict) -> None:
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
            bus.emit(0, None, "repair", "repair_landed", {"task": t["title"], "sha": out["sha"]})
        else:                                        # dirty tree / moved main → queue, don't lose it
            q = db.kv_get("repair:queue") or []
            q.append({"branch": t["branch"], "sprint_no": rec["no"], "slug": t["slug"],
                      "title": t["title"], "verification": t.get("verification"),
                      "worktree": t.get("worktree"), "created_at": time.time(),
                      "note": out.get("reason", "")})
            db.kv_set("repair:queue", q)
            t["status"] = "queued"
            bus.emit(0, None, "repair", "repair_queued", {"task": t["title"], "why": out.get("reason")})
    save_sprint(rec)
    set_state(phase="build", task_idx=st["task_idx"] + 1)


async def _phase_retro(st: dict, rec: dict) -> None:
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
    _sleep("resting between sprints", time.time() + int(tuning.get("repair_sprint_pause_s")))


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

async def loop() -> None:
    """The never-die background loop (upkeep.py's contract): errors are recorded and
    reported, never fatal — a self-repair engine that can crash itself is a joke."""
    while True:
        try:
            await tick()
        except Exception as e:
            db.kv_set("repair:last_error", {"ts": time.time(), "phase": state().get("phase", "?"),
                                            "detail": str(e)[:400]})
            try:
                bus.emit(0, None, "repair", "engine_error", {"detail": str(e)[:200]})
            except Exception:
                pass
        await asyncio.sleep(TICK_SECONDS)


def status() -> dict:
    """Everything the Repair screen renders, in one payload."""
    from . import selfops
    st = state()
    n = st.get("sprint_no") or int(db.kv_get("repair:seq") or 0)
    return {"enabled": enabled(), "state": st, "meters": {**meters(), "team": team_usage()},
            "factors": factors(), "sprint": sprint(n), "queue": db.kv_get("repair:queue") or [],
            "world": team(), "head": selfops.head(),
            "last_error": db.kv_get("repair:last_error"),
            "supervised": bool(tuning.get("repair_supervised"))}
