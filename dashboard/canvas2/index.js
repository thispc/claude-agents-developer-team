// Canvas v2 — orchestrator. Native SVG/DOM hit-testing + pointer capture. One gesture at a
// time; the browser routes every move to the captured element, so a drag can never be lost or
// stranded (pointercancel/lostpointercapture fire natively on blur/alt-tab). Selection, drag,
// connect and marquee all resolve via e.target — there is no hit graph to desync.
//
// mount(host, ctx) contract (ctx from js/canvas1.js): { worldId, roomId, room, agents, props,
//   getTool:()=>string, getDir:()=>string }. Downstream openers/menus/log use window.* directly.

import { createWorld, svgEl } from "./world.js";
import { node as buildNode, wireNode, setWireEnds, setPos, speechBubble, SIZES } from "./render.js";
import { hitTokenAt } from "./hit-test-utils.js";

const W = window;
const DRAG_THRESH = 4;              // px of screen travel before a press becomes a drag
const SNAP_DIST = 46;              // world units: drop within this of a free socket → seat
let ARM = null;                    // the live canvas instance (one open scene at a time)

const log = (cat, msg, extra, lvl) => { try { W.lwLog && W.lwLog(cat, "[v2] " + msg, extra || null, lvl || "info"); } catch (e) { /* */ } };

function tokenPositions(room, agents, props) {
  // seated agents ride their prop's slot; everyone else uses their stored pos (lwNodePos).
  const seated = {};
  (props || []).forEach((p) => {
    const slots = Number(p.slots) || 0;
    if (slots > 0 && Array.isArray(p.seated)) p.seated.forEach((aid, i) => { if (aid != null) seated[String(aid)] = { propId: String(p.id), slot: i }; });
  });
  const pos = new Map();
  (props || []).forEach((p, i) => { const q = W.lwNodePos(p, i, "prop"); pos.set("prop:" + p.id, q); });
  (agents || []).forEach((a, i) => {
    const s = seated[String(a.id)];
    if (s) {
      const pp = pos.get("prop:" + s.propId) || { x: 0, y: 0 };
      const off = (W.lwSlotPositions(props.find((x) => String(x.id) === s.propId) || {})[s.slot]) || { x: 0, y: 0 };
      pos.set("agent:" + a.id, { x: pp.x + off.x, y: pp.y + off.y });
    } else pos.set("agent:" + a.id, W.lwNodePos(a, i, "agent"));
  });
  return pos;
}

export function mount(host, ctx) {
  if (ARM) destroy();
  const world = createWorld(host);
  const { gTokens, gWires, gOverlay, svg } = world.el;
  const inst = {
    world, ctx, host,
    tokens: new Map(),     // id(str) -> { kind, data, g, x, y }
    edges: [],             // { tid, a, b, dir, closed, g }
    sel: new Set(),        // selected token ids (strings)
    selEdge: null,         // { tid, a, b } or null
    handles: [],           // overlay handle elements (single selection)
    gesture: null,         // active pointer gesture
    space: false,
    viewKey: "lw2:" + ctx.worldId + ":" + ctx.roomId,
  };
  ARM = inst;
  W.LWCanvas2._inst = inst;

  refresh(inst, ctx.room, ctx.agents, ctx.props);

  // restore saved view, else frame everything
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(inst.viewKey) || "null"); } catch (e) { /* */ }
  if (saved) world.setView(saved); else world.fit(sceneBounds(inst));

  wireEvents(inst);
  log("life", "canvas mounted", { agents: (ctx.agents || []).length, props: (ctx.props || []).length });
  return inst;
}

export function destroy() {
  const inst = ARM; if (!inst) return;
  if (inst._speechTimers) inst._speechTimers.forEach((t) => clearTimeout(t));
  try { inst._teardown && inst._teardown(); } catch (e) { /* */ }
  try { inst.world.destroy(); } catch (e) { /* */ }
  if (W.LWCanvas2._inst === inst) W.LWCanvas2._inst = null;
  ARM = null;
}

