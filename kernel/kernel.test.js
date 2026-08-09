// Tests for the trusted base.
//
// The kernel is the one component nothing else checks. Every module in the
// system is judged by verify(); verify() is judged here and nowhere else, which
// is why these tests are about the properties the design rests on rather than
// about coverage. Each one, if it broke, would break something silently.

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, mkdirSync, rmSync, readFileSync, appendFileSync, chmodSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { contractId, artifactDigest, loadManifest, readToolchain, canonicalise, globToRegExp, walk } from "./contract.js";
import { parseToml, TomlError } from "./toml.js";
import { Ledger, now } from "./ledger.js";
import { Store } from "./store.js";
import { sizeGate, parsimony } from "./sizegate.js";
import { loadWiring } from "./wiring.js";
import { resolveLive } from "./compose.js";
import { compatible } from "./compat.js";

// Images are digest-pinned everywhere, including in fixtures — a test that used
// a moving tag would be exercising a path the kernel now refuses.
const DIGEST = "node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293";
const PY_DIGEST = "python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df";

/** A minimal well-formed module on disk. @param {Partial<Record<string,string>>} [over] */
function fixture(over = {}) {
  const dir = mkdtempSync(join(tmpdir(), "kernel-test-"));
  mkdirSync(join(dir, "tests"));
  const files = {
    "module.toml": [
      `[module]`, `name = "fixture"`, ``,
      `[contract]`,
      `interface = ["interface.json"]`,
      `tests = ["tests/**/*.js"]`, ``,
      `[prose]`, `files = ["behavior.md"]`, ``,
      `[impl]`, `files = ["run.js"]`, `toolchain = "toolchain.json"`,
    ].join("\n"),
    "interface.json": JSON.stringify({ name: "fixture", operations: { go: { errors: ["EBAD"] } } }),
    "toolchain.json": JSON.stringify({ image: DIGEST, language: "js", entry: "run.js", test: ["node", "--test", "tests/"] }),
    "behavior.md": "# fixture\n\nProse. Not hashed.\n",
    "run.js": "export const go = () => 1;\n",
    "tests/go.test.js": "import assert from 'node:assert';\nassert.ok(true);\n",
    ...over,
  };
  for (const [rel, body] of Object.entries(files)) {
    if (body === undefined) continue;
    writeFileSync(join(dir, rel), body);
  }
  return dir;
}

// ── what identity means ──────────────────────────────────────────────────────

test("rewording the prose does not move the contract id", () => {
  const dir = fixture();
  const before = contractId(dir).id;
  writeFileSync(join(dir, "behavior.md"), "# fixture\n\nCompletely different words.\nA total rewrite.\n");
  assert.equal(contractId(dir).id, before, "prose is a search heuristic, not identity — rewording must be free");
  rmSync(dir, { recursive: true, force: true });
});

test("changing one assertion in a test does move it", () => {
  const dir = fixture();
  const before = contractId(dir).id;
  writeFileSync(join(dir, "tests/go.test.js"), "import assert from 'node:assert';\nassert.ok(1 === 1);\n");
  assert.notEqual(contractId(dir).id, before);
  rmSync(dir, { recursive: true, force: true });
});

test("the toolchain belongs to the ARTIFACT — which is what lets another language fill the same slot", () => {
  // While the toolchain sat in the contract, a Python implementation of the same
  // interface hashed to a DIFFERENT contract id, so it was not an alternative
  // way to fill the slot — it was a different slot, and two implementations
  // passing the identical suite had no way to say so.
  //
  // Nothing is lost by the move: the toolchain still changes the ARTIFACT
  // digest, so swapping the image still forces a fresh verification and "it
  // passed" remains a claim about a pinned runtime.
  const dir = fixture();
  const contractBefore = contractId(dir).id;
  const artifactBefore = artifactDigest(dir).digest;

  writeFileSync(join(dir, "toolchain.json"), JSON.stringify({ image: DIGEST.replace("20-alpine", "22-alpine"), language: "js", entry: "run.js", test: ["node", "--test", "tests/"] }));
  assert.equal(contractId(dir).id, contractBefore, "the runtime describes an implementation, not what every implementation must satisfy");
  assert.notEqual(artifactDigest(dir).digest, artifactBefore, "but it is still judged: a different runtime is a different claim");
  rmSync(dir, { recursive: true, force: true });
});

