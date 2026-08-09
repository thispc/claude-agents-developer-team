"""The PROJECT tenant of the Atlas — `/api/graph/project/{id}`, the same payload
shape `/api/graph/self` serves, composed from a project's own truth.

WHY THIS FILE EXISTS AND `graph.py` DID NOT GROW. `graph.py` is the FLEET's BFF: it
joins the modgraph rows to process-compose, to `launcher.ACTIVE`, to the crew's
record, and every one of those is a platform fact a user's project does not have.
Sharing the composer would have meant a tenant branch in the middle of every join —
which is precisely the thing the seam was built to avoid. What IS shared is the
CONTRACT: the keys of the payload, the keys of a node, the tri-state, the panel's
five sections. `tests/test_project_graph.py` asserts the two payloads against ONE
helper, so the two composers cannot drift.

WHAT A PROJECT REALLY HAS, and therefore what this serves (see projgraph.py for the
mapping in full): a TASK DAG, a TEAM, and a DELIVERABLE. Not services. So:

  every card refuses its switch   `service.control` is False on every node with the
                                  same reason, so the panel prints it instead of
                                  drawing a Start button that would 400.
  the ring is REAL, not invented  a task's test counts come from `tasks.verification`
                                  — the exit code the WORKER recorded outside the
                                  model's reach — and a task with no verification
                                  says "no verification recorded", never "0/2".
  the logs are the task's feed    `db.list_task_events` — what actually happened to
                                  this task, which is the honest analogue of "a tail
                                  of what the process printed".
  the conclusion is the artifact  download / preview / deployment / repo, as generic
                                  `lines` and `links` the Atlas renders for either
                                  tenant. No cluster, no fleet, no crew phase: this
                                  project has none of them, and omitting a key is
                                  how the panel stays honest without a branch.

THE GATE IS OWNERSHIP, not root. The fleet graph describes the repository the server
runs from, so it is an operator power; a project's graph describes the boss's own
project, so it is `owned_project` — the same gate every other `/api/projects/{id}/*`
route uses. `MODULE_GRAPH` still switches the whole surface off, so the flag keeps
meaning exactly what it meant.
"""

from __future__ import annotations

import json
import re

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .. import (bus, config, db, deliverables, deploy, github_client, modgraph,
                preview, projgraph, providers)
from .base import owned_project, router

GRAPH_DOWN = ("the module graph's store is not answering — the map is unavailable, "
              "but nothing else is: your team keeps building and every other view of "
              "this project is unaffected. Check the fleet (data/logs/fleet.log), or "
              "start it with ./run-local.sh.")

# The panel's honest sentence where a service would print its openapi.json.
CONTRACT_NOTE = (
    "a task is a piece of WORK, not a service: what it promises is the deliverable "
    "described in its brief above, not an endpoint. Cards that serve a contract of "
    "their own arrive when a project's modules become services.")
GROUP_CONTRACT_NOTE = (
    "a role room promises nothing of its own — what it holds is the tasks inside it.")
AIM_CONTRACT_NOTE = "this card is the project's brief: what all of it is for."


class AgentBody(BaseModel):
    agent_id: int


class NodeConfig(BaseModel):
    model: str | None = None       # None = leave alone; "" = back to the default
    autonomy: str | None = None


def _gated(project_id: int, request: Request) -> dict:
    """The flag, then ownership. Order matters: with the flag off the surface does
    not exist at all, and answering 403/404 by ownership first would leak that a
    project id is real on a server where nobody can see the graph anyway."""
    if not config.MODULE_GRAPH:
        raise HTTPException(404, "the module graph is disabled")
    return owned_project(project_id, request)


def _known_models() -> set[str]:
    return {m["id"] for p in providers.PROVIDERS.values() for m in p["models"]}


def _plan(project_id: int) -> dict | None:
    """The project's active plan, syncing it from the task DAG first.

    THE SYNC IS LAZY AND IT HAS TO BE. `create_project` seeds and `_create_batch`
    re-syncs, so a project born after this shipped is always current — but every
    project born BEFORE it has tasks and no plan at all, and a screen that told
    those bosses "no plan" would be the feature failing on precisely the projects
    worth looking at. `projgraph.sync` is dict-equality idempotent, so the cost on
    the polled path is one comparison, not a write."""
    projgraph.sync(project_id)
    return modgraph.active_plan(project_id)


