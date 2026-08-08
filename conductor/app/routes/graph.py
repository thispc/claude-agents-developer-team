"""The fleet graph's HTTP surface — /api/graph/self, the platform's own graph.

SINCE P6 EVERY CARD IS A SERVICE. The rows come from the modgraph service, the
LIVE half comes from process-compose (readiness, restarts, logs) and from the
conductor's own registries (launcher.ACTIVE, deploy.RUNNING, sandbox.status), and
this file joins them. Three consequences worth stating where someone will read
them before editing:

  the panel keeps the box closed  `/node/{key}` answers with the service's own
                                  CONTRACT (its committed /openapi.json, rendered
                                  as an endpoint list), its own /health document,
                                  who works it, what its own suite last said, and
                                  a tail of what it actually printed. It carries
                                  no path, no module and no line of code, because
                                  the owner's decree is "I don't wanna understand
                                  what's inside" and a panel that leaks a file
                                  name has already broken it.
  `paths` never reaches the wire  a node's boundary manifest still exists — it is
                                  what matches the crew's live work onto a card
                                  and what scopes a verify — and it is stripped
                                  from every payload, right here, by construction.
  the ephemeral cards are joined  live workers and deployed apps are cards without
                                  rows: they are read at render time and vanish
                                  when the process does. A plan version per worker
                                  spawn would turn the trace into noise.

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

SINCE P5 THIS FILE IS THE BFF AND ONLY THE BFF. The rows it reads live in the
modgraph service; `modgraph.py` is the client. Every `/api/graph/*` path is
unchanged and must stay so — the Atlas hardcodes them — and so is every payload
shape, with one addition: `degraded`. Two things deliberately did NOT follow the
tables out:

  the verify RUNNER   `graph_self_verify` shells out to the repo's own pytest
                      over real files in THIS process's checkout. The service
                      says which files and stores the verdict; a store that
                      spawned pytest in another process's working tree would
                      have imported that tree's whole world.
  the composition     who an agent id IS (the crew's record), what the crew is
                      touching right now, the cluster switch, the probes — all
                      conductor facts, joined onto the service's rows here.
"""

from __future__ import annotations

import re
import sys
import time

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .. import bus, config, db, fleet, modgraph, modgraph_health, providers, shell
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
    sub: str = ""                  # a sub-switch INSIDE the card (the conductor's crew)


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


GRAPH_DOWN = ("the module graph's store is not answering — the map is unavailable, "
              "but nothing else is: the crew keeps building and every other screen "
              "is unaffected. Check the fleet (data/logs/fleet.log), or start it "
              "with ./run-local.sh.")


def _self_plan() -> dict:
    plan = modgraph.active_plan(0)
    if not plan:
        # The boot seed is guarded and can fail without blocking startup; healing
        # here keeps the screen alive after such a boot — same deterministic seed.
        modgraph.seed_fleet_graph()
        plan = modgraph.active_plan(0)
    if not plan:
        raise HTTPException(503, GRAPH_DOWN if modgraph.degraded()
                            else "no plan for the platform graph")
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
                   for f in files for p in (n.get("paths") or [])):
                out.setdefault(n["key"], []).append(
                    {"task": str(t.get("title") or "")[:200],
                     "factor": str(t.get("factor") or "")})
    return out


def _trace_out(rows: list[dict]) -> list[dict]:
    """Trace rows with every path-shaped token taken out of their detail.

    A run's detail is the verify's headline, and pytest's last line is sometimes a
    warnings-summary URL or the file that failed. Both are the box being opened by
    accident on a screen whose whole promise is that it stays closed — and the
    trace is rendered verbatim, so the scrub belongs here rather than in a
    reviewer's memory."""
    return [{**r, "detail": fleet.scrub_text(r.get("detail") or "")} for r in rows]


