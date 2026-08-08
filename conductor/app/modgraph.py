"""modgraph.py — the conductor's one door to the module graph's store.

Since P5 the store itself is a SERVICE: services/modgraph, its own process on
8886, its own `data/modgraph.db`, its own committed contract. The six `graph_*`
tables, every helper that touched them, `derive_group_edges`, `affected_tests`,
`mastery` and the seed all moved. This file is the pure client that reaches them,
keeping every public name the in-process module ever had — `create_plan`,
`nodes`, `edges`, `note_run`, `close_run`, `map_test`, `set_assign`, `positions`,
`seed_self_graph`, `self_manifest` and the rest — so `routes/graph.py`,
`modgraph_author.py` and the engine's build hooks changed as little as the wire
allowed.

The P5 cutover finished here: the vendored in-process body
(`_modgraph_legacy.py`), the dual-mode branch and the `MODGRAPH_ROLLBACK` test
switch are gone, and `MODGRAPH_URL` is now REQUIRED. It is written into
data/env/conductor.env by tools/gen_fleet.py from services.yaml, so every
supported boot path — `./run-local.sh` and `./run-local.sh --legacy` alike — has
it. A conductor started by hand without it fails loudly in init() rather than
serving an Atlas with no graph behind it.

WHY init() AND NOT IMPORT. Importing this module must stay free: tools, tests and
the module graph's own seed import it without ever reaching the store. Boot does
call init(), and that is the honest place to refuse — one clear message naming
run-local.sh, before the first screen discovers there is nothing behind it.

WHAT DID NOT COME WITH IT, and why the cycle is broken. `modgraph_author` — the
crew's hidden manager authoring the plan its specialists will work — stayed in
the conductor, because it needs providers, the repair engine's headroom and the
crew's roster. It is a BRAIN; the service is a STORE. That is the one genuine
import cycle in the ranking (`modgraph_author ↔ repair`) and P5 cut it by putting
a wire in the middle: the service imports nothing from the conductor, pinned by a
grep test.

LATENCY BUDGET: one localhost round trip per verb, ~1-3ms; hard timeout 2s so a
wedged service can never hold a sprint or a poll. The Atlas's single read
(`/api/graph/self`) costs SEVEN of them — plan, nodes, edges, tests, assigns,
runs, positions — plus one for mastery, which is why `assigns()` exists at all:
per-node `get_assign` would have made it twenty-one on a payload that is polled,
and a faithful port that triples the latency of the screen is not faithful.

DEGRADED MODES (service down). The graph is OBSERVABILITY, not substrate, and
every shape below follows from that one sentence. **A TRACE GAP IS RECORDED AS A
GAP, NEVER AS A BLOCK** — the crew keeps building when this service is down,
which is the opposite of the lifeworld's honest pause (P4) and deliberately so: a
crew without its specialists would still be spending, anonymously, but a crew
without its MAP is still writing code, running tests and landing commits. All
that is lost is the record of which module it happened on.

    note_run          → 0, close_run → no-op, with one deduped warn. The build
                        hook stores no run id, so nothing later tries to close
                        one that does not exist.
    update_test_result→ 0 rows touched; the verify still ran and still answered.
    nodes/edges/tests → [] and `degraded()` True, so the Atlas renders an honest
    /runs/positions     "graph unavailable" instead of an empty repository.
    active_plan       → None, and `degraded()` is what tells the BFF that None
    /get_plan           means "cannot see the plan", not "there is no plan".
    assigns → {}, get_assign → None, mastery → None
                      → a node shows unassigned and unmastered rather than
                        wrongly assigned. `mastery` is None (not {}) on purpose:
                        the authoring pass's "mastery outranks the manager's
                        reshuffle" rule must not read an outage as "nobody has
                        earned anything" and re-deal every module.
    seed_self_graph   → 0 with a log; boot carries on. The next boot seeds.
    self_manifest     → {} and `degraded()` True, which is how the authoring
                        pass refuses to spend a model call on a plan that has
                        nowhere to land.
    affected_tests    → [], so a verify answers "no tests are mapped" — the same
                        400 it already gives for a node with no suite.
The verbs degrade for a MISSING url too, by the same path: init() is the loud
door, and a door that also killed every later call would turn one misconfigured
process into a crash loop.
"""

