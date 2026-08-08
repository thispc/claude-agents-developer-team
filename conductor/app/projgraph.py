"""projgraph.py — the PROJECT tenant of the Atlas, built from what a project
REALLY has.

THE HONEST SCOPE, first, because everything below follows from it. `docs/
PROJECT_SERVICES.md` describes a future in which a project's modules are running
microservices — their own process, port, contract and Start/Stop switch. **None of
that is built.** A project today has three real things:

    a TASK DAG      rows in `tasks`, with `deps` between them — the manager's plan
    a TEAM          rows in `agents`, hired per role, who pick tasks up
    a DELIVERABLE   the preserved snapshot (and optionally a repo, a preview and a
                    deployment) that the work adds up to

So this module maps exactly those onto the Atlas's existing vocabulary and NOTHING
ELSE. A card is a TASK, not a service; it has no port, no heartbeat, no logs of a
process and no switch, and the payload says so in the card's own `service.reason`
rather than leaving the Start button to fail at the click. The moment
PROJECT_SERVICES lands, `manifest()` grows a services branch and every renderer is
already right — which is the whole reason the Atlas was built devteam-first.

WHAT MAPS ONTO WHAT

    aim          the project's BRIEF. One node, key "aim".
    leaf nodes   one per TASK, key `task-<task id>`. The id and not the seq: seq is
                 a display number, the id is what `deps` point at and what mastery
                 is counted by across plan versions.
    groups       one ROOM per role — but only past ROOM_THRESHOLD tasks. Under it
                 the room is FLAT, the same call the fleet graph made for the same
                 reason: three chambers holding one card each put a click between
                 the boss and the thing he came to look at.
    edges        `tasks.deps`, verbatim: dep → task, edge_type "depends". Plus the
                 frame, DERIVED exactly as the fleet seed derives it — the aim feeds
                 every task nothing else feeds, and every task that feeds nothing
                 reaches the conclusion. A frame that is listed can disagree with the
                 wiring; a frame that is derived cannot.
    conclusion   the DELIVERABLE. One node, key "conclusion", wired in the payload to
                 the artifacts facts (download, preview, deployment, repo).
    tests        a task's own HARNESS VERIFICATION (`tasks.verification`) — the
                 exit code the worker recorded outside the model's reach. Not a
                 mapped pytest file: there is no such thing for a task, and inventing
                 one would be the first lie on the screen.

THE PLAN IS DERIVED, AND RE-DERIVED. `sync()` compares the task DAG to the stored
plan and writes a NEW VERSION only when they differ — the same idempotence rule
`seed_fleet_graph` holds, for the same reason: a plan that is rewritten on every
poll turns the trace into noise, and a plan that is never rewritten stops describing
the project. Assignments and positions carry forward BY KEY across the rewrite,
because they are steering, not plan.
"""

from __future__ import annotations

import json

from . import bus, db, logs, modgraph

# One ROOM per role only past this many tasks. Under it, every task sits in the top
# room in dependency columns — which is the DAG, drawn, and the thing a boss opens
# the Atlas to see. The fleet graph made the identical call ("Seven services fit in
# ONE room, so the fleet room is FLAT"), and a project of five tasks in four roles
# would otherwise be four chambers of one card each.
ROOM_THRESHOLD = 9

TASK_PREFIX = "task-"
ROLE_PREFIX = "role-"

AIM_KEY = "aim"
CONCLUSION_KEY = "conclusion"
CONCLUSION_TITLE = "The Deliverable — what this project produced"

# The tri-state, for WORK rather than for a process. Documented here because it is
# the one place a reader will look for it, and pinned in tests/test_project_graph.py.
#
#   green   the task is delivered and accepted
#   yellow  it is in flight, or waiting on a verdict, or its harness verification
#           failed — something is moving or something wants a look
#   red     it stopped in a way that needs a person (failed, cancelled)
#   grey    it has not started
#
# `beat` and `tests` ride along as the two underlying FACTS, so the card's tooltip
# and the panel can say why: `beat` is "is this work stopped", `tests` is "what did
# the harness verification say" — the exit code the worker recorded, never the
# model's account of it. Unlike the fleet tenant, `status` is NOT derivable from
# those two alone: in-flight is a third thing a process does not have.
_STATUS_HEALTH = {
    "done":               ("ok",   "green",  "delivered"),
    "failed":             ("fail", "red",    "this task stopped — it needs you"),
    "cancelled":          ("fail", "red",    "cancelled"),
    "queued":             ("ok",   "yellow", "picked up — a teammate is starting"),
    "running":            ("ok",   "yellow", "in flight — a teammate is on it now"),
    "pushed":             ("ok",   "yellow", "work is in, waiting on a verdict"),
    "review":             ("ok",   "yellow", "waiting on a verdict"),
    "changes_requested":  ("ok",   "yellow", "sent back for changes"),
    "planned":            ("ok",   "grey",   "not started — waiting on its dependencies"),
}
_UNKNOWN_HEALTH = ("ok", "grey", "not started")

