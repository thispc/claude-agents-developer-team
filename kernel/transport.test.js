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

test("python is actually being exercised, not silently skipped", () => {
  // A skipped half is the failure mode this whole file exists to prevent, so it
  // is stated rather than left to be noticed.
  assert.equal(HAVE_PYTHON, true, "python3 is not on PATH, so drive.py was never run and the agreement above is only half checked");
});