from __future__ import annotations

import os

import httpx

from . import config, db

_URL = (os.environ.get("MODGRAPH_URL") or "").strip().rstrip("/")

_TIMEOUT = 2.0
# A plan import writes a whole version in one transaction, and the seed builds its
# manifest by reading ~20 source files and parsing every test module. Both are well
# under a second in practice; 15s is the "something is wrong" line, not the
# expected cost.
_SLOW_TIMEOUT = 15.0

# Tests inject an httpx transport here (a TestClient on the mounted service, or one
# that raises) — the client code path stays identical.
_TRANSPORT: httpx.BaseTransport | None = None
_TOKEN = ""

# Whether the LAST read reached the service. An empty graph and a graph that could
# not be read look identical on screen, and the difference is the whole question
# ("this repo has no plan" vs "you cannot see the plan") — so the BFF carries this
# into the payload instead of leaving the Atlas to guess. Set by the read verbs;
# costs no extra round trip. Same seam as logs._degraded_read.
_degraded_read = False

_NO_URL = (
    "MODGRAPH_URL is not set — the module graph's store is a service since P5 and "
    "the conductor has no in-process fallback any more. Boot with ./run-local.sh "
    "(or ./run-local.sh --legacy), which generates data/env/conductor.env from "
    "services.yaml and starts services/modgraph on 8886."
)


def _token() -> str:
    """The service's own token, read from where gen_fleet minted it — the same
    resolution the /svc gateway uses (routes/svc.py)."""
    global _TOKEN
    if not _TOKEN:
        try:
            _TOKEN = (config.ROOT / "data" / "tokens" / "modgraph.token") \
                .read_text().strip()
        except OSError:
            _TOKEN = ""
    return _TOKEN


def _client(timeout: float = _TIMEOUT) -> httpx.Client:
    """SYNC, all of it. Every caller of this store — the graph payload, the
    engine's build hooks, the authoring pass's row writes — is a plain function on
    a path that has never been awaitable, and making twenty-five verbs async would
    ripple through every route and every test that drives them for no gain a
    localhost round trip can measure. A blocked event loop is bounded by the
    timeout; the same trade usage.py documents for the meter. Tests swap this
    factory for a TestClient on the mounted service."""
    return httpx.Client(base_url=_URL or "http://modgraph.invalid", timeout=timeout,
                        transport=_TRANSPORT,
                        headers={"X-Service-Token": _token()})


def _degraded(verb: str, err: Exception) -> None:
    """One deduped warn per window, never a raise — the degraded shapes are the
    contract; the log line is how a 3am operator learns which one fired, and it is
    also the TRACE GAP being recorded as a gap."""
    try:
        from . import logs
        logs.log("lifecycle", "modgraph_degraded",
                 f"modgraph service unreachable — {verb} degraded "
                 f"({type(err).__name__}: {str(err)[:120]})",
                 level="warn", dedupe_s=300, verb=verb)
    except Exception:
        pass


def _get(path: str, params: dict | None = None, *, timeout: float = _TIMEOUT):
    with _client(timeout) as c:
        r = c.get(path, params=params or {})
        r.raise_for_status()
        return r.json()


def _post(path: str, payload: dict | None = None, *,
          timeout: float = _TIMEOUT, method: str = "POST"):
    with _client(timeout) as c:
        r = c.request(method, path, json=payload if payload is not None else {})
        r.raise_for_status()
        return r.json()


