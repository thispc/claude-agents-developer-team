"""The modgraph service — every project as a graph of verified modules, starting
with this one.

The P5 extraction: the six `graph_*` tables, every store helper that touched
them, and the three derivations that read them now live in one process behind one
port, owning `data/modgraph.db` alone.

WHAT A PLAN IS. An immutable version — aim node → GROUP nodes (the architecture
layers) → their module children → conclusion node — where each node carries its
spec, its boundary manifest (paths), its test suite and its own agent/model
config, and each edge is TYPED and carries the contract the two sides actually
honour. Two levels, like an architecture diagram you can zoom. The graph is the
work model, not a diagram of it: verification selects tests from it, the crew's
live activity is matched onto it, and mastery is counted from its trace.

THE CYCLE BREAKS HERE. `modgraph_author` — the crew's hidden manager authoring
the plan its specialists will work — did not come with the tables, and that is
the whole point of the phase. It needs providers, the repair engine's headroom
and the crew's roster, so it STAYED in the conductor and became a client of this
service. Authorship is a brain; this is a store. **Nothing in this directory
imports the conductor** (there is a grep test), and the store has stayed
model-free since it was written — the no-LLM-in-the-scheduler verdict.

THREE THINGS THAT DELIBERATELY DID NOT MOVE:

  the verify runner   `/api/graph/self/verify` shells out to the repo's own
                      pytest over real files in the conductor's checkout. This
                      service says WHICH files (`/plans/{id}/affected`) and
                      stores the verdict (`/plans/{id}/test-result`); it never
                      runs anything.
  the BFF             `routes/graph.py` keeps every `/api/graph/*` path — the
                      Atlas hardcodes them — and keeps composing the payload from
                      this store, the crew's record and the probes.
  the probes          `modgraph_health`'s per-module PROBES and SERVICES tables
                      stay conductor-side: they import the very modules they
                      check. P6 deletes them outright and reads process-compose's
                      live state instead. `health_of`/`rollup`/`tests_state` are
                      pure functions over this service's answers and stayed with
                      the payload that shapes them; `mastery` came here, because
                      its JOIN is over two tables that are both this service's.

DEGRADED IS A GAP, NEVER A BLOCK. The graph is OBSERVABILITY, not substrate — the
contrast with P4 is deliberate and it is the one judgement call of this phase.
When the lifeworld is down the crew pauses, because a crew without its
specialists would still be spending, just anonymously. When THIS service is down
the crew keeps building: the code still gets written, the tests still run, the
commit still lands. All that is lost is the record of which module it happened
on, and a platform that stopped improving itself because its map was unavailable
would have the priority exactly backwards. See `conductor/app/modgraph.py` for
every degraded shape, and `tests/test_modgraph_service.py` for the drill.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))       # so `uvicorn app:app` works from any cwd
import helpers                      # vendored per service — never imported across services
import derive                       # the three computed answers
import seed                         # the offline manifest, read from the working tree
import store                        # this service's own persistence (data/modgraph.db)

SERVICE = os.environ.get("SERVICE_NAME", HERE.name)
SPEC = HERE / "openapi.json"
# The conductor's monolith store, for the one-time first-boot copy. Relative to
# the cwd process-compose runs from (the repo root) — same convention as DB_PATH.
LEGACY_DB_PATH = Path(os.environ.get("LEGACY_DB_PATH", "devteam.db"))


class StrictBody(BaseModel):
    """Strict validation: the contract says integer, so "5" is a 422, not a
    quiet coercion — lax mode is how a spec and a service drift apart."""
    model_config = ConfigDict(strict=True)


def _json_int(v):
    """JSON Schema's `integer` means "a number with zero fractional part" — 24.0
    qualifies — while Python's strict mode wants the int type itself. The
    contract is the spec, so meet ITS semantics exactly: integral numbers pass,
    strings and booleans stay rejected."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


JsonInt = Annotated[int, BeforeValidator(_json_int)]
OptInt = Annotated[int | None, BeforeValidator(_json_int)]

# A node key is a slug: the authoring pass caps it at 40 characters and the seed's
# longest is nine. The ceiling is generous, and it exists so the contract is
# NEGATIVELY testable — an unbounded string leaves the fuzzer nothing invalid to
# send, which reads as "this operation validates nothing" and, here, was true.
NODE_KEY_MAX = 200


