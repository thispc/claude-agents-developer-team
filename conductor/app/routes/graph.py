"""The module graph's HTTP surface — /api/graph/self, the platform's own graph.

Everything here is behind the same _root/may_self_repair gate as the repair
router, for the same reason: this graph describes (and verifies) the repository
the server runs from, so it is an operator power, not a user feature. The
payloads are deliberately source-agnostic in shape — V2 serves project graphs
through the same shapes with only the source swapped, which is the whole point
of building devteam-first.

Verification here is AFFECTED-ONLY and ADVISORY: a node's own suite plus the
contract tests of every edge it touches, run with a bounded timeout through
shell.sh, results written to the test rows and the trace — never a rollback,
never a block. A red ring on the canvas that you can still operate is the V1
gate; teeth arrive only when the graph starts dispatching work.
"""

from __future__ import annotations

import re
import sys
import time

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .. import bus, config, db, modgraph, modgraph_health, providers, shell
from ..guards import _root
from .base import router

# Bounded: the affected set is a handful of files, and a verify that can hang the
# event loop's executor forever is worse than one that reports "timed out".
VERIFY_TIMEOUT_S = 420

_FILE_RE = re.compile(r"[\w./-]+\.(?:py|js|css|html|md|sh|json|yml|yaml)")


class VerifyBody(BaseModel):
    node: str


class NodeConfig(BaseModel):
    model: str | None = None       # None = leave alone; "" = back to the default
    autonomy: str | None = None    # None = leave alone; "" | supervised | autonomous


class LayoutBody(BaseModel):
    positions: dict[str, list[float]] = {}


class ServiceBody(BaseModel):
    action: str                    # start | stop


class TeamBody(BaseModel):
    world_id: int
    room_id: int


class AgentBody(BaseModel):
    agent_id: int


class ReplaceBody(BaseModel):
    aspect: str                    # stack | tests | module  (agent has its own endpoint)
    note: str = ""


class ClusterBody(BaseModel):
    action: str                    # start | stop


def _gated(request: Request) -> dict:
    """Root/may_self_repair, and the flag: with MODULE_GRAPH off the graph does not
    exist as a surface at all, so the old app stays byte-identical in behaviour."""
    if not config.MODULE_GRAPH:
        raise HTTPException(404, "the module graph is disabled")
    return _root(request)


def _self_plan() -> dict:
    plan = modgraph.active_plan(0)
    if not plan:
        # The boot seed is guarded and can fail without blocking startup; healing
        # here keeps the screen alive after such a boot — same deterministic seed.
        modgraph.seed_self_graph()
        plan = modgraph.active_plan(0)
    if not plan:
        raise HTTPException(503, "no plan for the platform graph")
    return plan


def _crew_snapshot() -> dict:
    """repair.status(), best-effort. The graph must render on a box where the crew
    has never run — an empty snapshot is an answer, not an error."""
    from .. import repair
    try:
        return repair.status()
    except Exception:
        return {}


def _known_models() -> set[str]:
    """The model ids config accepts — the provider catalog's undated ids. The same
    set rides in the payloads, so the UI select and this validator cannot drift."""
    return {m["id"] for p in providers.PROVIDERS.values() for m in p["models"]}


def _agent_names() -> dict[int, str]:
    """{living human id: specialist name} via the crew record — best-effort, so an
    assigned node shows WHO works it, not a bare integer."""
    from .. import repair
    try:
        info = repair.team() or {}
        by_factor = {f["id"]: f["name"] for f in repair.factors()}
        return {int(hid): by_factor.get(fid, fid)
                for fid, hid in (info.get("agents") or {}).items() if hid}
    except Exception:
        return {}


def _agent_out(a: dict | None, names: dict[int, str]) -> dict | None:
    """The assignment as the payload's agent dict, or None when unassigned."""
    if not a or not (a.get("agent_id") or a.get("home_id")):
        return None
    hid = a.get("agent_id") or a.get("home_id")
    return {"agent_id": a.get("agent_id"), "home_id": a.get("home_id"),
            "name": names.get(int(hid), "")}