IN_FLIGHT = ("queued", "running")

# The refusals, in one place, so the card, the panel and the right-click menu all
# give the SAME reason — and so the sentence changes once when PROJECT_SERVICES
# lands.
NO_SWITCH_REASON = (
    "a task is work, not a process — there is nothing here to start or stop. "
    "Modules with their own switch arrive when a project's modules become "
    "services; today the switch that matters is the project's own Cancel.")
NO_REMOVE_REASON = (
    "this card is a TASK — the manager owns the DAG. Ask it to drop the task in "
    "the Manager tab; a card removed here would reappear the moment the plan is "
    "re-derived from the tasks.")


# --- keys ---------------------------------------------------------------------

def task_key(task_id: int) -> str:
    return f"{TASK_PREFIX}{int(task_id)}"


def task_id_of(key: str) -> int | None:
    """The task id behind a node key, or None when the key is not a task's."""
    k = str(key or "")
    if not k.startswith(TASK_PREFIX):
        return None
    try:
        return int(k[len(TASK_PREFIX):])
    except ValueError:
        return None


def role_key(role: str) -> str:
    return f"{ROLE_PREFIX}{str(role or 'unassigned').strip().lower()}"


# --- the manifest -------------------------------------------------------------

def _node_type(role: str) -> str:
    """Which GLYPH a task's card wears. The Atlas already draws 📦 code, 🔬 research
    and 🗄 data; a tester's card looking like a builder's is a small lie the existing
    vocabulary does not require us to tell."""
    r = str(role or "").lower()
    if any(w in r for w in ("test", "qa", "review", "research", "analy", "audit")):
        return "research"
    if any(w in r for w in ("data", "db", "sql", "etl")):
        return "data"
    return "code"


def _rooms_wanted(tasks: list[dict]) -> bool:
    roles = {str(t.get("role") or "") for t in tasks}
    return len(tasks) >= ROOM_THRESHOLD and len(roles) > 1


