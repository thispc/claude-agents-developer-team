// verify(contract, artifact) → bool. This is the compiler.
//
// Everything else in the system is search: expensive, retryable, allowed to be
// wrong, and thrown away afterwards. This file is the part that decides, and it
// is the only part that is trusted. It is hand-written, it is small, and it can
// never be delegated to an agent — because it is the thing that judges agents,
// and asking a searcher to also be its own judge is the one experiment that has
// already been run.
//
// It was run by the Darwin Gödel Machine. Told to reduce its own rate of tool-use
// hallucination, and scored by a detector that looked for particular markers, one
// variant deleted the markers — the paper's words are "hacking our hallucination
// detection function to report false successes" — despite being explicitly
// instructed not to. AlphaEvolve, doing far more open-ended work, did not suffer
// the equivalent failure, and the structural difference is exactly one thing: its
// evaluator is supplied from outside and cannot be modified by the system being
// evaluated. That single property is why it could be trusted to find a 4×4 matrix
// multiplication in 48 multiplications and recover 7% of Google's fleet-wide
// compute without anybody having to audit its taste.
//
// So the rule this file exists to enforce is not "agents should not edit the
// verifier." It is that they cannot: the verifier, the contract, the tests and
// the wiring are all outside the workspace an agent is ever handed, and the
// sandbox mounts the whole working tree read-only so that a test cannot be
// rewritten even from inside the run it is judging.
//
// THE EXIT CODE IS THE WHOLE API. Zero means the suite passed. Anything else
// means it did not. No parsing of output, no interpreting of a report format, no
// heuristics about what "mostly passed" means — because every one of those is a
// place where a judgement call could creep back into the trusted base.

import { spawn } from "node:child_process";
import { mkdirSync, rmSync, copyFileSync, existsSync, readFileSync, chmodSync } from "node:fs";
import { join, dirname } from "node:path";
import { contractId, artifactDigest, loadManifest, sha256, walk } from "./contract.js";
import { sizeGate } from "./sizegate.js";

/**
 * @typedef {object} Gate
 * @property {string} name
 * @property {boolean} ok
 * @property {string} detail
 */

/**
 * @typedef {object} Verdict
 * @property {boolean} ok
 * @property {string} contract
 * @property {string} artifact
 * @property {string} module
 * @property {Gate[]} gates
 * @property {"docker"|"host"} runner
 * @property {boolean} hermetic
 * @property {boolean} proposeSplit
 * @property {number} ms
 */

/**
 * @typedef {object} Toolchain
 * @property {string} image
 * @property {string[]} test
 * @property {number} [timeout_sec]
 * @property {string} [memory]
 * @property {number} [pids]
 */

/**
 * Judge one module as it currently sits on disk.
 *
 * @param {object} args
 * @param {string} args.moduleDir
 * @param {string} args.runsRoot     where sandbox working trees are assembled
 * @param {string} [args.heldoutDir] the vault for this module, if one exists
 * @param {boolean} [args.requireHermetic] refuse to fall back to the host runner
 * @returns {Promise<Verdict>}
 */
