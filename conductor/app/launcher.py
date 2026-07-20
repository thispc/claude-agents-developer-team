"""Launch worker agents.

Two backends:
- LocalLauncher: subprocess per worker (dev / docker-compose).
- K8sLauncher:   one Kubernetes Job per worker (DOKS). The conductor's ServiceAccount
                 needs create/list on jobs (see deploy/k8s/rbac.yaml).

Both pass the same env contract to worker/worker.py.
"""

import asyncio
import os
import signal
import subprocess
import sys

from . import config, db, bus


def owner_credentials(project: dict) -> dict[str, str]:
    """Credentials the agents on this project run under.

    Each user pays their own way: a normal user's agents use ONLY that user's own key
    or subscription token. They never fall back to the server's credentials — otherwise
    anyone who signed up would silently spend the operator's subscription.

    Only the root/operator account (and legacy owner-less projects) uses the server
    credentials, including the host machine's Claude CLI login.
    """
    from . import auth as auth_mod
    owner = auth_mod.get_user(project.get("owner_id") or 0)
    if owner and not owner["is_root"]:
        s = auth_mod.get_settings(owner)
        if s.get("anthropic_api_key"):
            return {"ANTHROPIC_API_KEY": s["anthropic_api_key"]}
        if s.get("claude_oauth_token"):
            return {"CLAUDE_CODE_OAUTH_TOKEN": s["claude_oauth_token"]}
        # No credentials of their own — deliberately return an unusable env rather
        # than inheriting the operator's. dispatch/creation guards catch this first.
        return {"ANTHROPIC_API_KEY": ""}
    # Root / operator: their stored settings, then the server's, then CLI login.
    if owner:
        s = auth_mod.get_settings(owner)
        if s.get("anthropic_api_key"):
            return {"ANTHROPIC_API_KEY": s["anthropic_api_key"]}
        if s.get("claude_oauth_token"):
            return {"CLAUDE_CODE_OAUTH_TOKEN": s["claude_oauth_token"]}
    if config.ANTHROPIC_API_KEY:
        return {"ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY}
    if config.CLAUDE_CODE_OAUTH_TOKEN:
        return {"CLAUDE_CODE_OAUTH_TOKEN": config.CLAUDE_CODE_OAUTH_TOKEN}
    return {}   # local CLI login — inherited from the environment


def owner_github_token(project: dict) -> str:
    from . import auth as auth_mod
    owner = auth_mod.get_user(project.get("owner_id") or 0)
    if owner:
        tok = auth_mod.get_settings(owner).get("github_token")
        if tok:
            return tok
    return config.GITHUB_TOKEN


def handoff_context(task: dict) -> str:
    """What teammates this task depends on actually delivered.

    Agents run in isolation, so without this a second backend has no idea what the
    first one built beyond reading the code. Passing the predecessors' final reports
    forward is the handoff note a human would have written.
    """
    import json as _json
    try:
        dep_ids = _json.loads(task.get("deps") or "[]")
    except Exception:
        return ""
    parts: list[str] = []
    for dep_id in dep_ids:
        d = db.get_task(dep_id)
        if not d or not d.get("report"):
            continue
        parts.append(
            f"### From {d['role']} (task #{d['id']}: {d['title']})\n"
            f"{d['report'].strip()[:2500]}")
    if not parts:
        return ""
    return ("Notes handed over by teammates whose work you build on. Their code is "
            "already merged into the branch you cloned — read these before starting so "
            "you match their contracts and don't redo their work.\n\n" + "\n\n".join(parts))


def _worker_env(task: dict, project: dict, model: str) -> dict[str, str]:
    auth = owner_credentials(project)
    return {
        **auth,
        "HANDOFF_CONTEXT": handoff_context(task),
        "CONDUCTOR_URL": config.CONDUCTOR_URL,
        "WORKER_TOKEN": config.WORKER_TOKEN,
        "TASK_ID": str(task["id"]),
        "PROJECT_ID": str(task["project_id"]),
        "ROLE": task["role"],
        "TASK_TITLE": task["title"],
        "TASK_DESCRIPTION": task["description"],
        "TASK_FEEDBACK": task.get("feedback", ""),
        "BRANCH": task["branch"],
        "REPO": project["repo"],
        "GITHUB_TOKEN": owner_github_token(project),
        "MODEL": model,
        # Teammate a stuck worker can consult (always a capable model, even when the
        # worker itself is a cheap one — that is the point).
        "CONSULT_MODEL": config.ESCALATION_MODEL,
        # If the last attempt died by running out of turns, give the retry more room
        # instead of failing the same way again.
        "MAX_TURNS": str(config.WORKER_MAX_TURNS_RETRY
                         if "maximum number of turns" in (task.get("report") or "").lower()
                         else config.WORKER_MAX_TURNS),
    }


RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "429", "overloaded", "quota",
                      "usage limit", "too many requests")