def _room_members(world_id: int, room_id: int, user: dict | None = None) -> dict | None:
    """One Studio room as a pool: {world_id, room_id, name, members} — or None when the
    room cannot be loaded (or the substrate is down). Members carry `factor` only when
    the room IS the crew's own sprint table, because factors are a crew concept, not a
    Studio one — and the crew's record is conductor kv, so the decoration happens here.

    Degraded: None → the caller falls back to an empty pool named "unavailable", which
    is what an Atlas room panel shows when the lifeworld service is not answering.
    """
    from .. import lifeworld_client, repair
    got = lifeworld_client.room_members(int(world_id), int(room_id),
                                        user or repair._root_user() or {})
    if not got:
        return None
    factor_of: dict[int, str] = {}
    info = repair.team() or {}
    if info.get("world_id") == int(world_id) and info.get("room_id") == int(room_id):
        factor_of = {int(hid): fid for fid, hid in (info.get("agents") or {}).items() if hid}
    members = []
    for m in got.get("members") or []:
        row: dict = {"agent_id": int(m["agent_id"]), "name": m.get("name", "")}
        if row["agent_id"] in factor_of:
            row["factor"] = factor_of[row["agent_id"]]
        members.append(row)
    return {"world_id": int(world_id), "room_id": int(room_id),
            "name": got.get("name", ""), "members": members}


def _pool_current() -> dict:
    """The ASSIGNMENT POOL — who module work can be assigned to. kv `graph:pool:0`
    holds an operator's re-point; absent (or pointing at a dead room) it falls back to
    the crew's own table, because the crew is the default staff of the platform's graph.

    Deliberately INDEPENDENT of the repair crew's sprint team: re-pointing the pool
    changes who /node/{key}/agent may name, and nothing else — repair:world, the
    sprint deliberation and the specialists' seats are untouched."""
    from .. import repair
    rec = db.kv_get("graph:pool:0") or {}
    if rec.get("world_id") and rec.get("room_id"):
        got = _room_members(rec["world_id"], rec["room_id"])
        if got:
            return got
    info = repair.team() or {}
    if info.get("world_id"):
        got = _room_members(info["world_id"], info["room_id"])
        if got:
            return got
    return {"world_id": 0, "room_id": 0,
            "name": "the IT crew (not yet materialised)", "members": []}


def _mastery_out(m: dict | None, names: dict[int, str]) -> dict | None:
    """The contract's mastery dict for one node, or None when nobody has a closed ok
    run on it — absence of history is an honest answer, not a zero-run master."""
    if not m:
        return None
    return {"agent_id": m["agent_id"], "name": names.get(int(m["agent_id"]), ""),
            "runs": m["runs"], "master": m["master"]}


def _uptime_s() -> int:
    try:
        from .. import main as app_main   # lazy: main imports this package at boot
        return int(time.time() - app_main.STARTED_AT)
    except Exception:
        return 0


def _cluster_state() -> dict:
    """The conclusion's cluster fact: can a candidate build run beside this one, and is
    one running now. `available` is about THIS BOX (the sandbox snapshots the live
    tree), never a promise the start endpoint might then break."""
    from .. import sandbox, selfops
    if not (selfops.LIVE_TREE / "conductor" / "app" / "main.py").exists():
        return {"available": False, "running": False,
                "reason": "this process does not run from a devteam checkout — the "
                          "sandbox needs the live tree to snapshot"}
    try:
        st = sandbox.status()
    except Exception:
        st = {}
    out: dict = {"available": True, "running": bool(st.get("running"))}
    if st.get("running") and st.get("url"):
        out["url"] = st["url"]
    return out


def _activity(nodes: list[dict], st: dict) -> dict[str, list[dict]]:
    """Which nodes the crew is touching RIGHT NOW: in-flight sprint tasks, their
    file mentions prefix-matched to each node's boundary manifest. The highlight
    follows real work — there is nothing to simulate and nothing to go stale."""
    out: dict[str, list[dict]] = {}
    tasks = ((st.get("sprint") or {}).get("tasks") or [])
    for t in tasks:
        if t.get("status") != "building":
            continue
        text = " ".join(str(t.get(k) or "") for k in ("title", "brief", "evidence"))
        files = _FILE_RE.findall(text)
        for n in nodes:
            if any(f == p or (p.endswith("/") and f.startswith(p))
                   for f in files for p in n["paths"]):
                out.setdefault(n["key"], []).append(
                    {"task": str(t.get("title") or "")[:200],
                     "factor": str(t.get("factor") or "")})
    return out