test("a module judged only by language-neutral cases is portable; one carrying language-specific tests is not", () => {
  const dir = fixture();
  assert.equal(contractId(dir).portable, false, "the fixture has tests/**/*.js, which can only judge a JavaScript implementation");

  // Move the same module to conformance-only and it becomes fillable by anything.
  rmSync(join(dir, "tests"), { recursive: true, force: true });
  writeFileSync(join(dir, "conformance.json"), JSON.stringify({ cases: [{ name: "one", op: "go", in: {}, expect: {} }] }));
  writeFileSync(join(dir, "module.toml"), readFileSync(join(dir, "module.toml"), "utf8").replace(`tests = ["tests/**/*.js"]`, `conformance = ["conformance.json"]`));
  assert.equal(contractId(dir).portable, true);
  rmSync(dir, { recursive: true, force: true });
});

test("a language with no kernel shim is refused by name, not accepted and mishandled", () => {
  const dir = fixture();
  writeFileSync(join(dir, "toolchain.json"), JSON.stringify({ image: `ghcr.io/x@sha256:${"0".repeat(64)}`, language: "brainfuck", entry: "run.js", test: ["x"] }));
  assert.throws(() => artifactDigest(dir), /has no kernel shim/);
  rmSync(dir, { recursive: true, force: true });
});

test("a test cannot be quietly dropped from the contract", () => {
  // This is the false-hit catastrophe, attempted two ways. If it succeeded, the
  // hashed set would shrink while the contract id stood still, and the ledger
  // would go on serving an artifact that was never judged against the dropped
  // test — with nothing downstream to notice, since the whole point of a hit is
  // that it is not re-verified.
  const dir = fixture();
  const before = contractId(dir).id;

  writeFileSync(join(dir, "tests/second.test.js"), "// another suite\n");
  const withTwo = contractId(dir).id;
  assert.notEqual(withTwo, before, "adding a test file changes what must be satisfied");

  // Attempt one: narrow the glob so the second suite stops being hashed. The
  // file is still sitting there, so it now belongs to no declared set and the
  // module is refused outright — a stronger answer than merely changing the id.
  const narrowed = readFileSync(join(dir, "module.toml"), "utf8").replace(`tests = ["tests/**/*.js"]`, `tests = ["tests/go.test.js"]`);
  writeFileSync(join(dir, "module.toml"), narrowed);
  assert.throws(() => contractId(dir), /belong to no declared set/);

  // Attempt two: narrow the glob AND delete the file, which is at least honest.
  // That is a real change to the contract, and the id says so.
  rmSync(join(dir, "tests/second.test.js"));
  assert.notEqual(contractId(dir).id, withTwo);
  assert.notEqual(contractId(dir).id, before, "module.toml is itself hashed, so the narrowed glob is visible even though the surviving files are identical");

  rmSync(dir, { recursive: true, force: true });
});

test("adding a held-out test moves the contract id, even though the agent never sees it", () => {
  // The hole this closes: the vault is part of what an artifact must prove, but
  // it lives outside the module directory. Leave it out of the hash and adding a
  // held-out test is a cache HIT — so every artifact already admitted stays
  // admitted having never faced the new test, and a hit is never re-verified.
  // Hashing it costs the secret nothing: a digest reveals no test.
  const dir = fixture();
  const vault = mkdtempSync(join(tmpdir(), "vault-"));

  const withoutVault = contractId(dir).id;
  const withEmptyVault = contractId(dir, vault).id;
  assert.equal(withEmptyVault, withoutVault, "an empty vault is the same as no vault");

  writeFileSync(join(vault, "adversarial.test.js"), "// something the implementer never sees\n");
  const withVault = contractId(dir, vault).id;
  assert.notEqual(withVault, withoutVault, "adding the first held-out test must be a cache miss");

  writeFileSync(join(vault, "adversarial.test.js"), "// a different check entirely\n");
  assert.notEqual(contractId(dir, vault).id, withVault, "changing a held-out test must be a cache miss too");

  for (const d of [dir, vault]) rmSync(d, { recursive: true, force: true });
});

