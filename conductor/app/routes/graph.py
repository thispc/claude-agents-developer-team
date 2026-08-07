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

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .. import bus, config, modgraph, providers, shell
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
    notices = st.get("notices") or {}
    health = ("critical" if notices.get("critical") else
              "attention" if notices.get("warning") else
              "ok" if st else "unknown")
    names = _agent_names()
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
        })
    return {
        "plan": {"id": pid, "version": plan["version"], "kind": plan["kind"],
                 "status": plan["status"], "authored_by": plan["authored_by"],
                 "notes": plan["notes"], "created_at": plan["created_at"]},
        "models": sorted(_known_models()),
        "nodes": node_out,
        "edges": [{"src": e["src_key"], "dst": e["dst_key"], "edge_type": e["edge_type"],
                   "contract": e["contract"], "contract_test": e["contract_test"]}
                  for e in modgraph.edges(pid)],
        "runs": modgraph.runs(pid, limit=40),
        "positions": modgraph.positions(pid),
        "conclusion": {"health": health,
                       "repair": {"phase": crew_state.get("phase", ""),
                                  "sprint": crew_state.get("sprint_no", 0)}},
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