def looks_rate_limited(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in RATE_LIMIT_MARKERS)


# model -> unix timestamp when it should be usable again (from real retry-after /
# reset values the API gives us on a 429). Empty until something actually throttles.
COOLDOWN: dict[str, float] = {}


def note_rate_limit(model: str, text: str) -> float | None:
    """Extract when a throttled model becomes usable again.

    The API tells us this on a 429 — 'retry-after: 60', a reset timestamp, or a
    'try again at ...' message. Capturing it beats guessing.
    """
    import re
    import time as _t
    from datetime import datetime, timezone
    t = (text or "")
    seconds = None
    m = re.search(r"retry[- ]after[\"':\s]+(\d+)", t, re.I)
    if m:
        seconds = int(m.group(1))
    if seconds is None:
        m = re.search(r"try again in (\d+)\s*(second|minute|hour)", t, re.I)
        if m:
            mult = {"second": 1, "minute": 60, "hour": 3600}[m.group(2).lower()]
            seconds = int(m.group(1)) * mult
    if seconds is None:
        # e.g. "resets at 2026-07-20T18:00:00Z" / "limit resets 6pm"
        m = re.search(r"(?:reset|available|try again)[^0-9]{0,20}(\d{4}-\d{2}-\d{2}T[\d:]+)", t, re.I)
        if m:
            try:
                when = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
                seconds = max(0, int(when.timestamp() - _t.time()))
            except Exception:
                seconds = None
    if seconds is None:
        seconds = 300      # nothing parseable: assume a short cooldown, clearly labelled
    until = _t.time() + seconds
    COOLDOWN[model] = until
    return until


def cooldown_left(model: str) -> int:
    import time as _t
    until = COOLDOWN.get(model)
    if not until:
        return 0
    left = int(until - _t.time())
    if left <= 0:
        COOLDOWN.pop(model, None)
        return 0
    return left


# Models a throttled task can be moved to, cheapest/most-available first.
FALLBACK_ORDER = ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"]


def pick_model(task: dict, project: dict | None = None) -> str:
    """Model precedence: an explicit reassignment by the manager > a fallback when the
    previous run was rate-limited > escalation after repeated failures > the boss's
    recruited per-role choice > the role's roles.json default > WORKER_MODEL."""
    # Only an EXPLICIT manager reassignment pins the model. task["model"] is just
    # what the last run used — reading it here would make every retry re-pick the
    # same model and silently disable escalation and rate-limit fallback.
    if task.get("pinned_model"):
        return task["pinned_model"]
    # Previous attempt died on a rate limit — move to a model that is not still in
    # its cooldown window rather than hammering the throttled one.
    if task["attempts"] >= 1 and looks_rate_limited(task.get("report", "")):
        prev = task.get("model") or config.WORKER_MODEL
        for m in FALLBACK_ORDER:
            if m != prev and not cooldown_left(m):
                return m
    if task["attempts"] >= 2:
        return config.ESCALATION_MODEL
    if project:
        import json
        for member in json.loads(project.get("team") or "[]"):
            if member.get("role") == task["role"] and member.get("model"):
                return config._resolve_model(member["model"])
    role = config.roles_by_name().get(task["role"])
    return role["model"] if role else config.WORKER_MODEL


