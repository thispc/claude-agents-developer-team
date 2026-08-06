# Known issues

**Status after the 2026-07-20 fix pass:** issues 4, 6, 8, 10, 11, 12, 13, 14,
15, 16, 17 are fixed (see the commit "fix 11 known bugs…"), plus two new ones
found by a security probe (suggest-team and model-health were anonymous — fixed).
Still open: 2, 3, 5, 9 below. (8 fixed since — see below.)

Bugs found and confirmed but **not fixed**, worst first. Each one says what
actually happens, why, and the concrete fix.

## 1. ~~Tasks stay "running" forever~~ — FIXED

Cancel now kills every agent on the project, a startup sweep releases ghosts
from a previous process, and the stall watchdog covers `queued` as well as
`running`. Kept here as a pointer: the Agents tab has a per-agent **■ stop**.

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

## 4. [FIXED] The static preview answers API calls with HTML

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

## 5. [FIXED] Rate-limit cooldowns are forgotten on restart

**Why:** `launcher.COOLDOWN` was a module-level dict. A restart cleared it, so
the next dispatch went straight back to the model that just rate-limited us,
and the fallback logic re-learned the hard way.

**Fix:** persisted via the `model_cooldown(model, until_ts, reason)` table —
`launcher.load_cooldowns()` reads it into `COOLDOWN` on startup, and
`launcher.note_rate_limit()` writes through `db.set_cooldown()`. Expired rows
are dropped on read by `db.load_cooldowns()`, so no cleanup job is needed.

---

## 6. [FIXED] Nothing ever removes a k8s deployment

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

## 7. [FIXED] Two concurrent deploys can grab the same port

**Why:** `_free_port()` checked whether a port was free with `connect_ex`, then
closed the probe socket and returned the number. The child binds it later, so
two deploys started together could scan, both see the same port free, and both
be handed it.

**Fix:** `_free_port()` now binds the port itself (`SO_REUSEADDR`) and hands
back the still-open socket — a concurrent scan's own `bind()` on that port
fails outright instead of racing. `deploy_local` holds the reservation for the
build and closes it in a `finally` right before the child starts, so a failed
build can't leak it either.

---

## 8. [FIXED] Nothing prunes worker workspaces

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

---

# Found by the full-codebase audit (2026-07-20)

Confirmed by reading every file. Not yet fixed.

## 9. A k8s worker Job is never cleaned out of the live-agent registry

`K8sLauncher` has no equivalent of `LocalLauncher._reap` running alongside a
live Job, so nothing removes its `ACTIVE` entry once the Job finishes.
Completed Jobs show as live agents forever, and a Job that dies without
reporting never fails its task while the conductor stays up. Still open.

**[FIXED, boot only]** `sweep_orphans` used to assume "a worker cannot
outlive the conductor" — true for a subprocess, **false for a Job owned by
the cluster** — and would fail tasks at startup whose Jobs were still
genuinely running. `K8sLauncher` now exposes `reap_orphans`/`job_status`,
the k8s analogue of `_reap`: it queries each queued/running task's Job
before deciding, and `sweep_orphans` defers to it when `LAUNCHER=k8s`
instead of failing everything unconditionally. The runtime half above (a
Job that finishes *while the conductor is up*) is unaffected — that still
needs the reconcile-pass-plus-`ACTIVE`-cleanup fix described here.

## 10. [FIXED] "429" in a task's own report reroutes it forever

`RATE_LIMIT_MARKERS` includes the bare strings `"429"` and `"quota"`, and
`pick_model` tests them against `task["report"]`. A tester task that legitimately
*mentions* HTTP 429 is treated as rate-limited on every retry, is permanently
misrouted, and emits a bogus `rate_limited` event with a fabricated 300s cooldown.
The stale report is never cleared on rework, so it persists.

**Fix:** only inspect reports from runs that actually failed, require a
co-occurring signal (`rate_limit`, `overloaded`, `retry-after`), and clear
`report` when a task is re-dispatched.

## 11. [FIXED] Rival branches are reused between contests

Rivals always use `task/<id>-c<i>`. `clear_contenders` deletes the rows but not
the git branches, and the worker checks out an existing branch — so a second
contest's rival #1 inherits the previous contest's losing code.

**Fix:** include the attempt number in the branch (`task/<id>-a<n>-c<i>`), or
delete the remote branches when clearing contenders.

## 12. [FIXED] A recruited role with a space silently loses its model

The manager normalises roles to `lower-kebab`, but the recruited roster stores
snake_case. "Propulsion Engineer" → `propulsion-engineer` never matches the
roster's `propulsion_engineer`, so the boss's per-role model choice is discarded
with no warning. The per-role `max_parallel` cap is skipped for recruited roles
too, since it only applies to roles found in `roles.json`.

**Fix:** normalise both sides through one helper, and apply the parallelism cap
from the roster as well.

## 13. [FIXED] `accept_task` doesn't restart the scheduler

Every other mutating manager tool calls `scheduler.ensure()`. If the scheduler
task has exited, accepting a task never restarts dispatching and its dependents
never run.

**Fix:** one line — call `scheduler.ensure(project_id)`.

## 14. [FIXED] A dispatch that throws kills the scheduler permanently

`await launcher.dispatch_task(...)` is unguarded in the scheduler loop. A k8s API
error or a subprocess failure escapes, kills the loop task, and nothing restarts
it until some manager tool happens to call `ensure()`. The exception is never
retrieved, so it is never logged either. `inc_runs` and `attempts+1` also happen
*before* the launch, so a failed launch still consumes the run cap.

**Fix:** wrap the dispatch in try/except, emit the failure as an event, and move
the counter increments after a successful launch.

## 15. [FIXED] The worker discards its work when the session errors

On any session exception the worker reports `failed` and returns **without
pushing**, so everything already written to the checkout is lost — and the retry
clones a fresh workspace. This is what lost the cart-and-checkout work.

**Fix:** commit and push whatever exists before reporting the failure; the branch
is reviewable even if incomplete.

## 16. [FIXED] Prompt files contradict the code

- `agents/manager.md` still says "your team members (backend, frontend, tester)"
  though roles are now fully dynamic, and still frames decisions around spending
  money — the exact framing the code removed because it made managers cut
  projects short.
- It promises "the third attempt auto-escalates", which `pinned_model` silently
  disables.
- It never mentions that `finish` will refuse while any task has failed.
- `roles.json` says a new role needs a `.md` file; unknown roles now get a
  generic prompt.

**Fix:** these are the manager's actual instructions, so drift here changes
behaviour. Re-read `manager.md` against `manager.py` and correct all four.

## 17. [FIXED] Sessions never expire

`sessions` has no expiry column and nothing deletes rows except explicit logout.
Every cookie ever issued is valid forever.

**Fix:** store an expiry, check it in `user_for_token`, sweep on startup.

## 18. [FIXED] Dead migrations mask real ones

All of the `ALTER TABLE` statements duplicated columns already in `SCHEMA`, so on
a fresh database every one raised and was swallowed by a blanket
`except OperationalError: pass` — which would also have swallowed a *genuine*
migration failure on an old database. The `seq` backfill also re-ran on every
startup with a correlated subquery per row.

**Fix:** `db.init()` now tracks progress with `PRAGMA user_version`. Only the
migrations past the stored version run at all, so a caught-up database (a fresh
one included) does none of this on later boots. What does run only ignores the
specific "duplicate column" error; anything else propagates. The `seq` backfill
moved inside the same guard, so it rides along once instead of re-running every
startup.
