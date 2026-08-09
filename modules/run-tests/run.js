// The only file an agent ever writes in this module.
//
// It may be rewritten entirely, in any style, by anything — as long as the shape
// in interface.json holds and the suite in tests/ passes. Correct here has no
// other meaning.

import { spawn } from "node:child_process";

/**
 * @param {{dir: string, command: string[], timeoutSec?: number, env?: Record<string, string>}} input
 * @returns {Promise<{ok: boolean, code: number, timedOut: boolean, ms: number, total: number, failures: {test: string, message: string, at?: string}[], summary: string}>}
 */
export async function run(input) {
  const timeoutMs = (input.timeoutSec ?? 120) * 1000;
  const started = Date.now();
  const [cmd, ...rest] = input.command;
  if (!cmd) throw Object.assign(new Error("command is empty"), { code: "ESPAWN" });

  const proc = await spawnCapped(cmd, rest, input.dir, timeoutMs, cleanEnv(input.env));
  const ms = Date.now() - started;
  const parsed = parseTap(proc.output);
  const ok = proc.code === 0 && !proc.timedOut;

  return {
    ok,
    code: proc.code,
    timedOut: proc.timedOut,
    ms,
    total: parsed.total,
    failures: parsed.failures,
    summary: summarise({ ok, timedOut: proc.timedOut, code: proc.code, parsed, ms, output: proc.output }),
  };
}

/**
 * The environment the command runs in. Built from nothing, never inherited.
 *
 * This is not tidiness, it is the difference between working and silently
 * lying. Node's own test runner sets NODE_TEST_CONTEXT in the environment of
 * every test file it runs. Inherit that, and a `node --test` this module spawns
 * decides it is a child of some other test run: it switches from TAP to a binary
 * V8-serialised protocol, and — this is the part that matters — it EXITS 0 WITH
 * FAILING TESTS, because reporting the failure is now somebody else's job.
 *
 * A test runner that reports "everything passed" when tests failed is worse than
 * one that crashes. And the visible suite could not have caught it: those tests
 * feed hand-written TAP to the parser and never run a real runner nested inside
 * another one, which is the only situation where the variable exists to leak.
 *
 * The same shape as the credential leak that bit v1, where workers inherited the
 * operator's CLAUDE_CODE_* variables. Both are one rule: a child process gets
 * what it was given, not what happened to be lying around.
 *
 * @param {Record<string, string>} [extra] explicitly requested additions
 */
function cleanEnv(extra) {
  return {
    PATH: process.env["PATH"] ?? "/usr/bin:/bin:/usr/local/bin",
    HOME: process.env["HOME"] ?? "/tmp",
    TMPDIR: process.env["TMPDIR"] ?? "/tmp",
    LANG: process.env["LANG"] ?? "C.UTF-8",
    NO_COLOR: "1",
    ...(extra ?? {}),
  };
}

/**
 * @param {string} cmd @param {string[]} args @param {string} cwd @param {number} timeoutMs
 * @param {Record<string, string>} env
 * @returns {Promise<{code: number, output: string, timedOut: boolean}>}
 */
function spawnCapped(cmd, args, cwd, timeoutMs, env) {
  return new Promise((resolve, reject) => {
    let output = "";
    let timedOut = false;
    let child;
    try {
      child = spawn(cmd, args, { cwd, env, detached: true, stdio: ["ignore", "pipe", "pipe"] });
    } catch (err) {
      reject(Object.assign(new Error(`cannot start ${cmd}: ${err instanceof Error ? err.message : String(err)}`), { code: "ESPAWN" }));
      return;
    }
    const CAP = 512 * 1024;
    const take = (/** @type {Buffer} */ b) => {
      if (output.length < CAP) output += b.toString("utf8");
    };
    child.stdout?.on("data", take);
    child.stderr?.on("data", take);

    const timer = setTimeout(() => {
      timedOut = true;
      // Kill the group, not the child. A runner that spawned workers of its own
      // would otherwise leave them holding the sandbox open after the deadline.
      try { process.kill(-(/** @type {number} */ (child.pid)), "SIGKILL"); } catch { /* already gone */ }
    }, timeoutMs);

    child.on("error", (err) => {
      clearTimeout(timer);
      reject(Object.assign(new Error(`cannot start ${cmd}: ${err.message}`), { code: "ESPAWN" }));
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: timedOut ? 124 : code ?? -1, output, timedOut });
    });
  });
}

