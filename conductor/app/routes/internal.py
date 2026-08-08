"""The worker door, the FLEET doors, and the live feed: /internal/* plus the websocket.

Workers are not users — they authenticate with a shared token, checked in
constant time, and may only report on the task they were actually given,
because one leaked token must not let a caller forge outcomes for every
(project, task) pair in the system. The websocket lives here for the same
reason in reverse: the bus is global, so the feed filters every event down to
what the connected user is allowed to see.

Since P2 there are more doors, and they belong to SERVICES rather than to
workers — a different population with a different token each, so they get their
own check rather than sharing the worker's:

    POST /internal/bus          an extracted service putting an event on the bus
    GET  /internal/tuning       an extracted service reading one of the owner's knobs
    POST /internal/complete     THE MODEL DOOR (P4)
    POST /internal/agents/note  the platform-wide activity register, written (P4)
    GET  /internal/agents/{key} …and read (P4)

Each exists to keep something single-owner. The events table has exactly one
writer (bus.py) and must keep having one, so a service that wants to say
something asks the conductor to say it — SERVICE_CONTRACT rule 5. The tuning
knobs are the owner's, live-editable on the Settings screen, and a service
running on its own private copy of them would quietly disagree with the screen
that set them. The register is one kv blob whose whole value is that every
subsystem writes to the same one.

THE MODEL DOOR is the strictest of them, and the reason the lifeworld could be
extracted at all. Model credentials never leave this process: a service sends
{provider, model, system, prompt, max_tokens, source, settings_ref} and gets text
back. `settings_ref` is a SIGNED reference to a principal (auth.mint_settings_ref)
— the conductor minted it, only the conductor can resolve it, and a forged one
resolves to nothing. Spend is metered here with the source the caller stated,
which is the P2 rule: attribution is a string somebody wrote down, never ambient.

Neither door is open to whoever holds any token. Each managed service's token
identifies WHICH service is calling, and `services.yaml` says which doors — and,
for tuning, which knobs — that service is allowed. A notifier has no business
reading the crew's budget, and the meter has no business emitting events.
"""

import hmac
import json
from pathlib import Path

from fastapi import Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .. import agents, auth, bus, config, db, providers, team, tuning, usage
from .base import can_see, router


class WorkerEvent(BaseModel):
    project_id: int
    task_id: int
    source: str
    kind: str
    payload: str


class ServiceEvent(BaseModel):
    """What a service may put on the bus. `source` is advisory — the door
    overrides it with the calling service's own name when it is blank, because an
    event whose origin is whatever the caller typed is not evidence of anything."""
    project_id: int = 0
    task_id: int | None = None
    source: str = ""
    kind: str
    payload: dict | str | None = None


class WorkerReport(BaseModel):
    project_id: int
    task_id: int
    status: str  # pushed | failed
    report: str
    cost_usd: float = 0
    # Tokens the worker's session actually consumed. Optional: an older worker binary
    # reports no tokens rather than failing, and its row then counts as calls only.
    tokens: int = 0
    cache_tokens: int = 0
    contender_id: int = 0
    verification: str = ""      # JSON, produced by the worker process not the model


def _check_token(token: str | None) -> None:
    # Constant-time: a plain != leaks the token one character at a time to anyone
    # who can measure the response.
    if not token or not hmac.compare_digest(token, config.WORKER_TOKEN):
        raise HTTPException(401, "bad worker token")


def _owns_task(project_id: int, task_id: int) -> None:
    """A worker may only report on the task it was actually given.

    Without this, one valid worker token lets any caller forge outcomes and costs
    for any (project, task) pair in the system.
    """
    t = db.get_task(task_id)
    if not t or t["project_id"] != project_id:
        raise HTTPException(400, "task does not belong to that project")


# --- the fleet doors: which service is calling, and may it use this one? ------

def _service_caller(token: str | None, door: str) -> tuple[str, dict]:
    """Identify the calling service by its own token, and check the allowlist.

    Two separate questions, deliberately. WHO is answered by the token — each
    managed service has its own, minted once into `data/tokens/<name>.token` by
    `tools/gen_fleet.py`, so a token is an identity and not merely a password.
    WHAT IT MAY DO is answered by `services.yaml`'s `doors:` list, carried into
    `data/fleet_topology.json` by the generator. One shared "internal token"
    would have collapsed both questions into "is the caller inside the fleet",
    and inside the fleet is not a permission.

    Compared with `hmac.compare_digest` against every candidate: a plain `!=`
    leaks a token one character at a time to anyone who can measure the response.
    The conductor's own token is skipped — it is this process; a door to yourself
    is a loop wearing a trenchcoat.
    """
    if not token:
        raise HTTPException(401, "missing service token")
    from . import svc                    # lazy: keeps this package's import order
    for name, entry in (svc._services() or {}).items():
        if not entry.get("managed") or entry.get("kind") == "core":
            continue
        f = Path(svc._DATA) / "tokens" / f"{name}.token"
        try:
            expected = f.read_text().strip()
        except OSError:
            continue
        if expected and hmac.compare_digest(token, expected):
            if door not in (entry.get("doors") or []):
                raise HTTPException(
                    403, f"the {name} service is not allowed the {door} door "
                         f"— add it to services.yaml and re-run tools/gen_fleet.py")
            return name, entry
    raise HTTPException(401, "bad service token")


