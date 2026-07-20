# Known issues

Bugs found and confirmed but **not fixed**, worst first. Each one says what
actually happens, why, and the concrete fix.

---

## 1. Tasks stay "running" forever once a project is stopped

**Confirmed:** projects 1, 2 and 3 (all `cancelled`) still hold tasks marked
`running` after 15–16 hours, with zero worker processes alive.

**Why:** `scheduler._run()` returns immediately when the project's status is
`done`, `failed` or `cancelled`:

```python
if not project or project["status"] in ("done", "failed", "cancelled"):
    return
```

The stall watchdog that flips a silent task to `failed` lives *inside* that
loop, so the moment a project stops, its in-flight tasks are frozen in
`running` and nothing ever reaps them. Workers also can't survive a conductor
restart, so every restart leaves more.

**What it breaks:** the Blockers tab reports them as "silent for 917 min" and
promises "it is killed automatically at 30 min" — which is a lie for these
tasks. The Agents tab shows machines that don't exist, and the deploy guard's
freshness heuristic can count them as live.

**Fix:**
1. Reap on stop — in the cancel/finish paths, flip `running`/`queued` tasks to
   `failed` with a reason like "project was cancelled while this was running".
2. Sweep on startup — an in-process worker cannot outlive the conductor, so at
   boot any task still `running` that is not in `launcher.ACTIVE` is dead by
   definition. Mark it `failed` ("conductor restarted while this was running").
   Do this in `main.py` startup, before schedulers start.
3. Then the Blockers copy becomes true.

---

## 2. A deployed app has no way to receive secrets

**Why:** `deploy._child_env()` is a fixed allowlist (PATH, HOME, PORT, …) —
deliberately, so agent-written code can't read your Anthropic key or GitHub
token. But that leaves *no* channel for the app's own legitimate config, and
the k8s manifests define no Secret either.

**What it breaks:** any app needing an API key, a database URL or an OAuth
client secret cannot run at all. The weather app only works because
Open-Meteo needs no key.

**Fix:** per-project environment variables, owned by the project owner.
1. Table `project_env(project_id, key, value, created_at)`, written only
   through an authenticated route; never returned to the browser in full
   (redact like `auth.redacted`).
2. Local: merge into `_child_env()` *after* the allowlist, and refuse keys that
   collide with platform ones (`ANTHROPIC_*`, `GITHUB_TOKEN`, `WORKER_TOKEN`).
3. k8s: emit a `Secret` and reference it with `envFrom.secretRef` in the
   Deployment.
4. UI: an "Environment" section in the Full deployment card.

Treat the values as secrets at rest — at minimum file-permission the DB; ideally
encrypt with a key from the environment.

---

## 3. Local deployments run agent-written code with your full user rights

**Why:** `deploy_local` starts the app with `subprocess.Popen` as the operator's
user. The environment is scrubbed, but the *filesystem* is not: the process can
read `~/.ssh`, `.env`, `devteam.db`, and reach `localhost:8000`.

**What it breaks:** nothing yet — but the code being run was written by a cheap
model from a prompt, and this is the one place it executes unsandboxed. The k8s
path does not have this problem; pods are isolated.

**Fix:** prefer Docker for local deploys too — `docker run --rm -p PORT:PORT
--network` a dedicated bridge, no mounts of the host tree. That is a small
change since the docker branch already exists; make it the default whenever
`docker` is available and keep the raw subprocess as an explicit opt-in
(`DEPLOY_ALLOW_HOST_PROCESS=1`) with a warning in the UI.

---

## 4. The static preview answers API calls with HTML

**Confirmed:** `GET /preview/11/api/weather?location=x` returns **HTTP 200** and
the body is `<!DOCTYPE html>`.

**Why:** `serve_preview` falls back to `index.html` for anything it can't find,
the standard SPA rule. For a JSON endpoint that produces a "successful" response
the frontend then fails to parse — which is exactly the confusing error in the
original screenshot.

**Fix:** in `preview.serve_preview`, only fall back to `index.html` when the path
has no file extension **and** does not look like an API call. Otherwise return
404 with a JSON body:

```json
{"error": "This is the static preview — it serves files only and cannot run
           this app's backend. Use Artifacts → Full deployment."}
```

That turns a silent mis-parse into a message naming the fix.

---

## 5. Rate-limit cooldowns are forgotten on restart

**Why:** `launcher.COOLDOWN` is a module-level dict. A restart clears it, so the
next dispatch goes straight back to the model that just rate-limited us, and
the fallback logic re-learns the hard way.

**Fix:** persist it — a small `model_cooldown(model, until_ts)` table, read on
startup and written in `note_rate_limit`. Expired rows are ignored, so no
cleanup job is needed.

---

## 6. Nothing ever removes a k8s deployment

**Why:** `deploy.stop()` only handles the local subprocess. There is no
`kubectl delete`, so every k8s deploy leaves its Deployment, Service and Ingress
behind forever, and each redeploy adds an image tag.

**What it breaks:** on DOKS this is money — idle pods hold node capacity and
push you toward another node.

**Fix:** give `stop()` a k8s branch:
`kubectl delete deploy,svc,ingress -n <ns> -l devteam/project=<id>`
(the label is already set). Wire the same Stop button to it, and prune images
older than the newest few tags.

---

## 7. Two concurrent deploys can grab the same port

**Why:** `_free_port()` checks whether a port is free, then returns it. The
child binds it later, so two deploys started together can be handed the same
number.

**Fix:** hold the socket. Bind it in `_free_port`, keep it open until the child
starts with `SO_REUSEADDR`, or simply catch the boot failure and retry with the
next port — the health check already detects "exited immediately".

---

## 8. Nothing prunes worker workspaces

**Confirmed:** `workspaces/` is **1.2 GB** of old repo clones, one per task
attempt, kept forever.

**Fix:** delete a task's workspace once its report is recorded, or keep only the
last N per project and sweep the rest on startup. Keep failed ones a while —
they're useful for debugging — but bound it by count or age.

---

## Gaps that are not bugs

- **`kubectl apply` against DOKS is untested.** Everything up to it is verified
  on kind; only the managed-cluster rollout is unproven. `doctl` isn't installed.
- **Deployed apps are HTTP only.** For real users, add cert-manager and a TLS
  block on the Ingress.
- **The deploy guard over-blocks after a restart.** `can_redeploy` treats
  recently-updated tasks as live because `launcher.ACTIVE` is empty after a
  restart. Deliberate — better to delay a deploy than kill live work — and it
  self-clears. Fixing issue 1 removes most of the annoyance.