def _tasks_by_key(project_id: int) -> dict[str, dict]:
    return {projgraph.task_key(t["id"]): t for t in db.list_tasks(project_id)}


def _verification(task: dict) -> dict:
    try:
        v = json.loads(task.get("verification") or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _tests_of(task: dict) -> dict:
    """A task's evidence, as the payload's test counters.

    THE ONE HONEST SOURCE is `tasks.verification` — the project's own detected
    command, run by the worker PROCESS so the model could not soften the result. A
    task that has none reports total 0, and the card's ring stays grey; inventing a
    suite for it would be the first lie on the screen."""
    v = _verification(task)
    if not v.get("ran"):
        return {"total": 0, "passing": 0, "failing": 0, "advisory": 0,
                "brief": v.get("reason") or "no verification recorded"}
    ok = bool(v.get("ok"))
    return {"total": 1, "passing": 1 if ok else 0, "failing": 0 if ok else 1,
            "advisory": 0,
            "brief": f"{v.get('cmd') or 'the project checks'} "
                     f"{'passed' if ok else 'FAILED'}"}


def _tests_state(task: dict) -> str:
    v = _verification(task)
    if not v.get("ran"):
        return "none"
    return "pass" if v.get("ok") else "fail"


def _health_of(task: dict) -> dict:
    """The tri-state for WORK. See projgraph._STATUS_HEALTH for the whole mapping and
    why `status` is not derivable from `beat` and `tests` alone in this tenant."""
    beat, status, note = projgraph._STATUS_HEALTH.get(
        str(task.get("status") or ""), projgraph._UNKNOWN_HEALTH)
    tests = _tests_state(task)
    if status == "green" and tests == "fail":
        # Delivered, but the harness disagreed. The verdict outranks the status —
        # that is what recording an exit code outside the model's reach was FOR.
        status, note = "yellow", "delivered, but the project's own checks failed"
    return {"beat": beat, "tests": tests, "status": status, "note": note}


_HS_RANK = {"grey": 0, "green": 1, "yellow": 2, "red": 3}


def _rollup(children: list[dict]) -> dict:
    """A role room answering for its tasks: worst-of, with grey (nothing started)
    below green rather than above it — a room of unstarted work is not healthy, it
    is simply not begun, and the card says so by staying unlit."""
    if not children:
        return {"beat": "ok", "tests": "none", "status": "grey",
                "note": "nothing in this room yet"}
    beat = "fail" if any(c["beat"] == "fail" for c in children) else "ok"
    if any(c["tests"] == "fail" for c in children):
        tests = "fail"
    elif any(c["tests"] == "pass" for c in children):
        tests = "pass"
    else:
        tests = "none"
    worst = max(children, key=lambda c: _HS_RANK.get(c["status"], 0))
    done = sum(1 for c in children if c["status"] == "green")
    return {"beat": beat, "tests": tests, "status": worst["status"],
            "note": f"{done}/{len(children)} delivered in this room"}


NO_SWITCH = {"kind": "task", "state": "none", "control": False,
             "reason": projgraph.NO_SWITCH_REASON,
             "remove": {"allowed": False, "reason": projgraph.NO_REMOVE_REASON}}


def _service_of(node: dict) -> dict:
    """Every project card refuses its switch, with the reason ON the card.

    The shape is the fleet's `card_service` shape exactly, because the right-click
    menu and the panel read it the same way for both tenants: `control: false` +
    `reason` disables Start/Stop and prints the sentence instead of letting the
    button fail at the click."""
    kind = {"aim": "brief", "group": "room", "conclusion": "deliverable"} \
        .get(node["node_type"], "task")
    return {**NO_SWITCH, "kind": kind}


def _activity_of(task: dict) -> list[dict]:
    """A card the team is working RIGHT NOW pulses and says what is happening. Real:
    it is the task's own status, not a simulation, and it goes out when the status
    does."""
    if str(task.get("status") or "") not in projgraph.IN_FLIGHT:
        return []
    n = int(task.get("attempts") or 0)
    what = "starting" if task["status"] == "queued" else "building"
    return [{"task": f"{what}{f' — attempt {n}' if n > 1 else ''}",
             "factor": str(task.get("role") or "")}]


def _roster(project_id: int) -> dict[int, dict]:
    return {int(a["id"]): a for a in db.list_agents(project_id)}


def _agent_out(node_key: str, assign: dict | None, task: dict | None,
               roster: dict[int, dict]) -> dict | None:
    """Who works this card. The Atlas CLAIM wins over the scheduler's own pick, and
    the payload says which of the two it is — a boss who assigned somebody and then
    saw a different name would rightly stop trusting the button."""
    claimed = int((assign or {}).get("agent_id") or 0)
    actual = int((task or {}).get("agent_id") or 0)
    agent_id = claimed or actual
    if not agent_id:
        return None
    a = roster.get(agent_id)
    note = ("claimed by you in the Atlas — the scheduler dispatches this task to them"
            if claimed else "picked this task up")
    if claimed and actual and claimed != actual:
        note = ("claimed by you in the Atlas — "
                f"{roster.get(actual, {}).get('name', 'someone else')} worked the "
                "last attempt, and the next one goes to this teammate")
    return {"agent_id": agent_id, "home_id": (a or {}).get("home_id"),
            "name": (a or {}).get("name") or f"agent #{agent_id}",
            "role": (a or {}).get("role") or "", "note": note}


def _mastery_out(m: dict | None, roster: dict[int, dict]) -> dict | None:
    if not m:
        return None
    return {"agent_id": m["agent_id"],
            "name": (roster.get(int(m["agent_id"])) or {}).get("name", ""),
            "runs": m["runs"], "master": m["master"]}


def _deployed_url(project_id: int) -> str:
    """Only what is genuinely running in THIS process. `deploy.status` re-detects the
    spec and reads six kilobytes of log; this payload is polled every six seconds."""
    try:
        r = deploy.RUNNING.get(deploy._slot(project_id))
        if r and r["proc"].poll() is None:
            return deploy.local_preview_url(project_id, r["port"])
    except Exception:
        pass
    return ""


def _conclusion(project: dict, tasks: list[dict]) -> dict:
    """The DELIVERABLE card and its panel: what this project actually produced, and
    every real way to get at it.

    `lines` and `links` are the tenant-neutral half of the conclusion contract — the
    Atlas renders them for whichever tenant sends them, and the fleet sends neither
    (it sends `fleet`, `cluster`, `uptime_s`, `repair` instead). That is how one
    panel serves two goals without a single branch on which one it is looking at."""
    pid = int(project["id"])
    status = str(project.get("status") or "")
    health = ("critical" if status in ("failed", "cancelled") else
              "attention" if status in ("hold", "review") else "ok")
    done = sum(1 for t in tasks if t["status"] == "done")
    failed = sum(1 for t in tasks if t["status"] == "failed")
    lines = [{"label": "status", "value": status or "unknown"},
             {"label": "tasks",
              "value": (f"{done}/{len(tasks)} delivered"
                        + (f" · {failed} failed" if failed else "")) if tasks
                       else "the manager has not planned any yet"}]
    if project.get("sprints", 1) and int(project.get("sprints") or 1) > 1:
        lines.append({"label": "sprint",
                      "value": f"{project.get('sprint', 1)}/{project['sprints']}"})
    links = []
    row = deliverables.latest(pid)
    if row:
        lines.append({"label": "deliverable",
                      "value": f"{row.get('files', 0)} file(s) kept here"})
        links.append({"label": "⬇ Download the deliverable",
                      "url": f"/api/projects/{pid}/download"})
    if preview.preview_root(pid):
        links.append({"label": "↗ Open the static preview", "url": f"/preview/{pid}/"})
    live = _deployed_url(pid)
    if live:
        links.append({"label": "↗ Open the running app", "url": live})
    repo = str(project.get("repo") or "")
    if repo:
        url = github_client.repo_url(repo)
        if url:
            links.append({"label": f"↗ {repo}", "url": url})
    note = ("nothing has been delivered yet — the deliverable appears here the moment "
            "a task lands its work" if not row and not links else "")
    return {"health": health,
            "beat": "fail" if status in ("failed", "cancelled") else "ok",
            "lines": lines, "links": links, "note": note}


# --- the payload ---------------------------------------------------------------

def _unavailable(project: dict) -> dict:
    """Every key a caller reads is present and not one of them is invented — the same
    call `/api/graph/self` makes, for the same reason: "the graph is unavailable" on a
    screen that still shows the project's own conclusion is more use than a red toast
    with nothing behind it."""
    return {"plan": None, "degraded": True, "reason": GRAPH_DOWN,
            "models": sorted(_known_models()),
            "nodes": [], "edges": [], "runs": [], "positions": {},
            "tenant": "project",
            "project": {"id": int(project["id"]), "name": project["name"],
                        "status": project["status"]},
            "conclusion": _conclusion(project, [])}


@router.get("/api/graph/project/{project_id}")
def graph_project(project_id: int, request: Request) -> dict:
    """The whole PROJECT in one payload: the Atlas's single read, tenant two.

    Four sources joined here and nowhere else: the modgraph service's plan rows (the
    shape of the graph), the project's own `tasks` (health, activity, evidence), its
    `agents` (who works what) and its deliverable/preview/deployment (the conclusion).
    Everything live is read from the TASK ROWS at render time rather than stored on
    the plan, so a status change shows up on the next poll without a replan."""
    project = _gated(project_id, request)
    plan = _plan(project_id)
    if not plan:
        return _unavailable(project)
    pid = plan["id"]
    stored = modgraph.nodes(pid)
    if not stored and modgraph.degraded():
        return _unavailable(project)
    by_task = _tasks_by_key(project_id)
    roster = _roster(project_id)
    assign_by = modgraph.assigns(pid)          # ONE call, never one per node
    mastery_by = modgraph.mastery(project_id) or {}
    tasks = list(by_task.values())

    health_by: dict[str, dict] = {}
    for n in stored:
        t = by_task.get(n["key"])
        if t:
            health_by[n["key"]] = _health_of(t)
    kids_of: dict[str, list[str]] = {}
    for n in stored:
        if n.get("parent_key"):
            kids_of.setdefault(n["parent_key"], []).append(n["key"])
    for n in stored:
        if n["node_type"] == "group":
            health_by[n["key"]] = _rollup(
                [health_by[k] for k in kids_of.get(n["key"], []) if k in health_by])

    node_out = []
    for n in stored:
        t = by_task.get(n["key"])
        a = assign_by.get(n["key"]) or {}
        if n["node_type"] == "group":
            kids = [by_task[k] for k in kids_of.get(n["key"], []) if k in by_task]
            counts = {f: sum(_tests_of(k)[f] for k in kids)
                      for f in ("total", "passing", "failing", "advisory")}
            counts["brief"] = (f"{counts['passing']}/{counts['total']} verified"
                               if counts["total"] else "no verification recorded")
            act = [x for k in kids for x in _activity_of(k)]
        elif t:
            counts, act = _tests_of(t), _activity_of(t)
        else:
            counts = {"total": 0, "passing": 0, "failing": 0, "advisory": 0,
                      "brief": ""}
            act = []
        # NO `paths` — a project node has none, and the key stops here either way:
        # the panel's whole promise is that it never names a file.
        node_out.append({
            "key": n["key"], "title": n["title"], "node_type": n["node_type"],
            "parent_key": n.get("parent_key") or "",
            "spec": n.get("spec") or "", "join_mode": n.get("join_mode") or "all_of",
            "tags": n.get("tags") or [],
            "config": {"model": a.get("model") or "", "autonomy": a.get("autonomy") or ""},
            "agent": _agent_out(n["key"], a, t, roster),
            "tests": counts,
            "activity": act,
            "health": health_by.get(n["key"]),
            "service": _service_of(n),
            "mastery": _mastery_out(mastery_by.get(n["key"]), roster),
        })

    shaped = [{"src": e["src_key"], "dst": e["dst_key"], "edge_type": e["edge_type"],
               "contract": e["contract"], "contract_test": e["contract_test"]}
              for e in modgraph.edges(pid)]
    parent_of = {n["key"]: n["parent_key"] for n in stored if n.get("parent_key")}
    if parent_of:
        # Same reconciliation the fleet does, and for the same reason: the top tier
        # cannot miss a crossing, and it cannot show one no child edge backs.
        child = [e for e in shaped if e["src"] in parent_of and e["dst"] in parent_of]
        edges_out = ([e for e in shaped
                      if not (e["src"] in parent_of and e["dst"] in parent_of)]
                     + modgraph.derive_group_edges(child, parent_of))
    else:
        edges_out = shaped
    return {
        "plan": {"id": pid, "version": plan["version"], "kind": plan["kind"],
                 "status": plan["status"], "authored_by": plan["authored_by"],
                 "notes": plan["notes"], "created_at": plan["created_at"]},
        "degraded": modgraph.degraded(),
        "models": sorted(_known_models()),
        "nodes": node_out,
        "edges": edges_out,
        "runs": modgraph.runs(pid, limit=40),
        "positions": modgraph.positions(pid),
        # Tenant-only extras. The fleet payload has neither, and no renderer reads
        # them — they are for the shell (the back link, the title bar).
        "tenant": "project",
        "project": {"id": project_id, "name": project["name"],
                    "status": project["status"], "brief": project["brief"]},
        "conclusion": _conclusion(project, tasks),
    }


def _headline(v: dict) -> str:
    """One line of what the verification said — its own headline when it recorded
    one, else the last line of its output, else why it did not run."""
    if v.get("headline"):
        return str(v["headline"])[:300]
    if not v.get("ran"):
        return str(v.get("reason") or "")[:300]
    lines = [ln.strip() for ln in str(v.get("output") or "").splitlines() if ln.strip()]
    if lines:
        return lines[-1][:300]
    return f"{v.get('cmd') or 'the checks'} exited {v.get('exit_code')}"


# The fields worth reading, per event kind, in the order they read best. Anything
# not listed falls back to the payload's own text — the feed is a record, not a
# schema, and a kind added later must still show up rather than vanish.
_LOG_FIELDS = ("summary", "title", "verdict", "text", "message", "detail", "command",
               "cmd", "name", "status", "model")

# `workspaces/task-19-a1/repo/` and friends. A worker's checkout lives at an
# absolute path on the SERVER, and that path is nobody's business but the
# operator's: it names the operator's home directory, the platform's install
# location and every other task's workspace beside it. What the boss actually wants
# from "Edit .../repo/src/weather.js" is `src/weather.js`, which is a file in the
# boss's own project — so the prefix is cut and the useful half kept.
_WS_CUT = re.compile(r"\S*/workspaces/[^/\s]+/(?:repo/)?")
_ABS_CUT = re.compile(r"(?:/[\w.-]+){3,}/")


def _log_line(e: dict) -> str:
    """One event as one readable line, with the SERVER's filesystem taken out of it."""
    raw = e.get("payload") or ""
    try:
        p = json.loads(raw)
    except (TypeError, ValueError):
        p = raw
    if isinstance(p, dict):
        parts = [f"{p[k]}" for k in _LOG_FIELDS if p.get(k)]
        text = " · ".join(parts) if parts else json.dumps(p, default=str)
    else:
        text = str(p)
    text = " ".join(text.split())
    text = _WS_CUT.sub("", text)
    text = _ABS_CUT.sub("…/", text)
    return f"{e['kind']}: {text}"[:200]


def _task_log(task: dict) -> list[str]:
    """This task's own feed — the honest analogue of "a tail of what the process
    printed", because a task IS a process that ran and this is what it said.

    Bounded to the last 40 rows, and scrubbed of the server's own paths: a project's
    source files are the boss's to see (they are in the Artifacts tab), but the
    absolute checkout path on the machine running the platform is not."""
    return [_log_line(e) for e in db.list_task_events(task["id"])[-40:]]


def _node(project_id: int, key: str) -> tuple[dict, dict, dict | None]:
    """(plan, node row, task row or None) — or 404. One lookup, three callers."""
    plan = _plan(project_id)
    if not plan:
        raise HTTPException(503, GRAPH_DOWN)
    node = next((n for n in modgraph.nodes(plan["id"]) if n["key"] == key), None)
    if not node:
        raise HTTPException(404, f"no card '{key}' in this project's graph")
    tid = projgraph.task_id_of(key)
    task = db.get_task(tid) if tid else None
    if task and int(task["project_id"]) != int(project_id):
        task = None                     # a key from another project's graph
    return plan, node, task


@router.get("/api/graph/project/{project_id}/node/{key}")
def graph_project_node(project_id: int, key: str, request: Request) -> dict:
    """The panel for one project card — the SAME five sections the fleet panel has,
    answered from what a task actually is.

      contract  a task promises a deliverable, not an endpoint, and the payload says
                that in one sentence instead of an empty endpoint list.
      health    the tri-state, plus the note that explains which of the four it is.
      agent     who works it, whether you claimed them, and earned mastery.
      tests     the harness verification the WORKER ran — its command, its verdict,
                its headline. Never the model's account of it.
      logs      this task's own event feed, which is the honest analogue of a tail of
                what a process printed.
    """
    _gated(project_id, request)
    plan, node, task = _node(project_id, key)
    a = modgraph.get_assign(plan["id"], key) or {}
    roster = _roster(project_id)
    v = _verification(task or {})
    if node["node_type"] == "group":
        kids = [t for t in db.list_tasks(project_id)
                if projgraph.role_key(t.get("role") or "unassigned") == key]
        health = _rollup([_health_of(t) for t in kids])
        tests = {"total": sum(1 for t in kids if _verification(t).get("ran")),
                 "ran": sum(1 for t in kids if _verification(t).get("ran")),
                 "passing": sum(1 for t in kids if _verification(t).get("ok")),
                 "failing": sum(1 for t in kids
                                if _verification(t).get("ran")
                                and not _verification(t).get("ok")),
                 "headline": "", "suite": "the checks every task in this room ran"}
        logs_out: list[str] = []
    elif task:
        health = _health_of(task)
        ran = bool(v.get("ran"))
        tests = {"total": 1 if ran else 0, "ran": 1 if ran else 0,
                 "passing": 1 if v.get("ok") else 0,
                 "failing": 1 if ran and not v.get("ok") else 0,
                 "headline": _headline(v),
                 "suite": (f"the project's own `{v.get('cmd')}`, run by the worker"
                           if ran else "this project declares no checks to run")}
        logs_out = _task_log(task)
    else:
        health = {"beat": "ok", "tests": "none", "status": "grey",
                  "note": "this card is part of the frame, not a task"}
        tests = {"total": 0, "ran": 0, "passing": 0, "failing": 0, "headline": "",
                 "suite": ""}
        logs_out = []
    note = (AIM_CONTRACT_NOTE if node["node_type"] == "aim" else
            GROUP_CONTRACT_NOTE if node["node_type"] == "group" else CONTRACT_NOTE)
    return {
        "node": {"key": node["key"], "title": node["title"],
                 "node_type": node["node_type"], "spec": node.get("spec") or "",
                 "join_mode": node.get("join_mode") or "all_of",
                 "parent_key": node.get("parent_key") or "",
                 "tags": node.get("tags") or [],
                 "kind": _service_of(node)["kind"]},
        "contract": None,
        "contract_note": note,
        "health": health,
        "service": _service_of(node),
        "tests": tests,
        "logs": logs_out,
        "trace": modgraph.runs(plan["id"], key, limit=20),
        "edges": [{"src": e["src_key"], "dst": e["dst_key"], "edge_type": e["edge_type"],
                   "contract": e["contract"], "contract_test": ""}
                  for e in modgraph.edges(plan["id"]) if key in (e["src_key"], e["dst_key"])],
        "config": {"model": a.get("model") or "", "autonomy": a.get("autonomy") or ""},
        "models": sorted(_known_models()),
        "agent": _agent_out(key, a, task, roster),
        "mastery": _mastery_out((modgraph.mastery(project_id) or {}).get(key), roster),
    }


@router.get("/api/graph/project/{project_id}/team")
def graph_project_team(project_id: int, request: Request) -> dict:
    """The pool a project's cards are staffed from — which is the PROJECT'S OWN TEAM
    and nothing else.

    The fleet tenant lets an operator re-point its pool at any Studio room, because
    the platform's staff is a choice. A project's is not: the roster was hired for
    this project, its teammates carry this project's notes, and offering somebody
    else's agents here would be offering to dispatch work to a stranger. So `teams`
    is deliberately empty — the Atlas hides its pool switcher when it is, which is
    the honest UI for "there is nothing to switch to"."""
    _gated(project_id, request)
    members = [{"agent_id": int(a["id"]), "name": a["name"], "factor": a["role"]}
               for a in db.list_agents(project_id)]
    # `current` deliberately carries NO world_id/room_id: that is what tells the
    # Atlas's header selector there is nothing to switch to, so it hides itself
    # rather than showing a one-option dropdown whose only action would 400. The
    # fleet's own drill already treats a dropdown with no backend as a finding;
    # this is the same rule from the other side.
    return {"current": {"name": "this project's team"},
            "members": members, "teams": [], "degraded": False,
            "note": "a project's cards are staffed from its own roster — hire on the "
                    "Command view, then claim cards here"}


@router.post("/api/graph/project/{project_id}/node/{key}/agent")
def graph_project_node_agent(project_id: int, key: str, body: AgentBody,
                             request: Request) -> dict:
    """Claim one TASK card for one teammate — and mean it.

    WHAT THIS ACTUALLY ENFORCES, precisely, because a button that implies more than
    it does is worse than none: the claim is stored on the plan (`graph_assign`) and
    `team.assign` consults it ABOVE its own idle/least-loaded round-robin, so the next
    dispatch of this task goes to this teammate. It does NOT move a run already in
    flight (a running session has its prompt and its checkout already), it does not
    change the task's ROLE — the role still writes the prompt — and it does not
    survive the teammate being removed from the project, because the claim is checked
    against the live roster at dispatch time.

    Groups and the frame refuse: work lands on tasks, so a claim on a room would be a
    claim on nothing."""
    _gated(project_id, request)
    plan, node, task = _node(project_id, key)
    if node["node_type"] in ("group", "aim", "conclusion"):
        raise HTTPException(400, f"'{key}' is a {node['node_type']} — work (and so a "
                                 "claim) lands on task cards only")
    roster = _roster(project_id)
    if int(body.agent_id) not in roster:
        raise HTTPException(400, f"agent {body.agent_id} is not on this project's team "
                                 "— hire them on the Command view first")
    a = modgraph.set_assign(plan["id"], key, agent_id=int(body.agent_id))
    who = roster[int(body.agent_id)]
    bus.emit(project_id, task["id"] if task else None, "boss", "graph_agent_assigned",
             {"node": key, "agent_id": int(body.agent_id), "name": who["name"]})
    return {"ok": True, "node": key,
            "agent": {"agent_id": a.get("agent_id"), "home_id": a.get("home_id"),
                      "name": who["name"], "role": who["role"],
                      "note": "claimed by you in the Atlas — the scheduler dispatches "
                              "this task to them"},
            "enforced": "the next dispatch of this task; a run already in flight is "
                        "not moved"}


@router.post("/api/graph/project/{project_id}/node/{key}/config")
def graph_project_node_config(project_id: int, key: str, body: NodeConfig,
                              request: Request) -> dict:
    """Steer one task card: which MODEL it runs on.

    The model is real and enforced — it is written to the task's `pinned_model`, the
    one override `launcher.pick_model` puts above every automatic rule, which is the
    same field the manager's own reassignment uses.

    Autonomy is REFUSED, deliberately. It is a project-wide setting the manager's
    gates read live; storing a per-task value the scheduler would never consult is a
    knob that lies, and one lying knob costs more trust than a missing one."""
    _gated(project_id, request)
    plan, node, task = _node(project_id, key)
    if body.autonomy:
        raise HTTPException(400, "autonomy is a whole-project setting — change it on "
                                 "the project's Command view. A per-task autonomy "
                                 "would not be honoured by the scheduler, so this "
                                 "refuses rather than pretending.")
    if body.model is None:
        return {"ok": True, "node": key,
                "config": {"model": (modgraph.get_assign(plan["id"], key) or {}).get("model") or "",
                           "autonomy": ""}}
    model = body.model.strip()
    known = _known_models()
    if model and model not in known:
        raise HTTPException(400, f"unknown model '{model}' — valid: "
                                 f"{', '.join(sorted(known))}, or '' for the default")
    if not task:
        raise HTTPException(400, f"'{key}' is not a task — only a task runs on a model")
    a = modgraph.set_assign(plan["id"], key, model=model)
    db.update_task(task["id"], pinned_model=model)
    return {"ok": True, "node": key,
            "config": {"model": a.get("model") or "", "autonomy": ""},
            "enforced": "pinned on the task — every future run of it uses this model"}


@router.post("/api/graph/project/{project_id}/resync")
def graph_project_resync(project_id: int, request: Request) -> dict:
    """Re-derive the graph from the task DAG, now.

    The project tenant's answer to the fleet's Replan button, and it costs nothing:
    there is no model call here because a project's graph is DERIVED, not authored —
    the manager plans TASKS, and this is the plan those tasks already are. It exists
    for the case the poll cannot fix on its own: a plan written while the store was
    down."""
    _gated(project_id, request)
    plan_id = projgraph.sync(project_id, announce=True)
    if not plan_id:
        raise HTTPException(503, GRAPH_DOWN)
    plan = modgraph.get_plan(plan_id) or {}
    return {"ok": True, "plan": {"id": plan_id, "version": plan.get("version"),
                                 "authored_by": plan.get("authored_by"),
                                 "status": plan.get("status")}}
