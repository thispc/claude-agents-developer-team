# devteam — an autonomous AI software development team

A self-hosted platform where a **Lead agent** (Claude Sonnet 5) plans a software project,
files GitHub issues, and dispatches **worker agents** (Claude Haiku 4.5 — backend,
frontend, tester) that run as Kubernetes Jobs. Workers clone the repo, implement their
task on a branch, push, and report back; the lead reviews, requests changes, opens and
merges PRs, and iterates until the brief is shipped. You watch everything on a live
dashboard.

The bet: many cheap-model iterations orchestrated well, running unattended for hours,
can match what an expensive model + a human-in-the-loop does in fewer shots — at a
fraction of the cost.

```
 you ──▶ dashboard ──▶ conductor (FastAPI, SQLite, WebSocket)
                          │  runs Lead agent (Sonnet 5) with team tools:
                          │  create_tasks · dispatch · wait · get_report ·
                          │  open_pr · request_changes · merge_pr · finish
                          │
                          ├──▶ GitHub (issues, PRs, merges)
                          │
                          └──▶ k8s Jobs (or local subprocesses)
                                 worker pod = headless Claude Code session (Haiku 4.5)
                                 clone → code → verify → push branch → report
```

## Repo layout

| Path | What |
|---|---|
| `conductor/` | FastAPI control plane: REST + WebSocket API, SQLite, lead agent, launchers |
| `worker/` | Worker entrypoint run in each Job/subprocess |
| `agents/` | Role system prompts: `lead.md`, `backend.md`, `frontend.md`, `tester.md` |
| `dashboard/` | Zero-build web UI (kanban + live agent feed + cost meter) |
| `deploy/` | Dockerfiles, docker-compose, DOKS manifests |

## Safety rails (why this won't eat your wallet)

- Per-project **budget cap** — the lead is told to wrap up when cost ≥ budget, and
  `dispatch` refuses new workers past it.
- **Max parallel workers** per project (`dispatch` enforces it).
- **Turn caps**: workers ≤ `WORKER_MAX_TURNS` (40), lead ≤ `LEAD_MAX_TURNS` (120).
- k8s Jobs get `activeDeadlineSeconds: 3600`, `backoffLimit: 0`, auto-cleanup.
- **Model escalation**: a task that fails twice re-runs on `ESCALATION_MODEL` (Sonnet 5).
- Only the conductor calls the GitHub API; workers get a repo-scoped token only to
  clone/push. The `wait` tool blocks without burning tokens.

## Run it locally (no cloud, no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r conductor/requirements.txt -r worker/requirements.txt
npm install -g @anthropic-ai/claude-code        # the Agent SDK drives this CLI

cp .env.example .env                             # fill ANTHROPIC_API_KEY (+ GitHub)
set -a && source .env && set +a
PYTHONPATH=conductor uvicorn app.main:app --port 8000
```

Open http://localhost:8000 → **New project** → write a brief, e.g.:

> Build a URL shortener. Python FastAPI backend in `api/` with POST /shorten and
> GET /{code} (redirect), SQLite storage. Static frontend in `web/` with a form that
> calls the API and shows the short link. Done = both run locally and a tester has
> verified shorten+redirect round-trip.

Set a small budget ($2–5) for the first run. Watch the lead plan, workers stream their
tool calls, and PRs appear on the repo.

### With docker-compose

```bash
cp .env.example .env   # fill values
docker compose -f deploy/docker-compose.yml up --build
```

## Deploy on DigitalOcean Kubernetes (DOKS)

```bash
# 1. Cluster (control plane is free) + registry
doctl kubernetes cluster create devteam --count 2 --size s-2vcpu-4gb --region blr1
doctl registry create YOUR_REGISTRY
doctl registry kubernetes-manifest | kubectl apply -f -   # pull secret

# 2. Build & push images
docker build -f deploy/Dockerfile.conductor -t registry.digitalocean.com/YOUR_REGISTRY/devteam-conductor:latest .
docker build -f deploy/Dockerfile.worker    -t registry.digitalocean.com/YOUR_REGISTRY/devteam-worker:latest .
doctl registry login
docker push registry.digitalocean.com/YOUR_REGISTRY/devteam-conductor:latest
docker push registry.digitalocean.com/YOUR_REGISTRY/devteam-worker:latest

# 3. Deploy
kubectl apply -f deploy/k8s/namespace.yaml
cp deploy/k8s/secrets.example.yaml deploy/k8s/secrets.yaml   # fill values
kubectl apply -f deploy/k8s/secrets.yaml
kubectl apply -f deploy/k8s/rbac.yaml
# edit deploy/k8s/conductor.yaml → set YOUR_REGISTRY, then:
kubectl apply -f deploy/k8s/conductor.yaml

kubectl -n devteam get svc devteam-dashboard   # EXTERNAL-IP → open in browser
```

Each dispatched task becomes a Job: `kubectl -n devteam get jobs -w`. "Autoscaling" v1
is conductor-driven Job creation bounded by `max_workers`; add DOKS cluster autoscaling
on the node pool if you want nodes to scale too.

## Budget plan ($140 DO credit, 10 days)

| Item | Rate | 10 days |
|---|---|---|
| DOKS control plane | free | $0 |
| 2 × s-2vcpu-4gb nodes | ~$24/mo each | ~$16 |
| Load balancer (optional — port-forward is free) | ~$12/mo | ~$4 |
| Container registry (starter) | free tier | $0 |
| **DO total** | | **~$20 of $140** |

Headroom: you could run 3 nodes + LB for the full 10 days and still use barely $30.
Anthropic tokens bill separately to your API key: a small project run
(lead Sonnet 5 + 3–6 Haiku worker runs) typically lands around **$0.50–$3**; the
per-project budget cap is the hard stop.

## Config reference

See `.env.example`. Key ones: `LEAD_MODEL` / `WORKER_MODEL` / `ESCALATION_MODEL`,
`LAUNCHER` (`local` | `k8s`), `MAX_CONCURRENT_WORKERS`, `PROJECT_BUDGET_USD`,
`GITHUB_TOKEN` (fine-grained PAT: Contents RW, Issues RW, Pull requests RW),
`GITHUB_REPO` (default `owner/repo` target).

## How a project flows

1. You submit a brief → conductor starts a Lead session (Sonnet 5).
2. Lead `create_tasks` → tasks in DB + GitHub issues.
3. Lead `dispatch` → worker Jobs (Haiku 4.5) clone, code, verify, push `task/N` branches, report.
4. Lead `wait` (token-free blocking) → reads reports → `open_pr` / `request_changes` (≤2 rounds, then model escalation).
5. Lead merges prerequisites before dependent tasks, dispatches the tester, and `finish`es with a summary.
6. Everything streams to the dashboard; every artifact lives on GitHub.
