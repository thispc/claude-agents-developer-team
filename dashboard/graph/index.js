// Module graph — orchestrator. The platform rendered as a living, TWO-LEVEL graph
// of verified modules: aim → architecture groups → (dive in) → each group's modules
// → conclusion. Level 0 reads like a clean architecture diagram; double-clicking a
// group is a MICROSCOPE — the camera flies at it while it dissolves into its
// subgraph. Which level you are on lives in the hash (#/graph, #/graph/<group>).
//
// Engine discipline (learned the hard way on canvas2, each point deliberate):
//   · createWorld/svgEl are IMPORTED from canvas2/world.js, never copied — one engine.
//   · This module owns exactly ONE instance of ITSELF, created by open() and torn
//     down by close(); it never registers globals another canvas reads, so mounting
//     the graph cannot kill the Studio's canvas or vice versa.
//   · Key listeners live on the #graphScreen element (tabindex="-1"), so typing
//     anywhere else in the app can never fall through to graph shortcuts.
//   · The inspector and every action button render inside #graphScreen's own aside —
//     no borrowed hosts from other screens.
//   · Reduced motion is decided here with matchMedia, not via another script's const.
//   · Motion is CSS transform/opacity only; the ONE rAF loop in this file lives
//     inside flyTo() and runs ONLY while the camera is in flight.

import { createWorld, svgEl } from "../canvas2/world.js";
import { wireNode, setWireEnds, setPos, speechBubble, SIZES } from "../canvas2/render.js";
import { buildNode, updateNode, nodeSize, GLYPH } from "./nodes.js";
import { layout, topoOrder } from "./layout.js";

const W = window;
const DRAG_THRESH = 4;                 // px of screen travel before a press becomes a drag
const WIRE_GAP = SIZES.AGENT_R + 9;    // render.js pulls this much off each wire end
const THEME_KEY = "gr:theme";          // "blueprint" (this screen's default) | "paper"
const L0_GRID = { colW: 350, rowH: 215 };   // architecture tier: generous air
const L1_GRID = { colW: 300, rowH: 175 };   // inside a group: a little tighter
const PORTAL = { w: 128, h: 34 };      // the pill stubs cross-group wires land on
const DRILL_MS = 450;                  // the microscope flight
const SWAP_MS = 230;                   // when the outgoing level yields the stage
const reduceMotion = () =>
  W.matchMedia && W.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Which plan_id:key pairs have already had their entrance. Module state, so a new
// plan version replays the reveal but a mere route-away-and-back does not; entering
// a level deliberately forgets its keys so every dive replays the staging.
const SEEN = new Set();

let inst = null;                       // this graph's one live instance

// ---- the source seam ------------------------------------------------------------
// projSrc's discipline: renderers never branch on where the truth comes from.
// V1 ships the devteam source; V2 adds a project source with the same five verbs.
const DEVTEAM_GRAPH_SRC = {
  name: "self",
  fetch: () => W.api("/api/graph/self"),
  verify: (key) => W.api("/api/graph/self/verify", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node: key }) }),
  saveLayout: (positions) => W.api("/api/graph/self/layout", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ positions }) }),
  setConfig: (key, cfg) => W.api(`/api/graph/self/node/${encodeURIComponent(key)}/config`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg) }),
  inspect: (key) => W.api(`/api/graph/self/node/${encodeURIComponent(key)}`),
};
const SOURCES = { self: DEVTEAM_GRAPH_SRC };

// ---- small helpers --------------------------------------------------------------
const esc = (s) => (W.escapeHtml ? W.escapeHtml(String(s ?? "")) : String(s ?? ""));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
function nowMs() { return (W.performance && performance.now) ? performance.now() : Date.now(); }

/** src/dst regardless of which serializer produced the edge. */
function edgeEnds(e) {
  return [String(e.src ?? e.src_key ?? ""), String(e.dst ?? e.dst_key ?? "")];
}

// ---- lifecycle ------------------------------------------------------------------
/** Open the graph at a level. `sub` is the drill sub-path from the hash —
 * "" for the architecture view, a group key for inside that group. Called again
 * while already open (every #/graph/... hash change lands here via route()), it
 * becomes NAVIGATION: the existing instance animates between levels instead of
 * being torn down, which is what makes the microscope transition possible. */
function open(sourceName, sub) {
  const level = decodeURIComponent(String(sub || "")) || null;
  if (inst && inst.srcName === (sourceName || "self")) {
    if (inst.data) navTo(inst, level);
    else inst.level = level;               // fetch still in flight — land there directly
    inst.screen.focus({ preventScroll: true });
    return;
  }
  close();
  const screen = document.getElementById("graphScreen");
  const host = document.getElementById("graphCanvas");
  const aside = document.getElementById("graphAside");
  if (!screen || !host || !aside) return;
  const world = createWorld(host);
  inst = {
    src: SOURCES[sourceName] || DEVTEAM_GRAPH_SRC,
    srcName: sourceName || "self",
    screen, host, aside, world,
    hostRect: host.getBoundingClientRect(),
    level,                     // null = architecture view, else the open group's key
    nodes: new Map(),          // key -> { key, data, g, x, y }  (visible level only)
    edges: [],                 // { a, b, g, d, pa, pb }
    portals: new Map(),        // portal pk -> { g, x, y, out, drill }
    byKey: new Map(),          // every node in the payload, visible or not
    sel: new Set(),
    inspectKey: null,
    gesture: null, space: false,
    data: null, firstPaint: true,
    transition: false,         // a level flight is in progress — repaints hold fire
    revealQueue: [], revealTimer: null,
    pollTimer: null, refetchTimer: null, swapTimer: null, drawTimer: null,
    crumb: document.getElementById("graphCrumb"),
    mini: document.getElementById("graphMini"),
    tip: document.getElementById("graphTip"),
    tipOn: false,
    viewKey: "gr:view:" + (SOURCES[sourceName] ? sourceName : "self"),
  };
  applyTheme(inst, safeGet(THEME_KEY) || "blueprint");
  const tBtn = document.getElementById("graphTheme");
  if (tBtn) tBtn.onclick = () => {
    if (inst) applyTheme(inst,
      inst.screen.dataset.gtheme === "blueprint" ? "paper" : "blueprint");
  };
  wireEvents(inst);
  asideDefault(inst);
  const back = document.getElementById("graphBack");
  if (back) back.onclick = () => { location.hash = "#/hq"; };
  // Live updates: the classic scripts own the socket; connectWs re-broadcasts
  // graph/repair kinds as DOM CustomEvents, which a module CAN hear.
  inst._onGraphEvent = (ev) => { if (inst) onGraphEvent(inst, ev.detail || {}); };
  document.addEventListener("graph-event", inst._onGraphEvent);
  // ...and a 6s poll as the fallback, PAUSED while something is selected (or the
  // camera is mid-flight) so a repaint never clears a selection, blanks the
  // inspector mid-sentence, or repaints the stage under a transition.
  inst.pollTimer = setInterval(() => {
    if (!inst || inst.sel.size || inst.inspectKey || inst.gesture || inst.transition) return;
    refetch(inst);
  }, 6000);
  screen.focus({ preventScroll: true });
  refetch(inst);
}

function close() {
  if (!inst) return;
  clearInterval(inst.pollTimer);
  clearTimeout(inst.refetchTimer);
  clearTimeout(inst.revealTimer);
  clearTimeout(inst.swapTimer);
  clearTimeout(inst.drawTimer);
  clearTimeout(inst.oldTimer);
  inst._fly = null;                        // the camera loop checks this token and stops
  hideTip(inst);
  document.removeEventListener("graph-event", inst._onGraphEvent);
  try { inst._teardown && inst._teardown(); } catch (e) { /* */ }
  try { inst.world.destroy(); } catch (e) { /* */ }
  inst = null;
}