def _read(verb: str, path: str, key: str, empty, params: dict | None = None,
          timeout: float = _TIMEOUT):
    """One read, its empty shape, and the honest flag. Every read verb goes through
    here so none of them can forget to say it could not see."""
    global _degraded_read
    try:
        got = _get(path, params, timeout=timeout)
        _degraded_read = False
        return got.get(key, empty)
    except Exception as e:
        _degraded_read = True
        _degraded(verb, e)
        return empty


def degraded() -> bool:
    """Did the last read fail to reach the store? The BFF puts this on the payload
    so the Atlas can say "graph unavailable" instead of drawing an empty
    repository, and so a picker never reads an outage as a fact."""
    return bool(_degraded_read)


# --- plans -------------------------------------------------------------------

def create_plan(project_id: int, *, kind: str = "template",
                authored_by: str = "seed", notes: str = "") -> int:
    try:
        return int(_post("/plans", {"project_id": int(project_id), "kind": kind,
                                    "authored_by": authored_by,
                                    "notes": notes}).get("plan_id") or 0)
    except Exception as e:
        _degraded("create_plan", e)
        return 0


def get_plan(plan_id: int) -> dict | None:
    return _read("get_plan", f"/plans/{int(plan_id)}", "plan", None)


def active_plan(project_id: int) -> dict | None:
    return _read("active_plan", "/plans/active", "plan", None,
                 params={"project_id": int(project_id)})


def activate(plan_id: int) -> None:
    try:
        _post(f"/plans/{int(plan_id)}/activate")
    except Exception as e:
        _degraded("activate", e)


def import_plan(project_id: int, *, kind: str = "template",
                authored_by: str = "manager", notes: str = "",
                nodes: list[dict], edges: list[dict], tests: list[dict],
                assigns: dict | None = None, positions: dict | None = None,
                activate: bool = True) -> dict | None:
    """A whole plan version, in one call and one transaction.

    NEW WITH THE BOUNDARY, and not a convenience. In-process, the manager's
    authoring pass and the operator's node-removal each wrote a plan with
    fifty-odd row calls and a failure halfway left a DRAFT nobody activated —
    untidy but harmless. Over a wire those become fifty round trips and fifty
    chances to be interrupted with the plan already ACTIVE and half its edges
    missing, which the Atlas would render as a repository that had lost its
    contracts. A plan is authored as one thing, so it is written as one."""
    try:
        return _post("/import-plan", {
            "project_id": int(project_id), "kind": kind, "authored_by": authored_by,
            "notes": notes, "nodes": nodes, "edges": edges, "tests": tests,
            "assigns": assigns or {}, "positions": positions or {},
            "activate": bool(activate)}, timeout=_SLOW_TIMEOUT).get("plan")
    except Exception as e:
        _degraded("import_plan", e)
        return None


# --- nodes & edges -----------------------------------------------------------

def add_node(plan_id: int, key: str, title: str, *, node_type: str = "code",
             spec: str = "", join_mode: str = "all_of", parent_key: str = "",
             tags: list | None = None, paths: list | None = None) -> int:
    try:
        return int(_post(f"/plans/{int(plan_id)}/nodes", {
            "key": key, "title": title, "node_type": node_type, "spec": spec,
            "join_mode": join_mode, "parent_key": parent_key,
            "tags": [str(t) for t in (tags or [])],
            "paths": [str(p) for p in (paths or [])]}).get("id") or 0)
    except Exception as e:
        _degraded("add_node", e)
        return 0


def add_edge(plan_id: int, src_key: str, dst_key: str, *, edge_type: str = "depends",
             contract: dict | None = None, contract_test: str = "") -> int:
    try:
        return int(_post(f"/plans/{int(plan_id)}/edges", {
            "src_key": src_key, "dst_key": dst_key, "edge_type": edge_type,
            "contract": contract or {},
            "contract_test": contract_test}).get("id") or 0)
    except Exception as e:
        _degraded("add_edge", e)
        return 0


