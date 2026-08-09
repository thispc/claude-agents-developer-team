// Canvas v2 — RENDER: build tokens (agents/props) and wires as real SVG nodes. Reuses the
// existing generators on `window` (lwAvatarSvg, colour helpers, slot geometry) so v2 is pixel-
// faithful to v1 without duplicating the look. Every token carries data-id/data-kind so native
// hit-testing (e.target.closest('.lw2-token')) resolves what was pressed — no hit graph.

import { svgEl } from "./world.js";

export const SIZES = { TABLE_R: 58, SOCKET_R: 80, RECT_W: 150, RECT_H: 92, SEAT_OUT: 20, AGENT_R: 30 };

const W = window;
const clamp01 = (v) => Math.max(0, Math.min(1, v == null ? 0.5 : +v));

export function tokenTransform(x, y) { return `translate(${x} ${y})`; }
export function setPos(g, x, y) { g.setAttribute("transform", tokenTransform(x, y)); }

// The MODEL SESSION USAGE meter (replaces the old mood bars): green while there's headroom,
// amber as the agent nears its model's session cap, red when it's out and asleep.
function usageMeter(usage, y) {
  usage = usage || {};
  const frac = clamp01(usage.frac || 0);
  const w = 56, h = 6, x0 = -w / 2;
  const color = usage.asleep ? "#b23b3b" : frac >= 0.85 ? "#c9721f" : frac >= 0.6 ? "#c9a11f" : "#3F7A3F";
  return svgEl("g", { class: "lw2-usage" }, [
    svgEl("rect", { x: x0, y, width: w, height: h, rx: 3, fill: "rgba(0,0,0,.13)" }),
    svgEl("rect", { x: x0, y, width: Math.max(1, w * frac), height: h, rx: 3, fill: color }),
  ]);
}

function labelText(text, y) {
  return svgEl("text", { class: "lw2-label", x: 0, y, "text-anchor": "middle", "dominant-baseline": "hanging", text: String(text) });
}

export function agentNode(a) {
  const R = SIZES.AGENT_R, seed = W.lwAvatarSeed(a), manager = W.lwIsManager(a);
  const rim = manager ? "#2C6A63" : W.lwAvatarColor(seed);
  const asleep = !!(a.usage && a.usage.asleep);
  // Working on something right now, according to the register — which knows about coding
  // sessions and verifications, not only model calls, and which expires a claim left behind
  // by a process that died. Falls back to the usage timestamp for any agent the register has
  // not heard of yet.
  const busy = !asleep && !!((a.activity && a.activity.busy) || (a.usage && a.usage.busy));
  const g = svgEl("g", { class: "lw2-token lw2-agent" + (manager ? " lw2-manager" : "")
    + (asleep ? " lw2-asleep" : "") + (busy ? " lw2-busy" : ""),
    "data-id": String(a.id), "data-kind": "agent" });
  if (busy) g.append(svgEl("circle", { r: SIZES.AGENT_R + 7, class: "lw2-busyring",
                                       fill: "none", "pointer-events": "none" }));
  g.append(
    svgEl("circle", { r: R + 3, fill: rim, class: "lw2-rim" }),
    svgEl("circle", { r: R, class: "lw2-body", fill: W.lwAvatarBg(seed), stroke: "rgba(255,255,255,.9)", "stroke-width": 2 }),
    svgEl("image", { href: W.lwSvgUri(W.lwAvatarSvg(seed, R * 2)), x: -R, y: -R, width: R * 2, height: R * 2, "pointer-events": "none" }),
    labelText(a.name || "someone", R + 8),
    usageMeter(a.usage, R + 26),
  );
  if (manager) g.append(svgEl("text", { class: "lw2-star", x: 0, y: -R - 10, "text-anchor": "middle", text: "★" }));
  if (asleep) g.append(svgEl("text", { class: "lw2-zzz", x: R - 4, y: -R + 2, "text-anchor": "middle", text: "z" }));
  return g;
}

