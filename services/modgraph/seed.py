"""The seed — the platform's own FLEET, read from services.yaml.

`seed_fleet_graph()` is the deterministic plan of what this platform actually
runs: one card per SERVICE. Not one card per code module — that was the previous
seed and the owner's correction deleted it outright: "a module MEANS it's a
microservice. I don't want to understand what's inside." A card here is a process
with a port, a contract, a database it alone opens and a switch that really stops
it, and the registry is the only place any of that is declared.

WHERE EVERY FIELD COMES FROM, so nothing on the wall is invented:

  which cards exist   `services.yaml` — every entry, whatever its kind. Adding a
                      service to the registry adds a card; nothing else does.
  the node KEY        the service NAME. This is the identity that survives a
                      replan, a reordered registry and a manager's retitling, so
                      assignment, mastery and test rows stay attached to the thing
                      they were earned on. It is also the name in the fleet log,
                      in `pc stop <name>`, and in the /svc gateway's URL — one
                      name for one thing, everywhere.
  the spec            the service's OWN opening docstring, read from its `app.py`
                      at seed time. What a service says it is beats what a seed
                      table remembered it being.
  the group tier      the two registry kinds that genuinely CONTAIN a varying
                      number of live things: `ephemeral` (the worker pool, whose
                      children are the workers running right now) and the
                      `external` entry with a port range that holds more than one
                      process (the apps room). Everything else is FLAT. See below.
  the edges           `callers`, `doors`, `peers` and `depends_on` in the
                      registry — the declared wiring, nothing derived from
                      imports, because there are no imports across a process
                      boundary to derive from.
  the suite           a managed service's own `services/<name>/tests`; every other
                      card's is parsed from the repo suite's imports, the same
                      mechanical rule the previous seed used.

WHY THE TOP ROOM IS FLAT. Seven services, a sandbox, a worker pool and an apps
room is eleven cards; the Atlas fits far more than that in one room. Chambers
would have meant a "core" doorway holding exactly one card and a "service"
doorway holding six — a click between the operator and the switch he came to
press, for a taxonomy he already knows. The two chambers that remain are the two
that are actually rooms: what is inside them is a LIST that changes while you
watch it, and a card cannot show a list. That is the honest reading of "derive
the group tier from the registry kinds" at this size.

WHY A SERVICE READS FILES. It reads the registry and the working tree — the thing
the graph is a description of — the way a linter does: open, parse, close. The
isolation rules are one process per database (SERVICE_CONTRACT 1) and no imports
across directories (rule 9), and both hold: nothing here imports the conductor,
and the only database this process opens is its own. What it must never do is
EXECUTE any of it or shell into the checkout — the affected-tests runner stays
conductor-side for exactly that reason.

`REPO_ROOT` is env-only, defaulting to the process's cwd, which is the repo root
under process-compose, under `run-local.sh --legacy`, and in every test that
mounts this service.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

import derive
import store

ROOT = Path(os.environ.get("REPO_ROOT") or ".").resolve()

MANAGED_KINDS = ("core", "service")

# The two registry entries whose card is a ROOM: what they hold is a live list
# (the workers running right now, the apps deployed right now), and the payload
# joins those children on at render time rather than storing a plan version per
# spawn. Read from the registry by KIND — an `ephemeral` entry and the `external`
# entry that manages more than one process — never from a hand-kept list of names.
def _is_room(name: str, svc: dict) -> bool:
    if svc.get("kind") == "ephemeral":
        return True
    return svc.get("kind") == "external" and name == "apps"


# The frame, and the three cards the registry declares without a docstring to
# read. Everything else's spec is the service's own words.
AIM_TITLE = ("devteam — a platform whose agent teams build software, "
             "including this platform")
AIM_SPEC = ("Decomposed into the SERVICES it actually runs: one card per process, "
            "each with its own port, its own contract, its own database and its "
            "own switch. What is inside one is its business; what it promises is "
            "on its card.")
CONCLUSION_TITLE = "The Artifact — the running platform"
CONCLUSION_SPEC = ("The deliverable: this fleet, serving this dashboard, improving "
                   "this repository. Health is read from the fleet manager and each "
                   "service's own /health, never asserted.")

_UNDOCUMENTED_SPECS = {
    "conductor": ("The conductor — the one origin the browser talks to, the only "
                  "holder of model credentials, and the process that calls every "
                  "other card in this room."),
    "worker-pool": ("The worker pool — the coding agents the scheduler spawns when "
                    "there is work and reaps when it ends. Its children are the "
                    "workers running right now; an empty pool is an idle platform, "
                    "not a broken one."),
    "sandbox": ("The sandbox — a whole second copy of this platform, booted beside "
                "the live one from a snapshot of the tree, with its own database, "
                "its own port and no credentials."),
    "apps": ("Deployed apps — what the teams actually built, each running on its "
             "own bind-reserved port. This card is a room: one card inside per app "
             "that is up."),
}


def _first_para(rel: str) -> str:
    """The first paragraph of a module's real docstring, read from the tree at seed
    time — the spec is what the service says it is, not what a seed file
    remembered it being."""
    try:
        text = (ROOT / rel).read_text()
    except OSError:
        return ""
    parts = text.split('"""')
    if len(parts) < 2:
        return ""
    return " ".join(parts[1].strip().split("\n\n")[0].split())[:600]