def _test_counts(rows: list[dict]) -> dict:
    suites = [t for t in rows if t["kind"] == "suite"]
    passing = sum(1 for t in suites if t["status"] == "passing")
    failing = sum(1 for t in suites if t["status"] in ("failing", "error"))
    return {"total": len(suites), "passing": passing, "failing": failing,
            "advisory": len(suites) - passing - failing}


@router.get("/api/graph/self")
def graph_self(request: Request) -> dict:
    """The whole platform graph in one payload: the canvas's single read. Both
    levels ride in it — the canvas derives them client-side from parent_key —
    and a GROUP node answers for its children: suite counts summed, activity
    busy whenever any child is busy. Work itself only ever lands on leaves."""
    _gated(request)
    plan = _self_plan()
    pid = plan["id"]
    nodes = modgraph.nodes(pid)
    tests = modgraph.tests(pid)
    by_node: dict[str, list[dict]] = {}
    for t in tests:
        by_node.setdefault(t["node_key"], []).append(t)
    st = _crew_snapshot()
    activity = _activity([n for n in nodes if n["node_type"] != "group"], st)
    kids_of: dict[str, list[str]] = {}
    for n in nodes:
        if n["parent_key"]:
            kids_of.setdefault(n["parent_key"], []).append(n["key"])
    crew_state = st.get("state") or {}
    from .. import selfops
    try:
        code_cc = selfops.code_currency()      # cached 10s; boot/head shas already short
    except Exception:
        code_cc = {}
    notices = st.get("notices") or {}
    health = ("critical" if notices.get("critical") else
              "attention" if notices.get("warning") else
              "ok" if st else "unknown")
    names = _agent_names()
    pool = _pool_current()
    names.update({int(m["agent_id"]): m["name"] for m in pool["members"]
                  if int(m["agent_id"]) not in names})
    # Health: leaves are PROBED (real heartbeats, cached ~10s), groups roll up —
    # red if any child red, else yellow if any yellow, else green.
    beat_by = modgraph_health.beats(nodes)
    health_by: dict[str, dict] = {}
    for n in nodes:
        if n["node_type"] != "group":
            health_by[n["key"]] = modgraph_health.health_of(
                beat_by.get(n["key"], "ok"),
                modgraph_health.tests_state(by_node.get(n["key"], [])))
    for n in nodes:
        if n["node_type"] == "group":
            health_by[n["key"]] = modgraph_health.rollup(
                [health_by[k] for k in kids_of.get(n["key"], []) if k in health_by])
    mastery_by = modgraph_health.mastery(0)
    node_out = []
    for n in nodes:
        a = modgraph.get_assign(pid, n["key"]) or {}
        if n["node_type"] == "group":
            # Rolled up, not stored: totals summed over the children, activity
            # the dedup'd union of theirs — busy iff any child is busy.
            kid_counts = [_test_counts(by_node.get(k, []))
                          for k in kids_of.get(n["key"], [])]
            counts = {f: sum(c[f] for c in kid_counts)
                      for f in ("total", "passing", "failing", "advisory")}
            act, seen = [], set()
            for k in kids_of.get(n["key"], []):
                for item in activity.get(k, []):
                    sig = (item["task"], item["factor"])
                    if sig not in seen:
                        seen.add(sig)
                        act.append(item)
        else:
            counts = _test_counts(by_node.get(n["key"], []))
            act = activity.get(n["key"], [])
        node_out.append({
            "key": n["key"], "title": n["title"], "node_type": n["node_type"],
            "parent_key": n["parent_key"],
            "spec": n["spec"], "join_mode": n["join_mode"], "tags": n["tags"],
            "paths": n["paths"],
            "config": {"model": a.get("model") or "", "autonomy": a.get("autonomy") or ""},
            "agent": _agent_out(a, names),
            "tests": counts,
            "activity": act,
            "health": health_by.get(n["key"]),
            "service": modgraph_health.service_of(n["key"]),
            "mastery": _mastery_out(mastery_by.get(n["key"]), names),
        })
    # The top tier CANNOT MISS A CROSSING. Stored group→group arrows are
    # reconciled against the stored child edges (derive_group_edges) instead of
    # being served verbatim: a manager-authored plan once carried ops→db with no
    # selfrepair→data above it, plus one arrow no child edge backed. Authored
    # contracts survive where a real crossing backs them; fabrications do not.
    shaped = [{"src": e["src_key"], "dst": e["dst_key"], "edge_type": e["edge_type"],
               "contract": e["contract"], "contract_test": e["contract_test"]}
              for e in modgraph.edges(pid)]
    parent_of = {n["key"]: n["parent_key"] for n in nodes if n["parent_key"]}
    group_keys = {n["key"] for n in nodes if n["node_type"] == "group"}
    tier = [e for e in shaped if e["src"] in group_keys and e["dst"] in group_keys]
    child = [e for e in shaped if e["src"] in parent_of and e["dst"] in parent_of]
    edges_out = ([e for e in shaped
                  if not (e["src"] in group_keys and e["dst"] in group_keys)]
                 + modgraph.derive_group_edges(child, parent_of, group_edges=tier))
    return {
        "plan": {"id": pid, "version": plan["version"], "kind": plan["kind"],
                 "status": plan["status"], "authored_by": plan["authored_by"],
                 "notes": plan["notes"], "created_at": plan["created_at"]},
        "models": sorted(_known_models()),
        "nodes": node_out,
        "edges": edges_out,
        "runs": modgraph.runs(pid, limit=40),
        "positions": modgraph.positions(pid),
        "conclusion": {"health": health,
                       "repair": {"phase": crew_state.get("phase", ""),
                                  "sprint": crew_state.get("sprint_no", 0)},
                       # The platform's own vitals: the beat is the leaves' — any
                       # failing module fails the whole, because "the running
                       # platform" is not running if a piece of it is not.
                       "beat": ("fail" if any(v == "fail" for v in beat_by.values())
                                else "ok"),
                       "uptime_s": _uptime_s(),
                       "boot_sha": code_cc.get("boot", ""),
                       "head_sha": code_cc.get("head", ""),
                       "cluster": _cluster_state()},
    }


