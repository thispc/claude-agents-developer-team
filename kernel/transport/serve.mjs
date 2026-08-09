// Kernel shim: turn a JavaScript module into a process that speaks the wire.
//
// This exists so `run.js` stays pure. The agent writes functions; it does not
// write plumbing, and it certainly does not write plumbing afresh in every
// module where a subtle difference could become a behaviour difference. See
// PROTOCOL.md for the format.
//
// Trusted kernel code. Never delegated.

import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";
import { readFileSync } from "node:fs";

const entry = process.argv[2] ?? "run.js";
const name = process.argv[3] ?? "module";

// ── determinism, before a single line of module code loads ──────────────────
//
// The ambient-variation gate that runs the suite twice can only catch
// nondeterminism that actually reaches the output on the run it happens to
// observe. A module leaking one bit of randomness escapes it about half the
// time — measured at 5 catches in 8 runs. Perturbing the environment harder
// does not fix that, because the problem is probabilistic by nature.
//
// Interposing here does fix it, structurally: a module cannot read a clock that
// does not tick or a generator that is seeded. The Internet Computer reaches the
// same conclusion from the consensus direction — `ic0.time()` is constant within
// a single entry-point invocation, so time is an INPUT to the computation rather
// than something read out of the air.
//
// Frozen rather than forbidden, deliberately. Throwing would break anything that
// merely timestamps a log line, and it would make verification behave
// differently from composition; a constant keeps both honest. A module that
// genuinely needs the real thing declares it in interface.json, where it is
// hashed into the contract and therefore visible:
//
//     { "impure": ["clock", "random"] }
const impure = new Set(readImpure());
if (!impure.has("clock")) freezeClock();
if (!impure.has("random")) seedRandom();

function readImpure() {
  try {
    return JSON.parse(readFileSync("interface.json", "utf8")).impure ?? [];
  } catch {
    return [];   // no interface here (a bare probe); default to the strict world
  }
}

function freezeClock() {
  const FROZEN = 1000000000000;   // 2001-09-09T01:46:40Z — arbitrary, and that is the point
  Date.now = () => FROZEN;
  const RealDate = Date;
  // Only the no-argument form is ambient. `new Date(x)` is a pure function of x
  // and must keep working, or date arithmetic breaks for no reason.
  globalThis.Date = /** @type {any} */ (new Proxy(RealDate, {
    construct: (target, args) => (args.length === 0 ? new target(FROZEN) : new target(...args)),
  }));
  globalThis.performance = /** @type {any} */ ({ now: () => 0 });
}

function seedRandom() {
  // A small deterministic generator (mulberry32). The sequence is identical on
  // every run, so a module that samples it still produces the same output twice.
  let seed = 0x9e3779b9;
  Math.random = () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

let impl;
try {
  impl = await import(pathToFileURL(resolve(entry)).href);
} catch (err) {
  // A module that will not even load is not a protocol error, it is a dead
  // module — so this is one of the few places exiting is right.
  process.stderr.write(`serve: cannot load ${entry}: ${err instanceof Error ? err.stack : String(err)}\n`);
  process.exit(70);
}

const operations = Object.keys(impl).filter((k) => typeof impl[k] === "function").sort();

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });

for await (const line of rl) {
  const text = line.trim();
  if (text === "") continue;

  let req;
  try {
    req = JSON.parse(text);
  } catch {
    write({ id: null, error: { code: "EPROTOCOL", message: "request was not valid JSON" } });
    continue;
  }

  const id = req.id ?? null;

  if (req.op === "__describe") {
    write({ id, out: { module: name, operations } });
    continue;
  }

  const fn = typeof req.op === "string" ? impl[req.op] : undefined;
  if (typeof fn !== "function") {
    write({ id, error: { code: "ENOOP", message: `no operation ${JSON.stringify(req.op)}; this module exposes ${operations.join(", ") || "nothing"}` } });
    continue;
  }

  try {
    const out = await fn(req.in);
    write({ id, out });
  } catch (err) {
    // A declared error is a RESPONSE, not a crash. The process stays alive,
    // because "this input was rejected" and "this module is broken" are
    // different facts and a caller has to be able to tell them apart.
    write({
      id,
      error: {
        code: /** @type {any} */ (err)?.code ?? "EUNCAUGHT",
        message: err instanceof Error ? err.message : String(err),
      },
    });
  }
}

/** @param {unknown} obj */
function write(obj) {
  // stdout carries responses and nothing else. Anything a module wants to say to
  // a human belongs on stderr; a stray line here corrupts the stream.
  process.stdout.write(JSON.stringify(obj) + "\n");
}
