import { test } from "node:test";
import assert from "node:assert/strict";
import { render } from "../run.js";

const WIRING = [
  `# a comment`,                    // 1
  ``,                               // 2
  `[[node]]`,                       // 3
  `name = "alpha"`,                 // 4
  `module = "modules/alpha"`,       // 5
  ``,                               // 6
  `[[node]]`,                       // 7
  `name = "beta"`,                  // 8
  `module = "modules/beta"`,        // 9
  ``,                               // 10
  `[[edge]]`,                       // 11
  `from = "alpha"`,                 // 12
  `to = "beta"`,                    // 13
  `why = "beta reads what alpha wrote"`, // 14
].join("\n");

const NODES = [{ name: "alpha", module: "modules/alpha" }, { name: "beta", module: "modules/beta" }];
const EDGES = [{ from: "alpha", to: "beta", why: "beta reads what alpha wrote" }];

/** @param {Partial<Parameters<typeof render>[0]>} [over] */
function call(over = {}) {
  return render({ wiringPath: "wiring.toml", wiringText: WIRING, nodes: NODES, edges: EDGES, ...over });
}

test("every node points at the line it is declared on", () => {
  const g = call();
  assert.equal(g.nodes.length, 2);
  const alpha = g.nodes.find((n) => n.id === "alpha");
  assert.ok(alpha);
  assert.deepEqual(alpha.evidence[0], { file: "wiring.toml", line: 3 });
});

test("every edge points at the line it is declared on", () => {
  const g = call();
  assert.equal(g.edges.length, 1);
  assert.deepEqual(g.edges[0]?.evidence[0], { file: "wiring.toml", line: 11 });
});

test("a node with no declaration in the file is DROPPED, not drawn", () => {
  // The failure this module exists to prevent: a graph that can assert a node
  // the code does not have.
  const g = call({ nodes: [...NODES, { name: "ghost", module: "modules/ghost" }] });
  assert.equal(g.nodes.length, 2, "the invented node must not be drawn");
  assert.ok(g.dropped.some((d) => d.what.includes("ghost")));
  assert.match(g.summary, /1 dropped for want of evidence/);
});

test("an edge whose end was dropped is dropped too — no arrows into nothing", () => {
  const g = call({
    nodes: [{ name: "alpha", module: "modules/alpha" }],
    edges: [{ from: "alpha", to: "beta" }],
  });
  assert.equal(g.edges.length, 0);
  assert.ok(
    g.dropped.some((d) => d.what.includes("alpha → beta")),
    `the edge should have been dropped, got ${JSON.stringify(g.dropped)}`
  );
});

test("an edge the wiring never declared is dropped even when both ends exist", () => {
  const g = call({ edges: [...EDGES, { from: "beta", to: "alpha" }] });
  assert.equal(g.edges.length, 1);
  assert.ok(g.dropped.some((d) => d.what.includes("beta → alpha")));
});

test("status comes from the ledger, not from anybody's opinion", () => {
  const ledger = [
    { t: "admit", contract: "c-a", artifact: "a-1", module: "alpha", at: "2026-01-01T00:00:00Z", proved: ["size", "tests"] },
  ];
  const g = call({ ledger, modules: { alpha: { contract: "c-a", loc: 120, surface: 3 } } });
  const alpha = g.nodes.find((n) => n.id === "alpha");
  assert.equal(alpha?.status, "live");
  assert.equal(alpha?.artifact, "a-1");
  assert.deepEqual(alpha?.proved, ["size", "tests"]);
  assert.equal(alpha?.loc, 120);
});

test("a module nothing has been admitted for reads as unbuilt, not as broken", () => {
  const g = call({ ledger: [], modules: { alpha: { contract: "c-a" } } });
  assert.equal(g.nodes.find((n) => n.id === "alpha")?.status, "unbuilt");
});