@router.post("/api/graph/self/verify")
def graph_self_verify(body: VerifyBody, request: Request) -> dict:
    """Run the AFFECTED tests for one node — its suite plus every contract it is
    party to — and record what happened. Advisory: the answer is information on
    the node, never an action against it."""
    _gated(request)
    plan = _self_plan()
    pid = plan["id"]
    if not any(n["key"] == body.node for n in modgraph.nodes(pid)):
        raise HTTPException(404, f"no node '{body.node}' in the active plan")
    files = [f for f in modgraph.affected_tests(pid, body.node)
             if (config.ROOT / f).exists()]
    if not files:
        raise HTTPException(400, f"no tests are mapped to '{body.node}'")
    bus.emit(0, None, "graph", "graph_verify_started",
             {"node": body.node, "files": files})
    run_id = modgraph.note_run(pid, body.node, "verify",
                               detail=f"affected-only: {len(files)} file(s)")
    res = shell.sh(sys.executable, "-m", "pytest", *files, "-q",
                   cwd=config.ROOT, timeout=VERIFY_TIMEOUT_S)
    out = (res.stdout or "") + "\n" + (res.stderr or "")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    headline = lines[-1][:300] if lines else "no output"
    # Attribute per file from pytest's own short summary; a run that failed in a
    # way we cannot attribute (collection crash, missing binary) is an error on
    # every file rather than a guessed pass.
    failed = {m.group(1) for m in re.finditer(r"^FAILED (\S+?)(?:::|\s|$)", out, re.M)}
    errored = {m.group(1) for m in re.finditer(r"^ERROR (\S+?)(?:::|\s|$)", out, re.M)}
    statuses: dict[str, str] = {}
    for f in files:
        if res.returncode == 0:
            statuses[f] = "passing"
        elif any(x.startswith(f) for x in errored):
            statuses[f] = "error"
        elif any(x.startswith(f) for x in failed):
            statuses[f] = "failing"
        elif failed or errored:
            statuses[f] = "passing"       # named culprits exist; this file was not one
        else:
            statuses[f] = "error"
        modgraph.update_test_result(pid, f, statuses[f], headline)
    ok = res.returncode == 0
    modgraph.close_run(run_id, "ok" if ok else "failed", detail=headline)
    bus.emit(0, None, "graph", "graph_verify_done",
             {"node": body.node, "ok": ok, "headline": headline,
              "failing": sorted(failed), "files": len(files)})
    return {"ok": ok, "node": body.node, "files": files, "headline": headline,
            "statuses": statuses}


