# What every button actually does

The butterfly-effect reference: for each control, the endpoint it calls and
everything that happens downstream — including whether it **spends money**,
**starts an agent**, or **kills one**.

Legend: 💸 spends tokens · 🤖 starts an agent · 🛑 stops an agent ·
🌐 touches GitHub · ⚠️ destructive

---

## Header

| Control | Calls | What actually happens |
|---|---|---|
| **devteam** logo | — | Back to the project list. Resets the URL to `#/`. |
| **Project dropdown** | `GET /projects/{id}/events`, `GET /projects/{id}` | Switches project, clears the feed, replays the last 250 events. |
| **+ New project** | — | Opens the 3-step wizard (nothing is created until step 3). |
| **auth badge** | fed by `GET /health` | Display only: subscription / API key / none. |
| **cost badge** | fed by `GET /projects/{id}` | Subscription: agent runs used. API key: dollars spent vs budget. |
| **status badge** | same | Project status. Hover for the reason (`summary`). |
| **🔔 bell** | `GET /notifications` | Pending manager questions across your projects. Answering one 💸 unblocks that manager immediately. |
| **⚙ settings** | `GET /me`, `POST /settings` | Stores *your* GitHub token and AI credentials. Your agents run on these — never the operator's. |
| **Check** (per credential) 🌐 | `POST /settings/verify` | Makes the smallest real call that proves the credential works: GitHub `/user`, a 1-token Claude message, an OpenAI model list, a Gemini completion on both flash and pro tiers. Checks what you typed, or the stored value if the box is empty. |
| **Save & verify** 🌐 | `POST /settings`, then `POST /settings/verify` per entered key | Saves first, then verifies each credential you entered and shows the verdict inline. Saving succeeds either way — a key can be valid while its provider is having a bad minute. Closes itself only if everything verified. |
| **⏻ logout** | `POST /logout` | Ends the session, reloads. |
| **↻ Restart manager** 💸🤖 | `POST /projects/{id}/restart` | Only for failed/review/cancelled. Sets status `planning` and starts a **fresh manager session**. Existing tasks are kept. |
| **Cancel** ⚠️🛑🌐 | `POST /projects/{id}/cancel` | Sets status `cancelled`, stops the scheduler, aborts the manager session, **kills every running agent** (unpushed work is lost), marks their tasks failed, and closes open GitHub issues. Tells you how many agents it stopped. |

---

## Home

| Control | Calls | What happens |
|---|---|---|
| **🧠 Plan it first** | — | Opens Plan mode — the round table. Nothing is created until you convene. |
| Project row | — | Opens it. |
| **Open** | — | Same. |
| **Cancel** ⚠️🛑 | `POST /projects/{id}/cancel` | Identical to the header Cancel. |
| **↻** 💸🤖 | `POST /projects/{id}/restart` | Identical to the header Restart. No confirmation. |
| **🗑** ⚠️🛑 | `DELETE /projects/{id}` | **Deletes the project for good** — tasks, activity and any running agents. The GitHub repo and its PRs are NOT touched. Cannot be undone. Refused for the platform's own project. |
| Repo link 🌐 | — | Opens GitHub. Does not open the project. |
| `⟲ this platform` tag | — | Marks the row that is devteam itself. |

---

## Command tab

The org chart: you at the top, your manager below, agents beneath.

| Control | Calls | What happens |
|---|---|---|
| **👑 Boss card** | — | Opens your full original request, repo, manager model and autonomy mode. |
| **Manager bubble** | — | Display only — the manager's latest message or thinking, from the live feed. |
| **Agent card** | `GET /tasks/{id}/events` | Opens the task dialog with the full agent transcript. |
| **❗ Needs your attention** banner | — | Appears when the project is in review/failed *and* has a reason. |
| ↳ **Tell the manager to fix it** 💸 | `POST /projects/{id}/directive` | **Sends** a message telling the manager the work is not done. Delivered at its next decision point. |
| **👔 Manager needs a decision** card | — | The manager is paused on `hold` until you answer. |
| ↳ **option buttons / reply** 💸 | `POST /questions/{qid}/answer` | Unblocks the manager with your answer. |
| **Message the manager** (bottom) 💸 | `POST /projects/{id}/directive` | Queued; the manager reads it at its next decision point, not instantly. |