/**
 * Pull structured failures out of TAP.
 *
 * The kernel does not need this — for admission the exit code is the whole
 * signal, deliberately, because anything that parses output is somewhere a
 * judgement call could creep into the trusted base. This is for the agent that
 * has to FIX the failure, which cannot act on "exit 1".
 *
 * Unparseable output is not an error. Plenty of runners are not TAP, and a
 * module that threw here would turn "your tests are in a different format" into
 * "your tests failed".
 *
 * @param {string} output
 * @returns {{total: number, failures: {test: string, message: string, at?: string}[]}}
 */
function parseTap(output) {
  const lines = output.split("\n");
  /** @type {{test: string, message: string, at?: string}[]} */
  const failures = [];
  let total = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? "";
    const point = /^\s*(not ok|ok)\s+\d+\s*-?\s*(.*)$/.exec(line);
    if (!point) continue;
    total++;
    if (point[1] !== "not ok") continue;

    const name = (point[2] ?? "").trim() || "(unnamed test)";
    // Skip the subtest roll-ups the runner emits for each containing describe();
    // they repeat a child's failure without adding anything to it.
    if (/# Subtest:/.test(name)) continue;

    let message = "";
    let at = "";
    for (let j = i + 1; j < lines.length && j < i + 60; j++) {
      const y = lines[j] ?? "";
      if (/^\s*(not ok|ok)\s+\d+/.test(y)) break;

      const err = /^(\s*)error:\s*(.*)$/.exec(y);
      if (err && !message) {
        const inline = (err[2] ?? "").trim();
        // A real runner puts anything multi-line in a YAML block scalar: the
        // line reads `error: |-` and the message is the indented block below it.
        // Reading only the inline part gives a failure whose message is "|-".
        if (inline === "|-" || inline === "|" || inline === ">-" || inline === ">") {
          message = unquote(gatherBlock(lines, j + 1, (err[1] ?? "").length));
        } else {
          message = unquote(inline);
        }
      }

      const loc = /^\s*location:\s*(.*)$/.exec(y);
      if (loc && !at) at = unquote(loc[1] ?? "");
    }
    failures.push({ test: name, message: message || "no error text in the report", ...(at ? { at } : {}) });
  }
  return { total, failures };
}

/**
 * The indented body of a YAML block scalar, flattened to one line.
 * @param {string[]} lines @param {number} from @param {number} indent
 */
function gatherBlock(lines, from, indent) {
  /** @type {string[]} */
  const block = [];
  for (let k = from; k < lines.length; k++) {
    const line = lines[k] ?? "";
    // A blank line inside the block is part of it; a blank line before anything
    // has been collected is just spacing.
    if (line.trim() === "") { if (block.length) continue; else continue; }
    if (line.length - line.trimStart().length <= indent) break;
    block.push(line.trim());
  }
  return block.join(" ");
}

/** TAP YAML quotes with either mark and escapes the newlines. @param {string} s */
function unquote(s) {
  let t = s.trim();
  if ((t.startsWith("'") && t.endsWith("'")) || (t.startsWith('"') && t.endsWith('"'))) t = t.slice(1, -1);
  return t.replace(/\\n/g, " ").replace(/\s+/g, " ").trim();
}

/** @param {{ok: boolean, timedOut: boolean, code: number, parsed: {total: number, failures: {test: string}[]}, ms: number, output: string}} a */
function summarise({ ok, timedOut, code, parsed, ms, output }) {
  if (timedOut) return `timed out after ${ms}ms — killed`;
  if (ok) return parsed.total > 0 ? `${parsed.total} assertions passed in ${ms}ms` : `exit 0 in ${ms}ms (no TAP output to count)`;
  if (parsed.failures.length > 0) {
    const first = parsed.failures[0]?.test ?? "";
    const more = parsed.failures.length - 1;
    return `${parsed.failures.length} failed, first "${first}"${more > 0 ? ` and ${more} more` : ""}`;
  }
  const lastLine = output.split("\n").map((l) => l.trim()).filter(Boolean).pop() ?? "";
  return `exit ${code}${lastLine ? ` — ${lastLine.slice(0, 120)}` : " with no output"}`;
}