@router.post("/internal/bus")
def service_event(body: ServiceEvent,
                  x_service_token: str | None = Header(None)) -> dict:
    """A fleet service putting an event on the platform bus.

    The events table has exactly ONE writer (bus.py) and this is how it keeps
    having one: an extracted service does not open the conductor's database, it
    asks. Everything downstream — the durable log, the dashboard websocket and
    its per-user visibility filter — is unchanged, which is the point: an event
    from the notify service looks like every other event on the feed.

    Project 0 is the platform itself, and that is the default: a service's events
    are about the box, not about anyone's project.
    """
    name, _ = _service_caller(x_service_token, "bus")
    ev = bus.emit(int(body.project_id or 0), body.task_id,
                  (body.source or name)[:60], body.kind[:60],
                  body.payload if body.payload is not None else {})
    return {"ok": True, "id": ev.get("id")}


@router.get("/internal/tuning")
def service_tuning(name: str, x_service_token: str | None = Header(None)) -> dict:
    """One tuning knob, for a service that runs on the owner's dials.

    Per-service knob allowlist, not just per-service door: the usage meter needs
    the window, the budget and the crew's idle share, and nothing else in the
    fifty-odd knobs is any of its business. The list lives in `services.yaml`
    beside the service it belongs to, so adding a knob to a service is a registry
    edit somebody reviews rather than a line of code nobody notices.
    """
    caller, entry = _service_caller(x_service_token, "tuning")
    if name not in (entry.get("knobs") or []):
        raise HTTPException(403, f"the {caller} service may not read {name!r} "
                                 f"— list it under knobs: in services.yaml")
    return {"name": name, "value": tuning.get(name)}


class ServiceComplete(BaseModel):
    """One completion, asked for by a service that holds no credentials."""
    provider: str = "anthropic"
    model: str = ""
    system: str = ""
    prompt: str = ""
    max_tokens: int = 2000
    source: str = ""            # who to bill; blank is stamped with the caller's name
    settings_ref: str = ""      # signed; resolves to the owner's settings, here only


class ServiceAgentNote(BaseModel):
    key: str
    state: str = "idle"
    what: str = ""
    fields: dict = Field(default_factory=dict)


# A service asking for more than this is asking for a different product. The
# ceiling is generous next to every prompt the substrate actually sends (the
# largest is a host plan at ~110 tokens per agent) and small enough that a bug in
# a loop cannot quietly spend a window.
MAX_SERVICE_TOKENS = 4000


@router.post("/internal/complete")
async def service_complete(body: ServiceComplete,
                           x_service_token: str | None = Header(None)) -> dict:
    """THE MODEL DOOR — the one place a fleet service can reach a model.

    The invariant this exists to hold: **model credentials never leave the
    conductor.** The lifeworld service runs a whole society of agents on the
    owner's account and has never seen a key; it sends a prompt and a SIGNED
    reference to whose account to spend on, and the resolution happens here,
    three lines below, in the process that already holds every other secret.

    Metered with the source the caller stated. P2 made attribution explicit on
    the wire precisely because a meter that guesses can bill the crew's own
    footsteps to the owner and put the crew to sleep forever; a service that
    forgets to say gets billed under its own name, which is at least true.
    """
    name, _ = _service_caller(x_service_token, "model")
    settings = auth.resolve_settings_ref(body.settings_ref)
    if not settings:
        # Deliberately NOT "spend the operator's key instead". A reference that
        # does not resolve means the caller could not name whose quota this is,
        # and guessing is how one account pays for another's agents.
        raise HTTPException(403, "the settings reference did not resolve — a service "
                                 "may only spend on a principal the conductor named")
    tokens = max(1, min(int(body.max_tokens or 0) or 1, MAX_SERVICE_TOKENS))
    try:
        text = await providers.complete(
            body.provider or "anthropic", body.model, body.system, body.prompt,
            settings, max_tokens=tokens, source=(body.source or name)[:24])
    except providers.ProviderError as e:
        # 502, not 500: the failure is upstream of this box, and every substrate
        # caller already has a free fallback for a raising `complete`.
        raise HTTPException(502, str(e)[:400])
    return {"text": text}


