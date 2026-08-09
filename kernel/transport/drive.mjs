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
import { createHash } from "node:crypto";

const [conformancePath, interfacePath, moduleName, ...rest] = process.argv.slice(2);
const dashdash = rest.indexOf("--");
const serveCmd = dashdash === -1 ? rest : rest.slice(dashdash + 1);

if (!conformancePath || !interfacePath || serveCmd.length === 0) {
  process.stderr.write("drive: usage: drive.mjs <conformance.json> <interface.json> <name> -- <serve command…>\n");
  process.exit(64);
}

const suite = JSON.parse(readFileSync(conformancePath, "utf8"));
// The corpus is written by the kernel, not generated here. Generation logic
// duplicated into two driver languages would be a second place for the two to
// disagree, and the drivers already carry as much duplicated judgement as is safe.
let corpus = {};
try { corpus = JSON.parse(readFileSync("corpus.json", "utf8")); } catch { /* no corpus: properties are skipped and said to be */ }
const iface = JSON.parse(readFileSync(interfacePath, "utf8"));
const cases = Array.isArray(suite.cases) ? suite.cases : [];
const CASE_TIMEOUT_MS = Number(suite.timeoutMs ?? 10000);

const child = spawn(/** @type {string} */ (serveCmd[0]), serveCmd.slice(1), { stdio: ["pipe", "pipe", "inherit"] });
const lines = createInterface({ input: /** @type {any} */ (child.stdout), crlfDelay: Infinity });

/** @type {((v: any) => void)[]} */
const waiting = [];
/** @type {string[]} */
const junk = [];
// Every response line, exactly as received. Hashed at the end so two runs of
// the same module under different ambient conditions can be compared byte for
// byte. Raw bytes rather than a canonical re-encoding: the comparison is a
// module against ITSELF, so there is nothing to normalise, and normalising
// would only create somewhere for a difference to hide.
const transcript = createHash("sha256");

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
  transcript.update(text + "\n");
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
  // A case may pin structure, or text, or both. An absent `expect` constrains
  // nothing — it must not be read as "expected undefined".
  const bad = (c.expect === undefined ? null : mismatch(c.expect, res.out, "")) ?? contains(c, res.out);
  if (bad) failures.push(`${label}: ${bad}`);
  else passed++;
}

// ── PROPERTIES: relations that must hold WITHOUT knowing the right answer.
//
// A case says "this input gives that output", which only tests what its author
// thought of. A property says "whatever the answer is, THIS must be true of it"
// — and it is checked over hundreds of generated inputs, so it cannot be
// satisfied by luck the way fourteen examples can. Writing one is reasoning
// work rather than enumeration work, which is why it is the part of a contract
// worth spending a strong model on.
// Ids continue past the cases so a late response can never be mistaken for a
// case's answer.
let nextId = 10000;
for (const prop of suite.properties ?? []) {
  const inputs = corpus[prop.op] ?? [];
  let held = 0, skipped = 0;
  /** @type {string|null} */
  let broke = null;

  for (const input of inputs) {
    const scope = { in: input };
    const base = await send({ id: nextId++, op: prop.op, in: input });
    // A property relates answers to each other, so an input the module refuses
    // has nothing to relate. Skipped, and the skip is counted — a property that
    // never actually ran is the quiet failure this whole design keeps meeting.
    if (base.error) { skipped++; continue; }
    scope.out = base.out;

    let usable = true;
    for (const call of prop.calls ?? []) {
      const res = await send({ id: nextId++, op: call.op ?? prop.op, in: fill(call.in, scope) });
      if (res.error) { usable = false; break; }
      scope[call.as] = res.out;
    }
    if (!usable) { skipped++; continue; }

    const bad = (prop.must ?? []).map((m) => check(m, scope)).find(Boolean);
    if (bad) { broke = `${bad}   with in=${JSON.stringify(input).slice(0, 160)}`; break; }
    held++;
  }

  if (broke) failures.push(`property "${prop.name}": ${broke}`);
  else if (held === 0) failures.push(`property "${prop.name}" never actually ran — all ${inputs.length} inputs were refused or unusable, so it proves nothing`);
  else passed++;
}

if (junk.length) failures.push(`the module wrote ${junk.length} non-JSON line(s) to stdout, which corrupts the wire. First: ${JSON.stringify(junk[0]?.slice(0, 120))}`);

