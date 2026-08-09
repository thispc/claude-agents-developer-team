// Canvas v2 — the WORLD: an infinite, GPU-composited pan/zoom surface built on a single
// CSS transform. There is NO pixel/hit canvas: tokens and wires are real SVG/DOM nodes, so
// the browser's own compositor does hit-testing (e.target) — it cannot desync at any DPR or
// transform, which is the entire reason v2 exists. Vector rendering stays crisp at any zoom.
//
// DOM shape:
//   host (#lwKonvaHost, clip)                     — fixed viewport
//     └─ viewport (.lw2-viewport, transformed)    — world space: translate(pan) scale(k)
//          └─ svg (.lw2-svg, overflow visible)
//               ├─ <g class=lw2-wires>            — edges, beneath tokens
//               ├─ <g class=lw2-tokens>           — agents / props
//               └─ <g class=lw2-overlay>          — selection frames, handles, marquee, rubber

const MIN_SCALE = 0.25, MAX_SCALE = 3.2;
const SVGNS = "http://www.w3.org/2000/svg";

export function svgEl(tag, attrs, kids) {
  const el = document.createElementNS(SVGNS, tag);
  if (attrs) for (const k in attrs) {
    if (attrs[k] == null) continue;
    if (k === "text") el.textContent = attrs[k];
    else el.setAttribute(k, attrs[k]);
  }
  if (kids) for (const c of kids) if (c) el.appendChild(c);
  return el;
}

export function createWorld(host) {
  host.classList.add("lw2-host");
  host.innerHTML = "";

  const viewport = document.createElement("div");
  viewport.className = "lw2-viewport";

  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("class", "lw2-svg");
  svg.setAttribute("overflow", "visible");
  // one shared arrowhead marker for directed wires
  const defs = svgEl("defs", null, [
    svgEl("marker", { id: "lw2-arrow", viewBox: "0 0 12 12", refX: "10", refY: "6",
      markerWidth: "11", markerHeight: "11", orient: "auto-start-reverse" },
      [svgEl("path", { d: "M1,1 L11,6 L1,11 z", fill: "hsl(160 52% 38%)" })]),
  ]);
  // A huge transparent rect that CATCHES empty-floor presses (pointer-events:all fires even on a
  // transparent fill), so marquee/pan start reliably and e.target is always a real SVG node.
  const bg = svgEl("rect", { class: "lw2-bg", x: -1e5, y: -1e5, width: 2e5, height: 2e5, fill: "transparent", "pointer-events": "all" });
  const gWires = svgEl("g", { class: "lw2-wires" });
  const gTokens = svgEl("g", { class: "lw2-tokens" });
  const gOverlay = svgEl("g", { class: "lw2-overlay" });
  svg.append(defs, bg, gWires, gTokens, gOverlay);
  viewport.appendChild(svg);
  host.appendChild(viewport);

  const state = { x: 0, y: 0, k: 1 };

  function apply() {
    viewport.style.transform = `translate(${state.x}px, ${state.y}px) scale(${state.k})`;
  }
  apply();

  function rect() { return host.getBoundingClientRect(); }

  // screen(client) → world
  function toWorld(clientX, clientY) {
    const r = rect();
    return { x: (clientX - r.left - state.x) / state.k, y: (clientY - r.top - state.y) / state.k };
  }
  // world → screen(client)
  function toScreen(wx, wy) {
    const r = rect();
    return { x: wx * state.k + state.x + r.left, y: wy * state.k + state.y + r.top };
  }

  function setView(v) {
    if (!v) return;
    state.x = v.x; state.y = v.y; state.k = Math.max(MIN_SCALE, Math.min(MAX_SCALE, v.k));
    apply();
  }
  function getView() { return { x: state.x, y: state.y, k: state.k }; }

  function panBy(dx, dy) { state.x += dx; state.y += dy; apply(); }

  // zoom keeping the world point under the cursor fixed
  function zoomAt(clientX, clientY, factor) {
    const before = toWorld(clientX, clientY);
    state.k = Math.max(MIN_SCALE, Math.min(MAX_SCALE, state.k * factor));
    const r = rect();
    state.x = (clientX - r.left) - before.x * state.k;
    state.y = (clientY - r.top) - before.y * state.k;
    apply();
  }

  // frame a set of world-space bounds {minX,minY,maxX,maxY} with padding
  function fit(bounds, pad = 90) {
    const r = rect();
    if (!bounds || !isFinite(bounds.minX)) { state.x = r.width / 2; state.y = r.height / 2; state.k = 1; apply(); return; }
    const bw = Math.max(1, bounds.maxX - bounds.minX), bh = Math.max(1, bounds.maxY - bounds.minY);
    const k = Math.max(MIN_SCALE, Math.min(MAX_SCALE, Math.min((r.width - pad * 2) / bw, (r.height - pad * 2) / bh, 1.15)));
    state.k = k;
    state.x = (r.width - bw * k) / 2 - bounds.minX * k;
    state.y = (r.height - bh * k) / 2 - bounds.minY * k;
    apply();
  }

  return {
    el: { host, viewport, svg, gWires, gTokens, gOverlay },
    toWorld, toScreen, setView, getView, panBy, zoomAt, fit,
    get scale() { return state.k; },
    destroy() { try { host.innerHTML = ""; host.classList.remove("lw2-host"); } catch (e) { /* */ } },
  };
}
