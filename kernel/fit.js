// The composition gate: does the picture fit together?
//
// Every module until now was judged ALONE. That leaves the one failure the
// research kept pointing at — components that are each correct and do not fit at
// the seam — completely unchecked, and it is not a small gap: the weather app has
// four edges and nothing verified that what one module emits is what the next
// one accepts. It worked because the same person wrote both sides.
//
// WHY THIS IS A SEPARATE ADMISSION, and not part of admitting a module.
//
// A module's verdict must depend only on the module's own contract, because a
// ledger hit is never re-verified. Make admission depend on its neighbours and
// the cached verdict silently depends on something outside its own hash — the
// exact failure the whole design exists to prevent. It would also be circular:
// you could never admit the first module, since admitting A would require B.
//
// So a COMPOSITION is its own thing with its own identity:
//
//     fit_id = H(graph ⊕ every node's contract_id)
//
// and a verdict records WHICH artifacts proved it. Swap any module and that is a
// miss, so the picture is checked again. `devteam gate` fails when the fit fails,
// so a broken picture still ships nothing — but each thing is judged against the
// identity it actually has.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { contractId, canonicalise, sha256 } from "./contract.js";
import { compatible, slice } from "./compat.js";

/**
 * @typedef {object} EdgeVerdict
 * @property {string} edge
 * @property {"fits"|"broken"|"unchecked"} status
 * @property {string[]} says
 */

/**
 * @typedef {object} FitVerdict
 * @property {boolean} ok
 * @property {string} id
 * @property {EdgeVerdict[]} edges
 * @property {number} checked
 * @property {number} unchecked
 * @property {string} summary
 */

/**
 * @param {object} args
 * @param {import("./wiring.js").Wiring} args.wiring
 * @param {string} args.root      the workspace directory
 * @param {(moduleDir: string) => string|undefined} [args.vaultFor]
 * @returns {FitVerdict}
 */
export function checkFit({ wiring, root, vaultFor }) {
  /** @type {Record<string, any>} */
  const ifaces = {};
  /** @type {Record<string, string>} */
  const contracts = {};
  for (const n of wiring.nodes) {
    const dir = join(root, n.module);
    ifaces[n.name] = JSON.parse(readFileSync(join(dir, "interface.json"), "utf8"));
    contracts[n.name] = contractId(dir, vaultFor?.(dir)).id;
  }

  const id = "f-" + sha256(Buffer.from(canonicalise({
    v: 1,
    nodes: wiring.nodes.map((n) => [n.name, contracts[n.name]]).sort(),
    edges: wiring.edges.map((e) => [e.from, e.produces ?? "", e.to, e.consumes ?? "", e.into ?? ""]).sort(),
  }), "utf8"));

  /** @type {EdgeVerdict[]} */
  const edges = [];
  for (const e of wiring.edges) {
    const label = `${e.from}${e.produces ? "." + e.produces : ""}${e.take ? "." + e.take : ""} → ${e.to}${e.consumes ? "." + e.consumes : ""}${e.into ? "." + e.into : ""}`;

    if (!e.produces || !e.consumes) {
      edges.push({ edge: label, status: "unchecked", says: [
        `names no operations, so nothing can be checked. Write it as \`from = "${e.from}.<operation>"\` and \`to = "${e.to}.<operation>"\` to have the shapes compared.`,
      ] });
      continue;
    }

    const out = ifaces[e.from]?.operations?.[e.produces]?.out;
    const inSchema = ifaces[e.to]?.operations?.[e.consumes]?.in;
    if (!out) { edges.push({ edge: label, status: "broken", says: [`${e.from} declares no operation called "${e.produces}"`] }); continue; }
    if (!inSchema) { edges.push({ edge: label, status: "broken", says: [`${e.to} declares no operation called "${e.consumes}"`] }); continue; }

    const source = slice(out, e.take ?? "");
    if ("missing" in source) {
      edges.push({ edge: label, status: "broken", says: [`${e.from}.${e.produces} does not output a field "${source.missing}"`] });
      continue;
    }
    const target = slice(inSchema, e.into ?? "");
    if ("missing" in target) {
      edges.push({ edge: label, status: "broken", says: [`${e.to}.${e.consumes} has no input field "${target.missing}"`] });
      continue;
    }

    const findings = compatible(source.schema, target.schema, e.into ? `the ${e.into}` : "the value");
    const errors = findings.filter((f) => f.level === "error");
    if (errors.length > 0) {
      edges.push({ edge: label, status: "broken", says: errors.map((f) => `${f.at} ${f.says}`) });
    } else {
      const soft = findings.filter((f) => f.level === "unchecked");
      edges.push({ edge: label, status: "fits", says: soft.map((f) => `${f.at} ${f.says}`) });
    }
  }

  const broken = edges.filter((e) => e.status === "broken");
  const unchecked = edges.filter((e) => e.status === "unchecked");
  return {
    ok: broken.length === 0,
    id,
    edges,
    checked: edges.length - unchecked.length,
    unchecked: unchecked.length,
    summary: broken.length
      ? `${broken.length} of ${edges.length} edge(s) do not fit`
      : `${edges.length - unchecked.length} of ${edges.length} edge(s) checked and fitting${unchecked.length ? `, ${unchecked.length} unchecked` : ""}`,
  };
}