def manifest(project: dict, tasks: list[dict]) -> dict:
    """The project's whole graph as one comparable value — the same shape
    `seed.self_manifest()` produces for the fleet, so `sync`'s idempotence is dict
    equality and nothing cleverer."""
    rooms = _rooms_wanted(tasks)
    brief = str(project.get("brief") or "").strip()
    nodes: list[dict] = [{
        "key": AIM_KEY, "title": str(project.get("name") or "the project"),
        "node_type": "aim",
        "spec": brief[:1200] or "no brief was written for this project",
        "join_mode": "all_of", "parent_key": "", "tags": [], "paths": []}]

    if rooms:
        seen: list[str] = []
        for t in tasks:
            r = str(t.get("role") or "unassigned")
            if r not in seen:
                seen.append(r)
        for r in seen:
            n = sum(1 for t in tasks if str(t.get("role") or "unassigned") == r)
            nodes.append({
                "key": role_key(r), "title": r.replace("_", " "), "node_type": "group",
                "spec": f"everything the {r.replace('_', ' ')} role owns on this "
                        f"project — {n} task{'' if n == 1 else 's'}",
                "join_mode": "all_of", "parent_key": "", "tags": ["role"], "paths": []})

    ids = {int(t["id"]) for t in tasks}
    for t in tasks:
        role = str(t.get("role") or "unassigned")
        nodes.append({
            "key": task_key(t["id"]),
            "title": str(t.get("title") or f"task {t.get('seq') or t['id']}"),
            "node_type": _node_type(role),
            "spec": str(t.get("description") or "")[:1200],
            "join_mode": "all_of",
            "parent_key": role_key(role) if rooms else "",
            "tags": [role, f"#{t.get('seq') or t['id']}"],
            "paths": []})
    nodes.append({
        "key": CONCLUSION_KEY, "title": CONCLUSION_TITLE, "node_type": "conclusion",
        "spec": "everything the team delivered, kept here and downloadable — plus "
                "the preview, the deployment and the repo when this project has them",
        "join_mode": "all_of", "parent_key": "", "tags": [], "paths": []})

    # The wiring: tasks.deps, verbatim. A dep pointing at a task that no longer
    # exists is dropped rather than drawn — an arrow into nothing is the one thing
    # the Atlas promised never to render.
    wiring: list[dict] = []
    for t in tasks:
        try:
            deps = json.loads(t.get("deps") or "[]")
        except (TypeError, ValueError):
            deps = []
        for d in sorted({int(x) for x in deps if isinstance(x, int)} & ids):
            if d == int(t["id"]):
                continue
            wiring.append({
                "src": task_key(d), "dst": task_key(t["id"]), "edge_type": "depends",
                "contract": {"kind": "task",
                             "rule": "the scheduler does not dispatch this task "
                                     "until that one is delivered"},
                "contract_test": ""})

    # The frame, DERIVED — never listed. Same rule as the fleet seed: fed by the aim
    # when nothing else feeds it, reaching the deliverable when it feeds nothing.
    keys = [task_key(t["id"]) for t in tasks]
    fed = {e["dst"] for e in wiring}
    feeds = {e["src"] for e in wiring}
    frame = [{"src": AIM_KEY, "dst": k, "edge_type": "depends", "contract": {},
              "contract_test": ""} for k in keys if k not in fed]
    frame += [{"src": k, "dst": CONCLUSION_KEY, "edge_type": "depends", "contract": {},
               "contract_test": ""} for k in keys if k not in feeds]
    if not keys:
        # A project whose manager has not planned yet: an aim, a deliverable, and one
        # honest arrow between them. The Atlas has something to show the second the
        # project exists, which is the whole point of seeding on creation.
        frame = [{"src": AIM_KEY, "dst": CONCLUSION_KEY, "edge_type": "depends",
                  "contract": {}, "contract_test": ""}]
    return {"nodes": nodes, "edges": frame + wiring, "tests": []}


def _stored_manifest(plan_id: int) -> dict:
    return modgraph._manifest_of(plan_id)


# --- syncing ------------------------------------------------------------------

def sync(project_id: int, *, announce: bool = False) -> int:
    """Make the project's active plan equal the task DAG. Returns the plan id, or 0.

    Idempotent: an unchanged DAG writes nothing. A changed one writes a NEW VERSION
    (the store never edits a plan), carrying the operator's per-node assignments and
    steering forward by key — those are steering, not plan, and losing them on every
    `add_tasks` would make the Atlas's one real verb worthless.

    Degraded (the modgraph service down) → 0 with a warn, and every caller carries
    on: the graph is observability, and a project must never fail to plan because
    the map is unavailable."""
    project = db.get_project(project_id)
    if not project:
        return 0
    tasks = db.list_tasks(project_id)
    man = manifest(project, tasks)
    cur = modgraph.active_plan(project_id)
    if cur is None and modgraph.degraded():
        return 0
    before: set[str] = set()
    carry_assigns: dict = {}
    carry_pos: dict = {}
    if cur:
        if _stored_manifest(cur["id"]) == man:
            return int(cur["id"])
        before = {n["key"] for n in modgraph.nodes(cur["id"])}
        keys = {n["key"] for n in man["nodes"]}
        carry_assigns = {k: v for k, v in (modgraph.assigns(cur["id"]) or {}).items()
                         if k in keys and any(v.get(f) for f in
                                              ("agent_id", "home_id", "model", "autonomy"))}
        carry_pos = {k: v for k, v in (modgraph.positions(cur["id"]) or {}).items()
                     if k in keys}
    plan = modgraph.import_plan(
        project_id, kind="run", authored_by="manager",
        notes="derived from the project's task DAG",
        nodes=man["nodes"], edges=man["edges"], tests=man["tests"],
        assigns=carry_assigns, positions=carry_pos)
    if not plan:
        logs.warn("lifecycle", "project_graph_unwritten",
                  "the project's graph could not be written — the Atlas will show "
                  "the previous version until the store answers again",
                  project=project_id)
        return 0
    plan_id = int(plan["id"])
    if announce:
        _announce(project_id, man, before)
    return plan_id


