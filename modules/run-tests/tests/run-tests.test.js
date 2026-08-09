// The contract, written as assertions. These are hashed into contract_id, so
// changing one of them re-derives this module — and only this module.
//
// The implementation cannot edit them: the sandbox mounts the whole working tree
// read-only, and the agent that writes run.js is never handed this directory.

import { test } from "node:test";
import assert from "node:assert/strict";
import { tmpdir } from "node:os";
import { run } from "../run.js";

const HERE = tmpdir();

test("exit 0 is a pass, and that is the entire admission signal", async () => {
  const r = await run({ dir: HERE, command: ["node", "-e", "process.exit(0)"] });
  assert.equal(r.ok, true);
  assert.equal(r.code, 0);
  assert.equal(r.timedOut, false);
  assert.ok(r.ms >= 0);
});

test("a non-zero exit is a failure even when the output looks cheerful", async () => {
  const r = await run({ dir: HERE, command: ["node", "-e", "console.log('ok 1 - everything is fine'); process.exit(1)"] });
  assert.equal(r.ok, false);
  assert.equal(r.code, 1);
});

test("TAP failures come back as structure, not as a wall of text", async () => {
  const script = [
    "console.log('TAP version 13');",
    "console.log('not ok 1 - adds two numbers');",
    "console.log('  ---');",
    "console.log(\"  location: 'tests/math.test.js:12'\");",
    "console.log(\"  error: 'Expected values to be strictly equal:\\\\n\\\\n3 !== 4'\");",
    "console.log('  ...');",
    "console.log('ok 2 - subtracts');",
    "process.exit(1);",
  ].join("");
  const r = await run({ dir: HERE, command: ["node", "-e", script] });

  assert.equal(r.ok, false);
  assert.equal(r.total, 2, "both assertions should be counted, passing and failing");
  assert.equal(r.failures.length, 1);
  const [f] = r.failures;
  assert.ok(f);
  assert.equal(f.test, "adds two numbers");
  assert.match(f.message, /3 !== 4/);
  assert.equal(f.at, "tests/math.test.js:12");
});

test("output that is not TAP is reported, not treated as a failure to parse", async () => {
  const r = await run({ dir: HERE, command: ["node", "-e", "console.log('BUILD SUCCESSFUL in 2s')"] });
  assert.equal(r.ok, true);
  assert.equal(r.total, 0);
  assert.deepEqual(r.failures, []);
});

test("a hang is killed at the deadline rather than held forever", async () => {
  const r = await run({ dir: HERE, command: ["node", "-e", "setInterval(() => {}, 1000)"], timeoutSec: 1 });
  assert.equal(r.timedOut, true);
  assert.equal(r.ok, false);
  assert.equal(r.code, 124);
  assert.match(r.summary, /timed out/);
});

test("a command that spawned children takes them with it when the deadline kills it", async () => {
  const script = "const {spawn}=require('child_process'); spawn('node',['-e','setInterval(()=>{},1000)'],{stdio:'ignore'}); setInterval(()=>{},1000);";
  const r = await run({ dir: HERE, command: ["node", "-e", script], timeoutSec: 1 });
  assert.equal(r.timedOut, true);
});

test("an empty command is refused before anything is spawned", async () => {
  await assert.rejects(() => run({ dir: HERE, command: [] }), (/** @type {any} */ e) => e.code === "ESPAWN");
});

test("a command that does not exist says so, and says it is a spawn problem", async () => {
  await assert.rejects(
    () => run({ dir: HERE, command: ["definitely-not-a-real-binary-9f3c"] }),
    (/** @type {any} */ e) => e.code === "ESPAWN" && /cannot start/.test(e.message)
  );
});

test("the summary is one line a human can read in a ledger", async () => {
  const r = await run({ dir: HERE, command: ["node", "-e", "console.log('not ok 1 - the important one'); process.exit(1)"] });
  assert.match(r.summary, /1 failed, first "the important one"/);
  assert.ok(!r.summary.includes("\n"), "a summary with a newline breaks every line-oriented view that shows it");
});

// ── Below this line: tests that exist because the HELD-OUT suite caught
// something the visible suite could not. Each one is now permanent.
//
// This is the loop the design runs on. A held-out failure is not just a
// rejection — it is a specification the visible suite was missing, so it gets
// written down here where the next implementation must satisfy it from the
// start. The suite co-evolves with what has actually gone wrong, rather than
// staying whatever its first author happened to imagine.

test("the command does not inherit this process's environment", async () => {
  // The failure this was written for: node's test runner sets NODE_TEST_CONTEXT
  // for every file it runs. Inherit it, and a `node --test` spawned from here
  // decides it is a child of some other run — it switches from TAP to a binary
  // protocol and EXITS 0 WITH FAILING TESTS. A runner that reports success on a
  // failing suite is the worst failure this module has.
  process.env["A_SECRET_FROM_THE_PARENT"] = "leaked";
  try {
    const leaked = await run({ dir: HERE, command: ["node", "-e", "process.exit(process.env.A_SECRET_FROM_THE_PARENT ? 3 : 0)"] });
    assert.equal(leaked.code, 0, "a variable set in this process reached the child");

    // This one is not hypothetical: these tests run under `node --test`, so
    // NODE_TEST_CONTEXT is genuinely set in this process while the assertion runs.
    const ctx = await run({ dir: HERE, command: ["node", "-e", "process.exit(process.env.NODE_TEST_CONTEXT ? 3 : 0)"] });
    assert.equal(ctx.code, 0, "NODE_TEST_CONTEXT reached the child, which is what makes a failing suite exit 0");
  } finally {
    delete process.env["A_SECRET_FROM_THE_PARENT"];
  }
});

test("variables the caller asks for are passed, and only those", async () => {
  const r = await run({
    dir: HERE,
    command: ["node", "-e", "process.exit(process.env.WANTED === 'yes' ? 0 : 3)"],
    env: { WANTED: "yes" },
  });
  assert.equal(r.code, 0);
});

test("a multi-line error in a YAML block scalar is read, not skipped", async () => {
  // Real `node --test` writes `error: |-` followed by an indented block. Reading
  // only the inline part gives a failure whose message is the literal "|-".
  const script = [
    "console.log('TAP version 13');",
    "console.log('not ok 1 - adds two numbers');",
    "console.log('  ---');",
    "console.log('  error: |-');",
    "console.log('    Expected values to be strictly equal:');",
    "console.log('');",
    "console.log('    2 !== 3');",
    "console.log('  code: ERR_ASSERTION');",
    "console.log('  ...');",
    "process.exit(1);",
  ].join("");
  const r = await run({ dir: HERE, command: ["node", "-e", script] });

  assert.equal(r.failures.length, 1);
  const [f] = r.failures;
  assert.ok(f);
  assert.match(f.message, /2 !== 3/, `got ${JSON.stringify(f.message)}`);
  assert.ok(!f.message.includes("|-"), "the block marker is not part of the message");
  assert.ok(!f.message.includes("ERR_ASSERTION"), "the block ends where the indentation does, so the next field is not swallowed");
});

test("enormous output cannot exhaust memory", async () => {
  const r = await run({ dir: HERE, command: ["node", "-e", "for (let i=0;i<20000;i++) console.log('x'.repeat(200))"] });
  assert.equal(r.ok, true);
  assert.ok(r.summary.length < 500);
});