// ---- scene build ----------------------------------------------------------
function refresh(inst, room, agents, props) {
  inst.ctx.room = room; inst.ctx.agents = agents; inst.ctx.props = props;
  const { gTokens, gWires } = inst.world.el;
  gTokens.innerHTML = ""; gWires.innerHTML = "";
  inst.tokens.clear(); inst.edges = [];
  const pos = tokenPositions(room, agents, props);

  (props || []).forEach((p) => addToken(inst, "prop", p, pos.get("prop:" + p.id)));
  (agents || []).forEach((a) => addToken(inst, "agent", a, pos.get("agent:" + a.id)));
  (room.threads || []).forEach((t) => (t.edges || []).forEach((e) => addEdge(inst, t, e)));
  showActivity(inst, room);
  reselect(inst);
}

// A bubble says what an agent is DOING — not what it said.
//
// It used to pop the whole of the last round's speech, on every agent, every repaint: six
// bubbles of transcript on top of the graph, which is unreadable as a picture and redundant
// with the conversation panel, where the words belong and can be scrolled and quoted. And it
// left no way to answer the question the canvas IS good at: which of these is working?
//
// So a bubble now appears only on an agent the register says is busy, and it carries the
// activity — "thinking", "building: fix the k8s reaper". One glance, one fact each.
function showActivity(inst, room) {
  if (inst._speechTimers) inst._speechTimers.forEach((t) => clearTimeout(t));
  inst._speechTimers = [];
  for (const a of room.agents || []) {
    const t = inst.tokens.get(String(a.id));
    const act = a.activity || {};
    if (!t || !act.busy) continue;
    const label = act.what ? `${act.state}: ${act.what}` : act.state;
    t.g.appendChild(speechBubble(label, 0));
  }
}

function addToken(inst, kind, data, p) {
  p = p || { x: 0, y: 0 };
  const g = buildNode(kind, data);
  setPos(g, p.x, p.y);
  inst.world.el.gTokens.appendChild(g);
  inst.tokens.set(String(data.id), { kind, data, g, x: p.x, y: p.y });
}

function addEdge(inst, t, e) {
  const A = inst.tokens.get(String(e[0])), B = inst.tokens.get(String(e[1]));
  if (!A || !B) return;
  const edge = { tid: t.id, a: String(e[0]), b: String(e[1]), dir: e[2] || "both", closed: !!t.closed };
  edge.g = wireNode({ tid: t.id, a: edge.a, b: edge.b, dir: edge.dir, closed: edge.closed, ax: A.x, ay: A.y, bx: B.x, by: B.y });
  inst.world.el.gWires.appendChild(edge.g);
  inst.edges.push(edge);
}

function updateWiresFor(inst, id) {
  inst.edges.forEach((edge) => {
    if (edge.a !== id && edge.b !== id) return;
    const A = inst.tokens.get(edge.a), B = inst.tokens.get(edge.b);
    if (A && B) setWireEnds(edge.g, A.x, A.y, B.x, B.y);
  });
}

function sceneBounds(inst) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  inst.tokens.forEach((t) => { minX = Math.min(minX, t.x - 60); minY = Math.min(minY, t.y - 60); maxX = Math.max(maxX, t.x + 60); maxY = Math.max(maxY, t.y + 80); });
  return { minX, minY, maxX, maxY };
}

// ---- selection ------------------------------------------------------------
function clearSel(inst) {
  inst.sel.forEach((id) => { const t = inst.tokens.get(id); if (t) t.g.classList.remove("lw2-sel"); });
  inst.sel.clear(); updateHandles(inst);
}
function addSel(inst, id) { const t = inst.tokens.get(id); if (t) { t.g.classList.add("lw2-sel"); inst.sel.add(id); } updateHandles(inst); }
function setSel(inst, id) { clearSel(inst); addSel(inst, id); }
function toggleSel(inst, id) { if (inst.sel.has(id)) { inst.sel.delete(id); const t = inst.tokens.get(id); if (t) t.g.classList.remove("lw2-sel"); updateHandles(inst); } else addSel(inst, id); }
function reselect(inst) { const keep = [...inst.sel]; inst.sel.clear(); keep.forEach((id) => addSel(inst, id)); }

