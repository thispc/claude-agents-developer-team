# <name> — a devteam fleet service

Instantiated from `templates/service/`. The rules are the repo-root
`SERVICE_CONTRACT.md`; this file is the service's own story.

## What it does

_Fill in: the one-sentence job this service does, and the data it owns._

## How to run it

```
cp -r templates/service services/<name>     # once
# add a block to services.yaml (kind: service, port, cmd, db), then:
python tools/gen_fleet.py
./run-local.sh                              # process-compose boots it with the fleet
```

Standalone (debugging): `SERVICE_TOKEN=dev DB_PATH=/tmp/x.db python app.py`.

## The contract

- `GET /health` — readiness JSON `{ok, service, db, checks}`
- `GET /openapi.json` — the committed contract. After changing routes:
  `python app.py --spec > openapi.json`, commit it, let oasdiff judge the diff.
- Everything else requires `X-Service-Token` (constant-time; the conductor's
  /svc gateway adds it server-side).

## Tests

`pytest tests/` — offline: smoke (in-process TestClient) + Schemathesis against
the committed spec over ASGI.

## Degraded mode (every CLIENT documents this)

_Fill in: what each caller does when this service is down. The contract requires
an answer per caller — "recall returns [] and never blocks a sprint" is the
canonical shape._

## UI (optional)

`ui/panel.html` is served at `/ui/*` and same-origin at `/svc/<name>/ui/` when
the services.yaml entry says `ui: true`. Delete `ui/` for a headless service.
