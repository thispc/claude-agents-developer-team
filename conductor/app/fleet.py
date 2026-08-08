"""fleet.py — the fleet's live state, and every card's real switch.

P6 made the Atlas's cards SERVICES. This module is what that cost: one place that
knows what the fleet is (`data/fleet_topology.json`, the generator's output), what
it is doing right now (process-compose's REST API on 8899), and how to start or
stop each part of it for real.

It replaces two things that were honest about in-process code and became lies the
moment the code became processes:

  modgraph_health.PROBES    a per-module function that imported the module it
                            checked and asserted something about it. A service in
                            another process cannot be probed that way, and the
                            probe that pretended to (import the package, grep it)
                            was already the wrong answer by P4. The beat is now
                            process-compose's readiness plus the service's own
                            `GET /health`, asked through the SAME shim every verb
                            uses — a card and the caller beside it can never
                            disagree about what "up" means.
  modgraph_health.SERVICES  the honest-switch table, whose whole content was
                            "almost nothing here has an off switch". Everything
                            does now: `POST /process/start/{name}` and `PATCH
                            /process/stop/{name}`.

WHAT IS STILL HONESTLY REFUSED, and why that is not the old table coming back:

  the conductor   pc would stop it perfectly well — and take the browser tab
                  asking for it with it, mid-request, along with every other
                  screen. So the card refuses with the real instruction (Ctrl-C in
                  the terminal running ./run-local.sh) and carries the one switch
                  inside it that IS a runtime switch: the IT crew's kv toggle, as
                  a SUB-SWITCH on the card.
  the worker pool a worker is spawned when there is work and reaped when the work
                  ends. There is no switch, only work, and a Stop button on it
                  would be a button that pretends.
  the apps room   one card per deployed app, each with its own real Stop. The room
                  itself is a room.

THE FLEET API MAY BE ABSENT, and that is a supported boot: `./run-local.sh
--legacy` starts every service as a plain child of the shell, so nothing answers
on 8899. Then `pc_states()` is None — not {} — and every card falls back to its
own `GET /health` for the beat and reports `control: false` with the reason. A
missing fleet manager must read as "I cannot see the switches", never as "the
services are down".

Offline-safe throughout: localhost only, no credentials, hard 2s timeouts, and
one ~3s cache in front of the state sweep because /api/graph/self is polled.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from . import config

MANAGED_KINDS = ("core", "service")

# The state sweep is one HTTP call and the payload is polled every few seconds;
# 3s is short enough that a Stop looks instant and long enough that a poll does
# not spend a round trip per card.
STATE_TTL_S = 3.0
PC_TIMEOUT = 2.0
# A committed spec does not change while the process runs. Cached per service so
# opening the same panel twice does not re-read a 40KB document.
SPEC_TTL_S = 60.0

_DATA = Path(config.ROOT) / "data"

# Tests inject an httpx transport here (a MockTransport standing in for the fleet
# API); the client code path stays identical. None = a real socket to 127.0.0.1.
_TRANSPORT: httpx.BaseTransport | None = None

_topo: dict = {"mtime": None, "doc": {}}
_states_cache: dict = {"ts": 0.0, "by_name": None}
_spec_cache: dict = {}


# --- the topology: what the fleet IS ------------------------------------------

def topology() -> dict:
    """data/fleet_topology.json — the generator's output, re-read when it changes.

    services.yaml is the fallback for a checkout where the generator has not run
    yet (a bare `pytest` on a fresh clone). One reader, so the gateway, the
    conductor's service doors and the Atlas can never disagree about who is in
    the fleet."""
    path = _DATA / "fleet_topology.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is not None and _topo["mtime"] == mtime:
        return _topo["doc"]
    doc: dict = {}
    if mtime is not None:
        try:
            doc = json.loads(path.read_text())
        except Exception:
            doc = {}
    if not doc.get("services"):
        doc = _from_registry()
    _topo.update(mtime=mtime, doc=doc)
    return doc


def _from_registry() -> dict:
    """services.yaml, shaped like the generated topology. The legacy-boot path."""
    try:
        import yaml
        reg = yaml.safe_load((config.ROOT / "services.yaml").read_text()) or {}
    except Exception:
        return {"services": {}}
    host = (reg.get("defaults") or {}).get("host", "127.0.0.1")
    out: dict = {}
    for name, s in (reg.get("services") or {}).items():
        managed = s.get("kind") in MANAGED_KINDS and s.get("port")
        entry: dict = {"kind": s.get("kind"), "managed": bool(managed),
                       "ui": bool(s.get("ui"))}
        if managed:
            entry.update(port=int(s["port"]),
                         url=f"http://{host}:{s['port']}",
                         health=s.get("health_path", "/health"),
                         depends_on=sorted(s.get("depends_on") or []),
                         doors=sorted(s.get("doors") or []),
                         knobs=sorted(s.get("knobs") or []),
                         peers=sorted(s.get("peers") or []),
                         public=bool(s.get("public")))
        else:
            entry["managed_by"] = s.get("managed_by", "")
            if "port_range" in s:
                entry["port_range"] = list(s["port_range"])
        out[name] = entry
    return {"generated_from": "services.yaml (the generator has not run)",
            "fleet_api": {"port": int((reg.get("fleet_api") or {}).get("port", 8899)),
                          "host": host,
                          "token_file": "data/tokens/fleet-api.token"},
            "services": out}


def services() -> dict:
    return topology().get("services", {}) or {}


def managed() -> dict[str, dict]:
    """{name: entry} for the processes the fleet manager actually owns."""
    return {n: s for n, s in services().items() if s.get("kind") in MANAGED_KINDS}


def is_managed(name: str) -> bool:
    return name in managed()


def service_url(name: str) -> str:
    return str((services().get(name) or {}).get("url") or "").rstrip("/")


def service_token(name: str) -> str:
    tok = _DATA / "tokens" / f"{name}.token"
    try:
        return tok.read_text().strip()
    except OSError:
        return ""


# --- process-compose's REST API -----------------------------------------------
#
# Pinned against the running binary (v1.120.0, verified in P6 against the vendored
# tools/bin/process-compose, not against its docs):
#
#   GET   /processes                            {"data": [ {name,status,is_ready,…} ]}
#   GET   /process/{name}                       one of those
#   POST  /process/start/{name}                 {"name": …}
#   PATCH /process/stop/{name}                  {"name": …}
#   GET   /process/logs/{name}/{end}/{limit}    {"logs": [ … ]}  — end 0 = the tail
#
# Auth is the header `X-Pc-Token-Key` (NOT a bearer token — that answers 401), and
# the token is the file run-local.sh passes to --token-file.

PC_HEADER = "X-Pc-Token-Key"


def api() -> dict:
    f = topology().get("fleet_api") or {}
    return {"host": f.get("host", "127.0.0.1"), "port": int(f.get("port", 8899)),
            "token_file": f.get("token_file", "data/tokens/fleet-api.token")}


def api_base() -> str:
    a = api()
    return f"http://{a['host']}:{a['port']}"


def api_token() -> str:
    try:
        return (config.ROOT / api()["token_file"]).read_text().strip()
    except OSError:
        return ""


def _client(timeout: float = PC_TIMEOUT) -> httpx.Client:
    return httpx.Client(base_url=api_base(), timeout=timeout, transport=_TRANSPORT,
                        headers={PC_HEADER: api_token()})


def pc_states(refresh: bool = False) -> dict[str, dict] | None:
    """{process name: {state, ready, running, restarts, pid, uptime, exit_code}},
    or None when the fleet manager is not answering.

    NONE IS NOT {}. A legacy boot has no fleet API at all and its services are
    perfectly healthy; a fleet API that is down while the services run is the same
    picture. Callers must read None as "the switches are not visible", and every
    one of them below does."""
    now = time.time()
    if not refresh and now - _states_cache["ts"] < STATE_TTL_S:
        return _states_cache["by_name"]
    out: dict[str, dict] | None
    try:
        with _client() as c:
            r = c.get("/processes")
            r.raise_for_status()
            rows = (r.json() or {}).get("data") or []
        out = {}
        for row in rows:
            name = str(row.get("name") or "")
            if not name:
                continue
            out[name] = {
                "state": str(row.get("status") or ""),
                # pc reports "Ready" / "Not Ready" / "" (no probe declared).
                "ready": str(row.get("is_ready") or ""),
                "running": bool(row.get("is_running")),
                "restarts": int(row.get("restarts") or 0),
                "pid": int(row.get("pid") or 0),
                "uptime": str(row.get("system_time") or ""),
                "exit_code": int(row.get("exit_code") or 0),
            }
    except Exception:
        out = None
    _states_cache.update(ts=now, by_name=out)
    return out


def pc_start(name: str) -> dict:
    with _client(5.0) as c:
        r = c.post(f"/process/start/{name}")
        r.raise_for_status()
        _states_cache["ts"] = 0.0          # the next read must see the truth
        return r.json() if r.content else {}


def pc_stop(name: str) -> dict:
    with _client(10.0) as c:
        r = c.request("PATCH", f"/process/stop/{name}")
        r.raise_for_status()
        _states_cache["ts"] = 0.0
        return r.json() if r.content else {}


def pc_logs(name: str, limit: int = 60) -> list[str]:
    """The last `limit` lines the process wrote, from the fleet's unified log.

    End offset 0 IS the tail (verified against the binary — it returns the same
    lines as `tail data/logs/fleet.log` filtered to the process). Never raises: a
    panel that cannot show logs shows none, it does not fail to open."""
    try:
        with _client(3.0) as c:
            r = c.get(f"/process/logs/{name}/0/{max(1, min(int(limit), 400))}")
            r.raise_for_status()
            return [str(x) for x in ((r.json() or {}).get("logs") or [])]
    except Exception:
        return []


def available() -> bool:
    return pc_states() is not None


# --- the contract: a service's own committed spec, rendered readably ----------

def contract(name: str) -> dict | None:
    """{title, version, endpoints: [{method, path, summary}]} from the service's
    own `GET /openapi.json`, or None.

    THE BLACK BOX STAYS CLOSED. This is the only thing the panel says about what
    is inside a service, and it is deliberately the one thing that is not inside
    it: the contract it promises to answer. No file, no module, no line of code —
    the operator's own words were "I don't wanna understand what's inside"."""
    if not is_managed(name):
        return None
    hit = _spec_cache.get(name)
    if hit and time.time() - hit["ts"] < SPEC_TTL_S:
        return hit["doc"]
    url = service_url(name)
    if not url:
        return None
    try:
        with httpx.Client(base_url=url, timeout=PC_TIMEOUT, transport=_TRANSPORT,
                          headers={"X-Service-Token": service_token(name)}) as c:
            r = c.get("/openapi.json")
            r.raise_for_status()
            spec = r.json() or {}
    except Exception:
        return None
    endpoints = []
    for path, ops in sorted((spec.get("paths") or {}).items()):
        for method, op in sorted((ops or {}).items()):
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            endpoints.append({"method": method.upper(), "path": path,
                              "summary": str((op or {}).get("summary") or "")[:200]})
    doc = {"title": str((spec.get("info") or {}).get("title") or name),
           "version": str((spec.get("info") or {}).get("version") or ""),
           "endpoints": endpoints}
    _spec_cache[name] = {"ts": time.time(), "doc": doc}
    return doc


