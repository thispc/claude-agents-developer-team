// Kernel shim: run the language-neutral conformance suite against a module by
// talking to it over the wire.
//
//   node drive.mjs <conformance.json> <interface.json> <module-name> -- <serve command…>
//
// This is the gate that makes a module replaceable. It never imports the module,
// only spawns it and speaks JSON, so it has no opinion about what language wrote
// the answers — which is the entire point. Exit 0 means every case passed.
//
// Its Python twin, drive.py, must behave IDENTICALLY. A driver that is subtly
// more forgiving than its sibling would admit a module in one language on weaker
// evidence than the same module in another, and nothing downstream would notice
// because both would simply report success. kernel.test.js runs both against the
// same wrong module and requires both to reject it.
//
// Trusted kernel code. Never delegated.

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";

const [conformancePath, interfacePath, moduleName, ...rest] = process.argv.slice(2);
const dashdash = rest.indexOf("--");
const serveCmd = dashdash === -1 ? rest : rest.slice(dashdash + 1);

if (!conformancePath || !interfacePath || serveCmd.length === 0) {
  process.stderr.write("drive: usage: drive.mjs <conformance.json> <interface.json> <name> -- <serve command…>\n");
  process.exit(64);
}

const suite = JSON.parse(readFileSync(conformancePath, "utf8"));
const iface = JSON.parse(readFileSync(interfacePath, "utf8"));
const cases = Array.isArray(suite.cases) ? suite.cases : [];
const CASE_TIMEOUT_MS = Number(suite.timeoutMs ?? 10000);

const child = spawn(/** @type {string} */ (serveCmd[0]), serveCmd.slice(1), { stdio: ["pipe", "pipe", "inherit"] });
const lines = createInterface({ input: /** @type {any} */ (child.stdout), crlfDelay: Infinity });

/** @type {((v: any) => void)[]} */
const waiting = [];
/** @type {string[]} */
const junk = [];

lines.on("line", (line) => {
  const text = line.trim();
  if (text === "") return;
  let msg;
  try {
    msg = JSON.parse(text);
  } catch {
    // stdout is the wire. A line that is not JSON means the module printed
    // something there, and a transport that shrugs that off is one that
    // eventually mis-parses a real response.
    junk.push(text);
    return;
  }
  const next = waiting.shift();
  if (next) next(msg);
});

/** @type {string[]} */
const failures = [];
let passed = 0;

// Every operation the interface declares must actually be exposed. A module that
// answers some of its interface is not a smaller module, it is a broken one.
const declared = Object.keys(iface.operations ?? {}).sort();
const described = await send({ id: 0, op: "__describe" });
if (described?.error) {
  failures.push(`__describe failed: ${described.error.code} ${described.error.message}`);
} else {
  const exposed = [...(described?.out?.operations ?? [])].sort();
  const missing = declared.filter((o) => !exposed.includes(o));
  // Extra operations are a failure too, not a bonus. The interface is the front
  // door and the ONLY way anything reaches this module; a helper that leaks out
  // of it is a second door nobody declared, and the next module along will start
  // depending on it.
  const extra = exposed.filter((/** @type {string} */ o) => !declared.includes(o));
  if (missing.length) failures.push(`interface declares ${missing.join(", ")}, which the module does not expose`);
  else if (extra.length) failures.push(`the module exposes ${extra.join(", ")}, which the interface does not declare — the interface is the whole front door, so an undeclared operation is a second one`);
  else passed++;
}

for (const [i, c] of cases.entries()) {
  const label = c.name ?? `case ${i + 1}`;
  let res;
  try {
    res = await send({ id: i + 1, op: c.op, in: c.in });
  } catch (err) {
    failures.push(`${label}: ${err instanceof Error ? err.message : String(err)}`);
    continue;
  }

  if (c.expectError !== undefined) {
    if (!res.error) failures.push(`${label}: expected error ${c.expectError}, got a result: ${JSON.stringify(res.out)}`);
    else if (res.error.code !== c.expectError) failures.push(`${label}: expected error ${c.expectError}, got ${res.error.code} (${res.error.message})`);
    else passed++;
    continue;
  }

  if (res.error) {
    failures.push(`${label}: expected a result, got error ${res.error.code}: ${res.error.message}`);
    continue;
  }
  const bad = mismatch(c.expect, res.out, "");
  if (bad) failures.push(`${label}: ${bad}`);
  else passed++;
}

if (junk.length) failures.push(`the module wrote ${junk.length} non-JSON line(s) to stdout, which corrupts the wire. First: ${JSON.stringify(junk[0]?.slice(0, 120))}`);

child.stdin.end();
child.kill("SIGKILL");

for (const f of failures) console.log(`not ok - ${f}`);
console.log(`# ${passed} passed, ${failures.length} failed`);
process.exit(failures.length === 0 ? 0 : 1);

/** @param {unknown} req @returns {Promise<any>} */
function send(req) {
  return new Promise((res, rej) => {
    const timer = setTimeout(() => {
      const at = waiting.indexOf(settle);
      if (at !== -1) waiting.splice(at, 1);
      rej(new Error(`no response within ${CASE_TIMEOUT_MS}ms — the module hung`));
    }, CASE_TIMEOUT_MS);
    /** @param {any} v */
    function settle(v) {
      clearTimeout(timer);
      res(v);
    }
    waiting.push(settle);
    child.stdin.write(JSON.stringify(req) + "\n");
  });
}

/**
 * `expect` is a SUBSET match: every key it names must be present and equal.
 * Anything the case does not mention, it does not constrain — so a module may
 * return extra fields, but never a wrong one.
 *
 * These rules must match drive.py exactly.
 * @param {any} want @param {any} got @param {string} path
 * @returns {string|null} a description of the first mismatch, or null
 */
function mismatch(want, got, path) {
  const where = path || "the result";
  if (want === null || typeof want !== "object") {
    return Object.is(want, got) ? null : `${where}: expected ${JSON.stringify(want)}, got ${JSON.stringify(got)}`;
  }
  if (Array.isArray(want)) {
    if (!Array.isArray(got)) return `${where}: expected an array, got ${JSON.stringify(got)}`;
    if (want.length !== got.length) return `${where}: expected ${want.length} item(s), got ${got.length}`;
    for (let i = 0; i < want.length; i++) {
      const bad = mismatch(want[i], got[i], `${where}[${i}]`);
      if (bad) return bad;
    }
    return null;
  }
  if (got === null || typeof got !== "object" || Array.isArray(got)) return `${where}: expected an object, got ${JSON.stringify(got)}`;
  for (const k of Object.keys(want)) {
    if (!(k in got)) return `${where}: missing "${k}"`;
    const bad = mismatch(want[k], got[k], path ? `${path}.${k}` : k);
    if (bad) return bad;
  }
  return null;
}
