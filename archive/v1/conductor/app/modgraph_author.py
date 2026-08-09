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

WHY THE STORE STAYS MODEL-FREE — AND WHY THIS FILE DID NOT MOVE IN P5. The store
is pinned at source level to never touch a model (the no-LLM-in-the-scheduler
verdict). Authorship therefore lives HERE: this module spends the call and hands
the store plain rows. The plan it writes is a new immutable version
(authored_by='manager') that supersedes the seed; an answer identical to the
active plan writes nothing at all, because a "new version" with the same bytes
would be history-noise pretending to be work.

P5 made the store a service and left this file exactly where it was, which is
what broke the one genuine import cycle in the whole decomposition. Look at what
these 370 lines actually need: `providers.complete` and the owner's resolved
settings, `repair`'s live roster, its headroom meter and its ledger, `tuning`'s
builder-model knob, the bus, and the operator's directive notes. That is the
conductor, five times over. It is a BRAIN — it reads a tree and a roster and
decides on a decomposition — and the thing it writes into is a STORE. Moving it
would have dragged the crew and the provider credentials into a service whose job
is six tables; leaving it turns `modgraph_author → modgraph` into an HTTP call and
`modgraph → repair` into nothing at all.

WHEN THE STORE IS DOWN, THIS PASS REFUSES BEFORE IT SPENDS. The inventory, the
current plan and the mechanical test mapping all come over the wire now, and an
authoring call made without them would produce a plan from a blank tree and then
fail to write it — a real model call, spent, for nothing. `should_author()` stays
true, so the next sprint tries again.

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

from . import bus, config, db, logs, modgraph, modgraph_health, providers, repair, tuning
from . import repair_builder as rb

NODE_TYPES = ("aim", "group", "code", "research", "data", "conclusion")
EDGE_TYPES = ("depends", "interface", "data", "artifact")