def service_health(name: str) -> dict | None:
    """A managed service's OWN `GET /health` document — the readiness JSON every
    service in the fleet promises (SERVICE_CONTRACT rule 2), verbatim.

    This is the second thing the panel shows, and it is deliberately the service's
    own words rather than a summary of them: "ok: false, checks: {db: true, table:
    false}" tells an operator which half is broken without telling him one line of
    what is inside."""
    if not is_managed(name):
        return None
    url, entry = service_url(name), services().get(name) or {}
    if not url:
        return None
    try:
        with httpx.Client(base_url=url, timeout=PC_TIMEOUT, transport=_TRANSPORT,
                          headers={"X-Service-Token": service_token(name)}) as c:
            r = c.get(str(entry.get("health") or "/health"))
            got = r.json() if r.status_code == 200 else {"ok": False,
                                                         "status": r.status_code}
    except Exception:
        return None
    return _no_paths(got)


def scrub_text(text: str) -> str:
    """One line of a service's own output, with anything path-shaped taken out.

    The panel shows a suite's HEADLINE — pytest's last line, "9 passed in 0.12s" —
    and that line occasionally carries a file name (`FAILED services/x/tests/…`) or
    a URL. Both are the box being opened by accident, and both are noise in a
    sentence whose whole value is the counts. One token-level pass, so what
    survives is exactly the part the operator asked for."""
    out = [("…" if ("/" in w or "\\" in w) else w) for w in str(text or "").split()]
    return " ".join(out)