function clearGraphUI(inst) {
  inst.selEdge = null;
  inst.edges.forEach((e) => e.g.classList.remove("lw2-wire-sel"));
  try { W.lwHideActions && W.lwHideActions(); } catch (e) { /* */ }
}

// The 4 connection handles around a single selection (overlay layer, in world coords).
function updateHandles(inst) {
  inst.handles.forEach((h) => h.remove()); inst.handles = [];
  if (inst.sel.size !== 1 || inst.ctx.getTool() !== "select") return;
  const t = inst.tokens.get([...inst.sel][0]); if (!t) return;
  const reach = tokenRadius(t) + 10;
  [[0, -reach], [reach, 0], [0, reach], [-reach, 0]].forEach(([dx, dy]) => {
    const dot = svgEl("circle", { class: "lw2-handle", cx: t.x + dx, cy: t.y + dy, r: 6, "data-id": t.data.id });
    inst.world.el.gOverlay.appendChild(dot); inst.handles.push(dot);
  });
}
function tokenRadius(t) {
  if (t.kind === "agent") return SIZES.AGENT_R + 6;
  const slots = Number(t.data.slots) || 0, shape = String(t.data.shape || "circle");
  if (slots > 0) return shape === "rect" ? Math.hypot(SIZES.RECT_W, SIZES.RECT_H) / 2 : SIZES.TABLE_R;
  return 34;
}

function selectEdge(inst, wireEl) {
  clearSel(inst); clearGraphUI(inst);
  const tid = wireEl.getAttribute("data-tid"), a = wireEl.getAttribute("data-a"), b = wireEl.getAttribute("data-b");
  inst.selEdge = { tid, a, b };
  wireEl.classList.add("lw2-wire-sel");
  try {
    W.lwShowActions && W.lwShowActions("connection", [{ label: "✕ Remove", danger: true, onClick: async () => {
      try { await W.api(`/api/lw/${inst.ctx.worldId}/room/${inst.ctx.roomId}/thread/disconnect`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ a, b }) }); W.sdFlash && W.sdFlash(); W.lwReloadRoom && W.lwReloadRoom(); }
      catch (e) { W.toast && W.toast("Could not remove: " + e.message); }
    } }]);
  } catch (e) { /* */ }
  log("select", "edge " + tid, { a, b });
}

// The graph's actions, shown when the selection IS a graph.
//
// This used to be bound to double-click, which meant double-clicking an agent could never
// open that agent — the graph swallowed the gesture. Double-click is now unambiguously
// "show me this one", and a graph reveals its actions when you have actually selected it:
// marquee across its nodes, and once the selection covers a whole thread the Run/Chat/Rules
// panel appears. Selecting a thing to act on it is the more ordinary idea anyway; the old
// binding was a shortcut that had quietly taken the obvious gesture hostage.
function graphActionsFor(inst, threadId) {
  const threads = (inst.ctx.room.threads) || [];
  const t = threads.find((th) => th.id === threadId);
  if (!t) return false;
  try {
    // No Run, no Chat. Running is what happens when you talk to someone in the graph — a
    // button that does it separately is a second way to do one thing, and the one people
    // reach for is the sentence they were going to type anyway. Chat is always on the right.
    W.lwShowActions && W.lwShowActions("graph", [
      { label: "⚙ Rules", onClick: () => W.sdOpenThreads && W.sdOpenThreads(t.id) },
      { label: "✕ Delete graph", danger: true, onClick: async () => {
        if (!confirm("Delete this graph? The tokens stay; only the connections go.")) return;
        try { await W.api(`/api/lw/${inst.ctx.worldId}/room/${inst.ctx.roomId}/thread/${t.id}`, { method: "DELETE" }); W.sdFlash && W.sdFlash(); W.lwReloadRoom && W.lwReloadRoom(); }
        catch (e) { W.toast && W.toast("Could not delete: " + e.message); }
      } },
    ]);
  } catch (e) { /* */ }
  return true;
}

/** Which thread the current selection completely covers, if any.
 *
 * Deliberately "covers the whole graph", not "touches one of its nodes": a partial selection
 * is usually somebody halfway through a marquee, and popping a Delete-graph button up under
 * a moving cursor is how accidents happen. */
