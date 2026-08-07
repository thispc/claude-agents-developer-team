// Module graph — THE ATLAS. Rooms and doors, not a camera.
//
// The free-camera canvas was rejected for a reason worth pinning: pan + zoom +
// screen-fixed portals made a small graph HARDER to read — cards adrift in dead
// space, arrows into nothing. The Atlas replaces all of it with game navigation:
//
//   · ONE ROOM on screen at a time — the top level (aim → chambers → the
//     Artifact) or the inside of one group. No pan, no zoom, no drag, no
//     camera: a CSS grid of dependency COLUMNS (topo order over the room's own
//     edges) that always fits the viewport, scrolling vertically only when it
//     truly must.
//   · Two card kinds, unmistakable at a glance: CHAMBERS (groups) are doorways
//     — layered depth, child count, "Enter ▸"; CAPILLARIES (leaves) are where
//     agents actually act — the specialist front and center. Click a chamber =
//     travel; click a capillary = the full side panel.
//   · Wiring that cannot lie: an SVG underlay draws ONLY real in-room edges as
//     orthogonal elbows between card anchors read from the DOM after layout.
//     Every cross-room dependency is a DOOR CHIP on the card itself ("→ Data ›
//     db"), derived exclusively from actual payload edges — clicking one
//     travels there and flash-highlights the target card.
//   · Keyboard first-class: arrows move focus, Enter opens/enters, Esc climbs
//     out, M toggles the full-screen Atlas map. All state lives in the hash
//     (#/graph, #/graph/<group>) so back/forward/pasted links all work.
//
// Engine discipline (kept from every canvas lesson, minus the canvas):
//   · This module owns exactly ONE instance of itself, created by open() and
//     torn down by close(); it registers no globals another screen reads.
//   · Key listeners live on #graphScreen (tabindex="-1"), never the document.
//   · The inspector and every action button render inside this screen's aside.
//   · Reduced motion is decided here with matchMedia; every transition is CSS
//     transform/opacity and stands down under prefers-reduced-motion.
//   · No JS animation loop at all — nothing here animates outside CSS.

import { buildCard, updateCard, buildArtifactCard, GLYPH } from "./nodes.js";
import { columns, topoOrder } from "./layout.js";

const W = window;
const THEME_KEY = "gr:theme";          // "blueprint" (this screen's default) | "paper"
const STAGGER_MS = 60;                 // card entrance stagger
const SWAP_MS = 200;                   // the outgoing room yields the stage
const SVG_NS = "http://www.w3.org/2000/svg";
const reduceMotion = () =>
  W.matchMedia && W.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Which plan_id:key pairs have already had their entrance. Module state, so a
// new plan version replays the reveal but a mere poll repaint does not; entering
// a room deliberately forgets its keys so every visit replays the staging.
const SEEN = new Set();

let inst = null;                       // this screen's one live instance

// ---- the source seam ------------------------------------------------------------
// projSrc's discipline: renderers never branch on where the truth comes from.
// V1 ships the devteam source; V2 adds a project source with the same verbs.
// (No layout-save verb any more: the Atlas has no draggable positions to save.)
const DEVTEAM_GRAPH_SRC = {
  name: "self",
  fetch: () => W.api("/api/graph/self"),
  verify: (key) => W.api("/api/graph/self/verify", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node: key }) }),
  setConfig: (key, cfg) => W.api(`/api/graph/self/node/${encodeURIComponent(key)}/config`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg) }),
  inspect: (key) => W.api(`/api/graph/self/node/${encodeURIComponent(key)}`),
  replan: () => W.api("/api/graph/self/replan", { method: "POST" }),
  // The verb tier (every caller treats a refusal as information, never a crash):
  service: (key, action) => W.api(`/api/graph/self/node/${encodeURIComponent(key)}/service`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }) }),
  setAgent: (key, agentId) => W.api(`/api/graph/self/node/${encodeURIComponent(key)}/agent`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_id: agentId }) }),
  removeNode: (key) => W.api(`/api/graph/self/node/${encodeURIComponent(key)}/remove`, {
    method: "POST" }),
  replaceAspect: (key, aspect, note) => W.api(`/api/graph/self/node/${encodeURIComponent(key)}/replace`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ aspect, note }) }),
  team: () => W.api("/api/graph/self/team"),
  setTeam: (ref) => {                     // ref = "worldId:roomId", the option's value
    const [world_id, room_id] = String(ref).split(":").map(Number);
    return W.api("/api/graph/self/team", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ world_id, room_id }) });
  },
  cluster: (action) => W.api("/api/graph/self/cluster", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }) }),
};
const SOURCES = { self: DEVTEAM_GRAPH_SRC };

// ---- small helpers --------------------------------------------------------------
const esc = (s) => (W.escapeHtml ? W.escapeHtml(String(s ?? "")) : String(s ?? ""));

/** src/dst regardless of which serializer produced the edge. */
function edgeEnds(e) {
  return [String(e.src ?? e.src_key ?? ""), String(e.dst ?? e.dst_key ?? "")];
}

function svgMk(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}

