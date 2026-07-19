"""Launch worker agents.

Two backends:
- LocalLauncher: subprocess per worker (dev / docker-compose).
- K8sLauncher:   one Kubernetes Job per worker (DOKS). The conductor's ServiceAccount
                 needs create/list on jobs (see deploy/k8s/rbac.yaml).

Both pass the same env contract to worker/worker.py.
"""

import asyncio
import sys

from . import config, db, bus


def _worker_env(task: dict, project: dict, model: str) -> dict[str, str]:
    auth: dict[str, str] = {}
    if config.ANTHROPIC_API_KEY:
        auth["ANTHROPIC_API_KEY"] = config.ANTHROPIC_API_KEY
    elif config.CLAUDE_CODE_OAUTH_TOKEN:
        auth["CLAUDE_CODE_OAUTH_TOKEN"] = config.CLAUDE_CODE_OAUTH_TOKEN
    # else: local CLI login — subprocess workers inherit the stored credentials.
    return {
        **auth,
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
        "GITHUB_TOKEN": config.GITHUB_TOKEN,
        "MODEL": model,
        "MAX_TURNS": str(config.WORKER_MAX_TURNS),
    }


def pick_model(task: dict) -> str:
    """Base model comes from the task's role config; escalate after repeated failures."""
    if task["attempts"] >= 2:
        return config.ESCALATION_MODEL
    role = config.roles_by_name().get(task["role"])
    return role["model"] if role else config.WORKER_MODEL


class LocalLauncher:
    async def launch(self, task: dict, project: dict) -> None:
        env = _worker_env(task, project, pick_model(task))
        workdir = config.WORKSPACES_DIR / f"task-{task['id']}-a{task['attempts']}"
        workdir.mkdir(parents=True, exist_ok=True)
        import os
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(config.WORKER_SCRIPT),
            env={**os.environ, **env, "WORKDIR": str(workdir)},
            cwd=str(workdir),
        )
        asyncio.get_event_loop().create_task(self._reap(proc, task))

    async def _reap(self, proc, task: dict) -> None:
        code = await proc.wait()
        # The worker's /internal/report is the source of truth. If the process
        # exited but the task is still queued/running, no report arrived — treat
        # it as failed regardless of exit code (the scheduler will retry; a
        # branch it already pushed is reused on the next attempt).
        fresh = db.get_task(task["id"])
        if fresh and fresh["status"] in ("queued", "running"):
            db.update_task(task["id"], status="failed",
                           report=f"worker process exited (code {code}) without posting a report")
            bus.emit(task["project_id"], task["id"], "system", "worker_died",
                     {"exit_code": code})


class K8sLauncher:
    def __init__(self) -> None:
        from kubernetes import client, config as kcfg
        try:
            kcfg.load_incluster_config()
        except Exception:
            kcfg.load_kube_config()
        self.batch = client.BatchV1Api()
        self.client = client

    async def launch(self, task: dict, project: dict) -> None:
        env = _worker_env(task, project, pick_model(task))
        k = self.client
        name = f"devteam-worker-{task['id']}-a{task['attempts']}"
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
                    metadata=k.V1ObjectMeta(labels={"app": "devteam-worker"}),
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
    db.update_task(task_id, status="queued", attempts=task["attempts"] + 1)
    task = db.get_task(task_id)
    model = pick_model(task)
    await get_launcher().launch(task, project)
    bus.emit(task["project_id"], task_id, source, "dispatched",
             {"role": task["role"], "title": task["title"], "model": model,
              "attempt": task["attempts"]})
    return f"dispatched task {task_id} ({task['role']}: {task['title']}) on {model}, attempt {task['attempts']}"