function selectedThread(inst) {
  const threads = (inst.ctx.room.threads) || [];
  if (inst.sel.size < 2) return null;
  for (const t of threads) {
    const members = new Set(t.edges.flatMap((e) => [String(e[0]), String(e[1])]));
    if (members.size !== inst.sel.size) continue;
    if ([...members].every((m) => inst.sel.has(m))) return t;
  }
  return null;
}

/** Point the conversation panel at whoever was just selected, and at the graph they are in.
 * Selecting somebody and then typing is the whole interaction now, so the two have to be the
 * same gesture. */
function aimChat(inst, id, kind) {
  if (kind !== "agent") return;
  const t = inst.tokens.get(id);
  const threads = (inst.ctx.room.threads) || [];
  const owner = threads.find((th) => (th.edges || []).some((e) => String(e[0]) === id || String(e[1]) === id));
  if (t && W.sdChatAim) W.sdChatAim(t.data, owner ? owner.id : 0);
}

/** Called whenever the selection settles.
 *
 * With a whole graph selected, its actions. With ONE of its nodes selected, a single button
 * that selects the rest — because "marquee across it" is not a thing anyone guesses, and a
 * capability nobody can find is one that does not exist. Clicking a node is how you find out
 * it is part of something. */
function offerGraphActions(inst) {
  // Nothing selected means you are addressing the room, so the conversation goes back to the
  // manager. Leaving it aimed at whoever you last clicked is how you end up asking one agent
  // a question you meant for the team.
  if (!inst.sel.size && W.sdChatAim) W.sdChatAim(null, 0);
  const t = selectedThread(inst);
  if (t) { graphActionsFor(inst, t.id); return; }
  clearGraphUI(inst);
  if (inst.sel.size !== 1) return;
  const id = [...inst.sel][0];
  const threads = (inst.ctx.room.threads) || [];
  const owner = threads.find((th) => (th.edges || []).some((e) => String(e[0]) === id || String(e[1]) === id));
  if (!owner) return;
  const members = [...new Set(owner.edges.flatMap((e) => [String(e[0]), String(e[1])]))];
  try {
    W.lwShowActions && W.lwShowActions("graph", [
      { label: `◌ Select its graph (${members.length})`, onClick: () => {
        clearSel(inst); members.forEach((m) => addSel(inst, m)); offerGraphActions(inst);
      } },
    ]);
  } catch (e) { /* the canvas works without a panel */ }
}

// ---- events / gestures ----------------------------------------------------
function wireEvents(inst) {
  const { svg } = inst.world.el;
  const onDown = (e) => pointerDown(inst, e);
  const onMove = (e) => pointerMove(inst, e);
  const onUp = (e) => pointerUp(inst, e);
  const onWheel = (e) => { e.preventDefault(); inst.world.zoomAt(e.clientX, e.clientY, e.deltaY > 0 ? 1 / 1.1 : 1.1); saveView(inst); };
  const onDbl = (e) => dblClick(inst, e);
  const onCtx = (e) => contextMenu(inst, e);
  const onKey = (e) => keyDown(inst, e);
  const onKeyUp = (e) => { if (e.code === "Space") inst.space = false; };

  svg.addEventListener("pointerdown", onDown);
  svg.addEventListener("pointermove", onMove);
  svg.addEventListener("pointerup", onUp);
  svg.addEventListener("pointercancel", onUp);
  svg.addEventListener("lostpointercapture", onUp);
  svg.addEventListener("wheel", onWheel, { passive: false });
  svg.addEventListener("dblclick", onDbl);
  svg.addEventListener("contextmenu", onCtx);
  document.addEventListener("keydown", onKey);
  document.addEventListener("keyup", onKeyUp);

  inst._teardown = () => {
    svg.removeEventListener("pointerdown", onDown); svg.removeEventListener("pointermove", onMove);
    svg.removeEventListener("pointerup", onUp); svg.removeEventListener("pointercancel", onUp);
    svg.removeEventListener("lostpointercapture", onUp); svg.removeEventListener("wheel", onWheel);
    svg.removeEventListener("dblclick", onDbl); svg.removeEventListener("contextmenu", onCtx);
    document.removeEventListener("keydown", onKey); document.removeEventListener("keyup", onKeyUp);
  };
}

