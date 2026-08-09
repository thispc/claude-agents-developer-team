#!/usr/bin/env node
// The kernel's command line. Thin on purpose — every command below is a few
// lines over kernel/, because anything that decides something belongs in the
// kernel where it can be read in one sitting, not in an entry point.

import { existsSync, readdirSync, statSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  contractId, artifactDigest, loadManifest, Store, Ledger, Names, buildModule, loadWiring, shortDigest,
} from "../kernel/index.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const STORE = join(ROOT, ".store");
const RUNS = join(ROOT, ".runs");
const HELDOUT = join(ROOT, "heldout");
const WIRING = join(ROOT, "wiring.toml");

const [, , cmd, ...rest] = process.argv;
const flags = new Set(rest.filter((a) => a.startsWith("--")));
const args = rest.filter((a) => !a.startsWith("--"));

try {
  await main();
} catch (err) {
  console.error(`\n  ${err instanceof Error ? err.message : String(err)}\n`);
  process.exit(1);
}

async function main() {
  switch (cmd) {
    case "id": return cmdId();
    case "build": return await cmdBuild(false);
    case "gate": return await cmdBuild(true);
    case "ledger": return cmdLedger();
    case "lookup": return cmdLookup();
    case "graph": return cmdGraph();
    default: return usage();
  }
}

function usage() {
  console.log(`
  devteam — modules as verified artifacts, agents as tactics

    devteam id <module>        the contract id, and every file that went into it
    devteam build [module...]  verify and, if it passes, admit. Default: everything wired.
    devteam gate               the same, but exits non-zero on any refusal (for CI)
    devteam ledger [contract]  what has satisfied which contract
    devteam lookup <prefix>    what a digest refers to, in words
    devteam graph              the wiring, as the kernel reads it

  flags
    --force      re-judge even when the ledger already has this exact artifact
    --hermetic   refuse to fall back to the host runner when docker is absent
`);
}

/** Every module the wiring declares, or the paths named on the command line. */
function targets() {
  if (args.length > 0) return args.map((a) => resolve(a));
  if (existsSync(WIRING)) return loadWiring(WIRING, ROOT).nodes.map((n) => join(ROOT, n.module));
  const dir = join(ROOT, "modules");
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .map((d) => join(dir, d))
    .filter((d) => statSync(d).isDirectory() && existsSync(join(d, "module.toml")));
}

function cmdId() {
  for (const dir of targets()) {
    const vault = vaultFor(dir);
    const c = contractId(dir, vault);
    const a = artifactDigest(dir);
    console.log(`\n  ${c.name}`);
    console.log(`    contract  ${c.id}`);
    console.log(`    artifact  ${a.digest}   (${a.loc} lines)`);
    console.log(`    hashed:`);
    for (const [label, files] of Object.entries(c.files)) {
      // The vault is hashed but not listed. Its filenames are a weak hint about
      // what it checks, and this command is the sort of thing an agent gets to
      // run; the count is what a person actually needs.
      if (label === "heldout") continue;
      for (const f of files) console.log(`      ${label.padEnd(9)} ${f.path}  ${shortDigest(f.sha256)}`);
    }
    console.log(`      heldout   ${c.files.heldout.length} file(s), hashed but not named here`);
    console.log(`    not hashed: the prose. Reword it freely — the contract id does not move.`);
  }
  console.log("");
}

/** The vault for a module, if it has one. @param {string} moduleDir */
function vaultFor(moduleDir) {
  const name = loadManifest(moduleDir).name;
  const dir = join(HELDOUT, name);
  return existsSync(dir) ? dir : undefined;
}

/** @param {boolean} isGate */
async function cmdBuild(isGate) {
  const store = new Store(STORE);
  const ledger = new Ledger(join(STORE, "ledger.jsonl"));
  const names = new Names(join(STORE, "names.json"));
  let refused = 0;

  for (const dir of targets()) {
    const r = await buildModule({
      moduleDir: dir, store, ledger, runsRoot: RUNS, heldoutRoot: HELDOUT,
      requireHermetic: flags.has("--hermetic"),
      force: flags.has("--force"),
    });
    const mark = r.status === "admitted" ? "✓" : r.status === "hit" ? "·" : "✗";
    console.log(`  ${mark} ${r.summary}`);
    if (r.verdict) {
      for (const g of r.verdict.gates) console.log(`      ${g.ok ? "ok  " : "FAIL"} ${g.name.padEnd(8)} ${g.detail}`);
      if (r.verdict.proposeSplit) {
        console.log(`      note  this module is large enough that a split should be PROPOSED.`);
        console.log(`            Nothing happens automatically: an agent may draft two contracts and a`);
        console.log(`            wiring diff, the pair must reproduce this module's stored behaviour, and`);
        console.log(`            you merge the diff. A module never divides itself.`);
      }
    }
    if (r.status === "admitted") {
      names.set(r.artifact, r.module, `admitted for contract ${shortDigest(r.contract)}`, new Date().toISOString());
    }
    if (r.status === "refused") refused++;
  }

  if (refused > 0 && isGate) {
    console.log(`\n  ${refused} module(s) refused. Nothing was admitted for them and nothing that was live changed.\n`);
    process.exit(1);
  }
  console.log("");
}

function cmdLedger() {
  const ledger = new Ledger(join(STORE, "ledger.jsonl"));
  const wanted = args[0];
  const contracts = wanted ? [wanted] : ledger.contracts();
  if (contracts.length === 0) return console.log("\n  the ledger is empty — nothing has been built yet\n");

  for (const c of contracts) {
    const admitted = ledger.admitted(c);
    const rejected = ledger.rejected(c);
    const live = ledger.live(c);
    console.log(`\n  ${c}`);
    console.log(`    ${admitted.length} artifact(s) satisfy this contract — it is a relation, not a function`);
    for (const r of admitted) {
      const isLive = live?.artifact === r.artifact;
      console.log(`      ${isLive ? "→" : " "} ${shortDigest(r.artifact)}  ${r.at}  ${r.module}  proved: ${r.proved.join(", ")}`);
    }
    if (live) console.log(`    live via ${live.via}`);
    for (const r of rejected) console.log(`      ✗ ${shortDigest(r.artifact)}  ${r.at}  refused at ${r.gate}: ${r.why}`);
  }
  console.log("");
}

function cmdLookup() {
  const prefix = args[0];
  if (!prefix) throw new Error("lookup needs a digest or a prefix of one");
  const names = new Names(join(STORE, "names.json"));
  const hit = names.lookup(prefix);
  if (!hit) return console.log(`\n  nothing in the store starts with ${prefix}\n`);
  if ("ambiguous" in hit) return console.log(`\n  ${prefix} matches ${hit.ambiguous.length} artifacts:\n${hit.ambiguous.map((h) => "    " + h).join("\n")}\n`);
  console.log(`\n  ${hit.digest}\n    module ${hit.entry.module}\n    ${hit.entry.at}\n    ${hit.entry.note}\n`);
}

function cmdGraph() {
  const w = loadWiring(WIRING, ROOT);
  console.log(`\n  ${w.nodes.length} nodes, ${w.edges.length} edges — from ${w.path}, which only a human edits\n`);
  for (const n of w.nodes) console.log(`    ${n.name.padEnd(16)} ${n.module}`);
  console.log("");
  for (const e of w.edges) console.log(`    ${e.from} → ${e.to}${e.why ? `   (${e.why})` : ""}`);
  console.log("");
}