async function refetch(i) {
  let data;
  try { data = await i.src.fetch(); }
  catch (e) {
    if (i === inst && !i.data)
      i.aside.innerHTML = `<p class="err">${esc((e && e.message) || String(e))}</p>`;
    return;
  }
  if (i !== inst) return;               // closed while the fetch was in flight
  if (i.transition) { scheduleRefetch(i); return; }   // never repaint mid-flight
  paint(i, data);
}

function scheduleRefetch(i) {
  if (i.refetchTimer) return;
  i.refetchTimer = setTimeout(() => {
    i.refetchTimer = null;
    if (!inst || inst.sel.size || inst.inspectKey || inst.gesture || inst.transition) return;
    refetch(inst);
  }, 800);
}

// ---- themes ---------------------------------------------------------------------
// Two looks, owner-switchable, persisted: "blueprint" — the deep-ink HUD this
// screen defaults to — and "paper", the app's own warm variables. The switch is
// one data attribute; every colour in graph.css resolves through #graphScreen-
// scoped custom properties, so the rest of the app never notices either way.
function safeGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function applyTheme(i, name) {
  const theme = name === "paper" ? "paper" : "blueprint";
  i.screen.dataset.gtheme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* */ }
  const b = document.getElementById("graphTheme");
  if (b) b.textContent = theme === "paper" ? "◑ paper" : "◐ blueprint";
}

// ---- the camera -----------------------------------------------------------------
/** THE one rAF driver in this module: an eased camera flight. It exists only for
 * the duration of a transition — no rAF loop runs while the graph is at rest.
 * The centre of interest travels on a cubic-out; on drill-ins the zoom itself
 * takes a back-eased curve, which is the tiny spring overshoot the microscope
 * lands with. Under prefers-reduced-motion the camera simply arrives. */
function easeOutCubic(p) { return 1 - Math.pow(1 - p, 3); }
function easeOutBack(p) { const c = 1.4; return 1 + (c + 1) * Math.pow(p - 1, 3) + c * Math.pow(p - 1, 2); }

function flyTo(i, target, ms, spring, done) {
  const token = {};
  i._fly = token;
  i.hostRect = i.host.getBoundingClientRect();      // once per flight, never per frame
  if (reduceMotion() || !ms) {
    i.world.setView(target); miniViewport(i); i._fly = null;
    if (done) done();
    return;
  }
  const r = i.hostRect;
  const from = i.world.getView();
  const c0 = { x: (r.width / 2 - from.x) / from.k, y: (r.height / 2 - from.y) / from.k };
  const c1 = { x: (r.width / 2 - target.x) / target.k, y: (r.height / 2 - target.y) / target.k };
  const t0 = nowMs();
  const tick = () => {
    if (i._fly !== token || inst !== i) return;     // superseded or closed — stand down
    const p = clamp((nowMs() - t0) / ms, 0, 1);
    const eP = easeOutCubic(p), eK = spring ? easeOutBack(p) : eP;
    const k = from.k + (target.k - from.k) * eK;
    const cx = c0.x + (c1.x - c0.x) * eP, cy = c0.y + (c1.y - c0.y) * eP;
    i.world.setView({ k, x: r.width / 2 - cx * k, y: r.height / 2 - cy * k });
    miniViewport(i);
    if (p < 1) requestAnimationFrame(tick);
    else { i._fly = null; if (done) done(); }
  };
  requestAnimationFrame(tick);
}

/** A view framing world-space bounds — world.fit's math without applying it,
 * so the camera can EASE there instead of jumping. */
function viewFor(i, bounds, pad) {
  pad = pad || 120;
  const r = i.hostRect;
  if (!bounds || !isFinite(bounds.minX)) return { x: r.width / 2, y: r.height / 2, k: 1 };
  const bw = Math.max(1, bounds.maxX - bounds.minX), bh = Math.max(1, bounds.maxY - bounds.minY);
  const k = clamp(Math.min((r.width - pad * 2) / bw, (r.height - pad * 2) / bh, 1.15), 0.25, 3.2);
  return { k, x: (r.width - bw * k) / 2 - bounds.minX * k, y: (r.height - bh * k) / 2 - bounds.minY * k };
}

// ---- the two levels -------------------------------------------------------------
/** The nodes a level shows. Top level: aim + groups + conclusion (or the whole
 * flat plan when no groups were authored — the old graph keeps working). Inside a
 * group: exactly its children. */
function levelNodes(data, level) {
  const ns = (data && data.nodes) || [];
  if (level) return ns.filter((n) => String(n.parent_key || "") === level);
  if (!ns.some((n) => n.node_type === "group")) return ns;
  return ns.filter((n) => !String(n.parent_key || ""));
}

/** Where a group card sits at level 0 — the world point its subgraph unfolds
 * around, so the microscope lands exactly where the card used to be. */
function groupAnchor(i, key) {
  const top = levelNodes(i.data, null);
  const keys = new Set(top.map((n) => String(n.key)));
  const es = ((i.data && i.data.edges) || []).filter((e) => {
    const [s, d] = edgeEnds(e); return keys.has(s) && keys.has(d);
  });
  const pos = layout(top, es, (i.data && i.data.positions) || {}, L0_GRID);
  return pos[key] || { x: 0, y: 0 };
}

/** Layout for one level: auto-layout on the level's own subgraph, a group's
 * children recentred onto the group's home spot (the reclaimed space), and saved
 * drags winning verbatim — keys are unique across levels, so the positions
 * payload needs no new shape. */
function levelLayout(i, level) {
  const ns = levelNodes(i.data, level);
  const keys = new Set(ns.map((n) => String(n.key)));
  const inEdges = ((i.data && i.data.edges) || []).filter((e) => {
    const [s, d] = edgeEnds(e); return keys.has(s) && keys.has(d);
  });
  const pos = layout(ns, inEdges, {}, level ? L1_GRID : L0_GRID);
  if (level && ns.length) {
    const a = groupAnchor(i, level);
    let cx = 0, cy = 0;
    ns.forEach((n) => { cx += pos[n.key].x; cy += pos[n.key].y; });
    cx /= ns.length; cy /= ns.length;
    ns.forEach((n) => { pos[n.key].x += a.x - cx; pos[n.key].y += a.y - cy; });
  }
  const saved = (i.data && i.data.positions) || {};
  ns.forEach((n) => {
    const s = saved[n.key];
    if (Array.isArray(s) && s.length === 2) pos[n.key] = { x: +s[0], y: +s[1] };
  });
  return { ns, keys, inEdges, pos };
}

function boundsFor(ns, pos) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of ns) {
    const p = pos[n.key]; if (!p) continue;
    const { w, h } = nodeSize(n);
    minX = Math.min(minX, p.x - w / 2 - 20); minY = Math.min(minY, p.y - h / 2 - 20);
    maxX = Math.max(maxX, p.x + w / 2 + 20); maxY = Math.max(maxY, p.y + h / 2 + 30);
  }
  return { minX, minY, maxX, maxY };
}

/** The view a level should be framed at, computed BEFORE it is painted — the
 * camera flies toward where the destination will be. */
function targetViewFor(i, level) {
  const { ns, pos } = levelLayout(i, level);
  return viewFor(i, boundsFor(ns, pos), level ? 190 : 120);
}

