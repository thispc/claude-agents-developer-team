# lifeworld — a devteam fleet service

The P4 extraction, and the plan's highest-risk phase. The rules are the repo-root
`SERVICE_CONTRACT.md`; this file is the service's own story.

## What it does

A small society of living agents — the substrate behind the Studio canvas and the
self-repair crew. Humans and artifacts share one atom (input → pod → output that
rewrites the pod), they live in Scenes, time is counted in LLM scans, and the one
law is absolute: **the model proposes, the code disposes**. Everything free is
free by construction; the single thing that can spend is one bounded deliberation
call, gated by attention. A whole Lifeworld idles at zero.

Owns `data/lifeworld.db` alone — one `lw_worlds` table, one JSON blob per world,
saved and loaded whole because a world is a playground read and rewritten per
scan, not queried column by column. On first boot it copies the conductor's
legacy rows (`devteam.db`'s `lw_worlds`, or `lw_worlds_legacy` after the shim's
rename) over a read-only ATTACH, exactly once, **preserving the row ids** —
every world id in the platform is a pointer at one (`repair:world`,
`graph:pool:0`, a project's team).

## The layout

```
app.py          the Studio surface: /worlds/… , one-for-one with /api/lw/…
crew.py         the self-repair crew's verbs — whole behaviours, never accessors
manifest.py     a team as one declarative spec, and the function that builds it
caller.py       who is asking: the conductor's stamp, and the ownership gate
store.py        persistence + the per-world asyncio lock
substrate/      the engine (25 files) — unchanged from conductor/app/lifeworld
                except ports.py, its ONE door upward, which is now a set of clients
helpers.py      vendored per service: token check, WAL sqlite, tiny kv
```

## How to run it

```
python tools/gen_fleet.py
./run-local.sh                              # process-compose boots it with the fleet
```

Standalone (debugging): `SERVICE_TOKEN=dev DB_PATH=/tmp/lw.db python app.py`.

## The dependency inversion

`substrate/ports.py` was always the substrate's only reach upward. What changed is
the other side of the door:

| accessor | before | now |
|---|---|---|
| `providers()` | `from .. import providers` | conductor `POST /internal/complete` |
| `tuning()` | `from .. import tuning` | conductor `GET /internal/tuning` (4 knobs) |
| `agents()` | `from .. import agents` | conductor `/internal/agents/…` |
| `knowledge()` | `from .. import knowledge` | the knowledge SERVICE, directly |
| `db()` | `from .. import db` | gone — `store.py` owns `data/lifeworld.db` |

**No credential is in this process.** A live world carries a `settings_ref` — a
short SIGNED string naming a principal, minted by `auth.mint_settings_ref` — in
the exact place the settings dict used to sit. The substrate passes it along and
cannot read it; only the conductor's model door resolves one, and a forged one
resolves to nothing. That invariant is what the whole phase is judged on.

One consequence, stated plainly: a lifeworld recall uses knowledge's **free local
backend**, because the embedding key is a credential and stays behind. Knowledge
re-embeds a row locally when the backends differ, so lessons written by the
conductor with a real embedder are still found — coarser, never absent. The
conductor's own recalls (the specialist briefing, the knowledge row written after
an outcome) keep the key and happen conductor-side, on purpose.

## The contract

- `GET /health` — readiness JSON `{ok, service, db, checks}` (db opens, table reads)
- `GET /openapi.json` — the committed contract. After changing routes:
  `python app.py --spec > openapi.json`, commit it, let oasdiff judge the diff.

**The Studio surface** — 35 routes under `/worlds`, one-for-one with the
conductor's `/api/lw/*`, which the dashboard hardcodes and which therefore must
never move. Worlds, agents, artifacts, rooms, threads (connect / update / refine /
chat / run / results), the manifest, the two verbs (`act`, `round`), and the agent
detail panel.

**The crew's verbs** — each one a WHOLE BEHAVIOUR, because each is a
read-modify-write on a world blob and the lock that guards those can only be held
on this side of the wire:

- `POST /worlds/{id}/crew-seating` `{factors, manager, protocol, scene_name, current_room_id}`
  → `{world_id, room_id, thread_id, agents:{factor:human_id}, outcome}`.
  `outcome` is `adopted` when a surviving room already seated exactly these
  personas — **the ids are kept**, because everything the crew has learned is
  keyed to them.
- `POST /worlds/{id}/crew-context` → who is building, and the association it has proved
- `POST /worlds/{id}/crew-decision` → `{decision_id, sig}`
- `POST /worlds/{id}/crew-outcome` → `{sig, saw}` for the conductor's knowledge write
- `POST /worlds/{id}/crew-consult` → a neighbour answers, or a machine-readable refusal
- `POST /worlds/{id}/crew-review` → a neighbour verdicts a green diff
- `POST /worlds/{id}/crew-deliberate` → the sprint memo, plus what it cost to make
- `POST /worlds/{id}/crew-chat-note`, `GET /worlds/{id}/crew-usage`
- `GET /worlds/{id}/room/{rid}/members`, `GET /rooms` — staffing pools for the module graph

Everything beyond `/health` and `/openapi.json` requires `X-Service-Token`
(constant-time) **and** the conductor's caller stamp (`X-Lw-Owner` and friends —
see `caller.py`). Ownership is enforced here, against the `owner_id` column,
because that column is here.

## Tests

`pytest services/lifeworld/tests` — offline: smoke (in-process TestClient) +
Schemathesis against the committed spec over ASGI. The conductor-side drills
(the crew loop end to end, the adoption/id-preservation heal across the process
boundary, every degraded shape, rollback mode) live in
`tests/test_lifeworld_service.py`.

## Degraded mode (every CLIENT documents this)

The one caller is `conductor/app/lifeworld_client.py`, which documents each shape:
`/api/lw/*` → an honest 503; `seat_crew` → `None`, so `repair.ensure_team` returns
`None` and the sprint tick logs and sleeps with the reason **"lifeworld down"**
(pausing is the honest behaviour — a crew without its specialists would still be
spending, just anonymously) on a bounded 60-second wake, so it resumes the moment
the service answers, with no restart and no lost sprint; a consult or review
declines and the build carries on alone; a decision or outcome is simply not
recorded; room members → `None`, so the Atlas room panel and the assignment pool
read "unavailable" rather than "empty".

## UI

Headless (`ui: false`) — the Studio canvas is its face, served same-origin by the
conductor at `/api/lw/*`, which is the one thing this extraction was not allowed
to change.
