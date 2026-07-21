# Everything the code does not yet do that the plan says it should

Audited against the stated product, not against the backlog. Each item says what
is true today, because "missing" and "half-built" need different work.

Legend: **✗** absent · **◐** partial · **⚠** defect in something that exists

---

## 1. Bring your own everything

The pitch is *your keys, your cloud, your git*. Two of those three are currently
false, and the third is true only for DigitalOcean.

| | | |
|---|---|---|
| G1 | ✗ | **Workers are Claude-only.** `FALLBACK_ORDER` is three Claude models and the worker is built on the Claude Agent SDK. `providers.py` speaks to three vendors but is used only by plan mode and recruiting. "Any agent in any role" is true for *planning*, false for *building*. Weeks of work: an agentic loop with file/shell tools per provider. |
| G2 | ✗ | **No per-role provider or model choice.** The roster is `{role, count, model}` where model is the string `worker` or `lead`, resolved to Claude ids. You cannot say "backend on Gemini, tester on Claude". |
| G3 | ✗ | **Git means github.com.** `API = "https://api.github.com"` is hardcoded. No GitHub Enterprise, Gitea, or self-hosted base URL. A user with their own git cannot use the platform at all. |
| G4 | ◐ | **Cloud means DigitalOcean.** `deploy.py` is generic k8s/docker, but `envs.py` and `cloud.py` assume DOCR, DO firewalls and DO load balancers. No AWS/GCP path, and the DOKS-specific knowledge is baked into code rather than a provider interface. |
| G5 | ✗ | **No custom model endpoints.** The provider list is fixed to Anthropic/OpenAI/Google. No Bedrock, Vertex, or self-hosted vLLM — which is the same "bring your own" promise applied to inference. |

## 2. The round table

| | | |
|---|---|---|
| G6 | ◐ | **Blueprint → project handoff is thin.** A blueprint produces a team roster, but personas, provider choices and the manager's character do not carry across. The round table's output is mostly discarded at the moment it matters. |
| G7 | ⚠ | **The UI defaults to `debate`, which the evidence does not support.** `diverge` is the mode the research favours for open-ended work and it is the non-default. The default should be the one that works. |
| G8 | ✗ | **Seats cannot be personas from the eventual team.** You argue an idea with strangers, then hire different people to build it. |

## 3. Team, roles and personas

| | | |
|---|---|---|
| G9 | ✗ | **No per-role persona.** Only `manager_persona` exists. `roles.json` carries a summary, not a character. The plan calls for a persona per agent. |
| G10 | ✗ | **Personas cannot be changed later.** No edit path at all, so "it can change personas later" is not possible for the user or the manager. |
| G11 | ◐ | **The user cannot upscale or downscale a role mid-run.** The manager has `reassign_task`; the human has no equivalent control. |
| G12 | ✗ | **No agent identity or continuity.** Every task gets a fresh session. "The backend engineer" is a role label, not a persistent teammate with memory of what they built. |

## 4. The orchestration algorithm — the heart

This is the part described as the core of the work, and it is the least developed
relative to the ambition. Today the manager plans a DAG once and reviews what
comes back; the scheduler dispatches whatever is unblocked.

| | | |
|---|---|---|
| G13 | ✗ | **No work-sharing or rebalancing.** Idle agents stay idle while others are blocked. The scheduler has no notion of capacity, only of dependencies. |
| G14 | ✗ | **No result reuse.** If a tester already verified something, a later task re-does it from scratch. Nothing is memoised across tasks. |
| G15 | ✗ | **Reviewed work is never discussed with teammates.** The manager judges alone. The plan says the reviewed work should be discussed with the team — and the research says the *aggregation* is worth several times more than extra voices, so this is the high-value version of "more agents". |
| G16 | ◐ | **`ask_teammate` consults a model, not a teammate.** It fires a fresh one-shot query with no access to what the actual teammate built. |
| G17 | ✗ | **No process model.** Neither agile nor waterfall is expressed anywhere. There is no backlog, no capacity, no ceremony, no notion of a task being pulled rather than pushed. |
| G18 | ✗ | **The algorithm cannot be tuned without editing code.** Contest width, escalation thresholds, review depth, when to consult — all constants in source. The plan explicitly expects frequent tweaking. |
| G19 | ✗ | **No measurement harness.** There is no way to tell whether a tweak improved anything: no per-run metrics, no comparison between configurations, no record of what was tried. Without this, tuning is guessing. |