def _no_paths(doc: dict) -> dict:
    """The readiness document with every FILE PATH taken out of it.

    Services say where their database is (`"db": "data/knowledge.db"`) because
    that is useful to the operator of the SERVICE. The Atlas is not that: the
    panel's whole promise is that the box stays closed, and a path is the most
    reliable way to open one. So it is dropped here, at the one place a service's
    own words cross into the panel, rather than left to whoever writes the next
    /health to remember."""
    out = {}
    for k, v in (doc or {}).items():
        if k in ("db", "path", "db_path", "file", "dir", "root"):
            continue
        if isinstance(v, str) and ("/" in v or "\\" in v):
            continue
        out[k] = _no_paths(v) if isinstance(v, dict) else v
    return out


# --- the beat: pc readiness AND the service's own /health ---------------------

# The shim each service is reached through. The card asks the same door every
# caller asks, never a private copy of the URL: a probe with its own idea of where
# a service lives is a probe that can report green while every call fails.
_SHIM_HEALTH: dict[str, str] = {
    "knowledge": "knowledge", "usage": "usage", "notify": "notify",
    "watch": "logs", "lifeworld": "lifeworld_client", "modgraph": "modgraph",
}


def _shim_ok(name: str) -> bool:
    mod = _SHIM_HEALTH.get(name)
    if not mod:
        return True
    try:
        from importlib import import_module
        return bool(import_module(f".{mod}", __package__).health())
    except Exception:
        return False


