# watch — a devteam fleet service

Instantiated from `templates/service/` (P3). The rules are the repo-root
`SERVICE_CONTRACT.md`; this file is the service's own story.

## What it does

Two things that were always one idea: the **log ring** (every fact the backend
has to say about itself) and the **monitor** (what those facts add up to).
Nobody reads 3000 log rows; the value of keeping them is that something else can
turn them into a short list of notices a person would want to know, each with the
evidence behind it.

Owns `data/watch.db` alone:

- `log_rows` — the ring, capped at 3000 by insertion order
- `error_rows` — capped at 300, so a chatty hour cannot push out the one row
  anyone actually goes looking for
- `decisions` — one row per fingerprint: what the human said about a notice

Before P3 all of that was **four kv values in the conductor's database**, and the
ring was rewritten whole on every single line: read the list, append, write it
back, under a thread lock. A thread lock is not a process lock. The moment the
platform became a fleet, two processes logging at the same instant meant one of
them silently vanished — on the record you go to precisely when you are trying to
find out what happened. Here a log line is one INSERT.

**Dedupe lives here because the ring does.** A loop that ticks every 20 seconds
and finds the same thing wrong writes the same line 180 times an hour. A repeat
inside `dedupe_s` bumps a counter on the stored row — so two processes hitting
the same fault collapse into one line with a count of two, instead of two lines
that each look like a single incident.

On first boot it copies the conductor's four legacy keys (`logs:ring`,
`logs:errors`, `monitor:decisions`, `monitor:auto`) over a read-only ATTACH. The
decisions are why that is not optional: without them the extraction itself would
be a notice storm, every already-dismissed notice looking new again.

## The split: this service owns no lever

Every action a notice can propose — pause the crew, abort the task in flight,
nudge a knob, file a bug on the backlog — targets machinery that lives in the
conductor. So the judgment half stayed there:

| here (facts + detection)            | conductor (judgment + action)             |
|-------------------------------------|-------------------------------------------|
| the two rings, capped               | `monitor.ACTIONS`, `approve()`, `sweep()` |
| the seven **log-derived** rules      | `AUTO_SAFE`                               |
| notices, derived on read            | `_rule_queue`, `_rule_stuck` (local rules) |
| the decisions store + `auto`        | `/api/logs/notices` — the composition      |

`/api/logs/notices` is a **composition**: this service's notices plus the
conductor's two local rules, merged into one list, deduped by fingerprint, sorted
once. `approve(fp)` resolves the notice from either source, runs the action
conductor-side, and only then POSTs to `/notices/{fp}/decide`. The order matters:
a decide that fails after a successful action is a notice you get asked about
again — never an action that did not happen.

The two local rules stayed because their evidence is not a log row: one reads
`repair:queue`, one reads `repair.state()`. That is also what erased the
platform's last cross-owner kv read — before P3 the monitor opened another
module's key, and moving it here would have made that a second *process* opening
another's database.

**No doors.** This is the only extracted service that asks the conductor for
nothing at all: no `bus`, no `tuning`. One rule used to quote the current
`repair_max_turns` in its prose; it says the same thing without the number, and
detection stays a pure function of log rows.

## How to run it

```
python tools/gen_fleet.py
./run-local.sh                              # process-compose boots it with the fleet
```

Standalone (debugging): `SERVICE_TOKEN=dev DB_PATH=/tmp/w.db python app.py`.

## The contract

- `GET /health` — readiness JSON `{ok, service, db, checks}` (db opens, both the
  ring and the decisions table read).
- `GET /openapi.json` — the committed contract. After changing routes:
  `python app.py --spec > openapi.json`, commit it, let oasdiff judge the diff.
- `POST /logs` `{rows: [...]}` → `{stored, deduped}` — **a batch**, never a row.
  The client is on the platform's hottest path and may not pay a round-trip per
  line. A row's six named fields are typed; every other key the caller sends
  rides along untyped, because that is the shape of a log row.
- `GET /logs?level=&cat=&event=&q=&since=&limit=&errors_only=` →
  `{logs, categories, levels}`. `level` is a **floor**.
- `GET /logs/stats?window_s=&now=` → counts by category and level, plus the last
  error and the category vocabulary.
- `GET /notices?window_s=&include_decided=` →
  `{notices, summary, decisions, auto}`. The last two ride along so the conductor
  can finish the list on its own side in **one** round-trip: it filters its local
  notices against the same decisions map, and the panel polls.
- `POST /notices/{fp}/decide` `{state, note?}` → the stored decision.
- `GET /auto` → `{auto}`; `POST /auto` `{on}` → `{auto}`. The standing decision
  lives beside the individual ones because it is one. It is *acted on* in the
  conductor.
- `GET /summary?window_s=` → this service's own counts. The screen's badge counts
  the composed list and the conductor computes that itself.
- Everything beyond `/health` and `/openapi.json` requires `X-Service-Token`.
- Unknown query parameters are **refused**, not ignored: `?levl=warn` silently
  returning every row reads as "no warnings", which is the most expensive kind of
  silence a log filter can produce.

## Tests

`pytest services/watch/tests` — offline: smoke (in-process TestClient) +
Schemathesis against the committed spec. The conductor's side of the seam — the
batching shim's latency budget, the composition, approve-here-decide-there — is
in `tests/test_watch_service.py`.

## Degraded mode (every CLIENT documents this)

Two clients, both in the conductor.

**`conductor/app/logs.py`** — the hot path, 64 fire-and-forget call sites, some
inside exception handlers, some in a 20s tick. It never does I/O from a log call:

- the **stdout echo stays local**, so the 3am terminal survives this service
  being down (process-compose collects it into `data/logs/fleet.log`);
- rows go on an in-memory queue and a daemon thread posts them in batches of ≤100
  or every 500ms, whichever comes first;
- beyond 1000 queued rows the newest are dropped with **one** stderr note per
  window — never through `logs.*` itself, which would recurse;
- an outage holds the queued rows and recovery sends them;
- reads (`recent`/`rows`/`stats`) flush first, so you always read your own writes;
- degraded: reads are `[]`, stats are zeros with `degraded: true`, and the route
  carries a banner so an empty log view cannot be misread as a quiet system.
- **`logs.log()` returns in under a millisecond, always** — `LOG_CALL_BUDGET_S`,
  timed in `tests/test_watch_service.py` with the service unreachable.

**`conductor/app/monitor.py`** — degraded, the notice list falls back to the two
local rules and says so (`degraded: true` plus a `banner` the screen shows above
the list). Approving a local notice still works: the action runs locally and only
the decision is lost, so the notice comes back. `auto_on()` reads `false` when it
cannot ask, so nothing runs unattended on a permission nobody could read.

The module graph's **ops** card goes red when this service is unreachable, by the
same argument the knowledge card does: a card reporting green while every row
written to it is dropped is a card nobody should believe.

## UI

Headless (`ui: false`) — the Improve screen's Notices and Activity tabs are its
face, served same-origin by the conductor.