function pointerDown(inst, e) {
  if (e.button === 2) return;                                  // context menu handles right-click
  const svg = inst.world.el.svg;
  svg.setPointerCapture(e.pointerId);                          // route ALL further events here — no lost moves
  const tool = inst.ctx.getTool();

  // pan: middle button or space-drag
  if (e.button === 1 || inst.space) { inst.gesture = { type: "pan", x: e.clientX, y: e.clientY }; inst.host.classList.add("lw2-panning"); return; }
  if (e.button !== 0) return;

  if (tool === "select") {
    const handleEl = e.target.closest && e.target.closest(".lw2-handle");
    if (handleEl) { beginConnect(inst, e, handleEl.getAttribute("data-id")); return; }
    const tokEl = hitTokenAt(e.clientX, e.clientY);
    if (tokEl) { pressToken(inst, e, tokEl); return; }
    const wireEl = e.target.closest && e.target.closest(".lw2-wire");
    if (wireEl) { selectEdge(inst, wireEl); inst.gesture = { type: "tap" }; return; }
    // empty floor → clear + marquee
    clearGraphUI(inst); if (!e.shiftKey) clearSel(inst);
    beginMarquee(inst, e);
    return;
  }
  // placement tools (agent/artifact): a click on empty floor asks the create flow (js/canvas1.js) to start creation
  if (e.target === svg || !hitTokenAt(e.clientX, e.clientY)) {
    const w = inst.world.toWorld(e.clientX, e.clientY);
    inst.gesture = { type: "place", world: w };
  }
}

function nowMs() { return (W.performance && performance.now) ? performance.now() : Date.now(); }
function pressToken(inst, e, tokEl) {
  const id = tokEl.getAttribute("data-id"), kind = tokEl.getAttribute("data-kind");
  // Whether this press MIGHT complete a double-click is decided here, but the double-click only
  // FIRES on pointerup-without-moving (so a click-then-drag is a drag, not a mis-read dbl). With
  // pointer capture the synthesized dblclick event is unreliable, so we track taps ourselves.
  const maybeDouble = !!(inst._lastClick && inst._lastClick.id === id && (nowMs() - inst._lastClick.t) < 350);
  const already = inst.sel.has(id), grouped = inst.sel.size >= 2 && already && !e.shiftKey;
  clearGraphUI(inst);
  if (e.shiftKey) toggleSel(inst, id);
  else if (!already) setSel(inst, id);
  // arm a drag: members = the whole selection if this token is in a 2+ selection, else just it
  const members = (grouped || inst.sel.size >= 2) ? [...inst.sel] : [id];
  inst.gesture = { type: "arm", id, kind, grouped, maybeDouble, start: { x: e.clientX, y: e.clientY },
    members: members.map((mid) => { const t = inst.tokens.get(mid); return { id: mid, kind: t.kind, x0: t.x, y0: t.y }; }),
    moved: false, world0: inst.world.toWorld(e.clientX, e.clientY) };
}
function fireDoubleClick(inst, id, kind) {
  // Always the thing you double-clicked. No exceptions and no "unless it happens to be in a
  // graph" — a gesture that means two different things depending on invisible state is a
  // gesture people stop trusting.
  const t = inst.tokens.get(id); if (!t) return;
  if (kind === "agent") W.openAgentPage && W.openAgentPage(t.data.id);
  else W.lwOpenArtifactPeek && W.lwOpenArtifactPeek(t.data.id);
}

function beginMarquee(inst, e) {
  const w = inst.world.toWorld(e.clientX, e.clientY);
  const rect = svgEl("rect", { class: "lw2-marquee", x: w.x, y: w.y, width: 0, height: 0 });
  inst.world.el.gOverlay.appendChild(rect);
  inst.gesture = { type: "marquee", x0: w.x, y0: w.y, rect, add: e.shiftKey };
}

