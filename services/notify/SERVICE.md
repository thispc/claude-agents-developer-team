# notify — a devteam fleet service

Instantiated from `templates/service/` (P2). The rules are the repo-root
`SERVICE_CONTRACT.md`; this file is the service's own story.

## What it does

Getting word out when nobody is looking at the dashboard. A platform fault — a
crashed manager session, a provider returning 500s, a JavaScript error in the
browser — becomes a GitHub issue, which is a channel that already pushes to your
email and phone. Two rules make it survivable rather than a spam machine:

- **One issue per distinct fault.** A crash loop produces the same fingerprint a
  thousand times and should produce one issue with a count.
- **A ceiling per hour.** An unanticipated failure mode ends in silence, not an
  unbounded write loop against a token that can also push code.

Owns `data/notify.db` alone: `notify_seen` (one row per fingerprint, counted with
`SET count=count+1` rather than read-then-written) and `notify_sent` (one row per
issue actually filed, which is what the hourly ceiling counts). On first boot it
copies the conductor's dedup memory (`notify_seen:*` in devteam.db) over a
read-only ATTACH — without that, the extraction itself would be a notification
storm, since every already-filed fault would look new again.

## How to run it

```
python tools/gen_fleet.py
./run-local.sh                              # process-compose boots it with the fleet
```

Standalone (debugging): `SERVICE_TOKEN=dev DB_PATH=/tmp/n.db python app.py`.

## The contract

- `GET /health` — readiness JSON `{ok, service, db, checks}` (db opens, table
  reads). Not gated on reaching GitHub: an unreachable git host is a degraded
  notifier, not a dead process, and it says so in the answer instead.
- `GET /openapi.json` — the committed contract. After changing routes:
  `python app.py --spec > openapi.json`, commit it, let oasdiff judge the diff.
- `POST /error` `{kind, detail, context?, repo?}` → `{sent, issue?|reason, count?}`
  — the deduplicated, throttled path
- `POST /issue` `{repo, title, body}` → `{sent, issue?|reason}` — the generic
  door: text somebody else composed, filed as-is. No fingerprint (two sprints
  with the same headline are two sprints), but the ceiling still applies, because
  the ceiling exists to protect the token rather than the inbox.
- `GET /status` → `{enabled, max_per_hour, sent_last_hour, distinct_faults}`
- `POST /forget` `{fingerprint?}` → `{removed}` — blank forgets everything
- Everything beyond /health and /openapi.json requires `X-Service-Token`.

## The credential, and what deliberately stayed behind

This is the **first service beyond the conductor to hold one**: `GITHUB_TOKEN`
arrives in its env because the GitHub call itself moved here, declared as
`env: [GITHUB_TOKEN, …]` in `services.yaml` and named in `SERVICE_CONTRACT.md`
rule 4 as the contract's one exception. No model credential ever follows it.

Two things stayed in the conductor on purpose:

- **the repo**, derived from the git remote — conductor knowledge, so it rides
  each request rather than becoming a second place to get it wrong;
- **`sprint_digest`**, which is a JOIN over projects and tasks. It composes its
  text conductor-side and posts the finished issue through `POST /issue`.

A filed issue is announced back through the conductor's `POST /internal/bus`
(`doors: [bus]`) — the events table keeps its single writer. That call is
best-effort: an issue that was filed but not announced is a notification that
worked.

## Tests

`pytest services/notify/tests` — offline: smoke (in-process TestClient, the git
host monkeypatched) + Schemathesis against the committed spec. The contract run
stubs the GitHub call at module scope, because "offline" has to be structural
rather than a fixture somebody can forget to request.

## Degraded mode (every CLIENT documents this)

The one caller is the conductor's client, `conductor/app/notify.py`. Every verb
answers `{"sent": false, "reason": "notify service down"}` with a deduped warn;
`status()` adds `degraded: true` so the UI can tell "nothing went wrong" from
"the notifier is broken", and `forget()` returns 0. Silence is this module's
designed failure mode and it is the right one: a notifier that raises takes down
the thing it was reporting on. The daily self-check completes without it.

## UI

Headless (`ui: false`) — it speaks in GitHub issues.