def nodes(plan_id: int) -> list[dict]:
    return _read("nodes", f"/plans/{int(plan_id)}/nodes", "nodes", [])


def edges(plan_id: int) -> list[dict]:
    return _read("edges", f"/plans/{int(plan_id)}/edges", "edges", [])


# --- the trace ---------------------------------------------------------------

def note_run(plan_id: int, node_key: str, kind: str, *, task_id: int | None = None,
             agent_id: int | None = None, status: str = "running",
             detail: str = "") -> int:
    """Open a trace row, or 0.

    ZERO IS THE DEGRADED SHAPE AND IT IS LOAD-BEARING. The engine stores what this
    returns and closes it later; a fabricated id would close somebody else's row
    when the service came back. Falsy means "there is no run to close", which the
    build hook already handles because a task that matched no node has always
    meant the same thing."""
    try:
        return int(_post("/runs", {
            "plan_id": int(plan_id), "node_key": node_key, "kind": kind,
            "task_id": task_id, "agent_id": agent_id, "status": status,
            "detail": detail[:2000]}).get("id") or 0)
    except Exception as e:
        _degraded("note_run", e)
        return 0


def close_run(run_id: int, status: str, detail: str = "") -> None:
    try:
        _post(f"/runs/{int(run_id)}", {"status": status, "detail": detail[:2000]},
              method="PATCH")
    except Exception as e:
        _degraded("close_run", e)


def runs(plan_id: int, node_key: str = "", limit: int = 50) -> list[dict]:
    return _read("runs", f"/plans/{int(plan_id)}/runs", "runs", [],
                 params={"node_key": node_key, "limit": max(1, min(int(limit), 500))})


# --- tests -------------------------------------------------------------------

def map_test(plan_id: int, node_key: str, path: str, *, kind: str = "suite",
             source: str = "", status: str = "mapped") -> int:
    try:
        return int(_post(f"/plans/{int(plan_id)}/tests", {
            "node_key": node_key, "path": path, "kind": kind, "source": source,
            "status": status}).get("id") or 0)
    except Exception as e:
        _degraded("map_test", e)
        return 0


def tests(plan_id: int, node_key: str = "") -> list[dict]:
    return _read("tests", f"/plans/{int(plan_id)}/tests", "tests", [],
                 params={"node_key": node_key})


def update_test_result(plan_id: int, path: str, status: str,
                       last_result: str = "") -> int:
    try:
        return int(_post(f"/plans/{int(plan_id)}/test-result", {
            "path": path, "status": status,
            "last_result": last_result[:2000]}).get("updated") or 0)
    except Exception as e:
        _degraded("update_test_result", e)
        return 0


def affected_tests(plan_id: int, node_key: str) -> list[str]:
    return _read("affected_tests", f"/plans/{int(plan_id)}/affected", "files", [],
                 params={"node_key": node_key})


# --- assignment --------------------------------------------------------------

def set_assign(plan_id: int, node_key: str, *, agent_id: int | None = None,
               home_id: int | None = None, model: str | None = None,
               autonomy: str | None = None) -> dict:
    try:
        got = _post(f"/plans/{int(plan_id)}/assign", {
            "node_key": node_key, "agent_id": agent_id, "home_id": home_id,
            "model": model, "autonomy": autonomy}).get("assign")
        if got:
            return got
    except Exception as e:
        _degraded("set_assign", e)
    # The historical signature never returned None, and its one caller reads two
    # keys straight off the answer. Echo what was asked for rather than inventing
    # what is stored — nothing was stored.
    return {"agent_id": agent_id, "home_id": home_id,
            "model": model or "", "autonomy": autonomy or ""}


def get_assign(plan_id: int, node_key: str) -> dict | None:
    return _read("get_assign", f"/plans/{int(plan_id)}/assign", "assign", None,
                 params={"node_key": node_key})


