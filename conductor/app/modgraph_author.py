"""modgraph_author.py — the crew's manager authors the platform's own module plan.

The seed graph (modgraph.seed_self_graph) is deterministic and true, but it is
MINE — a decomposition hardcoded by whoever wrote the seed table. The owner's
correction stands: the graph must be TEAM-authored. So the crew's hidden manager
— the same bounded mediator that hosts the sprint deliberation, not an SDK
session with tools — reads the repo's real module inventory and its own roster,
and authors the plan its specialists will work: two levels (architecture groups
over leaf modules), regrouped, renamed, annotated, and ASSIGNED — one
specialist per LEAF; a group is worked only through its children.

WHY ONE BOUNDED CALL. The crew's whole spend discipline is that every model call
is counted and none is open-ended (repair.py's meters, phase_cost, headroom).
Authoring rides the same rail: one `providers.complete` on the builder model, a
strict-JSON answer, and everything after it is deterministic validation — a path
that does not exist is dropped and logged, never trusted. Offline, this module
returns None and changes nothing; the seed stays in charge, which is exactly
what a fallback is for.

WHY THE STORE STAYS MODEL-FREE. modgraph.py is pinned at source level to never
touch a model (the no-LLM-in-the-scheduler verdict). Authorship therefore lives
HERE: this module spends the call and hands modgraph plain rows. The plan it
writes is a new immutable version (authored_by='manager') that supersedes the
seed; an answer identical to the active plan writes nothing at all, because a
"new version" with the same bytes would be history-noise pretending to be work.

The kv stamp `graph:authored:0` records which roster the manager last authored
for. The engine's per-sprint hook consults it (should_author) so authoring costs
one call per LINEUP, not one per sprint — a plan re-confirmed every five minutes
is quota spent proving nothing changed.
"""

from __future__ import annotations

import graphlib
import json
import re
import time

from . import bus, config, db, logs, modgraph, providers, repair, tuning
from . import repair_builder as rb

NODE_TYPES = ("aim", "group", "code", "research", "data", "conclusion")
EDGE_TYPES = ("depends", "interface", "data", "artifact")

AUTHOR_SYSTEM = (
    "You are the hidden manager of the devteam platform's own IT crew. You are handed the "
    "repository's REAL module inventory (every path exists; the specs are the modules' own "
    "docstrings), the plan currently on the wall, and your team roster. Author the module "
    "plan YOUR team will work — TWO LEVELS, like an architecture diagram you can zoom: a "
    "handful of `group` nodes (the layers: backend, frontend, data, …) at the top, and "
    "every real module a child of exactly one group via `parent`. You may regroup, "
    "rename, retitle and re-annotate modules as you judge best, and you assign each LEAF "
    "module to the specialist whose lens fits it. Return ONLY one JSON object, no prose:\n"
    '{"modules": [{"key": <short stable slug>, "title": <short>, '
    '"node_type": "aim"|"group"|"code"|"research"|"data"|"conclusion", '
    '"parent": <the owning group\'s key, leaf modules only>, '
    '"spec": <what the module IS, 1-3 sentences>, "join": "all_of"|"any_of", '
    '"tags": [<strings>], "paths": [<repo paths; directories end in "/">]}],\n'
    ' "edges": [{"src": <key>, "dst": <key>, '
    '"type": "depends"|"interface"|"data"|"artifact", '
    '"contract": {"rule": <the rule both sides honour>}}],\n'
    ' "assignments": {<LEAF module key>: <factor id>}}\n'
    "Rules: exactly one aim node and one conclusion node, both with empty paths and no "
    "parent; a group has no parent and its spec is one honest sentence about the layer — "
    "leave a group's paths empty, they are computed server-side as the union of its "
    "children's; a leaf's paths may name ONLY files and directories from the inventory — "
    "never invent a path; edges may connect groups to groups and leaves to leaves (the "
    "canvas clips by level); assignments go on LEAF modules only and every value must be "
    "one of the roster's factor ids; keep a key stable when the module is the same thing "
    "as in the current plan.")


def _sig() -> list[str]:
    """The roster signature authoring is keyed on: which factors are on the crew."""
    return sorted(f["id"] for f in repair.enabled_factors())


