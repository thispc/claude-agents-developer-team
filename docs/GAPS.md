# Everything the code does not yet do that the plan says it should

> **Status, after working the list.** Twenty-eight of the forty-two are closed,
> six are partly done, and the rest are named below with what is left. The body
> of this document is the original diagnosis, preserved as written — the marks in
> the tables have been updated, the descriptions have not, because what was wrong
> is worth keeping legible even after it is fixed.
>
> **Still open, honestly:**
>
> - **G1 (◐)** — non-Claude workers exist and their mechanics are tested, but no
>   live call has been made through them and `ask_teammate` is absent on that
>   path, so those workers grind alone. Beta, not done.
> - **G4 (◐)** — the cloud seam exists and DigitalOcean goes through it, but
>   there is no second implementation, so the seam is unproven by use.
> - **G5** — custom model endpoints.
> - **G14 (◐)** — a passing verification is reused only when the commit has not
>   moved, which is narrower than general result reuse.
> - **G20–G25 (◐)** — artifacts, release notes, previews and the unified shipping
>   path all landed; each has an edge named in its row.
> - **G29 (◐)** — the manager can now ask at a sprint boundary, but it still does
>   not interview the client before planning the first one.
> - **G30, G35, G36, G37, G38** — feedback tied to an artifact, the sandbox tied
>   to release notes, the staging gate, scheduled adoption, and the rollout.
>

Audited against the stated product, not against the backlog. Each item says what
is true today, because "missing" and "half-built" need different work.

Legend: **✗** absent · **◐** partial · **⚠** defect in something that exists · **✓** closed

---

## 1. Bring your own everything

The pitch is *your keys, your cloud, your git*. Two of those three are currently
false, and the third is true only for DigitalOcean.

| | | |
|---|---|---|
| G1 | ◐ | **Workers are Claude-only.** `FALLBACK_ORDER` is three Claude models and the worker is built on the Claude Agent SDK. `providers.py` speaks to three vendors but is used only by plan mode and recruiting. "Any agent in any role" is true for *planning*, false for *building*. Weeks of work: an agentic loop with file/shell tools per provider. |
| G2 | ✓ | **No per-role provider or model choice.** The roster is `{role, count, model}` where model is the string `worker` or `lead`, resolved to Claude ids. You cannot say "backend on Gemini, tester on Claude". |
| G3 | ✓ | **Git host is configurable; the dashboard's links are not.** `GIT_API`/`GIT_WEB`/`GIT_PROVIDER` route every API call, clone and credential check, and Gitea's real divergences (merge verb and body key, no `head` filter on list-pulls, `limit` not `per_page`, paged trees) are handled. Still hardcoded: the `github.com` links built in `routes.py` and `dashboard/app.js`, and the k8s launcher does not pass `GIT_WEB` into the worker's env, so k8s workers still clone from github.com. |
| G4 | ◐ | **Cloud means DigitalOcean, but it is now a seam rather than an assumption.** `provider.py` holds the four vendor facts that were spread through `envs.py` and `cloud.py`: which registry, what the pull secret is called, how published tags are listed, and why a preview is never a NodePort (DOKS opens them to 0.0.0.0/0 itself). DigitalOcean is inferred from `DOCR_REGISTRY`, so nothing configured today changes. Still absent: an AWS or GCP implementation, and DO's load-balancer costing is still reasoned about inline in the manifests. |
| G5 | ✗ | **No custom model endpoints.** The provider list is fixed to Anthropic/OpenAI/Google. No Bedrock, Vertex, or self-hosted vLLM — which is the same "bring your own" promise applied to inference. |

## 2. The round table

| | | |
|---|---|---|
| G6 | ✓ | **Blueprint → project handoff is thin.** A blueprint produces a team roster, but personas, provider choices and the manager's character do not carry across. The round table's output is mostly discarded at the moment it matters. |
| G7 | ✓ | **The UI defaults to `diverge`**, the mode the research favours for open-ended work, and the picker now prints what each costs (`N+1` vs `3N+1` calls) next to which one the evidence supports. The `roundtables.mode` column still defaults to `'debate'` on purpose: it is what every recorded table ran as, and changing a column default rewrites what past runs *were*. |
| G8 | ✓ | **Seats cannot be personas from the eventual team.** You argue an idea with strangers, then hire different people to build it. |

## 3. Team, roles and personas

| | | |
|---|---|---|
| G9 | ✓ | **No per-role persona.** Only `manager_persona` exists. `roles.json` carries a summary, not a character. The plan calls for a persona per agent. |
| G10 | ✓ | **Personas cannot be changed later.** No edit path at all, so "it can change personas later" is not possible for the user or the manager. |
| G11 | ✓ | **The user cannot upscale or downscale a role mid-run.** The manager has `reassign_task`; the human has no equivalent control. |
| G12 | ✓ | **No agent identity or continuity.** Every task gets a fresh session. "The backend engineer" is a role label, not a persistent teammate with memory of what they built. |

## 4. The orchestration algorithm — the heart

This is the part described as the core of the work, and it is the least developed
relative to the ambition. Today the manager plans a DAG once and reviews what
comes back; the scheduler dispatches whatever is unblocked.