test("the implementation is hashed separately from the contract it satisfies", () => {
  const dir = fixture();
  const c = contractId(dir).id;
  const a = artifactDigest(dir).digest;
  writeFileSync(join(dir, "run.js"), "export const go = () => { return 1; };\n");
  assert.equal(contractId(dir).id, c, "rewriting the implementation does not change what it must satisfy");
  assert.notEqual(artifactDigest(dir).digest, a);
  rmSync(dir, { recursive: true, force: true });
});

// ── the ways the hash could quietly stop describing the module ────────────────

test("a glob that matches nothing is refused, not ignored", () => {
  // A typo in a tests glob is the false-hit failure wearing a clean face: the
  // hashed set silently shrinks, and the ledger goes on serving an artifact that
  // was never judged against the tests the pattern no longer reaches.
  const dir = fixture();
  const toml = readFileSync(join(dir, "module.toml"), "utf8").replace(`tests = ["tests/**/*.js"]`, `tests = ["tests/**/*.js", "spec/**/*.js"]`);
  writeFileSync(join(dir, "module.toml"), toml);
  assert.throws(() => contractId(dir), /matches no file/);
  rmSync(dir, { recursive: true, force: true });
});

test("a file no set claims is refused", () => {
  const dir = fixture();
  writeFileSync(join(dir, "helper.js"), "// nobody declared me\n");
  assert.throws(() => contractId(dir), /belong to no declared set/);
  rmSync(dir, { recursive: true, force: true });
});

test("a file claimed by two sets is refused", () => {
  const base = fixture();
  const toml = readFileSync(join(base, "module.toml"), "utf8").replace(`files = ["behavior.md"]`, `files = ["behavior.md", "interface.json"]`);
  writeFileSync(join(base, "module.toml"), toml);
  assert.throws(() => contractId(base), /claimed by two different sets/);
  rmSync(base, { recursive: true, force: true });
});

test("a module with neither conformance cases nor tests cannot be declared at all", () => {
  const dir = fixture();
  rmSync(join(dir, "tests"), { recursive: true, force: true });
  const toml = readFileSync(join(dir, "module.toml"), "utf8").replace(`tests = ["tests/**/*.js"]`, `tests = []`);
  writeFileSync(join(dir, "module.toml"), toml);
  assert.throws(() => contractId(dir), /conformance, \[contract\] tests, or both/);
  rmSync(dir, { recursive: true, force: true });
});

test("the executable bit is part of a file's identity", () => {
  const dir = fixture();
  const before = contractId(dir).id;
  chmodSync(join(dir, "tests/go.test.js"), 0o755);
  assert.notEqual(contractId(dir).id, before, "a test that stops being runnable is a changed test");
  rmSync(dir, { recursive: true, force: true });
});

// ── canonical encoding ───────────────────────────────────────────────────────

test("canonical encoding does not depend on key order", () => {
  assert.equal(canonicalise({ b: 1, a: 2 }), canonicalise({ a: 2, b: 1 }));
});

test("canonical encoding refuses floats, which do not round-trip identically everywhere", () => {
  assert.throws(() => canonicalise({ x: 1.5 }), /non-integer/);
});

