"""The Studio: globally-persistent agents that reside between jobs.

An agent here is a durable person — a name, a character, a discipline, and one
current model — that outlives any single project and can be deployed into many.
The per-project `agents` row stays the instance a task is assigned to; this is the
identity behind it.

The centre of this module is the **free background tick**. Modelled on
`upkeep.loop` and `scheduler._run`, it wakes periodically, reads rows, and — for
almost every wake — does nothing but decide that nothing is due. That is the whole
promise: an agent "living in the background" is a deterministic sweep over rows it
already wrote, and the only thing that ever costs a token is memory consolidation,
which fires on accumulated work, at most a few agents per wake, under a hard daily
budget. Evolution runs in the same tick and is free.

Read the token budget in docs/AGENTS_HOME.md; the invariant every test pins is
that a Studio at rest spends exactly zero.
"""

import asyncio
import time
from typing import Any

from . import bus, config, db, evolution, memory, team, tuning

# How often the tick wakes to CHECK whether anything is due. Not how often it
# spends — spending is gated on accumulated episodes, not on this interval, so a
# faster tick does not mean a bigger bill.
TICK_SECONDS = 300
BUDGET_KEY = "home_spend_day"     # kv: {"day": <YYYYMMDD>, "tokens": <int>}


def create(owner_id: int, name: str = "", degree: str = "", persona: str = "",
           provider: str = "anthropic", model: str = "", traits: dict | None = None) -> dict:
    """Hire a new resident. An empty name gets one from the shared pool, so a
    Studio's people never share a name. `traits` are the personality dials set on the
    agent when it is created or edited."""
    if not name.strip():
        taken = {a["name"] for a in db.list_home_agents(owner_id, include_archived=True)}
        name = _name_for(taken)
    hid = db.create_home_agent(owner_id, name.strip(), degree.strip(),
                               persona.strip(), provider, model, traits=traits)
    bus.emit(0, None, "system", "home_hired", {"home": hid, "name": name})
    return db.get_home_agent(hid)


def _name_for(taken: set[str]) -> str:
    for n in team.NAMES:
        if n not in taken:
            return n
    import random
    return f"{random.choice(team.NAMES)}-{len(taken) + 1}"


def describe(owner_id: int) -> list[dict]:
    """The Studio for the UI: identity, counters, current model, memory size, mood.

    Mood is DERIVED from real signals here — it is never a stored feeling. A busy
    instance makes an agent 'focused'; recent rework makes it 'strained'; a pending
    boss question makes it 'needs_you'. The UI turns these into how the character
    looks, but the truth is always a row.
    """
    out = []
    for a in db.list_home_agents(owner_id):
        instances = db.home_instances(a["id"])
        busy = any(i["status"] == "busy" for i in instances)
        recent = db.runs_for_home(a["id"], limit=5)
        reworked = any(r["outcome"] == "rework" for r in recent)
        mem = memory.current_blob(a["id"])
        out.append({
            "id": a["id"], "name": a["name"], "degree": a["degree"],
            "persona": a["persona"], "traits": _traits_of(a),
            "provider": a["provider"], "model": a["model"],
            "model_locked": bool(a["model_locked"]), "status": a["status"],
            "lifetime_tasks": a["lifetime_tasks"],
            "lifetime_accepted": a["lifetime_accepted"],
            "lifetime_rework": a["lifetime_rework"],
            "memory_chars": len(mem),
            "on_project": next((i["project_id"] for i in instances
                                if i["status"] == "busy"), None),
            "mood": ("focused" if busy else "strained" if reworked else "content"),
        })
    return out


def _traits_of(a: dict) -> dict:
    """The agent's personality dials, parsed. Rows written before the field existed
    have none, which reads as an empty dict — a neutral person."""
    import json
    try:
        t = json.loads(a.get("traits") or "{}")
        return t if isinstance(t, dict) else {}
    except (ValueError, TypeError):
        return {}


def owns(owner_id: int, home_id: int) -> bool:
    a = db.get_home_agent(home_id)
    return bool(a and a["owner_id"] == owner_id)


def use(home_id: int, project_id: int, role: str = "") -> dict | None:
    """Deploy a global agent into a project as a per-project instance.

    Copies identity DOWN — name, persona, provider, model — onto a new `agents`
    row linked by `home_id`. That link is what lets the instance's outcomes flow
    back UP into the global agent's episodes and run history.
    """
    a = db.get_home_agent(home_id)
    if not a:
        return None
    existing = [i for i in db.home_instances(home_id) if i["project_id"] == project_id]
    if existing:
        return existing[0]
    aid = db.create_agent(project_id, a["name"], role or a["degree"] or "generalist",
                          persona=a["persona"], provider=a["provider"], model=a["model"])
    db.update_agent(aid, home_id=home_id)
    return db.get_agent(aid)