@router.get("/api/graph/self/node/{key}")
def graph_self_node(key: str, request: Request) -> dict:
    """The inspector payload: everything a double-click should know about one node."""
    _gated(request)
    plan = _self_plan()
    pid = plan["id"]
    node = next((n for n in modgraph.nodes(pid) if n["key"] == key), None)
    if not node:
        raise HTTPException(404, f"no node '{key}' in the active plan")
    a = modgraph.get_assign(pid, key)
    return {
        "node": {k: node[k] for k in ("key", "title", "node_type", "spec", "join_mode",
                                      "parent_key", "tags", "paths")},
        "tests": [{"kind": t["kind"], "path": t["path"], "status": t["status"],
                   "last_result": t["last_result"]}
                  for t in modgraph.tests(pid, key)],
        "trace": modgraph.runs(pid, key, limit=20),
        "edges": [{"src": e["src_key"], "dst": e["dst_key"], "edge_type": e["edge_type"],
                   "contract": e["contract"], "contract_test": e["contract_test"]}
                  for e in modgraph.edges(pid) if key in (e["src_key"], e["dst_key"])],
        "config": {"model": (a or {}).get("model") or "",
                   "autonomy": (a or {}).get("autonomy") or ""},
        "models": sorted(_known_models()),
        "agent": _agent_out(a, _agent_names()),
    }


@router.post("/api/graph/self/node/{key}/config")
def graph_self_node_config(key: str, body: NodeConfig, request: Request) -> dict:
    """Steer one node: its model, its autonomy. Config acts without a replan —
    the immutable plan says what the module IS, this says who works it and how."""
    _gated(request)
    plan = _self_plan()
    pid = plan["id"]
    if not any(n["key"] == key for n in modgraph.nodes(pid)):
        raise HTTPException(404, f"no node '{key}' in the active plan")
    if body.model is not None:
        model = body.model.strip()
        known = _known_models()
        if model and model not in known:
            # Say what WOULD work: dated ids ("claude-haiku-4-5-20251001") are the
            # first thing people paste, and a bare "unknown" teaches them nothing.
            raise HTTPException(
                400, f"unknown model '{model}' — valid: "
                     f"{', '.join(sorted(known))}, or '' for the default")
    if body.autonomy is not None and body.autonomy not in ("", "supervised", "autonomous"):
        raise HTTPException(400, f"unknown autonomy '{body.autonomy}'")
    a = modgraph.set_assign(pid, key,
                            model=None if body.model is None else body.model.strip(),
                            autonomy=body.autonomy)
    return {"ok": True, "node": key,
            "config": {"model": a.get("model") or "", "autonomy": a.get("autonomy") or ""}}


@router.post("/api/graph/self/layout")
def graph_self_layout(body: LayoutBody, request: Request) -> dict:
    """Persist dragged node positions (kv, merged — a drag wins over auto-layout,
    and a node nobody dragged stays absent so auto-layout keeps owning it)."""
    _gated(request)
    plan = _self_plan()
    return {"ok": True, "positions": modgraph.save_positions(plan["id"], body.positions)}


@router.post("/api/graph/self/replan")
async def graph_self_replan(request: Request) -> dict:
    """Ask the crew's hidden manager to author (or re-confirm) the platform's own
    plan — ONE bounded completion, rows written only when the answer differs from
    the plan on the wall. The button spends the call every time it is pressed;
    the engine's own trigger is staleness-checked so sprints do not."""
    _gated(request)
    from .. import modgraph_author, repair
    if not repair._live():
        raise HTTPException(409, "offline — the manager needs credentials to author "
                                 "a plan; the deterministic seed stays in charge")
    plan_id = await modgraph_author.author_self_plan()
    if plan_id is None:
        raise HTTPException(409, "the manager produced no usable plan — nothing changed")
    plan = modgraph.get_plan(plan_id) or {}
    return {"ok": True, "plan": {"id": plan_id, "version": plan.get("version"),
                                 "authored_by": plan.get("authored_by"),
                                 "status": plan.get("status")}}