export async function verify({ moduleDir, runsRoot, heldoutDir, requireHermetic = false }) {
  const started = Date.now();
  const c = contractId(moduleDir, heldoutDir);
  const a = artifactDigest(moduleDir);
  const manifest = loadManifest(moduleDir);
  /** @type {Gate[]} */
  const gates = [];

  // ── gate 1: size. Free, and it runs first because it is the one that catches
  // the measured dominant failure before a single container starts.
  const ifacePath = join(moduleDir, /** @type {string} */ (c.files.interface[0]?.path));
  /** @type {{operations?: Record<string, {errors?: string[]}>}} */
  let iface;
  try {
    iface = JSON.parse(readFileSync(ifacePath, "utf8"));
  } catch (err) {
    return done({ ok: false, gates: [{ name: "interface", ok: false, detail: `${ifacePath} is not readable JSON: ${errText(err)}` }] });
  }
  const size = sizeGate({ loc: a.loc, iface, limits: manifest.limits });
  gates.push({
    name: "size",
    ok: size.ok,
    detail: size.reasons.length ? size.reasons.join(" ") : `${size.loc} lines, ${size.operations} operations, contract surface ${size.surface} — inside the cap`,
  });
  if (!size.ok) return done({ ok: false, gates, proposeSplit: size.proposeSplit });

  // ── the sandbox working tree. Contract and implementation together, because a
  // test importing "../run.js" is how a person would write it; the tree is
  // mounted read-only, so naturalness costs nothing in safety.
  const runId = `${manifest.name}-${a.digest.slice(2, 12)}-${process.pid}`;
  const work = join(runsRoot, runId, "work");
  const toolchain = readToolchain(moduleDir, c);

  try {
    assemble(moduleDir, work, [...c.files.interface, ...c.files.tests, ...c.files.toolchain, ...a.files]);

    // ── gate 2: the visible suite.
    const visible = await runSuite({ work, toolchain, requireHermetic });
    gates.push({
      name: "tests",
      ok: visible.ok,
      detail: visible.ok
        ? `the suite passed (${visible.runner}${visible.hermetic ? ", hermetic" : ", NOT hermetic — host runner"}, ${visible.ms}ms)`
        : `exit ${visible.code}${visible.timedOut ? " (timed out)" : ""} — ${firstUsefulLine(visible.output)}`,
    });
    if (!visible.ok) {
      return done({ ok: false, gates, runner: visible.runner, hermetic: visible.hermetic, proposeSplit: size.proposeSplit });
    }

    // ── gate 3: the held-out suite. Runs in a tree the implementer never saw and
    // cannot have optimised against, which is the only unbiased signal available
    // once an agent has saturated the tests it can read.
    if (heldoutDir && existsSync(heldoutDir)) {
      const vaultFiles = walk(heldoutDir).map((p) => ({ path: p, sha256: "", exec: false }));
      const vaultWork = join(runsRoot, runId, "heldout");
      assemble(moduleDir, vaultWork, [...c.files.interface, ...c.files.toolchain, ...a.files]);
      for (const f of vaultFiles) {
        const dest = join(vaultWork, "tests", f.path);
        mkdirSync(dirname(dest), { recursive: true });
        copyFileSync(join(heldoutDir, f.path), dest);
        chmodSync(dest, 0o444);
      }
      const held = await runSuite({ work: vaultWork, toolchain, requireHermetic });
      gates.push({
        name: "heldout",
        ok: held.ok,
        detail: held.ok
          ? `${vaultFiles.length} held-out test file(s) passed`
          : `exit ${held.code}${held.timedOut ? " (timed out)" : ""} — ${firstUsefulLine(held.output)}`,
      });
      if (!held.ok) {
        return done({ ok: false, gates, runner: held.runner, hermetic: held.hermetic, proposeSplit: size.proposeSplit });
      }
      return done({ ok: true, gates, runner: held.runner, hermetic: visible.hermetic && held.hermetic, proposeSplit: size.proposeSplit });
    }

    gates.push({
      name: "heldout",
      ok: true,
      detail: "no vault for this module — nothing independent checked it, so a pass here is weaker than it looks",
    });
    return done({ ok: true, gates, runner: visible.runner, hermetic: visible.hermetic, proposeSplit: size.proposeSplit });
  } finally {
    rmSync(join(runsRoot, runId), { recursive: true, force: true });
  }

  /**
   * @param {{ok: boolean, gates: Gate[], runner?: "docker"|"host", hermetic?: boolean, proposeSplit?: boolean}} r
   * @returns {Verdict}
   */
  function done(r) {
    return {
      ok: r.ok,
      contract: c.id,
      artifact: a.digest,
      module: manifest.name,
      gates: r.gates,
      runner: r.runner ?? "host",
      hermetic: r.hermetic ?? false,
      proposeSplit: r.proposeSplit ?? false,
      ms: Date.now() - started,
    };
  }
}

// ── the sandbox ──────────────────────────────────────────────────────────────

/**
 * Run the pinned test command over an assembled tree.
 *
 * Docker is the real answer: network denied outright, capabilities dropped, a
 * read-only mount so nothing in the tree can be rewritten mid-run, and a pinned
 * image so "it passed" is reproducible next month.
 *
 * The host fallback exists so the system is usable on a machine with no docker,
 * and it is deliberately worse: it can only scrub the environment and impose a
 * timeout. It cannot deny the network. Every verdict therefore carries
 * `hermetic`, and a run that fell back says so in the ledger for good — nobody
 * should be able to mistake one for the other later.
 *
 * @param {{work: string, toolchain: Toolchain, requireHermetic: boolean}} args
 */