def no_stray_query(request: Request) -> None:
    """Refuse a query parameter this operation does not declare.

    FastAPI ignores unknown query params by default, and for a browser-facing app
    that is right — a stray `?utm_source=` must not 422 a page. This is not that:
    the only caller is one client in one repository, so an undeclared parameter is
    either a typo (`node-key` for `node_key`, silently ignored, filter quietly
    doing nothing) or a caller talking to a contract this service does not have.

    It is also what makes the contract NEGATIVELY testable. The spec says which
    parameters exist; a service that accepts others is not implementing the spec,
    and the fuzzer is right to call that a failure rather than a shrug."""
    route = request.scope.get("route")
    dependant = getattr(route, "dependant", None)
    declared = {p.alias for p in getattr(dependant, "query_params", [])}
    stray = sorted(set(request.query_params) - declared)
    if stray:
        raise HTTPException(422, f"unknown query parameter(s): {', '.join(stray)}"
                                 f" — this operation takes {sorted(declared) or 'none'}")


@contextmanager
def _conflict_is_409():
    """A duplicate node key or edge is a CONFLICT, not a crash.

    In-process this raised, and a raise was the right answer: plan rows are
    immutable, so writing the same key twice is a caller bug. Across a wire the
    same raise is a 500 — the service telling a caller that the service is broken,
    when the request was. 409 says which, and the UNIQUE indexes stay the thing
    that decides it, rather than a read-then-write race dressed up as a check."""
    try:
        yield
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, f"that row already exists in this plan ({e})")


# --- bodies -------------------------------------------------------------------

class NewPlan(StrictBody):
    project_id: JsonInt = 0
    kind: str = "template"
    authored_by: str = "seed"
    notes: str = ""


class NewNode(StrictBody):
    key: str
    title: str = ""
    node_type: str = "code"
    spec: str = ""
    join_mode: str = "all_of"
    parent_key: str = ""
    tags: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)


class NewEdge(StrictBody):
    src_key: str
    dst_key: str
    edge_type: str = "depends"
    contract: dict = Field(default_factory=dict)
    contract_test: str = ""


class NodeIn(StrictBody):
    """A node in a whole-plan import. Typed rather than a bare dict, because a
    `dict` in the contract means "any JSON object" and the fuzzer is right to send
    one: `n["key"]` on an object without one is a KeyError, and a KeyError reaches
    a caller as a 500 for what is really a malformed request."""
    key: str
    title: str = ""
    node_type: str = "code"
    spec: str = ""
    join_mode: str = "all_of"
    parent_key: str = ""
    tags: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)


class EdgeIn(StrictBody):
    """An edge, in the shape the derivation and the import both speak: `src`/`dst`
    rather than the rows' `src_key`/`dst_key`, because that is the shape the
    manifest has always used and the seed compares against."""
    src: str
    dst: str
    edge_type: str = "depends"
    contract: dict = Field(default_factory=dict)
    contract_test: str = ""


class TestIn(StrictBody):
    node: str
    path: str
    kind: str = "suite"
    source: str = ""
    status: str = "mapped"


class AssignIn(StrictBody):
    agent_id: OptInt = None
    home_id: OptInt = None
    model: str | None = None
    autonomy: str | None = None


class NewRun(StrictBody):
    plan_id: JsonInt
    node_key: str
    kind: str = "build"
    task_id: OptInt = None
    agent_id: OptInt = None
    status: str = "running"
    detail: str = ""


class CloseRun(StrictBody):
    status: str = "ok"
    detail: str = ""


class NewTest(StrictBody):
    node_key: str
    path: str
    kind: str = "suite"
    source: str = ""
    status: str = "mapped"


class TestResult(StrictBody):
    path: str
    status: str
    last_result: str = ""


class Assign(StrictBody):
    node_key: str
    agent_id: OptInt = None       # None = leave alone; 0/'' = clear
    home_id: OptInt = None
    model: str | None = None
    autonomy: str | None = None


class Positions(StrictBody):
    """A pair per key. The store drops junk anyway — absent-means-unset only stays
    true if nothing writes a fabricated coordinate — but the schema is the first
    line, and the two together are why [0,0] can only mean "someone put it there"."""
    positions: dict[str, list[float]] = Field(default_factory=dict)