@router.post("/api/graph/self/node/{key}/service")
def graph_self_node_service(key: str, body: ServiceBody, request: Request) -> dict:
    """Start or stop the REAL runtime switch behind one module. HONEST mapping only
    (modgraph_health.SERVICES): the crew's kv toggle, a tuning knob read at runtime —
    and a module with no such switch answers 400 with the reason instead of faking a
    stop, because an operator plans around what a stop button claims."""
    _gated(request)
    plan = _self_plan()
    if not any(n["key"] == key for n in modgraph.nodes(plan["id"])):
        raise HTTPException(404, f"no node '{key}' in the active plan")
    if body.action not in ("start", "stop"):
        raise HTTPException(400, "action must be start or stop")
    try:
        svc = modgraph_health.service_set(key, body.action)
    except ValueError as e:
        raise HTTPException(400, str(e))
    bus.emit(0, None, "graph", "graph_service",
             {"node": key, "action": body.action, "state": svc["state"]})
    return {"ok": True, "node": key, "service": svc}


@router.get("/api/graph/self/team")
def graph_self_team(request: Request) -> dict:
    """The ASSIGNMENT POOL and every Studio room that could serve as one.

    The pool decides who /node/{key}/agent may assign to modules — and it is
    deliberately INDEPENDENT of the repair crew's sprint team: re-pointing it never
    re-seats the crew, touches repair:world, or changes who deliberates a sprint.
    Default pool = the crew's own table."""
    u = _gated(request)
    from .. import lifeworld_client, repair
    cur = _pool_current()
    info = repair.team() or {}
    # The crew's own world is owned by ROOT and this route is reachable by any account
    # the operator granted self-repair to, so it rides along as an explicitly-gated
    # extra rather than by the caller's ownership. The gate is `_gated` above; the
    # substrate does not own that decision and does not pretend to.
    extra = [int(info["world_id"])] if info.get("world_id") else []
    teams = lifeworld_client.rooms(u, extra)
    return {"current": {"world_id": cur["world_id"], "room_id": cur["room_id"],
                        "name": cur["name"]},
            "members": cur["members"],
            "teams": teams if teams is not None else [],
            # An empty list because the substrate is down is not the same fact as an
            # empty list because nobody has built a team, and a picker that cannot tell
            # them apart invites someone to build a second crew.
            "degraded": teams is None}


@router.post("/api/graph/self/team")
def graph_self_team_set(body: TeamBody, request: Request) -> dict:
    """Re-point the ASSIGNMENT POOL (kv graph:pool:0) at another Studio room.

    This changes who can be ASSIGNED to modules from now on — nothing more. It is
    deliberately independent of the repair crew's sprint team: the crew keeps its own
    lineup, its sprint table and its deliberations exactly as they are; existing
    per-node assignments also stand until someone re-assigns them."""
    _gated(request)
    got = _room_members(body.world_id, body.room_id)
    if not got:
        raise HTTPException(404, f"no Studio room {body.world_id}/{body.room_id}")
    if not got["members"]:
        raise HTTPException(400, "that room seats no agents — an empty pool can staff nothing")
    db.kv_set("graph:pool:0", {"world_id": int(body.world_id), "room_id": int(body.room_id)})
    bus.emit(0, None, "graph", "graph_pool_changed",
             {"world_id": body.world_id, "room_id": body.room_id, "name": got["name"]})
    return {"ok": True,
            "current": {"world_id": got["world_id"], "room_id": got["room_id"],
                        "name": got["name"]},
            "members": got["members"]}