function beginConnect(inst, e, fromId) {
  const from = inst.tokens.get(fromId); if (!from) return;
  const dir = inst.ctx.getDir();
  const rubber = svgEl("line", { class: "lw2-rubber", x1: from.x, y1: from.y, x2: from.x, y2: from.y,
    "marker-end": dir === "one" ? "url(#lw2-arrow)" : null });
  inst.world.el.gOverlay.appendChild(rubber);
  inst.handles.forEach((h) => h.remove()); inst.handles = [];
  inst.gesture = { type: "connect", fromId, rubber };
  inst.host.classList.add("lw2-connecting");
}

function pointerMove(inst, e) {
  const g = inst.gesture; if (!g) return;
  if (g.type === "pan") { inst.world.panBy(e.clientX - g.x, e.clientY - g.y); g.x = e.clientX; g.y = e.clientY; return; }
  const w = inst.world.toWorld(e.clientX, e.clientY);
  if (g.type === "marquee") {
    g.rect.setAttribute("x", Math.min(w.x, g.x0)); g.rect.setAttribute("y", Math.min(w.y, g.y0));
    g.rect.setAttribute("width", Math.abs(w.x - g.x0)); g.rect.setAttribute("height", Math.abs(w.y - g.y0));
    return;
  }
  if (g.type === "connect") { g.rubber.setAttribute("x2", w.x); g.rubber.setAttribute("y2", w.y); return; }
  if (g.type === "arm") {
    if (!g.moved && Math.hypot(e.clientX - g.start.x, e.clientY - g.start.y) <= DRAG_THRESH) return;
    if (!g.moved) { g.moved = true; inst.host.classList.add("lw2-dragging"); clearGraphUI(inst); showPortal(inst, true); }
    const dx = w.x - g.world0.x, dy = w.y - g.world0.y;
    g.members.forEach((m) => moveToken(inst, m.id, m.x0 + dx, m.y0 + dy));
    if (g.members.length === 1 && g.kind === "agent") g.snap = snapScan(inst, g.id);
    portalOver(inst, e);
    return;
  }
}

function moveToken(inst, id, x, y) {
  const t = inst.tokens.get(id); if (!t) return;
  t.x = x; t.y = y; setPos(t.g, x, y);
  updateWiresFor(inst, id);
  if (t.kind === "prop") followProp(inst, t);   // a table carries its seated ring
  if (inst.sel.has(id)) updateHandles(inst);
}
function followProp(inst, tp) {
  const p = tp.data, slots = Number(p.slots) || 0; if (!(slots > 0) || !Array.isArray(p.seated)) return;
  const pos = W.lwSlotPositions(p);
  p.seated.forEach((aid, i) => { if (aid == null) return; const off = pos[i] || { x: 0, y: 0 }; moveToken(inst, String(aid), tp.x + off.x, tp.y + off.y); });
}

// nearest free socket for a dragging agent; glow it; return {propId,slot,x,y} or null
function snapScan(inst, agentId) {
  let best = null, bestD = SNAP_DIST;
  const at = inst.tokens.get(agentId); if (!at) return null;
  inst.tokens.forEach((tp) => {
    if (tp.kind !== "prop") return;
    const slots = Number(tp.data.slots) || 0; if (!(slots > 0)) return;
    const seated = Array.isArray(tp.data.seated) ? tp.data.seated : [], pos = W.lwSlotPositions(tp.data);
    for (let i = 0; i < slots; i++) {
      if (seated[i] != null && String(seated[i]) !== agentId) continue;
      const off = pos[i] || { x: 0, y: 0 }, sx = tp.x + off.x, sy = tp.y + off.y, d = Math.hypot(at.x - sx, at.y - sy);
      if (d < bestD) { bestD = d; best = { propId: String(tp.data.id), slot: i, x: sx, y: sy }; }
    }
  });
  inst.world.el.gTokens.querySelectorAll(".lw2-snap").forEach((n) => n.classList.remove("lw2-snap"));
  if (best) { const tp = inst.tokens.get(best.propId); if (tp) tp.g.classList.add("lw2-snap"); }
  return best;
}

