// The drivers must agree.
//
// There is one conformance driver per language, and they are the only place in
// the system where the same judgement is implemented twice. That is a real risk
// and it deserves a test rather than a promise: if drive.py were a shade more
// forgiving than drive.mjs, a Python implementation would be admitted on weaker
// evidence than the identical JavaScript one, and nothing downstream would
// notice — both would simply report success, and a ledger hit is never
// re-verified.
//
// So every case below builds the SAME wrong module in both languages and
// requires both drivers to reject it. A driver that passes something its sibling
// rejects fails here.

import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, copyFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const TRANSPORT = join(dirname(fileURLToPath(import.meta.url)), "transport");
const HAVE_PYTHON = spawnSync("python3", ["--version"]).status === 0;

const INTERFACE = { name: "probe", operations: { go: { errors: ["EBAD"] } } };
const CONFORMANCE = {
  timeoutMs: 5000,
  cases: [
    { name: "adds", op: "go", in: { n: 2 }, expect: { doubled: 4 } },
    { name: "refuses a non-number", op: "go", in: { n: "x" }, expectError: "EBAD" },
  ],
};

/**
 * A module directory with both implementations side by side.
 * @param {{js: string, py: string}} impls
 */
function probe(impls) {
  const dir = mkdtempSync(join(tmpdir(), "transport-"));
  writeFileSync(join(dir, "interface.json"), JSON.stringify(INTERFACE));
  writeFileSync(join(dir, "conformance.json"), JSON.stringify(CONFORMANCE));
  // The kernel writes this marker into every sandbox tree that lacks one, so
  // that `export` means ESM regardless of which Node the image carries. The
  // probe mirrors that, since it assembles its own tree.
  writeFileSync(join(dir, "package.json"), JSON.stringify({ type: "module" }));
  writeFileSync(join(dir, "run.js"), impls.js);
  writeFileSync(join(dir, "run.py"), impls.py);
  const shims = join(dir, ".kernel");
  mkdirSync(shims);
  for (const f of readdirSync(TRANSPORT)) {
    if (!f.endsWith(".md")) copyFileSync(join(TRANSPORT, f), join(shims, f));
  }
  return dir;
}

/** @param {string} dir @param {"js"|"py"} lang @returns {{code: number, out: string}} */
function drive(dir, lang) {
  const cmd = lang === "js"
    ? ["node", [".kernel/drive.mjs", "conformance.json", "interface.json", "probe", "--", "node", ".kernel/serve.mjs", "run.js", "probe"]]
    : ["python3", [".kernel/drive.py", "conformance.json", "interface.json", "probe", "--", "python3", ".kernel/serve.py", "run.py", "probe"]];
  const r = spawnSync(/** @type {string} */ (cmd[0]), /** @type {string[]} */ (cmd[1]), { cwd: dir, encoding: "utf8" });
  return { code: r.status ?? -1, out: (r.stdout ?? "") + (r.stderr ?? "") };
}

/**
 * Run both drivers and require them to reach the same verdict.
 * @param {{js: string, py: string}} impls @param {boolean} shouldPass @param {string} why
 */
