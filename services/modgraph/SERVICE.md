# modgraph — a devteam fleet service

The P5 extraction, and the last one before the Atlas is rewired. The rules are the
repo-root `SERVICE_CONTRACT.md`; this file is the service's own story.

## What it does

Holds every project as a graph of verified modules, starting with this one: a
**plan** is an immutable version — aim node → GROUP nodes (the architecture
layers) → their module children → conclusion node — where each node carries its
spec, its boundary manifest (paths), its test suite and its own agent/model
config, and each edge is TYPED and carries the contract both sides honour.

A replan never edits rows: it writes a new plan and marks the old one superseded,
because a mutable plan makes *what did we believe when this was built*
unanswerable the moment anyone improves it. Node **keys** stay stable across
versions, so assignments and positions survive a replan; the trace and the
planner-authored test source are append-only, because they are evidence.

Owns `data/modgraph.db` alone:

- `graph_plans` — the immutable versions; the only write after creation is status
- `graph_nodes`, `graph_edges` — the two levels and the contracts between them
- `graph_node_runs` — the append-only TRACE: every build and verify, closed once
- `graph_node_tests` — which suite belongs to which module, and how it last went
- `graph_assign` — per-node steering (agent, model, autonomy), mutable on purpose
- kv `graph:pos:{plan_id}` — the one cosmetic fact, kept out of the immutable rows

On first boot it copies the conductor's six tables (and the layouts keyed to their
plan ids) over a read-only ATTACH, once, **preserving the row ids** — a plan id is
a pointer held outside these tables.

## The layout

```
app.py     the HTTP surface: plans, nodes, edges, runs, tests, assignment, positions
store.py   the six tables, the layout kv, and the first-boot copy
derive.py  the three computed answers: the group tier, affected tests, mastery
seed.py    the offline manifest, read from the working tree (REPO_ROOT)
helpers.py vendored per service: token check, WAL sqlite, tiny kv
```

## How to run it

```
python tools/gen_fleet.py
./run-local.sh                              # process-compose boots it with the fleet
```

Standalone (debugging):
`SERVICE_TOKEN=dev DB_PATH=/tmp/mg.db REPO_ROOT=$PWD python app.py`.

## What did NOT move, and why

**`modgraph_author` stayed in the conductor.** It is the crew's hidden manager
authoring the plan its specialists will work, and it needs `providers.complete`
with the owner's resolved settings, the repair engine's headroom meter and
ledger, the crew's live roster, a tuning knob and the bus. That is the conductor,
five times over. It is a BRAIN; this is a STORE. **This is where the cycle
breaks:** `modgraph_author ↔ repair` was the one genuine import cycle in the whole
decomposition, and P5 cut it by putting a wire in the middle — with the store as
the end that must not reach back. Nothing in this directory imports the
conductor, and a grep test in the smoke suite says so.

**The affected-tests runner stayed in the conductor.** `/api/graph/self/verify`
shells out to the repo's own pytest over real files in the checkout the conductor
is serving from. This service says WHICH files (`GET /plans/{id}/affected`) and
stores the verdict (`POST /plans/{id}/test-result`); it runs nothing. A store that
spawned pytest in another process's working tree would have imported that tree's
whole world.

**`routes/graph.py` stayed the BFF.** Every `/api/graph/*` path is unchanged — the
Atlas hardcodes them — and the payload is still composed there, from this
service's rows plus facts only the conductor has: who an agent id is (the crew's
record), what the crew is touching right now, the cluster switch, the probes.

**The probes stayed too — and P6 supersedes them.** `modgraph_health`'s per-module
`PROBES` and its `SERVICES` honest-switch table import the very modules they
check, so they could not come; they are conductor-resident by nature. P6 deletes
them outright, replacing both with process-compose's live state (readiness plus a
real `GET /health`, and Start/Stop through the fleet's REST API). What survives
P6 is what is already service-independent: `health_of`, `rollup`, `tests_state`
— and `mastery`, which came HERE, because its JOIN is over two tables that are
both this service's, and a join you can still write is a join that was inside the
boundary.

## Reading the working tree

`seed.py` opens source files: module docstrings become node specs, and the test
mapping is parsed out of the suite's real imports. That is not a breach of the
isolation rule — the rule is one process per database and no imports across
directories, and both hold. It reads the tree the way a linter does. What it must
never do is EXECUTE any of it or shell into the checkout, which is exactly why
the verify runner stayed behind. `REPO_ROOT` is env-only like everything else and
defaults to the process's cwd, which is the repo root under process-compose.

**P6 replaces the content of `seed.py`, not its shape.** Its tables name the
platform's CODE modules; the next phase makes the nodes the FLEET's services,
read from `services.yaml` and live process state (`seed_fleet_graph`). The
builder, the manifest comparison and idempotence-by-dict-equality all survive
that.

