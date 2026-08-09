// HELD OUT. The implementer never sees this directory.
//
// The visible suite feeds this module a tidy wiring file that the same author
// wrote in the same sitting as the parser. That is the shape of test an agent
// saturates without learning anything: it encodes what the implementation
// already assumes. These are the inputs a real file produces — trailing
// comments, CRLF, names that contain each other — plus the repo's own
// wiring.toml, which is the only input that is not a guess about reality.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { render } from "../run.js";

test("the repo's own wiring.toml yields line numbers that are actually right", () => {
  // The claim this module makes is "open this file at this line and you will see
  // it". Nothing but the real file can check that claim.
  const path = "/work/wiring.toml";
  if (!existsSync(path)) return;   // not mounted in this run; the other cases still apply

  const text = readFileSync(path, "utf8");
  const lines = text.split("\n");
  const names = [...text.matchAll(/^\s*name\s*=\s*"([^"]+)"/gm)].map((m) => m[1] ?? "");
  const nodes = names.map((n) => ({ name: n, module: `modules/${n}` }));

  const g = render({ wiringPath: "wiring.toml", wiringText: text, nodes, edges: [] });
  assert.equal(g.dropped.length, 0, `every node in the real file should be found: ${JSON.stringify(g.dropped)}`);

  for (const n of g.nodes) {
    const line = /** @type {number} */ (n.evidence[0]?.line);
    assert.equal(lines[line - 1]?.trim(), "[[node]]", `node ${n.id} claims line ${line}, which reads ${JSON.stringify(lines[line - 1])}`);
  }
});

test("a trailing comment on the block header does not hide the block", () => {
  const text = [`[[node]]  # the first one`, `name = "alpha"`, `module = "modules/alpha"`].join("\n");
  const g = render({ wiringPath: "w.toml", wiringText: text, nodes: [{ name: "alpha", module: "modules/alpha" }], edges: [] });
  assert.equal(g.nodes.length, 1, `a comment after the header should not make the node invisible: ${JSON.stringify(g.dropped)}`);
  assert.equal(g.nodes[0]?.evidence[0]?.line, 1);
});

test("CRLF line endings do not shift or lose anything", () => {
  const text = [`[[node]]`, `name = "alpha"`, `module = "modules/alpha"`].join("\r\n");
  const g = render({ wiringPath: "w.toml", wiringText: text, nodes: [{ name: "alpha", module: "modules/alpha" }], edges: [] });
  assert.equal(g.nodes.length, 1, "a file saved on Windows is still a file");
  assert.equal(g.nodes[0]?.evidence[0]?.line, 1);
});

test("a node whose name contains another node's name is not confused with it", () => {
  const text = [
    `[[node]]`, `name = "run"`, `module = "modules/run"`, ``,
    `[[node]]`, `name = "run-tests"`, `module = "modules/run-tests"`,
  ].join("\n");
  const g = render({
    wiringPath: "w.toml", wiringText: text,
    nodes: [{ name: "run", module: "modules/run" }, { name: "run-tests", module: "modules/run-tests" }],
    edges: [],
  });
  assert.equal(g.nodes.length, 2);
  assert.equal(g.nodes.find((n) => n.id === "run")?.evidence[0]?.line, 1);
  assert.equal(g.nodes.find((n) => n.id === "run-tests")?.evidence[0]?.line, 5);
});

test("two edges that differ only in where the space falls are told apart", () => {
  // The edge index is keyed by joining two names. If the join uses a character
  // that can appear in a name, "a b"→"c" and "a"→"b c" collide, and one of them
  // silently inherits the other's line number.
  const text = [
    `[[edge]]`, `from = "a b"`, `to = "c"`, ``,
    `[[edge]]`, `from = "a"`, `to = "b c"`,
  ].join("\n");
  const nodes = [
    { name: "a b", module: "m/ab" }, { name: "c", module: "m/c" },
    { name: "a", module: "m/a" }, { name: "b c", module: "m/bc" },
  ];
  const nodeText = nodes.map((n) => `[[node]]\nname = "${n.name}"\nmodule = "${n.module}"\n`).join("\n");
  const g = render({
    wiringPath: "w.toml",
    wiringText: nodeText + "\n" + text,
    nodes,
    edges: [{ from: "a b", to: "c" }, { from: "a", to: "b c" }],
  });
  assert.equal(g.edges.length, 2, `both edges should be drawn: ${JSON.stringify(g.dropped)}`);
  const first = g.edges.find((e) => e.from === "a b");
  const second = g.edges.find((e) => e.from === "a");
  assert.notEqual(first?.evidence[0]?.line, second?.evidence[0]?.line, "two distinct edges must not report the same line");
});

test("a wiring file where every node is declared after the edges still works", () => {
  const text = [
    `[[edge]]`, `from = "alpha"`, `to = "beta"`, ``,
    `[[node]]`, `name = "alpha"`, `module = "m/a"`, ``,
    `[[node]]`, `name = "beta"`, `module = "m/b"`,
  ].join("\n");
  const g = render({
    wiringPath: "w.toml", wiringText: text,
    nodes: [{ name: "alpha", module: "m/a" }, { name: "beta", module: "m/b" }],
    edges: [{ from: "alpha", to: "beta" }],
  });
  assert.equal(g.nodes.length, 2);
  assert.equal(g.edges.length, 1, "declaration order in the file is not a dependency order");
});

test("the last block in a file with no trailing newline is not lost", () => {
  const text = `[[node]]\nname = "alpha"\nmodule = "m/a"`;   // no final \n
  const g = render({ wiringPath: "w.toml", wiringText: text, nodes: [{ name: "alpha", module: "m/a" }], edges: [] });
  assert.equal(g.nodes.length, 1, "a file that does not end in a newline is still a complete file");
});
