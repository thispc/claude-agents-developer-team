// Module graph — NODES: the SVG card for one module, built with svgEl only.
// Everything user- or planner-authored (titles, agent names, activity) goes
// through svgEl's `text` attribute, which is textContent underneath — never
// markup — so a node called "<img onerror=…>" renders as its own name.
//
// Card anatomy: rounded rect (~170×64) · type glyph · title · test-status ring
// (green all passing / red any failing / grey none run) · a small initial chip
// when an agent is assigned. Aim and conclusion are bigger and tinted — they are
// the sentence's subject and full stop, not two more modules. GROUP cards are
// the architecture tier: bigger still, rolled-up numbers, and a ⊕ affordance
// (class gr-drill, data-drill=key) the canvas resolves as "dive in".

import { svgEl } from "../canvas2/world.js";

export const NODE_W = 170, NODE_H = 64;
const BIG_W = 208, BIG_H = 80;
const GROUP_W = 236, GROUP_H = 104;

export const GLYPH = { aim: "🎯", code: "📦", research: "🔬", data: "🗄",
                       group: "🗂", conclusion: "🏁" };

export function nodeSize(n) {
  if (n.node_type === "group") return { w: GROUP_W, h: GROUP_H };
  const big = n.node_type === "aim" || n.node_type === "conclusion";
  return { w: big ? BIG_W : NODE_W, h: big ? BIG_H : NODE_H };
}

function ringClass(tests) {
  const t = tests || {};
  if (!t.total || (!t.passing && !t.failing)) return "gr-ring-none";  // nothing run yet
  return t.failing ? "gr-ring-bad" : t.passing === t.total ? "gr-ring-good" : "gr-ring-mixed";
}

function trim(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function agentInitial(agent) {
  // The payload carries the specialist's NAME when the crew record knows it —
  // "C" for Correctness reads as a person; "4" (a bare row id) read as a bug.
  const name = String((agent && (agent.name || agent.agent_id || agent.home_id)) || "");
  return (name.trim()[0] || "?").toUpperCase();
}

function testsBrief(tests) {
  const t = tests || {};
  if (!t.total) return "no tests mapped";
  return `${t.passing}/${t.total} passing${t.failing ? " · advisory" : ""}`;
}

/** The ⊕ affordance on a group card: its own hit circle carrying data-drill, so
 * the canvas's pointerdown resolves it BEFORE the card underneath — pressing ⊕
 * dives into the group, pressing the card selects or drags it. */
function drillPlus(key, x, y) {
  return svgEl("g", { class: "gr-drill", "data-drill": String(key) }, [
    svgEl("circle", { class: "gr-drill-bg", cx: x, cy: y, r: 12, "pointer-events": "all" }),
    svgEl("text", { class: "gr-drill-t", x, y: y + 1, "text-anchor": "middle",
      "dominant-baseline": "central", "pointer-events": "none", text: "＋" }),
  ]);
}

/** Fill (or refill) a node group's content. Kept separate from buildNode so
 * updateNode can repaint data without losing the group's classes/position. */
function fill(g, n) {
  while (g.firstChild) g.removeChild(g.firstChild);
  const { w, h } = nodeSize(n);
  const inner = svgEl("g", { class: "gr-cardg" });
  if (n.node_type === "group") {
    const kids = Number(n.children) || 0;
    inner.append(
      svgEl("rect", { class: "gr-card", x: -w / 2, y: -h / 2, width: w, height: h, rx: 16 }),
      svgEl("rect", { class: "gr-topline", x: -w / 2 + 16, y: -h / 2 - 1,
        width: w - 32, height: 2.5, rx: 1.2, "pointer-events": "none" }),
      svgEl("text", { class: "gr-glyph", x: -w / 2 + 22, y: -h / 2 + 23, "text-anchor": "middle",
        "dominant-baseline": "central", "pointer-events": "none", text: GLYPH.group }),
      svgEl("text", { class: "gr-title", x: -w / 2 + 40, y: -h / 2 + 22, "dominant-baseline": "central",
        "pointer-events": "none", text: trim(n.title || n.key, 22) }),
      svgEl("text", { class: "gr-sub", x: -w / 2 + 18, y: -h / 2 + 52, "dominant-baseline": "central",
        "pointer-events": "none", text: `${kids} module${kids === 1 ? "" : "s"} inside` }),
      svgEl("text", { class: "gr-sub", x: -w / 2 + 18, y: -h / 2 + 72, "dominant-baseline": "central",
        "pointer-events": "none", text: testsBrief(n.tests) }),
      svgEl("circle", { class: "gr-ring " + ringClass(n.tests),
        cx: w / 2 - 15, cy: -h / 2 + 15, r: 6, fill: "none", "pointer-events": "none" }),
      drillPlus(n.key, w / 2 - 21, h / 2 - 21),
    );
    g.appendChild(inner);
    return;
  }
  inner.append(
    svgEl("rect", { class: "gr-card", x: -w / 2, y: -h / 2, width: w, height: h, rx: 12 }),
    svgEl("text", { class: "gr-glyph", x: -w / 2 + 16, y: 1, "text-anchor": "middle",
      "dominant-baseline": "central", "pointer-events": "none",
      text: GLYPH[n.node_type] || GLYPH.code }),
    svgEl("text", { class: "gr-title", x: -w / 2 + 32, y: -6, "dominant-baseline": "central",
      "pointer-events": "none", text: trim(n.title || n.key, 24) }),
    svgEl("text", { class: "gr-sub", x: -w / 2 + 32, y: 13, "dominant-baseline": "central",
      "pointer-events": "none",
      text: (n.tests && n.tests.total) ? testsBrief(n.tests) : n.node_type }),
    svgEl("circle", { class: "gr-ring " + ringClass(n.tests),
      cx: w / 2 - 13, cy: -h / 2 + 13, r: 6, fill: "none", "pointer-events": "none" }),
  );
  if (n.agent) inner.append(
    svgEl("circle", { class: "gr-agent", cx: w / 2 - 14, cy: h / 2 - 14, r: 9, "pointer-events": "none" }),
    svgEl("text", { class: "gr-agent-i", x: w / 2 - 14, y: h / 2 - 13, "text-anchor": "middle",
      "dominant-baseline": "central", "pointer-events": "none", text: agentInitial(n.agent) }),
  );
  g.appendChild(inner);
}

/** One module as an SVG group. data-key is what native hit-testing resolves. */
export function buildNode(n) {
  const g = svgEl("g", {
    class: "gr-node gr-" + (n.node_type || "code"),
    "data-key": String(n.key), "data-kind": "module",
  });
  fill(g, n);
  return g;
}

/** Repaint an existing node's content in place (title, ring, chip) without
 * touching its transform, selection or reveal state. */
export function updateNode(g, n) {
  const busy = g.classList.contains("gr-busy"), sel = g.classList.contains("gr-sel");
  fill(g, n);
  g.classList.toggle("gr-busy", busy);
  g.classList.toggle("gr-sel", sel);
}
