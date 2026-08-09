#!/usr/bin/env node
// The kernel's command line. Thin on purpose — every command below is a few
// lines over kernel/, because anything that decides something belongs in the
// kernel where it can be read in one sitting, not in an entry point.

import { existsSync, readdirSync, statSync, readFileSync, mkdirSync } from "node:fs";
import { join, dirname, resolve, relative } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
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
    case "atlas": return await cmdAtlas();
    case "pin": return cmdPin();
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
    devteam pin <contract> [artifact]   choose which artifact is live — e.g. which language
    devteam lookup <prefix>    what a digest refers to, in words
    devteam graph              the wiring, as the kernel reads it
    devteam atlas              the system describing itself, via its own render-graph module

  flags
    --force        re-judge even when the ledger already has this exact artifact
    --allow-host   accept a NON-hermetic run when docker is unavailable. The verdict
                   and the ledger record that the network was not denied.
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

/**
 * The Atlas — and the first place the platform composes one of its own modules
 * rather than merely judging it.
 *
 * Note WHICH copy it runs. Not `modules/render-graph/run.js` on disk, but the
 * artifact the ledger says is live, materialised out of the content-addressed
 * store. That distinction is the whole design in one command: break the working
 * copy and this keeps working, because the admitted artifact was never touched.
 * A dev tool that imported the working copy would quietly make "passing means
 * live" decorative.
 *
 * The kernel itself never imports a module. Only this shell does, so a broken
 * module can never stop `devteam build` from being able to fix it.
 */
async function cmdAtlas() {
  const store = new Store(STORE);
  const ledger = new Ledger(join(STORE, "ledger.jsonl"));
  const wiring = loadWiring(WIRING, ROOT);

  /** @type {Record<string, {contract: string, loc: number, surface: number}>} */
  const facts = {};
  for (const n of wiring.nodes) {
    const dir = join(ROOT, n.module);
    const c = contractId(dir, vaultFor(dir));
    const a = artifactDigest(dir);
    const iface = JSON.parse(readFileSync(join(dir, /** @type {string} */ (c.files.interface.find((f) => f.path === "interface.json")?.path)), "utf8"));
    const ops = Object.keys(iface.operations ?? {});
    const errs = new Set(ops.flatMap((/** @type {string} */ o) => iface.operations[o].errors ?? []));
    facts[n.name] = { contract: c.id, loc: a.loc, surface: ops.length + errs.size };
  }

  const { render } = await liveModule("render-graph", store, ledger);
  const g = render({
    wiringPath: relative(ROOT, WIRING),
    wiringText: readFileSync(WIRING, "utf8"),
    nodes: wiring.nodes,
    edges: wiring.edges,
    ledger: ledger.all(),
    modules: facts,
  });

  console.log(`\n  ${g.summary}\n`);
  for (const n of g.nodes) {
    const dot = n.status === "live" ? "●" : n.status === "refused" ? "✗" : "○";
    console.log(`  ${dot} ${n.label.padEnd(14)} ${String(n.status).padEnd(8)} ${n.loc ?? "?"} lines · surface ${n.surface ?? "?"}`);
    if (n.proved?.length) console.log(`      proved   ${n.proved.join(", ")}`);
    if (n.note) console.log(`      note     ${n.note}`);
    for (const e of n.evidence) console.log(`      evidence ${e.file}:${e.line}`);
  }
  if (g.edges.length) console.log("");
  for (const e of g.edges) {
    console.log(`  ${e.from} → ${e.to}`);
    if (e.why) console.log(`      ${e.why}`);
    console.log(`      evidence ${e.evidence[0]?.file}:${e.evidence[0]?.line}`);
  }
  for (const d of g.dropped) console.log(`\n  DROPPED ${d.what} — ${d.why}`);
  console.log("");
}

/**
 * Import a module's LIVE artifact out of the store.
 * @param {string} name @param {import("../kernel/index.js").Store} store @param {import("../kernel/index.js").Ledger} ledger
 * @returns {Promise<any>}
 */