class DeriveBody(StrictBody):
    """The pure derivation, over the wire. `parent_of` is {child key: group key}."""
    child_edges: list[EdgeIn] = Field(default_factory=list)
    parent_of: dict[str, str] = Field(default_factory=dict)
    group_edges: list[EdgeIn] | None = None


class TestsForNodes(StrictBody):
    """{node key: boundary paths} → which test files exercise which node. The
    manager authors boundaries; it never authors which tests exist."""
    by_key: dict[str, list[str]] = Field(default_factory=dict)


class ImportPlan(StrictBody):
    """A whole plan version, written in one transaction — see store.import_plan."""
    project_id: JsonInt = 0
    kind: str = "template"
    authored_by: str = "manager"
    notes: str = ""
    nodes: list[NodeIn] = Field(default_factory=list)
    edges: list[EdgeIn] = Field(default_factory=list)
    tests: list[TestIn] = Field(default_factory=list)
    assigns: dict[str, AssignIn] = Field(default_factory=dict)
    positions: dict[str, list[float]] = Field(default_factory=dict)
    activate: bool = True


# --- boot ---------------------------------------------------------------------

store.init_store()
store.backfill_from_legacy(LEGACY_DB_PATH)

# openapi_url=None: FastAPI must not serve a live-generated spec at the contract's
# address — the committed file is the contract, and drift between the two is a
# thing the contract tests exist to catch.
app = FastAPI(title=SERVICE, openapi_url=None)

# Everything beyond /health and /openapi.json requires the service's own token,
# declared once on the router rather than per route — a gate you have to remember
# to add is a gate that will be missing from the endpoint added at 3am.
AUTH = [Depends(helpers.require_token), Depends(no_stray_query)]
# Declared once, on the router every verb hangs off, because a contract that omits
# the answers a caller is most likely to get is not a contract — and the fuzzer
# checks it literally. 401 is the token gate; 400 is what the framework itself
# returns for a body that is not JSON at all (before any model sees it), and 422
# for one that is JSON but the wrong shape.
COMMON = {401: {"description": "no X-Service-Token, or the wrong one"},
          400: {"description": "the request body could not be parsed as JSON"},
          409: {"description": "that node key or edge already exists in this plan — "
                               "plan rows are immutable, so a duplicate is a "
                               "conflict and never an overwrite"},
          422: {"description": "a body or query parameter the contract does not "
                               "declare, or one of the wrong shape"}}
r = APIRouter(dependencies=AUTH, responses=COMMON)


@app.get("/health")
def health() -> dict:
    """Readiness, not liveness: ok only when the service could actually answer — its own
    database opens and the plans table reads.

    `backfilled` rides alongside rather than inside `checks`, because it is not a
    readiness condition: a box with no legacy database to copy from is perfectly
    healthy. It is the conductor's signal that its own six `graph_*` tables are
    finally safe to drop — see store.settled(), and note that dropping them early
    would lose the TRACE, and mastery is computed from the trace.
    """
    def _table_ok() -> bool:
        try:
            helpers.db().execute("SELECT COUNT(*) FROM graph_plans").fetchone()
            return True
        except Exception:
            return False
    checks = {"db": helpers.db_ok(), "table": _table_ok()}
    return {"ok": all(checks.values()), "service": SERVICE,
            "db": str(helpers.DB_PATH), "checks": checks,
            "backfilled": store.settled()}


@app.get("/openapi.json")
def openapi_spec() -> JSONResponse:
    """The committed contract. Regenerate after changing routes:
    `python app.py --spec > openapi.json` — and let oasdiff judge the diff."""
    return JSONResponse(json.loads(SPEC.read_text()))


# --- plans --------------------------------------------------------------------

@r.get("/plans/active")
def plan_active(project_id: JsonInt = Query(0)) -> dict:
    """The one plan on the wall for a project, or null. `null` is a real answer —
    a box that has never seeded has no plan, and inventing one would be worse."""
    return {"plan": store.active_plan(project_id)}


@r.post("/plans")
def plan_create(body: NewPlan) -> dict:
    return {"plan_id": store.create_plan(body.project_id, kind=body.kind,
                                         authored_by=body.authored_by, notes=body.notes)}


@r.get("/plans/{plan_id}")
def plan_get(plan_id: JsonInt) -> dict:
    return {"plan": store.get_plan(plan_id)}


