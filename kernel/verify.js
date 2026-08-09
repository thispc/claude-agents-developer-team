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
import { mkdirSync, rmSync, copyFileSync, existsSync, readFileSync, writeFileSync, chmodSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { contractId, artifactDigest, loadManifest, readToolchain, sha256, walk } from "./contract.js";
import { sizeGate } from "./sizegate.js";
import { corpusFor } from "./generate.js";

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
 * @property {string} language  selects the kernel shim that starts this module and speaks to it
 * @property {string} entry     the file the serve shim loads to find the operations
 * @property {string[]} [test]  optional language-specific suite command
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
 * @param {boolean} [args.requireHermetic] refuse to fall back to the host runner.
 *   Defaults to TRUE and fails closed: without docker the sandbox cannot deny the
 *   network, and a gate that quietly weakens itself when its sandbox is missing is
 *   not a gate.
 * @returns {Promise<Verdict>}
 */
export async function verify({ moduleDir, runsRoot, heldoutDir, requireHermetic = true }) {
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
  const toolchain = readToolchain(moduleDir, manifest, { requireDigest: true });
  let hermetic = true;
  /** @type {"docker"|"host"} */
  let runner = "host";

  try {
    assemble(moduleDir, work, [...c.files.interface, ...c.files.conformance, ...c.files.tests, ...a.files]);
    installShims(work);
    // The corpus is generated HERE and written into the sandbox, so the drivers
    // only execute it. Generation duplicated into two driver languages would be
    // another place for them to disagree, and they already carry as much
    // duplicated judgement as is safe.
    writeCorpus(moduleDir, work, c, iface);

    // ── gate 2: CONFORMANCE. The gate that makes a module replaceable.
    //
    // It never imports the implementation. It starts it as a process and talks
    // to it over a pipe, which means it has no opinion about what language wrote
    // the answers — and that is the whole point. A module judged only by this
    // gate can be filled by any implementation that gives the same output for
    // the same input, which is exactly what "as long as tests pass I don't care
    // if it is written in Mandarin" asks for.
    if (c.files.conformance.length > 0) {
      const conf = await runDriver({ work, toolchain, manifest, requireHermetic, ambient: "baseline" });
      runner = conf.runner;
      hermetic = hermetic && conf.hermetic;
      gates.push({
        name: "conformance",
        ok: conf.ok,
        detail: conf.ok
          ? `${countCases(moduleDir, c)} case(s) + ${countProperties(moduleDir, c)} propert(ies) passed over the wire against a ${toolchain.language} implementation (${conf.runner}${conf.hermetic ? ", hermetic" : ", NOT hermetic"}, ${conf.ms}ms)`
          : `${firstUsefulLine(conf.output)}`,
      });
      if (!conf.ok) return done({ ok: false, gates, runner, hermetic, proposeSplit: size.proposeSplit });

      // ── gate 2b: DETERMINISM. The same suite again, in a deliberately hostile
      // world, requiring byte-identical answers.
      //
      // The suite on its own proves a module produced the right output ONCE. It
      // says nothing about whether it would do so again — a module can read a
      // clock, iterate a string set, or consult a locale and pass every case by
      // luck. That is the difference between "these tests passed" and "this
      // module is a function of its input", and only the second is worth
      // caching a verdict about, because a ledger hit is never re-verified.
      //
      // Its blind spot is stated honestly: perturbing the environment catches
      // nondeterminism probabilistically, and a module leaking one bit of
      // randomness escapes about half the time. That hole is closed elsewhere —
      // the serve shims freeze the clock and seed the generator before a line of
      // module code loads — so the two mechanisms are complementary rather than
      // redundant. This one catches what interposition cannot: hash ordering,
      // collation, timezone arithmetic, and anything read out of the environment.
      const impure = Array.isArray(/** @type {any} */ (iface).impure) ? /** @type {any} */ (iface).impure : [];
      const again = await runDriver({ work, toolchain, manifest, requireHermetic, ambient: "hostile" });
      const before = transcriptOf(conf.output);
      const after = transcriptOf(again.output);
      const stable = before !== null && before === after;

      gates.push({
        name: "determinism",
        ok: stable,
        detail: stable
          ? `identical answers under a different hash seed, locale, timezone, hostname, umask and clock${impure.length ? ` (declared impure: ${impure.join(", ")})` : ""}`
          : before === null || after === null
            ? `could not compare runs — the driver did not report a transcript. ${firstUsefulLine(again.output)}`
            : `the same inputs gave DIFFERENT answers on a second run (${String(before).slice(0, 12)} vs ${String(after).slice(0, 12)}). Something in this module depends on its surroundings: a string set's iteration order, a locale-sensitive comparison, a timezone, or something read from the environment. If it genuinely needs the clock or randomness, declare "impure" in interface.json — where it is hashed, and therefore visible.`,
      });
      if (!stable) return done({ ok: false, gates, runner, hermetic, proposeSplit: size.proposeSplit });
    }

    // ── gate 3: the language-specific suite, if the module has one. Its presence
    // is what makes a module NON-portable: these tests can only judge an
    // implementation in their own language.
    if (c.files.tests.length > 0) {
      if (!toolchain.test) {
        return done({ ok: false, gates: [...gates, { name: "tests", ok: false, detail: `${manifest.toolchain} declares no "test" command, but [contract] tests names ${c.files.tests.length} file(s). Either give it a command or move those cases into conformance.json, where any language can satisfy them.` }], proposeSplit: size.proposeSplit });
      }
      const visible = await runSuite({ work, toolchain, cmd: toolchain.test, requireHermetic });
      runner = visible.runner;
      hermetic = hermetic && visible.hermetic;
      gates.push({
        name: "tests",
        ok: visible.ok,
        detail: visible.ok
          ? `the ${toolchain.language} suite passed (${visible.runner}${visible.hermetic ? ", hermetic" : ", NOT hermetic — host runner"}, ${visible.ms}ms)`
          : `exit ${visible.code}${visible.timedOut ? " (timed out)" : ""} — ${firstUsefulLine(visible.output)}`,
      });
      if (!visible.ok) return done({ ok: false, gates, runner, hermetic, proposeSplit: size.proposeSplit });
    }

    // ── gate 4: the held-out suite. Runs in a tree the implementer never saw and
    // cannot have optimised against, which is the only unbiased signal available
    // once an agent has saturated the tests it can read.
    if (heldoutDir && existsSync(heldoutDir)) {
      const vaultFiles = walk(heldoutDir);
      const vaultWork = join(runsRoot, runId, "heldout");
      assemble(moduleDir, vaultWork, [...c.files.interface, ...c.files.conformance, ...a.files]);
      installShims(vaultWork);
      for (const rel of vaultFiles) {
        const dest = join(vaultWork, "tests", rel);
        mkdirSync(dirname(dest), { recursive: true });
        copyFileSync(join(heldoutDir, rel), dest);
        chmodSync(dest, 0o444);
      }
      // A language-specific module's vault holds tests in that language, run by
      // the toolchain's own command. A portable module's vault holds more
      // conformance cases, driven over the same wire — so a held-out test for a
      // portable module is portable too, and swapping the language does not
      // quietly discard the vault.
      const held = toolchain.test
        ? await runSuite({ work: vaultWork, toolchain, cmd: toolchain.test, requireHermetic })
        : await runDriver({ work: vaultWork, toolchain, manifest, requireHermetic, conformancePath: "tests/conformance.json" });
      runner = held.runner;
      hermetic = hermetic && held.hermetic;
      gates.push({
        name: "heldout",
        ok: held.ok,
        detail: held.ok
          ? `${vaultFiles.length} held-out file(s) passed`
          : `exit ${held.code}${held.timedOut ? " (timed out)" : ""} — ${firstUsefulLine(held.output)}`,
      });
      if (!held.ok) return done({ ok: false, gates, runner, hermetic, proposeSplit: size.proposeSplit });
      return done({ ok: true, gates, runner, hermetic, proposeSplit: size.proposeSplit });
    }

    gates.push({
      name: "heldout",
      ok: true,
      detail: "no vault for this module — nothing independent checked it, so a pass here is weaker than it looks",
    });
    return done({ ok: true, gates, runner, hermetic, proposeSplit: size.proposeSplit });
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
 * @param {{work: string, toolchain: Toolchain, cmd: string[], requireHermetic: boolean, ambient?: string}} args
 */
async function runSuite({ work, toolchain, cmd: command, requireHermetic, ambient }) {
  const timeoutMs = (toolchain.timeout_sec ?? 120) * 1000;
  const world = ambient ? AMBIENT[ambient] : undefined;
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
      ...(world?.hostname ? ["--hostname", world.hostname] : []),
      ...(world?.cpuset ? ["--cpuset-cpus", world.cpuset] : []),
      ...Object.entries(world?.env ?? {}).flatMap(([k, v]) => ["-e", `${k}=${v}`]),
      toolchain.image,
      // umask and the delay have to happen inside the container, so the command
      // is wrapped rather than run directly. `exec` keeps the process tree flat,
      // so the timeout still kills what it means to kill.
      ...(world ? ["sh", "-c", `umask ${world.umask}; ${world.delayS ? `sleep ${world.delayS}; ` : ""}exec "$@"`, "--", ...command] : command),
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

  const [cmd, ...rest] = command;
  if (!cmd) return { ok: false, code: -1, timedOut: false, ms: 0, output: "no command to run", runner: /** @type {"host"} */ ("host"), hermetic: false };
  const r = await run(cmd, rest, { timeoutMs, cwd: work, env: { ...minimalEnv(), ...(world?.env ?? {}) } });
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
 * Put the kernel's transport shims inside the sandbox tree.
 *
 * They are kernel code, not module code: the same serve and drive shims judge
 * every module of a given language, so a module cannot influence how it is
 * spoken to or how its answers are compared. They land under `.kernel/`, which
 * no manifest may claim, and the tree is mounted read-only anyway.
 *
 * @param {string} work
 */
function installShims(work) {
  const src = join(dirname(fileURLToPath(import.meta.url)), "transport");
  const dest = join(work, ".kernel");
  mkdirSync(dest, { recursive: true });
  for (const f of readdirSync(src)) {
    if (f.endsWith(".md")) continue;
    copyFileSync(join(src, f), join(dest, f));
  }

  // Make `export` mean ESM regardless of which Node is in the image.
  //
  // Without this marker a `.js` file is CommonJS, and `export function` is a
  // syntax error — except on Node versions new enough to guess from the syntax,
  // which some of the images here are and some are not. That difference was
  // real: the same module loaded inside node:20-alpine (20.20, which guesses)
  // and failed on the host (20.10, which does not). A module whose correctness
  // depends on a runtime's willingness to guess is a module that will break on
  // an image bump for reasons nobody will connect to the bump.
  //
  // Only written when the module did not bring its own, so a module that
  // declares itself CommonJS stays CommonJS.
  const marker = join(work, "package.json");
  if (!existsSync(marker)) writeFileSync(marker, JSON.stringify({ type: "module" }) + "\n");
}

/**
 * The two worlds a module must answer identically in.
 *
 * The gate runs the conformance suite twice and requires byte-identical
 * responses. Everything varied below was chosen for catch-rate per millisecond,
 * and three of the choices are less obvious than they look:
 *
 *   PYTHONHASHSEED — Python `dict` iteration is NOT affected by it; dicts have
 *     been insertion-ordered since 3.7 and iterate a dense array. `set` and
 *     `frozenset` OF STRINGS are affected, and that is the single highest-yield
 *     catch available for Python.
 *
 *   sv_SE, not fr_FR — French collates "ä" like "a" and never fires. Swedish
 *     sorts it after "z". The equivalent JavaScript risk is `Intl` /
 *     `localeCompare`, which reads LANG at runtime; both node:20-alpine and
 *     node:20-slim ship full ICU, so it is live.
 *
 *   Asia/Kolkata — a HALF-hour offset. Whole-hour offsets miss a class of
 *     date-arithmetic bugs that a :30 zone catches.
 *
 * `--cpuset-cpus` rather than `--cpus`: the latter is a CFS quota and leaves the
 * visible CPU count untouched, so it varies nothing a module can observe.
 *
 * Two runs of the SAME image are enough. Python hash randomisation is per
 * process, so two identical `docker run` invocations already diverge; a second
 * image would only add interpreter-version drift, which pinned digests control.
 *
 * WHAT IS DELIBERATELY NOT VARIED, and why, because a blind spot named is worth
 * more than a blind spot that looks like coverage:
 *
 *   the build path — Debian dropped this variation, and it was their HIGHEST
 *     yield one by a factor of five. The criterion they used was fidelity to the
 *     real environment rather than catch rate: their builders normalise the path,
 *     so varying it tested a difference that could no longer happen. Same here.
 *     Modules always run at /work, in production and under test alike.
 *
 *   directory ordering — the second-largest cause family in Debian, and
 *     unreachable from here: `disorderfs` is FUSE, so varying it needs
 *     `--device /dev/fuse --cap-add SYS_ADMIN`, which gives back most of what the
 *     sandbox exists to take away. Largely moot in this design anyway — a module
 *     is a pure function over stdin and reads no directory.
 *
 *   `setarch` / ASLR — blocked by Docker's default seccomp profile, which
 *     refuses `personality(ADDR_NO_RANDOMIZE)` precisely so that nothing in a
 *     container can switch ASLR off. Disabling seccomp to test determinism would
 *     be a poor trade.
 *
 *   faketime — the reproducible-builds tooling's own notes call it their biggest
 *     source of flaky failures: infinite builds, autotools producing different
 *     results with and without it, certificate expiry errors. A 1.1-second sleep
 *     catches the same clock reads with none of that.
 *
 * @type {Record<string, {hostname: string, umask: string, delayS: number, cpuset?: string, env: Record<string,string>}>}
 */
const AMBIENT = {
  baseline: {
    hostname: "aaaaaaaaaaaa",
    umask: "022",
    delayS: 0,
    env: { PYTHONHASHSEED: "0", TZ: "UTC", LANG: "C", LC_ALL: "C", SOURCE_DATE_EPOCH: "1000000000" },
  },
  hostile: {
    hostname: "zzzzzzzzzzzz",
    umask: "077",
    delayS: 1.1,
    cpuset: "0",
    env: { PYTHONHASHSEED: "4294967295", TZ: "Asia/Kolkata", LANG: "sv_SE.UTF-8", LC_ALL: "sv_SE.UTF-8", SOURCE_DATE_EPOCH: "1600000000" },
  },
};

/** The transcript digest a driver prints, or null if it did not get that far. @param {string} output */
function transcriptOf(output) {
  const m = /^#\s*transcript\s+([0-9a-f]{64})\s*$/m.exec(output);
  return m ? m[1] : null;
}

/** How each language starts a module and drives the suite. @type {Record<string, {serve: string[], drive: string[]}>} */
const SHIMS = {
  js: { serve: ["node", ".kernel/serve.mjs"], drive: ["node", ".kernel/drive.mjs"] },
  py: { serve: ["python3", ".kernel/serve.py"], drive: ["python3", ".kernel/drive.py"] },
};

/**
 * Run the language-neutral conformance suite by spawning the module and talking
 * to it over a pipe.
 *
 * @param {{work: string, toolchain: Toolchain, manifest: import("./contract.js").Manifest, requireHermetic: boolean, conformancePath?: string, ambient?: string}} args
 */
async function runDriver({ work, toolchain, manifest, requireHermetic, conformancePath, ambient }) {
  const shim = SHIMS[toolchain.language];
  if (!shim) throw new Error(`no kernel shim for language ${JSON.stringify(toolchain.language)}`);
  const conf = conformancePath ?? manifest.conformance[0] ?? "conformance.json";
  const iface = manifest.interface[0] ?? "interface.json";
  const cmd = [
    ...shim.drive, conf, iface, manifest.name,
    "--",
    ...shim.serve, toolchain.entry, manifest.name,
  ];
  return runSuite({ work, toolchain, cmd, requireHermetic, ...(ambient ? { ambient } : {}) });
}

/**
 * Write the generated inputs the property gate runs over.
 *
 * Seeded from the conformance cases, because a schema alone cannot build a
 * structurally valid input for any contract whose parts refer to each other —
 * and agreement about garbage is not evidence.
 *
 * @param {string} moduleDir @param {string} work
 * @param {ReturnType<typeof contractId>} c @param {any} iface
 */
function writeCorpus(moduleDir, work, c, iface) {
  const rel = c.files.conformance[0]?.path;
  if (!rel) return;
  let suite;
  try { suite = JSON.parse(readFileSync(join(moduleDir, rel), "utf8")); } catch { return; }
  if (!Array.isArray(suite.properties) || suite.properties.length === 0) return;

  /** @type {Record<string, any[]>} */
  const seeds = {};
  for (const cs of suite.cases ?? []) {
    if (cs.expectError === undefined && cs.op) (seeds[cs.op] ??= []).push(cs.in);
  }
  /** @type {Record<string, any[]>} */
  const corpus = {};
  const size = Number(suite.propertyInputs ?? 150);
  for (const op of new Set(suite.properties.map((/** @type {any} */ p) => p.op))) {
    corpus[op] = corpusFor(seeds[op] ?? [], iface.operations?.[op] ?? {}, size, 7);
  }
  writeFileSync(join(work, "corpus.json"), JSON.stringify(corpus));
}

/** How many cases the suite declares, for a verdict that says something. */
function countProperties(/** @type {string} */ moduleDir, /** @type {ReturnType<typeof contractId>} */ c) {
  try {
    const rel = c.files.conformance[0]?.path;
    if (!rel) return 0;
    const suite = JSON.parse(readFileSync(join(moduleDir, rel), "utf8"));
    return Array.isArray(suite.properties) ? suite.properties.length : 0;
  } catch { return 0; }
}

function countCases(/** @type {string} */ moduleDir, /** @type {ReturnType<typeof contractId>} */ c) {
  try {
    const rel = c.files.conformance[0]?.path;
    if (!rel) return 0;
    const suite = JSON.parse(readFileSync(join(moduleDir, rel), "utf8"));
    return Array.isArray(suite.cases) ? suite.cases.length : 0;
  } catch {
    return 0;
  }
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