# Live worker registry so the dashboard can show real running machines.
# task_id -> {kind, ref, role, model, project_id, started_at, workdir}
ACTIVE: dict[int, dict] = {}


class LocalLauncher:
    async def launch(self, task: dict, project: dict, contender_id: int | None = None,
                     label: str = "") -> None:
        model = task.get("model") or pick_model(task, project)
        env = _worker_env(task, project, model)
        if contender_id:
            env["CONTENDER_ID"] = str(contender_id)
        suffix = f"-{label}" if label else ""
        workdir = config.WORKSPACES_DIR / f"task-{task['id']}-a{task['attempts']}{suffix}"
        workdir.mkdir(parents=True, exist_ok=True)
        import os
        import time as _t
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(config.WORKER_SCRIPT),
            env={**os.environ, **env, "WORKDIR": str(workdir)},
            cwd=str(workdir),
        )
        ACTIVE[f"{task['id']}{suffix}"] = {"kind": "process", "ref": f"pid {proc.pid}",
                              "pid": proc.pid, "proc": proc,
                              "role": task["role"], "model": model,
                              "project_id": task["project_id"], "started_at": _t.time(),
                              "workdir": str(workdir), "title": task["title"],
                              "task_id": task["id"], "rival": label}
        asyncio.get_event_loop().create_task(
            self._reap(proc, task, f"{task['id']}{suffix}", contender_id))

    async def _reap(self, proc, task: dict, key=None, contender_id: int | None = None) -> None:
        code = await proc.wait()
        ACTIVE.pop(key if key is not None else task["id"], None)
        await asyncio.sleep(3)
        if contender_id:      # a rival that died without reporting just loses
            c = db.get_contender(contender_id)
            if c and c["status"] == "running":
                db.update_contender(contender_id, status="failed",
                                    report=f"attempt exited (code {code}) without reporting")
            return
        # The worker's /internal/report is the source of truth. Grace period: the
        # report POST can land a moment after the process exits, so wait before
        # declaring failure to avoid a spurious "died without reporting".
        await asyncio.sleep(3)
        fresh = db.get_task(task["id"])
        if fresh and fresh["status"] in ("queued", "running"):
            db.update_task(task["id"], status="failed",
                           report=f"worker process exited (code {code}) without posting a report")
            bus.emit(task["project_id"], task["id"], "system", "worker_died",
                     {"exit_code": code})


def _terminate(entry: dict) -> str:
    """Actually stop one running agent. Returns a human-readable outcome."""
    if entry.get("kind") == "process":
        proc = entry.get("proc")
        pid = entry.get("pid")
        try:
            if proc is not None and proc.returncode is None:
                proc.kill()          # SIGKILL: the SDK ignores SIGTERM mid-request
            elif pid:
                os.kill(pid, signal.SIGKILL)
            return f"killed pid {pid}"
        except ProcessLookupError:
            return f"pid {pid} had already exited"
        except Exception as e:
            return f"could not kill pid {pid}: {e}"
    if entry.get("kind") == "k8s-job":
        name = entry.get("ref", "")
        r = subprocess.run(["kubectl", "delete", "job", name, "-n", config.K8S_NAMESPACE,
                            "--ignore-not-found"], capture_output=True, text=True)
        return f"deleted job {name}" if r.returncode == 0 else \
               f"could not delete job {name}: {r.stderr.strip()[:120]}"
    return "unknown agent kind"


def kill_task(task_id: int, reason: str = "stopped by the boss") -> list[str]:
    """Stop every agent working on one task (including rival contenders)."""
    notes = []
    for key, entry in list(ACTIVE.items()):
        if entry.get("task_id") != task_id:
            continue
        notes.append(_terminate(entry))
        ACTIVE.pop(key, None)
    t = db.get_task(task_id)
    if t and t["status"] in ("queued", "running"):
        db.update_task(task_id, status="failed", report=reason)
        bus.emit(t["project_id"], task_id, "system", "agent_killed",
                 {"reason": reason, "detail": "; ".join(notes)})
    return notes


