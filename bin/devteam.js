#!/usr/bin/env node
// The kernel's command line. Thin on purpose — every command below is a few
// lines over kernel/, because anything that decides something belongs in the
// kernel where it can be read in one sitting, not in an entry point.

import { existsSync, readdirSync, statSync, readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { join, dirname, resolve, relative } from "node:path";
import { fileURLToPath } from "node:url";
import {
  contractId, artifactDigest, loadManifest, materialiseRunnable, openModule, checkFit, runScenarios, differ, Store, Ledger, Names, buildModule, loadWiring, shortDigest,
} from "../kernel/index.js";

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// Flags are separated from positionals across the WHOLE argv, so `--in=x build`
// and `build --in=x` mean the same thing. A tool where option order changes what
// runs is a tool people learn to distrust.
const argv = process.argv.slice(2);
const flags = new Set(argv.filter((a) => a.startsWith("--") && !a.startsWith("--in=")));
const positional = argv.filter((a) => !a.startsWith("--"));
const cmd = positional[0];
const args = positional.slice(1);

// A WORKSPACE is a directory with a wiring.toml, its own modules/ and its own
// heldout/. The repo root is one; `--in=apps/weather` is another.
//
// The STORE is deliberately NOT per-workspace. Artifacts are content-addressed,
// so two workspaces that happen to build the same bytes share them for free, and
// a module lifted from one project into another is already admitted. A store per
// project would throw that away and duplicate every blob.
const inFlag = argv.find((a) => a.startsWith("--in="));
const ROOT = inFlag ? resolve(REPO, inFlag.slice("--in=".length)) : REPO;
const STORE = join(REPO, ".store");
const RUNS = join(REPO, ".runs");
const HELDOUT = join(ROOT, "heldout");
const WIRING = join(ROOT, "wiring.toml");

if (!existsSync(WIRING) && cmd && cmd !== "lookup") {
  console.error(`\n  ${relative(REPO, WIRING) || WIRING} does not exist — that directory is not a workspace.\n  A workspace is a folder holding wiring.toml, modules/ and heldout/.\n`);
  process.exit(1);
}

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
    case "fit": return await cmdFit();
    case "differ": return await cmdDiffer();
    case "pin": return cmdPin();
    case "ui": return await cmdUi();
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
    devteam fit                do the modules actually fit together? (the composition gate)
    devteam differ [module]    fire generated inputs at every implementation of one contract.
                               Any disagreement is a hole in the CONTRACT, not in either of them.
    devteam graph              the wiring, as the kernel reads it
    devteam atlas              the system describing itself, via its own render-graph module
    devteam ui                 the same, in a browser, with a button that runs the real gates

  where
    --in=<dir>     work in another workspace — a folder with its own wiring.toml,
                   modules/ and heldout/. e.g. --in=apps/weather. The artifact
                   store is shared across all of them, because artifacts are
                   content-addressed and identical bytes are identical bytes.

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
 * Everything the Atlas and the UI both need: the wiring, the ledger, and each
 * module's contract facts. Gathered once so the two views cannot disagree about
 * what is true.
 *
 * Note WHICH copy of render-graph it runs — not the file on disk, but the
 * artifact the ledger says is live, materialised out of the content-addressed
 * store. Break the working copy and both views keep working, because the
 * admitted artifact was never touched. A tool that imported the working copy
 * would quietly make "passing means live" decorative.
 *
 * The kernel itself never imports a module. Only this shell does, so a broken
 * module can never stop `devteam build` from being able to fix it.
 */
async function gatherState() {
  const store = new Store(STORE);
  const ledger = new Ledger(join(STORE, "ledger.jsonl"));
  const wiring = loadWiring(WIRING, ROOT);

  /** @type {Record<string, any>} */
  const facts = {};
  for (const n of wiring.nodes) {
    const dir = join(ROOT, n.module);
    const c = contractId(dir, vaultFor(dir));
    const a = artifactDigest(dir);
    const iface = JSON.parse(readFileSync(join(dir, "interface.json"), "utf8"));
    const ops = Object.keys(iface.operations ?? {});
    const errs = new Set(ops.flatMap((/** @type {string} */ o) => iface.operations[o].errors ?? []));
    facts[n.name] = { contract: c.id, loc: a.loc, surface: ops.length + errs.size, language: a.language, portable: c.portable };
  }

  // SPAWNED, not imported. Until now this loaded agent-written code into the
  // same process that holds the ledger — an append-only file it could have
  // rewritten — with the operator's full environment. The weather app already
  // composed this way; the platform had not caught up with its own rule.
  const renderGraph = await liveModule("render-graph", store, ledger);
  const graph = await renderGraph.call("render", {
    wiringPath: relative(ROOT, WIRING),
    wiringText: readFileSync(WIRING, "utf8"),
    nodes: wiring.nodes,
    edges: wiring.edges,
    ledger: ledger.all(),
    modules: facts,
  });
  renderGraph.close();

  // The relation, per node: every artifact admitted for that contract, so the
  // page can show that a slot has been filled more than once and in what.
  for (const node of graph.nodes) {
    const f = facts[node.id];
    if (!f) continue;
    node.portable = f.portable;

    const seen = new Map();
    for (const r of ledger.admitted(f.contract)) seen.set(r.artifact, r);

    // Language and size come from the artifact that is LIVE, not from the
    // working copy. Reading them off disk is the same mistake the composer made:
    // pin an older artifact and the card describes a file nobody is running.
    const liveRecord = seen.get(node.artifact);
    node.language = liveRecord?.language ?? f.language;
    node.loc = liveRecord?.loc ?? f.loc;

    if (seen.size > 1) {
      node.alternatives = [...seen.values()].map((r) => ({
        artifact: r.artifact, language: r.language, loc: r.loc, live: r.artifact === node.artifact,
      }));
    }
  }
  // Placement is a pure function of the graph, so it is a module like anything
  // else — the picture cannot drift from the system it claims to draw.
  const layoutGraph = await liveModule("layout-graph", store, ledger);
  const layout = await layoutGraph.call("layout", { nodes: graph.nodes, edges: graph.edges });
  layoutGraph.close();

  const fit = checkFit({ wiring, root: ROOT, vaultFor });
  return { store, ledger, graph, layout, fit };
}

async function cmdAtlas() {
  const { graph: g } = await gatherState();
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
/**
 * Load one of the PLATFORM's own modules — render-graph, inspect-ui.
 *
 * Always from the repo, never from the workspace being looked at. These are
 * devteam's viewing tools; a project has no reason to ship its own copy, and
 * looking for one inside `apps/weather/modules/` is how `devteam --in=apps/weather ui`
 * came to report that the weather app was missing a module called render-graph.
 *
 * @param {string} name @param {import("../kernel/index.js").Store} store @param {import("../kernel/index.js").Ledger} ledger
 */
async function liveModule(name, store, ledger) {
  const moduleDir = join(REPO, "modules", name);
  const vault = join(REPO, "heldout", name);
  return openModule({
    runnable: materialiseRunnable({ moduleDir, store, ledger, runsRoot: RUNS, ...(existsSync(vault) ? { heldoutDir: vault } : {}) }),
  });
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

  if (isGate) {
    const fit = checkFit({ wiring: loadWiring(WIRING, ROOT), root: ROOT, vaultFor });
    console.log(`  ${fit.ok ? "·" : "✗"} composition: ${fit.summary}`);
    for (const e of fit.edges.filter((x) => x.status === "broken")) {
      console.log(`      FAIL ${e.edge}`);
      for (const line of e.says) console.log(`           ${line}`);
    }
    const runs = await scenarios(loadWiring(WIRING, ROOT));
    if (runs.length > 0) console.log(`  ${runs.every((r) => r.ok) ? "·" : "✗"} scenarios: ${runs.filter((r) => r.ok).length}/${runs.length} ran the real chain end to end`);
    for (const r of runs.filter((x) => !x.ok)) {
      console.log(`      FAIL ${r.name}`);
      for (const line of r.says) console.log(`           ${line}`);
    }
    if (!fit.ok || runs.some((r) => !r.ok)) {
      console.log(`\n  The modules each pass and do not fit together. Nothing was admitted for the composition.\n`);
      process.exit(1);
    }
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
 * Serve the inspector.
 *
 * The page itself is rendered by `inspect-ui` — a module, with a contract, a
 * conformance suite and a size cap, admitted like everything else. This command
 * is only the plumbing around it: gather state, hand it over, put the bytes on a
 * socket. That split is deliberate. A dashboard written outside the system would
 * be the one piece of the platform nothing could verify, which is exactly the
 * unaccountable code that made v1 unimprovable.
 *
 * Bound to 127.0.0.1 with no authentication, because POST /api/verify starts
 * containers. Loopback IS the access control here; if this ever needs to listen
 * on a real interface, it needs a real answer first.
 */
async function cmdUi() {
  const { createServer } = await import("node:http");
  const port = Number(process.env["PORT"] ?? 7788);

  /** @type {any[]} */
  let lastRun = [];

  /** @type {import("node:http").RequestListener} */
  const handler = (req, res) => {
    /** @param {number} code @param {string} type @param {string} body */
    const send = (code, type, body) => {
      res.writeHead(code, { "content-type": type, "cache-control": "no-store" });
      res.end(body);
    };
    (async () => {
      if (req.method === "POST" && req.url === "/api/verify") {
        lastRun = await runAll();
        return send(200, "application/json", JSON.stringify(lastRun));
      }
      if (req.method === "POST" && req.url === "/api/pin") {
        const form = new URLSearchParams(await body(req));
        pinArtifact(form.get("contract") ?? "", form.get("artifact") ?? "");
        // See-other back to the page: a refresh must not re-submit the switch.
        res.writeHead(303, { location: "/" });
        return res.end();
      }

      const { store, ledger, graph, layout, fit } = await gatherState();
      if (req.url === "/api/state") return send(200, "application/json", JSON.stringify({ ...graph, layout, fit, verdicts: lastRun }));
      if (req.url !== "/") return send(404, "text/plain; charset=utf-8", "not found");

      const ui = await liveModule("inspect-ui", store, ledger);
      const { html } = await ui.call("page", { title: relative(REPO, ROOT) || "devteam", ...graph, layout, fit, verdicts: lastRun });
      ui.close();
      return send(200, "text/html; charset=utf-8", html);
    })().catch((err) => {
      const message = err instanceof Error ? err.message : String(err);
      // The failure a person will actually hit is inspect-ui itself being
      // refused. Say so in plain text rather than serving a blank page — the
      // whole point of this screen is to tell you when something did not pass.
      send(500, "text/plain; charset=utf-8", `devteam ui could not render:\n\n${message}\n`);
    });
  };

  // BOTH loopback addresses, and this is not belt-and-braces.
  //
  // On macOS `localhost` resolves to ::1 before 127.0.0.1. Binding IPv4 only
  // means a browser sent to `localhost` tries IPv6 first and gets a refusal;
  // whether it then falls back is up to the browser, and when it does the page
  // is slow for no visible reason. Binding 0.0.0.0 would fix it and is the wrong
  // fix — POST /api/verify starts containers, and loopback IS the access control
  // here. So: two listeners, both loopback, neither reachable from the network.
  const hosts = ["127.0.0.1", "::1"];
  let listening = 0;
  /** @type {string[]} */
  const bound = [];

  for (const host of hosts) {
    const s = createServer(handler);
    s.on("error", (/** @type {any} */ err) => {
      if (err.code === "EADDRINUSE") {
        console.error(`\n  Port ${port} is already in use — a devteam inspector is very likely already running.`);
        console.error(`  Open http://127.0.0.1:${port} , or run with a different port:  PORT=7789 devteam ui\n`);
        process.exit(1);
      }
      // A machine with IPv6 disabled is fine; one listener is enough.
      if (host !== "127.0.0.1" && (err.code === "EAFNOSUPPORT" || err.code === "EADDRNOTAVAIL")) return;
      throw err;
    });
    s.listen(port, host, () => {
      bound.push(host === "::1" ? `[::1]:${port}` : `${host}:${port}`);
      if (++listening === 1) {
        console.log(`\n  devteam inspector → http://127.0.0.1:${port}`);
        console.log(`  The page is rendered by the inspect-ui module's admitted artifact.`);
        console.log(`  "Verify all" runs the real gates in a network-denied container.`);
        console.log(`\n  This is a server: it holds the terminal until you press Ctrl-C.\n`);
      }
    });
  }
}

/** @param {import("node:http").IncomingMessage} req @returns {Promise<string>} */
function body(req) {
  return new Promise((resolve, reject) => {
    let out = "";
    // Bounded: a form with two hidden fields is a few hundred bytes, and an
    // unbounded read on a public-ish socket is a way to be knocked over.
    req.on("data", (c) => { if (out.length < 8192) out += c; });
    req.on("end", () => resolve(out));
    req.on("error", reject);
  });
}

/**
 * Switch which admitted artifact is live. Shared by the CLI and the page, so
 * both obey the same rule: this is a choice between artifacts that ALREADY
 * PASSED, never a way to put something live that did not.
 * @param {string} contractPrefix @param {string} artifactPrefix
 */
function pinArtifact(contractPrefix, artifactPrefix) {
  const ledger = new Ledger(join(STORE, "ledger.jsonl"));
  const contracts = ledger.contracts().filter((c) => c.startsWith(contractPrefix));
  if (contracts.length !== 1) throw new Error(`"${contractPrefix}" matches ${contracts.length} contracts`);
  const contract = /** @type {string} */ (contracts[0]);
  const matches = distinct(ledger.admitted(contract)).filter((r) => r.artifact.startsWith(artifactPrefix));
  if (matches.length !== 1) throw new Error(`"${artifactPrefix}" matches ${matches.length} admitted artifacts for that contract`);
  const chosen = matches[0];
  ledger.append({
    t: "pin", contract, artifact: chosen.artifact, module: chosen.module,
    at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), by: "owner",
    ...(chosen.language ? { why: `chose the ${chosen.language} implementation` } : {}),
  });
  return chosen;
}

/** Run every wired module through the real gates. @returns {Promise<any[]>} */
async function runAll() {
  const store = new Store(STORE);
  const ledger = new Ledger(join(STORE, "ledger.jsonl"));
  /** @type {any[]} */
  const out = [];
  for (const dir of targets()) {
    const r = await buildModule({
      moduleDir: dir, store, ledger, runsRoot: RUNS, heldoutRoot: HELDOUT,
      requireHermetic: !flags.has("--allow-host"),
      force: true,
    });
    out.push({ module: r.module, status: r.status, summary: r.summary, gates: r.verdict?.gates ?? [] });
  }
  return out;
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

  const chosen = pinArtifact(contract, artifactPrefix);
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

/**
 * The composition gate. Runs on schemas, before anything is started — no
 * containers, milliseconds — because the cheapest check should catch the most,
 * and a shape mismatch is both the commonest way a composition breaks and the
 * one that needs no execution to prove.
 */
async function cmdFit() {
  const wiring = loadWiring(WIRING, ROOT);
  const fit = checkFit({ wiring, root: ROOT, vaultFor });

  console.log(`\n  ${fit.summary}\n  ${fit.id}\n`);
  for (const e of fit.edges) {
    const mark = e.status === "fits" ? "ok  " : e.status === "broken" ? "FAIL" : "  ? ";
    console.log(`  ${mark} ${e.edge}`);
    for (const line of e.says) console.log(`         ${line}`);
  }
  if (fit.unchecked > 0) {
    console.log(`\n  ${fit.unchecked} edge(s) name no operations, so nothing about them was checked.`);
    console.log(`  An unchecked edge is drawn exactly like a checked one, which is why the count is printed.`);
  }
  const runs = await scenarios(wiring);
  if (runs.length > 0) {
    console.log(`\n  ${runs.filter((r) => r.ok).length}/${runs.length} scenario(s) ran the real chain end to end`);
    for (const r of runs) {
      console.log(`  ${r.ok ? "ok  " : "FAIL"} ${r.name}  (${r.ms}ms)`);
      for (const line of r.says) console.log(`         ${line}`);
    }
  } else {
    console.log(`\n  No scenarios. The edge check compares shapes; only a scenario can catch a value`);
    console.log(`  that is the right shape and the wrong content. Add ${relative(REPO, join(ROOT, "scenarios.json"))}.`);
  }
  console.log("");
  if (!fit.ok || runs.some((r) => !r.ok)) process.exit(1);
}

/** @param {ReturnType<typeof loadWiring>} wiring */
function scenarios(wiring) {
  return runScenarios({
    root: ROOT, file: join(ROOT, "scenarios.json"), wiring,
    store: new Store(STORE), ledger: new Ledger(join(STORE, "ledger.jsonl")),
    runsRoot: RUNS, vaultFor,
  });
}

/**
 * The differential gate.
 *
 * A contract written by whoever also wrote the implementation encodes one
 * understanding twice; its cases pass because both sides share the same blind
 * spots. A second implementation, written FROM the contract rather than from the
 * first one's code, does not share them — so every disagreement between them is
 * a place the contract failed to say something.
 */
async function cmdDiffer() {
  const store = new Store(STORE);
  const ledger = new Ledger(join(STORE, "ledger.jsonl"));
  const cases = Number(process.env["CASES"] ?? 200);
  let holes = 0;

  for (const dir of targets()) {
    const vault = vaultFor(dir);
    const r = await differ({ moduleDir: dir, store, ledger, runsRoot: RUNS, ...(vault ? { heldoutDir: vault } : {}), cases });
    const mark = r.implementations < 2 ? "  · " : r.holes.length ? "  ✗ " : "  ok ";
    console.log(`${mark}${r.module}: ${r.summary}`);
    for (const h of r.holes.slice(0, 6)) {
      console.log(`        ${h.operation}(${trim(JSON.stringify(h.input))})`);
      console.log(`          ${h.says}`);
      for (const [who, a] of Object.entries(h.answers)) {
        console.log(`            ${who.padEnd(14)} ${a.error ? `refused ${a.error}` : trim(JSON.stringify(a.out))}`);
      }
    }
    if (r.holes.length > 6) console.log(`        … and ${r.holes.length - 6} more`);
    holes += r.holes.length;
  }
  console.log(holes === 0 ? "\n  No disagreements. That is evidence the contract is decided, not proof.\n" : "");
  if (holes > 0) process.exit(1);
}

/** @param {string} s */
function trim(s) { return s.length > 110 ? s.slice(0, 107) + "…" : s; }

function cmdGraph() {
  const w = loadWiring(WIRING, ROOT);
  console.log(`\n  ${w.nodes.length} nodes, ${w.edges.length} edges — from ${w.path}, which only a human edits\n`);
  for (const n of w.nodes) console.log(`    ${n.name.padEnd(16)} ${n.module}`);
  console.log("");
  for (const e of w.edges) console.log(`    ${e.from} → ${e.to}${e.why ? `   (${e.why})` : ""}`);
  console.log("");
}