async function pointerUp(inst, e) {
  const g = inst.gesture; inst.gesture = null;
  try { inst.world.el.svg.releasePointerCapture(e.pointerId); } catch (er) { /* */ }
  inst.host.classList.remove("lw2-panning", "lw2-dragging", "lw2-connecting");
  if (!g) return;
  if (g.type === "pan") { saveView(inst); return; }
  if (g.type === "tap") return;
  if (g.type === "place") { const w = inst.world.toWorld(e.clientX, e.clientY); try { W.lwStartCreate && W.lwStartCreate(inst.ctx.getTool(), w); } catch (er) { /* */ } return; }
  if (g.type === "marquee") { endMarquee(inst, g); return; }
  if (g.type === "connect") { await endConnect(inst, g, e); return; }
  if (g.type === "arm") {
    if (!g.moved) {                                           // released without moving → a click
      if (g.maybeDouble) { inst._lastClick = null; fireDoubleClick(inst, g.id, g.kind); }
      else {
        inst._lastClick = { id: g.id, t: nowMs() };           // remember for a possible next-click double
        offerGraphActions(inst);                             // a click is also a selection
            aimChat(inst, g.id, g.kind);                     // ...and aims the conversation
        if (g.kind === "agent") {                        // ...and shows the five facts
          const tk = inst.tokens.get(g.id);
          if (tk && W.lwPeekOpen) W.lwPeekOpen(tk.data);
        } else if (W.lwPeekClose) W.lwPeekClose();
      }
      return;
    }
    inst._lastClick = null;                                   // a drag is not part of a double-click
    showPortal(inst, false);
    inst.world.el.gTokens.querySelectorAll(".lw2-snap").forEach((n) => n.classList.remove("lw2-snap"));
    if (inst._overPortal) { await deleteMembers(inst, g.members); return; }
    await commitDrag(inst, g);
  }
}

function endMarquee(inst, g) {
  const bx = +g.rect.getAttribute("x"), by = +g.rect.getAttribute("y"), bw = +g.rect.getAttribute("width"), bh = +g.rect.getAttribute("height");
  g.rect.remove();
  if (bw > 4 || bh > 4) {
    if (!g.add) clearSel(inst);
    inst.tokens.forEach((t, id) => { if (t.x >= bx && t.x <= bx + bw && t.y >= by && t.y <= by + bh) addSel(inst, id); });
  }
  offerGraphActions(inst);
  log("marquee", "end", { w: Math.round(bw), h: Math.round(bh), sel: inst.sel.size });
}

async function commitDrag(inst, g) {
  // seat a single agent if it was dropped on a free socket
  if (g.members.length === 1 && g.kind === "agent" && g.snap) {
    const a = inst.tokens.get(g.id);
    try {
      await W.api(`/api/lw/${inst.ctx.worldId}/artifact/${g.snap.propId}/seat`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slot: g.snap.slot, human_id: a.data.id }) });
      await W.api(`/api/lw/${inst.ctx.worldId}/pos`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: a.data.id, x: g.snap.x, y: g.snap.y }) });
      W.sdFlash && W.sdFlash(); W.lwReloadRoom && W.lwReloadRoom(); return;
    } catch (er) { W.toast && W.toast("Could not seat them: " + er.message); }
  }
  // else persist every moved member's position
  await Promise.all(g.members.map((m) => {
    const t = inst.tokens.get(m.id); if (!t) return null;
    return W.api(`/api/lw/${inst.ctx.worldId}/pos`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: t.data.id, x: t.x, y: t.y }) }).catch(() => {});
  }));
  saveView(inst);
  log("drag", "drop", { n: g.members.length });
}

async function endConnect(inst, g, e) {
  g.rubber.remove();
  const w = inst.world.toWorld(e.clientX, e.clientY);
  let target = null, bestD = Infinity;
  inst.tokens.forEach((t, id) => { if (id === g.fromId) return; const d = Math.hypot(t.x - w.x, t.y - w.y); if (d < Math.max(tokenRadius(t) + 10, 46) && d < bestD) { bestD = d; target = id; } });
  updateHandles(inst);
  if (!target) { log("connect", "cancelled"); return; }
  const dir = inst.ctx.getDir();
  try {
    await W.api(`/api/lw/${inst.ctx.worldId}/room/${inst.ctx.roomId}/thread/connect`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ a: g.fromId, b: target, dir: dir === "one" ? "a2b" : "both" }) });
    W.sdFlash && W.sdFlash(); W.lwReloadRoom && W.lwReloadRoom();
  } catch (er) { W.toast && W.toast("Could not connect: " + er.message); }
}