def record_episode(agent: dict, kind: str, gist: str, *, project_id: int | None = None,
                   task_id: int | None = None) -> None:
    """Note something a project-instance did, on its global agent. FREE — the gist
    is already computed by team._gist; this just files it and bumps counters.

    A no-op for a project agent with no home_id, which is every agent created
    before the Studio existed — so nothing about existing projects changes.
    """
    home_id = (agent or {}).get("home_id")
    if not home_id:
        return
    weight = 2 if kind in ("rework", "escalation") else 1
    db.add_episode(home_id, kind, gist, project_id=project_id, task_id=task_id,
                   weight=weight)
    a = db.get_home_agent(home_id)
    if not a:
        return
    fields = {"lifetime_tasks": a["lifetime_tasks"] + (1 if kind == "task_done" else 0)}
    if kind == "task_done":
        fields["lifetime_accepted"] = a["lifetime_accepted"] + 1
    if kind == "rework":
        fields["lifetime_rework"] = a["lifetime_rework"] + 1
    db.update_home_agent(home_id, **fields)


# --- the daily token budget: the visible backstop ---

def _today() -> str:
    return time.strftime("%Y%m%d", time.gmtime())


def budget_state() -> dict[str, Any]:
    st = db.kv_get(BUDGET_KEY, {}) or {}
    if st.get("day") != _today():
        st = {"day": _today(), "tokens": 0}
    cap = int(tuning.get("home_token_budget_daily"))
    return {"day": st["day"], "spent": st.get("tokens", 0), "cap": cap,
            "remaining": max(0, cap - st.get("tokens", 0))}


def _charge(tokens: int) -> None:
    st = budget_state()
    db.kv_set(BUDGET_KEY, {"day": st["day"], "tokens": st["spent"] + tokens})


def _may_spend() -> bool:
    return budget_state()["remaining"] > 0


# --- the free background tick ---

async def tick(settings_for=None) -> dict[str, Any]:
    """One sweep. Free unless an agent has genuinely accumulated enough work.

    `settings_for(owner_id) -> settings` supplies each owner's own credentials for
    the (rare) consolidation call, so a background fold spends the OWNER'S key, not
    the operator's — the same isolation the rest of the platform enforces. Tests
    pass a stub; production passes the real resolver.
    """
    if not tuning.get("home_life_enabled"):
        return {"consolidated": 0, "evolved": 0, "spent_calls": 0}

    settings_for = settings_for or (lambda oid: {})
    from . import db as _db
    owners = {a["owner_id"] for a in _db._rows("SELECT DISTINCT owner_id FROM home_agents", ())}

    consolidated = evolved = spent = 0
    per_tick = int(tuning.get("home_compress_max_per_tick"))

    for owner_id in owners:
        for a in db.list_home_agents(owner_id):
            hid = a["id"]

            # Evolution first — it is free and might change the model the fold runs
            # under next time anyway.
            if evolution.evolve_one(hid):
                evolved += 1

            # Consolidation — the only thing that can bill, and only when DUE.
            if consolidated < per_tick and memory.is_due(hid):
                allow = _may_spend()
                res = await memory.consolidate(hid, settings_for(owner_id),
                                               allow_spend=allow)
                if res["folded"]:
                    consolidated += 1
                if res["spent"]:
                    spent += res["spent"]
                    # One cheap call, bounded output. Charge the configured ceiling
                    # as a conservative upper bound rather than guessing actual usage.
                    _charge(int(tuning.get("home_compress_max_tokens")))

    if consolidated or evolved:
        bus.emit(0, None, "system", "home_tick",
                 {"consolidated": consolidated, "evolved": evolved, "spent_calls": spent})
    return {"consolidated": consolidated, "evolved": evolved, "spent_calls": spent}


async def loop(settings_for=None) -> None:
    """Wake periodically, run a tick. Never dies.

    A crash here would silently end background memory and evolution, which is the
    failure mode where you find out weeks later that nobody has been learning.
    Every error is swallowed and reported rather than allowed to end the loop —
    the same contract upkeep.loop keeps.
    """
    while True:
        try:
            await tick(settings_for)
        except Exception as e:
            bus.emit(0, None, "system", "home_tick_error", {"detail": str(e)[:300]})
        await asyncio.sleep(TICK_SECONDS)


def default_settings_for(owner_id: int) -> dict:
    """Production resolver: each owner's own credentials for a background fold."""
    from . import auth
    u = auth.get_user(owner_id)
    return auth.get_settings(u) if u else {}
