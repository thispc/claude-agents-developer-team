# usage — a devteam fleet service

Instantiated from `templates/service/` (P2). The rules are the repo-root
`SERVICE_CONTRACT.md`; this file is the service's own story.

## What it does

One rolling meter of every model call this box makes, whoever makes it — the
self-repair crew, the project manager, workers, Studio seats. Each row carries a
SOURCE, so the meter can answer the only two questions the crew really has: is
anyone else using the subscription right now (contention → yield to the human),
and how much of this window is gone (utilization → don't hit the wall). Measured
in TOKENS, with cache reads counted separately, because on a subscription there
is no bill and a dollar figure answers no question the owner has.

Owns `data/usage.db` alone. On first boot it expands the conductor's legacy kv
blobs into rows over a read-only ATTACH, exactly once: `usage:ledger` (the pre-P2
meter) and `repair:ledger` (the crew's own call counter, cost-only rows imported
as calls). The second used to be the conductor's job, run from `repair.loop`
behind its own flag; the cutover folded it in here so there is one importer, one
marker, and no way to double-count.

**Why it is a table.** The meter used to be one kv blob rewritten whole on every
note — read the list, append, write it back, under a *thread* lock. A thread lock
is not a process lock, so the moment the platform became a fleet a lost update
became possible on the exact number that authorises spending someone else's
quota. `usage_rows` takes one INSERT per call. Killing that hazard is the point
of the extraction; the process boundary is what stops anyone reaching back in.

## How to run it

```
python tools/gen_fleet.py
./run-local.sh                              # process-compose boots it with the fleet
```

Standalone (debugging): `SERVICE_TOKEN=dev DB_PATH=/tmp/u.db python app.py`.

## The contract

- `GET /health` — readiness JSON `{ok, service, db, checks}` (db opens, table
  reads). Deliberately not gated on reaching the conductor: a probe that goes red
  because a peer restarted is how a fleet takes itself down in a ring.
- `GET /openapi.json` — the committed contract. After changing routes:
  `python app.py --spec > openapi.json`, commit it, let oasdiff judge the diff.
- `POST /note` `{source, model?, tok?, cache?, usd?, calls?, ts?}` → `{id}`
- `GET /snapshot?now=` → the whole utilization picture (window, budget, used,
  owner vs repair, allowance, quiet seconds, contention, by-source)
- `GET /verdict?now=` → `{ok, why, wake}` — the utilization half of the crew's
  sleep decision
- `GET /rows?since=` → `{rows}`
- Everything beyond /health and /openapi.json requires `X-Service-Token`
  (constant-time). No model credential enters this service's env.

**`source` is required and never guessed.** No contextvar, no default, no
opinion: the conductor resolves attribution at its own call site
(`providers.complete` gained an explicit `source` argument) and the wire always
carries a literal. A meter that guesses who spent can bill the crew's own
footsteps to the owner and put the crew to sleep forever.

## The knobs it runs on

Four dials, all the owner's, read from the conductor over
`GET /internal/tuning?name=` with this service's token and cached ~30s:
`usage_window_h`, `usage_budget_tokens`, `repair_idle_share`,
`repair_yield_quiet_s`. `services.yaml` declares both the door (`doors: [tuning]`)
and the exact knobs (`knobs: [...]`); anything outside that list is a 403.

When the conductor is unreachable the service keeps **the last value it actually
saw** rather than falling back to its baked default — stale-but-real beats
default-but-wrong, because a default could silently re-widen an allowance the
owner had narrowed. The defaults are only for a service that has never once
reached the conductor, and a drill asserts they still match `tuning.KNOBS`.

## Tests

`pytest services/usage/tests` — offline: smoke (in-process TestClient, the knob
hop answered by a stub transport) + Schemathesis against the committed spec.

## Degraded mode (every CLIENT documents this)

The one caller is the conductor's client, `conductor/app/usage.py` — since the P2
cutover a pure client with no in-process fallback, so the conductor requires
`USAGE_URL` and refuses to boot without it. `note` is
dropped with a deduped warn (metering must never break the thing being metered),
`rows` is `[]`, `snapshot` is every number zero plus `degraded: true`, and
`verdict` **fails safe**: `(False, "usage meter unreachable", now + 300)`. That
one verb refuses instead of shrugging, because "I cannot see the quota" and "the
quota is free" are opposite answers and only one is safe to act on. The wake is
bounded at five minutes so a flapping meter costs the crew minutes, not a night —
and the crew resumes on `pc start usage` with no restart.

## UI

Headless (`ui: false`) — an API-only meter, honestly.