function deckNode(p) {
  const g = svgEl("g", { class: "lw2-token lw2-prop lw2-deck", "data-id": String(p.id), "data-kind": "prop" });
  const cw = 40, ch = 56;
  for (let i = 2; i >= 0; i--)
    g.append(svgEl("rect", { x: -cw / 2 + i * 4, y: -ch / 2 - i * 4, width: cw, height: ch, rx: 6,
      fill: i === 0 ? "#fbfaf6" : "#efece3", stroke: "#cfc9bd", "stroke-width": 1.5, class: i === 0 ? "lw2-body" : "" }));
  g.append(svgEl("rect", { x: -cw / 2 + 5, y: -ch / 2 - 3, width: cw - 10, height: ch - 10, rx: 4,
    fill: "none", stroke: "rgba(46,110,91,.55)", "stroke-dasharray": "3 3", "stroke-width": 1, "pointer-events": "none" }));
  g.append(labelText(p.name || "deck", ch / 2 + 6));
  return g;
}

function tileNode(p) {
  const g = svgEl("g", { class: "lw2-token lw2-prop lw2-tile", "data-id": String(p.id), "data-kind": "prop" });
  const w = 58, h = 58, hue = W.lwHue(p.name), ink = `hsl(${hue} 42% 38%)`;
  g.append(svgEl("rect", { x: -w / 2, y: -h / 2, width: w, height: h, rx: 12, class: "lw2-body",
    fill: `hsl(${hue} 24% 92%)`, stroke: `hsl(${hue} 34% 62%)`, "stroke-width": 2 }));
  const fig = String(p.figure || ""), key = fig.startsWith("ic:") ? fig.slice(3) : "mono";
  if (key === "mono")
    g.append(svgEl("text", { x: 0, y: 0, "text-anchor": "middle", "dominant-baseline": "central",
      "font-size": 26, "font-weight": "bold", "font-family": "Georgia, serif", fill: ink, "pointer-events": "none", text: ((p.name || "?")[0] || "?").toUpperCase() }));
  else
    g.append(svgEl("image", { href: W.lwSvgUri(W.lwObjGlyphSvg(key, 34, ink)), x: -17, y: -17, width: 34, height: 34, "pointer-events": "none" }));
  g.append(labelText(p.name || "object", h / 2 + 6));
  return g;
}

function tableNode(p) {
  const g = svgEl("g", { class: "lw2-token lw2-prop lw2-table", "data-id": String(p.id), "data-kind": "prop" });
  const slots = Math.max(1, Number(p.slots) || 1), hue = W.lwHue(p.name), shape = String(p.shape || "circle");
  const fill = `hsl(${hue} 38% 54%)`, stroke = `hsl(${hue} 40% 40%)`;
  let labelY;
  if (shape === "rect") {
    g.append(svgEl("rect", { x: -SIZES.RECT_W / 2, y: -SIZES.RECT_H / 2, width: SIZES.RECT_W, height: SIZES.RECT_H, rx: 18, class: "lw2-body", fill, stroke, "stroke-width": 3 }));
    labelY = SIZES.RECT_H / 2 + 16;
  } else if (shape === "path" && Array.isArray(p.path) && p.path.length >= 3) {
    g.append(svgEl("polygon", { points: p.path.map((q) => `${+q[0]},${+q[1]}`).join(" "), class: "lw2-body", fill, stroke, "stroke-width": 3 }));
    labelY = Math.max(...p.path.map((q) => +q[1])) + 18;
  } else {
    g.append(svgEl("circle", { r: SIZES.TABLE_R, class: "lw2-body", fill, stroke, "stroke-width": 3 }));
    g.append(svgEl("circle", { r: SIZES.TABLE_R - 12, fill: "none", stroke: "rgba(255,255,255,.4)", "stroke-width": 1.5, "pointer-events": "none" }));
    labelY = SIZES.TABLE_R + 16;
  }
  const seated = Array.isArray(p.seated) ? p.seated : [], pos = W.lwSlotPositions(p);
  for (let i = 0; i < slots; i++) {
    const off = pos[i] || { x: 0, y: 0 }, filled = seated[i] != null;
    g.append(svgEl("circle", { cx: off.x, cy: off.y, r: 15, "pointer-events": "none",
      fill: filled ? "rgba(255,255,255,.16)" : "rgba(255,255,255,.05)",
      stroke: filled ? "rgba(255,255,255,.75)" : "rgba(255,255,255,.5)", "stroke-width": 2,
      "stroke-dasharray": filled ? null : "4 4" }));
  }
  const nSeated = seated.filter((s) => s != null).length;
  g.append(labelText(`${p.name || "table"} · ${nSeated}/${slots} seated`, labelY));
  return g;
}