| | | |
|---|---|---|
| G13 | ✓ | **No work-sharing or rebalancing.** Idle agents stay idle while others are blocked. The scheduler has no notion of capacity, only of dependencies. |
| G14 | ◐ | **No result reuse.** If a tester already verified something, a later task re-does it from scratch. Nothing is memoised across tasks. |
| G15 | ✓ | **Reviewed work is never discussed with teammates.** The manager judges alone. The plan says the reviewed work should be discussed with the team — and the research says the *aggregation* is worth several times more than extra voices, so this is the high-value version of "more agents". |
| G16 | ✓ | **`ask_teammate` consults a model, not a teammate.** It fires a fresh one-shot query with no access to what the actual teammate built. |
| G17 | ✓ | **No process model.** Neither agile nor waterfall is expressed anywhere. There is no backlog, no capacity, no ceremony, no notion of a task being pulled rather than pushed. |
| G18 | ✓ | **The algorithm cannot be tuned without editing code.** Contest width, escalation thresholds, review depth, when to consult — all constants in source. The plan explicitly expects frequent tweaking. |
| G19 | ✓ | **No measurement harness.** There is no way to tell whether a tweak improved anything: no per-run metrics, no comparison between configurations, no record of what was tried. Without this, tuning is guessing. |

## 5. Artifacts and output

| | | |
|---|---|---|
| G20 | ◐ | **Each sprint is frozen once the project leaves it.** `artifacts.py` writes the sprint's task record and the repo's file list into `sprint_artifacts`, so a later cycle reworking a task no longer rewrites what shipped earlier. Captured on read rather than at the sprint boundary, because the boundary lives in the manager session and would miss every project that already ran. Sprints from before this exists have no snapshot and read live, labelled as such. |
| G21 | ◐ | **Release notes per sprint, assembled from the record.** Every line names the task it came from. A model may rewrite them into prose, but the draft is discarded whole if it cites a task the sprint did not contain, and the itemised facts are returned alongside either way. No dashboard surface yet. |
| G22 | ◐ | **Per-teammate view over task artifacts**, not a second ownership model — `/api/projects/{id}/by-agent` groups tasks by `agent_id`, and projects from before teammates had names fall back to grouping by role. No dashboard surface yet. |
| G23 | ◐ | **Markdown deliverables render as documents.** Headings, lists, tables, code blocks, quotes and links, from a renderer written here rather than a dependency the pod cannot install. It treats every file as hostile — agent-authored content from a cloned repo — so nothing reaches the page unescaped and only `http(s)`/`mailto`/relative URLs survive as links. Still no diagrams: a ` ```mermaid ` block is labelled and shown as source, because shipping a drawing library needs a CDN or a build step and there is neither. |
| G24 | ◐ | **One set of shipping mechanics, used by both paths.** `rollout.py` holds content-hash tags, the build that returns no tag on failure, the trial run before promotion, promotion into a named Deployment, and rollback; `deploy.py` and `envs.py` both call it. A user's app is now identified by the code it was built from, is run once with nothing routed to it before it replaces the app that works, and is changed with `set image` rather than a re-applied manifest. Still separate on purpose: `cloud.py`, which patches the Deployment it is running inside from a pod with no kubectl. |
| G25 | ◐ | **Per-branch previews for a user's app.** A branch gets its own image, Deployment, hostname and `devteam/branch` label, beside the deployed app rather than over it — locally on its own port, on the cluster behind the shared ingress. No dashboard surface yet: `/api/projects/{id}/previews`, GET, POST and DELETE. |

## 6. Sprints and the feedback loop

| | | |
|---|---|---|
| G26 | ✓ | **The manager does not decide the sprint count.** The user picks a number up front. The plan says the manager should judge how many are needed. |
| G27 | ✓ | **No end-of-sprint review with the user.** A digest is filed as a GitHub issue; there is no in-app moment where you look at the artifacts and respond. |
| G28 | ✓ | **No scheduled check-ins.** "One-on-ones before running unlimited sprints" has no mechanism. |
| G29 | ◐ | **The manager never elicits requirements.** It plans unilaterally from the brief. It never interviews the client, and at sprint boundaries it decides alone rather than asking what is wanted next. |
| G30 | ◐ | **Feedback has no structured path.** You can send a free-text directive. There is no "here are my notes on this artifact" that ties comment to object. |

## 7. Self-improvement

| | | |
|---|---|---|
| G31 | ✓ | **Nothing is scheduled.** Self-repair runs when a human asks. The plan calls for a daily pass set by root. |
| G32 | ✓ | **Logs are captured, never analysed.** Non-200s and browser errors reach the events table and GitHub issues; nothing aggregates them into candidate work. |
| G33 | ✓ | **Bugs are not artifacts.** A found defect becomes a GitHub issue, not an object the platform holds, triages, prioritises and turns into a sprint. |
| G34 | ✓ | **No prioritisation model.** Nothing decides which bug is worth a sprint. |
| G35 | ◐ | **The mock sandbox is not tied to what changed.** `DEMO_MODE` gives a clickable build; it is not linked to release notes, so "see what changed" is still reading a diff. |
| G36 | ⚠ | **The staging gate is unproven and off.** `REQUIRE_STAGING=0`, and the in-pod suite has never completed — one test hangs on the shared node and I have not found which. |
| G37 | ✗ | **No scheduled adoption.** `AUTO_UPDATE=0`; a verified image waits for a human. |

## 8. Defects in what already exists

| | | |
|---|---|---|
| G38 | ⚠ | **Production is running an image that predates today's fixes** — artifacts rework, `hold`-restart, files API, `SELF_REPO`. |
| G39 | ✓ | **In-memory state lost on restart:** notification dedup (`_SEEN`) and login lockout (`_FAILED`). A crash-looping pod re-notifies; a restart clears a lockout. |
| G40 | ✓ | **Nothing notices if the platform is down.** Notifications are sent *by* the platform, so a crash-loop is silent. Needs external uptime monitoring. |
| G41 | ✓ | **Workspace pruning keeps 8 clones regardless of size.** On a 10Gi volume a few large repos still fill it, and a full volume stops everything. |
| G42 | ✓ | **`envs.promote` assumes one deployment name.** Fine today, wrong the moment there are two apps in a namespace. |

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
