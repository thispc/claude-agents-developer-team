# How the network actually works

Four different shapes run the same code, and they are genuinely different. The
short answer to "is the sandbox a mini Kubernetes cluster?" is **no** — it is one
extra process on your laptop. Only previews and production are Kubernetes.

| | Where it runs | Isolation | Reached by |
|---|---|---|---|
| **Local dev** | a process on your Mac | none — your real DB, your real keys | `localhost:8000` |
| **Sandbox** | a *second* process on your Mac | own DB file, no credentials | `localhost:8700` |
| **Preview** | a k8s namespace on DOKS | own namespace + volume, no credentials | node IP + NodePort |
| **Production** | the `devteam` namespace on DOKS | own volume, holds the real Secret | node IP + NodePort |

---

## The one thing worth understanding first

**A worker is not a container.** With `LAUNCHER=local` — which is what runs today,
locally and on DOKS — a worker is a **child process of the conductor**, started
with `subprocess.Popen`. It shares the conductor's filesystem and its memory
limit.

So on DOKS the conductor pod contains: the FastAPI server, the manager's Claude
session, and every worker agent, all in one container.

```
┌─ pod: devteam-conductor ──────────────────┐
│  uvicorn (FastAPI)  :8000                 │
│  manager session    (in-process)          │
│  worker  ──► subprocess ──► claude CLI    │
│  worker  ──► subprocess ──► claude CLI    │
│                                           │
│  /data  ── PersistentVolumeClaim          │
│     devteam.db                            │
│     workspaces/task-12-a1/repo   ← clones │
└───────────────────────────────────────────┘
```

That is why the pod has a 3 GiB memory limit and why `MAX_CONCURRENT_WORKERS`
matters: three agents each running a Node CLI and a git clone inside one
container is the actual constraint. `LAUNCHER=k8s` exists to make each worker its
own Job instead, and that is the change to make when one pod stops being enough.

---

## Local development

```
browser ──► localhost:8000 ──► uvicorn (your machine)
                                 │
                                 ├─ devteam.db          (repo root)
                                 ├─ workspaces/…        (repo root)
                                 └─ subprocess workers ─► api.anthropic.com
                                                       ─► github.com
```

The dashboard is **read from disk on every request**, but the Python was loaded
once at startup. That gap is the whole reason for the "app is half-updated"
banner: edit both and the page runs ahead of the server.

## Sandbox — a process, not a cluster

`Try it` copies the tree to `.sandbox/tree` and starts **a second uvicorn** on
`:8700`. No Docker, no Kubernetes, nothing to schedule.

```
browser ──► localhost:8700 ──► uvicorn (.sandbox/tree)
                                 │
                                 ├─ .sandbox/sandbox.db      ← fresh, seeded
                                 ├─ HOME=.sandbox/home       ← no keychain
                                 └─ DEMO_MODE=1: no workers spawn at all
```

Four isolations, all enforced by the **parent** process through the child's
environment — never by code inside the candidate, because that code is the thing
under test:

- every secret blanked (**blanked**, not unset: the child inherits `os.environ`,
  so an unset variable is your real one)
- `HOME` redirected, so `_has_cli_login()` cannot find your macOS keychain
- its own SQLite file
- `DEMO_MODE=1`, so `dispatch_task` is intercepted before credentials are read

Nothing leaves the machine. No agent runs, no GitHub call is made.

## Preview on DOKS — this one *is* Kubernetes

```
you ──► http://<node-ip>:3xxxx  (NodePort)
              │
              ▼
        ┌─ namespace: devteam-first ─────────┐
        │  Service (NodePort)                │
        │  Deployment ─► pod                 │
        │  PVC (own volume, own DB)          │
        │  Secret docr-creds (pull only)     │
        └────────────────────────────────────┘
```

Its own namespace, its own volume, `DEMO_MODE=1` and **no application Secret** —
so it cannot run an agent or reach GitHub even if it wanted to. The separate
volume is the load-bearing part: a bad migration destroys a throwaway database
rather than your projects, and a destructive migration is exactly what a preview
exists to catch.

## Production on DOKS

Same shape, two differences: it mounts `devteam-secrets`, and it is not in demo
mode. That is the entire distinction between "a preview" and "the real thing".

```
        ┌─ namespace: devteam ───────────────┐
        │  Deployment ─► pod                 │
        │     envFrom: devteam-secrets  ◄────┼── the real credentials
        │     workers as subprocesses        │
        │  PVC 10Gi ─► devteam.db, workspaces│
        │  Service NodePort 30080            │
        └────────────────────────────────────┘
```

**`replicas: 1` and `strategy: Recreate` are not conservatism.** SQLite sits on a
ReadWriteOnce volume, so a second replica would fight over the file and a rolling
update would deadlock — the new pod cannot mount the volume until the old one
releases it, and the old one will not terminate until the new one is ready. It
hangs with no error.

---

## Why NodePort and not a LoadBalancer

DigitalOcean bills **every** LoadBalancer (~$12/month). One per preview is how a
credit disappears into networking rather than compute. NodePort reaches the same
pod through the node's existing public IP for nothing.

The cost of that choice: no TLS, no friendly hostname, and the port is open to
the whole internet. Fine for a preview full of fake data. **Not fine for
production**, which now holds a real Secret — before relying on it, either put a
DigitalOcean firewall in front limiting the port to your IP, or add an Ingress
with TLS once there is a domain (one LB, shared by every environment, which is
the point at which an LB starts being worth its price).

---

## Which way traffic actually flows

Every arrow is outbound from the conductor. Nothing dials in except your browser.

```
conductor ──► api.anthropic.com      manager session + worker agents
conductor ──► github.com             clone, push, PRs, issues
conductor ──► generativelanguage…    only if a round-table seat is Gemini
worker    ──► conductor              POST /internal/report, X-Worker-Token
```

That last one is the only inbound path that is not you, and it is why
`WORKER_TOKEN` matters: anyone holding it can post a report as any worker. On
DOKS the worker reaches the conductor over `localhost` inside the same pod, so it
never crosses the network at all.

## Secrets

`deploy/k8s/make-secret.sh` builds the Secret straight from `.env` — no
filled-in `secrets.yaml` is ever written, because a plaintext copy of every
credential in the repo directory is the easiest way to commit one by accident.

It deliberately does **not** copy `DIGITALOCEAN_API_TOKEN`. That token can create
and destroy clusters; the conductor never needs it, so the cluster does not get
to hold it.
