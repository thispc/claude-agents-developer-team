# devteam

Describe an idea. A **manager** agent plans it, hires a team, and dispatches
**worker** agents that clone your repo, work on branches, open PRs, and iterate
until it ships. You watch it happen, answer questions, and stop anything at any
time.

The bet: many cheap-model iterations, orchestrated well, beat one expensive
model with a human babysitting it.

Not limited to software — the roster is generated from your brief, so a rocket
blueprint or a research review gets a rocket or research team, not a backend
engineer.

---

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install -r conductor/requirements.txt
cp .env.example .env          # then fill in GITHUB_TOKEN and, optionally, an AI key
set -a && source .env && set +a          # required: there is no dotenv loader
PYTHONPATH=conductor .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>, sign in as `root` (password from `ROOT_PASSWORD`,
default `devteam` — change it), add your credentials in ⚙ Settings, and create a
project.

**Authentication for agents**, in precedence order:

1. `ANTHROPIC_API_KEY` — pay-per-token, real money.
2. `CLAUDE_CODE_OAUTH_TOKEN` — runs on a Claude Pro/Max plan (`claude setup-token`).
3. The host's `claude` CLI login — whatever you are already signed in as.

Each user supplies their own in Settings. A normal user's agents **never** fall
back to the operator's credentials.

---

## Documentation

| Doc | What's in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it works: the four moving parts, the life of a task, model selection, the database, credential isolation |
| [docs/UI_ACTIONS.md](docs/UI_ACTIONS.md) | What every button actually triggers — including which ones spend money, start an agent, or kill one |
| [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) | Confirmed bugs that are **not** fixed, each with the fix |
| [deploy/README.md](deploy/README.md) | Local kind rehearsal cluster, the DigitalOcean differences, and what it costs |
| [docs/ROUNDTABLE_DESIGN.md](docs/ROUNDTABLE_DESIGN.md) | Plan mode: why the circle is arranged the way it is, with citations |

---

## What it does

- **Deliberates first (optional)** — Plan mode seats 3–6 *different* models
  (Claude, GPT, Gemini) around a circle with a moderator in the middle. They
  propose independently, then argue, then revise, and the moderator writes a
  blueprint — including the strongest objection nobody answered. Worth knowing:
  debate does **not** reliably beat one good model on closed-form benchmarks, so
  this is a deliberate spend on plan *quality*, not a free accuracy win — see
  [docs/ROUNDTABLE_DESIGN.md](docs/ROUNDTABLE_DESIGN.md) for the numbers and the
  honest case for it.
- **Plans** — the manager turns your brief into a task DAG with real dependencies.
- **Hires** — a roster suggested from the brief, which you edit before starting.
- **Dispatches** — a deterministic scheduler runs every task whose dependencies
  are done. This costs **no tokens**: the manager writes rows, code does the rest.
- **Escalates** — two failed attempts move a task to a stronger model; a
  rate-limited model is routed around automatically.
- **Competes** — optionally run 2–3 rivals on the same task and let the manager
  pick the winner.
- **Collaborates** — workers hand off notes to their successors and can call
  `ask_teammate` to consult a stronger model instead of grinding alone.
- **Ships** — PRs open automatically; the manager reviews, requests changes, merges.
- **Runs it** — one click builds and runs the actual app (backend included),
  locally or as a pod on a cluster.
- **Fixes itself** — devteam appears in its own project list; raise an issue
  against it and the team fixes the platform you are using.

## Safety rails

- **Agent-run cap** (`max_runs`, default 40) — the primary limit, and the only
  one that means anything on a subscription. Dispatch stops when it is reached.
- **Budget cap** (`budget_usd`) — real only with an API key. On a subscription
  no tokens are billed, so recorded cost is 0 and this rail is inert by design.
- **Max parallel workers** per project, and an optional per-role cap.
- **Turn caps** — `WORKER_MAX_TURNS` (120), `WORKER_MAX_TURNS_RETRY` (180),
  `LEAD_MAX_TURNS` (120).
- **Stall watchdog** — a task with no activity for `WORKER_STUCK_SECONDS`
  (30 min) is failed and retried.
- **Startup sweep** — a local worker cannot outlive the conductor, so anything
  still marked running at boot is released.
- **Kill switches** — stop one agent from the Agents tab, or cancel the project
  to stop all of them.

## Repo layout

| Path | What's there |
|---|---|
| `conductor/app/` | FastAPI server: routes, manager, scheduler, launcher, deploy, auth, db |
| `worker/worker.py` | The worker agent: clone → work → push → report |
| `agents/` | Role prompts (`manager.md`, `backend.md`, `frontend.md`, `tester.md`) and `roles.json`. Unknown roles get a capable generic prompt |
| `dashboard/` | The UI. Vanilla JS, no build step |
| `deploy/` | Dockerfiles, k8s manifests, the kind rehearsal cluster |
| `docs/` | Architecture, UI reference, known issues |

## The manager's tools

`create_tasks` · `add_tasks` · `status` · `wait` · `ask_boss` · `get_report` ·
`request_changes` · `compare_work` · `pick_winner` · `reassign_task` ·
`accept_task` · `merge_pr` · `finish`

Note what is *not* there: the manager cannot dispatch a worker or open a PR.
Both happen automatically in the scheduler, so orchestration costs no tokens and
a manager that dies mid-project loses no work.

## Running on Kubernetes

`LAUNCHER=k8s` runs each worker as a Job. Deploying a project's *app* is chosen
per request and is independent of that setting.

Rehearse locally first — same code path, no cloud bill:

```bash
brew install kind
./deploy/kind-up.sh
```

See [deploy/README.md](deploy/README.md) for the DigitalOcean differences
(image architecture, registry, and why one shared ingress instead of a load
balancer per app) and for sizing.

## Configuration

`.env.example` lists every variable with a comment. The ones that matter most:
`ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`, `GITHUB_TOKEN`, `LAUNCHER`,
`MAX_AGENT_RUNS`, `MAX_CONCURRENT_WORKERS`, `WORKER_MODEL`, `ESCALATION_MODEL`,
`APPS_DOMAIN`, `DEPLOY_REGISTRY`.

There is no dotenv loader — `source` the file, or use `deploy/docker-compose.yml`.
