// Two implementations of one contract, fired at with the same generated inputs.
// Every disagreement is a hole in the contract.
//
// This is the answer to "how do you know the contract is finished?", and it is
// the only honest one available. A contract written by the same person who wrote
// the implementation encodes one understanding twice; its cases pass because
// both sides share the same blind spots. A SECOND implementation, written from
// the contract rather than from the code, does not share them — so wherever the
// two disagree, the contract failed to say something it needed to say.
//
// sqllogictest is the industrial version of this: 7.2 million queries, only
// affordable because the expected answers were minted from a reference engine
// rather than typed by a human. Ethereum runs five clients against shared
// fixtures for the same reason, with consensus at stake. The trick is old. What
// is new here is having two implementations of a MODULE-sized contract sitting
// in a store, already admitted, doing nothing.
//
// A disagreement is not a bug report about either implementation. Both passed
// every case. It is a report about the CONTRACT: it permitted two answers where
// it should have permitted one.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { contractId, loadManifest } from "./contract.js";
import { materialiseRunnable } from "./compose.js";
import { openModule } from "./client.js";
import { corpusFor } from "./generate.js";

/**
 * @typedef {object} Disagreement
 * @property {string} operation
 * @property {any} input
 * @property {Record<string, any>} answers  keyed by "language:artifact-prefix"
 * @property {string} says
 */

/**
 * @typedef {object} DifferVerdict
 * @property {string} module
 * @property {string} contract
 * @property {number} implementations
 * @property {number} cases
 * @property {number} accepted  how many inputs actually reached the implementation
 * @property {Disagreement[]} holes
 * @property {string} summary
 */

/**
 * @param {object} args
 * @param {string} args.moduleDir
 * @param {import("./store.js").Store} args.store
 * @param {import("./ledger.js").Ledger} args.ledger
 * @param {string} args.runsRoot
 * @param {string} [args.heldoutDir]
 * @param {number} [args.cases]
 * @param {number} [args.seed]
 * @returns {Promise<DifferVerdict>}
 */
export async function differ({ moduleDir, store, ledger, runsRoot, heldoutDir, cases = 200, seed = 1 }) {
  const manifest = loadManifest(moduleDir);
  const c = contractId(moduleDir, heldoutDir);
  const iface = JSON.parse(readFileSync(join(moduleDir, manifest.interface[0] ?? "interface.json"), "utf8"));

  // The conformance cases are the seed corpus. They are the only inputs known to
  // be structurally valid, and a schema alone cannot build one for any contract
  // whose parts refer to each other.
  /** @type {Record<string, any[]>} */
  const seeds = {};
  for (const rel of manifest.conformance) {
    try {
      for (const cs of JSON.parse(readFileSync(join(moduleDir, rel), "utf8")).cases ?? []) {
        if (cs.expectError === undefined && cs.op) (seeds[cs.op] ??= []).push(cs.in);
      }
    } catch { /* a module with no conformance file falls back to schema sampling */ }
  }

  // Every artifact admitted for this contract, deduplicated BY ITS CODE.
  //
  // Not by artifact digest, which would be false comfort of exactly the kind
  // this whole project exists to refuse. Re-admitting a module after changing
  // its toolchain — pinning an image by digest, say — mints a new artifact
  // digest around byte-identical code. Counting those as two implementations
  // and reporting that they "agreed on 200 inputs" would be reporting that a
  // function agrees with itself.
  //
  // The code digest is the artifact's file list minus the toolchain, so two
  // artifacts count as one implementation exactly when their code is the same.
  const byCode = new Map();
  for (const r of ledger.admitted(c.id)) {
    let code;
    try {
      code = store.manifest(r.artifact)
        .filter((f) => f.path !== manifest.toolchain)
        .map((f) => `${f.path}:${f.sha256}`).sort().join("|");
    } catch { continue; }   // an artifact the store no longer holds
    if (!byCode.has(code)) byCode.set(code, r);
  }
  const records = [...byCode.values()];

  if (records.length < 2) {
    return {
      module: manifest.name, contract: c.id, implementations: records.length, cases: 0, accepted: 0, holes: [],
      summary: records.length === 0
        ? "nothing admitted"
        : `only ONE distinct implementation — nothing to compare. A contract is tested by a second implementation written FROM it, not from the first one's code, and re-admitting the same code under a new toolchain does not count.`,
    };
  }

  const handles = records.map((r) => ({
    label: `${r.language ?? "?"}:${r.artifact.slice(2, 10)}`,
    handle: openModule({
      runnable: materialiseRunnable({ moduleDir, store, ledger, runsRoot, ...(heldoutDir ? { heldoutDir } : {}), pinned: r.artifact }),
    }),
  }));

  /** @type {Disagreement[]} */
  const holes = [];
  let fired = 0;
  let accepted = 0;

  try {
    for (const [op, operation] of Object.entries(iface.operations ?? {})) {
      for (const input of corpusFor(seeds[op] ?? [], operation, cases, seed)) {
        fired++;
        /** @type {Record<string, any>} */
        const answers = {};
        for (const { label, handle } of handles) {
          try {
            answers[label] = { out: await handle.call(op, input) };
          } catch (err) {
            answers[label] = { error: /** @type {any} */ (err)?.code ?? "EUNCAUGHT" };
          }
        }

        if (!Object.values(answers).every((a) => a.error)) accepted++;
        const shapes = new Set(Object.values(answers).map((a) => JSON.stringify(a)));
        if (shapes.size > 1) {
          holes.push({ operation: op, input, answers, says: explain(answers) });
          // Twenty is plenty to act on, and the twenty-first is usually the same
          // hole wearing a different input.
          if (holes.length >= 20) break;
        }
      }
      if (holes.length >= 20) break;
    }
  } finally {
    for (const { handle } of handles) handle.close();
  }

  // How many inputs actually REACHED the implementation. Without this the tool
  // can report perfect agreement about inputs that were all rejected at the front
  // door, which is what it did on its first honest run.
  return {
    module: manifest.name, contract: c.id,
    implementations: records.length, cases: fired, accepted, holes,
    summary: holes.length === 0
      ? accepted === 0
        ? `${records.length} implementations agreed, but NOTHING reached them — all ${fired} inputs were refused at the front door, so this says nothing about the contract`
        : `${records.length} implementations agreed on all ${fired} inputs (${accepted} of which they actually accepted)`
      : `${holes.length} hole(s) in the contract — ${records.length} implementations that all passed its suite disagree on ${holes.length} of ${fired} inputs (${accepted} accepted)`,
  };
}