def _fleet_summary() -> dict:
    """The Artifact card's fleet line: managed processes declared, and how many the
    fleet manager reports up. `visible: false` means the manager itself is not
    answering — the legacy boot has no fleet API at all and its services are fine."""
    st = fleet.pc_states()
    declared = sorted(fleet.managed())
    if st is None:
        return {"visible": False, "declared": len(declared), "running": 0,
                "api": fleet.api_base()}
    up = [n for n in declared if (st.get(n) or {}).get("running")]
    return {"visible": True, "declared": len(declared), "running": len(up),
            "api": fleet.api_base(),
            "down": sorted(set(declared) - set(up))}


def _test_counts(rows: list[dict]) -> dict:
    suites = [t for t in rows if t["kind"] == "suite"]
    passing = sum(1 for t in suites if t["status"] == "passing")
    failing = sum(1 for t in suites if t["status"] in ("failing", "error"))
    return {"total": len(suites), "passing": passing, "failing": failing,
            "advisory": len(suites) - passing - failing}


def _graph_unavailable() -> dict:
    """The payload when the store cannot be read: every key a caller reads is
    present and not one of them is invented.

    HONEST-EMPTY BEATS AN ERROR HERE, and it is the opposite call from the
    Studio's (which 503s when the lifeworld is down, because rendering an empty
    world would let the next save persist it). Nothing the Atlas draws is ever
    saved back, so the failure mode is only what the operator is told — and "the
    graph is unavailable" on a screen that still shows the crew's phase, the
    uptime and the cluster is more use at 3am than a red toast with no screen
    behind it. `degraded` is what the Atlas keys its banner off; `plan: null` is
    what stops it pretending version 1 exists."""
    st = _crew_snapshot()
    crew_state = st.get("state") or {}
    return {
        "plan": None, "degraded": True, "reason": GRAPH_DOWN,
        "models": sorted(_known_models()),
        "nodes": [], "edges": [], "runs": [], "positions": {},
        "conclusion": {"health": "unknown",
                       "repair": {"phase": crew_state.get("phase", ""),
                                  "sprint": crew_state.get("sprint_no", 0)},
                       "beat": "ok", "uptime_s": _uptime_s(),
                       "boot_sha": "", "head_sha": "", "cluster": _cluster_state()},
    }