def registry() -> dict:
    """services.yaml, parsed. The ONE source the fleet's cards come from."""
    try:
        reg = yaml.safe_load((ROOT / "services.yaml").read_text()) or {}
    except Exception:
        return {"services": {}}
    if not isinstance(reg, dict) or not isinstance(reg.get("services"), dict):
        return {"services": {}}
    return reg


def _boundary(name: str, svc: dict) -> list[str]:
    """The card's boundary manifest — which files a change to this service touches.

    SERVER-SIDE ONLY. It is what matches the crew's live activity onto a card and
    what scopes a verify; it is stripped from every payload the Atlas sees,
    because the panel's whole promise is that the box stays closed."""
    if svc.get("dir"):
        return [str(svc["dir"]).rstrip("/") + "/"]
    return {
        "conductor": ["conductor/", "dashboard/", "worker/"],
        "worker-pool": ["worker/", "conductor/app/launcher.py"],
        "sandbox": ["conductor/app/sandbox.py"],
        "apps": ["conductor/app/deploy.py", "conductor/app/rollout.py"],
    }.get(name, [])


def _tags(name: str, svc: dict) -> list[str]:
    tags = [str(svc.get("kind") or "")]
    if svc.get("port"):
        tags.append(f"port {svc['port']}")
    if svc.get("port_range"):
        lo, hi = svc["port_range"]
        tags.append(f"ports {lo}-{hi}")
    for door in sorted(svc.get("doors") or []):
        tags.append(f"door: {door}")
    return [t for t in tags if t]


def _spec(name: str, svc: dict) -> str:
    if name in _UNDOCUMENTED_SPECS:
        return _UNDOCUMENTED_SPECS[name]
    if svc.get("dir"):
        got = _first_para(f"{str(svc['dir']).rstrip('/')}/app.py")
        if got:
            return got
    return f"a {svc.get('kind', 'fleet')} entry in the platform's own registry"


# --- the edges: the declared wiring, and nothing else -------------------------

def _edges(reg: dict) -> list[dict]:
    """Every arrow the registry actually declares.

    Direction is FEEDS: src feeds dst, which is the convention the columns read
    left to right. A service feeds the conductor (the conductor calls it and the
    answer comes back); the conductor feeds the things it spawns. A `doors:` entry
    is not a second arrow — it is a term on the same edge's contract, because two
    arrows between the same pair is a cycle drawn to look like detail."""
    svcs = reg.get("services") or {}
    core = next((n for n, s in svcs.items() if s.get("kind") == "core"), "conductor")
    out: list[dict] = []
    for name, svc in svcs.items():
        if svc.get("kind") != "service":
            continue
        contract = {
            "kind": "service",
            "rule": (f"the conductor calls {name} over HTTP on 127.0.0.1:"
                     f"{svc.get('port')} with its own minted token, and nothing "
                     f"else ever opens its store"),
        }
        if svc.get("doors"):
            contract["doors"] = sorted(svc["doors"])
            contract["door_rule"] = (
                "and reaches back through exactly these conductor doors — being "
                "inside the fleet is not a permission")
        if svc.get("knobs"):
            contract["knobs"] = sorted(svc["knobs"])
        out.append({"src": name, "dst": core, "edge_type": "interface",
                    "contract": contract, "contract_test": ""})
        for peer in sorted(svc.get("peers") or []):
            out.append({
                "src": peer, "dst": name, "edge_type": "interface",
                "contract": {"kind": "peer",
                             "rule": (f"{name} calls {peer} directly — the one "
                                      "service→service edge in the fleet, and the "
                                      "only reason a peer's token is written into "
                                      "another service's environment")},
                "contract_test": ""})
        for dep in sorted(svc.get("depends_on") or []):
            out.append({"src": dep, "dst": name, "edge_type": "depends",
                        "contract": {"kind": "startup",
                                     "rule": f"{name} does not start until {dep} "
                                             "reports healthy"},
                        "contract_test": ""})
    for name, svc in svcs.items():
        if svc.get("kind") not in ("ephemeral", "external"):
            continue
        out.append({
            "src": core, "dst": name, "edge_type": "interface",
            "contract": {"kind": svc["kind"],
                         "rule": (f"the conductor's {svc.get('managed_by') or name} "
                                  f"starts and reaps these itself; the fleet manager "
                                  f"never touches them")},
            "contract_test": ""})
    return out