@router.post("/api/graph/self/node/{key}/agent")
def graph_self_node_agent(key: str, body: AgentBody, request: Request) -> dict:
    """Point one LEAF module at a pool member (graph_assign — config acts, no replan).
    Groups refuse: a layer is worked through its children, so a specialist pinned to a
    group would be a specialist pinned to nothing. The agent must belong to the CURRENT
    pool — assignment is choosing from the staff, not conjuring an id."""
    _gated(request)
    plan = _self_plan()
    pid = plan["id"]
    node = next((n for n in modgraph.nodes(pid) if n["key"] == key), None)
    if not node:
        raise HTTPException(404, f"no node '{key}' in the active plan")
    if node["node_type"] in ("group", "aim", "conclusion"):
        raise HTTPException(400, f"'{key}' is a {node['node_type']} — work (and so "
                                 "assignment) lands on leaf modules only")
    pool = _pool_current()
    members = {int(m["agent_id"]): m["name"] for m in pool["members"]}
    if int(body.agent_id) not in members:
        raise HTTPException(400, f"agent {body.agent_id} is not in the current pool "
                                 f"'{pool['name']}' — re-point the pool first")
    a = modgraph.set_assign(pid, key, agent_id=int(body.agent_id))
    bus.emit(0, None, "graph", "graph_agent_assigned",
             {"node": key, "agent_id": int(body.agent_id),
              "name": members[int(body.agent_id)]})
    return {"ok": True, "node": key,
            "agent": {"agent_id": a.get("agent_id"), "home_id": a.get("home_id"),
                      "name": members[int(body.agent_id)]}}


@router.post("/api/graph/self/node/{key}/remove")
def graph_self_node_remove(key: str, request: Request) -> dict:
    """Remove a node the immutable way: a NEW plan version minus it, edges through it
    dropped, the old version superseded byte-identical. Refused for the frame (aim,
    conclusion) and for a group that still has children — empty the layer first.
    Assignments and positions carry forward by key (they are steering, not plan), and
    the crew's manager finds a directive-style note about the removal the next time it
    authors, so the module does not quietly reappear."""
    u = _gated(request)
    plan = _self_plan()
    pid = plan["id"]
    nodes = modgraph.nodes(pid)
    node = next((n for n in nodes if n["key"] == key), None)
    if not node:
        raise HTTPException(404, f"no node '{key}' in the active plan")
    if node["node_type"] in ("aim", "conclusion"):
        raise HTTPException(400, f"the {node['node_type']} is the plan's frame — a plan "
                                 "without one is not a plan")
    kids = [n["key"] for n in nodes if n["parent_key"] == key]
    if kids:
        raise HTTPException(400, f"'{key}' still has children ({', '.join(sorted(kids))}) "
                                 "— remove or re-parent them first")
    new_id = modgraph.create_plan(0, kind=plan["kind"], authored_by=u["username"],
                                  notes=f"node '{key}' removed by {u['username']}")
    survivors = [n for n in nodes if n["key"] != key]
    for n in survivors:
        paths = n["paths"]
        if n["node_type"] == "group" and n["key"] == node["parent_key"]:
            # The group invariant must survive the removal: its boundary is the union
            # of the children that REMAIN, not a memory of one that is gone.
            paths = sorted({p for c in survivors
                            if c["parent_key"] == n["key"] for p in c["paths"]})
        modgraph.add_node(new_id, n["key"], n["title"], node_type=n["node_type"],
                          spec=n["spec"], join_mode=n["join_mode"],
                          parent_key=n["parent_key"], tags=n["tags"], paths=paths)
    for e in modgraph.edges(pid):
        if key in (e["src_key"], e["dst_key"]):
            continue
        modgraph.add_edge(new_id, e["src_key"], e["dst_key"], edge_type=e["edge_type"],
                          contract=e["contract"], contract_test=e["contract_test"])
    for t in modgraph.tests(pid):
        if t["node_key"] == key:
            continue
        modgraph.map_test(new_id, t["node_key"], t["path"], kind=t["kind"],
                          source=t["source"], status=t["status"])
    for n in survivors:
        a = modgraph.get_assign(pid, n["key"])
        if a and (a.get("agent_id") or a.get("home_id") or a.get("model") or a.get("autonomy")):
            modgraph.set_assign(new_id, n["key"], agent_id=a.get("agent_id"),
                                home_id=a.get("home_id"), model=a.get("model"),
                                autonomy=a.get("autonomy"))
    pos = modgraph.positions(pid)
    pos.pop(key, None)
    if pos:
        db.kv_set(f"graph:pos:{new_id}", pos)
    modgraph.activate(new_id)
    notes = db.kv_get("graph:notes:0") or []
    notes.append({"ts": time.time(),
                  "note": f"The operator removed the module '{key}' ({node['title']}) "
                          "from the plan. Do not reintroduce it unless the operator "
                          "asks; fold anything it still covered into other modules."})
    db.kv_set("graph:notes:0", notes[-12:])
    new_plan = modgraph.get_plan(new_id) or {}
    bus.emit(0, None, "graph", "graph_node_removed",
             {"key": key, "plan": new_id, "version": new_plan.get("version")})
    return {"ok": True, "removed": key,
            "plan": {"id": new_id, "version": new_plan.get("version"),
                     "authored_by": new_plan.get("authored_by"),
                     "status": new_plan.get("status")}}