function bothAgree(impls, shouldPass, why) {
  const dir = probe(impls);
  try {
    const js = drive(dir, "js");
    const py = HAVE_PYTHON ? drive(dir, "py") : null;

    assert.equal(js.code === 0, shouldPass, `drive.mjs: ${why}\n${js.out}`);
    if (py) {
      assert.equal(py.code === 0, shouldPass, `drive.py: ${why}\n${py.out}`);
      assert.equal(js.code === 0, py.code === 0, `the two drivers disagreed, which means one language is judged more leniently than the other:\n  js: ${js.out}\n  py: ${py.out}`);
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

const CORRECT = {
  js: `export function go(input) {
    if (typeof input?.n !== "number") throw Object.assign(new Error("n must be a number"), { code: "EBAD" });
    return { doubled: input.n * 2 };
  }`,
  py: `class _Refusal(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

def go(payload):
    n = (payload or {}).get("n")
    if not isinstance(n, (int, float)) or isinstance(n, bool):
        raise _Refusal("EBAD", "n must be a number")
    return {"doubled": n * 2}
`,
};

test("a correct module passes both drivers", () => {
  bothAgree(CORRECT, true, "a correct implementation should be admitted");
});

test("a wrong answer is rejected by both", () => {
  bothAgree({
    js: CORRECT.js.replace("input.n * 2", "input.n * 3"),
    py: CORRECT.py.replace("n * 2", "n * 3"),
  }, false, "arithmetic that does not match the conformance case must fail");
});

test("a module that returns a result where an error was declared is rejected by both", () => {
  bothAgree({
    js: `export function go(input) { return { doubled: typeof input?.n === "number" ? input.n * 2 : 0 }; }`,
    py: `def go(payload):
    n = (payload or {}).get("n")
    return {"doubled": n * 2 if isinstance(n, (int, float)) and not isinstance(n, bool) else 0}
`,
  }, false, "swallowing a bad input instead of refusing it must fail");
});

test("an operation the interface never declared is rejected by both — the interface is the whole front door", () => {
  bothAgree({
    js: `${CORRECT.js}
    export function secretBackDoor() { return { oops: true }; }`,
    py: `${CORRECT.py}

def secret_back_door():
    return {"oops": True}
`,
  }, false, "an undeclared exported operation is a second door nobody wrote down");
});

test("junk on stdout is rejected by both — stdout is the wire", () => {
  bothAgree({
    js: `console.log("hello from a debug statement");
    ${CORRECT.js}`,
    py: `print("hello from a debug statement")
${CORRECT.py}`,
  }, false, "a stray print corrupts the stream and must not be shrugged off");
});

test("a module that hangs is killed by both rather than waited on forever", () => {
  bothAgree({
    js: `export function go() { while (true) {} }`,
    py: `def go(payload):
    while True:
        pass
`,
  }, false, "a hang is the worst failure available, because nothing can tell it apart from slow");
});

// ── determinism, enforced at the shim ────────────────────────────────────────
//
// The ambient-variation gate in verify.js catches nondeterminism
// probabilistically — a module leaking one bit of randomness escapes it about
// half the time. These tests cover the half that cannot be caught that way and
// must instead be made impossible: the clock does not tick and the generator is
// seeded before a single line of module code loads.

/** Ask a module the same question twice, a real second apart. @param {"js"|"py"} lang @param {string} src @param {string} [iface] */
function twice(lang, src, iface) {
  const dir = mkdtempSync(join(tmpdir(), "determinism-"));
  const shims = join(dir, ".kernel");
  mkdirSync(shims);
  for (const f of readdirSync(TRANSPORT)) if (!f.endsWith(".md")) copyFileSync(join(TRANSPORT, f), join(shims, f));
  writeFileSync(join(dir, "package.json"), JSON.stringify({ type: "module" }));
  writeFileSync(join(dir, "interface.json"), iface ?? JSON.stringify({ name: "p", operations: { go: {} } }));
  writeFileSync(join(dir, lang === "js" ? "run.js" : "run.py"), src);

  const ask = () => {
    const cmd = lang === "js"
      ? ["node", [".kernel/serve.mjs", "run.js", "p"]]
      : ["python3", [".kernel/serve.py", "run.py", "p"]];
    const r = spawnSync(/** @type {string} */ (cmd[0]), /** @type {string[]} */ (cmd[1]), {
      cwd: dir, encoding: "utf8", input: '{"id":1,"op":"go","in":{}}\n',
    });
    return (r.stdout ?? "").trim();
  };

  const first = ask();
  spawnSync("sleep", ["1.1"]);
  const second = ask();
  rmSync(dir, { recursive: true, force: true });
  return { first, second };
}

test("javascript: the clock does not tick and the generator is seeded", () => {
  const { first, second } = twice("js",
    "export function go(){ return { now: Date.now(), iso: new Date().toISOString(), rand: Math.random(), perf: performance.now() }; }");
  assert.equal(first, second, `a second apart, the answers differed:\n  ${first}\n  ${second}`);
  assert.match(first, /"now":1000000000000/, "the clock should be frozen, not merely stable");
});

test("javascript: a module that declares itself impure gets the real thing back", () => {
  // The escape hatch has to actually work, or "declare it in the interface"
  // is advice nobody can follow. And it is hashed there, so using it is visible.
  const { first, second } = twice("js",
    "export function go(){ return { now: Date.now() }; }",
    JSON.stringify({ name: "p", operations: { go: {} }, impure: ["clock"] }));
  assert.notEqual(first, second, "an impure module should see time pass");
});

if (HAVE_PYTHON) {
  test("python: time, random, uuid4 and os.urandom are all pinned", () => {
    const { first, second } = twice("py",
      "import time, random, os, uuid, datetime\n" +
      "def go(payload):\n" +
      "    return {'t': time.time(), 'r': random.random(), 'u': str(uuid.uuid4()),\n" +
      "            'd': datetime.datetime.now().isoformat(), 'b': os.urandom(4).hex()}\n");
    assert.equal(first, second, `a second apart, the answers differed:\n  ${first}\n  ${second}`);
    // uuid4 reads os.urandom directly, so seeding `random` alone would leave it
    // live — and a UUID in the output is the loudest nondeterminism there is.
    assert.match(first, /"u": "01000000-/, "uuid4 must be pinned too, not only random.random()");
  });
}

test("the driver reports a transcript digest, and it is stable for a stable module", () => {
  const dir = probe(CORRECT);
  try {
    const a = drive(dir, "js");
    const b = drive(dir, "js");
    const digest = (/** @type {string} */ out) => /^#\s*transcript\s+([0-9a-f]{64})$/m.exec(out)?.[1];
    assert.ok(digest(a.out), `no transcript line in:\n${a.out}`);
    assert.equal(digest(a.out), digest(b.out), "the same module answering the same questions must hash the same, or the determinism gate has nothing to compare");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("python is actually being exercised, not silently skipped", () => {
  // A skipped half is the failure mode this whole file exists to prevent, so it
  // is stated rather than left to be noticed.
  assert.equal(HAVE_PYTHON, true, "python3 is not on PATH, so drive.py was never run and the agreement above is only half checked");
});
