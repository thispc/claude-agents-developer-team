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

const entry = process.argv[2] ?? "run.js";
const name = process.argv[3] ?? "module";

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
