// Tests for the trusted base.
//
// The kernel is the one component nothing else checks. Every module in the
// system is judged by verify(); verify() is judged here and nowhere else, which
// is why these tests are about the properties the design rests on rather than
// about coverage. Each one, if it broke, would break something silently.

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, mkdirSync, rmSync, readFileSync, appendFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { contractId, artifactDigest, canonicalise, globToRegExp, walk } from "./contract.js";
import { parseToml, TomlError } from "./toml.js";
import { Ledger, now } from "./ledger.js";
import { Store } from "./store.js";
import { sizeGate, parsimony } from "./sizegate.js";
import { loadWiring } from "./wiring.js";

/** A minimal well-formed module on disk. @param {Partial<Record<string,string>>} [over] */
function fixture(over = {}) {
  const dir = mkdtempSync(join(tmpdir(), "kernel-test-"));
  mkdirSync(join(dir, "tests"));
  const files = {
    "module.toml": [
      `[module]`, `name = "fixture"`, ``,
      `[contract]`,
      `interface = ["interface.json"]`,
      `tests = ["tests/**/*.js"]`,
      `toolchain = ["toolchain.json"]`, ``,
      `[prose]`, `files = ["behavior.md"]`, ``,
      `[impl]`, `files = ["run.js"]`,
    ].join("\n"),
    "interface.json": JSON.stringify({ name: "fixture", operations: { go: { errors: ["EBAD"] } } }),
    "toolchain.json": JSON.stringify({ image: "node:20-alpine", test: ["node", "--test", "tests/"] }),
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

test("changing the toolchain moves it — 'the tests passed' is a fact about a runtime", () => {
  const dir = fixture();
  const before = contractId(dir).id;
  writeFileSync(join(dir, "toolchain.json"), JSON.stringify({ image: "node:22-alpine", test: ["node", "--test", "tests/"] }));
  assert.notEqual(contractId(dir).id, before);
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

test("a module with no tests cannot be declared at all", () => {
  const dir = fixture();
  const toml = readFileSync(join(dir, "module.toml"), "utf8").replace(`tests = ["tests/**/*.js"]`, `tests = []`);
  writeFileSync(join(dir, "module.toml"), toml);
  assert.throws(() => contractId(dir), /tests cannot be empty/);
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

test("walk is deterministic and skips editor litter", () => {
  const dir = fixture();
  writeFileSync(join(dir, ".DS_Store"), "junk");
  const files = walk(dir);
  assert.deepEqual(files, [...files].sort(), "order must not depend on the filesystem");
  assert.ok(!files.includes(".DS_Store"));
  rmSync(dir, { recursive: true, force: true });
});

