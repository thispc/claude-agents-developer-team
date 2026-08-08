"""Derivation — the three answers the graph computes rather than stores.

All three moved here with the rows they read, because a derivation performed on a
COPY of a table you no longer own is a derivation that can disagree with the
table. The conductor asks for the answer, not for the rows to compute it from.

    derive_group_edges  the top tier: which layer depends on which, RECONCILED
                        against an authored tier. Pure — lists in, list out.
    affected_tests      the files a change to one node obliges us to run.
    mastery             who has actually finished work on each module, from the
                        trace. Its JOIN is internal: both tables are this
                        service's, which is the whole test of where the boundary
                        went.
"""

from __future__ import annotations

import store

# Verified ok runs on one node before its top agent is called the master. Three, not
# one: a single lucky landing is not mastery, and the number must be small enough to
# be earnable within a day of sprints.
MASTER_RUNS = 3


def derive_group_edges(child_edges: list[dict], parent_of: dict[str, str],
                       group_edges: list[dict] | None = None) -> list[dict]:
    """Top-level edges are COMPUTED, never curated: group A → group B iff any
    child of A has an edge to any child of B (and A is not B — an edge inside
    one layer is that layer's private business). The first qualifying child
    edge in input order is the representative: its type, contract and contract
    test ride up, so the top level shows a real rule, not a hollow arrow.

    `group_edges` — an AUTHORED top tier (the manager writes its own) — is
    reconciled rather than trusted. The live hole this closes: plan v6 carried
    the child edge ops→db while its authored tier had no selfrepair→data over
    it, and one selfrepair→agents arrow no child edge backed. So: a crossing
    the author missed is derived anyway; an authored arrow that matches a real
    crossing keeps its deliberate type/contract over the mechanical
    representative; and an authored arrow with no child crossing behind it is
    DROPPED — an arrow into nothing is exactly the lie this derivation exists
    to prevent. Pure — lists in, list out, no rows, no files, no model.

    IT IS GRAPH DOMAIN, NOT PRESENTATION, which is why it crossed with the store
    and did not stay with the BFF. The seed calls it to build the plan it writes;
    the payload calls it again to reconcile what a manager authored. Two callers
    on two sides of a wire running two copies of this rule is how the stored plan
    and the rendered graph start telling different stories about the same repo."""
    authored: dict[tuple[str, str], dict] = {}
    for e in group_edges or []:
        authored.setdefault((e["src"], e["dst"]), e)
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for e in child_edges:
        ga, gb = parent_of.get(e["src"], ""), parent_of.get(e["dst"], "")
        if not ga or not gb or ga == gb or (ga, gb) in seen:
            continue
        seen.add((ga, gb))
        rep = authored.get((ga, gb), e)
        out.append({"src": ga, "dst": gb, "edge_type": rep["edge_type"],
                    "contract": rep["contract"],
                    "contract_test": rep.get("contract_test", "")})
    return out


def affected_tests(plan_id: int, node_key: str) -> list[str]:
    """The files a change to this node obliges us to run: the node's own suite plus
    the contract test of every edge the node touches, either end. A GROUP node's
    affected set is the union of its children's — verifying a layer means
    verifying every module in it, nothing more and nothing less; test rows
    themselves live on LEAVES only.

    Pure selection — reads rows, returns paths, runs nothing and no model. This is
    the Nx-style affected-only discipline: verifying `guards` should not cost a
    full-suite run, but it MUST cost every contract `guards` participates in,
    because the other side of an interface is exactly who a change here breaks.

    The RUNNER stays in the conductor: it shells out to the repo's own pytest over
    real files in the checkout the conductor is serving from. This end of the wire
    only ever says WHICH files."""
    kids = [n["key"] for n in store.nodes(plan_id)
            if node_key and n["parent_key"] == node_key]
    if kids:
        merged: set[str] = set()
        for k in kids:
            merged.update(affected_tests(plan_id, k))
        return sorted(merged)
    out = {t["path"] for t in store.tests(plan_id, node_key) if t["kind"] == "suite"}
    for e in store.edges(plan_id):
        if node_key in (e["src_key"], e["dst_key"]) and e["contract_test"]:
            out.add(e["contract_test"])
    return sorted(out)


def mastery(project_id: int = 0) -> dict[str, dict]:
    """{node_key: {"agent_id", "runs", "master"}} — the top agent per node.

    An agent that keeps finishing work on a module becomes its master — computed
    from the trace, never stored, so it cannot drift from what actually happened.

    Counted over CLOSED ok runs of kind build|verify across EVERY plan version of the
    project: node keys are the stable identity, so mastery survives a replan by
    construction. Ties keep the incumbent — the agent whose first ok run came earlier —
    because a challenger 'catches up to' a master, it does not split the title, and a
    later arrival must EXCEED the master's count to take over.

    ONE JOIN, and it stayed a join. `graph_node_runs ⋈ graph_plans` is the only
    cross-table read in the feature and both tables are this service's, so nothing
    here became HTTP composition. The agent ids it returns are the lifeworld's
    human ids, and this service does not resolve them to names — that is the
    conductor's decoration, from the crew record it owns."""
    proj = store._real_id(project_id)
    if proj is None:
        return {}
    rows = store._rows(
        "SELECT r.node_key AS k, r.agent_id AS a, COUNT(*) AS n, MIN(r.id) AS first"
        " FROM graph_node_runs r JOIN graph_plans p ON p.id = r.plan_id"
        " WHERE p.project_id=? AND r.status='ok' AND r.ended_at IS NOT NULL"
        "   AND r.kind IN ('build','verify') AND r.agent_id IS NOT NULL"
        " GROUP BY r.node_key, r.agent_id", (proj,))
    out: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: (r["k"], -r["n"], r["first"])):
        if r["k"] in out:
            continue                       # sorted best-first: the first row per key wins
        out[r["k"]] = {"agent_id": int(r["a"]), "runs": int(r["n"]),
                       "master": int(r["n"]) >= MASTER_RUNS}
    return out