---

## Board tab

Four columns: Planned · In progress (`queued`,`running`) · Review / Needs
attention (`pushed`,`review`,`failed`) · Done. Cards open the task dialog;
issue/PR links go to GitHub.

---

## Dependencies tab

The DAG. Node colour = status; `added` = created mid-project; `⬆sonnet` = the
task escalated to a stronger model. Click a node to open the task.

---

## Artifacts tab

| Control | Calls | What happens |
|---|---|---|
| **▶ Open the demo app** | `/preview/{id}/` | Static files only. **Any call the app makes to its own backend will 404** — that is expected. |
| **▶ Build / ↻ Rebuild demo** | `POST /projects/{id}/preview` | Clones the latest main and syncs the built static files. |
| **🚀 Build & deploy** 💸? | `POST /projects/{id}/deploy` | Builds the latest main and **runs the real app** — backend included. Locally: a subprocess on its own port. In k8s: a Deployment + Service (+ Ingress if `APPS_DOMAIN` is set). Disabled when the project has no server to run. |
| **↻ Rebuild & restart** | same | Fresh checkout, rebuild, restart. |
| **■ Stop** 🛑 | `DELETE /projects/{id}/deploy` | Kills the deployment and frees the port. |
| **Build & runtime log** | — | The actual build output — where a failed deploy explains itself. |
| **A file in the Files list** | `GET /projects/{id}/file` | `.md` renders as a document, everything else keeps its whitespace as code. The document view escapes everything the file contains and drops any link that is not `http(s)`, `mailto:` or relative — the file was written by an agent in a cloned repo, so it is treated as hostile input rather than as content. ` ```mermaid ` blocks are labelled and shown as source; no diagram library is loaded. |

The deployed app gets its **own origin** (a port, or a Service), never a path
under the dashboard — otherwise its absolute `/api/...` calls would hit the
control plane. It also runs with a scrubbed environment: no platform credentials.

---

## Agents tab

| Control | Calls | What happens |
|---|---|---|
| **logs** | `GET /tasks/{id}/machine-logs` | Tails the process or pod log. Scroll position is preserved across refreshes. |
| **■ stop** ⚠️🛑 | `POST /tasks/{id}/kill` | **Kills that agent now.** Its task is marked failed and unpushed work is lost. You can re-run it afterwards. |
| Model health bars | `GET /model-health` | Observed health (healthy/strained/throttled/cooling), *not* a published quota — Anthropic exposes none for subscriptions. Shows exact numbers only with an API key. |

---

## Blockers tab

Everything currently in the way, derived fresh on every read — a blocker that no
longer applies simply stops being listed.

| Control | Calls | What happens |
|---|---|---|
| **Open task #N** | — | The task dialog. |
| **↻ Re-run it** 💸🤖 | `POST /tasks/{id}/retry` | Sets the task back to `planned`; the scheduler dispatches a **new agent**. |
| **Open settings** | — | Opens the settings dialog. |
| **Answer the manager** | — | Switches to Command, where the question card is. |

Severity: *Stopping the project* (critical) · *Slowing it down* (warning).

---

## Self-repair tab (root only, on the devteam project)

| Control | Calls | What happens |
|---|---|---|
| **🔧 Put the team on it** 💸🤖🌐 | `POST /self/issue` | Opens a GitHub issue and hands the brief to the manager, which plans the fix. Work happens on a branch — **nothing reaches the running app until you deploy**. |
| **⬆ Pull & restart** ⚠️ | `POST /self/redeploy` | `git pull` into the live tree and **restarts this process**. Refused if the tree is dirty or agents are mid-run; refused and reverted if the new code fails to import. |
| **↩ Roll back** ⚠️ | `POST /self/rollback` | Resets to the commit recorded before the last deploy and restarts. |

---

## Task dialog

| Control | Calls | What happens |
|---|---|---|
| **↻ Re-run this task** 💸🤖 | `POST /tasks/{id}/retry` | Back to `planned`, dispatched again. `attempts` keeps counting, so this can trigger escalation to a stronger model. |
| **✓ Mark done / skip** ⚠️🛑 | `POST /tasks/{id}/skip` | Marks it `done` so dependents unblock. If an agent is running on it, **that agent is stopped** — otherwise it would keep spending on work already marked finished. |
| **✎ Edit spec / deps** | `POST /tasks/{id}/edit` | Edits title/description/dependencies. A change that would create a dependency cycle is rejected. Hidden while the task is running. |

---

## New-project wizard

1. **Your idea** — name, brief, repo. *Assemble my team* 💸 calls
   `POST /suggest-team`, which reads your brief and proposes a domain-appropriate
   roster. If it fails you get a default team and a note saying so.
2. **Your team** — edit roles, counts and model tier. Custom roles are allowed;
   a role with no prompt file gets a capable generic one.
3. **Go** — autonomy (*keep me in the loop* vs *full autonomy — the manager
   never asks*), the manager's model and character, and caps.
   **🚀 Hire team & start** 💸🤖🌐 creates the project, creates the repo if
   needed, and starts the manager.

---

## Activity feed

| Control | What it shows |
|---|---|
| **Simple** | Plain-language updates only. |
| **Detailed** | Every tool call and thought. |
| **⚙ Decisions** | Only scaling/routing decisions: dispatches with attempt number and model, escalations, reassignments, rate-limit fallbacks, contests, stalls, blocked DAGs. |

---

## Things that are *not* buttons but change behaviour

- **Autonomy mode** (set at creation): *supervised* means the manager asks you
  before big calls; *full autonomy* means it decides everything and only
  interrupts if truly stuck.
- **Agent-run cap** (`max_runs`): the real limit on a subscription. When it is
  reached, dispatch stops.
- **Max agents at once** (`max_workers`): how many workers run in parallel.
- **Two attempts failed** → the task automatically escalates to a stronger
  model. Unless the manager has pinned one, which disables escalation.


---

## Plan mode — the round table

Reached from **🧠 Plan it first** on the home page. Nothing is created until you
convene; nothing is built until you click Build.

| Control | Calls | What happens |
|---|---|---|
| **Seat row** (name / provider / model / persona) | — | Configures one voice. The provider dropdown disables any provider you have no key for. |
| **+ Add a seat** | — | Up to 8. Warns past 6, where deliberation degrades. |
| **✕** on a seat | — | Removes it; refuses below 3. |
| **seat warning** | — | Tells you when your table is homogeneous — the configuration research found does *not* beat asking one model once. |
| **Moderator in the centre** | — | Which model synthesises. It weighs arguments, not head-counts. |
| **Diverge only / Full debate** | — | Diverge (the default) = `N+1` calls (propose → synthesise). Debate = `3N+1` (adds critique + revise), so roughly 3× the money and the wait. Diverge is the shape the evidence supports for open-ended work; debate stress-tests but risks losing facts to consensus pressure. |
| **▶ Convene the table** 💸 | `POST /tables` then `POST /tables/{id}/run` | Runs the chosen mode. A 4-seat table is 5 calls (diverge) or 13 (debate). |
| **🚀 Build this with a team** 💸🤖🌐 | `POST /tables/{id}/build` | Creates a real project whose brief *is* the blueprint (dissent included) and whose roster is the proposed team, then starts the manager. |

Cost note: seats run concurrently within a round, so wall-clock is ~3 rounds,
but you pay for every seat in every round.