@router.post("/internal/agents/note")
def service_agent_note(body: ServiceAgentNote,
                       x_service_token: str | None = Header(None)) -> dict:
    """A service telling the register what one of its agents is doing.

    The register is a single kv blob and its entire value is that everything
    writes to the SAME one: a lifeworld agent has to show up beside workers and
    crew on one board, or the board answers "is this one working?" differently
    depending on who you ask. So a service does not keep its own — it asks.
    """
    _service_caller(x_service_token, "agents")
    fields = {k: v for k, v in (body.fields or {}).items()
              if isinstance(v, (str, int, float, bool)) or v is None}
    return agents.note(str(body.key)[:120], body.state, body.what, **fields)


@router.get("/internal/agents/{key:path}")
def service_agent_get(key: str, x_service_token: str | None = Header(None)) -> dict:
    """One register row, resolved — expired claims read idle, however confidently
    they were made. `{key:path}` because a key is colon-joined (`lw:3:14`) and a
    plain path segment would still work, but a future kind with a slash in it
    would 404 instead of being answered."""
    _service_caller(x_service_token, "agents")
    return agents.get(key)


@router.post("/internal/events")
def worker_event(body: WorkerEvent, x_worker_token: str | None = Header(None)) -> dict:
    _check_token(x_worker_token)
    _owns_task(body.project_id, body.task_id)
    bus.emit(body.project_id, body.task_id, body.source, body.kind, body.payload)
    if body.kind == "agent_status" and body.payload == "running":
        db.update_task(body.task_id, status="running")
    else:
        db.touch_task(body.task_id)  # keep the stall watchdog from firing on busy tasks
    return {"ok": True}


def _close_run(task_id: int, status: str, cost_usd: float,
               contender_id: int | None = None) -> None:
    """Close the measurement row for a dispatch that just reported.

    'delivered' rather than 'accepted': the worker finished, which is not the same
    as the work being any good. The manager relabels it when it judges.
    """
    run = db.open_run_for(task_id, contender_id)
    if run:
        db.finish_run(run["id"], "delivered" if status == "pushed" else "failed",
                      cost_usd=cost_usd)


@router.get("/internal/teammate")
def teammate_context(project_id: int, role: str = "", name: str = "",
                     exclude_task: int = 0,
                     x_worker_token: str | None = Header(None)) -> dict:
    """Who a stuck worker is actually talking to, and what they have built.

    `ask_teammate` used to fire a one-shot query at a stronger model. That is a
    second opinion, but it is not a teammate: it had no idea what anyone on the
    project had already built, so its advice regularly contradicted decisions
    another agent had made an hour earlier — and the asker, having no way to know,
    followed it. Handing over the real teammate's persona and their delivered work
    is the difference between consulting a colleague and consulting a stranger who
    happens to be clever.
    """
    _check_token(x_worker_token)
    people = db.list_agents(project_id)
    if not people:
        return {"found": False}
    match = None
    if name:
        match = next((a for a in people if a["name"].lower() == name.lower()), None)
    if not match and role:
        match = next((a for a in people if a["role"].lower() == role.lower()), None)
    if not match:
        # Nobody named: the most experienced teammate is the best default, because
        # the point of asking is to reach someone who has seen more of this project.
        match = sorted(people, key=lambda a: -a["tasks_done"])[0]

    delivered = [t for t in db.list_tasks(project_id)
                 if t.get("agent_id") == match["id"] and t["status"] == "done"
                 and t["id"] != exclude_task]
    return {
        "found": True,
        "name": match["name"],
        "role": match["role"],
        "persona": match["persona"],
        "notes": match["notes"],
        "model": match["model"],
        "work": [{"title": t["title"], "report": (t["report"] or "")[:1500]}
                 for t in delivered[-3:]],
        "team": [{"name": a["name"], "role": a["role"]} for a in people],
    }