test("globs: ** crosses directories and * does not", () => {
  assert.ok(globToRegExp("src/**/*.js").test("src/a/b/c.js"));
  assert.ok(globToRegExp("src/**/*.js").test("src/c.js"), "a/**/b must also match a/b");
  assert.ok(!globToRegExp("src/*.js").test("src/a/b.js"));
  assert.ok(globToRegExp("*.json").test("interface.json"));
  assert.ok(!globToRegExp("*.json").test("nested/interface.json"));
});

// ── the TOML subset ──────────────────────────────────────────────────────────

test("TOML: tables, arrays of tables, and multi-line arrays", () => {
  const t = parseToml([
    `[a]`, `x = 1`, `list = [`, `  "one",  # a comment inside`, `  "two",`, `]`, ``,
    `[[n]]`, `name = "first"`, `[[n]]`, `name = "second"`,
  ].join("\n"), "t");
  assert.equal(t["a"].x, 1);
  assert.deepEqual(t["a"].list, ["one", "two"]);
  assert.equal(t["n"].length, 2);
  assert.equal(t["n"][1].name, "second");
});

test("TOML: syntax the subset does not implement fails loudly and by name", () => {
  assert.throws(() => parseToml(`a = { b = 1 }`, "t"), /inline tables/);
  assert.throws(() => parseToml(`a = 2026-08-09`, "t"), /datetimes/);
  assert.throws(() => parseToml(`a = 0xff`, "t"), /hex/);
  assert.throws(() => parseToml(`[a]\n[a]`, "t"), /declared twice/);
  assert.throws(() => parseToml(`a = 1\na = 2`, "t"), /set twice/);
  assert.throws(() => parseToml(`a = "unterminated`, "t"), TomlError);
});

// ── the ledger ───────────────────────────────────────────────────────────────

test("the ledger is a relation: one contract, many artifacts", () => {
  const dir = mkdtempSync(join(tmpdir(), "ledger-"));
  const l = new Ledger(join(dir, "ledger.jsonl"));
  l.append({ t: "admit", contract: "c-1", artifact: "a-1", module: "m", at: now(), proved: ["tests"] });
  l.append({ t: "admit", contract: "c-1", artifact: "a-2", module: "m", at: now(), proved: ["tests"] });
  assert.equal(l.admitted("c-1").length, 2, "many artifacts may satisfy one contract — that is N-version programming for free");
  rmSync(dir, { recursive: true, force: true });
});

test("a human's pin outranks anything admitted after it", () => {
  const dir = mkdtempSync(join(tmpdir(), "ledger-"));
  const l = new Ledger(join(dir, "ledger.jsonl"));
  l.append({ t: "admit", contract: "c-1", artifact: "a-1", module: "m", at: now(), proved: [] });
  l.append({ t: "pin", contract: "c-1", artifact: "a-1", module: "m", at: now(), by: "owner" });
  l.append({ t: "admit", contract: "c-1", artifact: "a-2", module: "m", at: now(), proved: [] });
  const live = l.live("c-1");
  assert.equal(live?.artifact, "a-1");
  assert.equal(live?.via, "pin", "auto-admit must not be able to overrule a human who said 'no, this one'");
  rmSync(dir, { recursive: true, force: true });
});

test("a rejection changes nothing about what is live", () => {
  const dir = mkdtempSync(join(tmpdir(), "ledger-"));
  const l = new Ledger(join(dir, "ledger.jsonl"));
  l.append({ t: "admit", contract: "c-1", artifact: "a-good", module: "m", at: now(), proved: [] });
  l.append({ t: "reject", contract: "c-1", artifact: "a-bad", module: "m", at: now(), gate: "tests", why: "exit 1" });
  assert.equal(l.live("c-1")?.artifact, "a-good", "a failed improvement is a non-event, not a rollback");
  rmSync(dir, { recursive: true, force: true });
});