def kill_project(project_id: int, reason: str) -> list[str]:
    """Stop every agent on a project and stop lying about task status.

    Cancelling a project used to leave its workers running — they kept burning
    tokens and their tasks sat 'running' forever, because the only thing that
    ever cleared that status was a report from the worker we had abandoned.
    """
    notes = []
    for key, entry in list(ACTIVE.items()):
        if entry.get("project_id") != project_id:
            continue
        notes.append(_terminate(entry))
        ACTIVE.pop(key, None)
    for t in db.list_tasks(project_id):
        if t["status"] in ("queued", "running"):
            db.update_task(t["id"], status="failed", report=reason)
    for c in db.list_running_contenders(project_id):
        db.update_contender(c["id"], status="failed", report=reason)
    if notes:
        bus.emit(project_id, None, "system", "agents_killed",
                 {"count": len(notes), "reason": reason, "detail": "; ".join(notes)})
    return notes


def sweep_orphans() -> int:
    """A worker cannot outlive the conductor that spawned it.

    So at startup, any task still marked running/queued is by definition a ghost
    from a previous process. Left alone they show up forever as agents you cannot
    kill, and as blockers promising a watchdog that will never come.
    """
    n = 0
    for p in db.list_projects():
        for t in db.list_tasks(p["id"]):
            if t["status"] in ("queued", "running"):
                db.update_task(t["id"], status="failed",
                               report="the conductor restarted while this was running, "
                                      "so the agent was lost; re-run the task to continue")
                n += 1
    return n