def _pc_ok(name: str, st: dict | None) -> bool:
    """process-compose's own verdict on one process. A process with a readiness
    probe must be Ready, not merely Running — the whole reason the fleet declares
    probes is that a uvicorn that has bound its port is not yet a service."""
    if st is None:
        return True                         # not visible; the shim answers alone
    row = st.get(name)
    if row is None:
        return True                         # not a pc process (legacy boot, tests)
    if not row["running"]:
        return False
    return row["ready"] in ("Ready", "")    # "" = no readiness probe declared


def beat_of(key: str, node: dict, st: dict | None) -> str:
    """"ok" | "fail" for one card. Every branch is a REAL check of the thing the
    card names — there is no "the row exists so it must be fine" left anywhere."""
    kind = card_kind(key, node)
    if kind in ("core", "service"):
        return "ok" if (_pc_ok(key, st) and _shim_ok(key)) else "fail"
    if kind == "worker-pool":
        # A pool that cannot write a workspace is a pool that cannot clone, which
        # is a worker in name only.
        try:
            config.WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
            return "ok" if os.access(config.WORKSPACES_DIR, os.W_OK) else "fail"
        except OSError:
            return "fail"
    if kind == "sandbox":
        from . import sandbox
        try:
            return "fail" if sandbox.status().get("died") else "ok"
        except Exception:
            return "fail"
    if kind == "worker":
        return "ok" if node.get("live", {}).get("alive", True) else "fail"
    if kind == "app":
        return "ok" if node.get("live", {}).get("alive", True) else "fail"
    return "ok"                             # apps room, aim, conclusion


def beats(nodes: list[dict]) -> dict[str, str]:
    """{key: beat} for EVERY node, one state sweep for all of them.

    Groups included, which is new in P6 and follows from what a group now is: the
    worker pool is not an abstract layer that only exists through its children —
    it is a real thing with a real check (can it write a workspace at all), and on
    a box with nothing building it has no children to roll up from. So a room gets
    its own beat AND rolls its children's in; see routes/graph.py."""
    st = pc_states()
    out: dict[str, str] = {}
    for n in nodes:
        key = str(n["key"])
        try:
            out[key] = beat_of(key, n, st)
        except Exception:
            out[key] = "fail"               # a check that raises IS a failed beat
    return out


# --- what kind of card is this ------------------------------------------------

def card_kind(key: str, node: dict | None = None) -> str:
    """core | service | worker-pool | sandbox | apps | worker | app | frame | orphan.

    Read from the topology by KEY, which is the node key precisely because the key
    IS the service name — the stable identity across plan versions, so assignment,
    mastery and test rows survive a replan and a services.yaml reorder alike.

    ORPHAN is the one interesting answer: a plan row naming nothing in the
    registry. It is the only card the operator may actually REMOVE, because it is
    the only one where removing the row does not leave a process running behind
    a map that has stopped describing the box."""
    if node and node.get("node_type") in ("aim", "conclusion"):
        return "frame"
    if node and node.get("fleet_kind"):
        return str(node["fleet_kind"])
    entry = services().get(key) or {}
    kind = entry.get("kind") or ""
    if kind in MANAGED_KINDS:
        return kind
    if kind:                                 # ephemeral / external, by registry
        return key if key in ("worker-pool", "sandbox", "apps") else "orphan"
    return "orphan" if node is not None else "frame"


# --- the ephemeral children: live workers, deployed apps ----------------------

