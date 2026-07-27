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


def new_thread(tid: int) -> dict:
    """A fresh graph: an edge list, a single free-text RULEBOOK the manager obeys, and a manager."""
    return {"id": tid, "name": f"thread {tid}", "edges": [], "closed": False,
            "rulebook": "", "manager": default_manager()}


def edge_eq(e: list, a: int, b: int) -> bool:
    return (e[0] == a and e[1] == b) or (e[0] == b and e[1] == a)