class K8sLauncher:
    def __init__(self) -> None:
        from kubernetes import client, config as kcfg
        try:
            kcfg.load_incluster_config()
        except Exception:
            kcfg.load_kube_config()
        self.batch = client.BatchV1Api()
        self.client = client

    async def launch(self, task: dict, project: dict, contender_id: int | None = None,
                     label: str = "") -> None:
        import time as _t
        model = task.get("model") or pick_model(task, project)
        env = _worker_env(task, project, model)
        if contender_id:
            env["CONTENDER_ID"] = str(contender_id)
        k = self.client
        suffix = f"-{label}" if label else ""
        name = f"devteam-worker-{task['id']}-a{task['attempts']}{suffix}"
        ACTIVE[f"{task['id']}{suffix}"] = {"kind": "k8s-job", "ref": name, "role": task["role"],
                              "model": model, "project_id": task["project_id"],
                              "started_at": _t.time(), "workdir": config.K8S_NAMESPACE,
                              "title": task["title"], "task_id": task["id"], "rival": label}
        job = k.V1Job(
            metadata=k.V1ObjectMeta(
                name=name,
                labels={"app": "devteam-worker", "task-id": str(task["id"])},
            ),
            spec=k.V1JobSpec(
                backoff_limit=0,
                ttl_seconds_after_finished=600,
                active_deadline_seconds=3600,
                template=k.V1PodTemplateSpec(
                    metadata=k.V1ObjectMeta(labels={"app": "devteam-worker",
                                                    "task-id": str(task["id"])}),
                    spec=k.V1PodSpec(
                        restart_policy="Never",
                        containers=[
                            k.V1Container(
                                name="worker",
                                image=config.WORKER_IMAGE,
                                env=[k.V1EnvVar(name=n, value=v) for n, v in env.items()],
                                resources=k.V1ResourceRequirements(
                                    requests={"cpu": "250m", "memory": "512Mi"},
                                    limits={"cpu": "1", "memory": "1536Mi"},
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )
        await asyncio.to_thread(
            self.batch.create_namespaced_job, namespace=config.K8S_NAMESPACE, body=job
        )

    def pod_logs(self, task_id: int, tail: int = 200) -> str:
        """Logs straight from the worker's pod (k8s mode)."""
        from kubernetes import client as kclient
        core = kclient.CoreV1Api()
        pods = core.list_namespaced_pod(
            namespace=config.K8S_NAMESPACE, label_selector=f"task-id={task_id}")
        if not pods.items:
            return "no pod found for this task (it may have finished and been cleaned up)"
        return core.read_namespaced_pod_log(
            name=pods.items[0].metadata.name, namespace=config.K8S_NAMESPACE, tail_lines=tail)


_launcher = None


def get_launcher():
    global _launcher
    if _launcher is None:
        _launcher = K8sLauncher() if config.LAUNCHER == "k8s" else LocalLauncher()
    return _launcher


async def dispatch_task(task_id: int, source: str = "scheduler") -> str:
    """Shared dispatch path (used by the DAG scheduler)."""
    task = db.get_task(task_id)
    if not task:
        return f"error: task {task_id} not found"
    project = db.get_project(task["project_id"])
    if not project:
        return f"error: project for task {task_id} not found"
    if project["status"] == "cancelled":
        return "error: project is cancelled"
    running = db.count_running(task["project_id"])
    if running >= project["max_workers"]:
        return f"error: {running} workers already running (max {project['max_workers']}); call wait first"
    # Per-role parallelism cap: don't run more than the role allows at once.
    role_cfg = config.roles_by_name().get(task["role"])
    if role_cfg:
        role_running = sum(1 for t in db.list_tasks(task["project_id"])
                           if t["role"] == task["role"] and t["status"] in ("queued", "running"))
        if role_running >= role_cfg["max_parallel"]:
            return (f"error: {role_running} {task['role']} workers already running "
                    f"(role cap {role_cfg['max_parallel']}); call wait first")
    # Agent-run cap is the primary safety rail (meaningful on subscription, where
    # there is no dollar cost). The dollar cap still applies on API billing.
    if project["runs_used"] >= project["max_runs"]:
        return (f"error: agent-run cap reached ({project['runs_used']} of {project['max_runs']} "
                "runs); wrap up and finish.")
    if config.ANTHROPIC_API_KEY and project["cost_usd"] >= project["budget_usd"]:
        return f"error: budget exhausted (${project['cost_usd']:.2f} of ${project['budget_usd']:.2f})"

    db.inc_runs(task["project_id"])
    model = pick_model(task, project)
    # Record the model this run actually uses so it's visible in the UI and the
    # manager can reason about who is on what.
    db.update_task(task_id, status="queued", attempts=task["attempts"] + 1, model=model)
    task = db.get_task(task_id)

    # --- competitive mode: N rivals attack the same task at once ---------------
    n = int(task.get("compete") or 0)
    if n > 1:
        n = min(n, 3)
        db.clear_contenders(task_id)
        # Deliberately vary the model across rivals when we can: two different
        # models disagree in more useful ways than two runs of the same one.
        models = [model] + [m for m in FALLBACK_ORDER if m != model and not cooldown_left(m)]
        launched = []
        for i in range(1, n + 1):
            cm = models[(i - 1) % len(models)] if models else model
            branch = f"task/{task_id}-c{i}"
            cid = db.create_contender(task_id, i, branch, cm)
            rival = {**task, "branch": branch, "model": cm}
            await get_launcher().launch(rival, project, contender_id=cid, label=f"c{i}")
            launched.append(f"#{i} on {cm}")
        bus.emit(task["project_id"], task_id, source, "contest_started",
                 {"rivals": n, "detail": launched, "title": task["title"]})
        return (f"dispatched {n} rival attempts at task {task_id} "
                f"({', '.join(launched)}); the manager judges them when they finish.")

    await get_launcher().launch(task, project)
    bus.emit(task["project_id"], task_id, source, "dispatched",
             {"role": task["role"], "title": task["title"], "model": model,
              "attempt": task["attempts"]})
    return f"dispatched task {task_id} ({task['role']}: {task['title']}) on {model}, attempt {task['attempts']}"