AUTHOR_SYSTEM = (
    "You are the hidden manager of the devteam platform's own IT crew. You are handed the "
    "platform's FLEET — every card is a real service with its own process, port, contract "
    "and database, taken from the registry (services.yaml) — the plan currently on the "
    "wall, and your team roster. Author the plan YOUR team will work.\n"
    "THE CARDS ARE FIXED. You may NOT invent a card, drop one, or rename a key: a card "
    "that is not in the registry is not a process anybody is running, and the registry is "
    "changed by editing a repository file, not by you. What you DO decide is how the fleet "
    "reads and who works it: each card's title and spec, its tags, the typed edges and the "
    "contracts on them, and the specialist assigned to each one.\n"
    "Return ONLY one JSON object, no prose:\n"
    '{"modules": [{"key": <EXACTLY a key from the inventory>, "title": <short>, '
    '"spec": <what this service IS and promises, 1-3 sentences>, '
    '"join": "all_of"|"any_of", "tags": [<strings>]}],\n'
    ' "edges": [{"src": <key>, "dst": <key>, '
    '"type": "depends"|"interface"|"data"|"artifact", '
    '"contract": {"rule": <the rule both sides honour>}}],\n'
    ' "assignments": {<card key>: <factor id>}}\n'
    "Rules: every key must appear in the inventory and every inventory key should appear "
    "in your answer; node types, parents and boundaries are the registry's and are not "
    "yours to set, so do not send them; an edge naming a card that does not exist is "
    "discarded; assignments go on the SERVICE cards (never the aim, the Artifact or a "
    "room) and every value must be one of the roster's factor ids. Describe what a service "
    "PROMISES, never what is inside it — the operator has said he does not want to know.")


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

    SINCE P6 THE CARD SET IS NOT NEGOTIABLE. The inventory is the registry, and a
    card that is not in it is not a process anybody is running — so a key the
    manager invented is dropped with a log, a card it forgot is restored from the
    manifest, and node_type, parent and boundary are taken from the manifest
    whatever the answer said. What the manager genuinely decides survives intact:
    the title, the spec, the tags, the edges and their contracts, and who works
    each card. It may be creative about how the fleet READS; it does not get to
    be creative about what the fleet IS.

    Everything else that can be checked still is — an unknown edge type falls back
    to the default, an edge naming a card that does not exist is discarded, an
    assignment to a factor not on the roster is discarded."""
    base = {n["key"]: n for n in man["nodes"]}
    order = list(base)
    invented: list[str] = []
    authored: dict[str, dict] = {}
    for m in obj.get("modules") or []:
        if not isinstance(m, dict):
            continue
        key = re.sub(r"[^a-z0-9-]+", "-", str(m.get("key") or "").lower()).strip("-")[:40]
        if not key or key in authored:
            continue
        if key not in base:
            invented.append(key)
            continue
        join = str(m.get("join") or "all_of")
        authored[key] = {
            "title": str(m.get("title") or base[key]["title"])[:140],
            "spec": str(m.get("spec") or base[key]["spec"])[:600],
            "join_mode": join if join in ("all_of", "any_of") else "all_of",
            "tags": [str(t)[:40] for t in (m.get("tags") or [])][:8] or base[key]["tags"],
        }
    if invented:
        logs.warn("sprint", "graph_cards_invented",
                  f"the manager named {len(invented)} card(s) that are not services in "
                  "the registry — dropped: " + "; ".join(invented[:5]), n=len(invented))
    # The structural half comes from the manifest, ALWAYS: key, type, parent and
    # boundary describe a process the fleet runs, and the manager describing them
    # differently would not change the process, only the map of it.
    nodes = [{**base[k], **authored.get(k, {})} for k in order]

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
    # The roster is given to the model as "id: Name — brief", and models answer
    # with the NAME often enough that dropping those threw away real assignments.
    # A name (or a differently-cased id) maps back to its id; only a value that
    # matches nothing on the roster is discarded.
    by_alias = {f["id"].lower(): f["id"] for f in fs}
    by_alias.update({str(f.get("name") or "").strip().lower(): f["id"]
                     for f in fs if str(f.get("name") or "").strip()})

    def _fid(v: str) -> str:
        return v if v in fids else by_alias.get(v.strip().lower(), "")

    # Assignments land on the SERVICE cards only: the frame is not work, and a
    # room is worked through the things inside it, so a specialist pinned to
    # either would be a specialist pinned to nothing.
    leaf_keys = {n["key"] for n in nodes
                 if n["node_type"] not in ("aim", "conclusion", "group")}
    assigns = {str(k): _fid(str(v)) for k, v in (obj.get("assignments") or {}).items()
               if str(k) in leaf_keys and _fid(str(v))}
    return nodes, edges, assigns


def _candidate_manifest(nodes: list[dict], edges: list[dict]) -> dict:
    """The would-be plan in modgraph._manifest_of's comparable shape, with the test
    mapping derived MECHANICALLY — a service's suite is its own directory, every
    other card's is parsed from the repo suite's imports, and the manager authors
    neither. The one builder, asked over the wire, so an authored plan and the
    offline seed can never disagree about which suite belongs to which card."""
    by_key = {n["key"]: n["paths"] for n in nodes}
    tmap = {path: keys & set(by_key)
            for path, keys in modgraph._tests_for_nodes(by_key).items()}
    tmap = {path: keys for path, keys in tmap.items() if keys}
    edge_list = [{"src": e["src"], "dst": e["dst"], "edge_type": e["edge_type"],
                  "contract": e["contract"], "contract_test": ""} for e in edges]
    test_list = sorted(
        [{"node": key, "kind": "suite", "path": path}
         for path, keys in tmap.items() for key in keys],
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
    if not man.get("nodes"):
        # The store could not be read (or has nothing to say about the tree). The
        # completion below is a REAL model call on the owner's quota, and it would
        # be spent authoring a decomposition of an empty inventory into a store
        # that cannot receive it. should_author() stays true; the next sprint retries.
        logs.warn("sprint", "graph_author_skipped",
                  "the module graph's inventory is unavailable — authoring skipped "
                  "rather than spending a call on a plan with nowhere to land")
        return None
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
    # Directive-style notes the operator left on the graph (a removed module, above
    # all) — the manager must SEE them or the next authoring pass quietly undoes an
    # explicit human act. Consumed once an authoring call has actually read them.
    op_notes = db.kv_get("graph:notes:0") or []
    notes_txt = ""
    if op_notes:
        notes_txt = ("\n\nOPERATOR NOTES (explicit directives — honour them):\n"
                     + "\n".join(f"- {str(n.get('note') or '')[:300]}"
                                 for n in op_notes[-12:]))
    model = str(tuning.get("repair_builder_model"))
    try:
        raw = await providers.complete(
            "anthropic", model, AUTHOR_SYSTEM,
            f"INVENTORY (the tree as it is):\n{inventory}\n\n"
            f"CURRENT PLAN:\n{current}\n\nTEAM ROSTER:\n{roster}{notes_txt}\n\n"
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
    if op_notes:
        db.kv_set("graph:notes:0", [])     # the manager has read them; a note is not a law
    if cur and cur_man == cand:
        _stamp(cur["id"])
        logs.info("sprint", "graph_plan_unchanged",
                  "the manager re-read the tree and kept the plan as it stands",
                  plan=cur["id"])
        return int(cur["id"])

    agents = (info or {}).get("agents") or {}
    final: dict[str, int] = {}
    for key, fid in assigns.items():
        hid = agents.get(fid)
        if hid:
            final[key] = int(hid)
    # MASTERY OUTRANKS RESHUFFLING: an agent with enough verified ok runs on a node
    # keeps it, whatever the manager proposed — continuity on a module is worth more
    # than a tidy re-deal, and the trace (not this pass) is what earns it back.
    #
    # `or {}` reads as "nothing has been earned", and that would be a LIE if the store
    # were unreachable — so it cannot be reached that way: the inventory is read from
    # the same service at the top of this function and an unreadable one returns above,
    # before a single token is spent. This is the display default, not the outage path.
    leaf_keys = {n["key"] for n in cand["nodes"]
                 if n["node_type"] not in ("aim", "conclusion", "group")}
    for key, m in (modgraph_health.mastery(0) or {}).items():
        if not m["master"] or key not in leaf_keys:
            continue
        if final.get(key) != int(m["agent_id"]):
            logs.info("sprint", "graph_master_kept",
                      f"'{key}' keeps its master (agent {m['agent_id']}, {m['runs']} "
                      "verified runs) over the manager's pick",
                      node=key, master=int(m["agent_id"]), proposed=final.get(key, 0))
            final[key] = int(m["agent_id"])

    # ONE WRITE. The nodes, the edges, the mechanical test mapping, the assignments
    # and the activation are a single transaction in the service. Before P5 this was
    # fifty-odd row calls inside one process; over a wire that is fifty chances to be
    # interrupted with the plan already ACTIVE and half its edges missing — which the
    # Atlas would render as a repository that had lost its contracts.
    plan = modgraph.import_plan(
        0, kind="template", authored_by="manager",
        notes="authored by the crew's manager from the live inventory",
        nodes=cand["nodes"], edges=cand["edges"],
        tests=[dict(t, status="mapped") for t in cand["tests"]],
        assigns={key: {"agent_id": hid} for key, hid in final.items()})
    if not plan:
        logs.warn("sprint", "graph_plan_unwritten",
                  "the manager authored a plan the store would not take — nothing "
                  "changed, and no stamp is written, so the next sprint tries again")
        return None
    plan_id = int(plan["id"])

    by_key = {n["key"]: n for n in cand["nodes"]}
    order = _topo(cand)
    for i, key in enumerate(order):
        n = by_key[key]
        bus.emit(0, None, "graph", "graph_node_planned",
                 {"key": key, "title": n["title"], "node_type": n["node_type"],
                  "factor": assigns.get(key, ""), "i": i, "n": len(order)})
    # No re-read: the import returned the plan row it wrote, in the one call.
    bus.emit(0, None, "graph", "graph_plan_ready",
             {"plan": plan_id, "version": plan.get("version"),
              "nodes": len(cand["nodes"]), "authored_by": "manager"})
    _stamp(plan_id)
    logs.info("sprint", "graph_plan_authored",
              f"the manager authored plan v{plan.get('version')}: "
              f"{len(cand['nodes'])} nodes, {len(cand['edges'])} edges, "
              f"{len(assigns)} assigned", plan=plan_id, nodes=len(cand["nodes"]))
    return plan_id