@router.post("/internal/report")
def worker_report(body: WorkerReport, x_worker_token: str | None = Header(None)) -> dict:
    _check_token(x_worker_token)
    _owns_task(body.project_id, body.task_id)
    status = "pushed" if body.status == "pushed" else "failed"
    task = db.get_task(body.task_id)
    # Worker sessions draw on the same subscription quota the self-repair crew watches, so
    # they are metered here — once, before the branchy paths below each bill the project.
    usage.note("worker", (task or {}).get("model") or "", tok=body.tokens,
               cache=body.cache_tokens, usd=body.cost_usd)

    # A rival attempt reports into its own row; the task only advances once every
    # rival is in, and then it goes to the manager to judge — not straight to a PR.
    if body.contender_id:
        db.update_contender(body.contender_id, status=status, report=body.report[:12000])
        db.add_project_cost(body.project_id, body.cost_usd)
        _close_run(body.task_id, status, body.cost_usd, contender_id=body.contender_id)
        rivals = db.list_contenders(body.task_id)
        c = db.get_contender(body.contender_id)
        bus.emit(body.project_id, body.task_id, f"rival {c['idx'] if c else '?'}",
                 "rival_finished", {"status": status, "model": c["model"] if c else "",
                                    "summary": body.report[:800]})
        if all(r["status"] in ("pushed", "failed") for r in rivals):
            ok = [r for r in rivals if r["status"] == "pushed"]
            if ok:
                # Write a digest onto the TASK as well. get_report reads task.report,
                # and a contest used to leave it empty — so a manager that called
                # get_report saw "(no report yet)", concluded nothing was delivered,
                # and sent perfectly good rival work back again and again.
                digest = (f"CONTEST: {len(ok)} of {len(rivals)} rivals delivered. "
                          f"Use compare_work to judge them, then pick_winner.\n\n" +
                          "\n\n".join(f"--- rival #{r['idx']} ({r['model']}) [{r['status']}] ---\n"
                                       f"{(r['report'] or '')[:1500]}" for r in rivals))
                db.update_task(body.task_id, status="review", report=digest)
                bus.emit(body.project_id, body.task_id, "system", "contest_ready",
                         {"rivals": len(rivals), "finished_ok": len(ok)})
            else:
                db.update_task(body.task_id, status="failed",
                               report="all rival attempts failed:\n\n" +
                                      "\n\n".join(f"[#{r['idx']} {r['model']}] {r['report'][:800]}"
                                                  for r in rivals))
        return {"ok": True}
    from ..launcher import looks_rate_limited, note_rate_limit, cooldown_left
    if status == "failed" and looks_rate_limited(body.report):
        model = (task.get("model") if task else "") or ""
        note_rate_limit(model, body.report)      # capture the real retry-after
        bus.emit(body.project_id, body.task_id, "system", "rate_limited",
                 {"model": model, "cooldown_s": cooldown_left(model),
                  "detail": body.report[:300]})
    # A report from a superseded worker must not drag the task backwards.
    #
    # On the mars-rover run two workers ended up on task #7 at once (a re-dispatch
    # while the first was still alive). The manager accepted the first one's work
    # and closed the task; forty seconds later the second reported, and the task
    # flipped from 'done' back to 'pushed' — permanently, because the project was
    # already finished and nothing was left running to move it on again.
    if task and task["status"] == "done":
        bus.emit(body.project_id, body.task_id, "system", "late_report_ignored",
                 {"status": status, "note": "this task was already accepted; a second "
                                            "worker reported afterwards"})
        db.add_project_cost(body.project_id, body.cost_usd)
        return {"ok": True, "ignored": "task already accepted"}

    db.update_task(body.task_id, status=status, report=body.report,
                   verification=body.verification or "",
                   cost_usd=(task["cost_usd"] if task else 0) + body.cost_usd)
    db.add_project_cost(body.project_id, body.cost_usd)
    _close_run(body.task_id, status, body.cost_usd)
    # Back to the pool. Not credited with the task yet — that happens when the
    # manager accepts, because finishing and being any good are different events.
    team.release(db.get_task(body.task_id) or {}, body.report)
    try:
        v = json.loads(body.verification or "{}")
    except Exception:
        v = {}
    if v.get("ran"):
        bus.emit(body.project_id, body.task_id, "system",
                 "verified" if v.get("ok") else "verification_failed",
                 {"cmd": v.get("cmd"), "exit_code": v.get("exit_code")})
    bus.emit(body.project_id, body.task_id, f"worker:{task['role'] if task else '?'}",
             "report", {"status": status, "cost_usd": body.cost_usd,
                        "summary": body.report[:2000]})
    return {"ok": True}


# --- websocket live feed ---

@router.websocket("/ws")
async def ws_feed(ws: WebSocket) -> None:
    """Live event feed, filtered to what this user is allowed to see.

    The bus is global: every project's events pass through it. Without the
    check below an anonymous socket received the live activity — briefs, agent
    messages, reports — of every project belonging to every user.
    """
    user = auth.user_for_token(ws.cookies.get("devteam_session"))
    if not user:
        await ws.close(code=1008)      # policy violation
        return
    await ws.accept()
    q = bus.subscribe()
    visible: dict[int, bool] = {}      # project_id -> allowed, resolved once each
    try:
        while True:
            event = await q.get()
            pid = event.get("project_id")
            if pid not in visible:
                if pid == 0:
                    # Project 0 is the platform itself: the crew's and the module
                    # graph's events. Same gate as the Improve tile — visible to
                    # whoever may self-repair, DROPPED for everyone else, so the
                    # graph screen gets a live feed without HQ-style polling.
                    visible[0] = config.may_self_repair(user["username"],
                                                       bool(user["is_root"]))
                else:
                    p = db.get_project(pid) if pid else None
                    visible[pid] = bool(p and can_see(p, user))
            if visible[pid]:
                await ws.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(q)