@r.post("/plans/{plan_id}/activate")
def plan_activate(plan_id: JsonInt) -> dict:
    store.activate(plan_id)
    return {"ok": True, "plan": store.get_plan(plan_id)}


@r.post("/import-plan")
def plan_import(body: ImportPlan) -> dict:
    """A whole plan version in one call and one transaction. The two bulk writers
    — the manager's authoring pass and the operator removing a node — use this
    instead of fifty round trips that could each be the one that fails.

    NOT under `/plans/`: every literal there collides with `GET /plans/{plan_id}`,
    which would answer a wrong-method request with 422 ("import is not an integer")
    where the contract promises 405. `/plans/active` gets away with it because it
    IS a GET; this one is not."""
    with _conflict_is_409():
        return {"plan": store.import_plan(
            body.project_id, kind=body.kind, authored_by=body.authored_by,
            notes=body.notes,
            nodes_in=[n.model_dump() for n in body.nodes],
            edges_in=[e.model_dump() for e in body.edges],
            tests_in=[t.model_dump() for t in body.tests],
            assigns_in={k: a.model_dump() for k, a in body.assigns.items()},
            positions_in=body.positions, activate_it=bool(body.activate))}


@r.get("/plans/{plan_id}/manifest")
def plan_manifest(plan_id: JsonInt) -> dict:
    """The stored plan re-shaped for comparison with the tree's own manifest —
    the authoring pass's "did anything actually change" test, which is dict
    equality and nothing cleverer."""
    return seed.manifest_of(plan_id)


# --- nodes & edges ------------------------------------------------------------

@r.get("/plans/{plan_id}/nodes")
def plan_nodes(plan_id: JsonInt) -> dict:
    return {"nodes": store.nodes(plan_id)}


@r.post("/plans/{plan_id}/nodes")
def plan_add_node(plan_id: JsonInt, body: NewNode) -> dict:
    with _conflict_is_409():
        return {"id": store.add_node(plan_id, body.key, body.title,
                                     node_type=body.node_type, spec=body.spec,
                                     join_mode=body.join_mode,
                                     parent_key=body.parent_key, tags=body.tags,
                                     paths=body.paths)}


@r.get("/plans/{plan_id}/edges")
def plan_edges(plan_id: JsonInt) -> dict:
    return {"edges": store.edges(plan_id)}


@r.post("/plans/{plan_id}/edges")
def plan_add_edge(plan_id: JsonInt, body: NewEdge) -> dict:
    with _conflict_is_409():
        return {"id": store.add_edge(plan_id, body.src_key, body.dst_key,
                                     edge_type=body.edge_type, contract=body.contract,
                                     contract_test=body.contract_test)}


# --- the trace ----------------------------------------------------------------

@r.post("/runs")
def run_open(body: NewRun) -> dict:
    """One appended trace row per attempt at a node. The two callers are the
    self-repair engine's build/verify hooks and the BFF's verify route."""
    return {"id": store.note_run(body.plan_id, body.node_key, body.kind,
                                 task_id=body.task_id, agent_id=body.agent_id,
                                 status=body.status, detail=body.detail)}


@r.patch("/runs/{run_id}")
def run_close(run_id: JsonInt, body: CloseRun) -> dict:
    store.close_run(run_id, body.status, body.detail)
    return {"ok": True, "id": run_id}


@r.get("/plans/{plan_id}/runs")
def plan_runs(plan_id: JsonInt, node_key: str = Query("", max_length=NODE_KEY_MAX),
              limit: JsonInt = Query(50, ge=1, le=500)) -> dict:
    return {"runs": store.runs(plan_id, node_key, limit)}


# --- tests --------------------------------------------------------------------

@r.get("/plans/{plan_id}/tests")
def plan_tests(plan_id: JsonInt, node_key: str = Query("", max_length=NODE_KEY_MAX)) -> dict:
    return {"tests": store.tests(plan_id, node_key)}


@r.post("/plans/{plan_id}/tests")
def plan_map_test(plan_id: JsonInt, body: NewTest) -> dict:
    return {"id": store.map_test(plan_id, body.node_key, body.path, kind=body.kind,
                                 source=body.source, status=body.status)}


@r.post("/plans/{plan_id}/test-result")
def plan_test_result(plan_id: JsonInt, body: TestResult) -> dict:
    """What a run of this file said, recorded everywhere the file is mapped. The
    run itself happened in the conductor's checkout; this is only the verdict."""
    return {"updated": store.update_test_result(plan_id, body.path, body.status,
                                                body.last_result)}


