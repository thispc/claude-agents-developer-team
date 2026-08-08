"""Threads — the agent graph.

Agents are nodes; a thread is an edge between two of them, directional (a→b / b→a) or
bidirectional (both), and a group of connected agents is the unit a round plays over. Each
connected thread carries its own rule table and a hidden manager (the Host). Threads are kept
as explicit records (id + edges + rules_rows + manager) so those properties survive edge edits;
after any edit we re-split a thread that has fallen into more than one connected component.

Pure graph arithmetic — no model, no state beyond the edge list.
"""

from __future__ import annotations

from typing import Any


def members_of(thread: dict) -> list[int]:
    """The distinct agent ids appearing in a thread's edges, in first-seen order."""
    out: list[int] = []
    for e in thread.get("edges", []):
        for x in (e[0], e[1]):
            if x not in out:
                out.append(x)
    return out


def components(edges: list) -> list[list[int]]:
    """Connected components over the UNDIRECTED edge set (union-find)."""
    parent: dict[Any, Any] = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        parent[find(a)] = find(b)

    nodes: list = []
    for e in edges:
        a, b = e[0], e[1]
        for n in (a, b):
            if n not in nodes:
                nodes.append(n)
        union(a, b)
    groups: dict[Any, list] = {}
    for n in nodes:
        groups.setdefault(find(n), []).append(n)
    return list(groups.values())


def default_manager() -> dict:
    return {"model": "", "budget": 2}


# --- the deliberation PROTOCOL: policy as data, not code -----------------------------------
# The platform provides mechanism (agents, bounded calls, routing, memos); HOW a graph
# deliberates is a small declarative config — the axes the multi-agent-debate literature
# actually measures. Defaults reproduce the platform's historical behaviour exactly, so every
# saved thread keeps working; presets name evidence-backed configurations researchers can cite.
DEFAULT_PROTOCOL: dict[str, Any] = {
    "preset": "classic",
    "init": "ghostwritten",        # round 1: "ghostwritten" (manager composes all) | "independent"
                                   #   (each agent speaks with its OWN model, blind — the
                                   #   diverse-initialization fix; CoMM 2024, +12-24pp)
    "anonymize": False,            # strip names from the MEDIATION-round transcript the manager
                                   #   reads (identity sycophancy >> self-bias; arXiv 2510.07517).
                                   #   The closing memo and the user-facing chat keep names by
                                   #   design — positions must be attributable to agents.
    "on_unanimity": "accept",      # "accept" | "devils_advocate": if a round comes back
                                   #   unanimous, the next round appoints a dissenter
                                   #   (premature-consensus fix; Smit et al. ICML 2024)
    "max_rounds": 4,               # hard cap on rounds per run (cost stays bounded)
}
PROTOCOL_PRESETS: dict[str, dict[str, Any]] = {
    "classic": {},                                          # the original behaviour
    "evidence-2026": {"init": "independent", "anonymize": True, "on_unanimity": "devils_advocate"},
}
_PROTO_VALUES = {"init": {"ghostwritten", "independent"},
                 "on_unanimity": {"accept", "devils_advocate"}}


def protocol_of(thread: dict) -> dict[str, Any]:
    """The thread's resolved protocol: defaults ← preset ← the thread's own overrides.
    Old threads with no protocol field resolve to exactly the historical behaviour."""
    stored = thread.get("protocol") or {}
    preset = stored.get("preset", "classic")
    out = dict(DEFAULT_PROTOCOL)
    out.update(PROTOCOL_PRESETS.get(preset, {}))
    out.update({k: v for k, v in stored.items() if k in DEFAULT_PROTOCOL})
    out["preset"] = preset if preset in PROTOCOL_PRESETS else "classic"
    return out


def clean_protocol(p: dict | None) -> dict[str, Any]:
    """Validate a protocol PATCH from the API — unknown keys dropped, bad values rejected
    to defaults, max_rounds clamped. Stored small; resolved through protocol_of at run time."""
    p = p if isinstance(p, dict) else {}
    out: dict[str, Any] = {}
    # isinstance guards first: a JSON list/object value is unhashable, and `in` on a set/dict
    # would raise TypeError → an HTTP 500 instead of the junk simply being dropped.
    if isinstance(p.get("preset"), str) and p["preset"] in PROTOCOL_PRESETS:
        out["preset"] = p["preset"]
    for k, allowed in _PROTO_VALUES.items():
        v = p.get(k)
        if isinstance(v, str) and v in allowed:
            out[k] = v
    if "anonymize" in p:
        out["anonymize"] = bool(p["anonymize"])
    if "max_rounds" in p:
        try:
            out["max_rounds"] = max(1, min(int(p["max_rounds"]), 4))
        except (TypeError, ValueError, OverflowError):   # OverflowError: JSON `Infinity` parses fine
            pass
    return out


def new_thread(tid: int) -> dict:
    """A fresh graph: an edge list, a single free-text RULEBOOK the manager obeys, a manager,
    and a deliberation protocol (policy-as-data; empty = classic defaults)."""
    return {"id": tid, "name": f"thread {tid}", "edges": [], "closed": False,
            "rulebook": "", "manager": default_manager(), "protocol": {}}


def edge_eq(e: list, a: int, b: int) -> bool:
    return (e[0] == a and e[1] == b) or (e[0] == b and e[1] == a)