def _frame_edges(reg: dict, edges: list[dict]) -> list[dict]:
    """aim → the cards it is made OF, and the terminal cards → the Artifact.

    Derived, not listed: a card is fed by the aim when nothing else in the fleet
    feeds it, and it reaches the Artifact when it feeds nothing else. So the frame
    can never disagree with the wiring — adding a service re-frames the room by
    itself."""
    svcs = reg.get("services") or {}
    keys = [n for n in svcs]
    fed = {e["dst"] for e in edges}
    feeds = {e["src"] for e in edges}
    out = [{"src": "aim", "dst": k, "edge_type": "depends", "contract": {},
            "contract_test": ""} for k in keys if k not in fed]
    out += [{"src": k, "dst": "conclusion", "edge_type": "depends", "contract": {},
             "contract_test": ""} for k in keys if k not in feeds]
    return out


# --- the suites: a card's own tests -------------------------------------------

# Which conductor module a non-service card owns, for the import-parsed mapping
# of the repo's own suite. `conductor` is the default: the e2e suite IS the
# conductor's suite, and saying otherwise would leave most of it mapped to nothing.
_MODULE_CARD = {"launcher": "worker-pool", "sandbox": "sandbox", "deploy": "apps",
                "rollout": "apps"}

_IMPORT_RES = (
    re.compile(r"^[ \t]*from app import ([\w ,]+)", re.M),
    re.compile(r"^[ \t]*from app\.(\w+)", re.M),
    re.compile(r"^[ \t]*import app\.(\w+)", re.M),
)


def tests_for_nodes(by_key: dict[str, list[str]]) -> dict[str, set[str]]:
    """{test file: the card keys it exercises}, mechanically.

    A managed service's suite is its OWN directory — that is what
    SERVICE_CONTRACT rule 6 put there, and it is the only suite that can fail
    because of that service alone. Every other card's comes from the repo suite's
    parsed imports: a test that imports `app.deploy` is a test of the apps card, a
    test that imports anything else in the conductor package is a test of the
    conductor. Derived, so a new test file maps itself.

    `by_key` carries the boundaries for signature compatibility with the manager's
    authoring pass; the mapping is by KEY, because a service's suite belongs to
    the service and not to whichever paths a manager felt like listing."""
    asked = set(by_key)
    out: dict[str, set[str]] = {}
    svcs = registry().get("services") or {}
    for name, svc in svcs.items():
        if name not in asked or not svc.get("dir"):
            continue
        tdir = ROOT / str(svc["dir"]).rstrip("/") / "tests"
        for f in sorted(tdir.glob("test_*.py")):
            out.setdefault(str(f.relative_to(ROOT)), set()).add(name)
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        try:
            src = f.read_text()
        except OSError:
            continue
        mods: set[str] = set()
        for m in _IMPORT_RES[0].findall(src):
            for part in m.split(","):
                mods.add(part.strip().split(" as ")[0].strip())
        for pat in _IMPORT_RES[1:]:
            mods.update(pat.findall(src))
        if not mods:
            continue
        hit = {_MODULE_CARD[m] for m in mods if m in _MODULE_CARD} or {"conductor"}
        hit &= asked
        if hit:
            out.setdefault(f"tests/{f.name}", set()).update(hit)
    return out


def dedupe_tests(test_list: list[dict]) -> list[dict]:
    """A file can qualify twice; the UNIQUE index dedupes the rows, so dedupe the
    manifest the same way or equality would lie."""
    seen, out = set(), []
    for t in sorted(test_list, key=lambda t: (t["node"], t["kind"], t["path"])):
        k = (t["node"], t["kind"], t["path"])
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


# --- the manifest -------------------------------------------------------------