child.stdin.end();
child.kill("SIGKILL");

for (const f of failures) console.log(`not ok - ${f}`);
console.log(`# ${passed} passed, ${failures.length} failed`);
console.log(`# transcript ${transcript.digest("hex")}`);
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
 * Build a derived input, replacing {"$": "path"} with a value already in scope.
 * @param {any} node @param {Record<string, any>} scope @returns {any}
 */
function fill(node, scope) {
  if (Array.isArray(node)) return node.map((n) => fill(n, scope));
  if (node === null || typeof node !== "object") return node;
  if (typeof node["$"] === "string") return at(scope, node["$"]);
  /** @type {Record<string, any>} */
  const out = {};
  for (const [k, v] of Object.entries(node)) out[k] = fill(v, scope);
  return out;
}

/**
 * One assertion. Data, not an expression language — six comparators cover what
 * a metamorphic relation needs, and a parser here would be a place for the two
 * drivers to drift apart.
 * @param {any} m @param {Record<string, any>} scope @returns {string|null}
 */
function check(m, scope) {
  const [kind] = Object.keys(m);
  const args = m[/** @type {string} */ (kind)];
  const A = at(scope, args[0]);
  // A number on the right is a literal; a string is a path. Unambiguous, because
  // a path is always a string and a bound is almost always a number — and it
  // keeps the comparator set free of a type annotation nobody would enjoy writing.
  const B = kind === "is" ? args[1] : typeof args[1] === "number" ? args[1] : at(scope, args[1]);
  const j = (/** @type {any} */ v) => JSON.stringify(v);
  switch (kind) {
    case "equal": case "is":
      return j(A) === j(B) ? null : `${args[0]} is ${j(A)}, expected ${kind === "is" ? j(B) : `${args[1]} which is ${j(B)}`}`;
    case "notEqual":
      return j(A) !== j(B) ? null : `${args[0]} and ${args[1]} are both ${j(A)}, and must differ`;
    case "atMost":
      return Number(A) <= Number(B) ? null : `${args[0]} is ${j(A)}, which is more than ${j(B)}`;
    case "atLeast":
      return Number(A) >= Number(B) ? null : `${args[0]} is ${j(A)}, which is less than ${j(B)}`;
    case "count":
      return (A?.length ?? -1) === Number(B) ? null : `${args[0]} has ${A?.length} item(s), expected ${j(B)}`;
    default:
      return `unknown comparator ${j(kind)}`;
  }
}

/** @param {any} obj @param {string} path */
function at(obj, path) {
  let node = obj;
  for (const step of String(path).split(".")) {
    if (node === null || typeof node !== "object") return undefined;
    node = Array.isArray(node) ? node[Number(step)] : node[step];
  }
  return node;
}

/**
 * `expectContains` / `expectNotContains` — substring assertions on named string
 * fields, for modules whose output is text.
 *
 * Exact matching is right for structured output and useless for a rendered
 * document: pinning a whole HTML page byte-for-byte makes every cosmetic edit a
 * contract change, and pinning nothing makes the contract vacuous. What is
 * actually worth stating about a page is that particular things appear in it and
 * particular things never do — which is also the only way to assert escaping,
 * since the interesting claim is the ABSENCE of a live tag.
 *
 * @param {any} c the case @param {any} out the module's result
 * @returns {string|null}
 */
function contains(c, out) {
  for (const [field, needles] of Object.entries(c.expectContains ?? {})) {
    const value = out?.[field];
    if (typeof value !== "string") return `expectContains names "${field}", which is ${value === undefined ? "missing" : "not a string"}`;
    for (const needle of /** @type {string[]} */ (needles)) {
      if (!value.includes(needle)) return `"${field}" does not contain ${JSON.stringify(needle)}`;
    }
  }
  for (const [field, needles] of Object.entries(c.expectNotContains ?? {})) {
    const value = out?.[field];
    if (typeof value !== "string") return `expectNotContains names "${field}", which is ${value === undefined ? "missing" : "not a string"}`;
    for (const needle of /** @type {string[]} */ (needles)) {
      if (value.includes(needle)) return `"${field}" contains ${JSON.stringify(needle)}, which it must not`;
    }
  }
  return null;
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