def assigns(plan_id: int) -> dict[str, dict]:
    """Every assignment on the plan, by node key — one call for the callers that
    want all of them (the payload, and the engine's node matcher)."""
    return _read("assigns", f"/plans/{int(plan_id)}/assigns", "assigns", {})


# --- positions ---------------------------------------------------------------

def positions(plan_id: int) -> dict:
    return _read("positions", f"/plans/{int(plan_id)}/positions", "positions", {})


def save_positions(plan_id: int, pos: dict) -> dict:
    try:
        return _post(f"/plans/{int(plan_id)}/positions",
                     {"positions": pos or {}}).get("positions") or {}
    except Exception as e:
        _degraded("save_positions", e)
        return {}


# --- derivation --------------------------------------------------------------

def derive_group_edges(child_edges: list[dict], parent_of: dict[str, str],
                       group_edges: list[dict] | None = None) -> list[dict]:
    """The top tier, reconciled against an authored one — computed by the service
    because it is GRAPH DOMAIN, not presentation: the seed calls it to build the
    plan it writes, and the payload calls it again to reconcile what a manager
    authored. Two copies of that rule on two sides of a wire is how the stored
    plan and the rendered graph start telling different stories.

    Degraded → [], so the top room shows its cards with no arrows between layers
    rather than arrows nothing backs."""
    try:
        return list(_post("/derive-group-edges", {
            "child_edges": child_edges, "parent_of": parent_of,
            "group_edges": group_edges}).get("edges") or [])
    except Exception as e:
        _degraded("derive_group_edges", e)
        return []


def mastery(project_id: int = 0) -> dict | None:
    """{node_key: {agent_id, runs, master}}, or None when the store cannot be read.
    NONE, NOT {} — the authoring pass's "a master keeps its module" rule reads
    this, and an outage that looked like "nobody has earned anything" would let
    one reshuffle undo every earned continuity on the box. The payload treats None
    as "no mastery to show", which is honest."""
    global _degraded_read
    try:
        got = _get("/mastery", {"project_id": int(project_id)})
        _degraded_read = False
        return dict(got.get("mastery") or {})
    except Exception as e:
        _degraded_read = True
        _degraded("mastery", e)
        return None


# --- the seed and the manifest ----------------------------------------------

def seed_self_graph() -> int:
    """Ensure project 0 has an active plan; return its id, or 0.

    Degraded → 0 with a log, and boot carries on: main.py already guards this call
    because a seed failure must never block startup, and the graph screen heals
    itself on the next read."""
    try:
        return int(_post("/seed", timeout=_SLOW_TIMEOUT).get("plan_id") or 0)
    except Exception as e:
        _degraded("seed_self_graph", e)
        return 0


def self_manifest() -> dict:
    """The repo's real module inventory, read from the working tree by the service
    — the ONE builder, shared by the offline seed and the manager's authoring
    pass, so both describe the same tree.

    Degraded → {}, which is exactly how `modgraph_author` learns to refuse:
    authoring an inventory nobody could read would spend a real model call to
    write a plan into a store that cannot take it."""
    global _degraded_read
    try:
        got = _get("/manifest", timeout=_SLOW_TIMEOUT)
        _degraded_read = False
        return got
    except Exception as e:
        _degraded_read = True
        _degraded("self_manifest", e)
        return {}


def _manifest_of(plan_id: int) -> dict:
    """The stored plan, re-shaped for comparison with self_manifest()."""
    global _degraded_read
    try:
        got = _get(f"/plans/{int(plan_id)}/manifest", timeout=_SLOW_TIMEOUT)
        _degraded_read = False
        return got
    except Exception as e:
        _degraded_read = True
        _degraded("manifest_of", e)
        return {}