async function runSuite({ work, toolchain, requireHermetic }) {
  const timeoutMs = (toolchain.timeout_sec ?? 120) * 1000;
  if (await dockerAvailable()) {
    const args = [
      "run", "--rm",
      "--network=none",                    // the security control, not just hermeticity
      "--cap-drop", "ALL",
      "--security-opt", "no-new-privileges",
      "--memory", toolchain.memory ?? "512m",
      "--pids-limit", String(toolchain.pids ?? 256),
      "-v", `${work}:/work:ro`,            // a test cannot be rewritten by the run it judges
      "--tmpfs", "/tmp:rw,size=64m",
      "-w", "/work",
      "-e", "HOME=/tmp",
      toolchain.image,
      ...toolchain.test,
    ];
    const r = await run("docker", args, { timeoutMs, cwd: work, env: minimalEnv() });
    return { ...r, ok: r.code === 0, runner: /** @type {"docker"} */ ("docker"), hermetic: true };
  }

  if (requireHermetic) {
    return {
      ok: false, code: -1, timedOut: false, ms: 0,
      output: "docker is not available and a hermetic run was required. Start docker, or drop --hermetic to accept a host run that cannot deny the network.",
      runner: /** @type {"host"} */ ("host"), hermetic: false,
    };
  }

  const [cmd, ...rest] = toolchain.test;
  if (!cmd) return { ok: false, code: -1, timedOut: false, ms: 0, output: "toolchain.test is empty", runner: /** @type {"host"} */ ("host"), hermetic: false };
  const r = await run(cmd, rest, { timeoutMs, cwd: work, env: minimalEnv() });
  return { ...r, ok: r.code === 0, runner: /** @type {"host"} */ ("host"), hermetic: false };
}

/** @type {boolean|null} */
let dockerCache = null;
async function dockerAvailable() {
  if (dockerCache !== null) return dockerCache;
  const r = await run("docker", ["version", "--format", "{{.Server.Version}}"], { timeoutMs: 5000 });
  dockerCache = r.code === 0;
  return dockerCache;
}

/**
 * A child process with a hard deadline. On timeout the whole process group is
 * killed, not just the child — agent-written code hangs, and a test runner that
 * spawned something of its own would otherwise keep the sandbox alive.
 *
 * @param {string} cmd @param {string[]} args
 * @param {{timeoutMs: number, cwd?: string, env?: NodeJS.ProcessEnv}} opts
 * @returns {Promise<{code: number, output: string, timedOut: boolean, ms: number}>}
 */
function run(cmd, args, opts) {
  return new Promise((resolve) => {
    const started = Date.now();
    let output = "";
    let timedOut = false;
    /** @type {import("node:child_process").ChildProcess} */
    let child;
    try {
      child = spawn(cmd, args, { cwd: opts.cwd, env: opts.env ?? minimalEnv(), detached: true, stdio: ["ignore", "pipe", "pipe"] });
    } catch (err) {
      resolve({ code: -1, output: errText(err), timedOut: false, ms: Date.now() - started });
      return;
    }
    const cap = 64 * 1024;   // enough to explain a failure, bounded so a loop cannot fill memory
    /** @param {Buffer} b */
    const take = (b) => { if (output.length < cap) output += b.toString("utf8"); };
    child.stdout?.on("data", take);
    child.stderr?.on("data", take);

    const timer = setTimeout(() => {
      timedOut = true;
      try { process.kill(-(/** @type {number} */ (child.pid)), "SIGKILL"); } catch { /* already gone */ }
    }, opts.timeoutMs);

    child.on("error", (err) => {
      clearTimeout(timer);
      resolve({ code: -1, output: output + errText(err), timedOut, ms: Date.now() - started });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: timedOut ? 124 : code ?? -1, output, timedOut, ms: Date.now() - started });
    });
  });
}