// ---- navigation: the microscope -------------------------------------------------
/** All drilling funnels through the hash, so the address bar, the back button and
 * a pasted link all mean the same thing. route() → openModuleGraph → open → navTo. */
function drillTo(i, key) {
  hideTip(i);
  location.hash = key ? "#/graph/" + encodeURIComponent(key) : "#/graph";
}

/** The level transition, frame by frame: (1) the outgoing cards bow out — every
 * card takes .gr-exit (scale-down fade) except a drilled-into group, which takes
 * .gr-zoombig and BLOOMS, its card expanding past the camera as it fades, wires
 * fading with them; (2) the camera starts its 450ms eased flight toward the
 * destination's framing (spring overshoot on the way DOWN); (3) ~230ms in, while
 * everything is still moving, the stage swaps — the old level's elements are
 * dropped and the new level paints hidden, then reveals card-by-card (60ms
 * stagger), wires drawing themselves last via dash-offset; (4) the camera lands,
 * the transition flag lifts and polling resumes. Reduced motion: an instant swap
 * at the destination view, nothing in between. */
function navTo(i, level) {
  level = level || null;
  if (level === i.level) { renderCrumb(i); return; }
  if (!i.data) { i.level = level; return; }
  clearSel(i); i.inspectKey = null; hideTip(i); asideDefault(i);
  const dirIn = !!level && !i.level;         // top → group gets the spring
  if (reduceMotion()) {
    const v = targetViewFor(i, level);
    setLevelNow(i, level, true);
    i.world.setView(v); miniViewport(i); saveView(i);
    return;
  }
  i.transition = true;
  clearTimeout(i.swapTimer);
  clearTimeout(i.revealTimer);
  i.revealTimer = null; i.revealQueue = [];
  // (1) the outgoing level bows out
  i.nodes.forEach((t) => t.g.classList.add(
    level && t.key === level ? "gr-zoombig" : "gr-exit"));
  i.edges.forEach((e) => e.g.classList.add("gr-exit"));
  i.portals.forEach((p) => p.g.classList.add("gr-exit"));
  // (2) the flight
  flyTo(i, targetViewFor(i, level), DRILL_MS, dirIn, () => {
    i.transition = false;
    saveView(i);
    scheduleRefetch(i);
  });
  // (3) the swap, mid-flight
  i.swapTimer = setTimeout(() => { if (inst === i) setLevelNow(i, level); }, SWAP_MS);
}

/** Cut to a level: forget the level's entrance keys so the staging replays, and
 * paint. Outgoing cards KEEP the stage briefly so their exit transition can
 * finish under the incoming reveal (CSS gives .gr-exit/.gr-zoombig
 * pointer-events:none, so they cannot shadow hit-testing while they fade). */
function setLevelNow(i, level, instant) {
  i.level = level;
  const olds = [];
  i.nodes.forEach((t) => olds.push(t.g));
  i.nodes.clear();
  clearTimeout(i.oldTimer);
  if (instant || reduceMotion()) olds.forEach((g) => g.remove());
  else i.oldTimer = setTimeout(() => olds.forEach((g) => g.remove()), 340);
  const planId = i.data && i.data.plan && i.data.plan.id;
  levelNodes(i.data, level).forEach((n) => SEEN.delete(planId + ":" + n.key));
  paint(i, i.data);
}

// ---- painting -------------------------------------------------------------------
function paint(i, data) {
  i.data = data;
  i.hostRect = i.host.getBoundingClientRect();
  i.byKey = new Map((data.nodes || []).map((n) => [String(n.key), n]));
  const planId = data.plan && data.plan.id;
  // a replan can dissolve the group being viewed — fall back to the architecture
  // (and let the hash follow; route() lands on navTo(null), already a no-op here)
  if (i.level && !levelNodes(data, i.level).length) { i.level = null; location.hash = "#/graph"; }
  const counts = {};
  for (const n of data.nodes || []) {
    const p = String(n.parent_key || "");
    if (p) counts[p] = (counts[p] || 0) + 1;
  }
  const { ns, inEdges, pos } = levelLayout(i, i.level);
  const live = new Set();
  for (const n of ns) {
    if (n.node_type === "group") n.children = counts[String(n.key)] || 0;
    const key = String(n.key);
    live.add(key);
    let t = i.nodes.get(key);
    if (!t) {
      const g = buildNode(n);
      if (!SEEN.has(planId + ":" + key)) g.classList.add("gr-hidden");
      i.world.el.gTokens.appendChild(g);
      t = { key, data: n, g, x: 0, y: 0 };
      i.nodes.set(key, t);
    } else {
      updateNode(t.g, n);
      t.data = n;
    }
    const p = pos[key] || { x: 0, y: 0 };
    t.x = p.x; t.y = p.y;
    setPos(t.g, p.x, p.y);
  }
  for (const [key, t] of [...i.nodes]) if (!live.has(key)) { t.g.remove(); i.nodes.delete(key); }
  const fresh = topoOrder(ns, inEdges).filter((k) => !SEEN.has(planId + ":" + k));
  buildPortals(i, fresh.length > 0);
  rebuildWires(i);
  showActivity(i);
  reselect(i);
  const planInfo = document.getElementById("graphPlanInfo");
  if (planInfo && data.plan) planInfo.textContent =
    `v${data.plan.version} · by ${data.plan.authored_by || "seed"}`;
  if (fresh.length) reveal(i, planId, fresh);
  if (i.firstPaint) { i.firstPaint = false; restoreView(i); }
  renderMini(i);
  renderCrumb(i);
}

/** The staged entrance: cards scale/fade in one at a time in topo order (~60ms
 * stagger, a CSS scale/opacity transition), wires and portal stubs arriving only
 * AFTER their endpoints — the wires drawing themselves via a dash-offset sweep.
 * Under prefers-reduced-motion — the check comes FIRST, before any stepper is
 * armed — everything simply appears. */
function reveal(i, planId, freshKeys) {
  if (reduceMotion()) {
    for (const k of freshKeys) {
      SEEN.add(planId + ":" + k);
      const t = i.nodes.get(k);
      if (t) t.g.classList.remove("gr-hidden");
    }
    i.portals.forEach((p) => p.g.classList.remove("gr-hidden"));
    rebuildWires(i);
    return;
  }
  for (const k of freshKeys)
    if (!i.revealQueue.some((q) => q[1] === k)) i.revealQueue.push([planId, k]);
  if (i.revealTimer) return;            // the stepper is already walking
  const step = () => {
    i.revealTimer = null;
    const q = i.revealQueue.shift();
    if (!q) {                            // every card is on stage → the wires' turn
      i.portals.forEach((p) => p.g.classList.remove("gr-hidden"));
      i._staged = true;                  // rebuildWires arms the dash-draw once
      rebuildWires(i);
      return;
    }
    SEEN.add(q[0] + ":" + q[1]);
    const t = i.nodes.get(q[1]);
    if (t) {
      void t.g.getBoundingClientRect();          // commit the hidden state first
      t.g.classList.remove("gr-hidden");         // → CSS transition plays
    }
    i.revealTimer = setTimeout(step, 60);
  };
  i.revealTimer = setTimeout(step, 40);
}

/** Where a wire meets a card: the boundary point toward the other node, pushed so
 * render.js's own end-cut lands the arrowhead just OUTSIDE the card instead of
 * underneath it (cards are far wider than the agent circles the cut was sized for). */
