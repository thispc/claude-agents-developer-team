// Scenarios: run the real chain and check what comes out the far end.
//
// The static half of the composition gate compares SCHEMAS, which is cheap and
// catches the commonest break — a field renamed on one side of a seam. It cannot
// catch a value that is the right shape and the wrong content: a module that
// emits degrees where the next expects tenths, or counts days from one instead
// of zero. Both sides say "number" and both are individually correct.
//
// So this half runs the modules. Actually spawns them, actually passes the
// output of one into the next, and pins what arrives at the end. It is slower
// and it is the only thing that can see those failures.
//
// It runs the SAME artifacts the app runs — whatever the ledger says is live —
// so a scenario passing is a statement about the system as it is configured
// right now, not about a set of modules in principle.

import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { materialiseRunnable } from "./compose.js";
import { openModule } from "./client.js";

/**
 * @typedef {object} ScenarioVerdict
 * @property {string} name
 * @property {boolean} ok
 * @property {string[]} says
 * @property {number} ms
 */

/**
 * @param {object} args
 * @param {string} args.root      the workspace
 * @param {string} args.file      scenarios.json
 * @param {import("./wiring.js").Wiring} args.wiring
 * @param {import("./store.js").Store} args.store
 * @param {import("./ledger.js").Ledger} args.ledger
 * @param {string} args.runsRoot
 * @param {(dir: string) => string|undefined} [args.vaultFor]
 * @returns {Promise<ScenarioVerdict[]>}
 */
export async function runScenarios({ root, file, wiring, store, ledger, runsRoot, vaultFor }) {
  if (!existsSync(file)) return [];
  const suite = JSON.parse(readFileSync(file, "utf8"));
  const scenarios = Array.isArray(suite.scenarios) ? suite.scenarios : [];
  if (scenarios.length === 0) return [];

  /** @type {Map<string, import("./client.js").ModuleHandle>} */
  const open = new Map();
  const byName = new Map(wiring.nodes.map((n) => [n.name, n]));

  /** @param {string} name */
  const handleFor = (name) => {
    const existing = open.get(name);
    if (existing) return existing;
    const node = byName.get(name);
    if (!node) throw new Error(`step names "${name}", which the wiring does not declare`);
    const moduleDir = join(root, node.module);
    const handle = openModule({
      runnable: materialiseRunnable({ moduleDir, store, ledger, runsRoot, ...(vaultFor?.(moduleDir) ? { heldoutDir: /** @type {string} */ (vaultFor(moduleDir)) } : {}) }),
    });
    open.set(name, handle);
    return handle;
  };

  /** @type {ScenarioVerdict[]} */
  const out = [];
  try {
    for (const [i, s] of scenarios.entries()) {
      const name = s.name ?? `scenario ${i + 1}`;
      const started = Date.now();
      /** @type {string[]} */
      const says = [];
      /** @type {Record<string, any>} */
      const world = {};

      try {
        if (typeof s.given?.fixture === "string") {
          world["fixture"] = JSON.parse(readFileSync(join(root, s.given.fixture), "utf8"));
        }
        for (const [k, v] of Object.entries(s.given ?? {})) {
          if (k !== "fixture") world[k] = v;
        }

        for (const [j, step] of (s.steps ?? []).entries()) {
          const handle = handleFor(step.node);
          const input = resolve(step.in, world, `step ${j + 1} (${step.node}.${step.op})`);
          const result = await handle.call(step.op, input);
          if (step.as) world[step.as] = result;
        }

        for (const [path, want] of Object.entries(s.expect ?? {})) {
          const got = pick(world, path);
          if (!same(got, want)) {
            says.push(`${path} is ${JSON.stringify(got)}, expected ${JSON.stringify(want)}`);
          }
        }
        if (s.expectFailure) {
          says.push(`expected the chain to be refused with ${s.expectFailure}, and it ran to the end`);
        }
      } catch (err) {
        // A refusal mid-chain is sometimes the finding and sometimes the point.
        // Unexpected, it means one module would not accept what the one before it
        // produced — exactly what a scenario exists to discover. Expected, it is
        // the correct behaviour, and a gate unable to say so would push people
        // into only ever testing happy paths.
        const e = /** @type {any} */ (err);
        if (s.expectFailure) {
          if (e?.code !== s.expectFailure) says.push(`expected ${s.expectFailure}, got ${e?.code ?? "no code"} — ${e?.message ?? String(err)}`);
        } else {
          says.push(`${e?.module ? `${e.module}: ` : ""}${e?.code ? `${e.code} — ` : ""}${e?.message ?? String(err)}`);
        }
      }

      out.push({ name, ok: says.length === 0, says, ms: Date.now() - started });
    }
  } finally {
    for (const h of open.values()) h.close();
  }
  return out;
}

/**
 * Replace `{"$": "canonical.days"}` with the value from an earlier step.
 * @param {any} node @param {Record<string, any>} world @param {string} where
 * @returns {any}
 */
function resolve(node, world, where) {
  if (Array.isArray(node)) return node.map((n) => resolve(n, world, where));
  if (node === null || typeof node !== "object") return node;
  if (typeof node["$"] === "string") {
    const got = pick(world, node["$"]);
    if (got === undefined) throw new Error(`${where}: nothing named "${node["$"]}" has been produced yet`);
    return got;
  }
  /** @type {Record<string, any>} */
  const out = {};
  for (const [k, v] of Object.entries(node)) out[k] = resolve(v, world, where);
  return out;
}

/** @param {any} obj @param {string} path */
function pick(obj, path) {
  let node = obj;
  for (const step of path.split(".")) {
    if (node === null || typeof node !== "object") return undefined;
    node = Array.isArray(node) ? node[Number(step)] : node[step];
  }
  return node;
}

/** @param {any} a @param {any} b */
function same(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}
