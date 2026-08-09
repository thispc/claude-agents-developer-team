// wiring.toml — the composition graph, and the one file agents never write.
//
// This is the safety property of the entire system, so it is worth being precise
// about why it is drawn here and not somewhere more convenient.
//
// A module's implementation is cheap to reverse: every version is in a
// content-addressed store, pointing back is one appended line, and the blast
// radius is one contract. That is why implementations can be auto-admitted with
// nobody watching. A change to the GRAPH is the opposite on both counts — it
// reaches everything downstream, and undoing it means reasoning about what ran
// in between. Oversight belongs where reversal is expensive, which puts
// implementations on the loop and topology in it.
//
// The measured record backs this up in an uncomfortably direct way: essentially
// every reported failure of autonomous agent systems is a TOPOLOGY failure
// rather than an implementation failure. Subagents that could spawn subagents
// went fifty levels deep and burned 1.2 million tokens with nothing recoverable
// at the end, dozens of them launched before a human could intervene; the fix
// that shipped was not better subagents, it was forbidding the spawn. Agent
// sprawl surveys find most organisations cannot even enumerate the agents they
// are running. The common shape is always an edge nobody chose.
//
// Which is also why a module may not create another module. A new module is a
// new node and a new edge; autonomous multiplication is not an extension of this
// design, it is a contradiction of it. An agent may PROPOSE a split — and the
// proposal has real work behind it, since the pair must reproduce the parent's
// behaviour against the parent's own stored implementation — but the diff to
// this file is merged by a person.
//
// Erlang got here thirty years ago and it is worth naming: the supervision tree
// is written by the developer, and even a DynamicSupervisor is a statically
// declared node whose CHILDREN are dynamic. The shape is human. The population
// is not.

import { readFileSync, existsSync } from "node:fs";
import { join, isAbsolute } from "node:path";
import { parseToml } from "./toml.js";

/**
 * @typedef {object} WiringNode
 * @property {string} name
 * @property {string} module  repo-relative path to the module directory
 */

/**
 * @typedef {object} WiringEdge
 * @property {string} from
 * @property {string} to
 * @property {string} [why]   why this cable exists, in the author's words
 */

/**
 * @typedef {object} Wiring
 * @property {WiringNode[]} nodes
 * @property {WiringEdge[]} edges
 * @property {string} path
 */

/**
 * @param {string} file  path to wiring.toml
 * @param {string} repoRoot
 * @returns {Wiring}
 */
export function loadWiring(file, repoRoot) {
  if (!existsSync(file)) throw new Error(`${file}: no wiring file. The graph is authored, not inferred — without it there is no system, only a pile of modules.`);
  const t = parseToml(readFileSync(file, "utf8"), file);

  const rawNodes = Array.isArray(t["node"]) ? t["node"] : [];
  const rawEdges = Array.isArray(t["edge"]) ? t["edge"] : [];

  /** @type {WiringNode[]} */
  const nodes = [];
  const seen = new Set();
  for (const n of rawNodes) {
    if (typeof n["name"] !== "string" || typeof n["module"] !== "string") {
      throw new Error(`${file}: every [[node]] needs a "name" and a "module" path`);
    }
    if (seen.has(n["name"])) throw new Error(`${file}: two nodes are both called "${n["name"]}"`);
    seen.add(n["name"]);
    if (isAbsolute(n["module"])) throw new Error(`${file}: node "${n["name"]}" has an absolute module path — keep it repo-relative so the wiring means the same thing on every machine`);
    const dir = join(repoRoot, n["module"]);
    if (!existsSync(join(dir, "module.toml"))) {
      throw new Error(`${file}: node "${n["name"]}" points at ${n["module"]}, which has no module.toml. The wiring may only name modules that exist — a graph that can assert a node the code does not have is the exact failure v1 died of.`);
    }
    nodes.push({ name: n["name"], module: n["module"] });
  }

  /** @type {WiringEdge[]} */
  const edges = [];
  for (const e of rawEdges) {
    if (typeof e["from"] !== "string" || typeof e["to"] !== "string") {
      throw new Error(`${file}: every [[edge]] needs "from" and "to"`);
    }
    if (!seen.has(e["from"])) throw new Error(`${file}: edge from "${e["from"]}", which is not a declared node`);
    if (!seen.has(e["to"])) throw new Error(`${file}: edge to "${e["to"]}", which is not a declared node`);
    if (e["from"] === e["to"]) throw new Error(`${file}: "${e["from"]}" cannot be wired to itself`);
    edges.push({ from: e["from"], to: e["to"], ...(typeof e["why"] === "string" ? { why: e["why"] } : {}) });
  }

  return { nodes, edges, path: file };
}