function anchor(from, to, size) {
  const dx = to.x - from.x, dy = to.y - from.y, len = Math.hypot(dx, dy) || 1;
  const ux = dx / len, uy = dy / len;
  const t = Math.min(size.w / 2 / Math.max(Math.abs(ux), 1e-6),
                     size.h / 2 / Math.max(Math.abs(uy), 1e-6));
  const d = Math.max(0, Math.min(t + 6, len / 2 - 4) - WIRE_GAP);
  return { x: from.x + ux * d, y: from.y + uy * d };
}

// ---- portals: where a wire leaves the room --------------------------------------
/** One foreign group, one direction → one stub. The pk encodes both. */
function portalPk(i, foreignKey, out) {
  const n = i.byKey.get(String(foreignKey));
  if (!n) return null;
  const gkey = String(n.parent_key || "");
  const drill = gkey && i.byKey.has(gkey) ? gkey : "";
  const holder = drill ? i.byKey.get(drill) : null;
  return { pk: (out ? ">" : "<") + (drill || "~top"), drill,
           label: holder ? (holder.title || drill) : (n.title || String(n.key)) };
}

/** Cross-group child edges do not vanish at a level — they land on small "portal"
 * stubs at the frame's edge, labeled with the foreign group's name; pressing a
 * stub drills straight to that group (or back to the top for aim/conclusion).
 * Incoming traffic stacks on the left of the subgraph, outgoing on the right. */
function buildPortals(i, hidden) {
  i.portals.forEach((p) => p.g.remove());
  i.portals.clear();
  if (!i.level || !i.data) return;
  const wanted = new Map();
  for (const e of i.data.edges || []) {
    const [s, d] = edgeEnds(e);
    const sIn = i.nodes.has(s), dIn = i.nodes.has(d);
    if (sIn === dIn) continue;                     // in-level (drawn) or elsewhere (not ours)
    const p = portalPk(i, sIn ? d : s, sIn);
    if (!p) continue;
    if (!wanted.has(p.pk)) wanted.set(p.pk, { ...p, out: p.pk[0] === ">", n: 0 });
    wanted.get(p.pk).n += 1;
  }
  if (!wanted.size) return;
  const bb = sceneBounds(i);
  if (!isFinite(bb.minX)) return;
  const midY = (bb.minY + bb.maxY) / 2;
  const ins = [...wanted.values()].filter((w) => !w.out);
  const outs = [...wanted.values()].filter((w) => w.out);
  const place = (w, idx, total, x) => {
    const y = midY + (idx - (total - 1) / 2) * 66;
    const name = String(w.label).length > 15 ? String(w.label).slice(0, 14) + "…" : String(w.label);
    const g = svgEl("g", {
      class: "gr-portal" + (hidden ? " gr-hidden" : ""),
      "data-drill": w.drill, transform: `translate(${x} ${y})`,
    }, [
      svgEl("rect", { class: "gr-portal-pill", x: -PORTAL.w / 2, y: -PORTAL.h / 2,
        width: PORTAL.w, height: PORTAL.h, rx: PORTAL.h / 2, "pointer-events": "all" }),
      svgEl("text", { class: "gr-portal-t", x: 0, y: 1, "text-anchor": "middle",
        "dominant-baseline": "central", "pointer-events": "none",
        text: (w.out ? "→ " : "← ") + name }),
    ]);
    i.world.el.gTokens.appendChild(g);
    i.portals.set(w.pk, { g, x, y, out: w.out, drill: w.drill });
  };
  ins.forEach((w, idx) => place(w, idx, ins.length, bb.minX - 170));
  outs.forEach((w, idx) => place(w, idx, outs.length, bb.maxX + 170));
}

function rebuildWires(i) {
  const gW = i.world.el.gWires;
  gW.innerHTML = "";
  i.edges = [];
  const staged = i._staged;              // one dash-draw sweep, armed by reveal()
  i._staged = false;
  let di = 0;
  ((i.data && i.data.edges) || []).forEach((e, idx) => {
    const [s, d] = edgeEnds(e);
    const A = i.nodes.get(s), B = i.nodes.get(d);
    let pa = null, pb = null;
    if (!A && !B) return;                           // an edge of some other level
    if (!A || !B) {
      if (!i.level) return;                         // top level draws only its own tier
      const p = portalPk(i, A ? d : s, !!A);
      const stub = p && i.portals.get(p.pk);
      if (!stub || stub.g.classList.contains("gr-hidden")) return;
      if (A) pb = stub; else pa = stub;
    }
    if ((A && A.g.classList.contains("gr-hidden")) ||
        (B && B.g.classList.contains("gr-hidden"))) return;
    const An = A ? { x: A.x, y: A.y } : { x: pa.x, y: pa.y };
    const Bn = B ? { x: B.x, y: B.y } : { x: pb.x, y: pb.y };
    const a1 = anchor(An, Bn, A ? nodeSize(A.data) : PORTAL);
    const b1 = anchor(Bn, An, B ? nodeSize(B.data) : PORTAL);
    const g = wireNode({ tid: idx, a: s, b: d, dir: "a2b", closed: true,
                         ax: a1.x, ay: a1.y, bx: b1.x, by: b1.y });
    g.classList.add("gr-wire", "gr-wire-" + (e.edge_type || "depends"));
    if (pa || pb) g.classList.add("gr-wire-portal");
    if (staged) {
      g.classList.add("gr-draw");
      g.style.setProperty("--gr-dd", Math.min(di * 45, 500) + "ms");
    }
    gW.appendChild(g);
    i.edges.push({ a: s, b: d, g, d: e, pa, pb });
    di += 1;
  });
  if (staged) {                          // the sweep is one-shot; hand back to gr-flow
    clearTimeout(i.drawTimer);
    i.drawTimer = setTimeout(() => {
      if (inst === i) i.edges.forEach((x) => x.g.classList.remove("gr-draw"));
    }, 1200);
  }
  flowWires(i);
}

function updateWiresFor(i, key) {
  for (const edge of i.edges) {
    if (edge.a !== key && edge.b !== key) continue;
    const A = i.nodes.get(edge.a), B = i.nodes.get(edge.b);
    const An = A ? { x: A.x, y: A.y } : edge.pa, Bn = B ? { x: B.x, y: B.y } : edge.pb;
    if (!An || !Bn) continue;
    const a1 = anchor(An, Bn, A ? nodeSize(A.data) : PORTAL);
    const b1 = anchor(Bn, An, B ? nodeSize(B.data) : PORTAL);
    setWireEnds(edge.g, a1.x, a1.y, b1.x, b1.y);
  }
}

// ---- live activity --------------------------------------------------------------
/** A node the crew is touching right now glows and says what is being done to it. */
function setBusy(i, t, act) {
  t.g.querySelectorAll(".lw2-bubble-fo").forEach((b) => b.remove());
  t.g.classList.toggle("gr-busy", !!act);
  if (act) t.g.appendChild(speechBubble(act.task || act.what || "working", 0));
}
function showActivity(i) {
  i.nodes.forEach((t) => setBusy(i, t, ((t.data && t.data.activity) || [])[0]));
  flowWires(i);
}

/** Live edge flow: a wire whose either endpoint is busy carries a slow marching-
 * dash shimmer — the data is visibly moving. The animation itself is pure CSS;
 * this only flips the class. */
function flowWires(i) {
  for (const e of i.edges) {
    const A = i.nodes.get(e.a), B = i.nodes.get(e.b);
    const on = !!((A && A.g.classList.contains("gr-busy")) ||
                  (B && B.g.classList.contains("gr-busy")));
    e.g.classList.toggle("gr-flow", on);
  }
}