export function propNode(p) {
  const slots = Number(p.slots) || 0, kind = String(p.kind || "").toLowerCase();
  if (slots > 0) return tableNode(p);
  if (kind.includes("deck") || kind.includes("card")) return deckNode(p);
  return tileNode(p);
}

export function node(kind, data) { return kind === "agent" ? agentNode(data) : propNode(data); }

// A speech bubble above a token (foreignObject → an HTML div so text wraps). Non-interactive;
// it rides the token's own transform, so it moves when the token moves. Rebuilt each round.
export function speechBubble(text, order) {
  const fo = svgEl("foreignObject", { class: "lw2-bubble-fo", x: -95, y: -118, width: 190, height: 96, "pointer-events": "none", overflow: "visible" });
  const wrap = document.createElement("div");
  wrap.className = "lw2-bubble-wrap";
  const div = document.createElement("div");
  div.className = "lw2-bubble";
  if (order) { const n = document.createElement("span"); n.className = "lw2-bubble-n"; n.textContent = String(order); div.appendChild(n); }   // speaking order
  div.appendChild(document.createTextNode(String(text || "").slice(0, 180)));
  wrap.appendChild(div);
  fo.appendChild(wrap);
  return fo;
}

// A wire between two world points, honouring direction (both | a2b | b2a). A fat transparent
// hit-line sits under the thin visible one so it's easy to click (native hit-testing on the stroke).
// How far short of a token's centre a wire stops. Wires used to run centre to centre, which
// drew the arrowhead UNDERNEATH the circle — the direction was rendered and then hidden, and
// the owner quite reasonably reported never being able to tell one-way from two-way.
const WIRE_GAP = SIZES.AGENT_R + 9;

/** Pull both ends of a wire in, so its arrowheads land outside the tokens. */
export function wireEnds(ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const len = Math.hypot(dx, dy) || 1;
  // Never invert a short wire: if the tokens overlap, draw the little that is left rather
  // than a line pointing backwards.
  const cut = Math.min(WIRE_GAP, Math.max(0, (len - 6) / 2));
  const ux = (dx / len) * cut, uy = (dy / len) * cut;
  return { x1: ax + ux, y1: ay + uy, x2: bx - ux, y2: by - uy };
}

export function wireNode(edge) {
  const dir = edge.dir || "both";
  const oneWay = dir === "a2b" || dir === "b2a";
  const e = wireEnds(edge.ax, edge.ay, edge.bx, edge.by);
  const g = svgEl("g", { class: "lw2-wire" + (oneWay ? " lw2-wire-one" : " lw2-wire-both"),
    "data-tid": String(edge.tid), "data-a": String(edge.a), "data-b": String(edge.b) });
  g.append(
    svgEl("line", { class: "lw2-wire-hit", x1: e.x1, y1: e.y1, x2: e.x2, y2: e.y2,
      stroke: "transparent", "stroke-width": 18, "pointer-events": "stroke" }),
    // ONE head means one-way; TWO heads mean both ways. Head-versus-no-head was the old
    // encoding and it is nearly invisible at any real zoom — the difference has to be a
    // thing that is THERE rather than a thing that is absent, because conversation flow
    // depends on reading it correctly.
    svgEl("line", { class: "lw2-wire-vis", x1: e.x1, y1: e.y1, x2: e.x2, y2: e.y2,
      stroke: "hsl(160 45% 42%)", "stroke-width": 2.5, "stroke-linecap": "round", "pointer-events": "none",
      "stroke-dasharray": edge.closed ? null : "8 5",
      "marker-end": dir === "a2b" || dir === "both" ? "url(#lw2-arrow)" : null,
      "marker-start": dir === "b2a" || dir === "both" ? "url(#lw2-arrow)" : null }),
  );
  return g;
}
export function setWireEnds(g, ax, ay, bx, by) {
  const e = wireEnds(ax, ay, bx, by);
  g.querySelectorAll("line").forEach((ln) => {
    ln.setAttribute("x1", e.x1); ln.setAttribute("y1", e.y1);
    ln.setAttribute("x2", e.x2); ln.setAttribute("y2", e.y2);
  });
}