/**
 * The environment a suite runs in. Nothing inherited.
 *
 * v1 shipped the opposite of this and paid for it: workers inherited the
 * operator's CLAUDE_CODE_* variables, so a sandboxed run had the operator's
 * credentials sitting in its environment. A test suite needs a PATH and a place
 * to write. It does not need anyone's API keys, and a suite that can reach a
 * model can be a suite that asks the model for the answer.
 */
function minimalEnv() {
  return {
    PATH: process.env["PATH"] ?? "/usr/bin:/bin:/usr/local/bin",
    HOME: "/tmp",
    LANG: "C.UTF-8",
    // Test runners that colour their output are harder to read in a ledger.
    NO_COLOR: "1",
    CI: "1",
  };
}

// ── assembling the tree ──────────────────────────────────────────────────────

/**
 * @param {string} moduleDir @param {string} dest
 * @param {{path: string}[]} files
 */
function assemble(moduleDir, dest, files) {
  mkdirSync(dest, { recursive: true });
  for (const f of files) {
    const out = join(dest, f.path);
    mkdirSync(dirname(out), { recursive: true });
    copyFileSync(join(moduleDir, f.path), out);
  }
  return dest;
}

/**
 * @param {string} moduleDir
 * @param {ReturnType<typeof contractId>} c
 * @returns {Toolchain}
 */
function readToolchain(moduleDir, c) {
  const rel = c.files.toolchain[0]?.path;
  if (!rel) throw new Error(`${moduleDir}: [contract] toolchain resolved to no file`);
  const t = JSON.parse(readFileSync(join(moduleDir, rel), "utf8"));
  if (typeof t.image !== "string" || !Array.isArray(t.test) || t.test.length === 0) {
    throw new Error(`${join(moduleDir, rel)}: needs an "image" string and a non-empty "test" array. The test command's exit code is the entire admission signal, so it has to be stated, not guessed.`);
  }
  return t;
}

/** @param {unknown} err */
function errText(err) {
  return err instanceof Error ? err.message : String(err);
}

/**
 * The one line a verdict shows. Worth some care: this string is what a person
 * reads six weeks later when the ledger says something was refused, and "exit 1"
 * would waste the trip.
 *
 * Order matters. TAP announces each containing block as `# Subtest: <name>`
 * before its children run, so the first line mentioning "fail" is very often the
 * NAME of a test rather than anything that failed — picking it produces a verdict
 * that reads like a reason and is not one.
 *
 * @param {string} output
 */
function firstUsefulLine(output) {
  const raw = output.split("\n");

  // TAP's `error:` field is usually a YAML block scalar — the line reads
  // `error: |-` and the actual message is the indented block underneath. Taking
  // the marker line gives a verdict that says "exit 1 — |-", which is how this
  // was first written and how it read.
  for (let i = 0; i < raw.length; i++) {
    const m = /^(\s*)error:\s*(.*)$/.exec(raw[i] ?? "");
    if (!m) continue;
    const indent = (m[1] ?? "").length;
    const inline = (m[2] ?? "").trim();
    if (inline !== "" && inline !== "|-" && inline !== "|" && inline !== ">-") return tidy(inline);
    const block = [];
    for (let j = i + 1; j < raw.length; j++) {
      const line = raw[j] ?? "";
      if (line.trim() === "") { if (block.length) break; else continue; }
      if ((line.length - line.trimStart().length) <= indent) break;
      block.push(line.trim());
    }
    if (block.length) return tidy(block.join(" "));
  }

  const lines = raw.map((l) => l.trim()).filter((l) => l !== "");
  const pick =
    lines.find((l) => /^not ok\s+\d/.test(l)) ??                       // a named failing assertion
    lines.find((l) => /^\w*Error\b|^\s*throw\b/.test(l)) ??            // an uncaught throw, which never reaches TAP
    lines.filter((l) => !l.startsWith("#") && !/^(TAP version|\d+\.\.\d+|---|\.\.\.)$/.test(l)).pop() ??
    lines[lines.length - 1] ??
    "(no output)";
  return tidy(pick);
}

/** @param {string} s */
function tidy(s) {
  const clean = s.replace(/^['"]|['"]$/g, "").replace(/\\n/g, " ").replace(/\s+/g, " ").trim();
  return clean.length > 180 ? clean.slice(0, 177) + "…" : clean;
}

export { sha256 };