function onGraphEvent(i, e) {
  if (!e || !e.kind) return;
  if (e.kind === "graph_node_active" || e.kind === "graph_node_idle") {
    const t = e.node != null && i.nodes.get(String(e.node));
    if (t) {
      setBusy(i, t, e.kind === "graph_node_active"
        ? { task: e.task || e.what || "working" } : null);
      flowWires(i);
    }
    return;
  }
  scheduleRefetch(i);          // verify results, replans, crew events → repaint soon
}

// ---- breadcrumb -----------------------------------------------------------------
/** Persistent, top-left: `aim` at the architecture level, `aim ▸ Group` inside a
 * group — the aim crumb is the way back up. */
function renderCrumb(i) {
  if (!i.crumb) return;
  const aim = (i.data && (i.data.nodes || []).find((n) => n.node_type === "aim"));
  const root = esc((aim && aim.title) || "aim");
  if (!i.level) {
    i.crumb.innerHTML = `<span class="gr-crumb-here">🎯 ${root}</span>`;
    return;
  }
  const g = i.byKey.get(i.level);
  i.crumb.innerHTML =
    `<button class="gr-crumb-up" title="Back to the architecture (Esc)">🎯 ${root}</button>` +
    `<span class="gr-crumb-sep">▸</span>` +
    `<span class="gr-crumb-here">${esc((g && g.title) || i.level)}</span>`;
  const b = i.crumb.querySelector(".gr-crumb-up");
  if (b) b.onclick = () => drillTo(i, "");
}

// ---- minimap --------------------------------------------------------------------
// Bottom-right, click-to-jump. NOT a second render of the scene: plain rects drawn
// from the same node records, plus one rectangle for the viewport.
const MINI_W = 168, MINI_H = 112;
function renderMini(i) {
  if (!i.mini) return;
  if (!i.miniSvg) {
    i.miniSvg = svgEl("svg", { class: "gr-mini-svg", width: MINI_W, height: MINI_H,
                               viewBox: `0 0 ${MINI_W} ${MINI_H}` });
    i.miniRects = svgEl("g");
    i.miniView = svgEl("rect", { class: "gr-mini-view", fill: "none", rx: 2 });
    i.miniSvg.append(i.miniRects, i.miniView);
    i.mini.innerHTML = "";
    i.mini.appendChild(i.miniSvg);
    i.mini.onpointerdown = (e) => {          // click-to-jump: centre the camera there
      const m = i.miniMap;
      if (!m) return;
      const r = i.mini.getBoundingClientRect();
      const wx = (e.clientX - r.left - m.ox) / m.s, wy = (e.clientY - r.top - m.oy) / m.s;
      const v = i.world.getView(), hr = i.hostRect;
      flyTo(i, { k: v.k, x: hr.width / 2 - wx * v.k, y: hr.height / 2 - wy * v.k },
            320, false, () => saveView(i));
    };
  }
  const bb = sceneBounds(i);
  if (!isFinite(bb.minX)) { i.miniMap = null; i.miniRects.innerHTML = ""; return; }
  const s = Math.min((MINI_W - 14) / Math.max(1, bb.maxX - bb.minX),
                     (MINI_H - 14) / Math.max(1, bb.maxY - bb.minY));
  i.miniMap = {
    s,
    ox: (MINI_W - (bb.maxX - bb.minX) * s) / 2 - bb.minX * s,
    oy: (MINI_H - (bb.maxY - bb.minY) * s) / 2 - bb.minY * s,
  };
  i.miniRects.innerHTML = "";
  i.nodes.forEach((t) => {
    const { w, h } = nodeSize(t.data);
    i.miniRects.appendChild(svgEl("rect", {
      class: "gr-mini-n gr-mini-" + (t.data.node_type || "code"),
      x: t.x * s + i.miniMap.ox - (w * s) / 2, y: t.y * s + i.miniMap.oy - (h * s) / 2,
      width: Math.max(3, w * s), height: Math.max(2.5, h * s), rx: 1.5,
    }));
  });
  miniViewport(i);
}

function miniViewport(i) {
  const m = i.miniMap;
  if (!m || !i.miniView) return;
  const v = i.world.getView(), r = i.hostRect;
  const x0 = (0 - v.x) / v.k, y0 = (0 - v.y) / v.k;
  const ww = r.width / v.k, wh = r.height / v.k;
  i.miniView.setAttribute("x", x0 * m.s + m.ox);
  i.miniView.setAttribute("y", y0 * m.s + m.oy);
  i.miniView.setAttribute("width", Math.max(4, ww * m.s));
  i.miniView.setAttribute("height", Math.max(4, wh * m.s));
}

// ---- edge tooltip ---------------------------------------------------------------
/** Hovering a wire floats its CONTRACT next to the cursor: the two parties, the
 * edge type, the rule itself and the test that enforces it — all escaped, because
 * contracts are planner-authored JSON. */
function showTip(i, wireG, e) {
  if (!i.tip || i.transition) return;
  const ed = ((i.data && i.data.edges) || [])[+wireG.getAttribute("data-tid")];
  if (!ed) return;
  const [s, d] = edgeEnds(ed);
  const rule = ed.contract && Object.keys(ed.contract).length
    ? `<code>${esc(JSON.stringify(ed.contract))}</code>`
    : `<span class="dim">no contract recorded</span>`;
  i.tip.innerHTML =
    `<b>${esc(s)} → ${esc(d)}</b><span class="gr-edge-tip-type">${esc(ed.edge_type || "depends")}</span>` +
    rule +
    (ed.contract_test ? `<div class="dim">${esc(ed.contract_test)}</div>` : "");
  i.tip.hidden = false;
  i.tipOn = true;
  i.stageRect = i.tip.parentElement.getBoundingClientRect();
  moveTip(i, e);
}
function moveTip(i, e) {
  if (!i.tipOn || !i.stageRect) return;
  const x = clamp(e.clientX - i.stageRect.left + 16, 8, Math.max(8, i.stageRect.width - 360));
  const y = clamp(e.clientY - i.stageRect.top + 14, 8, Math.max(8, i.stageRect.height - 140));
  i.tip.style.transform = `translate(${x}px, ${y}px)`;
}
function hideTip(i) {
  if (!i || !i.tip) return;
  i.tipOn = false;
  i.tip.hidden = true;
}

// ---- selection ------------------------------------------------------------------
function clearSel(i) {
  i.sel.forEach((k) => { const t = i.nodes.get(k); if (t) t.g.classList.remove("gr-sel"); });
  i.sel.clear();
}
function addSel(i, k) { const t = i.nodes.get(k); if (t) { t.g.classList.add("gr-sel"); i.sel.add(k); } }
function setSel(i, k) { clearSel(i); addSel(i, k); }
function reselect(i) { const keep = [...i.sel].filter((k) => i.nodes.has(k)); i.sel.clear(); keep.forEach((k) => addSel(i, k)); }

/** The selection settled: one node → its light card (groups get their own, with
 * the union-verify); the conclusion → its card; nothing → the how-to. The full
 * inspector only ever opens on a double-click — and on a group, a double-click
 * IS the microscope. */
function selectionChanged(i) {
  i.inspectKey = null;
  if (i.sel.size === 1) {
    const key = [...i.sel][0];
    const t = i.nodes.get(key);
    if (t && t.data.node_type === "conclusion") asideConclusion(i);
    else if (t && t.data.node_type === "group") asideGroup(i, key);
    else asideLight(i, key);
  } else if (i.sel.size > 1) {
    i.aside.innerHTML = `<p class="dim">${i.sel.size} modules selected — drag to arrange them together.</p>`;
  } else {
    asideDefault(i);
  }
}

