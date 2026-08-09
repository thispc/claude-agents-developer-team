// Talking to a module the way the verifier does: spawn it, speak the wire.
//
// This is what makes an app polyglot rather than merely allowing polyglot
// modules. Importing a module works only when the composer happens to be written
// in the same language as the module — so an app that imports has a hidden
// requirement that every module be JavaScript, and the whole contract/artifact
// separation buys nothing. An app that spawns has no opinion at all.
//
// It is also the safer half of a real gap. A composer that imports agent-written
// code runs it in its own process, next to whatever else that process holds; a
// composer that spawns gets an OS process boundary, a scrubbed environment, and
// a timeout for free. The same shim that judged the module in the sandbox is the
// one serving it here, so it behaves at composition time exactly as it did when
// it was admitted.

import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

/**
 * @typedef {object} ModuleHandle
 * @property {(op: string, input?: unknown) => Promise<any>} call
 * @property {() => Promise<string[]>} operations
 * @property {() => void} close
 * @property {string} language
 * @property {string} artifact
 */

/**
 * @param {object} args
 * @param {import("./compose.js").Runnable} args.runnable  from materialiseRunnable()
 * @param {number} [args.timeoutMs] per call
 * @returns {ModuleHandle}
 */
export function openModule({ runnable, timeoutMs = 30000 }) {
  const [cmd, ...rest] = runnable.serve;
  if (!cmd) throw new Error(`${runnable.name}: no serve command`);

  const child = spawn(cmd, rest, {
    cwd: runnable.dir,
    // Nothing inherited. A module is a function of its input, and the operator's
    // environment is not its input — v1 leaked CLAUDE_CODE_* into workers this
    // way, and the same rule that fixed that applies here.
    env: {
      PATH: process.env["PATH"] ?? "/usr/bin:/bin:/usr/local/bin",
      HOME: "/tmp",
      LANG: "C",
      LC_ALL: "C",
      TZ: "UTC",
      NO_COLOR: "1",
    },
    stdio: ["pipe", "pipe", "inherit"],
  });

  /** @type {Map<number, {resolve: (v: any) => void, reject: (e: Error) => void, timer: NodeJS.Timeout}>} */
  const waiting = new Map();
  let nextId = 1;
  /** @type {Error|null} */
  let dead = null;

  const lines = createInterface({ input: /** @type {any} */ (child.stdout), crlfDelay: Infinity });
  lines.on("line", (line) => {
    const text = line.trim();
    if (text === "") return;
    let msg;
    try {
      msg = JSON.parse(text);
    } catch {
      // stdout is the wire. A module that prints there has corrupted the stream,
      // and guessing which lines are real is how a transport eventually
      // mis-parses a genuine response.
      fail(new Error(`${runnable.name} wrote a non-JSON line to stdout, which corrupts the wire: ${JSON.stringify(text.slice(0, 120))}`));
      return;
    }
    const pending = waiting.get(msg.id);
    if (!pending) return;
    waiting.delete(msg.id);
    clearTimeout(pending.timer);
    if (msg.error) {
      pending.reject(Object.assign(new Error(msg.error.message ?? "refused"), { code: msg.error.code ?? "EUNKNOWN", module: runnable.name }));
    } else {
      pending.resolve(msg.out);
    }
  });

  child.on("error", (err) => fail(err));
  child.on("close", (code) => {
    if (waiting.size > 0) fail(new Error(`${runnable.name} exited with code ${code} while ${waiting.size} call(s) were outstanding`));
  });

  /** @param {Error} err */
  function fail(err) {
    dead = err;
    for (const [, p] of waiting) {
      clearTimeout(p.timer);
      p.reject(err);
    }
    waiting.clear();
  }

  /** @param {string} op @param {unknown} input */
  function call(op, input = undefined) {
    if (dead) return Promise.reject(dead);
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        waiting.delete(id);
        reject(new Error(`${runnable.name}.${op} did not answer within ${timeoutMs}ms`));
      }, timeoutMs);
      waiting.set(id, { resolve, reject, timer });
      child.stdin.write(JSON.stringify({ id, op, in: input }) + "\n");
    });
  }

  return {
    call,
    operations: async () => (await call("__describe")).operations,
    close: () => {
      try { child.stdin.end(); } catch { /* already gone */ }
      try { child.kill("SIGKILL"); } catch { /* already gone */ }
    },
    language: runnable.language,
    artifact: runnable.artifact,
  };
}
