// Module graph — orchestrator. The platform rendered as a living graph of verified
// modules: aim → modules (each with its own suite, agent, config) → conclusion.
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

import { createWorld, svgEl } from "../canvas2/world.js";
import { wireNode, setWireEnds, setPos, speechBubble, SIZES } from "../canvas2/render.js";
import { buildNode, updateNode, nodeSize, GLYPH } from "./nodes.js";
import { layout, topoOrder } from "./layout.js";

const W = window;
const DRAG_THRESH = 4;                 // px of screen travel before a press becomes a drag
const WIRE_GAP = SIZES.AGENT_R + 9;    // render.js pulls this much off each wire end
const reduceMotion = () =>
  W.matchMedia && W.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Which plan_id:key pairs have already had their entrance. Module state, so a new
// plan version replays the reveal but a mere route-away-and-back does not.
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

// ---- lifecycle ------------------------------------------------------------------
function open(sourceName) {
  close();
  const screen = document.getElementById("graphScreen");
  const host = document.getElementById("graphCanvas");
  const aside = document.getElementById("graphAside");
  if (!screen || !host || !aside) return;
  const world = createWorld(host);
  inst = {
    src: SOURCES[sourceName] || DEVTEAM_GRAPH_SRC,
    screen, host, aside, world,
    nodes: new Map(),          // key -> { key, data, g, x, y }
    edges: [],                 // { a, b, g }
    sel: new Set(),
    inspectKey: null,
    gesture: null, space: false,
    data: null, firstPaint: true,
    revealQueue: [], revealTimer: null,
    pollTimer: null, refetchTimer: null,
    viewKey: "gr:view:" + (SOURCES[sourceName] ? sourceName : "self"),
  };
  wireEvents(inst);
  asideDefault(inst);
  const back = document.getElementById("graphBack");
  if (back) back.onclick = () => { location.hash = "#/hq"; };
  // Live updates: the classic scripts own the socket; connectWs re-broadcasts
  // graph/repair kinds as DOM CustomEvents, which a module CAN hear.
  inst._onGraphEvent = (ev) => { if (inst) onGraphEvent(inst, ev.detail || {}); };
  document.addEventListener("graph-event", inst._onGraphEvent);
  // ...and a 6s poll as the fallback, PAUSED while something is selected so a
  // repaint never clears a selection or blanks the inspector mid-sentence.
  inst.pollTimer = setInterval(() => {
    if (!inst || inst.sel.size || inst.inspectKey || inst.gesture) return;
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
  paint(i, data);
}

function scheduleRefetch(i) {
  if (i.refetchTimer) return;
  i.refetchTimer = setTimeout(() => {
    i.refetchTimer = null;
    if (!inst || inst.sel.size || inst.inspectKey || inst.gesture) return;
    refetch(inst);
  }, 800);
}

// ---- painting -------------------------------------------------------------------
function paint(i, data) {
  i.data = data;
  const planId = data.plan && data.plan.id;
  const pos = layout(data.nodes || [], data.edges || [], data.positions || {});
  const live = new Set();
  for (const n of data.nodes || []) {
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
  rebuildWires(i);
  showActivity(i);
  reselect(i);
  const planInfo = document.getElementById("graphPlanInfo");
  if (planInfo && data.plan) planInfo.textContent =
    `v${data.plan.version} · by ${data.plan.authored_by || "seed"}`;
  const fresh = topoOrder(data.nodes || [], data.edges || [])
    .filter((k) => !SEEN.has(planId + ":" + k));
  if (fresh.length) reveal(i, planId, fresh);
  if (i.firstPaint) { i.firstPaint = false; restoreView(i); }
}

/** The staged entrance: nodes appear one at a time in topo order (~400ms steps,
 * a CSS scale/opacity transition), wires drawing only once both ends exist.
 * Under prefers-reduced-motion — the check comes FIRST, before any stepper is
 * armed — everything simply appears. */
function reveal(i, planId, freshKeys) {
  if (reduceMotion()) {
    for (const k of freshKeys) {
      SEEN.add(planId + ":" + k);
      const t = i.nodes.get(k);
      if (t) t.g.classList.remove("gr-hidden");
    }
    rebuildWires(i);
    return;
  }
  for (const k of freshKeys)
    if (!i.revealQueue.some((q) => q[1] === k)) i.revealQueue.push([planId, k]);
  if (i.revealTimer) return;            // the stepper is already walking
  const step = () => {
    i.revealTimer = null;
    const q = i.revealQueue.shift();
    if (!q) { rebuildWires(i); return; }
    SEEN.add(q[0] + ":" + q[1]);
    const t = i.nodes.get(q[1]);
    if (t) {
      void t.g.getBoundingClientRect();          // commit the hidden state first
      t.g.classList.remove("gr-hidden");         // → CSS transition plays
    }
    rebuildWires(i);
    i.revealTimer = setTimeout(step, 400);
  };
  i.revealTimer = setTimeout(step, 60);
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

function rebuildWires(i) {
  const gW = i.world.el.gWires;
  gW.innerHTML = "";
  i.edges = [];
  ((i.data && i.data.edges) || []).forEach((e, idx) => {
    const s = String(e.src ?? e.src_key ?? ""), d = String(e.dst ?? e.dst_key ?? "");
    const A = i.nodes.get(s), B = i.nodes.get(d);
    if (!A || !B) return;
    if (A.g.classList.contains("gr-hidden") || B.g.classList.contains("gr-hidden")) return;
    const a1 = anchor(A, B, nodeSize(A.data)), b1 = anchor(B, A, nodeSize(B.data));
    const g = wireNode({ tid: idx, a: s, b: d, dir: "a2b", closed: true,
                         ax: a1.x, ay: a1.y, bx: b1.x, by: b1.y });
    g.classList.add("gr-wire", "gr-wire-" + (e.edge_type || "depends"));
    gW.appendChild(g);
    i.edges.push({ a: s, b: d, g });
  });
}

function updateWiresFor(i, key) {
  for (const edge of i.edges) {
    if (edge.a !== key && edge.b !== key) continue;
    const A = i.nodes.get(edge.a), B = i.nodes.get(edge.b);
    if (!A || !B) continue;
    const a1 = anchor(A, B, nodeSize(A.data)), b1 = anchor(B, A, nodeSize(B.data));
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
}

function onGraphEvent(i, e) {
  if (!e || !e.kind) return;
  if (e.kind === "graph_node_active" || e.kind === "graph_node_idle") {
    const t = e.node != null && i.nodes.get(String(e.node));
    if (t) setBusy(i, t, e.kind === "graph_node_active"
      ? { task: e.task || e.what || "working" } : null);
    return;
  }
  scheduleRefetch(i);          // verify results, replans, crew events → repaint soon
}

// ---- selection ------------------------------------------------------------------
function clearSel(i) {
  i.sel.forEach((k) => { const t = i.nodes.get(k); if (t) t.g.classList.remove("gr-sel"); });
  i.sel.clear();
}
function addSel(i, k) { const t = i.nodes.get(k); if (t) { t.g.classList.add("gr-sel"); i.sel.add(k); } }
function setSel(i, k) { clearSel(i); addSel(i, k); }
function reselect(i) { const keep = [...i.sel].filter((k) => i.nodes.has(k)); i.sel.clear(); keep.forEach((k) => addSel(i, k)); }

/** The selection settled: one node → its light card; the conclusion → its card;
 * nothing → the how-to. The full inspector only ever opens on a double-click. */
function selectionChanged(i) {
  i.inspectKey = null;
  if (i.sel.size === 1) {
    const key = [...i.sel][0];
    const t = i.nodes.get(key);
    if (t && t.data.node_type === "conclusion") asideConclusion(i);
    else asideLight(i, key);
  } else if (i.sel.size > 1) {
    i.aside.innerHTML = `<p class="dim">${i.sel.size} modules selected — drag to arrange them together.</p>`;
  } else {
    asideDefault(i);
  }
}

// ---- the aside (this screen's own inspector/action host) ------------------------
const esc = (s) => (W.escapeHtml ? W.escapeHtml(String(s ?? "")) : String(s ?? ""));

function asideDefault(i) {
  i.aside.innerHTML = `<div class="gr-tip">
    <p class="dim">Click a module for its card; <b>double-click</b> opens the inspector.
    Drag to arrange — positions stick. Scroll to zoom, hold space to pan,
    <kbd>f</kbd> frames everything.</p></div>`;
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
  const onMove = (e) => pointerMove(i, e);
  const onUp = (e) => pointerUp(i, e);
  const onWheel = (e) => { e.preventDefault(); i.world.zoomAt(e.clientX, e.clientY, e.deltaY > 0 ? 1 / 1.1 : 1.1); saveView(i); };
  const onDbl = (e) => e.preventDefault();     // real dblclick handled via pressToken
  const onKey = (e) => keyDown(i, e);
  const onKeyUp = (e) => { if (e.code === "Space") i.space = false; };

  svg.addEventListener("pointerdown", onDown);
  svg.addEventListener("pointermove", onMove);
  svg.addEventListener("pointerup", onUp);
  svg.addEventListener("pointercancel", onUp);
  svg.addEventListener("lostpointercapture", onUp);
  svg.addEventListener("wheel", onWheel, { passive: false });
  svg.addEventListener("dblclick", onDbl);
  // Keys belong to THIS screen (it carries tabindex="-1" and is focused on open) —
  // a listener on the whole document would fire under every other screen too.
  i.screen.addEventListener("keydown", onKey);
  i.screen.addEventListener("keyup", onKeyUp);

  i._teardown = () => {
    svg.removeEventListener("pointerdown", onDown); svg.removeEventListener("pointermove", onMove);
    svg.removeEventListener("pointerup", onUp); svg.removeEventListener("pointercancel", onUp);
    svg.removeEventListener("lostpointercapture", onUp); svg.removeEventListener("wheel", onWheel);
    svg.removeEventListener("dblclick", onDbl);
    i.screen.removeEventListener("keydown", onKey); i.screen.removeEventListener("keyup", onKeyUp);
  };
}

function pointerDown(i, e) {
  if (e.button === 2) return;
  i.screen.focus({ preventScroll: true });     // clicks keep the keys aimed here
  const svg = i.world.el.svg;
  svg.setPointerCapture(e.pointerId);          // route ALL further events here — no lost moves
  if (e.button === 1 || i.space) {
    i.gesture = { type: "pan", x: e.clientX, y: e.clientY };
    i.host.classList.add("lw2-panning");
    return;
  }
  if (e.button !== 0) return;
  const tokEl = e.target.closest && e.target.closest(".gr-node");
  if (tokEl) { pressToken(i, e, tokEl); return; }
  // empty floor → clear + marquee
  if (!e.shiftKey) { clearSel(i); i.inspectKey = null; selectionChanged(i); }
  beginMarquee(i, e);
}

function nowMs() { return (W.performance && performance.now) ? performance.now() : Date.now(); }

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
  if (g.type === "pan") { i.world.panBy(e.clientX - g.x, e.clientY - g.y); g.x = e.clientX; g.y = e.clientY; return; }
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
  if (g.type === "marquee") { endMarquee(i, g); return; }
  if (g.type === "arm") {
    if (!g.moved) {                              // released without moving → a click
      if (g.maybeDouble) { i._lastClick = null; openInspector(i, g.key); }
      else { i._lastClick = { key: g.key, t: nowMs() }; selectionChanged(i); }
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
}

function keyDown(i, e) {
  const tag = (e.target && e.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (e.target && e.target.isContentEditable)) return;
  if (e.code === "Space") { i.space = true; e.preventDefault(); return; }
  if (e.key === "Escape") { i.inspectKey = null; clearSel(i); selectionChanged(i); return; }
  if (e.key === "f" || e.key === "F") { i.world.fit(sceneBounds(i)); saveView(i); return; }
  if (e.key === "Enter" && i.sel.size === 1) openInspector(i, [...i.sel][0]);
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

function saveView(i) { try { localStorage.setItem(i.viewKey, JSON.stringify(i.world.getView())); } catch (e) { /* */ } }
function restoreView(i) {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(i.viewKey) || "null"); } catch (e) { /* */ }
  if (saved) i.world.setView(saved); else i.world.fit(sceneBounds(i));
}

// The classic scripts' door in: core.js's openModuleGraph calls this.
W.ModuleGraph = { open, close };