// ---- the aside (this screen's own inspector/action host) ------------------------
function asideDefault(i) {
  i.aside.innerHTML = `<div class="gr-tip">
    <p class="dim">Click a module for its card; <b>double-click</b> opens the inspector.
    Double-click a <b>group</b> (or its ⊕) to dive inside; <kbd>Esc</kbd> or the
    breadcrumb climbs back out. Drag to arrange — positions stick. Scroll to zoom,
    hold space to pan, <kbd>f</kbd> frames everything, double-click empty space to
    re-frame. Hover a wire to read its contract.</p></div>`;
}

function testsLine(tests) {
  const t = tests || {};
  if (!t.total) return `<span class="dim">no tests mapped</span>`;
  let s = `${t.passing}/${t.total} passing`;
  if (t.failing) s += ` · <b class="gr-failtext">${t.failing} failing</b>
    <span class="dim">(advisory — a red ring informs, it never blocks)</span>`;
  return s;
}

function asideLight(i, key) {
  const t = i.nodes.get(key);
  if (!t) { asideDefault(i); return; }
  const n = t.data;
  const act = (n.activity || [])[0];
  const agent = n.agent ? String(n.agent.agent_id ?? n.agent.home_id ?? "") : "";
  i.aside.innerHTML = `
    <div class="gr-light">
      <div class="gr-light-head">
        <span class="gr-light-glyph">${GLYPH[n.node_type] || GLYPH.code}</span>
        <div><b>${esc(n.title || n.key)}</b><div class="dim">${esc(n.node_type)} · ${esc(n.key)}</div></div>
      </div>
      ${(n.tags || []).length ? `<div class="gr-tags">${n.tags.map((g) => `<span class="gr-tag">${esc(g)}</span>`).join("")}</div>` : ""}
      <p class="gr-testline">${testsLine(n.tests)}</p>
      ${agent ? `<p class="dim">agent: <b>${esc(agent)}</b></p>` : ""}
      ${act ? `<p class="gr-actline">● ${esc(act.task || act.what || "working")}</p>` : ""}
      <p class="dim gr-hint">double-click the node for spec, tests, trace &amp; config</p>
    </div>`;
}

/** A group's light card: rolled-up numbers, the dive, and "Verify group" — the
 * backend runs the union of every child's affected set. */
function asideGroup(i, key) {
  const t = i.nodes.get(key);
  if (!t) { asideDefault(i); return; }
  const n = t.data;
  const kids = Number(n.children) || 0;
  const act = (n.activity || [])[0];
  i.aside.innerHTML = `
    <div class="gr-light">
      <div class="gr-light-head">
        <span class="gr-light-glyph">${GLYPH.group}</span>
        <div><b>${esc(n.title || n.key)}</b><div class="dim">group · ${esc(n.key)} · ${kids} module${kids === 1 ? "" : "s"}</div></div>
      </div>
      ${(n.tags || []).length ? `<div class="gr-tags">${n.tags.map((g) => `<span class="gr-tag">${esc(g)}</span>`).join("")}</div>` : ""}
      <p class="gr-testline">${testsLine(n.tests)}</p>
      ${act ? `<p class="gr-actline">● ${esc(act.task || act.what || "working")}</p>` : ""}
      <div class="gr-actions">
        <button class="primary" id="grGroupOpen">⊕ Open group</button>
        <button id="grGroupVerify">▶ Verify group</button>
        <span class="dim" id="grVerifyOut"></span>
      </div>
      <p class="dim gr-hint">double-click the card (or press Enter) to dive in</p>
    </div>`;
  const ob = i.aside.querySelector("#grGroupOpen");
  if (ob) ob.onclick = () => drillTo(i, key);
  const vb = i.aside.querySelector("#grGroupVerify");
  if (vb) vb.onclick = async () => {
    const out = i.aside.querySelector("#grVerifyOut");
    vb.disabled = true;
    if (out) out.textContent = "running the union of the children's tests…";
    try {
      const r = await i.src.verify(key);
      if (out) out.textContent = (r.ok ? "✓ " : "✕ ") + (r.headline || "");
    } catch (e) {
      if (out) out.textContent = (e && e.message) || String(e);
    }
    vb.disabled = false;
    if (inst === i) scheduleRefetch(i);
  };
}

function asideConclusion(i) {
  const c = (i.data && i.data.conclusion) || {};
  const rep = c.repair || {};
  i.aside.innerHTML = `
    <div class="gr-light gr-conclusion">
      <div class="gr-light-head"><span class="gr-light-glyph">🏁</span><div><b>Conclusion</b>
        <div class="dim">what all of it adds up to</div></div></div>
      <p>health: <b class="gr-health gr-health-${esc(c.health || "unknown")}">${esc(c.health || "unknown")}</b></p>
      <p class="dim">crew: ${esc(rep.phase || "idle")}${rep.sprint ? ` · sprint ${esc(rep.sprint)}` : ""}</p>
      <button class="rp-mini" id="grOpenHq">🏢 Open HQ</button>
    </div>`;
  const b = i.aside.querySelector("#grOpenHq");
  if (b) b.onclick = () => { location.hash = "#/hq"; };
}

async function openInspector(i, key) {
  const t = i.nodes.get(key);
  if (!t) return;
  setSel(i, key);
  i.inspectKey = key;
  i.aside.innerHTML = `<p class="dim">reading ${esc(key)}…</p>`;
  let d;
  try { d = await i.src.inspect(key); }
  catch (e) {
    if (inst === i && i.inspectKey === key)
      i.aside.innerHTML = `<p class="err">${esc((e && e.message) || String(e))}</p>`;
    return;
  }
  if (inst !== i || i.inspectKey !== key) return;    // they moved on mid-fetch
  renderInspector(i, key, d);
}

const MODELS = ["", "claude-sonnet-5", "claude-opus-4-8", "claude-fable-5", "claude-haiku-4-5"];