/** @param {Record<string, any>} answers */
function explain(answers) {
  const entries = Object.entries(answers);
  const errs = entries.filter(([, a]) => a.error);
  if (errs.length > 0 && errs.length < entries.length) {
    return `${errs.map(([l]) => l).join(", ")} refused it (${errs.map(([, a]) => a.error).join(", ")}); the others answered. The contract does not say whether this input is valid.`;
  }
  if (errs.length === entries.length) {
    return `all refused it, with different codes: ${entries.map(([l, a]) => `${l}=${a.error}`).join(", ")}. The contract does not say which error this is.`;
  }
  // WHERE they differ, not merely THAT they do. A truncated dump of two large
  // objects is unusable; the field is the whole finding, because that field is
  // the sentence the contract failed to write.
  const [[aName, a], [bName, b]] = /** @type {[[string, any], [string, any]]} */ (entries);
  const at = firstDifference(a.out, b.out, "");
  return at
    ? `${at.path} — ${aName} says ${JSON.stringify(at.a)}, ${bName} says ${JSON.stringify(at.b)}. The contract never said which.`
    : "all answered, differently. The contract permits more than one answer here.";
}

/**
 * The first place two answers diverge, as a path.
 * @param {any} a @param {any} b @param {string} at
 * @returns {{path: string, a: any, b: any} | null}
 */
function firstDifference(a, b, at) {
  if (JSON.stringify(a) === JSON.stringify(b)) return null;
  if (a === null || b === null || typeof a !== "object" || typeof b !== "object") {
    return { path: at || "the answer", a, b };
  }
  if (Array.isArray(a) !== Array.isArray(b)) return { path: at || "the answer", a, b };
  if (Array.isArray(a)) {
    if (a.length !== b.length) return { path: `${at}.length`, a: a.length, b: b.length };
    for (let i = 0; i < a.length; i++) {
      const d = firstDifference(a[i], b[i], `${at}[${i}]`);
      if (d) return d;
    }
    return null;
  }
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) {
    const d = firstDifference(a[k], b[k], at ? `${at}.${k}` : k);
    if (d) return d;
  }
  return null;
}