@router.post("/api/graph/self/node/{key}/replace")
async def graph_self_node_replace(key: str, body: ReplaceBody, request: Request) -> dict:
    """File a REAL ticket to replace one aspect of a module — its stack, its tests or
    the module itself — through the same self-issue machinery as /api/self/issue: the
    platform's own project, a directive the manager plans, the same loop the platform
    applies to every repo including this one. (Replacing the AGENT is not a ticket —
    that is the /node/{key}/agent endpoint, an act of steering, not of work.)"""
    u = _gated(request)
    plan = _self_plan()
    pid = plan["id"]
    node = next((n for n in modgraph.nodes(pid) if n["key"] == key), None)
    if not node:
        raise HTTPException(404, f"no node '{key}' in the active plan")
    if body.aspect not in ("stack", "tests", "module"):
        raise HTTPException(400, "aspect must be stack, tests or module — to change "
                                 "who works it, use the /agent endpoint instead")
    aspect_line = {
        "stack": "Replace the STACK this module is built on, keeping its behaviour.",
        "tests": "Replace this module's TEST SUITE — the current one no longer earns trust.",
        "module": "Replace the MODULE itself: same boundary, same contracts, new implementation.",
    }[body.aspect]
    title = f"[graph] Replace the {body.aspect} of module '{key}'"
    md = (f"{aspect_line}\n\n"
          f"**Module:** {node['title']} (`{key}`)\n"
          f"**Boundary:** {', '.join(node['paths']) or '(no paths)'}\n"
          f"**Spec:** {node['spec'] or '(none)'}\n\n"
          f"**Operator's note:** {body.note.strip() or '(none given)'}\n\n"
          "Raised from the module graph. Keep the module's boundary manifest and the "
          "contracts on its edges honoured; the graph's suite mapping will follow the "
          "files mechanically.")
    from .. import manager, selfops
    from .base import _manager_tasks
    proj_id = selfops.ensure_project(u["id"])
    res = await selfops.file_issue(proj_id, title, md, "improvement")
    import asyncio
    if proj_id not in _manager_tasks or _manager_tasks[proj_id].done():
        _manager_tasks[proj_id] = asyncio.get_event_loop().create_task(
            manager.run_manager(proj_id))
    bus.emit(0, None, "graph", "graph_replace_filed",
             {"node": key, "aspect": body.aspect, "project_id": proj_id})
    return {"ok": True, "project_id": proj_id, "node": key, "aspect": body.aspect, **res}


@router.post("/api/graph/self/cluster")
def graph_self_cluster(body: ClusterBody, request: Request) -> dict:
    """Start or stop the candidate build beside the live one — the conclusion card's
    cluster verb. Delegates to the SAME internals as /api/self/sandbox (sandbox.start/
    stop): a snapshot of the live tree, its own database, no credentials, its own port."""
    _gated(request)
    if body.action not in ("start", "stop"):
        raise HTTPException(400, "action must be start or stop")
    state = _cluster_state()
    if not state["available"]:
        raise HTTPException(409, state.get("reason", "the sandbox cannot run here"))
    from .. import sandbox
    if body.action == "stop":
        res = sandbox.stop()
        bus.emit(0, None, "graph", "graph_cluster", {"action": "stop"})
        return {"ok": True, "running": False, "killed": bool(res.get("killed"))}
    res = sandbox.start("live")
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "the sandbox would not start"))
    bus.emit(0, None, "system", "sandbox_started",     # same event the selfhost route emits
             {"ref": res["ref"], "commit": res["commit"], "port": res["port"]})
    return {"ok": True, "running": True, "url": res["url"], "port": res["port"],
            "commit": res["commit"]}