def _tests_for_nodes(by_key: dict[str, list[str]]) -> dict[str, set[str]]:
    """Which test files exercise which AUTHORED boundary. Sets are not JSON, so
    the wire carries sorted lists and this end restores the shape its one caller
    (the authoring pass's candidate manifest) has always used."""
    global _degraded_read
    try:
        got = _post("/tests-for-nodes", {"by_key": by_key}, timeout=_SLOW_TIMEOUT)
        _degraded_read = False
        return {path: set(keys) for path, keys in (got.get("map") or {}).items()}
    except Exception as e:
        _degraded_read = True
        _degraded("tests_for_nodes", e)
        return {}


# --- lifecycle ---------------------------------------------------------------

def health() -> bool:
    """Is the store actually answering? The service's own /health — the same
    endpoint process-compose probes — asked through this door rather than around
    it, so every verb and any future card agree on what "up" means."""
    try:
        with _client(2.0) as c:
            r = c.get("/health")
            return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False


# The six tables and the one kv prefix the strangler left in the conductor's
# database. The layouts are in the list because they moved WITH the rows they are
# keyed to: a `graph:pos:{plan_id}` left behind names a plan id in another
# process's database, which is not a layout, it is a coincidence waiting to happen.
_LEGACY_TABLES = ("graph_plans", "graph_nodes", "graph_edges", "graph_node_runs",
                  "graph_node_tests", "graph_assign")
_LEGACY_KV_PREFIX = "graph:pos:"


def init() -> None:
    """Refuse to boot without the store, and finish the strangler.

    The conductor declares no `graph_*` table any more. They are dropped here — in
    the module that replaced the code which used to read them — but ONLY once the
    service says it has finished with them, and that condition is the whole point
    of this function rather than a formality.

    NOTHING ORDERS THE TWO PROCESSES. process-compose starts the conductor and the
    modgraph service together with no `depends_on` between them, so on the first
    boot after the cutover this can easily run before the service has attached and
    copied.

    WHAT A PREMATURE DROP WOULD COST IS THE TRACE, AND THE TRACE IS THE ONE PART
    THAT CANNOT BE REBUILT. Plans, nodes and edges regenerate from the working
    tree in under a second (the seed is deterministic), and the crew's manager
    re-authors on the next lineup change. `graph_node_runs` does not: it is every
    build and verify any specialist has ever closed, and MASTERY IS COUNTED FROM
    IT, never stored. Drop it early and every module silently loses its master, so
    the next authoring pass reshuffles specialists off the modules they had
    earned — and nothing anywhere reports that anything was lost. `graph_assign`
    goes the same way: the operator's own per-node steering, gone without a word.

    `GET /health` answers `backfilled` for exactly this; it means "the first-boot
    decision has been made, either way", including the boring cases where there
    was nothing to copy. Until it is true the tables stay and the next boot tries
    again — a dead table costs a few kilobytes, and nothing in the conductor reads
    one ever again.

    P5-A ALSO DID NOT RENAME THEM. The earlier phases renamed their legacy table
    aside; here the rollback was the vendored body, which read `graph_plans` and
    its five siblings by name, so renaming would have made a rollback find six
    empty tables and reseed a fresh plan over the trace. The rows stayed put and
    the service copied them out with the IDS PRESERVED, because a plan id is a
    pointer held outside these tables.
    """
    if not _URL:
        raise RuntimeError(_NO_URL)
    try:
        with _client(2.0) as c:
            r = c.get("/health")
            r.raise_for_status()
            settled = bool(r.json().get("backfilled"))
    except Exception as e:
        # Deliberately not fatal. A conductor that refused to boot because a PEER
        # was still starting is how a fleet takes itself down in a ring, and the
        # only thing waiting on this answer is six tables nobody reads.
        _degraded("init", e)
        return
    if not settled:
        return
    for table in _LEGACY_TABLES:
        db._execute(f"DROP TABLE IF EXISTS {table}")
    db._execute("DELETE FROM kv WHERE k LIKE ?", (_LEGACY_KV_PREFIX + "%",))