def worker_cards() -> list[dict]:
    """One card per LIVE worker, from launcher.ACTIVE.

    Not stored rows. A plan version per worker spawn would turn the trace into
    noise and the plan into a log, so the pool's children are joined onto the
    payload the same way health and activity are: read at render time, gone when
    the worker is reaped."""
    from . import launcher
    out = []
    for slot, w in sorted(launcher.ACTIVE.items(), key=lambda kv: str(kv[0])):
        proc = w.get("proc")
        alive = True
        try:
            if proc is not None and hasattr(proc, "returncode"):
                alive = proc.returncode is None
        except Exception:
            alive = True
        out.append({
            "key": f"worker:{slot}", "title": str(w.get("title") or f"task {slot}")[:70],
            "node_type": "code", "parent_key": "worker-pool", "fleet_kind": "worker",
            "spec": (f"{w.get('role') or 'worker'} · {w.get('model') or 'default model'} "
                     f"· project {w.get('project_id')}"),
            "tags": ["ephemeral", str(w.get("role") or "worker")],
            "live": {"alive": alive, "ref": str(w.get("ref") or ""),
                     "started_at": float(w.get("started_at") or 0),
                     "task_id": w.get("task_id"), "project_id": w.get("project_id")},
        })
    return out


def app_cards() -> list[dict]:
    """One card per deployed app the conductor is running, from deploy.RUNNING —
    the EXTERNAL cards, probed on their real bind-reserved ports."""
    from . import deploy
    out = []
    for slot, r in sorted(deploy.RUNNING.items(), key=lambda kv: str(kv[0])):
        proc = r.get("proc")
        alive = True
        try:
            alive = proc is None or proc.poll() is None
        except Exception:
            alive = True
        port = int(r.get("port") or 0)
        pid_s, branch = str(slot), ""
        if "@" in pid_s:
            pid_s, branch = pid_s.split("@", 1)
        out.append({
            "key": f"app:{slot}", "title": f"app #{pid_s}" + (f" ({branch})" if branch else ""),
            "node_type": "code", "parent_key": "apps", "fleet_kind": "app",
            "spec": (f"a project's own app, running on port {port}"
                     + (f" from branch {branch}" if branch else "")),
            "tags": ["external", str((r.get("spec") or {}).get("kind") or "app")],
            "live": {"alive": alive, "port": port, "branch": branch,
                     "project_id": int(pid_s) if pid_s.isdigit() else 0,
                     "started_at": float(r.get("started") or 0)},
        })
    return out


def ephemeral_cards() -> list[dict]:
    return worker_cards() + app_cards()


# --- the switches -------------------------------------------------------------

NO_PC = ("the fleet manager is not answering on "
         "{base} — start the fleet with ./run-local.sh (the --legacy boot runs the "
         "services as plain children of one shell and has no switches at all)")

CONDUCTOR_REASON = (
    "the conductor is the process serving this screen: process-compose would stop "
    "it perfectly well, and take the Atlas, the dashboard and this request with it. "
    "Stop it where you started it — Ctrl-C in the terminal running ./run-local.sh. "
    "The switch that IS inside it is the IT crew, below.")

POOL_REASON = ("a worker is spawned when there is work and reaped when the work "
               "ends — there is no switch here, only work")

WORKER_REASON = ("a worker exits when its task does; cancel the task from the "
                 "project's board rather than killing the process under it")

APPS_REASON = ("this room holds one card per deployed app, and each of those has "
               "its own real Stop — enter it")

FRAME_REASON = "the aim and the Artifact are the plan's frame, not processes"

REMOVE_SERVICE_REASON = (
    "a card here IS a service, and the fleet's membership is services.yaml. "
    "Removing this card would mean deleting its block from the registry and "
    "re-running the generator — a repository change, reviewed like any other, not "
    "a click. File it with Replace ▸ Module if the service should go.")

REMOVE_EPHEMERAL_REASON = ("this card is a live process, not a plan row — it "
                           "disappears on its own when the work ends")

ORPHAN_REASON = ("this card names nothing in services.yaml — the fleet has no such "
                 "process, so there is nothing to start or stop. It can be removed.")