test("a torn last line is dropped; a damaged line in the middle is refused", () => {
  const dir = mkdtempSync(join(tmpdir(), "ledger-"));
  const path = join(dir, "ledger.jsonl");
  const l = new Ledger(path);
  l.append({ t: "admit", contract: "c-1", artifact: "a-1", module: "m", at: now(), proved: [] });
  appendFileSync(path, '{"t":"admit","contract":"c-1"');   // a crash mid-append
  assert.equal(l.all().length, 1, "only the final line can be torn, and it is dropped");

  appendFileSync(path, "\n" + JSON.stringify({ t: "admit", contract: "c-2", artifact: "a-2", module: "m", at: now(), proved: [] }) + "\n");
  assert.throws(() => l.all(), /not valid JSON/, "a damaged line with good lines after it means the file was edited, and reading past it would make the ledger lie");
  rmSync(dir, { recursive: true, force: true });
});

// ── the store ────────────────────────────────────────────────────────────────

test("putting the same artifact twice is a no-op, and materialising round-trips", () => {
  const mod = fixture();
  const dir = mkdtempSync(join(tmpdir(), "store-"));
  const s = new Store(dir);
  const a = artifactDigest(mod);

  assert.equal(s.put(mod, a.digest, a.files).fresh, true);
  assert.equal(s.put(mod, a.digest, a.files).fresh, false, "identical bytes are the same artifact — nothing downstream should be invalidated");

  const out = mkdtempSync(join(tmpdir(), "out-"));
  s.materialise(a.digest, out);
  assert.equal(readFileSync(join(out, "run.js"), "utf8"), readFileSync(join(mod, "run.js"), "utf8"));

  for (const d of [mod, dir, out]) rmSync(d, { recursive: true, force: true });
});

// ── the gates ────────────────────────────────────────────────────────────────

test("the size cap refuses; the mitosis mark only proposes", () => {
  const iface = { operations: { a: {} } };
  const limits = { maxLoc: 2000, mitosisLoc: 1500, mitosisOps: 7 };

  const small = sizeGate({ loc: 900, iface, limits });
  assert.equal(small.ok, true);
  assert.equal(small.proposeSplit, false);

  const large = sizeGate({ loc: 1700, iface, limits });
  assert.equal(large.ok, true, "past the mitosis mark the module still works and still serves traffic");
  assert.equal(large.proposeSplit, true);

  const huge = sizeGate({ loc: 2400, iface, limits });
  assert.equal(huge.ok, false, "past the cap a passing suite has stopped being evidence");
});

test("too many exported operations proposes a split even when the module is small", () => {
  const iface = { operations: Object.fromEntries(Array.from({ length: 9 }, (_, i) => [`op${i}`, {}])) };
  const v = sizeGate({ loc: 300, iface, limits: { maxLoc: 2000, mitosisLoc: 1500, mitosisOps: 7 } });
  assert.equal(v.proposeSplit, true, "surface is the other axis: 300 lines doing nine separable jobs is still nine jobs");
});

test("a split that does not shrink the contract surface is refused", () => {
  assert.equal(parsimony(10, [6, 6]).ok, false, "two children exposing as much as the parent copied the work rather than dividing it");
  assert.equal(parsimony(10, [4, 4]).ok, true);
});

// ── the wiring ───────────────────────────────────────────────────────────────

test("the wiring may not name a module that does not exist", () => {
  const root = mkdtempSync(join(tmpdir(), "wiring-"));
  const file = join(root, "wiring.toml");
  writeFileSync(file, `[[node]]\nname = "ghost"\nmodule = "modules/ghost"\n`);
  assert.throws(() => loadWiring(file, root), /no module.toml/, "a graph that can assert a node the code does not have is worse than no graph");
  rmSync(root, { recursive: true, force: true });
});