// ---- delete portal (drag to the right edge) -------------------------------
function portalEl() { return document.getElementById("sdPortal") || null; }
function showPortal(inst, on) { try { W.lwPortalEl && W.lwPortalEl(); } catch (e) { /* */ } const el = portalEl(); if (el) el.classList.toggle("active", !!on); if (!on) { inst._overPortal = false; if (el) el.classList.remove("hot"); } }
function portalOver(inst, e) {
  const el = portalEl(); if (!el) return;
  const r = inst.host.getBoundingClientRect();
  const over = (e.clientX - r.left) >= r.width - 130;
  inst._overPortal = over; el.classList.toggle("hot", over);
}
async function deleteMembers(inst, members) {
  showPortal(inst, false);
  const ids = members.map((m) => { const t = inst.tokens.get(m.id); return t && t.data.id; }).filter((x) => x != null);
  let ok = 0;
  await Promise.all(ids.map((id) => W.api(`/api/lw/${inst.ctx.worldId}/entity/${id}`, { method: "DELETE" }).then(() => { ok++; }).catch(() => {})));
  W.toast && W.toast(ok === ids.length ? `Deleted ${ok} ${ok === 1 ? "thing" : "things"}` : `Deleted ${ok} of ${ids.length} — the rest rolled back`);
  W.sdFlash && W.sdFlash(); W.lwReloadRoom && W.lwReloadRoom();
}

// ---- double-click / context menu / keys -----------------------------------
function dblClick(inst, e) {
  // Token double-click is handled in pressToken/pointerUp (pointer-capture makes the native
  // dblclick target unreliable). This stays only to swallow the browser's default dbl behaviour.
  e.preventDefault();
}
function contextMenu(inst, e) {
  e.preventDefault();
  const tokEl = hitTokenAt(e.clientX, e.clientY);
  if (tokEl) {
    const t = inst.tokens.get(tokEl.getAttribute("data-id"));
    if (t.kind === "agent") W.lwAgentMenu && W.lwAgentMenu(e, t.data);
    else W.lwPropMenu && W.lwPropMenu(e, t.data);
    return;
  }
  if (W.lwFloorMenu) { const w = inst.world.toWorld(e.clientX, e.clientY); W.lwFloorMenu(e, w); }
}
function keyDown(inst, e) {
  if (document.getElementById("lifeworld") && document.getElementById("lifeworld").hidden) return;
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable)) return;
  if (e.code === "Space") { inst.space = true; return; }
  if (e.key === "Escape") { clearSel(inst); clearGraphUI(inst); return; }
  if (e.key === "f" || e.key === "F") { inst.world.fit(sceneBounds(inst)); saveView(inst); return; }
  if ((e.key === "Delete" || e.key === "Backspace") && inst.sel.size) {
    e.preventDefault();
    deleteMembers(inst, [...inst.sel].map((id) => ({ id }))); return;
  }
  if (e.key === "Enter" && inst.sel.size) { const t = inst.tokens.get([...inst.sel][0]); if (t) { if (t.kind === "agent") W.openPersonDrawer && W.openPersonDrawer(t.data.id, t.data.name); else W.lwOpenArtifactPeek && W.lwOpenArtifactPeek(t.data.id); } }
}

function saveView(inst) { try { localStorage.setItem(inst.viewKey, JSON.stringify(inst.world.getView())); } catch (e) { /* */ } }

// public bridge
W.LWCanvas2 = {
  mount, destroy,
  _inst: null,
  setTool() { if (ARM) updateHandles(ARM); },
  setDir() { /* read live via getDir at connect-time */ },
  reflow(room, agents, props) { if (ARM) refresh(ARM, room, agents, props); },
};
