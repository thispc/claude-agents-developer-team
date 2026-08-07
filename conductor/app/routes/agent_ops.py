"""The live machinery: which agents are running, how healthy their models are,
and the button that stops one.

Named agent_ops rather than agents because app.agents already exists — this is
the operational view of running workers, not the agent rows themselves.
"""

from fastapi import HTTPException, Request

from .. import auth, config, db, launcher
from .base import can_see, current_user, owned_project, owned_task, router


@router.get("/api/agents")
def list_agents(request: Request, project_id: int | None = None) -> dict:
    """Live infrastructure: worker machines currently running, plus how work is being
    executed (local processes vs Kubernetes Jobs). Scoped to one project when given."""
    from .. import launcher as lx
    import time as _t
    user = current_user(request)
    if project_id is not None:
        owned_project(project_id, request)
    agents = []
    for key, info in list(lx.ACTIVE.items()):
        if project_id is not None and info["project_id"] != project_id:
            continue
        owner = db.get_project(info["project_id"])
        if not owner or not can_see(owner, user):
            continue        # never surface another user's machines
        task_id = info.get("task_id", key)
        t = db.get_task(task_id)
        p = db.get_project(info["project_id"])
        agents.append({
            "task_id": task_id, "rival": info.get("rival", ""),
            "kind": info["kind"], "ref": info["ref"],
            "role": info["role"], "model": info["model"], "title": info.get("title", ""),
            "project_id": info["project_id"], "project": p["name"] if p else "",
            "uptime_s": int(_t.time() - info["started_at"]),
            "status": t["status"] if t else "gone",
            "location": info.get("workdir", ""),
        })
    live_ids = {a["task_id"] for a in agents}
    # Agents that already finished still matter — you want their logs afterwards, so
    # list recent ones too instead of letting them disappear when the machine stops.
    finished = []
    scope = ([db.get_project(project_id)] if project_id is not None
             else db.list_projects()[:6])
    for p in [x for x in scope if x]:
        for t in db.list_tasks(p["id"]):
            if t["id"] not in live_ids and t["model"] and t["attempts"] > 0:
                finished.append({
                    "task_id": t["id"], "kind": "finished", "ref": "—",
                    "role": t["role"], "model": t["model"], "title": t["title"],
                    "project_id": p["id"], "project": p["name"],
                    "uptime_s": 0, "status": t["status"], "location": "",
                })
    finished.sort(key=lambda a: -a["task_id"])
    return {"mode": config.LAUNCHER, "namespace": config.K8S_NAMESPACE,
            "max_parallel": config.MAX_CONCURRENT_WORKERS,
            "running": len(agents), "agents": agents, "finished": finished[:15]}


@router.get("/api/tasks/{task_id}/machine-logs")
def machine_logs(task_id: int, request: Request) -> dict:
    """Raw logs from the machine running this task (k8s pod logs when on a cluster)."""
    owned_task(task_id, request)
    if config.LAUNCHER == "k8s":
        try:
            from ..launcher import get_launcher
            return {"source": "k8s pod", "logs": get_launcher().pod_logs(task_id)}
        except Exception as e:
            return {"source": "k8s pod", "logs": f"could not read pod logs: {e}"}
    evs = db.list_task_events(task_id)
    lines = [f"[{e['kind']}] {e['payload'][:400]}" for e in evs]
    return {"source": "local process (event stream)", "logs": "\n".join(lines) or "no output yet"}


def _api_key_quota(api_key: str) -> list[dict]:
    """Real remaining capacity, straight from Anthropic's rate-limit headers.

    Only possible with an API key: every response carries
    anthropic-ratelimit-{requests,tokens}-{limit,remaining,reset}. We use the free
    count_tokens endpoint so the probe costs nothing. Subscriptions have no
    equivalent endpoint, so this returns [] for them.
    """
    try:
        import anthropic
    except Exception:
        return []
    out = []
    try:
        client = anthropic.Anthropic(api_key=api_key)
        raw = client.messages.with_raw_response.count_tokens(
            model=config.WORKER_MODEL,
            messages=[{"role": "user", "content": "quota probe"}],
        )
        h = raw.headers
        def num(name):
            v = h.get(name)
            return int(v) if v and v.isdigit() else None
        out.append({
            "scope": "account",
            "requests_limit": num("anthropic-ratelimit-requests-limit"),
            "requests_remaining": num("anthropic-ratelimit-requests-remaining"),
            "requests_reset": h.get("anthropic-ratelimit-requests-reset"),
            "tokens_limit": num("anthropic-ratelimit-tokens-limit"),
            "tokens_remaining": num("anthropic-ratelimit-tokens-remaining"),
            "tokens_reset": h.get("anthropic-ratelimit-tokens-reset"),
        })
    except Exception:
        return []
    return [o for o in out if o.get("requests_limit") or o.get("tokens_limit")]


@router.get("/api/model-health")
def model_health(request: Request) -> dict:
    """Observed health per model, from OUR OWN signals — recent successes vs
    rate-limit/overload hits. Anthropic exposes no remaining-quota API, so this
    reports what we can actually measure, never a fabricated 'percent left'."""
    import time as _t
    user = current_user(request)
    window = _t.time() - 6 * 3600
    stats: dict[str, dict] = {}
    visible = [p for p in db.list_projects() if can_see(p, user)][:12]
    for p in visible:
        for t in db.list_tasks(p["id"]):
            m = t.get("model")
            if not m or t["updated_at"] < window:
                continue
            s = stats.setdefault(m, {"model": m, "runs": 0, "ok": 0, "throttled": 0})
            s["runs"] += 1
            if t["status"] in ("done", "pushed", "review"):
                s["ok"] += 1
            if launcher_looks_rate_limited(t.get("report", "")):
                s["throttled"] += 1
    out = []
    for s in stats.values():
        # Health = how much of this model's recent work got through untroubled.
        throttle_rate = s["throttled"] / max(s["runs"], 1)
        health = max(0, min(100, round((1 - throttle_rate) * 100)))
        s["health"] = health
        s["state"] = "throttled" if throttle_rate >= 0.5 else (
            "strained" if throttle_rate > 0 else "healthy")
        out.append(s)
    from ..launcher import cooldown_left
    for s in out:
        s["cooldown_s"] = cooldown_left(s["model"])
        if s["cooldown_s"]:
            s["state"] = "cooling"
    out.sort(key=lambda x: x["model"])

    # Exact remaining capacity IS available on API keys (rate-limit headers).
    u = auth.user_for_token(request.cookies.get("devteam_session"))
    key = (auth.get_settings(u).get("anthropic_api_key") if u else "") or config.ANTHROPIC_API_KEY
    quota = _api_key_quota(key) if key else []

    note = ("Exact remaining requests/tokens read from Anthropic's rate-limit headers."
            if quota else
            "Subscription mode: Anthropic publishes no remaining-quota endpoint, so this "
            "shows observed throttling from this app's own runs plus the real retry-after "
            "window whenever a limit is actually hit.")
    return {"models": out, "window_hours": 6, "quota": quota, "note": note}


def launcher_looks_rate_limited(text: str) -> bool:
    from ..launcher import looks_rate_limited
    return looks_rate_limited(text)


@router.post("/api/tasks/{task_id}/kill")
def kill_agent(task_id: int, request: Request) -> dict:
    """Stop the agent(s) working on one task, right now."""
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, "no such task")
    owned_project(t["project_id"], request)
    notes = launcher.kill_task(task_id, "stopped by the boss from the Agents tab")
    return {"ok": True, "stopped": len(notes), "detail": notes}