def card_service(key: str, node: dict | None = None) -> dict:
    """The contract's `service` dict for one card: what it is doing and whether
    the operator may change that — plus, on the conductor, the one sub-switch that
    lives inside the process.

    Never fakes. Every `control: false` carries the reason, and every reason names
    the real thing that would have to happen instead."""
    kind = card_kind(key, node)
    st = pc_states()
    base = {"kind": kind, "remove": {"allowed": False, "reason": REMOVE_SERVICE_REASON}}
    if kind == "frame":
        return {**base, "state": "none", "control": False, "reason": FRAME_REASON,
                "remove": {"allowed": False,
                           "reason": "a plan without an aim or a goal is not a plan"}}
    if kind in ("core", "service"):
        row = (st or {}).get(key)
        state = "unknown" if st is None else ("running" if row and row["running"]
                                              else "stopped")
        out = {**base, "state": state, "control": st is not None and kind != "core"}
        if row:
            out["pc"] = {"state": row["state"], "ready": row["ready"],
                         "restarts": row["restarts"], "uptime": row["uptime"],
                         "pid": row["pid"]}
        if kind == "core":
            out["control"] = False
            out["reason"] = CONDUCTOR_REASON
            from . import repair
            out["sub"] = {"key": "repair", "label": "the IT crew",
                          "state": "running" if repair.enabled() else "stopped",
                          "control": True,
                          "note": "the crew's kv toggle, which the engine's 20s "
                                  "tick obeys — the truest runtime switch on the box"}
        elif st is None:
            out["reason"] = NO_PC.format(base=api_base())
        return out
    if kind == "worker-pool":
        n = len(worker_cards())
        return {**base, "state": "running" if n else "idle", "control": False,
                "reason": POOL_REASON,
                "remove": {"allowed": False, "reason": REMOVE_SERVICE_REASON}}
    if kind == "sandbox":
        from . import sandbox
        try:
            running = bool(sandbox.status().get("running"))
        except Exception:
            running = False
        return {**base, "state": "running" if running else "stopped", "control": True}
    if kind == "apps":
        # IDLE, not "stopped": nothing was stopped, there is simply nothing
        # deployed. The same word the worker pool uses for the same fact.
        n = len(app_cards())
        return {**base, "state": "running" if n else "idle", "control": False,
                "reason": APPS_REASON}
    if kind == "worker":
        live = (node or {}).get("live") or {}
        return {**base, "state": "running" if live.get("alive", True) else "stopped",
                "control": False, "reason": WORKER_REASON,
                "remove": {"allowed": False, "reason": REMOVE_EPHEMERAL_REASON}}
    if kind == "app":
        live = (node or {}).get("live") or {}
        return {**base, "state": "running" if live.get("alive", True) else "stopped",
                "control": True,
                "remove": {"allowed": False, "reason": REMOVE_EPHEMERAL_REASON}}
    if kind == "orphan":
        return {**base, "state": "none", "control": False, "reason": ORPHAN_REASON,
                "remove": {"allowed": True, "reason": ""}}
    return {**base, "state": "none", "control": False, "reason": FRAME_REASON}


def card_set(key: str, action: str, node: dict | None = None, sub: str = "") -> dict:
    """Flip the REAL switch behind one card. Raises ValueError with the honest
    reason for a card that has none — the route turns that into the 400 the
    contract has always promised."""
    kind = card_kind(key, node)
    on = action == "start"
    if sub:
        cur = card_service(key, node)
        entry = cur.get("sub") or {}
        if entry.get("key") != sub:
            raise ValueError(f"'{key}' has no sub-switch called {sub!r}")
        from . import repair
        repair.toggle(on)
        return card_service(key, node)
    if kind in ("core", "service"):
        if kind == "core":
            raise ValueError(CONDUCTOR_REASON)
        if pc_states() is None:
            raise ValueError(NO_PC.format(base=api_base()))
        try:
            pc_start(key) if on else pc_stop(key)
        except Exception as e:
            raise ValueError(f"the fleet manager refused: {type(e).__name__}: "
                             f"{str(e)[:160]}")
        return card_service(key, node)
    if kind == "sandbox":
        from . import sandbox
        if on:
            res = sandbox.start("live")
            if not res.get("ok"):
                raise ValueError(res.get("error", "the sandbox would not start"))
        else:
            sandbox.stop()
        return card_service(key, node)
    if kind == "app":
        from . import deploy
        live = (node or {}).get("live") or {}
        if not on:
            deploy.stop(int(live.get("project_id") or 0), str(live.get("branch") or ""))
            return {**card_service(key, node), "state": "stopped"}
        raise ValueError("an app is started by deploying it — open the project's "
                         "Deploy screen, which builds the code before it runs it")
    raise ValueError({"worker-pool": POOL_REASON, "worker": WORKER_REASON,
                      "apps": APPS_REASON,
                      "orphan": ORPHAN_REASON}.get(kind, FRAME_REASON))