test("an edge may not reference an undeclared node, and nothing may wire to itself", () => {
  const root = mkdtempSync(join(tmpdir(), "wiring-"));
  mkdirSync(join(root, "modules", "a"), { recursive: true });
  writeFileSync(join(root, "modules", "a", "module.toml"), "");
  const file = join(root, "wiring.toml");

  writeFileSync(file, `[[node]]\nname = "a"\nmodule = "modules/a"\n\n[[edge]]\nfrom = "a"\nto = "b"\n`);
  assert.throws(() => loadWiring(file, root), /not a declared node/);

  writeFileSync(file, `[[node]]\nname = "a"\nmodule = "modules/a"\n\n[[edge]]\nfrom = "a"\nto = "a"\n`);
  assert.throws(() => loadWiring(file, root), /cannot be wired to itself/);
  rmSync(root, { recursive: true, force: true });
});

test("composing reads the toolchain from the ARTIFACT, not from the working copy", () => {
  // The bug this pins, found by swapping a real app's module to Python and then
  // pinning the JavaScript one back: `resolveLive` materialises the ADMITTED
  // artifact but was reading language and entry from the module directory. Pin an
  // older artifact — or leave the working copy in another language — and the two
  // disagree, so the composer tries to start a file the artifact does not
  // contain. The failure is loud; the cause is not.
  const dir = mkdtempSync(join(tmpdir(), "compose-"));
  mkdirSync(join(dir, "impl"));
  writeFileSync(join(dir, "module.toml"), [
    `[module]`, `name = "swappable"`, ``,
    `[contract]`, `interface = ["interface.json"]`, `conformance = ["conformance.json"]`, ``,
    `[impl]`, `files = ["impl/**"]`, `toolchain = "impl/toolchain.json"`,
  ].join("\n"));
  writeFileSync(join(dir, "interface.json"), JSON.stringify({ name: "swappable", operations: { go: {} } }));
  writeFileSync(join(dir, "conformance.json"), JSON.stringify({ cases: [] }));

  // An admitted artifact in JavaScript.
  writeFileSync(join(dir, "impl/run.js"), "export function go(){ return {}; }\n");
  writeFileSync(join(dir, "impl/toolchain.json"), JSON.stringify({ image: DIGEST, language: "js", entry: "impl/run.js" }));

  const storeDir = mkdtempSync(join(tmpdir(), "compose-store-"));
  const store = new Store(storeDir);
  const ledger = new Ledger(join(storeDir, "ledger.jsonl"));
  const c = contractId(dir).id;
  const a = artifactDigest(dir);
  store.put(dir, a.digest, a.files);
  ledger.append({ t: "admit", contract: c, artifact: a.digest, module: "swappable", at: now(), proved: [], language: "js" });

  // Now the working copy moves to Python, exactly as a language swap does.
  rmSync(join(dir, "impl/run.js"));
  writeFileSync(join(dir, "impl/run.py"), "def go(payload):\n    return {}\n");
  writeFileSync(join(dir, "impl/toolchain.json"), JSON.stringify({ image: PY_DIGEST, language: "py", entry: "impl/run.py" }));

  const live = resolveLive({ moduleDir: dir, store, ledger, runsRoot: join(storeDir, "runs") });
  assert.equal(live.language, "js", "the admitted artifact is the JavaScript one, so composing it must start JavaScript");
  assert.equal(live.entry, "impl/run.js");
  assert.ok(existsSync(live.path), "the entry the composer names has to exist in the tree it materialised");

  for (const d of [dir, storeDir]) rmSync(d, { recursive: true, force: true });
});

test("walk is deterministic and skips editor litter", () => {
  const dir = fixture();
  writeFileSync(join(dir, ".DS_Store"), "junk");
  const files = walk(dir);
  assert.deepEqual(files, [...files].sort(), "order must not depend on the filesystem");
  assert.ok(!files.includes(".DS_Store"));
  rmSync(dir, { recursive: true, force: true });
});


// ── the composition gate ─────────────────────────────────────────────────────
//
// The check that nothing did until now: does what one module emits fit what the
// next one accepts? It runs on schemas, so it costs milliseconds — and it only
// ever fails when a mismatch can be PROVEN, because a gate that cries wolf on
// under-specified interfaces teaches people to widen their schemas until it goes
// quiet, which is the opposite of the point.

