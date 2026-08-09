# knowledge — a devteam fleet service

Instantiated from `templates/service/` (P1). The rules are the repo-root
`SERVICE_CONTRACT.md`; this file is the service's own story.

## What it does

What an agent has learned, stored so it can be found again: cue/says rows with
embedded vectors, blended-score retrieval (similarity + term overlap + track
record + recency), reinforcement, and a per-owner prune. Owns `data/knowledge.db`
alone. On first boot it copies the conductor's legacy rows (devteam.db's
`knowledge` — or `knowledge_legacy` after the shim's rename) over a read-only
ATTACH, exactly once.

## How to run it

```
python tools/gen_fleet.py
./run-local.sh                              # process-compose boots it with the fleet
```

Standalone (debugging): `SERVICE_TOKEN=dev DB_PATH=/tmp/k.db python app.py`.

## The contract

- `GET /health` — readiness JSON `{ok, service, db, checks}` (db opens, table reads)
- `GET /openapi.json` — the committed contract. After changing routes:
  `python app.py --spec > openapi.json`, commit it, let oasdiff judge the diff.
- `POST /recall` `{owner, query, k?, kind?, include_global?, settings?}` → `{hits}`
- `POST /remember` `{owner, cue, says, kind?, sig?, payload?, good?, bad?, settings?}` → `{id}`
- `POST /reinforce` `{id, outcome}` → `{ok}`
- `POST /forget` `{owner, row_id?, sig?}` → `{removed}`
- `GET /stats?owner=` → `{rows, total, backends}`
- `POST /tokens` `{text}` → `{tokens}` — the tokenizer as contract, so the
  lifeworld's leak-checks and recall agree on what a word is
- Everything beyond /health and /openapi.json requires `X-Service-Token`
  (constant-time). The embedding key (`settings.openai_api_key`) rides each
  request body and is never stored — this service holds no model credentials.

## Tests

`pytest services/knowledge/tests` — offline: smoke (in-process TestClient) +
Schemathesis against the committed spec over ASGI.

## Degraded mode (every CLIENT documents this)

The one caller is the conductor's client, `conductor/app/knowledge.py` — since the
P1 cutover a pure client with no in-process fallback, so the conductor requires
`KNOWLEDGE_URL` and refuses to boot without it. Latency budget and per-verb
degraded shapes are documented there: recall → `[]` with a deduped warning (a
sprint never blocks), remember/reinforce/forget → no-op `0`, stats →
`{"total": 0, "degraded": true}`, tokens → `[]` with a log line, and `health()` →
`false`, which is what turns the module graph's knowledge card red.

## UI

Headless (`ui: false`) — an API-only store, honestly.