function renderInspector(i, key, d) {
  const n = d.node || {};
  const cfg = d.config || {};
  const testRows = (d.tests || []).map((tt) => `
    <div class="gr-test gr-test-${esc(tt.status)}">
      <span class="gr-test-dot"></span><code>${esc(tt.path)}</code>
      <span class="dim">${esc(tt.kind)} · ${esc(tt.status)}</span>
      ${tt.last_result ? `<div class="gr-test-last dim">${esc(String(tt.last_result).slice(0, 160))}</div>` : ""}
    </div>`).join("") || `<p class="dim">no tests mapped to this module</p>`;
  const edgeRows = (d.edges || []).map((e) => {
    const s = String(e.src ?? e.src_key ?? ""), dd = String(e.dst ?? e.dst_key ?? "");
    const contract = e.contract && Object.keys(e.contract).length
      ? `<div class="gr-contract"><code>${esc(JSON.stringify(e.contract).slice(0, 140))}</code>${
          e.contract_test ? `<span class="dim"> · ${esc(e.contract_test)}</span>` : ""}</div>` : "";
    return `<div class="gr-edge"><b>${esc(s)}</b> → <b>${esc(dd)}</b>
      <span class="dim">${esc(e.edge_type || "depends")}</span>${contract}</div>`;
  }).join("") || `<p class="dim">no edges touch this module</p>`;
  const traceRows = (d.trace || []).slice(-8).reverse().map((r) => `
    <div class="gr-run"><span class="dim">${esc(r.kind || "run")} · ${esc(r.status || "")}</span>
      ${r.detail ? ` ${esc(String(r.detail).slice(0, 120))}` : ""}</div>`).join("")
    || `<p class="dim">nothing has happened to this module yet</p>`;
  i.aside.innerHTML = `
    <div class="gr-inspect">
      <div class="gr-light-head">
        <span class="gr-light-glyph">${GLYPH[n.node_type] || GLYPH.code}</span>
        <div><b>${esc(n.title || key)}</b><div class="dim">${esc(n.node_type || "")} · ${esc(key)}</div></div>
        <button class="rp-link gr-close" id="grInsClose">close</button>
      </div>
      ${n.spec ? `<p class="gr-spec">${esc(n.spec)}</p>` : ""}
      ${(n.tags || []).length ? `<div class="gr-tags">${n.tags.map((g) => `<span class="gr-tag">${esc(g)}</span>`).join("")}</div>` : ""}
      <div class="gr-sec"><div class="gr-sec-h">Tests <span class="dim">advisory — red informs, never blocks</span></div>${testRows}</div>
      <div class="gr-sec"><div class="gr-sec-h">Edges &amp; contracts</div>${edgeRows}</div>
      <div class="gr-sec"><div class="gr-sec-h">Trace</div>${traceRows}</div>
      <div class="gr-sec"><div class="gr-sec-h">Steering</div>
        <label class="gr-cfg">model
          <select id="grCfgModel">${MODELS.map((mm) =>
            `<option value="${esc(mm)}"${mm === (cfg.model || "") ? " selected" : ""}>${mm ? esc(mm) : "default"}</option>`).join("")}</select>
        </label>
        <label class="gr-cfg">autonomy
          <select id="grCfgAutonomy">${["", "supervised", "autonomous"].map((a) =>
            `<option value="${a}"${a === (cfg.autonomy || "") ? " selected" : ""}>${a || "default"}</option>`).join("")}</select>
        </label>
      </div>
      <div class="gr-actions"><button class="primary" id="grVerify">▶ Verify now</button>
        <span class="dim" id="grVerifyOut"></span></div>
    </div>`;
  wireInspector(i, key);
}

function wireInspector(i, key) {
  const closeBtn = i.aside.querySelector("#grInsClose");
  if (closeBtn) closeBtn.onclick = () => { i.inspectKey = null; clearSel(i); asideDefault(i); };
  const send = async (cfg) => {
    try { await i.src.setConfig(key, cfg); W.toast && W.toast("Saved"); }
    catch (e) { W.toast && W.toast((e && e.message) || String(e)); }
  };
  const mSel = i.aside.querySelector("#grCfgModel");
  if (mSel) mSel.onchange = () => send({ model: mSel.value });
  const aSel = i.aside.querySelector("#grCfgAutonomy");
  if (aSel) aSel.onchange = () => send({ autonomy: aSel.value });
  const vBtn = i.aside.querySelector("#grVerify");
  if (vBtn) vBtn.onclick = async () => {
    const out = i.aside.querySelector("#grVerifyOut");
    vBtn.disabled = true;
    if (out) out.textContent = "running the affected tests…";
    try {
      const r = await i.src.verify(key);
      if (out) out.textContent = (r.ok ? "✓ " : "✕ ") + (r.headline || "");
    } catch (e) {
      if (out) out.textContent = (e && e.message) || String(e);
    }
    vBtn.disabled = false;
    if (inst !== i) return;
    await refetch(i);                          // rings repaint from the recorded truth
    if (i.inspectKey === key) openInspector(i, key);
  };
}

// ---- events / gestures (the canvas2 core, minus seats/portal/threads) -----------
function wireEvents(i) {
  const { svg } = i.world.el;
  const onDown = (e) => pointerDown(i, e);
  const onMove = (e) => { if (i.tipOn) moveTip(i, e); pointerMove(i, e); };
  const onUp = (e) => pointerUp(i, e);
  const onWheel = (e) => {
    e.preventDefault();
    const k0 = i.world.scale;
    // proportional zoom: trackpads glide, wheels step — both feel continuous
    const f = Math.exp((e.deltaY < 0 ? 1 : -1) * Math.min(60, Math.abs(e.deltaY)) * 0.004);
    i.world.zoomAt(e.clientX, e.clientY, f);
    miniViewport(i);
    saveView(i);
    // the zoom-out gesture past the threshold IS the way back up a level
    if (i.level && !i.transition && e.deltaY > 0 && k0 > 0.32 && i.world.scale <= 0.32)
      drillTo(i, "");
  };
  const onDbl = (e) => e.preventDefault();     // real dblclick handled via pressToken
  const onKey = (e) => keyDown(i, e);
  const onKeyUp = (e) => { if (e.code === "Space") i.space = false; };
  const onOver = (e) => {
    const wg = e.target.closest && e.target.closest(".gr-wire");
    if (wg && !i.gesture) showTip(i, wg, e);
  };
  const onOut = (e) => {
    if (e.target.closest && e.target.closest(".gr-wire")) hideTip(i);
  };

  svg.addEventListener("pointerdown", onDown);
  svg.addEventListener("pointermove", onMove);
  svg.addEventListener("pointerup", onUp);
  svg.addEventListener("pointercancel", onUp);
  svg.addEventListener("lostpointercapture", onUp);
  svg.addEventListener("wheel", onWheel, { passive: false });
  svg.addEventListener("dblclick", onDbl);
  svg.addEventListener("pointerover", onOver);
  svg.addEventListener("pointerout", onOut);
  // Keys belong to THIS screen (it carries tabindex="-1" and is focused on open) —
  // a listener on the whole document would fire under every other screen too.
  i.screen.addEventListener("keydown", onKey);
  i.screen.addEventListener("keyup", onKeyUp);

  i._teardown = () => {
    svg.removeEventListener("pointerdown", onDown); svg.removeEventListener("pointermove", onMove);
    svg.removeEventListener("pointerup", onUp); svg.removeEventListener("pointercancel", onUp);
    svg.removeEventListener("lostpointercapture", onUp); svg.removeEventListener("wheel", onWheel);
    svg.removeEventListener("dblclick", onDbl);
    svg.removeEventListener("pointerover", onOver); svg.removeEventListener("pointerout", onOut);
    i.screen.removeEventListener("keydown", onKey); i.screen.removeEventListener("keyup", onKeyUp);
  };
}

function pointerDown(i, e) {
  if (e.button === 2) return;
  i.screen.focus({ preventScroll: true });     // clicks keep the keys aimed here
  hideTip(i);
  const svg = i.world.el.svg;
  svg.setPointerCapture(e.pointerId);          // route ALL further events here — no lost moves
  if (e.button === 1 || i.space) {
    i.gesture = { type: "pan", x: e.clientX, y: e.clientY };
    i.host.classList.add("lw2-panning");
    return;
  }
  if (e.button !== 0) return;
  // ⊕ affordances and portal stubs resolve BEFORE the node card underneath them
  const drillEl = e.target.closest && e.target.closest("[data-drill]");
  if (drillEl) {
    i.gesture = { type: "drill", key: drillEl.getAttribute("data-drill") || "",
                  x: e.clientX, y: e.clientY };
    return;
  }
  const tokEl = e.target.closest && e.target.closest(".gr-node");
  if (tokEl) { pressToken(i, e, tokEl); return; }
  // empty floor → clear + marquee
  if (!e.shiftKey) { clearSel(i); i.inspectKey = null; selectionChanged(i); }
  beginMarquee(i, e);
}