## 5. Artifacts and output

| | | |
|---|---|---|
| G20 | ✗ | **No artifact versioning across sprints.** Sprint 1's output is overwritten by sprint 2. You cannot see what shipped when. |
| G21 | ✗ | **No release notes.** Nothing summarises what changed between sprints. |
| G22 | ✗ | **No per-agent artifacts.** The Artifacts tab has no agent dimension; you cannot see what a given role produced. |
| G23 | ◐ | **Non-code output is readable, not beautiful.** Files render as plain text — no markdown rendering, no diagrams, no document view. A blueprint project's output looks like a source dump. |
| G24 | ◐ | **Two unrelated "run it" paths.** `deploy.py` (project apps) and `sandbox.py`/`envs.py` (the platform) do the same job with none of the same features. Project deploys have no canary, no image identity, no verification gate. |
| G25 | ⚠ | **Project deploys can only go forward and back, not sideways.** Rollback exists now; there is still no preview-per-branch for a user's app. |

## 6. Sprints and the feedback loop

| | | |
|---|---|---|
| G26 | ✗ | **The manager does not decide the sprint count.** The user picks a number up front. The plan says the manager should judge how many are needed. |
| G27 | ✗ | **No end-of-sprint review with the user.** A digest is filed as a GitHub issue; there is no in-app moment where you look at the artifacts and respond. |
| G28 | ✗ | **No scheduled check-ins.** "One-on-ones before running unlimited sprints" has no mechanism. |
| G29 | ✗ | **The manager never elicits requirements.** It plans unilaterally from the brief. It never interviews the client, and at sprint boundaries it decides alone rather than asking what is wanted next. |
| G30 | ◐ | **Feedback has no structured path.** You can send a free-text directive. There is no "here are my notes on this artifact" that ties comment to object. |

## 7. Self-improvement

| | | |
|---|---|---|
| G31 | ✗ | **Nothing is scheduled.** Self-repair runs when a human asks. The plan calls for a daily pass set by root. |
| G32 | ✗ | **Logs are captured, never analysed.** Non-200s and browser errors reach the events table and GitHub issues; nothing aggregates them into candidate work. |
| G33 | ✗ | **Bugs are not artifacts.** A found defect becomes a GitHub issue, not an object the platform holds, triages, prioritises and turns into a sprint. |
| G34 | ✗ | **No prioritisation model.** Nothing decides which bug is worth a sprint. |
| G35 | ◐ | **The mock sandbox is not tied to what changed.** `DEMO_MODE` gives a clickable build; it is not linked to release notes, so "see what changed" is still reading a diff. |
| G36 | ⚠ | **The staging gate is unproven and off.** `REQUIRE_STAGING=0`, and the in-pod suite has never completed — one test hangs on the shared node and I have not found which. |
| G37 | ✗ | **No scheduled adoption.** `AUTO_UPDATE=0`; a verified image waits for a human. |

## 8. Defects in what already exists

| | | |
|---|---|---|
| G38 | ⚠ | **Production is running an image that predates today's fixes** — artifacts rework, `hold`-restart, files API, `SELF_REPO`. |
| G39 | ⚠ | **In-memory state lost on restart:** notification dedup (`_SEEN`) and login lockout (`_FAILED`). A crash-looping pod re-notifies; a restart clears a lockout. |
| G40 | ⚠ | **Nothing notices if the platform is down.** Notifications are sent *by* the platform, so a crash-loop is silent. Needs external uptime monitoring. |
| G41 | ⚠ | **Workspace pruning keeps 8 clones regardless of size.** On a 10Gi volume a few large repos still fill it, and a full volume stops everything. |
| G42 | ⚠ | **`envs.promote` assumes one deployment name.** Fine today, wrong the moment there are two apps in a namespace. |

---

## What I would argue about in this plan

**"Each agent has its own artifacts" may be the wrong shape.** Agents are
stateless task-runners; the *task* is the durable thing and the agent is how it
got done. Per-agent artifacts would mostly re-index the same objects. Per-role
*views* over task artifacts probably give what you want without inventing a
second ownership model.

**G15 and G19 are the highest-value items in the whole list**, and they are not
the ones that look most impressive. Discussing reviewed work with teammates is
the evidence-backed version of "more agents", and a measurement harness is what
makes every future tweak to the algorithm knowable rather than a hunch. Without
G19, work on the orchestration algorithm cannot be evaluated — which makes it the
prerequisite for the part you called the heart.