@router.get("/api/graph/self")
def graph_self(request: Request) -> dict:
    """The whole FLEET in one payload: the Atlas's single read.

    Three sources, joined here and nowhere else: the modgraph service's rows (what
    the fleet IS — one card per services.yaml entry), process-compose's live state
    (what each process is doing), and the conductor's own registries (the workers
    and deployed apps that exist only while they run). A GROUP node answers for its
    children — and since P6 the only groups are the two ROOMS, so a chamber's
    rollup is genuinely a rollup of live things.

    Eight round trips to the store (plan, nodes, edges, tests, assigns, runs,
    positions, mastery) plus ONE to the fleet manager, ~10ms on localhost.
    `assigns` is one call and not one per node on purpose — see modgraph.assigns."""
    _gated(request)
    plan = modgraph.active_plan(0)
    if not plan:
        modgraph.seed_fleet_graph()
        plan = modgraph.active_plan(0)
    if not plan:
        if modgraph.degraded():
            return _graph_unavailable()
        raise HTTPException(503, "no plan for the platform graph")
    pid = plan["id"]
    stored = modgraph.nodes(pid)
    tests = modgraph.tests(pid)
    by_node: dict[str, list[dict]] = {}
    for t in tests:
        by_node.setdefault(t["node_key"], []).append(t)
    st = _crew_snapshot()
    # Groups included: a room carries a boundary of its own now (the pool's is the
    # worker script and the launcher), so work on it must light ITS card up too.
    activity = _activity(stored, st)
    # The live children, joined on: a worker under the pool, a deployed app under
    # the apps room. Only under a room the PLAN actually has — a card whose parent
    # is not on the wall would be a card in no room at all.
    stored_keys = {n["key"] for n in stored}
    nodes = stored + [c for c in fleet.ephemeral_cards()
                      if c["parent_key"] in stored_keys]
    kids_of: dict[str, list[str]] = {}
    for n in nodes:
        if n.get("parent_key"):
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
    # Health: a leaf's beat is the FLEET's answer for it — process-compose's
    # readiness plus the service's own /health — and a group rolls its children up:
    # red if any child red, else yellow if any yellow, else green.
    beat_by = fleet.beats(nodes)
    health_by: dict[str, dict] = {
        n["key"]: modgraph_health.health_of(
            beat_by.get(n["key"], "ok"),
            modgraph_health.tests_state(by_node.get(n["key"], [])))
        for n in nodes}
    for n in nodes:
        if n["node_type"] == "group":
            # A room's own health AND its children's. The pool has a real check of
            # its own (can it write a workspace) and a real suite of its own, and on
            # an idle box it has no children at all — rolling up only the children
            # would report a broken pool as green because nothing was in it.
            health_by[n["key"]] = modgraph_health.rollup(
                [health_by[n["key"]]]
                + [health_by[k] for k in kids_of.get(n["key"], []) if k in health_by])
    # One call, not one per node: fourteen round trips on a payload the Atlas
    # polls is a latency regression dressed up as a faithful port.
    assign_by = modgraph.assigns(pid)
    mastery_by = modgraph_health.mastery(0) or {}
    node_out = []
    for n in nodes:
        a = assign_by.get(n["key"]) or {}
        if n["node_type"] == "group":
            # Its OWN rows plus its children's: a room has a suite of its own (the
            # pool's is every test that drives the launcher) and a card that hid it
            # behind an empty child list would say "no tests mapped" about
            # seventeen of them.
            kid_counts = [_test_counts(by_node.get(k, []))
                          for k in [n["key"]] + kids_of.get(n["key"], [])]
            counts = {f: sum(c[f] for c in kid_counts)
                      for f in ("total", "passing", "failing", "advisory")}
            act, seen = list(activity.get(n["key"], [])), set()
            for item in act:
                seen.add((item["task"], item["factor"]))
            for k in kids_of.get(n["key"], []):
                for item in activity.get(k, []):
                    sig = (item["task"], item["factor"])
                    if sig not in seen:
                        seen.add(sig)
                        act.append(item)
        else:
            counts = _test_counts(by_node.get(n["key"], []))
            act = activity.get(n["key"], [])
        # NO `paths`. A card is a service, and the whole promise of the panel is
        # that the box stays closed — so the boundary manifest stops here, on the
        # server, where the activity matcher and the verify scoper use it.
        node_out.append({
            "key": n["key"], "title": n["title"], "node_type": n["node_type"],
            "parent_key": n.get("parent_key") or "",
            "spec": n.get("spec") or "", "join_mode": n.get("join_mode") or "all_of",
            "tags": n.get("tags") or [],
            "config": {"model": a.get("model") or "", "autonomy": a.get("autonomy") or ""},
            "agent": _agent_out(a, names),
            "tests": counts,
            "activity": act,
            "health": health_by.get(n["key"]),
            "service": fleet.card_service(n["key"], n),
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
    parent_of = {n["key"]: n["parent_key"] for n in nodes if n.get("parent_key")}
    group_keys = {n["key"] for n in nodes if n["node_type"] == "group"}
    tier = [e for e in shaped if e["src"] in group_keys and e["dst"] in group_keys]
    child = [e for e in shaped if e["src"] in parent_of and e["dst"] in parent_of]
    edges_out = ([e for e in shaped
                  if not (e["src"] in group_keys and e["dst"] in group_keys)]
                 + modgraph.derive_group_edges(child, parent_of, group_edges=tier))
    # The live workers' one real edge, joined on with the cards themselves: a
    # worker reports THROUGH the conductor and nowhere else, which is the worker
    # contract and the only dependency that genuinely crosses a room wall in this
    # topology. It is what puts a door chip on a worker's card.
    core = next((n["key"] for n in stored
                 if fleet.card_kind(n["key"], n) == "core"), "")
    if core:
        edges_out += [
            {"src": n["key"], "dst": core, "edge_type": "interface",
             "contract": {"kind": "worker",
                          "rule": "a worker reports only through /internal/* with the "
                                  "shared token, and only on the task it was given"},
             "contract_test": ""}
            for n in nodes if fleet.card_kind(n["key"], n) == "worker"]
    return {
        "plan": {"id": pid, "version": plan["version"], "kind": plan["kind"],
                 "status": plan["status"], "authored_by": plan["authored_by"],
                 "notes": plan["notes"], "created_at": plan["created_at"]},
        # A read that failed PART WAY through is the case this catches: the plan
        # answered and the nodes did not. Empty-because-unreadable and
        # empty-because-that-is-the-graph must never look the same on screen.
        "degraded": modgraph.degraded(),
        "models": sorted(_known_models()),
        "nodes": node_out,
        "edges": edges_out,
        "runs": _trace_out(modgraph.runs(pid, limit=40)),
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
                       "cluster": _cluster_state(),
                       # The fleet in one line, for the Artifact card: how many of
                       # the registry's managed processes are actually up. `visible`
                       # false is the legacy boot (or a dead fleet API) — "I cannot
                       # see the switches", which is NOT "nothing is running".
                       "fleet": _fleet_summary()},
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
    # NO `-q` HERE. pytest.ini already carries one, and a second makes it `-qq`,
    # which suppresses the summary line entirely — so for as long as this runner
    # asked for quiet twice, the "headline" it stored was pytest's last line with
    # no verdict in it at all (the warnings-summary footer, a documentation URL).
    # The panel showed that URL where a result belongs. One -q is quiet; two is
    # silent about the only thing being asked.
    res = shell.sh(sys.executable, "-m", "pytest", *files,
                   cwd=config.ROOT, timeout=VERIFY_TIMEOUT_S)
    out = (res.stdout or "") + "\n" + (res.stderr or "")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    # THE VERDICT LINE, not merely the last one. pytest's final line is often the
    # warnings-summary footer ("-- Docs: https://…"), which says nothing about the
    # run and, on the panel, is a URL where a result should be. Prefer the last
    # line that actually counts something; fall back to the last line only when
    # the run produced no verdict at all (a collection crash, a missing binary).
    verdicts = [ln for ln in lines
                if any(w in ln for w in ("passed", "failed", "error", "no tests ran"))]
    headline = (verdicts or lines or ["no output"])[-1][:300]
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


def _card(pid: int, key: str) -> tuple[dict | None, bool]:
    """One card by key: (node, is_a_plan_row). A live worker or a deployed app is a
    card with no row, so every verb that acts on a card looks here rather than only
    in the plan — otherwise the Stop button on a running app would 404."""
    node = next((n for n in modgraph.nodes(pid) if n["key"] == key), None)
    if node:
        return node, True
    live = next((c for c in fleet.ephemeral_cards() if c["key"] == key), None)
    return live, False


CONTRACT_NOTES = {
    "worker-pool": "workers are spawned processes, not a service: their contract is "
                   "the environment the launcher hands them and the three "
                   "/internal/* requests they answer with.",
    "worker": "a worker reports only through /internal/* with the shared token, and "
              "only on the task it was actually given.",
    "sandbox": "the sandbox is a whole second copy of this platform — its contract "
               "is this one, served on its own port.",
    "apps": "each deployed app serves whatever its own team built; there is no one "
            "contract for the room.",
    "app": "this is a project's own app — its contract is the project's, not the "
           "platform's.",
}


@router.get("/api/graph/self/node/{key}")
def graph_self_node(key: str, request: Request) -> dict:
    """The panel: everything a click should know about one card — AND NOTHING THAT
    IS INSIDE IT.

    The owner's decree is the shape of this payload: "I don't wanna understand
    what's inside — that's the vibe-coding part." So what comes back is what a
    service PROMISES and what it is DOING, five things and no sixth:

      contract   its own committed /openapi.json, fetched through the fleet and
                 rendered as an endpoint list. The one honest answer to "what is
                 this thing" that is not a description of its insides.
      health     the tri-state the card glows with, plus the service's own
                 readiness document verbatim (`ok`, and which check failed).
      agent      who works it, and whether they have earned mastery of it.
      tests      what its OWN suite last said. Counts and headlines only — the
                 file names live in the store and stop there.
      logs       a tail of what the process actually printed, from the fleet's
                 unified log.

    No path, no module, no line of code appears anywhere in it, and the UI pin in
    tests/test_module_graph_ui.py fails if one ever does."""
    _gated(request)
    plan = _self_plan()
    pid = plan["id"]
    node, stored = _card(pid, key)
    if not node:
        raise HTTPException(404, f"no card '{key}' in the fleet")
    a = modgraph.get_assign(pid, key) if stored else None
    kind = fleet.card_kind(key, node)
    rows = modgraph.tests(pid, key) if stored else []
    ran = [t for t in rows if t["status"] in ("passing", "failing", "error")]
    headline = next((t["last_result"] for t in reversed(rows) if t["last_result"]), "")
    beat = fleet.beat_of(key, node, fleet.pc_states())
    return {
        "node": {"key": node["key"], "title": node["title"],
                 "node_type": node["node_type"], "spec": node.get("spec") or "",
                 "join_mode": node.get("join_mode") or "all_of",
                 "parent_key": node.get("parent_key") or "",
                 "tags": node.get("tags") or [], "kind": kind},
        # The contract, or an honest sentence about why this card has none.
        "contract": fleet.contract(key),
        "contract_note": CONTRACT_NOTES.get(kind, ""),
        "health": {**modgraph_health.health_of(beat, modgraph_health.tests_state(rows)),
                   "detail": fleet.service_health(key)},
        "service": fleet.card_service(key, node),
        # COUNTS AND HEADLINES, never file names: the suite is the card's own, and
        # which files it is made of is the store's business.
        "tests": {"total": len(rows), "ran": len(ran),
                  "passing": sum(1 for t in ran if t["status"] == "passing"),
                  "failing": sum(1 for t in ran if t["status"] in ("failing", "error")),
                  # SCRUBBED: pytest's last line occasionally names the file that
                  # failed, and a file name is the box being opened by accident.
                  "headline": fleet.scrub_text(headline)[:300],
                  "suite": (f"the {key} service's own suite" if kind == "service"
                            else f"the suite that exercises {key}")},
        "logs": fleet.pc_logs(key, 40) if kind in ("core", "service") else [],
        "trace": _trace_out(modgraph.runs(pid, key, limit=20)) if stored else [],
        "edges": [{"src": e["src_key"], "dst": e["dst_key"], "edge_type": e["edge_type"],
                   "contract": e["contract"], "contract_test": ""}
                  for e in modgraph.edges(pid) if key in (e["src_key"], e["dst_key"])],
        "config": {"model": (a or {}).get("model") or "",
                   "autonomy": (a or {}).get("autonomy") or ""},
        "models": sorted(_known_models()),
        "agent": _agent_out(a, _agent_names()),
        "mastery": _mastery_out((modgraph_health.mastery(0) or {}).get(key),
                                _agent_names()),
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
    """Start or stop ONE CARD, for real.

    A managed service goes through the fleet manager (`POST /process/start/{name}`,
    `PATCH /process/stop/{name}`) — the same switch `pc stop knowledge` throws, so
    the button and the terminal cannot disagree. The sandbox goes through
    sandbox.start/stop; a deployed app through deploy.stop; the IT crew is a
    SUB-SWITCH on the conductor's card (`{"action": "stop", "sub": "repair"}`),
    because it is a kv toggle inside the process serving this request.

    A card that genuinely has no switch answers 400 WITH THE REASON, and the reason
    names what would have to happen instead. That contract is older than this phase
    and it is why the button is worth anything: an operator plans around what a stop
    button claims."""
    _gated(request)
    plan = _self_plan()
    node, _stored = _card(plan["id"], key)
    if not node:
        raise HTTPException(404, f"no card '{key}' in the fleet")
    if body.action not in ("start", "stop"):
        raise HTTPException(400, "action must be start or stop")
    try:
        svc = fleet.card_set(key, body.action, node, sub=body.sub.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    bus.emit(0, None, "graph", "graph_service",
             {"node": key, "action": body.action, "sub": body.sub,
              "state": svc.get("state")})
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
        live, _ = _card(pid, key)
        if live:
            raise HTTPException(400, fleet.REMOVE_EPHEMERAL_REASON)
        raise HTTPException(404, f"no node '{key}' in the active plan")
    if node["node_type"] in ("aim", "conclusion"):
        raise HTTPException(400, f"the {node['node_type']} is the plan's frame — a plan "
                                 "without one is not a plan")
    # SINCE P6 A CARD IS A SERVICE, and the fleet's membership is services.yaml.
    # A "remove" that quietly dropped the card would leave the process running, the
    # port bound and the registry unchanged — a map that had stopped describing the
    # box. The refusal names the real act instead, and Replace ▸ Module is how you
    # ask the crew to perform it.
    if not (fleet.card_service(key, node).get("remove") or {}).get("allowed", False):
        raise HTTPException(400, fleet.REMOVE_SERVICE_REASON)
    kids = [n["key"] for n in nodes if n["parent_key"] == key]
    if kids:
        raise HTTPException(400, f"'{key}' still has children ({', '.join(sorted(kids))}) "
                                 "— remove or re-parent them first")
    survivors = [n for n in nodes if n["key"] != key]
    out_nodes = []
    for n in survivors:
        paths = n["paths"]
        if n["node_type"] == "group" and n["key"] == node["parent_key"]:
            # The group invariant must survive the removal: its boundary is the union
            # of the children that REMAIN, not a memory of one that is gone.
            paths = sorted({p for c in survivors
                            if c["parent_key"] == n["key"] for p in c["paths"]})
        out_nodes.append({**{k: n[k] for k in ("key", "title", "node_type", "spec",
                                               "join_mode", "parent_key", "tags")},
                          "paths": paths})
    out_edges = [{"src": e["src_key"], "dst": e["dst_key"], "edge_type": e["edge_type"],
                  "contract": e["contract"], "contract_test": e["contract_test"]}
                 for e in modgraph.edges(pid) if key not in (e["src_key"], e["dst_key"])]
    out_tests = [{"node": t["node_key"], "path": t["path"], "kind": t["kind"],
                  "source": t["source"], "status": t["status"]}
                 for t in modgraph.tests(pid) if t["node_key"] != key]
    assign_by = modgraph.assigns(pid)
    out_assigns = {n["key"]: assign_by[n["key"]] for n in survivors
                   if n["key"] in assign_by
                   and any(assign_by[n["key"]].get(f)
                           for f in ("agent_id", "home_id", "model", "autonomy"))}
    pos = modgraph.positions(pid)
    pos.pop(key, None)
    # ONE WRITE, for the reason the authoring pass has one: a plan is a whole
    # thing. Rebuilding it row by row over a wire would mean a version that could
    # be ACTIVATED with half its edges — and the operator's evidence for the
    # removal would be a graph that had silently lost its contracts.
    new_plan = modgraph.import_plan(
        0, kind=plan["kind"], authored_by=u["username"],
        notes=f"node '{key}' removed by {u['username']}",
        nodes=out_nodes, edges=out_edges, tests=out_tests,
        assigns=out_assigns, positions=pos)
    if not new_plan:
        raise HTTPException(503, GRAPH_DOWN)
    new_id = int(new_plan["id"])
    notes = db.kv_get("graph:notes:0") or []
    notes.append({"ts": time.time(),
                  "note": f"The operator removed the module '{key}' ({node['title']}) "
                          "from the plan. Do not reintroduce it unless the operator "
                          "asks; fold anything it still covered into other modules."})
    db.kv_set("graph:notes:0", notes[-12:])
    # No re-read: the import returned the plan row it wrote.
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