@r.get("/plans/{plan_id}/affected")
def plan_affected(plan_id: JsonInt, node_key: str = Query("", max_length=NODE_KEY_MAX)) -> dict:
    """The files a change to this node obliges the conductor to run."""
    return {"files": derive.affected_tests(plan_id, node_key)}


# --- assignment ---------------------------------------------------------------

@r.get("/plans/{plan_id}/assigns")
def plan_assigns(plan_id: JsonInt) -> dict:
    """Every assignment on the plan, by node key — one call for the payload and
    the engine's node matcher, which both want all of them."""
    return {"assigns": store.assigns(plan_id)}


@r.get("/plans/{plan_id}/assign")
def plan_assign_get(plan_id: JsonInt, node_key: str = Query("", max_length=NODE_KEY_MAX)) -> dict:
    return {"assign": store.get_assign(plan_id, node_key)}


@r.post("/plans/{plan_id}/assign")
def plan_assign_set(plan_id: JsonInt, body: Assign) -> dict:
    return {"assign": store.set_assign(plan_id, body.node_key, agent_id=body.agent_id,
                                       home_id=body.home_id, model=body.model,
                                       autonomy=body.autonomy)}


# --- positions ----------------------------------------------------------------

@r.get("/plans/{plan_id}/positions")
def plan_positions(plan_id: JsonInt) -> dict:
    return {"positions": store.positions(plan_id)}


@r.post("/plans/{plan_id}/positions")
def plan_save_positions(plan_id: JsonInt, body: Positions) -> dict:
    return {"positions": store.save_positions(plan_id, body.positions)}


# --- derivation ---------------------------------------------------------------

@r.post("/derive-group-edges")
def derive_group_edges(body: DeriveBody) -> dict:
    """The top tier, reconciled. Pure: no rows are read and none are written —
    the BFF hands in the stored child edges and the stored authored tier and gets
    back the arrows that are actually backed by a crossing."""
    return {"edges": derive.derive_group_edges(
        [e.model_dump() for e in body.child_edges], body.parent_of,
        group_edges=None if body.group_edges is None
        else [e.model_dump() for e in body.group_edges])}


@r.get("/mastery")
def mastery(project_id: JsonInt = Query(0)) -> dict:
    """{node_key: {agent_id, runs, master}} — earned from the trace, never stored."""
    return {"mastery": derive.mastery(project_id), "master_runs": derive.MASTER_RUNS}


# --- the seed -----------------------------------------------------------------

@r.post("/seed")
def do_seed() -> dict:
    """Ensure project 0 has an active plan of the FLEET; return its id.
    Idempotent — an unchanged services.yaml reseeds to zero writes, and a plan
    the manager authored outranks the seed entirely."""
    return {"plan_id": seed.seed_fleet_graph()}


@r.get("/manifest")
def manifest() -> dict:
    """The fleet as services.yaml declares it. The manager's authoring pass is
    given this as its INVENTORY — the cards it may retitle, re-spec and staff,
    and the ONLY cards it may name, because a card that is not in the registry is
    not a process anybody is running."""
    return seed.self_manifest()


@r.post("/tests-for-nodes")
def tests_for_nodes(body: TestsForNodes) -> dict:
    """Which test files exercise which node, for a set of AUTHORED boundaries.
    Sets are not JSON, so the map goes over the wire as sorted lists.

    Filtered to the keys that were ASKED about. The parser also credits the
    frontend and worker leaves by their file reads (`dashboard_js`, `index.html`,
    `canvas2`, `worker/`) rather than by imports, and those key names are the
    SEED's. A manager-authored plan that named its layers differently would
    otherwise get rows for nodes it does not have."""
    by_key = dict(body.by_key or {})
    asked = set(by_key)
    out = {path: sorted(keys & asked)
           for path, keys in seed.tests_for_nodes(by_key).items()}
    return {"map": {path: keys for path, keys in out.items() if keys}}


app.include_router(r)


if __name__ == "__main__":
    if "--spec" in sys.argv:
        # The one honest way to update the contract: print what the code actually serves,
        # commit it, and let the tests + oasdiff hold you to it.
        print(json.dumps(app.openapi(), indent=2))
    else:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8886")))