def self_manifest() -> dict:
    """The whole fleet graph as one comparable value: aim → every service (the two
    rooms carrying their kind) → the Artifact. Idempotence is dict equality on
    this, nothing cleverer.

    The ONE manifest builder: shared by the offline seed and, over the wire, by
    the manager's authoring pass — which may retitle, re-spec and re-assign, but
    may not invent a card, because a card that is not in the registry is not a
    process anybody is running."""
    reg = registry()
    svcs = reg.get("services") or {}
    node_list: list[dict] = [{
        "key": "aim", "title": AIM_TITLE, "node_type": "aim", "spec": AIM_SPEC,
        "join_mode": "all_of", "parent_key": "", "tags": [], "paths": []}]
    for name, svc in svcs.items():                 # registry order, stable
        node_list.append({
            "key": name, "title": name,
            "node_type": "group" if _is_room(name, svc) else "code",
            "spec": _spec(name, svc), "join_mode": "all_of", "parent_key": "",
            "tags": _tags(name, svc), "paths": _boundary(name, svc)})
    node_list.append({
        "key": "conclusion", "title": CONCLUSION_TITLE, "node_type": "conclusion",
        "spec": CONCLUSION_SPEC, "join_mode": "all_of", "parent_key": "",
        "tags": [], "paths": []})

    wiring = _edges(reg)
    edge_list = _frame_edges(reg, wiring) + wiring
    by_key = {n["key"]: n["paths"] for n in node_list}
    tmap = tests_for_nodes(by_key)
    test_list = dedupe_tests([{"node": key, "kind": "suite", "path": path}
                              for path, keys in tmap.items() for key in keys])
    return {"nodes": node_list, "edges": edge_list, "tests": test_list}


def manifest_of(plan_id: int) -> dict:
    """The stored plan, re-shaped for comparison with self_manifest()."""
    return {
        "nodes": [{"key": n["key"], "title": n["title"], "node_type": n["node_type"],
                   "spec": n["spec"], "join_mode": n["join_mode"],
                   "parent_key": n["parent_key"], "tags": n["tags"], "paths": n["paths"]}
                  for n in store.nodes(plan_id)],
        "edges": [{"src": e["src_key"], "dst": e["dst_key"], "edge_type": e["edge_type"],
                   "contract": e["contract"], "contract_test": e["contract_test"]}
                  for e in store.edges(plan_id)],
        "tests": [{"node": t["node_key"], "kind": t["kind"], "path": t["path"]}
                  for t in store.tests(plan_id)],
    }


def seed_fleet_graph() -> int:
    """Ensure project 0 has an active plan of the FLEET; return its id.

    Idempotent: an unchanged registry reseeds to zero writes. Drift — a service
    added to services.yaml, a docstring reworded, a test file added — makes a NEW
    version and supersedes the old one; the old rows are never touched, because a
    fallback that edits history is exactly as untrustworthy as a manager that
    does. A plan the crew's manager authored outranks the seed entirely."""
    man = self_manifest()
    registry_keys = {n["key"] for n in man["nodes"]}
    cur = store.active_plan(0)
    if cur and cur["authored_by"] != "seed":
        # A plan the crew's manager authored outranks the seed — but only if it is
        # a plan of THIS FLEET. A box that ran the platform before P6 has a manager
        # plan on the wall describing CODE MODULES ("routes", "guards", "canvas"),
        # and nothing about it is startable, stoppable or even real any more.
        # Keeping it out of deference would leave that operator staring at the
        # exact screen the owner's correction deleted, forever, because the seed
        # politely refuses to touch manager work. So the deference is conditional
        # on the one thing that makes a plan a plan: it must name every process the
        # registry says is running. A post-P6 authored plan always does — the
        # authoring pass restores any card the manager forgot — so this only ever
        # fires on a plan from before the cards were services.
        if registry_keys <= {n["key"] for n in store.nodes(int(cur["id"]))}:
            return int(cur["id"])
    if not man["nodes"][1:-1]:
        # No registry, no fleet. Writing an aim and a goal with nothing between
        # them would put an empty room on the wall and call it the platform.
        return int(cur["id"]) if cur else 0
    if cur and manifest_of(cur["id"]) == man:
        return int(cur["id"])
    plan = store.import_plan(
        0, kind="template", authored_by="seed",
        notes="the fleet, regenerated from services.yaml",
        nodes_in=man["nodes"], edges_in=man["edges"], tests_in=man["tests"])
    return int(plan["id"])


# The old name, kept dead on purpose: it described a graph of CODE MODULES, and a
# caller that still asks for it is asking for the thing this phase deleted.
def seed_self_graph() -> int:      # pragma: no cover - a tripwire, not a path
    raise RuntimeError(
        "seed_self_graph described the platform's code modules as if they were "
        "modules; P6 replaced it with seed_fleet_graph(), whose cards are the "
        "services in services.yaml")