function pressToken(i, e, tokEl) {
  const key = tokEl.getAttribute("data-key");
  // Whether this press MIGHT complete a double-click is decided here, but it only
  // FIRES on pointerup-without-moving (a click-then-drag is a drag). With pointer
  // capture the browser's synthesized dblclick is unreliable, so taps are tracked here.
  const maybeDouble = !!(i._lastClick && i._lastClick.key === key && (nowMs() - i._lastClick.t) < 350);
  const already = i.sel.has(key), grouped = i.sel.size >= 2 && already && !e.shiftKey;
  if (e.shiftKey) { if (already) { i.sel.delete(key); const t = i.nodes.get(key); if (t) t.g.classList.remove("gr-sel"); } else addSel(i, key); }
  else if (!already) setSel(i, key);
  const members = (grouped || i.sel.size >= 2) ? [...i.sel] : [key];
  i.gesture = { type: "arm", key, maybeDouble, start: { x: e.clientX, y: e.clientY },
    members: members.map((mk) => { const t = i.nodes.get(mk); return { key: mk, x0: t.x, y0: t.y }; }),
    moved: false, world0: i.world.toWorld(e.clientX, e.clientY) };
}

function beginMarquee(i, e) {
  const w = i.world.toWorld(e.clientX, e.clientY);
  const rect = svgEl("rect", { class: "lw2-marquee", x: w.x, y: w.y, width: 0, height: 0 });
  i.world.el.gOverlay.appendChild(rect);
  i.gesture = { type: "marquee", x0: w.x, y0: w.y, rect, add: e.shiftKey };
}

function pointerMove(i, e) {
  const g = i.gesture;
  if (!g) return;
  if (g.type === "pan") {
    i.world.panBy(e.clientX - g.x, e.clientY - g.y);
    g.x = e.clientX; g.y = e.clientY;
    miniViewport(i);
    return;
  }
  const w = i.world.toWorld(e.clientX, e.clientY);
  if (g.type === "marquee") {
    g.rect.setAttribute("x", Math.min(w.x, g.x0)); g.rect.setAttribute("y", Math.min(w.y, g.y0));
    g.rect.setAttribute("width", Math.abs(w.x - g.x0)); g.rect.setAttribute("height", Math.abs(w.y - g.y0));
    return;
  }
  if (g.type === "arm") {
    if (!g.moved && Math.hypot(e.clientX - g.start.x, e.clientY - g.start.y) <= DRAG_THRESH) return;
    if (!g.moved) { g.moved = true; i.host.classList.add("lw2-dragging"); }
    const dx = w.x - g.world0.x, dy = w.y - g.world0.y;
    g.members.forEach((m) => moveToken(i, m.key, m.x0 + dx, m.y0 + dy));
  }
}

function moveToken(i, key, x, y) {
  const t = i.nodes.get(key);
  if (!t) return;
  t.x = x; t.y = y;
  setPos(t.g, x, y);
  updateWiresFor(i, key);
}

async function pointerUp(i, e) {
  const g = i.gesture;
  i.gesture = null;
  try { i.world.el.svg.releasePointerCapture(e.pointerId); } catch (er) { /* */ }
  i.host.classList.remove("lw2-panning", "lw2-dragging");
  if (!g) return;
  if (g.type === "pan") { saveView(i); return; }
  if (g.type === "drill") {                      // ⊕ or a portal stub, released in place
    if (Math.hypot(e.clientX - g.x, e.clientY - g.y) <= DRAG_THRESH) drillTo(i, g.key);
    return;
  }
  if (g.type === "marquee") { endMarquee(i, g); return; }
  if (g.type === "arm") {
    if (!g.moved) {                              // released without moving → a click
      if (g.maybeDouble) {
        i._lastClick = null;
        const t = i.nodes.get(g.key);
        if (t && t.data.node_type === "group") drillTo(i, g.key);   // the microscope
        else openInspector(i, g.key);
      } else {
        i._lastClick = { key: g.key, t: nowMs() };
        selectionChanged(i);
      }
      return;
    }
    i._lastClick = null;                          // a drag is not part of a double-click
    await commitDrag(i, g);
  }
}

function endMarquee(i, g) {
  const bx = +g.rect.getAttribute("x"), by = +g.rect.getAttribute("y");
  const bw = +g.rect.getAttribute("width"), bh = +g.rect.getAttribute("height");
  g.rect.remove();
  if (bw > 4 || bh > 4) {
    if (!g.add) clearSel(i);
    i.nodes.forEach((t, key) => { if (t.x >= bx && t.x <= bx + bw && t.y >= by && t.y <= by + bh) addSel(i, key); });
  } else if (!g.add) {
    // a plain tap on empty floor — two of them inside 350ms re-frame the level
    const t = nowMs();
    if (i._floorTap && t - i._floorTap < 350) {
      i._floorTap = 0;
      flyTo(i, viewFor(i, sceneBounds(i), i.level ? 190 : 120), 320, false, () => saveView(i));
    } else {
      i._floorTap = t;
    }
  }
  selectionChanged(i);
}

async function commitDrag(i, g) {
  const positions = {};
  for (const m of g.members) {
    const t = i.nodes.get(m.key);
    if (t) positions[m.key] = [Math.round(t.x), Math.round(t.y)];
  }
  try { await i.src.saveLayout(positions); }
  catch (e) { W.toast && W.toast("Could not save the layout: " + ((e && e.message) || e)); }
  saveView(i);
  renderMini(i);
}

function keyDown(i, e) {
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (e.target && e.target.isContentEditable)) return;
  if (e.code === "Space") { i.space = true; e.preventDefault(); return; }
  if (e.key === "Escape") {
    if (i.inspectKey || i.sel.size) { i.inspectKey = null; clearSel(i); selectionChanged(i); }
    else if (i.level) drillTo(i, "");            // Esc climbs the breadcrumb
    return;
  }
  if (e.key === "f" || e.key === "F") {
    flyTo(i, viewFor(i, sceneBounds(i), i.level ? 190 : 120), 300, false, () => saveView(i));
    return;
  }
  if (e.key === "Enter" && i.sel.size === 1) {
    const key = [...i.sel][0];
    const t = i.nodes.get(key);
    if (t && t.data.node_type === "group") drillTo(i, key);
    else openInspector(i, key);
  }
}

function sceneBounds(i) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  i.nodes.forEach((t) => {
    const { w, h } = nodeSize(t.data);
    minX = Math.min(minX, t.x - w / 2 - 20); minY = Math.min(minY, t.y - h / 2 - 20);
    maxX = Math.max(maxX, t.x + w / 2 + 20); maxY = Math.max(maxY, t.y + h / 2 + 30);
  });
  return { minX, minY, maxX, maxY };
}

// Views persist PER LEVEL — coming back to the architecture finds it framed the
// way it was left, and each group remembers its own vantage.
function viewStoreKey(i) { return i.viewKey + (i.level ? ":" + i.level : ""); }
function saveView(i) { try { localStorage.setItem(viewStoreKey(i), JSON.stringify(i.world.getView())); } catch (e) { /* */ } }
function restoreView(i) {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(viewStoreKey(i)) || "null"); } catch (e) { /* */ }
  if (saved) i.world.setView(saved); else i.world.fit(sceneBounds(i));
  miniViewport(i);
}

// The classic scripts' door in: core.js's openModuleGraph calls this, passing the
// drill sub-path parsed from the hash.
W.ModuleGraph = { open, close };