async function liveModule(name, store, ledger) {
  const dir = join(ROOT, "modules", name);
  const c = contractId(dir, vaultFor(dir));
  const live = ledger.live(c.id);
  if (!live) {
    throw new Error(`${name}: nothing is admitted for the current contract.\n  Run \`node bin/devteam.js build\` — and if it is refused, that refusal is the answer, not an obstacle to work around.`);
  }
  const out = join(RUNS, "live", live.artifact);
  if (!existsSync(join(out, "run.js"))) {
    mkdirSync(out, { recursive: true });
    store.materialise(live.artifact, out);
  }
  return import(pathToFileURL(join(out, "run.js")).href);
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
      // Fails CLOSED. Without docker the sandbox cannot deny the network, and a
      // gate that quietly weakens itself when its sandbox is unavailable is not a
      // gate. --allow-host is the explicit, recorded way to accept a weaker run.
      requireHermetic: !flags.has("--allow-host"),
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
  // Prefixes, because nobody types 64 hex characters.
  const contracts = wanted ? ledger.contracts().filter((c) => c.startsWith(wanted)) : ledger.contracts();
  if (contracts.length === 0) {
    return console.log(wanted ? `\n  no contract starts with ${wanted}\n` : "\n  the ledger is empty — nothing has been built yet\n");
  }

  for (const c of contracts) {
    const admitted = distinct(ledger.admitted(c));
    const rejected = ledger.rejected(c);
    const live = ledger.live(c);
    console.log(`\n  ${c}`);
    console.log(`    ${admitted.length} artifact(s) satisfy this contract — it is a relation, not a function`);
    for (const r of admitted) {
      const isLive = live?.artifact === r.artifact;
      const lang = r.language ? ` ${r.language.padEnd(3)}` : "  ? ";
      const seal = r.hermetic === false ? "  NOT HERMETIC" : "";
      console.log(`      ${isLive ? "→" : " "} ${shortDigest(r.artifact)} ${lang}  ${r.at}  ${r.loc ?? "?"} lines  proved: ${r.proved.join(", ")}${seal}`);
    }
    if (live) console.log(`    live via ${live.via}`);
    for (const r of rejected) console.log(`      ✗ ${shortDigest(r.artifact)}  ${r.at}  refused at ${r.gate}: ${r.why}`);
  }
  console.log("");
}

/**
 * One row per artifact, keeping the most recent record.
 *
 * Re-judging the same bytes appends another admit line — the ledger is
 * append-only and that history is worth keeping — but showing the same artifact
 * three times makes a relation of two look like a relation of three.
 * @param {import("../kernel/index.js").Ledger extends never ? never : any[]} records
 */
function distinct(records) {
  /** @type {Map<string, any>} */
  const byArtifact = new Map();
  for (const r of records) byArtifact.set(r.artifact, r);
  return [...byArtifact.values()];
}

/**
 * Choose which artifact is live for a contract.
 *
 * This is the command that makes many-implementations-per-contract worth having.
 * Once two artifacts satisfy the same contract — say a JavaScript one and a
 * Python one — "which is live" stops being a fact about the code and becomes a
 * decision. Nothing is deleted either way, and switching back is another line.
 *
 * A pin outranks any later auto-admit, permanently. Auto-admit exists so the
 * system can improve itself unattended; the pin is how a person says "no, this
 * one" and has it stay said.
 */
function cmdPin() {
  const [contractPrefix, artifactPrefix] = args;
  const ledger = new Ledger(join(STORE, "ledger.jsonl"));

  if (!contractPrefix) throw new Error("pin needs a contract, and an artifact to point it at:\n  devteam pin <contract-prefix> <artifact-prefix>");

  const contracts = ledger.contracts().filter((c) => c.startsWith(contractPrefix));
  if (contracts.length === 0) throw new Error(`no contract starts with ${contractPrefix}`);
  if (contracts.length > 1) throw new Error(`${contractPrefix} matches ${contracts.length} contracts — be more specific`);
  const contract = /** @type {string} */ (contracts[0]);

  const admitted = distinct(ledger.admitted(contract));
  if (!artifactPrefix) {
    console.log(`\n  ${contract}\n  ${admitted.length} artifact(s) satisfy it. Name one:\n`);
    for (const r of admitted) {
      console.log(`    ${shortDigest(r.artifact)}  ${(r.language ?? "?").padEnd(4)}  ${r.at}  ${r.loc ?? "?"} lines`);
    }
    console.log("");
    return;
  }

  const matches = admitted.filter((r) => r.artifact.startsWith(artifactPrefix));
  if (matches.length === 0) throw new Error(`no admitted artifact for this contract starts with ${artifactPrefix}.\n  Pinning is only ever a choice between things that already passed — it is not a way to put something live that did not.`);
  if (matches.length > 1) throw new Error(`${artifactPrefix} matches ${matches.length} artifacts — be more specific`);
  const chosen = /** @type {import("../kernel/index.js").Ledger extends never ? never : any} */ (matches[0]);

  ledger.append({
    t: "pin",
    contract,
    artifact: chosen.artifact,
    module: chosen.module,
    at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
    by: "owner",
    ...(chosen.language ? { why: `chose the ${chosen.language} implementation` } : {}),
  });
  console.log(`\n  pinned ${chosen.module} → ${shortDigest(chosen.artifact)}${chosen.language ? ` (${chosen.language})` : ""}`);
  console.log(`  Auto-admit cannot move this. Nothing was deleted; switching back is another pin.\n`);
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