## The contract

- `GET /health` — readiness JSON `{ok, service, db, checks, backfilled}`.
  `backfilled` is not a readiness condition — a box with nothing to copy is
  perfectly healthy — it is the CONDUCTOR's signal that its own six tables are
  safe to drop.
- `GET /openapi.json` — the committed contract. After changing routes:
  `python app.py --spec > openapi.json`, commit it, let oasdiff judge the diff.

Plans (`/plans`, `/plans/active`, `/plans/{id}`, `/plans/{id}/activate`,
`/plans/import`, `/plans/{id}/manifest`), nodes and edges, the trace
(`POST /runs`, `PATCH /runs/{id}`, `GET /plans/{id}/runs`), tests
(`/plans/{id}/tests`, `/test-result`, `/affected`), assignment
(`/plans/{id}/assigns`, `/assign`), positions, and the three derivations
(`/derive-group-edges`, `/mastery`, `/seed` + `/manifest` + `/tests-for-nodes`).

Two of those are shapes the in-process module never had, and both exist because
the boundary changed what is expensive or what is safe:

- **`GET /plans/{id}/assigns`** — every assignment in one call. In-process,
  `get_assign` per node was a dict lookup in a loop; over a wire it is a round
  trip per node on a payload the Atlas polls, and fourteen of those per poll is a
  latency regression dressed up as a faithful port.
- **`POST /plans/import`** — a whole plan version in one call and one
  transaction. Both bulk writers (the manager's authoring pass, and the operator
  removing a node) used to write fifty-odd rows in one process, where a failure
  halfway left a draft nobody had activated. Over a wire that becomes fifty
  chances to be interrupted with the plan already ACTIVE and half its edges
  missing. A plan is authored as one thing, so it is written as one.

Everything beyond `/health` and `/openapi.json` requires `X-Service-Token`,
checked in constant time. **No doors, no peers, no extra env** — this service asks
the conductor for nothing, which is the shape that broke the cycle.

## Degraded mode (every CLIENT documents this)

The one caller is `conductor/app/modgraph.py`. **A trace gap is recorded as a gap,
never as a block**, and that is the deliberate opposite of P4's call: when the
lifeworld is down the crew PAUSES, because a crew without its specialists would
still be spending, just anonymously. When this service is down the crew keeps
building — the code still gets written, the tests still run, the commit still
lands. All that is lost is the record of which module it happened on, and a
platform that stopped improving itself because its map was unavailable would have
the priority exactly backwards. **The graph is observability, not the substrate.**

| verb | when it is down |
|---|---|
| `note_run` / `close_run` | `0` / no-op with one deduped warn. Falsy means "no run to close", which the build hook already handles |
| `update_test_result` | `0` rows touched — the verify still ran and still answered |
| `nodes` / `edges` / `tests` / `runs` / `positions` | `[]` / `{}` and `degraded()` true, so `/api/graph/self` answers 200 with an honest "graph unavailable" instead of drawing an empty repository |
| `active_plan` / `get_plan` | `None`, with `degraded()` telling the BFF that means "cannot see the plan", not "there is no plan" |
| `assigns` / `get_assign` | `{}` / `None` — a node reads unassigned rather than wrongly assigned |
| `mastery` | **`None`, not `{}`** — the authoring pass's "a master keeps its module" rule reads this, and an outage that looked like "nobody has earned anything" would let one reshuffle undo every earned continuity on the box |
| `self_manifest` | `{}`, which is how the authoring pass refuses to spend a model call on a plan with nowhere to land |
| `seed_self_graph` | `0` with a log; boot carries on and the next boot seeds |
| `affected_tests` | `[]`, so a verify answers "no tests are mapped" — the same 400 a node with no suite already gives |

## Tests

`pytest services/modgraph/tests` — offline, no sockets:

- `test_modgraph_smoke.py` — the contract, the token, the no-conductor-import
  grep, the seed's idempotence, immutable versions, the one-transaction import,
  the reconciled tier, affected-only selection, mastery across plan versions, and
  the first-boot copy with the ids preserved
- `test_modgraph_contract.py` — Schemathesis against the **committed** spec over ASGI

What stays conductor-side is the seam and the screen: `tests/test_module_graph.py`
(the seed against the real tree, the payload), `tests/test_module_runtime.py` and
`tests/test_graph_author.py` (the authoring brain, which did not move), and
`tests/test_modgraph_service.py` — every endpoint's auth, the derivation across
the wire, every degraded shape, and the drill that matters most: **the crew keeps
building with this service stopped.**

## UI

Headless (`ui: false`) — the Atlas is its face, served same-origin by the
conductor at `/api/graph/*`, which is the one thing this extraction was not
allowed to change.