// ---- lifecycle ------------------------------------------------------------------
/** Open the Atlas at a room. `sub` is the sub-path from the hash — "" for the
 * top room, a group key for inside that group. Called again while already open
 * (every #/graph/... hash change lands here via route()), it becomes
 * NAVIGATION: the existing instance transitions between rooms. */
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
  const wrap = document.getElementById("graphRoomWrap");
  const room = document.getElementById("graphRoom");
  const wires = document.getElementById("graphWires");
  const aside = document.getElementById("graphAside");
  if (!screen || !wrap || !room || !wires || !aside) return;
  inst = {
    src: SOURCES[sourceName] || DEVTEAM_GRAPH_SRC,
    srcName: sourceName || "self",
    screen, wrap, room, wires, aside,
    level,                     // null = the top room, else the open group's key
    cards: new Map(),          // key -> element (visible room only)
    wireEls: [],               // [{ a, b, el }] for the live-flow toggle
    roomKeys: new Set(),
    byKey: new Map(),          // every node in the payload, visible or not
    inspectKey: null,
    focusKey: null,
    flashKey: null,            // set by door-chip/atlas travel; flashes on arrival
    atlasOpen: false,
    data: null,
    transition: false,         // a room swap is in progress — repaints hold fire
    revealTimer: null, revealQueue: [],
    pollTimer: null, refetchTimer: null, swapTimer: null, drawTimer: null,
    flashTimer: null,
    crumb: document.getElementById("graphCrumb"),
    atlas: document.getElementById("graphAtlas"),
    goalChip: document.getElementById("graphGoal"),
  };
  applyTheme(inst, safeGet(THEME_KEY) || "blueprint");
  const tBtn = document.getElementById("graphTheme");
  if (tBtn) tBtn.onclick = () => {
    if (inst) applyTheme(inst,
      inst.screen.dataset.gtheme === "blueprint" ? "paper" : "blueprint");
  };
  const aBtn = document.getElementById("graphAtlasBtn");
  if (aBtn) aBtn.onclick = () => { if (inst) toggleAtlas(inst); };
  wireEvents(inst);
  asideDefault(inst);
  loadTeam(inst);
  const back = document.getElementById("graphBack");
  if (back) back.onclick = () => { location.hash = "#/hq"; };
  // Live updates: the classic scripts own the socket; connectWs re-broadcasts
  // graph/repair kinds as DOM CustomEvents, which a module CAN hear.
  inst._onGraphEvent = (ev) => { if (inst) onGraphEvent(inst, ev.detail || {}); };
  document.addEventListener("graph-event", inst._onGraphEvent);
  // ...and a 6s poll as the fallback, PAUSED while the panel or the Atlas map is
  // open (or a room swap is mid-flight) so a repaint never blanks the inspector
  // mid-sentence or repaints the stage under a transition.
  inst.pollTimer = setInterval(() => {
    if (!inst || inst.inspectKey || inst.transition || inst.atlasOpen) return;
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
  clearTimeout(inst.flashTimer);
  document.removeEventListener("graph-event", inst._onGraphEvent);
  try { inst._teardown && inst._teardown(); } catch (e) { /* */ }
  inst.room.innerHTML = "";
  inst.wires.innerHTML = "";
  if (inst.atlas) { inst.atlas.hidden = true; inst.atlas.innerHTML = ""; }
  if (inst.goalChip) { inst.goalChip.hidden = true; inst.goalChip.innerHTML = ""; }
  const legend = document.getElementById("graphLegend");
  if (legend) { legend.hidden = true; legend.innerHTML = ""; }
  document.querySelectorAll(".gr-dialog").forEach((d) => d.remove());
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
  if (i.transition) { scheduleRefetch(i); return; }   // never repaint mid-swap
  paint(i, data);
}

function scheduleRefetch(i) {
  if (i.refetchTimer) return;
  i.refetchTimer = setTimeout(() => {
    i.refetchTimer = null;
    if (!inst || inst.inspectKey || inst.transition || inst.atlasOpen) return;
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

// ---- rooms ----------------------------------------------------------------------
/** The nodes a room shows. Top room: aim + chambers (or the whole flat plan when
 * no groups were authored — the graph keeps working). Inside a group: exactly
 * its children. The CONCLUSION is never a room node here — at the top it is the
 * ARTIFACT, the always-last column card; in sub-rooms it is the header's goal
 * chip linking home. */
function roomNodes(data, level) {
  const ns = ((data && data.nodes) || []).filter((n) => n.node_type !== "conclusion");
  if (level) return ns.filter((n) => String(n.parent_key || "") === level);
  if (!ns.some((n) => n.node_type === "group")) return ns;
  return ns.filter((n) => !String(n.parent_key || ""));
}

/** The GOAL's display name. The owner renamed it; the payload may still carry an
 * older title — override client-side ONLY when the backend has not renamed it. */
function goalTitle(n) {
  const t = String((n && n.title) || "");
  return /artifact/i.test(t) ? t : "The Artifact — the running platform";
}

// Group health rolls WORST-OF its children, derived client-side whenever the
// payload has not rolled it itself (every health field is optional — the Atlas
// must render gracefully against a backend that predates the contract).
const HS_RANK = { green: 0, yellow: 1, red: 2 };
function deriveGroupHealth(data) {
  const worst = {};
  for (const n of (data && data.nodes) || []) {
    const p = String(n.parent_key || "");
    const r = n.health && HS_RANK[n.health.status];
    if (p && r != null) worst[p] = Math.max(worst[p] ?? 0, r);
  }
  for (const n of (data && data.nodes) || []) {
    if (n.node_type !== "group" || (n.health && n.health.status)) continue;
    const r = worst[String(n.key)];
    if (r != null) n.health = { status: ["green", "yellow", "red"][r] };
  }
}

// ---- navigation -----------------------------------------------------------------
/** All travel funnels through the hash, so the address bar, the back button and
 * a pasted link all mean the same room. route() → openModuleGraph → open → navTo. */
function drillTo(i, key) {
  const want = key ? "#/graph/" + encodeURIComponent(key) : "#/graph";
  // Never rewrite an already-correct hash: assigning it again mints a duplicate
  // history entry, and the first browser-back then appears to do nothing.
  if (location.hash !== want) location.hash = want;
  else navTo(i, key || null);
}

/** Door-chip / Atlas travel: go to a room and flash-highlight one card there. */
function travel(i, roomKey, targetKey) {
  i.flashKey = targetKey || null;
  if (i.atlasOpen) toggleAtlas(i, false);
  if ((roomKey || null) === i.level) { flashNow(i); return; }
  drillTo(i, roomKey || "");
}

function flashNow(i) {
  if (!i.flashKey) return;
  const el = i.cards.get(String(i.flashKey));
  i.flashKey = null;
  if (!el) return;
  el.classList.remove("gr-flash");
  void el.offsetWidth;                       // restart the animation
  el.classList.add("gr-flash");
  i.focusKey = el.getAttribute("data-key");
  applyFocus(i);
  clearTimeout(i.flashTimer);
  i.flashTimer = setTimeout(() => el.classList.remove("gr-flash"), 1800);
}

/** The room transition, frame by frame: entering a chamber = the clicked card
 * scales up toward the viewer while the room fades; ~200ms in the stage swaps
 * and the new room's cards stagger in (~60ms, topo order), wires drawing
 * themselves last. Leaving = the reverse (the room recedes). Under
 * prefers-reduced-motion: an instant swap, nothing in between. */
function navTo(i, level) {
  level = level || null;
  if (level === i.level) { renderCrumb(i); flashNow(i); return; }
  if (!i.data) { i.level = level; return; }
  i.inspectKey = null;
  clearSel(i);
  asideDefault(i);
  if (i.atlasOpen) toggleAtlas(i, false);
  if (reduceMotion()) { setRoomNow(i, level); return; }
  i.transition = true;
  clearTimeout(i.swapTimer);
  clearTimeout(i.revealTimer);
  i.revealTimer = null; i.revealQueue = [];
  const enteringCard = level ? i.cards.get(String(level)) : null;
  if (enteringCard) {
    // entering: the chamber card blooms into the new room
    enteringCard.classList.add("gr-openit");
    i.room.classList.add("gr-room-enter");
  } else {
    // leaving (or a cross-jump): the room recedes
    i.room.classList.add("gr-room-leave");
  }
  i.swapTimer = setTimeout(() => {
    if (inst !== i) return;
    i.transition = false;
    setRoomNow(i, level);
    scheduleRefetch(i);
  }, SWAP_MS);
}

/** Cut to a room now: forget the room's entrance keys so the staging replays,
 * then paint. */
function setRoomNow(i, level) {
  i.level = level;
  i.room.classList.remove("gr-room-enter", "gr-room-leave");
  const planId = i.data && i.data.plan && i.data.plan.id;
  roomNodes(i.data, level).forEach((n) => SEEN.delete(planId + ":" + n.key));
  i.focusKey = null;
  paint(i, i.data);
}

// ---- painting -------------------------------------------------------------------
function paint(i, data) {
  i.data = data;
  deriveGroupHealth(data);
  i.byKey = new Map((data.nodes || []).map((n) => [String(n.key), n]));
  const planId = data.plan && data.plan.id;
  // A replan can dissolve the group being viewed — fall back to the top room.
  // REPLACE the history entry rather than pushing one (the back-button lesson).
  if (i.level && !roomNodes(data, i.level).length) {
    i.level = null;
    if (location.hash !== "#/graph") history.replaceState(null, "", "#/graph");
  }
  const counts = {};
  for (const n of data.nodes || []) {
    const p = String(n.parent_key || "");
    if (p) counts[p] = (counts[p] || 0) + 1;
  }
  const ns = roomNodes(data, i.level);
  for (const n of ns)
    if (n.node_type === "group") n.children = counts[String(n.key)] || 0;
  const conclusion = (data.nodes || []).find((n) => n.node_type === "conclusion");
  const showArtifact = !i.level && !!conclusion;
  i.roomKeys = new Set(ns.map((n) => String(n.key)));
  if (showArtifact) i.roomKeys.add(String(conclusion.key));

  // In-room edges only — the SVG underlay draws nothing else.
  const inEdges = ((data.edges) || []).filter((e) => {
    const [s, d] = edgeEnds(e);
    return i.roomKeys.has(s) && i.roomKeys.has(d);
  });

  // Dependency columns over the room's own edges; the Artifact is ALWAYS the
  // last column of the top room, whatever the topology says.
  const cols = columns(ns, inEdges.filter((e) => {
    const [s, d] = edgeEnds(e);
    return (!conclusion || (s !== String(conclusion.key) && d !== String(conclusion.key)));
  }));
  const colCount = cols.length + (showArtifact ? 1 : 0);
  i.room.innerHTML = "";
  i.room.style.setProperty("--gr-cols", String(Math.max(1, colCount)));
  const nsBy = new Map(ns.map((n) => [String(n.key), n]));
  const oldCards = i.cards;
  i.cards = new Map();
  const place = (colEl, key, el) => { colEl.appendChild(el); i.cards.set(key, el); };
  cols.forEach((col) => {
    const colEl = document.createElement("div");
    colEl.className = "gr-col";
    for (const key of col) {
      const n = nsBy.get(key);
      let el = oldCards.get(key);
      if (el && el.getAttribute("data-kind") !== "artifact") updateCard(el, n);
      else el = buildCard(n);
      if (!SEEN.has(planId + ":" + key)) el.classList.add("gr-hidden");
      place(colEl, key, el);
    }
    i.room.appendChild(colEl);
  });
  if (showArtifact) {
    const colEl = document.createElement("div");
    colEl.className = "gr-col gr-col-artifact";
    const key = String(conclusion.key);
    const el = buildArtifactCard(conclusion, data.conclusion, goalTitle(conclusion));
    if (!SEEN.has(planId + ":" + key)) el.classList.add("gr-hidden");
    place(colEl, key, el);
    i.room.appendChild(colEl);
  }

  // Door chips — cross-room dependencies, from the payload's edges alone.
  for (const n of ns) attachDoors(i, i.cards.get(String(n.key)), n);

  showActivity(i);
  reselect(i);
  const planInfo = document.getElementById("graphPlanInfo");
  if (planInfo && data.plan) planInfo.textContent =
    `v${data.plan.version} · by ${data.plan.authored_by || "seed"}`;

  i.inEdges = inEdges;
  const fresh = topoOrder(ns, inEdges)
    .concat(showArtifact ? [String(conclusion.key)] : [])
    .filter((k) => !SEEN.has(planId + ":" + k));
  if (fresh.length) reveal(i, planId, fresh);
  else drawWires(i, false);

  if (!i.focusKey || !i.cards.has(i.focusKey))
    i.focusKey = (cols[0] && cols[0][0]) || (ns[0] && String(ns[0].key)) || null;
  applyFocus(i, true);
  flashNow(i);
  renderCrumb(i);
  renderGoalChip(i);
  renderLegend(i);
  if (i.atlasOpen) renderAtlas(i);
}

/** The staged entrance: cards scale/fade in one at a time in topo order (~60ms
 * stagger, a CSS transition), the wires drawing themselves only AFTER their
 * endpoints via a dash-offset sweep. Under prefers-reduced-motion — the check
 * comes FIRST, before any stepper is armed — everything simply appears. */
function reveal(i, planId, freshKeys) {
  if (reduceMotion()) {
    for (const k of freshKeys) {
      SEEN.add(planId + ":" + k);
      const el = i.cards.get(k);
      if (el) el.classList.remove("gr-hidden");
    }
    drawWires(i, false);
    return;
  }
  for (const k of freshKeys)
    if (!i.revealQueue.some((q) => q[1] === k)) i.revealQueue.push([planId, k]);
  if (i.revealTimer) return;            // the stepper is already walking
  const step = () => {
    i.revealTimer = null;
    const q = i.revealQueue.shift();
    if (!q) { drawWires(i, true); return; }        // all cards on stage → the wires' turn
    SEEN.add(q[0] + ":" + q[1]);
    const el = i.cards.get(q[1]);
    if (el) {
      void el.offsetWidth;                          // commit the hidden state first
      el.classList.remove("gr-hidden");             // → CSS transition plays
    }
    i.revealTimer = setTimeout(step, STAGGER_MS);
  };
  i.revealTimer = setTimeout(step, 40);
}

// ---- the wires: an SVG underlay of real in-room edges ---------------------------
/** Orthogonal elbows between card anchor points READ FROM THE DOM after layout —
 * deterministic, no simulation of where the grid put things. Source anchor:
 * right edge midpoint; destination anchor: left edge midpoint. */
function drawWires(i, staged) {
  const svg = i.wires;
  svg.innerHTML = "";
  i.wireEls = [];
  const wr = i.wrap.getBoundingClientRect();
  const wpx = Math.max(i.wrap.scrollWidth, Math.round(wr.width));
  const hpx = Math.max(i.wrap.scrollHeight, Math.round(wr.height));
  svg.setAttribute("width", wpx);
  svg.setAttribute("height", hpx);
  svg.setAttribute("viewBox", `0 0 ${wpx} ${hpx}`);
  const defs = svgMk("defs");
  const marker = svgMk("marker", { id: "grArrow", viewBox: "0 0 10 10",
    refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" });
  marker.appendChild(svgMk("path", { d: "M0,0 L10,5 L0,10 Z", class: "gr-arrowhead" }));
  defs.appendChild(marker);
  svg.appendChild(defs);
  let di = 0;
  for (const e of i.inEdges || []) {
    const [s, d] = edgeEnds(e);
    const A = i.cards.get(s), B = i.cards.get(d);
    if (!A || !B) continue;
    const ra = A.getBoundingClientRect(), rb = B.getBoundingClientRect();
    const scroll = { x: -wr.left, y: -wr.top };
    const x0 = ra.right + scroll.x, y0 = ra.top + ra.height / 2 + scroll.y;
    const x1 = rb.left + scroll.x, y1 = rb.top + rb.height / 2 + scroll.y;
    let dPath;
    if (x1 > x0 + 12) {
      const mx = (x0 + x1) / 2;
      dPath = `M ${x0} ${y0} L ${mx} ${y0} L ${mx} ${y1} L ${x1 - 3} ${y1}`;
    } else {
      // a back-edge or same-column edge: route around via the cards' outer rims
      const yb = Math.max(ra.bottom, rb.bottom) + 18 + scroll.y;
      dPath = `M ${x0} ${y0} L ${x0 + 14} ${y0} L ${x0 + 14} ${yb} `
            + `L ${x1 - 14} ${yb} L ${x1 - 14} ${y1} L ${x1 - 3} ${y1}`;
    }
    const p = svgMk("path", {
      class: "gr-wire gr-wire-" + (e.edge_type || "depends"),
      d: dPath, fill: "none", "marker-end": "url(#grArrow)",
    });
    if (staged && !reduceMotion()) {
      p.classList.add("gr-draw");
      p.style.setProperty("--gr-dd", Math.min(di * 45, 500) + "ms");
    }
    svg.appendChild(p);
    i.wireEls.push({ a: s, b: d, el: p });
    di += 1;
  }
  if (staged) {
    clearTimeout(i.drawTimer);
    i.drawTimer = setTimeout(() => {
      if (inst === i) i.wireEls.forEach((x) => x.el.classList.remove("gr-draw"));
    }, 1200);
  }
  flowWires(i);
}

// ---- door chips: where a dependency leaves the room -----------------------------
/** The chips for one card, derived EXCLUSIVELY from the payload's edges — a
 * chip exists iff a real edge crosses the room's wall at this card. The foreign
 * endpoint resolves to its owning room (its parent group; the top room when it
 * has none) plus the leaf itself, so the label reads "→ Data › db" and the
 * click can land with the target flash-highlighted. Never derived from group
 * adjacency — the wall's truth is the child edges themselves. */
function doorChips(i, n) {
  const key = String(n.key);
  const out = [];
  const seen = new Set();
  for (const e of (i.data && i.data.edges) || []) {
    const [s, d] = edgeEnds(e);
    const feeds = s === key && !i.roomKeys.has(d);
    const needs = d === key && !i.roomKeys.has(s);
    if (!feeds && !needs) continue;
    const foreign = i.byKey.get(feeds ? d : s);
    if (!foreign) continue;
    let room, roomTitle, target;
    if (foreign.node_type === "conclusion") {
      room = ""; roomTitle = "The Artifact"; target = String(foreign.key);
    } else if (foreign.node_type === "group") {
      room = String(foreign.key); roomTitle = String(foreign.title || foreign.key); target = "";
    } else {
      const p = String(foreign.parent_key || "");
      room = p && i.byKey.has(p) ? p : "";
      const holder = room ? i.byKey.get(room) : null;
      roomTitle = holder ? String(holder.title || room) : "overview";
      target = String(foreign.key);
    }
    const sig = (feeds ? ">" : "<") + room + "›" + target;
    if (seen.has(sig)) continue;
    seen.add(sig);
    const leaf = foreign.node_type === "group" ? "" : String(foreign.title || foreign.key);
    out.push({
      dir: feeds ? "out" : "in", room, target,
      label: roomTitle + (leaf && leaf !== roomTitle ? " › " + leaf : ""),
    });
  }
  return out;
}

function attachDoors(i, el, n) {
  if (!el) return;
  const host = el.querySelector(".gr-doors");
  if (!host) return;
  host.innerHTML = "";
  for (const c of doorChips(i, n)) {
    const b = document.createElement("button");
    b.className = "gr-door gr-door-" + c.dir;
    b.setAttribute("data-door", c.room);
    b.setAttribute("data-target", c.target);
    b.title = (c.dir === "out" ? "feeds " : "needs ") + c.label + " — travel there";
    const arrow = document.createElement("span");
    arrow.className = "gr-door-arrow";
    arrow.textContent = c.dir === "out" ? "→" : "←";
    const name = document.createElement("span");
    name.className = "gr-door-name";
    name.textContent = c.label;                    // textContent — titles are authored text
    b.append(arrow, name);
    host.appendChild(b);
  }
}

// ---- live activity --------------------------------------------------------------
/** A card the crew is touching right now pulses and says what is being done. */
function setBusy(i, el, act) {
  el.classList.toggle("gr-busy", !!act);
  let line = el.querySelector(".gr-act");
  if (act) {
    if (!line) {
      line = document.createElement("div");
      line.className = "gr-act";
      const doors = el.querySelector(".gr-doors");
      el.insertBefore(line, doors || null);
    }
    line.textContent = "● " + String(act.task || act.what || "working");
  } else if (line) line.remove();
}
function showActivity(i) {
  i.cards.forEach((el, key) => {
    const n = i.byKey.get(key);
    setBusy(i, el, ((n && n.activity) || [])[0]);
  });
  flowWires(i);
}

/** Live edge flow: a wire whose either endpoint is busy carries a marching-dash
 * shimmer — the data is visibly moving. The animation itself is pure CSS. */
function flowWires(i) {
  for (const w of i.wireEls) {
    const A = i.cards.get(w.a), B = i.cards.get(w.b);
    const on = !!((A && A.classList.contains("gr-busy")) ||
                  (B && B.classList.contains("gr-busy")));
    w.el.classList.toggle("gr-flow", on);
  }
}

function onGraphEvent(i, e) {
  if (!e || !e.kind) return;
  if (e.kind === "graph_node_active" || e.kind === "graph_node_idle") {
    const el = e.node != null && i.cards.get(String(e.node));
    if (el) {
      setBusy(i, el, e.kind === "graph_node_active"
        ? { task: e.task || e.what || "working" } : null);
      flowWires(i);
    }
    return;
  }
  scheduleRefetch(i);          // verify results, replans, crew events → repaint soon
}

// ---- breadcrumb + the goal chip -------------------------------------------------
/** Persistent, in the bar: `aim` at the top room, `aim ▸ Group` inside one —
 * the aim crumb is the way back up. */
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
    `<button class="gr-crumb-up" title="Back to the top room (Esc)">🎯 ${root}</button>` +
    `<span class="gr-crumb-sep">▸</span>` +
    `<span class="gr-crumb-here">${esc((g && g.title) || i.level)}</span>`;
  const b = i.crumb.querySelector(".gr-crumb-up");
  if (b) b.onclick = () => drillTo(i, "");
}

/** In sub-rooms the Artifact stays present as a slim goal chip in the header,
 * linking home; in the top room the Artifact card itself is on stage. */
function renderGoalChip(i) {
  const el = i.goalChip;
  if (!el) return;
  if (!i.data || !i.level) { el.hidden = true; return; }
  const node = (i.data.nodes || []).find((n) => n.node_type === "conclusion");
  const c = i.data.conclusion || {};
  el.innerHTML = "";
  const flag = document.createElement("span");
  flag.textContent = "🏁";
  const name = document.createElement("span");
  name.className = "gr-goalchip-name";
  name.textContent = goalTitle(node);
  const dot = document.createElement("span");
  dot.className = "gr-goal-dot gr-goaldot-" + String(c.health || "unknown");
  el.append(flag, name, dot);
  el.title = goalTitle(node) + " — back to the top room";
  el.hidden = false;
  el.onclick = () => {
    if (node) i.flashKey = String(node.key);
    drillTo(i, "");
  };
}

// ---- the health legend: three states, one line each -----------------------------
function renderLegend(i) {
  const el = document.getElementById("graphLegend");
  if (!el) return;
  if (el.childElementCount) { el.hidden = false; return; }   // built once per mount
  el.innerHTML = "";
  for (const [k, line] of [
    ["green", "green — calm glow: heartbeat and tests both fine"],
    ["yellow", "amber — pulsing: tests failing, heartbeat fine"],
    ["red", "red — strobing: the heartbeat itself is failing"],
  ]) {
    const row = document.createElement("div");
    row.className = "gr-legend-row";
    const dot = document.createElement("span");
    dot.className = "gr-legend-dot gr-hs-" + k;
    const txt = document.createElement("span");
    txt.textContent = line;
    row.append(dot, txt);
    el.appendChild(row);
  }
  el.hidden = false;
}

// ---- the Atlas map: the whole two-level tree, one overlay -----------------------
/** M (or the bar button) opens the full-screen map: one box per room — the top
 * room first, then every chamber — each with compact health-coloured dots for
 * its cards, the current room marked. Click a room to jump; click a dot to
 * jump AND flash that card. The Hollow Knight moment: you always know where
 * you are, and everywhere is one click away. */
function toggleAtlas(i, on) {
  const want = on != null ? !!on : !i.atlasOpen;
  i.atlasOpen = want;
  if (!i.atlas) return;
  i.atlas.hidden = !want;
  if (want) renderAtlas(i);
}

function renderAtlas(i) {
  const host = i.atlas;
  if (!host || !i.data) return;
  host.innerHTML = "";
  const inner = document.createElement("div");
  inner.className = "gr-atlas-inner";
  const head = document.createElement("div");
  head.className = "gr-atlas-head";
  const title = document.createElement("b");
  title.textContent = "🗺 The Atlas";
  const hint = document.createElement("span");
  hint.className = "dim";
  hint.textContent = "click anywhere to travel · M or Esc closes";
  head.append(title, hint);
  inner.appendChild(head);
  const grid = document.createElement("div");
  grid.className = "gr-atlas-grid";
  const dotFor = (n) => {
    const dot = document.createElement("button");
    dot.className = "gr-atlas-dot "
      + ((n.health && HS_RANK[n.health.status] != null) ? "gr-hs-" + n.health.status : "gr-hs-unknown");
    dot.title = String(n.title || n.key);
    dot.setAttribute("data-jump-node", String(n.key));
    return dot;
  };
  const roomBox = (roomKey, label, nodes) => {
    const box = document.createElement("div");
    box.className = "gr-atlas-room" + ((roomKey || null) === i.level ? " gr-atlas-here" : "");
    const h = document.createElement("button");
    h.className = "gr-atlas-roomname";
    h.textContent = label + ((roomKey || null) === i.level ? "  ◉ you are here" : "");
    h.setAttribute("data-jump-room", roomKey);
    box.appendChild(h);
    const dots = document.createElement("div");
    dots.className = "gr-atlas-dots";
    nodes.forEach((n) => dots.appendChild(dotFor(n)));
    box.appendChild(dots);
    return box;
  };
  const tops = roomNodes(i.data, null);
  const conclusion = (i.data.nodes || []).find((n) => n.node_type === "conclusion");
  grid.appendChild(roomBox("", "Overview", tops.concat(conclusion ? [conclusion] : [])));
  for (const g of tops.filter((n) => n.node_type === "group"))
    grid.appendChild(roomBox(String(g.key), String(g.title || g.key),
                             roomNodes(i.data, String(g.key))));
  inner.appendChild(grid);
  host.appendChild(inner);
}

// ---- the team selector: the pool modules are staffed from -----------------------
/** Lives in the graph header bar. GET /team fills it; changing it POSTs the pool
 * switch. Rendered only when the backend answers — the endpoint may not have
 * landed yet, and a dead dropdown would be a lie. */
function teamMembers(r) {
  const list = (r && (r.members || r.agents)) || (Array.isArray(r) ? r : []);
  return (Array.isArray(list) ? list : [])
    .map((m) => ({ id: m.agent_id ?? m.id ?? m.home_id,
                   name: String(m.name || m.label || `agent #${m.agent_id ?? m.id}`) }))
    .filter((m) => m.id != null);
}

async function loadTeam(i) {
  const sel = document.getElementById("graphTeam");
  if (!sel) return;
  let r = null;
  try { r = await i.src.team(); }
  catch (e) { sel.hidden = true; return; }         // honest: no endpoint, no dropdown
  if (inst !== i || !r) return;
  i.teamInfo = r;                                  // the agent picker rides the same payload
  // current/teams are {world_id, room_id, name} dicts; the option VALUE is "wid:rid"
  // (what setTeam posts) and the LABEL is the human name — never the object itself.
  const refOf = (t) => `${t.world_id}:${t.room_id}`;
  const c = r.current && typeof r.current === "object" ? r.current : null;
  const teams = (Array.isArray(r.teams) ? r.teams : [])
    .filter((t) => t && t.world_id != null)
    .map((t) => ({ ref: refOf(t), name: String(t.name || `team ${refOf(t)}`) }));
  if (c && c.world_id != null && !teams.some((t) => t.ref === refOf(c)))
    teams.unshift({ ref: refOf(c), name: String(c.name || `team ${refOf(c)}`) });
  if (!teams.length) { sel.hidden = true; return; }
  sel.innerHTML = "";
  for (const t of teams) {
    const o = document.createElement("option");
    o.value = t.ref;
    o.textContent = "team: " + t.name;
    if (c && t.ref === refOf(c)) o.selected = true;
    sel.appendChild(o);
  }
  sel.hidden = false;
  sel.onchange = async () => {
    try {
      await i.src.setTeam(sel.value);
      const label = sel.options[sel.selectedIndex]?.textContent || sel.value;
      W.toast && W.toast(`${label} — modules staff from this pool now`);
    } catch (e) { W.toast && W.toast((e && e.message) || String(e), true); }
    if (inst === i) { loadTeam(i); scheduleRefetch(i); }
  };
}

// ---- selection ------------------------------------------------------------------
function clearSel(i) {
  i.cards.forEach((el) => el.classList.remove("gr-sel"));
}
function setSel(i, key) {
  clearSel(i);
  const el = i.cards.get(String(key));
  if (el) el.classList.add("gr-sel");
}
function reselect(i) {
  if (i.inspectKey && i.cards.has(i.inspectKey)) setSel(i, i.inspectKey);
}

// ---- the aside (this screen's own panel/action host) ----------------------------
function asideDefault(i) {
  i.aside.innerHTML = `<div class="gr-tip">
    <p class="dim"><b>The Atlas</b> — one room at a time, no camera to lose.
    <b>Click</b> a capillary card (where an agent works) and everything about it
    — its agent, spec, tests, wiring, steering — opens here. <b>Click</b> a
    chamber card (a doorway) or press <kbd>Enter</kbd> to walk into that room;
    <kbd>Esc</kbd> or the breadcrumb climbs back out. Arrow keys move the
    focus. Door chips on a card ("→ Data › db") are its real cross-room
    dependencies — click one to travel there. <kbd>M</kbd> opens the full map.
    Right-click a card for its verbs.</p></div>`;
}

function testsLine(tests) {
  const t = tests || {};
  if (!t.total) return `<span class="dim">no tests mapped</span>`;
  let s = `${t.passing}/${t.total} passing`;
  if (t.failing) s += ` · <b class="gr-failtext">${t.failing} failing</b>
    <span class="dim">(advisory — a red ring informs, it never blocks)</span>`;
  return s;
}

/** The agent row — FRONT AND CENTER, right under the title. A leaf shows its
 * specialist (avatar initial + name) or an honest "Unassigned" with the one
 * action that fixes it: asking the manager to author the plan. A group is
 * staffed through its children, and says so instead of pretending. */
function agentRow(i, n, d) {
  if (n.node_type === "group") {
    return `<div class="gr-agent-row gr-agent-grp"><span class="gr-agent-avatar">🗂</span>
      <div class="dim">a group is staffed through its modules — agents live on the leaves inside</div></div>`;
  }
  const a = d.agent;
  if (a) {
    const name = String(a.name || "").trim() || `agent #${a.agent_id ?? a.home_id}`;
    return `<div class="gr-agent-row">
      <span class="gr-agent-avatar">${esc((name.trim()[0] || "?").toUpperCase())}</span>
      <div><b>${esc(name)}</b><div class="dim">the specialist working this module</div></div></div>`;
  }
  return `<div class="gr-agent-row gr-agent-none">
    <span class="gr-agent-avatar">?</span>
    <div><b>Unassigned</b> — the manager staffs modules when it authors the plan
      <div class="gr-actions"><button class="rp-mini" id="grStaff">Have the manager plan now</button>
      <span class="dim" id="grStaffOut"></span></div></div></div>`;
}

function fmtUptime(s) {
  s = Math.max(0, Math.floor(+s || 0));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
}

/** Where the mini cluster iframe may point. `sandbox` is non-negotiable, and the
 * frame may NEVER be this app's own root — embedding the live platform inside
 * itself is the one recursion the goal panel refuses. */
function clusterSafeUrl(u) {
  try {
    const url = new URL(String(u || ""), location.href);
    if (!/^https?:$/.test(url.protocol)) return null;
    if (url.origin === location.origin && (url.pathname === "/" || url.pathname === "")) return null;
    return url.href;
  } catch (e) { return null; }
}

function clusterHtml(c) {
  const cl = c.cluster;                            // optional by contract
  if (!cl) return `<p class="dim">this server does not report a mini cluster yet</p>`;
  if (!cl.available)
    return `<p class="dim">${esc(cl.reason || "the mini cluster is not available on this server")}</p>`;
  if (cl.running) {
    const url = clusterSafeUrl(cl.url);
    const frame = url
      ? `<div class="gr-monitor"><iframe sandbox="allow-same-origin allow-scripts"
           src="${esc(url)}" title="mini cluster"></iframe></div>`
      : `<p class="err">the cluster URL points at this app itself — refusing to embed it</p>`;
    return `${frame}<div class="gr-actions">
      <button id="grClusterStop">■ Stop mini cluster</button>
      <span class="dim" id="grClusterOut"></span></div>`;
  }
  return `<div class="gr-actions"><button class="primary" id="grClusterStart">▶ Start mini cluster</button>
    <span class="dim" id="grClusterOut"></span></div>`;
}

function wireCluster(i) {
  const go = async (action, btn) => {
    btn.disabled = true;
    const o = i.aside.querySelector("#grClusterOut");
    if (o) o.textContent = action === "start" ? "booting the mini cluster…" : "stopping…";
    try { await i.src.cluster(action); }
    catch (e) {
      if (o) o.textContent = (e && e.message) || String(e);
      btn.disabled = false;
      return;
    }
    if (inst !== i) return;
    await refetch(i);
    asideConclusion(i);                            // repaint from the fresh truth
  };
  const s = i.aside.querySelector("#grClusterStart");
  if (s) s.onclick = () => go("start", s);
  const st = i.aside.querySelector("#grClusterStop");
  if (st) st.onclick = () => go("stop", st);
}

/** The GOAL panel — the Artifact card's click. Live beat, uptime and shas when
 * the backend sends them (all optional), plus the mini cluster. */
function asideConclusion(i) {
  const c = (i.data && i.data.conclusion) || {};
  const node = ((i.data && i.data.nodes) || []).find((n) => n.node_type === "conclusion");
  const rep = c.repair || {};
  i.aside.innerHTML = `
    <div class="gr-light gr-conclusion">
      <div class="gr-light-head"><span class="gr-light-glyph">🏁</span><div><b>${esc(goalTitle(node))}</b>
        <div class="dim">the GOAL — what all of it adds up to</div></div></div>
      <p>health: <b class="gr-health gr-health-${esc(c.health || "unknown")}">${esc(c.health || "unknown")}</b>${
        c.beat != null ? ` · beat: <b>${esc(String(c.beat))}</b>` : ""}</p>
      ${c.uptime_s != null ? `<p class="dim">up ${esc(fmtUptime(c.uptime_s))}</p>` : ""}
      ${c.boot_sha ? `<p class="dim">boot <code>${esc(String(c.boot_sha).slice(0, 10))}</code></p>` : ""}
      <p class="dim">crew: ${esc(rep.phase || "idle")}${rep.sprint ? ` · sprint ${esc(rep.sprint)}` : ""}</p>
      <div class="gr-sec"><div class="gr-sec-h">Mini cluster</div><div id="grCluster">${clusterHtml(c)}</div></div>
      <button class="rp-mini" id="grOpenHq">🏢 Open HQ</button>
    </div>`;
  wireCluster(i);
  const b = i.aside.querySelector("#grOpenHq");
  if (b) b.onclick = () => { location.hash = "#/hq"; };
}

/** THE panel: one side panel with everything, opened by a SINGLE click on any
 * capillary (or Peek on any card). What you see is always complete: agent
 * first, then spec, tests, wiring, trace and steering. Groups add the dive and
 * the union-verify. Kept verbatim from the previous graph — it is good. */
async function openPanel(i, key) {
  if (!i.byKey.has(String(key))) { asideDefault(i); return; }
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
  renderPanel(i, key, d);
}

// Fallback only — the live option list rides in the payload (d.models), so the
// select can never drift from what the server's validator accepts.
const MODELS = ["claude-sonnet-5", "claude-opus-4-8", "claude-fable-5", "claude-haiku-4-5"];

// ---- tech stack: derived CLIENT-SIDE from the node's paths whenever the payload
// carries no `stack` of its own (the field is optional by contract) — file
// extensions are the honest signal the client already holds.
const EXT_KIND = [
  [/\.py$/i, "Python / FastAPI"],
  [/\.(js|mjs)$/i, "JavaScript / vanilla"],
  [/\.sql$/i, "SQL"],
  [/\.css$/i, "CSS"],
  [/\.html?$/i, "HTML"],
  [/\.(md|txt)$/i, "docs"],
];
function techStack(stack, paths) {
  if (Array.isArray(stack) && stack.length)
    return stack.map((s) => (typeof s === "string"
      ? { kind: s, files: 0 }
      : { kind: String(s.kind || s.name || "?"), files: Number(s.files ?? s.count ?? 0) }));
  const counts = new Map();
  for (const p of paths || []) {
    const kind = (EXT_KIND.find(([re]) => re.test(String(p))) || [0, "other"])[1];
    counts.set(kind, (counts.get(kind) || 0) + 1);
  }
  return [...counts].map(([kind, files]) => ({ kind, files }));
}

/** A group's stack is its children's files — a group card has no paths itself. */
function nodePaths(i, key, n) {
  if ((n.node_type || "") !== "group") return n.paths || [];
  const out = [];
  for (const c of (i.data && i.data.nodes) || [])
    if (String(c.parent_key || "") === String(key)) out.push(...(c.paths || []));
  return out;
}

/** The mastery badge: EARNED, never asserted — ★ only after the backend has
 * counted enough verified runs; anything less says exactly how far along it is.
 * The whole field is optional (the payload may predate the backend). */
function masteryLine(m) {
  if (!m || m.runs == null) return "";
  const runs = Number(m.runs) || 0;
  if (m.master) return `<p class="gr-mastery gr-mastery-on">★ Master of this module
    <span class="dim">· ${runs} verified run${runs === 1 ? "" : "s"}</span></p>`;
  return `<p class="gr-mastery dim">working toward mastery — ${Math.min(runs, 3)}/3 runs</p>`;
}

function renderPanel(i, key, d) {
  const n = d.node || {};
  const cfg = d.config || {};
  const live = (i.byKey && i.byKey.get(key)) || {};
  const isGroup = n.node_type === "group";
  const act = ((live.activity || [])[0]);
  const models = ["", ...(Array.isArray(d.models) && d.models.length ? d.models : MODELS)];
  // A group's suite rows live on its leaves; its rolled-up counts ride on the
  // graph payload node — show those, never a lying "no tests mapped".
  const testRows = isGroup
    ? `<p class="gr-testline">${testsLine(live.tests)}</p>
       <p class="dim">per-file results live on each module inside — enter the room to see them</p>`
    : (d.tests || []).map((tt) => `
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
  // The optional contract fields ride the GRAPH payload node (`live`) until the
  // inspector payload learns them — read them from either, never require both.
  const health = n.health || live.health || null;
  const mastery = n.mastery || live.mastery || null;
  const stackRows = techStack(n.stack || live.stack, nodePaths(i, key, { ...live, ...n }))
    .map((s) => `<span class="gr-stack-chip">${esc(s.kind)}${
      s.files ? ` <span class="dim">· ${s.files} file${s.files === 1 ? "" : "s"}</span>` : ""}</span>`)
    .join("") || `<span class="dim">no files mapped — nothing to derive a stack from</span>`;
  const healthLine = health && health.status
    ? `<p class="gr-healthline gr-hl-${esc(health.status)}">health: <b>${esc(health.status)}</b>${
        health.status === "yellow" ? ` <span class="dim">— tests failing, heartbeat fine</span>` :
        health.status === "red" ? ` <span class="dim">— heartbeat failing</span>` : ""}</p>` : "";
  i.aside.innerHTML = `
    <div class="gr-inspect">
      <div class="gr-light-head">
        <span class="gr-light-glyph">${GLYPH[n.node_type] || GLYPH.code}</span>
        <div><b>${esc(n.title || key)}</b><div class="dim">${esc(n.node_type || "")} · ${esc(key)}${
          isGroup ? ` · ${Number(live.children) || 0} modules` : ""}</div></div>
        <button class="rp-link gr-close" id="grInsClose">close</button>
      </div>
      ${act ? `<p class="gr-actline">● ${esc(act.task || act.what || "working")}</p>` : ""}
      ${healthLine}
      ${n.spec ? `<p class="gr-spec">${esc(n.spec)}</p>` : ""}
      ${(n.tags || []).length ? `<div class="gr-tags">${n.tags.map((g) => `<span class="gr-tag">${esc(g)}</span>`).join("")}</div>` : ""}
      <div class="gr-sec" id="grSecStack"><div class="gr-sec-h">Tech stack
        <button class="rp-mini gr-replace" id="grRepStack">Replace</button></div>
        <div class="gr-stack">${stackRows}</div></div>
      <div class="gr-sec" id="grSecTests"><div class="gr-sec-h">Test suite
        <button class="rp-mini gr-replace" id="grRepTests">Replace</button></div>
        <p class="dim">Tests are advisory — a red ring informs, it never blocks.</p>
        ${testRows}
        <div class="gr-actions"><button${isGroup ? "" : ` class="primary"`} id="grVerify">▶ Verify now</button>
        <span class="dim" id="grVerifyOut"></span></div></div>
      <div class="gr-sec" id="grSecAgent"><div class="gr-sec-h">Agent
        <button class="rp-mini gr-replace" id="grAgentPick">Change agent</button></div>
        ${agentRow(i, n, d)}
        ${masteryLine(mastery)}</div>
      <div class="gr-sec"><div class="gr-sec-h">Edges &amp; contracts</div>${edgeRows}</div>
      <div class="gr-sec"><div class="gr-sec-h">Trace</div>${traceRows}</div>
      <div class="gr-sec"><div class="gr-sec-h">Steering</div>
        <label class="gr-cfg">model
          <select id="grCfgModel">${models.map((mm) =>
            `<option value="${esc(mm)}"${mm === (cfg.model || "") ? " selected" : ""}>${mm ? esc(mm) : "default"}</option>`).join("")}</select>
        </label>
        <label class="gr-cfg">autonomy
          <select id="grCfgAutonomy">${["", "supervised", "autonomous"].map((a) =>
            `<option value="${a}"${a === (cfg.autonomy || "") ? " selected" : ""}>${a || "default"}</option>`).join("")}</select>
        </label>
      </div>
      ${isGroup ? `<div class="gr-actions"><button class="primary" id="grGroupOpen">⊕ Enter room</button></div>` : ""}
    </div>`;
  wirePanel(i, key, isGroup);
}

function wirePanel(i, key, isGroup) {
  const closeBtn = i.aside.querySelector("#grInsClose");
  if (closeBtn) closeBtn.onclick = () => { i.inspectKey = null; clearSel(i); asideDefault(i); };
  const ob = i.aside.querySelector("#grGroupOpen");
  if (ob) ob.onclick = () => drillTo(i, key);
  // The three sections' Replace flows: stack/tests file a ticket; the agent
  // section's replace IS the picker (a group is staffed through its leaves).
  const repStack = i.aside.querySelector("#grRepStack");
  if (repStack) repStack.onclick = () => replaceDialog(i, key, "stack", "Tech stack");
  const repTests = i.aside.querySelector("#grRepTests");
  if (repTests) repTests.onclick = () => replaceDialog(i, key, "tests", "Test suite");
  const pick = i.aside.querySelector("#grAgentPick");
  if (pick) {
    if (isGroup) { pick.disabled = true; pick.title = "a group is staffed through its modules"; }
    else pick.onclick = () => agentPicker(i, key);
  }
  const staff = i.aside.querySelector("#grStaff");
  if (staff) staff.onclick = async () => {          // "Have the manager plan now"
    const out = i.aside.querySelector("#grStaffOut");
    staff.disabled = true;
    if (out) out.textContent = "asking the manager to author the plan…";
    try {
      const r = await i.src.replan();
      if (out) out.textContent = `✓ plan v${(r.plan || {}).version} by ${(r.plan || {}).authored_by}`;
    } catch (e) {
      if (out) out.textContent = (e && e.message) || String(e);
      staff.disabled = false;
      return;
    }
    if (inst !== i) return;
    await refetch(i);                          // the new plan repaints the room
    if (i.inspectKey === key && i.byKey.has(key)) openPanel(i, key);
  };
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
    if (out) out.textContent = isGroup
      ? "running the union of the children's tests…" : "running the affected tests…";
    try {
      const r = await i.src.verify(key);
      if (out) out.textContent = (r.ok ? "✓ " : "✕ ") + (r.headline || "");
    } catch (e) {
      if (out) out.textContent = (e && e.message) || String(e);
    }
    vBtn.disabled = false;
    if (inst !== i) return;
    await refetch(i);                          // rings repaint from the recorded truth
    if (i.inspectKey === key) openPanel(i, key);
  };
}

// ---- events: click, keyboard, context menu — nothing else -----------------------
// No pointer capture, no zoom gesture, no drag: the Atlas listens for clicks on the
// room (delegated), keys on the screen element, and the right-click menu.
function wireEvents(i) {
  const onClick = (e) => {
    const door = e.target.closest && e.target.closest(".gr-door");
    if (door) {
      travel(i, door.getAttribute("data-door") || "", door.getAttribute("data-target") || "");
      return;
    }
    const card = e.target.closest && e.target.closest(".gr-card");
    if (card) activateCard(i, card);
  };
  const onAtlasClick = (e) => {
    const dot = e.target.closest && e.target.closest("[data-jump-node]");
    if (dot) {
      const key = dot.getAttribute("data-jump-node");
      const n = i.byKey.get(String(key)) || {};
      const room = n.node_type === "group" ? String(n.key) : String(n.parent_key || "");
      travel(i, room, n.node_type === "group" ? "" : String(key));
      return;
    }
    const roomBtn = e.target.closest && e.target.closest("[data-jump-room]");
    if (roomBtn) { travel(i, roomBtn.getAttribute("data-jump-room") || "", ""); return; }
    if (e.target === i.atlas) toggleAtlas(i, false);   // the backdrop closes it
  };
  const onKey = (e) => keyDown(i, e);
  const onCtx = (e) => contextMenu(i, e);
  const onResize = () => { if (inst === i && !i.transition) drawWires(i, false); };

  i.room.addEventListener("click", onClick);
  if (i.atlas) i.atlas.addEventListener("click", onAtlasClick);
  // Keys belong to THIS screen (it carries tabindex="-1" and is focused on open) —
  // a listener on the whole document would fire under every other screen too.
  // The context menu rides the screen too, so the graph's verbs never leak
  // under any other screen.
  i.screen.addEventListener("keydown", onKey);
  i.screen.addEventListener("contextmenu", onCtx);
  W.addEventListener("resize", onResize);

  i._teardown = () => {
    i.room.removeEventListener("click", onClick);
    if (i.atlas) i.atlas.removeEventListener("click", onAtlasClick);
    i.screen.removeEventListener("keydown", onKey);
    i.screen.removeEventListener("contextmenu", onCtx);
    W.removeEventListener("resize", onResize);
  };
}

/** One click, one meaning per card kind: a CHAMBER is a doorway (travel), a
 * CAPILLARY (and the aim) opens the full panel, the ARTIFACT opens the goal. */
function activateCard(i, cardEl) {
  const key = cardEl.getAttribute("data-key");
  i.focusKey = key;
  applyFocus(i);
  const kind = cardEl.getAttribute("data-kind");
  if (kind === "chamber") { drillTo(i, key); return; }
  if (kind === "artifact") { clearSel(i); i.inspectKey = null; setSel(i, key); asideConclusion(i); return; }
  openPanel(i, key);
}

// ---- keyboard: arrows move focus, Enter opens/enters, Esc climbs, M = map -------
function applyFocus(i, quiet) {
  i.cards.forEach((el) => el.classList.remove("gr-focus"));
  const el = i.focusKey && i.cards.get(String(i.focusKey));
  if (el) {
    el.classList.add("gr-focus");
    if (!quiet) el.scrollIntoView({ block: "nearest", behavior: reduceMotion() ? "auto" : "smooth" });
  }
}

/** Geometric focus movement: from the focused card's centre, the nearest card
 * centre in the pressed direction wins — DOM rects, not grid guesses, so it
 * matches exactly what the eye sees. */
function moveFocus(i, dx, dy) {
  const cur = i.focusKey && i.cards.get(String(i.focusKey));
  if (!cur) {
    const first = i.cards.keys().next();
    if (!first.done) { i.focusKey = first.value; applyFocus(i); }
    return;
  }
  const cr = cur.getBoundingClientRect();
  const cx = cr.left + cr.width / 2, cy = cr.top + cr.height / 2;
  let best = null, bestScore = Infinity;
  i.cards.forEach((el, key) => {
    if (el === cur) return;
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const vx = x - cx, vy = y - cy;
    const along = vx * dx + vy * dy;               // must be forward of the press
    if (along <= 4) return;
    const across = Math.abs(vx * dy) + Math.abs(vy * dx);
    const score = along + across * 2.5;            // prefer straight-ahead
    if (score < bestScore) { bestScore = score; best = key; }
  });
  if (best) { i.focusKey = best; applyFocus(i); }
}

function keyDown(i, e) {
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (e.target && e.target.isContentEditable)) return;
  if (e.key === "m" || e.key === "M") { toggleAtlas(i); e.preventDefault(); return; }
  if (i.atlasOpen) {
    if (e.key === "Escape") { toggleAtlas(i, false); e.preventDefault(); }
    return;
  }
  if (e.key === "Escape" || e.key === "Backspace") {
    if (i.inspectKey) { i.inspectKey = null; clearSel(i); asideDefault(i); }
    else if (i.level) drillTo(i, "");            // climb out of the room
    e.preventDefault();
    return;
  }
  if (e.key === "Enter") {
    const el = i.focusKey && i.cards.get(String(i.focusKey));
    if (el) activateCard(i, el);
    e.preventDefault();
    return;
  }
  const dir = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
  if (dir) { moveFocus(i, dir[0], dir[1]); e.preventDefault(); }
}

// ---- the right-click verb menu ---------------------------------------------------
/** Right-click IS the verb surface. On a card: Start, Stop, Peek, Test, Remove,
 * Replace. On the room floor: the room menu. The native menu is suppressed
 * inside the stage only — inputs and the aside keep the browser's own. */
function contextMenu(i, e) {
  const t = e.target;
  const tag = (t && t.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
      (t.closest && (t.closest("#graphAside") || t.closest(".gr-dialog")))) return;
  e.preventDefault();
  if (!W.studioMenu) return;
  const cardEl = t.closest && t.closest(".gr-card");
  if (cardEl) nodeMenu(i, e, cardEl.getAttribute("data-key"));
  else levelMenu(i, e);
}

function nodeMenu(i, e, key) {
  const n = i.byKey.get(String(key)) || {};
  const svc = n.service || null;         // optional — the payload may predate the backend
  const locked = !!(svc && svc.control === false);
  const why = (svc && svc.reason) || "";
  const state = (svc && svc.state) || "";
  const items = [];
  if (state) items.push({ label: `service: ${state}`, disabled: true });
  items.push(
    { label: "Start", disabled: locked || state === "running", title: locked ? why : "",
      act: () => serviceAction(i, key, "start") },
    { label: "Stop", disabled: locked || state === "stopped", title: locked ? why : "",
      act: () => serviceAction(i, key, "stop") },
    { sep: true },
    { label: "Peek", act: () => openPanel(i, key) },
    { label: "Test", act: () => verifyNode(i, key) },
    { sep: true },
    { label: "Remove", act: () => removeNodeFlow(i, key) },
    { label: "Replace ▸", act: () => replaceMenu(i, e, key) },
  );
  W.studioMenu(e.clientX, e.clientY, items);
}

function levelMenu(i, e) {
  W.studioMenu(e.clientX, e.clientY, [
    { label: "Open the Atlas map", act: () => toggleAtlas(i, true) },
    { label: "Toggle theme", act: () => applyTheme(i,
        i.screen.dataset.gtheme === "blueprint" ? "paper" : "blueprint") },
    { label: "Back to overview", disabled: !i.level, act: () => drillTo(i, "") },
  ]);
}

async function serviceAction(i, key, action) {
  try {
    const r = await i.src.service(key, action);
    const state = (r && ((r.service && r.service.state) || r.state)) || "done";
    W.toast && W.toast(`${key}: ${action} → ${state}`);
  } catch (er) { W.toast && W.toast((er && er.message) || String(er), true); }
  if (inst === i) scheduleRefetch(i);
}

/** The Test verb: the existing affected-only verify, the ring repainting after. */
async function verifyNode(i, key) {
  W.toast && W.toast(`testing ${key}…`);
  let r = null;
  try { r = await i.src.verify(key); }
  catch (er) { W.toast && W.toast((er && er.message) || String(er), true); }
  if (r) W.toast && W.toast((r.ok ? "✓ " : "✕ ") + (r.headline || "tests finished"), !r.ok);
  if (inst !== i) return;
  await refetch(i);                       // rings repaint from the recorded truth
}

async function removeNodeFlow(i, key) {
  const n = i.byKey.get(String(key)) || {};
  if (!W.confirm(`Remove '${n.title || key}' from the plan?\nThe change lands as a new plan version.`)) return;
  try { await i.src.removeNode(key); }
  catch (er) { W.toast && W.toast((er && er.message) || String(er), true); return; }
  W.toast && W.toast(`removed ${key} — repainting the room`);
  if (inst !== i) return;
  if (i.inspectKey === key) { i.inspectKey = null; clearSel(i); asideDefault(i); }
  await refetch(i);                       // node gone, new plan version in the header
}

function replaceMenu(i, e, key) {
  W.studioMenu(e.clientX + 26, e.clientY + 10, [
    { label: "Tech stack", act: () => replaceDialog(i, key, "stack", "Tech stack") },
    { label: "Test suite", act: () => replaceDialog(i, key, "tests", "Test suite") },
    { label: "Module", act: () => replaceDialog(i, key, "module", "Module") },
  ]);
}

// ---- the glass dialogs (replace note, agent picker) ------------------------------
function grDialog(i, titleText) {
  document.querySelectorAll(".gr-dialog").forEach((d) => d.remove());
  const box = document.createElement("div");
  box.className = "gr-dialog";
  const h = document.createElement("b");
  h.textContent = titleText;
  box.appendChild(h);
  // Keys typed into the dialog are the dialog's own — never the screen's shortcuts.
  box.addEventListener("keydown", (ev) => {
    ev.stopPropagation();
    if (ev.key === "Escape") box.remove();
  });
  i.wrap.parentElement.appendChild(box);           // the stage, not the room grid
  return box;
}

/** Replace = a TICKET, not a mutation: one honest note line, filed to the crew. */
function replaceDialog(i, key, aspect, label) {
  const box = grDialog(i, `Replace ${label} — ${key}`);
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "one line: what should the replacement do differently?";
  const row = document.createElement("div");
  row.className = "gr-dialog-row";
  const go = document.createElement("button");
  go.className = "primary";
  go.textContent = "File the ticket";
  const cancel = document.createElement("button");
  cancel.textContent = "Cancel";
  cancel.onclick = () => box.remove();
  row.append(go, cancel);
  box.append(input, row);
  input.focus();
  const submit = async () => {
    go.disabled = true;
    let r;
    try { r = await i.src.replaceAspect(key, aspect, input.value.trim()); }
    catch (er) {
      go.disabled = false;
      W.toast && W.toast((er && er.message) || String(er), true);
      return;
    }
    const pid = r && (r.project_id ?? (r.project || {}).id);
    W.toast && W.toast(`ticket filed${pid != null ? ` — #${pid}` : ""}`);
    box.textContent = "";
    const done = document.createElement("b");
    done.textContent = `✓ ticket filed${pid != null ? ` — #${pid}` : ""}`;
    const row2 = document.createElement("div");
    row2.className = "gr-dialog-row";
    if (pid != null && W.openProject) {
      const openB = document.createElement("button");
      openB.className = "primary";
      openB.textContent = `Open #${pid} →`;
      openB.onclick = () => { box.remove(); W.openProject(pid, "command"); };
      row2.appendChild(openB);
    }
    const closeB = document.createElement("button");
    closeB.textContent = "Close";
    closeB.onclick = () => box.remove();
    row2.appendChild(closeB);
    box.append(done, row2);
  };
  go.onclick = submit;
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") submit(); });
}

/** The agent picker — fed by the TEAM (GET /api/graph/self/team), assignment via
 * POST /node/{key}/agent. When the backend has not landed, it says so honestly. */
async function agentPicker(i, key) {
  const box = grDialog(i, `Change agent — ${key}`);
  const note = document.createElement("p");
  note.className = "dim";
  note.textContent = "pick from the team pool — the pool itself changes from the header bar";
  box.appendChild(note);
  let members = [];
  let failed = null;
  try { members = teamMembers(await i.src.team()); }
  catch (er) { failed = (er && er.message) || String(er); }
  if (inst !== i) { box.remove(); return; }
  if (failed || !members.length) {
    const p = document.createElement("p");
    p.className = "dim";
    p.textContent = failed
      ? `the team endpoint is not answering yet: ${failed}`
      : "the team has no members to offer";
    box.appendChild(p);
  }
  const list = document.createElement("div");
  list.className = "gr-picklist";
  for (const m of members) {
    const b = document.createElement("button");
    b.className = "gr-pick";
    const av = document.createElement("span");
    av.className = "gr-agent-avatar";
    av.textContent = (m.name.trim()[0] || "?").toUpperCase();
    const nm = document.createElement("span");
    nm.textContent = m.name;                       // textContent — names are free text
    b.append(av, nm);
    b.onclick = async () => {
      b.disabled = true;
      try { await i.src.setAgent(key, m.id); }
      catch (er) {
        b.disabled = false;
        W.toast && W.toast((er && er.message) || String(er), true);
        return;
      }
      W.toast && W.toast(`${m.name} now works ${key}`);
      box.remove();
      if (inst !== i) return;
      await refetch(i);
      if (i.inspectKey === key) openPanel(i, key);
    };
    list.appendChild(b);
  }
  const row = document.createElement("div");
  row.className = "gr-dialog-row";
  const cancel = document.createElement("button");
  cancel.textContent = "Cancel";
  cancel.onclick = () => box.remove();
  row.appendChild(cancel);
  box.append(list, row);
}

// The classic scripts' door in: core.js's openModuleGraph calls this, passing the
// room sub-path parsed from the hash.
W.ModuleGraph = { open, close };
