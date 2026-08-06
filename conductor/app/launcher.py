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

from . import ambition, config, db, bus, team, tuning


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
        # Both variables are always set, one of them to "". Returning only the key
        # we want was not enough: the worker env is built as {**os.environ, **env},
        # so an unset variable is inherited from the operator's shell — a user with
        # only an API key still leaked the operator's CLAUDE_CODE_OAUTH_TOKEN, and a
        # user with nothing fell through to the operator's `claude` CLI login.
        blank = {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": "",
                 # A worker on a non-Anthropic teammate runs on one of these. Blanked
                 # for the same reason as the others: unset means inherited from the
                 # operator's shell, which is how a user would spend someone else's key.
                 "OPENAI_API_KEY": s.get("openai_api_key", ""),
                 "GEMINI_API_KEY": s.get("gemini_api_key", ""),
                 # …and the CLI login lives in a config dir under the operator's
                 # HOME, so point elsewhere or it is reachable regardless.
                 "CLAUDE_CONFIG_DIR": str(config.WORKSPACES_DIR / f"cfg-u{owner['id']}")}
        if s.get("anthropic_api_key"):
            return {**blank, "ANTHROPIC_API_KEY": s["anthropic_api_key"]}
        if s.get("claude_oauth_token"):
            return {**blank, "CLAUDE_CODE_OAUTH_TOKEN": s["claude_oauth_token"]}
        return blank      # no Claude credentials: OpenAI/Gemini teammates still run
    # Root / operator: their stored settings, then the server's, then CLI login.
    if owner:
        s = auth_mod.get_settings(owner)
        byok = {k: v for k, v in (("OPENAI_API_KEY", s.get("openai_api_key")),
                                  ("GEMINI_API_KEY", s.get("gemini_api_key"))) if v}
        if s.get("anthropic_api_key"):
            return {**byok, "ANTHROPIC_API_KEY": s["anthropic_api_key"]}
        if s.get("claude_oauth_token"):
            return {**byok, "CLAUDE_CODE_OAUTH_TOKEN": s["claude_oauth_token"]}
        if byok:
            return byok
    if config.ANTHROPIC_API_KEY:
        return {"ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY}
    if config.CLAUDE_CODE_OAUTH_TOKEN:
        return {"CLAUDE_CODE_OAUTH_TOKEN": config.CLAUDE_CODE_OAUTH_TOKEN}
    return {}   # local CLI login — inherited from the environment


def owner_github_token(project: dict) -> str:
    """The token a worker clones and pushes with — the project owner's.

    A non-root user with no token of their own used to fall through to the
    operator's here, so their worker cloned and pushed as the operator. Resolved
    through the one place that gets this right for everyone.
    """
    from . import github_client
    return github_client.token_for(project)


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


def prior_attempt(task: dict) -> str:
    """What the previous attempt on THIS task got done before it stopped.

    A retry used to start cold: the only context it received was manager feedback,
    which a run that died on a session limit never has. So the agent re-derived
    everything it had already worked out, and the expensive part of the work was
    paid for twice. The branch carried the committed files, but not the reasoning.

    On the mars-rover run six of eight agent runs were retries of this kind.
    """
    if not task.get("attempts") or not (task.get("report") or "").strip():
        return ""
    why = ("Your previous attempt was cut short by a capacity limit, not by a "
           "mistake" if looks_rate_limited(task.get("report", ""))
           else "Your previous attempt did not finish")
    return (f"{why}. This is what it reported before stopping — the branch already "
            f"has whatever it committed, so CONTINUE from here rather than starting "
            f"over:\n\n{task['report'][:4000]}")


def _worker_env(task: dict, project: dict, model: str) -> dict[str, str]:
    auth = owner_credentials(project)
    agent = db.get_agent(task["agent_id"]) if task.get("agent_id") else None
    provider = team.provider_for(agent)
    # Provider and model have to agree or the worker asks OpenAI for a Claude model
    # and gets a 404. They can disagree honestly: every model-picking rule above —
    # the rate-limit fallback, the escalation ladder, the roles.json default — was
    # written when every worker was Claude, so a throttled Gemini teammate is handed
    # claude-haiku by pick_model. The teammate's provider is the thing the boss
    # actually chose, so it wins and the model comes back to that provider's default.
    if provider != "anthropic" and (not model or model.startswith("claude")):
        from . import providers
        own = (agent or {}).get("model") or ""
        model = (own if not own.startswith("claude") else "") \
            or providers.default_model(provider) or model
    return {
        **auth,
        # Who this is. Empty for projects created before teammates existed, and the
        # worker treats empty as "no persona", so those keep behaving as they did.
        "AGENT_NAME": (agent or {}).get("name", ""),
        "AGENT_CONTEXT": team.system_addendum(agent),
        "HANDOFF_CONTEXT": handoff_context(task),
        "CONDUCTOR_URL": config.CONDUCTOR_URL,
        "WORKER_TOKEN": config.WORKER_TOKEN,
        "TASK_ID": str(task["id"]),
        "PROJECT_ID": str(task["project_id"]),
        "ROLE": task["role"],
        "TASK_TITLE": task["title"],
        "TASK_DESCRIPTION": task["description"],
        "TASK_FEEDBACK": task.get("feedback", ""),
        "PRIOR_ATTEMPT": prior_attempt(task),
        # What the last attempt's checks concluded, so a retry over an unchanged
        # commit does not pay to re-derive the same green result. Only ever reused
        # when it PASSED and the commit still matches — see run_verification.
        "PRIOR_VERIFICATION": (task.get("verification") or ""
                               if tuning.get("reuse_verification") else ""),
        "BRANCH": task["branch"],
        "REPO": project["repo"],
        "GITHUB_TOKEN": owner_github_token(project),
        # The git host to clone from. The local launcher would inherit this from
        # the process environment, but a k8s worker gets only this dict — so
        # without it a self-hosted git user's cloud workers silently clone from
        # github.com and fail on a repository that is not there.
        "GIT_WEB": config.GIT_WEB,
        # Which engine builds. Empty for every project that predates non-Claude
        # teammates, and the worker reads empty as "anthropic", so those are
        # untouched.
        "PROVIDER": provider,
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


# Strong signals: their presence alone means we were throttled.
#
# "session limit" earns its place the hard way. Anthropic's subscription message is
# "You've hit your session limit · resets 3pm", which matched none of the others —
# so two deaths that were purely capacity got classified as QUALITY failures and
# sent the task up the escalation ladder. On the mars-rover run that burned four
# retries and the manager had to reassign by hand, writing "both prior attempts
# died on a session/rate limit (not a quality failure)".
RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "overloaded", "session limit",
                      "usage limit", "too many requests", "retry-after")
# Weak signals: a task's own output can legitimately contain these (a tester
# reporting an HTTP 429, code mentioning a quota). Only count them alongside a
# strong signal or an explicit API error shape.
_WEAK_MARKERS = ("429", "quota")


def looks_rate_limited(text: str) -> bool:
    t = (text or "").lower()
    if any(m in t for m in RATE_LIMIT_MARKERS):
        return True
    # A weak marker only counts next to an error/status context, not when a task
    # merely describes a 429 it saw while testing.
    return any(m in t for m in _WEAK_MARKERS) and \
        any(ctx in t for ctx in ("error", "status", "exceeded", "throttl"))


# model -> unix timestamp when it should be usable again (from real retry-after /
# reset values the API gives us on a 429). Empty until something actually throttles.
COOLDOWN: dict[str, float] = {}


def load_cooldowns() -> int:
    """Restore throttle state at startup. Without this every restart forgot which
    models were rate limited and dispatched straight back onto them."""
    try:
        COOLDOWN.update(db.load_cooldowns())
    except Exception:
        return 0
    return len(COOLDOWN)


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
    try:
        db.set_cooldown(model, until, text[:200])   # survive a restart
    except Exception:
        pass
    # The only quota signal we did not invent — worth a record in the provider's own words,
    # because every other number on the usage screen is a proxy for this one.
    from . import logs
    logs.warn("quota", "rate_limited", f"{model} is cooling down for {seconds}s",
              model=model, seconds=seconds, said=(text or "")[:200])
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


def pick_model(task: dict, project: dict | None = None, agent: dict | None = None) -> str:
    """Model precedence: an explicit reassignment by the manager > a fallback when the
    previous run was rate-limited > escalation after repeated failures > THIS teammate's
    own model > the boss's recruited per-role choice > the role's roles.json default >
    WORKER_MODEL.

    A teammate's model sits below escalation on purpose, and getting that wrong broke
    a live run. Taking the teammate's model unconditionally — which is what "the person
    has their own model" naively means — silently disabled every rule above it: the
    manager reassigned a failing task to sonnet-5, the dispatch ignored it and sent the
    teammate's model again, and the task failed the same way a second time. Whose model
    it is answers a different question from what to do when that model is not working."""
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
    # How many failures before spending a bigger model. At the exacting setting
    # this is 1, because there a cheap first attempt is not a saving: the failure
    # costs a full run, and so does the retry.
    if task["attempts"] >= int(ambition.get(project, "escalate_after_attempts",
                                            int(tuning.get("escalate_after_attempts")))):
        # Escalate RELATIVE to whatever just failed. Returning a fixed model made
        # this a no-op whenever the recruited roster had already assigned that
        # model: the mars-rover run "escalated" sonnet-5 to sonnet-5, spending a
        # retry without changing anything about the attempt.
        prev = task.get("model") or ""
        if prev in FALLBACK_ORDER:
            nxt = FALLBACK_ORDER.index(prev) + 1
            return FALLBACK_ORDER[nxt] if nxt < len(FALLBACK_ORDER) else prev
        return config.ESCALATION_MODEL
    # This particular teammate's model, if they were given one. Resolved through
    # the same alias table as everything else — the roster speaks in tiers
    # ("worker", "lead"), and passing one of those through as a model id reaches
    # the vendor as a model called "worker" and fails the run outright.
    if agent and agent.get("model"):
        return config._resolve_model(agent["model"])
    if project:
        import json
        want = canon_role(task["role"])
        for member in json.loads(project.get("team") or "[]"):
            if canon_role(member.get("role", "")) == want and member.get("model"):
                return config._resolve_model(member["model"])
    # Where the work STARTS, before anything has failed. An exacting project
    # begins on the stronger tier rather than arriving there via a wasted attempt.
    tier = ambition.worker_tier(project)
    if tier:
        return config._resolve_model(tier)
    role = config.roles_by_name().get(task["role"])
    return role["model"] if role else config.WORKER_MODEL


def canon_role(role: str) -> str:
    """Canonical role key: lowercase, spaces and underscores both become hyphens.

    The manager stores task roles as lower-kebab; the recruited roster may store
    "Propulsion Engineer" or "propulsion_engineer". Without normalising both sides
    a recruited role silently loses its per-role model and parallelism cap.
    """
    return "-".join(str(role).strip().lower().replace("_", " ").replace("-", " ").split())


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


def _dir_bytes(path) -> int:
    """What a clone actually costs on the volume.

    Files that vanish mid-walk (a worker finishing at the same moment) are skipped
    rather than raised on: an approximate size is fine here, and a pruner that dies
    on a race is not.
    """
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def prune_workspaces(keep: int = 8, budget: int | None = None) -> int:
    """Delete old per-attempt worker clones: keep the most recent `keep`, and keep
    the whole directory under `budget` bytes.

    Every task attempt clones the repo into its own dir and nothing removed them,
    so workspaces/ grew without bound (1.2 GB before this existed). Counting them
    was never a bound though — repo size is unbounded, so eight clones of a large
    repo fill the 10Gi volume exactly as well as eighty of a small one, and a full
    volume stops the whole platform because every agent needs somewhere to clone.
    """
    import re
    import shutil
    base = config.WORKSPACES_DIR
    budget = config.WORKSPACES_BUDGET_BYTES if budget is None else budget
    if not base.exists():
        return 0

    # A clone whose task is finished has nothing left in it that isn't on the
    # branch it pushed. Keeping 30 of them regardless of state meant a project
    # with retries sat on hundreds of megabytes of dead checkouts — the mars-rover
    # run left 246 MB across 5 attempts of a single task. Live work is never
    # touched, however old it looks.
    live: set[int] = set()
    try:
        for t in _rows_of_live_tasks():
            live.add(t["id"])
    except Exception:
        return 0        # if we cannot tell what is live, delete nothing

    def task_of(d) -> int | None:
        m = re.match(r"task-(\d+)", d.name)
        return int(m.group(1)) if m else None

    dirs = sorted((d for d in base.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    finished = [d for d in dirs if (task_of(d) not in live)]
    sizes = {d: _dir_bytes(d) for d in dirs}
    on_disk = sum(sizes.values())

    # Keep a handful of the most recent finished clones for post-mortems.
    doomed = list(finished[keep:])
    freed = sum(sizes[d] for d in doomed)
    # Then oldest-first through the post-mortem window until the volume fits.
    # A clone that survives the count rule still goes if the budget needs it —
    # post-mortems are a convenience and a full volume halts the platform.
    for d in reversed(finished[:keep]):
        if on_disk - freed <= budget:
            break
        doomed.append(d)
        freed += sizes[d]

    for d in doomed:
        shutil.rmtree(d, ignore_errors=True)

    if on_disk - freed > budget:
        # Everything prunable is gone and it still does not fit, so what remains is
        # live work that must not be touched. Pruning cannot fix this one, and the
        # old behaviour — return a tidy count and let the volume fill — reported
        # success right up until every agent lost the ability to clone.
        bus.emit(0, None, "system", "workspaces_over_budget",
                 {"bytes": on_disk - freed, "budget": budget,
                  "live_clones": len(dirs) - len(doomed),
                  "detail": f"{(on_disk - freed) // 1048576} MB of worker clones "
                            f"against a {budget // 1048576} MB budget, and every "
                            f"prunable one is already gone — the rest belong to "
                            f"live tasks. Raise WORKSPACES_BUDGET_BYTES or the "
                            f"volume, or stop some work."})
    return len(doomed)


def _rows_of_live_tasks() -> list[dict]:
    """Tasks whose workspace must survive: still queued, running, or awaiting review."""
    return db._rows(
        "SELECT id FROM tasks WHERE status IN ('queued','running','pushed','review')")


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
    # A sandboxed candidate build must not be able to spend a run, touch a repo or
    # load a credential — so the mock engine intercepts before any of that happens,
    # not inside the worker where a bug could still slip past.
    if config.DEMO_MODE:
        from . import demo
        return await demo.simulate(task_id)
    task = db.get_task(task_id)
    if not task:
        return f"error: task {task_id} not found"
    project = db.get_project(task["project_id"])
    if not project:
        return f"error: project for task {task_id} not found"
    if project["status"] == "cancelled":
        return "error: project is cancelled"
    # If every candidate model is in cooldown, don't spend an attempt hammering a
    # throttled one — leave the task planned and let the scheduler retry after the
    # soonest window opens. This is what lets an overnight run ride out a limit.
    #
    # FALLBACK_ORDER is Claude models, so this only speaks for a Claude teammate.
    # Holding a Gemini worker back because Anthropic is throttled would stall work
    # that has nothing to do with the limit.
    if team.provider_for(team.assign(task)) == "anthropic" and \
            all(cooldown_left(m) > 0 for m in FALLBACK_ORDER):
        soonest = min(cooldown_left(m) for m in FALLBACK_ORDER)
        bus.emit(task["project_id"], task_id, "system", "waiting_on_cooldown",
                 {"seconds": soonest,
                  "models": {m: cooldown_left(m) for m in FALLBACK_ORDER}})
        return (f"waiting: every model is rate limited; the soonest is usable again "
                f"in {soonest}s — task stays planned and will dispatch then")

    creds = owner_credentials(project)
    # Any provider a worker can actually build on. Anthropic is no longer the only
    # one: an OpenAI or Gemini teammate runs the agentic loop in worker/agentic.py,
    # so refusing to dispatch without a Claude key would be the "bring your own
    # keys" promise failing at the only step that matters.
    if not any(creds.get(k) for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                                      "OPENAI_API_KEY", "GEMINI_API_KEY")) \
            and project.get("owner_id"):
        owner = db.get_project(task["project_id"])
        db.update_task(task_id, status="failed",
                       report="the project owner has no AI credentials (Anthropic key or "
                              "Claude subscription token, OpenAI key, or Gemini key), so "
                              "no agent can run. Add one in Settings.")
        bus.emit(task["project_id"], task_id, "system", "no_credentials", {})
        return "error: project owner has no AI credentials"
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
    # Claim a named teammate before choosing a model: a teammate may carry their
    # own model, and that choice has to win over the platform's default or
    # per-role model selection is decorative.
    agent = team.claim(task)
    model = pick_model(task, project, agent)
    # Record the model this run actually uses so it's visible in the UI and the
    # manager can reason about who is on what.
    # Clear the previous run's report on re-dispatch: pick_model reads it for
    # rate-limit signals, and a stale report would mislead every future attempt.
    db.update_task(task_id, status="queued", attempts=task["attempts"] + 1,
                   model=model, report="")
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
            branch = f"task/{task_id}-a{task['attempts'] + 1}-c{i}"
            cid = db.create_contender(task_id, i, branch, cm)
            rival = {**task, "branch": branch, "model": cm}
            db.start_run(task["project_id"], task_id, role=task["role"], model=cm,
                         attempt=task["attempts"], kind="contender",
                         sprint=task.get("sprint") or 1, contender_id=cid,
                         agent_id=task.get("agent_id"),
                         profile=tuning.profile_of(project))
            await get_launcher().launch(rival, project, contender_id=cid, label=f"c{i}")
            launched.append(f"#{i} on {cm}")
        bus.emit(task["project_id"], task_id, source, "contest_started",
                 {"rivals": n, "detail": launched, "title": task["title"]})
        return (f"dispatched {n} rival attempts at task {task_id} "
                f"({', '.join(launched)}); the manager judges them when they finish.")

    db.start_run(task["project_id"], task_id, role=task["role"], model=model,
                 attempt=task["attempts"], kind="task", sprint=task.get("sprint") or 1,
                 agent_id=task.get("agent_id"), profile=tuning.profile_of(project))
    await get_launcher().launch(task, project)
    bus.emit(task["project_id"], task_id, source, "dispatched",
             {"role": task["role"], "title": task["title"], "model": model,
              "attempt": task["attempts"]})
    return f"dispatched task {task_id} ({task['role']}: {task['title']}) on {model}, attempt {task['attempts']}"