def _announce(project_id: int, man: dict, before: set[str]) -> None:
    """`graph_node_planned` per NEW node, in dependency order — the same event the
    crew's authoring pass emits, which is what makes the Atlas's staged reveal play
    as the manager plans instead of after it."""
    order = _topo(man)
    fresh = [k for k in order if k not in before]
    by_key = {n["key"]: n for n in man["nodes"]}
    for i, key in enumerate(fresh):
        n = by_key.get(key) or {}
        bus.emit(project_id, None, "graph", "graph_node_planned",
                 {"key": key, "title": n.get("title", ""),
                  "node_type": n.get("node_type", ""),
                  "factor": (n.get("tags") or [""])[0], "i": i, "n": len(fresh)})
    bus.emit(project_id, None, "graph", "graph_plan_ready",
             {"nodes": len(man["nodes"]), "authored_by": "manager"})


def _topo(man: dict) -> list[str]:
    """Dependency order over the manifest's own edges; anything in a cycle falls out
    at the end rather than being dropped."""
    keys = [n["key"] for n in man["nodes"]]
    indeg = {k: 0 for k in keys}
    out: dict[str, list[str]] = {k: [] for k in keys}
    for e in man["edges"]:
        if e["src"] in indeg and e["dst"] in indeg:
            out[e["src"]].append(e["dst"])
            indeg[e["dst"]] += 1
    ready = [k for k in keys if not indeg[k]]
    order: list[str] = []
    while ready:
        k = ready.pop(0)
        order.append(k)
        for nxt in out[k]:
            indeg[nxt] -= 1
            if not indeg[nxt]:
                ready.append(nxt)
    return order + [k for k in keys if k not in order]


# --- the claim: what an assignment actually MEANS ------------------------------

def claims(project_id: int) -> dict[str, int]:
    """{node key: agent id} — who the boss claimed each card for, in the Atlas.

    Degraded → {}, so a store outage never silently re-deals the team: `team.assign`
    falls straight back to the round-robin it used before this existed."""
    plan = modgraph.active_plan(project_id)
    if not plan:
        return {}
    out: dict[str, int] = {}
    for key, a in (modgraph.assigns(plan["id"]) or {}).items():
        if a.get("agent_id"):
            out[key] = int(a["agent_id"])
    return out


def claimed_agent(task: dict) -> dict | None:
    """The teammate the boss claimed this task's card for, as an `agents` row — or
    None.

    THIS IS THE ENFORCEMENT POINT, and it is deliberately one function: `team.assign`
    consults it above its own round-robin, so a claim made on the Atlas decides who
    the scheduler actually dispatches. The agent must still belong to THIS project —
    a stale claim (the teammate was replaced, the roster changed) is ignored rather
    than obeyed, because dispatching a task to somebody else's teammate is worse than
    dispatching it to the least-loaded one.

    Cross-role on purpose: if the boss put the design director on a backend task, the
    boss meant it. The role still decides the prompt, so the work is not misdescribed.
    """
    try:
        pid = int(task.get("project_id") or 0)
        if not pid:
            return None
        agent_id = claims(pid).get(task_key(task["id"]))
        if not agent_id:
            return None
        agent = db.get_agent(int(agent_id))
        if agent and int(agent["project_id"]) == pid:
            return agent
    except Exception:
        return None
    return None


def claim_lines(project_id: int) -> list[str]:
    """One line per claimed card, for the manager's `status` tool: who the boss said
    owns which task. The manager cannot dispatch, so this is information it plans
    AROUND rather than an instruction it executes — which is exactly what it is."""
    try:
        by_key = claims(project_id)
        if not by_key:
            return []
        names = {a["id"]: a["name"] for a in db.list_agents(project_id)}
        out = []
        for key, agent_id in sorted(by_key.items()):
            tid = task_id_of(key)
            t = db.get_task(tid) if tid else None
            if not t or int(t["project_id"]) != int(project_id):
                continue
            who = names.get(int(agent_id), f"agent #{agent_id}")
            out.append(f"task {t.get('seq') or t['id']} '{t['title']}' — the boss "
                       f"claimed this for {who} in the Atlas")
        return out
    except Exception:
        return []