/** @param {any} p @param {any} c */
const errs = (p, c) => compatible(p, c).filter((f) => f.level === "error");

test("a required field the producer never emits is refused", () => {
  const produces = { type: "object", required: ["a"], properties: { a: { type: "number" } } };
  const accepts = { type: "object", required: ["a", "b"], properties: { a: { type: "number" }, b: { type: "number" } } };
  const [first] = errs(produces, accepts);
  assert.ok(first);
  assert.match(first.says, /required here, and the producer never emits it/);
});

test("a field the producer emits only SOMETIMES cannot satisfy a requirement", () => {
  const produces = { type: "object", required: [], properties: { a: { type: "number" } } };
  const accepts = { type: "object", required: ["a"], properties: { a: { type: "number" } } };
  const [first] = errs(produces, accepts);
  assert.ok(first);
  assert.match(first.says, /only emits it sometimes/);
});

test("disjoint types are refused", () => {
  assert.equal(errs({ type: "string" }, { type: "number" }).length, 1);
  assert.equal(errs({ type: "integer" }, { type: "number" }).length, 0, "an integer is a number");
});

test("an enum value the consumer does not accept is refused — this is the rain/rainy case", () => {
  const produces = { type: "string", enum: ["clear", "rainy"] };
  const accepts = { type: "string", enum: ["clear", "rain"] };
  const [first] = errs(produces, accepts);
  assert.ok(first);
  assert.match(first.says, /"rainy"/);
});

test("extra fields the consumer does not want are fine — JSON ignores them", () => {
  const produces = { type: "object", required: ["a", "extra"], properties: { a: { type: "number" }, extra: { type: "string" } } };
  const accepts = { type: "object", required: ["a"], properties: { a: { type: "number" } } };
  assert.deepEqual(errs(produces, accepts), []);
});

test("the check reaches inside arrays, which is where the weather app's break was", () => {
  const produces = { type: "object", required: ["days"], properties: { days: { type: "array", items: { type: "object", required: ["maxC"], properties: { maxC: { type: "number" } } } } } };
  const accepts = { type: "object", required: ["days"], properties: { days: { type: "array", items: { type: "object", required: ["highC"], properties: { highC: { type: "number" } } } } } };
  const [first] = errs(produces, accepts);
  assert.ok(first);
  assert.match(first.at, /days\[\]\.highC/, "the message has to point at the field, not at the top of the value");
});

test("a schema that says nothing is reported as UNCHECKED, never as broken", () => {
  const found = compatible({ type: "object" }, { type: "object", properties: { a: { enum: [1, 2] } } });
  assert.equal(found.filter((f) => f.level === "error").length, 0, "nothing here can be proven wrong");
});

test("an image pinned by tag is refused AT ADMISSION, but an already-admitted one still runs", () => {
  // This sat unnoticed while the design claimed the opposite. toolchain.json is
  // hashed into the ARTIFACT, so a tag lets two genuinely different runtimes
  // share one artifact digest, and the ledger would then serve — unverified —
  // an artifact that passed under a runtime nobody can reproduce. It is the same
  // defect that disqualified Extism from this design.
  const dir = fixture();
  writeFileSync(join(dir, "toolchain.json"), JSON.stringify({ image: "node:20-alpine", language: "js", entry: "run.js", test: ["node", "--test", "tests/"] }));
  // artifactDigest does not enforce it — only admission does.
  assert.equal(artifactDigest(dir).image, "node:20-alpine");
  assert.throws(() => readToolchain(dir, loadManifest(dir), { requireDigest: true }), /pinned by tag, which moves/);

  writeFileSync(join(dir, "toolchain.json"), JSON.stringify({
    image: "node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293",
    language: "js", entry: "run.js", test: ["node", "--test", "tests/"],
  }));
  assert.equal(artifactDigest(dir).language, "js");
  rmSync(dir, { recursive: true, force: true });
});
