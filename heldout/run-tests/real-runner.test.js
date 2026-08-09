// HELD OUT. The agent that writes run.js is never handed this directory, and
// the module's own tests/ folder does not contain it.
//
// This is not a convention, it is the only unbiased signal left once an agent
// has read the tests it is being judged by. Every frontier agent saturates a
// visible suite; the differences between them only appear against tests they
// could not optimise against. The measured gap between the two widens by roughly
// 28 points for every tenfold increase in module size — which is also why the
// size cap and this vault are the same argument made twice.
//
// What it caught, the first time it ran: the visible suite tests TAP parsing
// against hand-written TAP that puts the error on one line in quotes. Real
// `node --test` almost never does that — it emits a YAML block scalar,
// `error: |-` followed by an indented block. So the implementation passed every
// test it could see while being unable to parse the output of the actual runner
// it exists to run. Nothing in the visible suite could have found that, because
// the visible suite was written from the same misunderstanding as the code.
//
// The rule this encodes: to check a thing that runs a test runner, RUN A TEST
// RUNNER. Do not hand it a fixture of what you believe the runner prints.

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { run } from "../run.js";

/** @param {string} body */
function scratch(body) {
  const dir = mkdtempSync(join(tmpdir(), "heldout-"));
  writeFileSync(join(dir, "x.test.js"), body);
  return dir;
}

test("a real node --test failure comes back as structure", async () => {
  const dir = scratch(`
    const { test } = require("node:test");
    const assert = require("node:assert/strict");
    test("adds two numbers", () => { assert.strictEqual(1 + 1, 3); });
  `);
  const r = await run({ dir, command: ["node", "--test", "x.test.js"] });

  assert.equal(r.ok, false, "a failing suite is not ok");
  assert.ok(r.failures.length >= 1, `a real failing run must yield at least one structured failure, got ${JSON.stringify(r.failures)}`);

  const f = r.failures.find((x) => x.test.includes("adds two numbers"));
  assert.ok(f, `expected a failure named "adds two numbers", got ${JSON.stringify(r.failures.map((x) => x.test))}`);
  assert.match(
    /** @type {{message: string}} */ (f).message,
    /2 !== 3|Expected values to be strictly equal/,
    `the failure must carry the runner's actual assertion text. Got: ${JSON.stringify(/** @type {{message: string}} */ (f).message)}`
  );
});

test("a real node --test pass is not reported as a failure", async () => {
  const dir = scratch(`
    const { test } = require("node:test");
    const assert = require("node:assert/strict");
    test("adds two numbers", () => { assert.strictEqual(1 + 1, 2); });
  `);
  const r = await run({ dir, command: ["node", "--test", "x.test.js"] });
  assert.equal(r.ok, true);
  assert.deepEqual(r.failures, []);
  assert.ok(r.total >= 1, "a passing run should still count its assertions");
});

test("a suite that throws before any test runs is still reported usefully", async () => {
  const dir = scratch(`throw new Error("the module under test does not load");`);
  const r = await run({ dir, command: ["node", "--test", "x.test.js"] });
  assert.equal(r.ok, false);
  assert.ok(
    r.summary.length > 0 && !/^exit \d+$/.test(r.summary),
    `"exit 1" on its own wastes the reader's trip. Got: ${JSON.stringify(r.summary)}`
  );
});