def should_author() -> bool:
    """The engine's cheap staleness check — no model, no rows, one kv read.

    True until a manager pass has authored for the CURRENT lineup. The stamp is
    written only when an authoring call actually completed (including the
    identical-no-op case: the manager looked and kept the plan — that answer is
    paid for and holds until the roster changes)."""
    stamp = db.kv_get("graph:authored:0") or {}
    return stamp.get("factors") != _sig()


def _stamp(plan_id: int) -> None:
    db.kv_set("graph:authored:0",
              {"factors": _sig(), "ts": time.time(), "plan_id": int(plan_id)})


def _validated(obj: dict, man: dict, fs: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """The manager's answer, held to the ground: (nodes, edges, assignments).

    Every claim that can be checked is checked — a path that is not in the tree
    is dropped and logged, an unknown node type or edge type falls back to the
    default, an edge naming a node that does not exist is discarded, an
    assignment to a factor not on the roster is discarded. The manager may be
    creative about SHAPE; it does not get to be creative about facts."""
    dropped: list[str] = []
    nodes: list[dict] = []
    seen: set[str] = set()
    for m in obj.get("modules") or []:
        if not isinstance(m, dict):
            continue
        key = re.sub(r"[^a-z0-9-]+", "-", str(m.get("key") or "").lower()).strip("-")[:40]
        if not key or key in seen:
            continue
        seen.add(key)
        ntype = str(m.get("node_type") or "code")
        ntype = ntype if ntype in NODE_TYPES else "code"
        join = str(m.get("join") or "all_of")
        join = join if join in ("all_of", "any_of") else "all_of"
        parent = re.sub(r"[^a-z0-9-]+", "-",
                        str(m.get("parent") or "").lower()).strip("-")[:40]
        paths: list[str] = []
        if ntype != "group":                # a group's paths are computed, below
            for p in (m.get("paths") or [])[:40]:
                p = str(p).strip().lstrip("/")
                if p and (config.ROOT / p).exists():
                    paths.append(p)
                elif p:
                    dropped.append(f"{key}: {p}")
        nodes.append({"key": key, "title": str(m.get("title") or key)[:140],
                      "node_type": ntype, "spec": str(m.get("spec") or "")[:600],
                      "join_mode": join, "parent_key": parent,
                      "tags": [str(t)[:40] for t in (m.get("tags") or [])][:8],
                      "paths": paths})
    if dropped:
        logs.warn("sprint", "graph_paths_dropped",
                  f"the manager named {len(dropped)} path(s) that do not exist — dropped: "
                  + "; ".join(dropped[:5]), n=len(dropped))
    # The frame is not optional: a plan without an aim or a conclusion is not a
    # plan, so a missing one is borrowed from the seed manifest rather than
    # failing the whole authoring pass over the least interesting nodes.
    if not any(n["node_type"] == "aim" for n in nodes):
        nodes.insert(0, dict(man["nodes"][0]))
    if not any(n["node_type"] == "conclusion" for n in nodes):
        nodes.append(dict(man["nodes"][-1]))
    nodes.sort(key=lambda n: {"aim": 0, "conclusion": 2}.get(n["node_type"], 1))

    # Two levels, held structurally: a parent must name a GROUP in this same
    # answer — groups, the aim and the conclusion sit at the top themselves, and
    # a parent pointing anywhere else flattens to top level rather than minting
    # a hierarchy the canvas has no idea how to clip. A group's boundary is then
    # COMPUTED — the union of its children's paths — because the one fact the
    # top level must never drift from is what the modules under it cover.
    group_keys = {n["key"] for n in nodes if n["node_type"] == "group"}
    for n in nodes:
        if n["node_type"] in ("aim", "conclusion", "group") \
                or n["parent_key"] not in group_keys:
            n["parent_key"] = ""
    for g in nodes:
        if g["node_type"] == "group":
            g["paths"] = sorted({p for n in nodes
                                 if n["parent_key"] == g["key"] for p in n["paths"]})

    keyset = {n["key"] for n in nodes}
    edges: list[dict] = []
    eseen: set[tuple] = set()
    for e in obj.get("edges") or []:
        if not isinstance(e, dict):
            continue
        src, dst = str(e.get("src") or ""), str(e.get("dst") or "")
        etype = str(e.get("type") or "depends")
        etype = etype if etype in EDGE_TYPES else "depends"
        if src not in keyset or dst not in keyset or src == dst:
            continue
        if (src, dst, etype) in eseen:
            continue
        eseen.add((src, dst, etype))
        raw = e.get("contract")
        contract = raw if isinstance(raw, dict) else ({"rule": str(raw)} if raw else {})
        edges.append({"src": src, "dst": dst, "edge_type": etype, "contract": contract})

    fids = {f["id"] for f in fs}
    # Assignments land on LEAVES only: a group is worked through its children,
    # so a specialist pinned to a layer would be a specialist pinned to nothing.
    leaf_keys = {n["key"] for n in nodes
                 if n["node_type"] not in ("aim", "conclusion", "group")}
    assigns = {str(k): str(v) for k, v in (obj.get("assignments") or {}).items()
               if str(k) in leaf_keys and str(v) in fids}
    return nodes, edges, assigns


def _candidate_manifest(nodes: list[dict], edges: list[dict]) -> dict:
    """The would-be plan in modgraph._manifest_of's comparable shape, with the test
    mapping derived MECHANICALLY from the authored boundaries — the same parsed-
    imports discipline as the seed, filtered to keys this plan actually has. The
    manager authors boundaries; it never authors which tests exist. LEAVES only:
    a group's paths are the union of its children's, so letting it into the
    mapping would swallow every suite its children should own — groups answer
    through the payload's rollup instead."""
    by_key = {n["key"]: n["paths"] for n in nodes if n["node_type"] != "group"}
    tmap = {path: keys & set(by_key)
            for path, keys in modgraph._tests_for_nodes(by_key).items()}
    tmap = {path: keys for path, keys in tmap.items() if keys}
    edge_list = []
    for e in edges:
        both = sorted(p for p, keys in tmap.items()
                      if e["src"] in keys and e["dst"] in keys)
        edge_list.append({"src": e["src"], "dst": e["dst"], "edge_type": e["edge_type"],
                          "contract": e["contract"],
                          "contract_test": both[0] if both else ""})
    test_list = sorted(
        [{"node": key, "kind": "suite", "path": path}
         for path, keys in tmap.items() for key in keys] +
        [{"node": e["dst"], "kind": "contract", "path": e["contract_test"]}
         for e in edge_list if e["contract_test"]],
        key=lambda t: (t["node"], t["kind"], t["path"]))
    seen, deduped = set(), []
    for t in test_list:
        k = (t["node"], t["kind"], t["path"])
        if k not in seen:
            seen.add(k)
            deduped.append(t)
    return {"nodes": nodes, "edges": edge_list, "tests": deduped}


def _topo(cand: dict) -> list[str]:
    """Reveal order: every edge's src before its dst, aim naturally first. A cycle
    in the manager's edges must not eat the reveal — fall back to authored order."""
    preds: dict[str, set] = {n["key"]: set() for n in cand["nodes"]}
    for e in cand["edges"]:
        preds[e["dst"]].add(e["src"])
    try:
        return list(graphlib.TopologicalSorter(preds).static_order())
    except graphlib.CycleError:
        return [n["key"] for n in cand["nodes"]]


async def author_self_plan() -> int | None:
    """ONE bounded completion: the crew's manager authors (or re-confirms) the
    devteam module plan. Returns the active plan id, or None when offline or the
    answer was unusable — in both of which cases nothing changed.

    Differs from the active plan → a NEW immutable version (authored_by=
    'manager'), nodes/edges/mechanical test mapping written, activated, each node
    assigned to its specialist (factor → the crew's living human id), and the
    canvas reveal fired: `graph_node_planned` per node in topological order, then
    `graph_plan_ready`. Identical → no rows, no events, just the stamp."""
    if not repair._live():
        return None
    try:
        info = repair.ensure_team() or repair.team() or {}
    except Exception:
        info = repair.team() or {}
    fs = repair.enabled_factors()
    man = modgraph.self_manifest()
    cur = modgraph.active_plan(0)
    cur_man = modgraph._manifest_of(cur["id"]) if cur else None

    inventory = "\n".join(
        f"- {n['key']} ({n['node_type']}): {n['title']}\n"
        f"    paths: {', '.join(n['paths']) or '(none)'}\n"
        f"    spec: {n['spec'][:200]}"
        for n in man["nodes"])
    roster = "\n".join(f"- {f['id']}: {f['name']} — {f['brief']}" for f in fs)
    current = "(no active plan)"
    if cur and cur_man:
        current = json.dumps({
            "authored_by": cur["authored_by"], "version": cur["version"],
            "modules": [{"key": n["key"], "title": n["title"], "paths": n["paths"]}
                        for n in cur_man["nodes"]]})
    model = str(tuning.get("repair_builder_model"))
    try:
        raw = await providers.complete(
            "anthropic", model, AUTHOR_SYSTEM,
            f"INVENTORY (the tree as it is):\n{inventory}\n\n"
            f"CURRENT PLAN:\n{current}\n\nTEAM ROSTER:\n{roster}\n\n"
            "Return the JSON object.",
            repair._root_settings(), max_tokens=3000)
        # Billed only when the call actually went out — most failures here are
        # "no credentials after all", and a phantom row would move real meters.
        repair.ledger_add("plan", model, 0)
    except Exception as e:
        logs.warn("sprint", "graph_author_failed", str(e)[:200])
        return None
    obj = rb._json_block(raw, "{", "}")
    if not isinstance(obj, dict) or not obj.get("modules"):
        logs.warn("sprint", "graph_author_unparseable",
                  "the manager's plan did not parse — nothing changed")
        return None

    nodes, edges, assigns = _validated(obj, man, fs)
    cand = _candidate_manifest(nodes, edges)
    if cur and cur_man == cand:
        _stamp(cur["id"])
        logs.info("sprint", "graph_plan_unchanged",
                  "the manager re-read the tree and kept the plan as it stands",
                  plan=cur["id"])
        return int(cur["id"])

    plan_id = modgraph.create_plan(
        0, kind="template", authored_by="manager",
        notes="authored by the crew's manager from the live inventory")
    for n in cand["nodes"]:
        modgraph.add_node(plan_id, n["key"], n["title"], node_type=n["node_type"],
                          spec=n["spec"], join_mode=n["join_mode"],
                          parent_key=n["parent_key"], tags=n["tags"], paths=n["paths"])
    for e in cand["edges"]:
        modgraph.add_edge(plan_id, e["src"], e["dst"], edge_type=e["edge_type"],
                          contract=e["contract"], contract_test=e["contract_test"])
    for t in cand["tests"]:
        modgraph.map_test(plan_id, t["node"], t["path"], kind=t["kind"], status="mapped")
    modgraph.activate(plan_id)
    agents = (info or {}).get("agents") or {}
    for key, fid in assigns.items():
        hid = agents.get(fid)
        if hid:
            modgraph.set_assign(plan_id, key, agent_id=int(hid))

    by_key = {n["key"]: n for n in cand["nodes"]}
    order = _topo(cand)
    for i, key in enumerate(order):
        n = by_key[key]
        bus.emit(0, None, "graph", "graph_node_planned",
                 {"key": key, "title": n["title"], "node_type": n["node_type"],
                  "factor": assigns.get(key, ""), "i": i, "n": len(order)})
    plan = modgraph.get_plan(plan_id) or {}
    bus.emit(0, None, "graph", "graph_plan_ready",
             {"plan": plan_id, "version": plan.get("version"),
              "nodes": len(cand["nodes"]), "authored_by": "manager"})
    _stamp(plan_id)
    logs.info("sprint", "graph_plan_authored",
              f"the manager authored plan v{plan.get('version')}: "
              f"{len(cand['nodes'])} nodes, {len(cand['edges'])} edges, "
              f"{len(assigns)} assigned", plan=plan_id, nodes=len(cand["nodes"]))
    return plan_id