test("a refusal after an admission leaves the node live, and says so", () => {
  const ledger = [
    { t: "admit", contract: "c-a", artifact: "a-1", module: "alpha", at: "2026-01-01T00:00:00Z", proved: ["tests"] },
    { t: "reject", contract: "c-a", artifact: "a-2", module: "alpha", at: "2026-01-02T00:00:00Z", gate: "heldout", why: "exit 1" },
  ];
  const g = call({ ledger, modules: { alpha: { contract: "c-a" } } });
  const alpha = g.nodes.find((n) => n.id === "alpha");
  assert.equal(alpha?.status, "live", "a failed improvement is a non-event — the module that was live is still live");
  assert.match(/** @type {string} */ (alpha?.note), /refused at the heldout gate and changed nothing/);
});

test("a contract with only refusals reads as refused", () => {
  const ledger = [{ t: "reject", contract: "c-a", artifact: "a-1", module: "alpha", at: "2026-01-01T00:00:00Z", gate: "size", why: "too big" }];
  const g = call({ ledger, modules: { alpha: { contract: "c-a" } } });
  assert.equal(g.nodes.find((n) => n.id === "alpha")?.status, "refused");
});

test("a pin outranks a later admission, and the node says it was pinned by hand", () => {
  const ledger = [
    { t: "admit", contract: "c-a", artifact: "a-1", module: "alpha", at: "2026-01-01T00:00:00Z", proved: [] },
    { t: "pin", contract: "c-a", artifact: "a-1", module: "alpha", at: "2026-01-02T00:00:00Z", by: "owner" },
    { t: "admit", contract: "c-a", artifact: "a-2", module: "alpha", at: "2026-01-03T00:00:00Z", proved: [] },
  ];
  const g = call({ ledger, modules: { alpha: { contract: "c-a" } } });
  const alpha = g.nodes.find((n) => n.id === "alpha");
  assert.equal(alpha?.artifact, "a-1");
  assert.match(/** @type {string} */ (alpha?.note), /pinned by hand/);
});

test("comments and blank lines do not shift the reported line numbers", () => {
  const padded = ["# one", "# two", "", "", WIRING].join("\n");
  const g = render({ wiringPath: "wiring.toml", wiringText: padded, nodes: NODES, edges: EDGES });
  assert.equal(g.nodes.find((n) => n.id === "alpha")?.evidence[0]?.line, 7, "4 lines of padding + the original line 3");
});

// ── Below this line: what the HELD-OUT suite caught. Now permanent.
//
// The visible tests above feed this module a tidy wiring file written in the
// same sitting as the parser, which is the shape of test that encodes what the
// implementation already assumes. These are what a file people actually edit
// looks like.

test("a trailing comment on a block header does not hide the block", () => {
  // The failure this was written for: `raw === "[[node]]"` makes
  // `[[node]]  # the first one` invisible, and an invisible node is dropped
  // silently — the one outcome this module exists to prevent.
  const text = [`[[node]]  # the first one`, `name = "solo"`, `module = "modules/solo"`].join("\n");
  const g = render({ wiringPath: "w.toml", wiringText: text, nodes: [{ name: "solo", module: "modules/solo" }], edges: [] });
  assert.equal(g.nodes.length, 1, `got ${JSON.stringify(g.dropped)}`);
  assert.equal(g.nodes[0]?.evidence[0]?.line, 1);
});

test("CRLF line endings neither shift nor lose anything", () => {
  const text = [`[[node]]`, `name = "solo"`, `module = "modules/solo"`].join("\r\n");
  const g = render({ wiringPath: "w.toml", wiringText: text, nodes: [{ name: "solo", module: "modules/solo" }], edges: [] });
  assert.equal(g.nodes.length, 1, "a file saved on Windows is still a file");
  assert.equal(g.nodes[0]?.evidence[0]?.line, 1);
});

test("the last block is not lost when the file has no trailing newline", () => {
  const text = `[[node]]\nname = "solo"\nmodule = "modules/solo"`;
  const g = render({ wiringPath: "w.toml", wiringText: text, nodes: [{ name: "solo", module: "modules/solo" }], edges: [] });
  assert.equal(g.nodes.length, 1);
});

test("nothing wired at all renders an empty graph rather than throwing", () => {
  const g = render({ wiringPath: "wiring.toml", wiringText: "", nodes: [], edges: [] });
  assert.deepEqual(g.nodes, []);
  assert.deepEqual(g.edges, []);
  assert.match(g.summary, /0 node/);
});
