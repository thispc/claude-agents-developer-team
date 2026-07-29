// studio.js — The Studio shell — Lifeworld state, scene top bar, cast, time transport, activity + root canvas logs, scene rules, the graph Rules panel, Run→Decision-Memo, and the graph chat.
// Split from the old monolithic app.js (order preserved; classic scripts share one global scope; index.html defines load order).

// ============================================================================
// The Lifeworld — the owner's private society. A WORLD holds three registries:
//  AGENTS (people), ARTIFACTS (objects) and ROOMS. People and objects are authored
//  once from a short free-text BRIEF (the LLM fills in the internals), then PLACED
//  into rooms. A room has a THEME (open/home/classroom/campus/office/casino); only a
//  casino renders the poker felt — every other theme gets a tasteful, CSS-only
//  setting. The overview is a MAP: every person and object shown as a card, grouped
//  and colour-coded by the room it sits in, with an "unplaced" group for the rest.
//
//  Reuses the Studio's figure/sigil() language, the inline composer drawers (never a
//  browser popup), the createElement/textContent context menus (studioMenu), the
//  poker felt + .pcard cards and the shared control-padding tokens. All free text
//  reaches innerHTML only through escapeHtml; every model call in Live mode is billed
//  and surfaced in the room's cost-aware ticker.
// ============================================================================
let lwWorlds = [];
let lwWorld = null;            // GET /api/lw/{id} — the overview payload
let lwWorldId = null;
let lwTab = "overview";        // overview | agents | artifacts | rooms
let lwRoom = null;             // the open ROOM (GET .../room/{rid})
let lwRoomId = null;
let lwLive = false;            // Live = agents actually think (spends tokens)
let lwSeenLog = new Set();     // room-log 'n's already shown, so only new lines animate
let lwRoomTypes = [];          // [{type,theme,blurb}] from the API

// Distinct, well-spaced accent hues so each room reads as its own colour on the
// map. Indexed by the room's position in the world's room list.
const LW_ROOM_HUES = [162, 26, 214, 276, 128, 336, 46, 194];
const lwLiveQ = () => (lwLive ? "?live=1" : "");

// --- Konva room canvas: the infinite paintable room -------------------------
// One Stage per open room, mounted into #lwKonvaHost. Tokens live in worldLayer;
// the dotted grid (worldLayer's sibling) is revealed only while something drags.
// Pan/zoom is applied to the Stage (so it transforms every layer) and cached per
// room so a rebuild after a drag/round/create never yanks the viewport.
let lwKonva = null;          // { stage, gridLayer, worldLayer, host, agents:Map, props:Map, ... }
let lwTool = "select";       // select | agent | artifact | manager
let lwCreateFlow = null;     // active single-flight creation, or null
let lwGridTimer = null;
const lwViewCache = {};      // roomId -> {x,y,scale}
const LW_TABLE_R = 58;       // collating-table disc radius (world units)
const LW_SOCKET_R = 80;      // radius of the ring the seat sockets sit on
const LW_SNAP_DIST = 46;     // drop within this of a free socket → magnetic seat
const LW_NUDGE = 6;          // world units an arrow-key press moves the selection
const LW_RECT_W = 150;       // rectangular-table footprint
const LW_RECT_H = 92;
const LW_SEAT_OUT = 20;      // how far outside a shape's edge its seat sockets sit
// Tools each carry a one-key shortcut (shown in the dock, wired on the canvas).
// Icons are drawn as inline vector — the whole canvas is off the emoji look.
const LW_TOOLS = [
  { id: "select",   key: "V", label: "Select" },
  { id: "agent",    key: "A", label: "Agent" },
  { id: "artifact", key: "O", label: "Object" },
];
let lwThreadDir = "both";      // "both" (bidirectional) | "one" (unidirectional, tail→head)
// The style variants a new person can wear; the final face blends the variant with
// the agent's own identity, so two people who pick the same variant still differ.
const LW_AV_VARIANTS = ["a", "b", "c", "d", "e", "f"];
// The object "figure" choices — geometric vector glyphs (mono = a monogram letter).
const LW_OBJ_ICONS = ["mono", "cards", "doc", "star", "gem", "ring"];

// --- screen switch: bring the section on, hide every sibling (mirrors the Studio).
function showLifeworld() {
  $("#home").hidden = true; $("main").hidden = true;
  for (const id of ["plan", "selfPage", "aboutPage", "studio", "scenes"]) {
    const e = $("#" + id); if (e) e.hidden = true;
  }
  $("#projectBar").hidden = true;
  $("#lifeworld").hidden = false;
  currentProject = null;
}

// ============================================================================
// The Studio — one implicit world per user; scenes are canvases; the canvas is the
// hero. openStudio() brings the section on and calls lwEnterStudio() to open a scene.
// ============================================================================
let sdTau = 0;

async function lwEnterStudio() {
  let worlds = [];
  try { worlds = (await api("/api/lw")).worlds || []; }
  catch (e) { $("#lwStage").innerHTML = `<p class="empty">Could not open the Studio: ${escapeHtml(e.message || String(e))}</p>`; return; }
  if (worlds.length) lwWorldId = worlds[0].id;
  else {
    try { lwWorldId = (await api("/api/lw", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "Studio" }) })).world.id; }
    catch (e) { toast(`Could not start the Studio: ${e.message}`); return; }
  }
  let ov = { rooms: [] };
  try { ov = await api(`/api/lw/${lwWorldId}`); } catch (e) { /* a fresh world */ }
  sdTau = (ov.world && ov.world.tau) || 0;
  const rooms = ov.rooms || [];
  let rid = rooms.length ? rooms[rooms.length - 1].id : null;
  if (rid == null) {
    try { rid = (await api(`/api/lw/${lwWorldId}/room`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "untitled", type: "freeplay" }) })).room.id; }
    catch (e) { toast(`Could not create a scene: ${e.message}`); return; }
  }
  await lwOpenScene(rid);
}

async function lwOpenScene(rid) {
  sdPause();
  lwRoomId = rid; lwSeenLog = new Set();
  let d;
  try { d = await api(`/api/lw/${lwWorldId}/room/${rid}`); }
  catch (e) { $("#lwStage").innerHTML = `<p class="empty">Could not open the scene: ${escapeHtml(e.message || String(e))}</p>`; return; }
  lwRenderRoom(d.room || d);
}

// --- top bar: editable title, autosave indicator, scenes, rename -----------
let sdSaveT = null;
// The whole scene autosaves in the DB on every action; this indicator just tells the
// truth about it. Idle rests on "Saved"; a change flashes the spinner, then back to Saved.
function sdFlash() {
  const el = $("#sdSave"); if (!el) return;
  el.className = "sd-save saving"; el.textContent = "";
  clearTimeout(sdSaveT);
  sdSaveT = setTimeout(() => { if ($("#sdSave") !== el) return; el.className = "sd-save saved"; el.textContent = "Saved"; }, 350);
}
function sdSavedIdle() { const el = $("#sdSave"); if (el && !el.classList.contains("saving")) { el.className = "sd-save saved"; el.textContent = "Saved"; } }
async function sdSaveNow() {
  if (!lwWorldId) return;
  sdFlash();
  try { await api(`/api/lw/${lwWorldId}/touch`, { method: "POST" }); } catch (e) { /* the indicator already reassured */ }
}
async function sdRenameScene(name) {
  if (!lwWorldId || !lwRoomId) return;
  sdFlash();
  try {
    const d = await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/scene`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    if (lwRoom) lwRoom.name = (d.room && d.room.name) || name;
  } catch (e) { toast(`Could not rename: ${e.message}`); }
}
async function sdToggleScenes() {
  const menu = $("#sdScenesMenu"), btn = $("#sdScenesBtn"); if (!menu) return;
  if (!menu.hidden) { menu.hidden = true; if (btn) btn.setAttribute("aria-expanded", "false"); return; }
  let rooms = [];
  try { rooms = (await api(`/api/lw/${lwWorldId}`)).rooms || []; } catch (e) { toast(`Could not list scenes: ${e.message}`); return; }
  menu.innerHTML = rooms.map((r) =>
    `<div class="sd-menu-row${r.id === lwRoomId ? " on" : ""}">
      <button class="sd-menu-item" data-scene="${escapeHtml(String(r.id))}">
        <span class="sd-menu-name">${escapeHtml(r.name || "untitled")}</span>
        <span class="sd-menu-sub">${(r.agents || []).length} cast · ${(r.props || []).length} props</span>
      </button>
      <button class="sd-menu-del" data-del="${escapeHtml(String(r.id))}" data-name="${escapeHtml(r.name || "untitled")}" title="Delete scene" aria-label="Delete this scene">🗑</button>
    </div>`).join("")
    + `<button class="sd-menu-item sd-menu-new" id="sdNewScene">＋ New scene</button>`;
  menu.hidden = false; if (btn) btn.setAttribute("aria-expanded", "true");
  menu.querySelectorAll("[data-scene]").forEach((b) => b.addEventListener("click", () => { menu.hidden = true; lwOpenScene(Number(b.dataset.scene)); }));
  menu.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", (ev) => {
    ev.stopPropagation(); menu.hidden = true; sdDeleteScene(Number(b.dataset.del), b.dataset.name);
  }));
  $("#sdNewScene").addEventListener("click", async () => {
    menu.hidden = true;
    try { const r = await api(`/api/lw/${lwWorldId}/room`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "untitled", type: "freeplay" }) }); sdFlash(); lwOpenScene(r.room.id); }
    catch (e) { toast(`Could not add a scene: ${e.message}`); }
  });
}

// Delete a scene (from the dropdown or the title-bar trash). The cast is world-level, so
// agents/props survive; only this canvas goes. Afterwards we open another scene, or mint a
// fresh untitled one so the Studio is never empty.
async function sdDeleteScene(rid, name) {
  if (!lwWorldId || rid == null) return;
  if (!confirm(`Delete the scene "${name || "untitled"}"?\n\nIts agents and props stay in your cast — only this canvas is removed.`)) return;
  sdPause();
  try { await api(`/api/lw/${lwWorldId}/room/${rid}`, { method: "DELETE" }); }
  catch (e) { toast(`Could not delete the scene: ${e.message}`); return; }
  sdFlash(); toast("Scene deleted");
  let rooms = [];
  try { rooms = (await api(`/api/lw/${lwWorldId}`)).rooms || []; } catch (e) { /* fall through to a fresh scene */ }
  const next = rooms.find((r) => r.id !== rid);
  if (next) { lwOpenScene(next.id); return; }
  try { const r = await api(`/api/lw/${lwWorldId}/room`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "untitled", type: "freeplay" }) }); lwOpenScene(r.room.id); }
  catch (e) { toast(`Could not open a scene: ${e.message}`); }
}
function sdDeleteCurrentScene() { if (lwRoomId != null) sdDeleteScene(lwRoomId, (lwRoom && lwRoom.name) || "untitled"); }

// --- the Cast: every agent, grouped by the scene they are in ---------------
async function sdOpenRoster() {
  const host = $("#sdRosterHost"); if (!host || !lwWorldId) return;
  host.hidden = false;
  host.innerHTML = `<div class="sd-roster-card"><p class="dim">gathering the cast…</p></div>`;
  let d;
  try { d = await api(`/api/lw/${lwWorldId}`); }
  catch (e) { host.innerHTML = `<div class="sd-roster-card"><p class="dim">Could not read the cast: ${escapeHtml(e.message)}</p></div>`; return; }
  const rooms = d.rooms || [], agents = d.agents || [];
  const byRoom = {}; agents.forEach((a) => { const k = a.room == null ? "loose" : String(a.room); (byRoom[k] = byRoom[k] || []).push(a); });
  const nameOf = (k) => k === "loose" ? "Not in a scene" : ((rooms.find((r) => String(r.id) === k) || {}).name || "untitled");
  const card = (a) => {
    const seed = lwAvatarSeed({ name: a.name, id: a.id, figure: a.figure });
    const want = (dominantWant(a.wants) || {}).name || "";
    return `<button class="sd-cast" data-room="${a.room == null ? "" : escapeHtml(String(a.room))}" data-agent="${escapeHtml(String(a.id))}">
      <span class="sd-cast-face"><img alt="" src="${lwSvgUri(lwAvatarSvg(seed, 40))}"></span>
      <span class="sd-cast-name">${escapeHtml(a.name || "someone")}</span>
      ${want ? `<span class="sd-cast-want">wants ${escapeHtml(want)}</span>` : ""}
      ${lwMoodBars(a.mood || {})}
    </button>`;
  };
  const groups = Object.keys(byRoom).sort((x, y) => (x === "loose") - (y === "loose"));
  host.innerHTML = `<div class="sd-roster-card">
    <div class="sd-roster-head"><h3>The Cast</h3>
      <span class="dim">${agents.length} agent${agents.length === 1 ? "" : "s"} · ${rooms.length} scene${rooms.length === 1 ? "" : "s"}</span>
      <button class="sd-close" id="sdRosterClose">✕</button></div>
    ${groups.length ? groups.map((k) => `<div class="sd-roster-group">
      <div class="sd-roster-scene">${escapeHtml(nameOf(k))} <span class="dim">· ${byRoom[k].length}</span></div>
      <div class="sd-roster-grid">${byRoom[k].map(card).join("")}</div></div>`).join("")
      : `<p class="dim">No agents yet. Pick the Agent tool and click the canvas.</p>`}
  </div>`;
  $("#sdRosterClose").addEventListener("click", () => { host.hidden = true; });
  host.onclick = (e) => { if (e.target === host) host.hidden = true; };   // click backdrop to close
  host.querySelectorAll("[data-agent]").forEach((b) => b.addEventListener("click", async () => {
    host.hidden = true;
    const rid = b.dataset.room;
    if (rid) await lwOpenScene(Number(rid));
    setTimeout(() => { const e = lwKonva && lwKonva.agents.get(String(b.dataset.agent)); if (e) lwSelSet("agent", e); }, 450);
  }));
}

// --- the time transport: play/pause, single-step, speed --------------------
let sdPlaying = false, sdTimer = null, sdSpeed = 2, sdMax = 10, sdRun = 0;   // sec between beats, cap, done
function sdTimeBarHtml() {
  const maxLbl = sdMax === Infinity ? "∞" : sdMax;
  return `<div class="sd-time" id="sdTime">
    <button class="sd-play" id="sdPlay" title="Play — run beats automatically until the cap">▶</button>
    <button class="sd-step" id="sdStep" title="Run one beat now">⏭</button>
    <div class="sd-progress" id="sdProg" title="beats run in this play">
      <i class="sd-prog-fill" id="sdProgFill"></i><span class="sd-prog-lbl" id="sdProgLbl">0 / ${maxLbl}</span></div>
    <label class="sd-speed" title="seconds between beats">⏱<input type="range" id="sdSpeedRange" min="1" max="10" step="1" value="${sdSpeed}"><b id="sdSpeedLbl">${sdSpeed}s</b></label>
    <label class="sd-maxwrap" title="stop after this many beats">cap
      <select id="sdMaxSel">${[5, 10, 25, 50].map((v) => `<option value="${v}"${v === sdMax ? " selected" : ""}>${v}</option>`).join("")}<option value="inf"${sdMax === Infinity ? " selected" : ""}>∞</option></select></label>
    <span class="sd-tau" id="lwTau"></span>
  </div>`;
}
function sdWireTime() {
  const p = $("#sdPlay"); if (p) p.addEventListener("click", sdTogglePlay);
  const s = $("#sdStep"); if (s) s.addEventListener("click", () => { if (!sdPlaying) lwPlayRound(); });
  const r = $("#sdSpeedRange");
  if (r) r.addEventListener("input", (e) => { sdSpeed = Number(e.target.value); const l = $("#sdSpeedLbl"); if (l) l.textContent = sdSpeed + "s"; if (sdPlaying) sdArm(); });
  const m = $("#sdMaxSel");
  if (m) m.addEventListener("change", (e) => { sdMax = e.target.value === "inf" ? Infinity : Number(e.target.value); sdPaintProg(); });
  paintPlay(); sdPaintProg();
}
function paintPlay() { const b = $("#sdPlay"); if (b) { b.textContent = sdPlaying ? "⏸" : "▶"; b.classList.toggle("on", sdPlaying); } }
function sdPaintProg() {
  const fill = $("#sdProgFill"), lbl = $("#sdProgLbl"); if (!fill) return;
  const inf = sdMax === Infinity;
  fill.style.width = inf ? "0%" : Math.min(100, (sdRun / sdMax) * 100) + "%";
  fill.classList.toggle("indet", inf && sdPlaying);
  if (lbl) lbl.textContent = `${sdRun} / ${inf ? "∞" : sdMax}`;
}
function sdTogglePlay() { sdPlaying ? sdPause() : sdPlay(); }
function sdPlay() { sdRun = 0; sdPlaying = true; paintPlay(); sdPaintProg(); sdArm(); }
function sdPause() { sdPlaying = false; clearTimeout(sdTimer); sdTimer = null; paintPlay(); sdPaintProg(); }
function sdArm() {
  clearTimeout(sdTimer);
  const tick = async () => {
    if (!sdPlaying || !lwKonva) { sdPause(); return; }
    if (lwKonva.drag || (typeof Konva !== "undefined" && Konva.isDragging())) {   // don't rebuild the stage
      lwLogOn && lwLogThr("beatdefer", 500, "life", "beat deferred — a drag is in flight", null, "debug");
      sdTimer = setTimeout(tick, 400); return;                                    // mid-drag — wait for the drop
    }
    sdPulse();
    await lwPlayRound();
    if (!lwKonva) { sdPause(); return; }
    sdRun++; sdPaintProg();
    if (sdRun >= sdMax) { sdPause(); return; }        // bounded — stop at the cap
    if (sdPlaying) sdTimer = setTimeout(tick, sdSpeed * 1000);
  };
  sdTimer = setTimeout(tick, sdSpeed * 1000);
}
function sdPulse() { const f = $("#sdProgFill"); if (!f || reduceMotion()) return; f.classList.remove("beat"); void f.offsetWidth; f.classList.add("beat"); }

// --- activity: a categorised log of what each beat did ---------------------
let sdActOpen = false, sdActTab = "beats";
// A directed graph of who acted on whom this scene, built from the log's frm→who edges.
// Self-hosted SVG; manager ("manage") beats only fold in for root — a black box otherwise.
function sdFlowHtml() {
  const root = lwCanRootDebug();
  const agents = (lwRoom && lwRoom.agents) || [];
  if (!agents.length) return `<p class="sd-act-empty">No agents in this scene yet.</p>`;
  const log = ((lwRoom && lwRoom.log) || []).filter((l) => root || l.kind !== "manage");
  const edges = {};
  log.forEach((l) => {
    if (l.frm == null || l.who == null || l.frm === l.who) return;
    const k = l.frm + ">" + l.who; edges[k] = edges[k] || { c: 0, manage: false };
    edges[k].c++; if (l.kind === "manage") edges[k].manage = true;
  });
  const N = agents.length, R = N > 1 ? 96 : 0, cx = 130, cy = 118, pos = {};
  agents.forEach((a, i) => { const ang = -Math.PI / 2 + i * 2 * Math.PI / N; pos[a.id] = { x: cx + R * Math.cos(ang), y: cy + R * Math.sin(ang), name: a.name || "" }; });
  const maxC = Math.max(1, ...Object.values(edges).map((e) => e.c));
  const arcs = Object.entries(edges).map(([k, e]) => {
    const [f, t] = k.split(">").map(Number), A = pos[f], B = pos[t]; if (!A || !B) return "";
    const dx = B.x - A.x, dy = B.y - A.y, len = Math.hypot(dx, dy) || 1, ox = -dy / len * 16, oy = dx / len * 16;
    const w = (1 + 3 * (e.c / maxC)).toFixed(1), col = e.manage ? "#d64545" : "var(--accent)";
    return `<path d="M${A.x.toFixed(0)} ${A.y.toFixed(0)} Q${(A.x + dx / 2 + ox).toFixed(0)} ${(A.y + dy / 2 + oy).toFixed(0)} ${B.x.toFixed(0)} ${B.y.toFixed(0)}" stroke="${col}" stroke-width="${w}" fill="none" opacity="0.55" marker-end="url(#flowArrow)"/>`;
  }).join("");
  const nodes = agents.map((a) => { const p = pos[a.id]; return `<g><circle cx="${p.x.toFixed(0)}" cy="${p.y.toFixed(0)}" r="15" fill="var(--accent-soft)" stroke="var(--accent)" stroke-width="1.5"/><text x="${p.x.toFixed(0)}" y="${(p.y + 27).toFixed(0)}" text-anchor="middle" font-size="9" fill="var(--text)">${escapeHtml(p.name.slice(0, 9))}</text></g>`; }).join("");
  const hasManage = Object.values(edges).some((e) => e.manage);
  return `<div class="sd-flow"><svg viewBox="0 0 260 250" width="100%" role="img" aria-label="conversation flow">
    <defs><marker id="flowArrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L8 4L0 8z" fill="var(--accent)"/></marker></defs>
    ${arcs}${nodes}</svg>
    <p class="sd-flow-cap">who acted on whom · line weight = how much${hasManage ? ` · <span style="color:#d64545">red = host</span>` : ""}</p></div>`;
}

function sdActivityHtml(log) {
  const root = lwCanRootDebug();
  const tab = (id, label) => `<button class="sd-act-tab${sdActTab === id ? " on" : ""}" data-acttab="${id}">${label}</button>`;
  const tabs = `<div class="sd-act-tabs">${tab("beats", "Beats")}${tab("flow", "Flow")}${root ? tab("canvas", `Canvas${lwLogOn ? ` <span class="sd-log-live" title="capturing"></span>` : ""}`) : ""}</div>`;
  const head = `<div class="sd-act-head">${tabs}<button class="sd-act-x" id="sdActX" aria-label="Close">✕</button></div>`;
  if (root && sdActTab === "canvas") return head + lwLogPanelHtml();
  if (sdActTab === "flow") return head + sdFlowHtml();
  log = (log || []).filter((l) => root || l.kind !== "manage");   // the manager's beats are a black box unless root
  if (!log.length) return head + `<p class="sd-act-empty">Nothing yet. Run a beat to see what happens.</p>`;
  const rows = log.slice(-100).reverse().map((l) => {
    const thought = l.tier === 2 || l.billed, who = l.who != null ? lwNameOf(l.who) : "";
    return `<div class="sd-act-row${thought ? " thought" : ""}">
      <span class="sd-act-kind k-${escapeHtml(String(l.kind || "x"))}">${escapeHtml(String(l.kind || ""))}</span>
      <span class="sd-act-text">${who ? `<b>${escapeHtml(who)}</b> ` : ""}${escapeHtml(String(l.text || ""))}</span>
      ${thought ? `<span class="sd-act-badge" title="a model actually thought — billed">💭</span>` : ""}
    </div>`;
  }).join("");
  return head + `<div class="sd-act-list">${rows}</div>`;
}
function sdShowActivity(open) {
  sdActOpen = open;
  const panel = $("#sdActivity"); if (!panel) return;
  panel.hidden = !open;
  const btn = $("#sdActBtn"); if (btn) btn.classList.toggle("on", open);
  if (!open) return;
  panel.classList.toggle("wide", sdActTab === "flow" || (lwCanRootDebug() && sdActTab === "canvas"));
  panel.innerHTML = sdActivityHtml(lwRoom && lwRoom.log);
  const x = $("#sdActX"); if (x) x.addEventListener("click", () => sdShowActivity(false));
  panel.querySelectorAll(".sd-act-tab").forEach((b) => b.addEventListener("click", () => { sdActTab = b.dataset.acttab; sdShowActivity(true); }));
  if (lwCanRootDebug() && sdActTab === "canvas") lwLogWireCanvasPanel();
}
function sdToggleActivity() { sdShowActivity(!sdActOpen); }

// --- canvas debug logs (ROOT ONLY): a fine-grained, filterable ring buffer of every
// interaction event, shown in the Activity panel's "Canvas" tab. Off by default, gated on
// me.is_root — these are control-plane diagnostics, not user-facing, and cost nothing when
// capture is off (lwLog returns on the first line). Turn Capture on, reproduce the issue on
// the canvas, filter by category/level, then Copy the lines out.
const LW_LOG_CATS = ["pointer", "hit", "cursor", "drag", "pan", "marquee", "select", "portal", "life", "net", "idle"];
const LW_LOG_LEVELS = { debug: 0, info: 1, warn: 2 };
const LW_LOG_MAX = 3000;
let lwLogOn = false, lwLogBuf = [], lwLogFilter = new Set(LW_LOG_CATS), lwLogLevel = "debug";
const lwLogThrTs = {};
function lwCanRootDebug() { return !!(me && me.is_root); }
function lwLog(cat, msg, data, level) {
  if (!lwLogOn) return;
  lwLogBuf.push({ wall: Date.now(), cat, level: level || "info", msg, data });
  if (lwLogBuf.length > LW_LOG_MAX) lwLogBuf.shift();
  lwLogSchedule();
}
function lwLogThr(key, ms, cat, msg, data, level) {   // throttle a hot event (drag/pointer move)
  if (!lwLogOn) return;
  const now = Date.now();
  if (lwLogThrTs[key] && now - lwLogThrTs[key] < ms) return;
  lwLogThrTs[key] = now; lwLog(cat, msg, data, level);
}
function lwLogTime(wall) {
  const d = new Date(wall), p = (n, w) => String(n).padStart(w || 2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}
function lwLogFmt(data) {
  if (data == null) return "";
  try { return typeof data === "string" ? data : JSON.stringify(data); } catch (e) { return String(data); }
}
function lwLogVisible() {
  const min = LW_LOG_LEVELS[lwLogLevel] ?? 0;
  return lwLogBuf.filter((e) => lwLogFilter.has(e.cat) && (LW_LOG_LEVELS[e.level] ?? 1) >= min);
}
let lwLogRenderT = null;
function lwLogSchedule() {                          // batch live re-renders so a firehose can't thrash the DOM
  if (!(sdActOpen && sdActTab === "canvas") || lwLogRenderT) return;
  lwLogRenderT = setTimeout(() => { lwLogRenderT = null; lwLogRender(); }, 140);
}
function lwLogRowHtml(e) {
  const d = e.data != null ? ` <span class="sd-log-data">${escapeHtml(lwLogFmt(e.data))}</span>` : "";
  return `<div class="sd-log-row lvl-${e.level}"><span class="sd-log-t">${lwLogTime(e.wall)}</span>`
    + `<span class="sd-log-cat c-${e.cat}">${e.cat}</span><span class="sd-log-msg">${escapeHtml(e.msg)}${d}</span></div>`;
}
function lwLogRender() {
  const list = $("#lwLogList"); if (!list) return;
  const vis = lwLogVisible();
  list.innerHTML = vis.length ? vis.slice(-500).reverse().map(lwLogRowHtml).join("")
    : `<p class="sd-act-empty">${lwLogOn ? "No lines match the filter yet — interact with the canvas." : "Capture is off. Turn it on, then reproduce the issue."}</p>`;
  const n = $("#lwLogN"); if (n) n.textContent = `${vis.length}/${lwLogBuf.length}`;
}
function lwLogPanelHtml() {
  const chips = LW_LOG_CATS.map((c) => `<button class="sd-log-chip${lwLogFilter.has(c) ? " on" : ""}" data-logcat="${c}">${c}</button>`).join("");
  const lvls = Object.keys(LW_LOG_LEVELS).map((l) => `<option value="${l}"${lwLogLevel === l ? " selected" : ""}>${l}+</option>`).join("");
  return `<div class="sd-log-ctl">
      <button class="sd-log-cap${lwLogOn ? " on" : ""}" id="lwLogCap">${lwLogOn ? "● Capturing" : "○ Capture"}</button>
      <select class="sd-log-lvl" id="lwLogLvl" title="minimum level">${lvls}</select>
      <button class="sd-log-btn" id="lwLogCopy" title="Copy the shown lines">Copy</button>
      <button class="sd-log-btn" id="lwLogClear" title="Clear the buffer">Clear</button>
      <span class="sd-log-n" id="lwLogN" title="shown / captured"></span>
    </div>
    <div class="sd-log-cats">${chips}</div>
    <div class="sd-log-list" id="lwLogList"></div>
    <p class="sd-log-hint">Control-plane diagnostics · root only. Capture on → reproduce on the canvas → Copy → paste back.</p>`;
}
function lwLogWireCanvasPanel() {
  const cap = $("#lwLogCap");
  if (cap) cap.addEventListener("click", () => {
    lwLogOn = !lwLogOn;
    if (lwLogOn) lwLogBuf.push({ wall: Date.now(), cat: "life", level: "info", msg: "capture started",
      data: { dpr: window.devicePixelRatio, vw: window.innerWidth, vh: window.innerHeight, tool: lwTool,
        scale: lwKonva ? +lwKonva.stage.scaleX().toFixed(3) : null, agents: lwKonva ? lwKonva.agents.size : 0, props: lwKonva ? lwKonva.props.size : 0 } });
    cap.classList.toggle("on", lwLogOn); cap.textContent = lwLogOn ? "● Capturing" : "○ Capture";
    lwLogRender();
    const live = document.querySelector(".sd-act-tab .sd-log-live"), tab = document.querySelector('.sd-act-tab[data-acttab="canvas"]');
    if (tab) { if (lwLogOn && !live) tab.insertAdjacentHTML("beforeend", ` <span class="sd-log-live"></span>`); else if (!lwLogOn && live) live.remove(); }
  });
  const lvl = $("#lwLogLvl"); if (lvl) lvl.addEventListener("change", (e) => { lwLogLevel = e.target.value; lwLogRender(); });
  $("#lwLogCopy") && $("#lwLogCopy").addEventListener("click", lwLogCopy);
  $("#lwLogClear") && $("#lwLogClear").addEventListener("click", () => { lwLogBuf = []; lwLogRender(); });
  const panel = $("#sdActivity");
  panel && panel.querySelectorAll(".sd-log-chip").forEach((b) => b.addEventListener("click", () => {
    const c = b.dataset.logcat; lwLogFilter.has(c) ? lwLogFilter.delete(c) : lwLogFilter.add(c);
    b.classList.toggle("on", lwLogFilter.has(c)); lwLogRender();
  }));
  lwLogRender();
}
function lwLogCopy() {
  const vis = lwLogVisible();
  const text = vis.map((e) => `${lwLogTime(e.wall)} [${e.cat}/${e.level}] ${e.msg}${e.data != null ? " " + lwLogFmt(e.data) : ""}`).join("\n");
  const done = () => toast(`Copied ${vis.length} log lines`);
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done).catch(() => lwLogCopyFallback(text, done));
  else lwLogCopyFallback(text, done);
}
function lwLogCopyFallback(text, done) {
  const ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { toast("Copy failed — select the text manually"); }
  ta.remove();
}
// name what a press landed on, for the pointer log
function lwTokenAncestor(node) { return node && node.findAncestor ? (node.findAncestor(".token", true) || node.findAncestor(".selframe", true)) : null; }
function lwHitDesc(node) {
  if (!node || !lwKonva) return "none";
  if (node === lwKonva.stage) return "stage(empty floor)";
  const tok = lwTokenAncestor(node);
  const self = (node.name && node.name()) || node.className || "?";
  if (tok && tok === lwKonva.selframe) return "selframe";
  if (tok) return `${tok.getAttr("lwType")}#${tok.getAttr("lwId")} [${self}]`;
  return String(self);
}
function lwRoundPt(p) { return p ? { x: Math.round(p.x), y: Math.round(p.y) } : null; }
// When a press lands on empty floor, find the token nearest the click and whether the click
// was inside its box — so we can tell "a token is right there but the hit graph missed it"
// from "nothing is actually there / it's displaced".
function lwNearestToken(w) {
  if (!lwKonva || !w) return null;
  let best = null;
  const scan = (map, type) => map.forEach((entry) => {
    const p = entry.node.position();
    const d = Math.hypot(p.x - w.x, p.y - w.y);
    let insideBox = false;
    try { const r = entry.node.getClientRect({ relativeTo: lwKonva.worldLayer }); insideBox = w.x >= r.x && w.x <= r.x + r.width && w.y >= r.y && w.y <= r.y + r.height; } catch (e) { /* */ }
    if (!best || d < best.dist) best = { id: entry.data.id, type, dist: d, at: p, insideBox };
  });
  scan(lwKonva.agents, "agent"); scan(lwKonva.props, "prop");
  return best;
}

// --- scene rules: a small popover; saved to the scene, obeyed each run ------
// Scene rules as ordered "ingress rows": each row is a typed effect (gate or shaper) with an
// optional when-match, evaluated top-to-bottom. Reused for a thread's own rule table too.
const LW_RULE_EFFECTS = ["deny", "allow", "clamp", "bias", "annotate"];
const LW_RULE_KINDS = ["", "greet", "say", "scold", "praise", "deal", "see"];
const LW_RULE_FIELDS = ["mood.stress", "mood.confidence", "mood.hope", "mood.focus", "vitals.energy", "drives.social", "drives.esteem", "drives.curiosity"];

function sdRuleRowHtml(r, i) {
  const eff = r.effect || "annotate", isShape = eff === "clamp" || eff === "bias";
  const kind = (r.when && r.when.kind) || "";
  const opt = (list, cur, lbl) => list.map((v) => `<option value="${escapeHtml(String(v))}"${String(v) === String(cur) ? " selected" : ""}>${escapeHtml(lbl ? lbl(v) : v)}</option>`).join("");
  return `<div class="sd-rule" data-i="${i}">
    <select class="sd-rule-eff" data-k="effect">${opt(LW_RULE_EFFECTS, eff)}</select>
    <span class="sd-rule-when">when <select data-k="kind">${opt(LW_RULE_KINDS, kind, (k) => k || "any")}</select></span>
    ${isShape ? `<select class="sd-rule-field" data-k="field">${opt(LW_RULE_FIELDS, r.field || LW_RULE_FIELDS[0])}</select>
      <input class="sd-rule-val" data-k="value" type="number" step="0.05" value="${r.value ?? 0}">` : ""}
    <input class="sd-rule-note" data-k="note" placeholder="${eff === "annotate" ? "text the model reads" : "note (optional)"}" value="${escapeHtml(r.note || "")}">
    <button class="sd-rule-del" title="Delete rule" aria-label="Delete rule">✕</button>
  </div>`;
}

function sdRenderRules(draft, host) {
  host = host || $("#sdRulesRows"); if (!host) return;
  host.innerHTML = draft.length ? draft.map(sdRuleRowHtml).join("")
    : `<p class="sd-rules-empty">No rules — like an empty security group. Add one below.</p>`;
  host.querySelectorAll(".sd-rule").forEach((row) => {
    const i = Number(row.dataset.i);
    row.querySelectorAll("[data-k]").forEach((el) => {
      el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => {
        const k = el.dataset.k, r = draft[i];
        if (k === "kind") { r.when = r.when || {}; if (el.value) r.when.kind = el.value; else delete r.when.kind; }
        else if (k === "value") r.value = Number(el.value);
        else r[k] = el.value;
        if (k === "effect") sdRenderRules(draft, host);   // reveal/hide the field+value for shaper effects
      });
    });
    row.querySelector(".sd-rule-del").addEventListener("click", () => { draft.splice(i, 1); sdRenderRules(draft, host); });
  });
}

// ---- the Rulebook panel: one free-text rulebook per graph + its hidden manager ----
async function sdOpenThreads(focusId) {
  const host = $("#sdRosterHost"); if (!host || !lwWorldId) return;
  host.hidden = false;
  host.innerHTML = `<div class="sd-roster-card"><p class="dim">reading the graph…</p></div>`;
  let room;
  try { room = (await api(`/api/lw/${lwWorldId}/room/${lwRoomId}`)).room; }
  catch (e) { host.innerHTML = `<div class="sd-roster-card"><p class="dim">Could not read graphs: ${escapeHtml(e.message)}</p></div>`; return; }
  const nameOf = (id) => { const a = (room.agents || []).find((x) => x.id === id) || (room.props || []).find((x) => x.id === id); return a ? a.name : `#${id}`; };
  const models = LW_MODELS.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join("");
  let threads = (room.threads || []).slice();
  if (focusId != null) threads.sort((a, b) => (a.id === focusId ? -1 : b.id === focusId ? 1 : 0));   // focused graph first
  // ONE rules field for the whole graph — no thread/arrow/edge plumbing exposed. A graph's rules
  // ARE its manager's brief; the manager (model + a bounded per-round budget) runs them.
  const card = (t) => `<div class="sd-thread" data-tid="${t.id}">
      <div class="sd-thread-members">${escapeHtml([...new Set((t.edges || []).flatMap((e) => [e[0], e[1]]))].map(nameOf).join(" · ") || "no members")}</div>
      <div class="sc-label">Rules for this graph <span class="dim">plain language — the manager makes it happen</span></div>
      <textarea class="sc-input sd-thread-book" rows="5" placeholder="e.g. anyone can start — I want a debate on the most sustainable route from A to B.">${escapeHtml(t.rulebook || "")}</textarea>
      <div class="sd-book-actions"><button class="sd-book-refine">✨ Refine with AI</button></div>
      <div class="sc-label">Hidden manager <span class="dim">orchestrates it · a black box unless you're root</span></div>
      <div class="sd-thread-mgr">
        <label>Model <select class="sd-mgr-model">${models}</select></label>
        <label>Budget <input class="sd-mgr-budget" type="number" min="0" max="4" value="${(t.manager && t.manager.budget) ?? 2}"></label>
      </div>
      <div class="sc-label">Protocol <span class="dim">HOW the graph deliberates — policy as data, not code</span></div>
      <div class="sd-thread-mgr">
        <label>Preset <select class="sd-proto-preset">
          <option value="classic">classic — manager composes every round</option>
          <option value="evidence-2026">evidence-2026 — independent openings · anonymized · devil's advocate on unanimity</option>
        </select></label>
      </div>
      <div class="sc-actions"><button class="sc-ctl primary sd-thread-save">Save rules</button></div>
    </div>`;
  host.innerHTML = `<div class="sd-roster-card sd-threads-card">
    <div class="sd-roster-head"><h3>Graph rules</h3><span class="dim">the manager runs these across the graph</span>
      <button class="sd-close" id="sdThreadsClose">✕</button></div>
    <div class="sd-threads-list">${threads.length ? threads.map(card).join("") : `<p class="dim">No graph yet. Select a token, then drag one of its four handles onto another token to connect them.</p>`}</div>
  </div>`;
  $("#sdThreadsClose").addEventListener("click", () => { host.hidden = true; });
  threads.forEach((t) => {
    const el = host.querySelector(`.sd-thread[data-tid="${t.id}"]`); if (!el) return;
    const mm = el.querySelector(".sd-mgr-model"); if (mm) mm.value = (t.manager && t.manager.model) || "";
    const pp = el.querySelector(".sd-proto-preset"); if (pp) pp.value = (t.protocol && t.protocol.preset) || "classic";
    const book = el.querySelector(".sd-thread-book");
    el.querySelector(".sd-book-refine").addEventListener("click", async (ev) => {
      const btn = ev.currentTarget; btn.disabled = true; btn.textContent = "refining…";
      try { const r = await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/thread/${t.id}/refine`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: book.value }) }); if (r.text) book.value = r.text; }
      catch (e) { toast(`Could not refine: ${e.message}`); }
      btn.disabled = false; btn.textContent = "✨ Refine with AI";
    });
    el.querySelector(".sd-thread-save").addEventListener("click", async () => {
      const body = { rulebook: book.value,
        manager: { model: mm ? mm.value : "", budget: Number(el.querySelector(".sd-mgr-budget").value) || 0 },
        protocol: Object.assign({}, t.protocol || {}, { preset: pp ? pp.value : "classic" }) };  // keep API-set axes
      sdFlash();
      try { await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/thread/${t.id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); toast("rules saved"); await lwReloadRoom(); }
      catch (e) { toast(`Could not save: ${e.message}`); }
    });
  });
}

// ---- run a deliberation: N rounds, then the manager's DECISION MEMO (the kept result) ----
async function sdRunGraph(tid) {
  const host = $("#sdRosterHost"); if (!host || !lwWorldId) return;
  host.hidden = false;
  host.innerHTML = `<div class="sd-roster-card"><p class="dim">deliberating${lwLive ? " (live)" : ""}… the manager runs the rounds, then writes the memo</p></div>`;
  try {
    const r = await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/thread/${tid}/run${lwLiveQ()}${lwLive ? "&" : "?"}rounds=2`, { method: "POST" });
    sdFlash(); await lwReloadRoom();               // the debate's bubbles land on the canvas
    sdShowMemo(r.result, tid);
  } catch (e) { host.innerHTML = `<div class="sd-roster-card"><p class="dim">Run failed: ${escapeHtml(e.message)}</p></div>`; }
}
function sdShowMemo(memo, tid) {
  const host = $("#sdRosterHost"); if (!host || !memo) return;
  host.hidden = false;
  const who = (id) => escapeHtml((memo.names || {})[String(id)] || `#${id}`);
  host.innerHTML = `<div class="sd-roster-card sd-memo-card">
    <div class="sd-roster-head"><h3>Decision memo <span class="sd-memo-v">v${memo.v || 1}</span></h3>
      <span class="dim">${memo.rounds || 1} round${(memo.rounds || 1) > 1 ? "s" : ""}</span>
      <button class="sd-close" id="sdMemoClose">✕</button></div>
    <div class="sd-memo-q">${escapeHtml(memo.question || "")}</div>
    <div class="sd-memo-positions">${(memo.positions || []).map((p) =>
      `<div class="sd-memo-pos"><b>${who(p.who)}</b><span>${escapeHtml(p.position || "")}</span></div>`).join("")}</div>
    ${memo.dissent ? `<div class="sd-memo-dissent"><b>dissent</b><span>${escapeHtml(memo.dissent)}</span></div>` : ""}
    <div class="sd-memo-rec"><b>recommendation</b><span>${escapeHtml(memo.recommendation || "")}</span></div>
    <div class="sc-actions"><button class="sc-ctl" id="sdMemoAgain">▶ Run again</button></div>
  </div>`;
  $("#sdMemoClose").addEventListener("click", () => { host.hidden = true; });
  const again = $("#sdMemoAgain"); if (again && tid != null) again.addEventListener("click", () => sdRunGraph(tid));
}

// ---- the graph chat: talk to any agent, or to the pinned manager (the main use case) ----
let sdChatState = null;
async function sdOpenChat(tid, peer) {
  const host = $("#sdRosterHost"); if (!host || !lwWorldId) return;
  host.hidden = false;
  host.innerHTML = `<div class="sd-roster-card"><p class="dim">opening chat…</p></div>`;
  let room;
  try { room = (await api(`/api/lw/${lwWorldId}/room/${lwRoomId}`)).room; }
  catch (e) { host.innerHTML = `<div class="sd-roster-card"><p class="dim">Could not open chat: ${escapeHtml(e.message)}</p></div>`; return; }
  const threads = room.threads || [];
  const t = (tid != null && threads.find((x) => x.id === tid)) || threads[0];
  if (!t) { host.innerHTML = `<div class="sd-roster-card"><div class="sd-roster-head"><h3>Chat</h3><button class="sd-close" onclick="this.closest('#sdRosterHost').hidden=true">✕</button></div><p class="dim">Connect some tokens into a graph first.</p></div>`; return; }
  const agentById = new Map((room.agents || []).map((a) => [a.id, a]));
  const members = [...new Set((t.edges || []).flatMap((e) => [e[0], e[1]]))].map((id) => agentById.get(id)).filter(Boolean);
  sdChatState = { tid: t.id, peer: peer || (sdChatState && sdChatState.tid === t.id ? sdChatState.peer : "manager"), members, chats: t.chats || {}, search: "" };
  sdRenderChat();
}
function sdScrollChat() { const m = $("#sdChatMsgs"); if (m) m.scrollTop = m.scrollHeight; }
function sdRenderChat() {
  const host = $("#sdRosterHost"), st = sdChatState; if (!host || !st) return;
  const peers = [{ id: "manager", name: "Manager", manager: true }, ...st.members.map((a) => ({ id: a.id, name: a.name, agent: a }))];
  const q = (st.search || "").toLowerCase();
  const shown = peers.filter((p) => p.manager || p.name.toLowerCase().includes(q));   // the manager is always pinned + shown
  const av = (p) => p.manager ? `<div class="sd-chat-av mgr">★</div>`
    : `<img class="sd-chat-av" alt="" src="${lwSvgUri(lwAvatarSvg(lwAvatarSeed(p.agent), 36))}">`;
  const peerRow = (p) => {
    const on = String(p.id) === String(st.peer), last = (st.chats[String(p.id)] || []).slice(-1)[0];
    return `<button class="sd-chat-peer${on ? " on" : ""}${p.manager ? " pinned" : ""}" data-peer="${escapeHtml(String(p.id))}">
      ${av(p)}<span class="sd-chat-peer-main"><span class="sd-chat-peer-name">${escapeHtml(p.name)}${p.manager ? ` <span class="sd-chat-pin">pinned</span>` : (p.agent && p.agent.usage && p.agent.usage.asleep ? ` <span class="sd-chat-zzz">z</span>` : "")}</span>
      <span class="sd-chat-peer-last">${last ? escapeHtml(trim(last.text, 40)) : `<span class="dim">no messages yet</span>`}</span></span></button>`;
  };
  const active = peers.find((p) => String(p.id) === String(st.peer)) || peers[0];
  const convo = st.chats[String(st.peer)] || [];
  const msgs = convo.length ? convo.map((m) => `<div class="sd-msg ${m.role === "user" ? "me" : "them"}"><div class="sd-msg-b">${escapeHtml(m.text)}</div></div>`).join("")
    : `<p class="dim sd-chat-empty">Say hello to ${escapeHtml(active.name)}.${active.manager ? " The manager mediates the whole graph." : ""}</p>`;
  host.innerHTML = `<div class="sd-roster-card sd-chat-card">
    <div class="sd-roster-head"><h3>Chat</h3><span class="dim">graph ${st.tid}</span><button class="sd-close" id="sdChatClose">✕</button></div>
    <div class="sd-chat-body">
      <div class="sd-chat-list">
        <input class="sd-chat-search" placeholder="Search people…" value="${escapeHtml(st.search)}">
        <div class="sd-chat-peers">${shown.map(peerRow).join("")}</div>
      </div>
      <div class="sd-chat-pane">
        <div class="sd-chat-pane-head">${av(active)}<span>${escapeHtml(active.name)}${active.manager ? ` · <span class="dim">mediator</span>` : ""}</span></div>
        <div class="sd-chat-msgs" id="sdChatMsgs">${msgs}</div>
        <div class="sd-chat-compose"><input id="sdChatText" placeholder="Message ${escapeHtml(active.name)}…" autocomplete="off"><button id="sdChatSend" class="sc-ctl primary">Send</button></div>
      </div>
    </div></div>`;
  $("#sdChatClose").addEventListener("click", () => { host.hidden = true; });
  const search = host.querySelector(".sd-chat-search");
  search.addEventListener("input", (e) => { st.search = e.target.value; const at = e.target.selectionStart; sdRenderChat(); const s2 = host.querySelector(".sd-chat-search"); if (s2) { s2.focus(); s2.setSelectionRange(at, at); } });
  host.querySelectorAll(".sd-chat-peer").forEach((b) => b.addEventListener("click", () => { st.peer = b.dataset.peer; sdRenderChat(); }));
  const send = async () => {
    const inp = $("#sdChatText"), text = inp.value.trim(); if (!text) return;
    inp.value = "";
    (st.chats[String(st.peer)] = st.chats[String(st.peer)] || []).push({ role: "user", text });   // optimistic
    sdRenderChat(); sdScrollChat();
    const busy = $("#sdChatMsgs"); if (busy) busy.insertAdjacentHTML("beforeend", `<div class="sd-msg them" id="sdChatBusy"><div class="sd-msg-b dim">…</div></div>`); sdScrollChat();
    try {
      const r = await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/thread/${st.tid}/chat`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ to: st.peer, text }) });
      if (r.chat) st.chats[String(st.peer)] = r.chat;
    } catch (e) { toast(`Could not send: ${e.message}`); }
    sdRenderChat(); sdScrollChat();
  };
  $("#sdChatSend").addEventListener("click", send);
  $("#sdChatText").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send(); } });
  $("#sdChatText").focus();
  sdScrollChat();
}

function sdToggleRules() {
  const pop = $("#sdRulesPop"); if (!pop) return;
  if (!pop.hidden) { pop.hidden = true; return; }
  pop.innerHTML = `<div class="sd-rules-head">Scene rules <span class="dim">the whole room · a graph has its own rulebook</span></div>
    <textarea class="sc-input" id="sdRulesText" rows="4" placeholder="e.g. everyone stays in character; keep it civil; no one reveals their card."></textarea>
    <div class="sc-actions"><button class="sc-ctl primary" id="sdRulesSave">Save</button>
      <button class="sc-ctl" id="sdRulesClose">Close</button></div>`;
  pop.hidden = false;
  const ta = $("#sdRulesText"); if (ta) { ta.value = (lwRoom && lwRoom.rules) || ""; ta.focus(); }
  $("#sdRulesClose").addEventListener("click", () => { pop.hidden = true; });
  $("#sdRulesSave").addEventListener("click", async () => {
    const val = $("#sdRulesText").value || "";
    sdFlash();
    try {
      await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/scene`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rules: val }) });
      if (lwRoom) lwRoom.rules = val;
      pop.hidden = true;
    } catch (e) { toast(`Could not save rules: ${e.message}`); }
  });
}

async function openLifeworld(skipHash, worldId) {
  showLifeworld();
  lwWorldId = worldId || null;
  lwTab = "overview"; lwRoomId = null; lwRoom = null; lwWorld = null; lwSeenLog = new Set();
  if (!skipHash) setHash(worldId ? `#/lifeworld/${worldId}` : "#/lifeworld");
  await renderLifeworld();
}

async function renderLifeworld() {
  if ($("#lifeworld").hidden) return;
  if (lwWorldId) await renderWorkspace();
  else await renderWorldLobby();
}

// The mode the bar and stage are in: the lobby, one of the workspace tabs, or an
// open room (which lives under the Rooms tab but reveals the Live toggle + clock).
function lwMode() {
  if (!lwWorldId) return "lobby";
  if (lwTab === "rooms" && lwRoomId) return "room";
  return lwTab;
}

// The bar carries the tabs, the world title, the per-context "＋ New" buttons and —
// only inside a room — the Live toggle and world clock. Show only what the context
// owns; highlight the active tab (Rooms stays lit while a room is open).
function setLwBar(mode) {
  const inWorld = mode !== "lobby";
  const inRoom = mode === "room";
  $("#lwTabs").hidden = !inWorld;
  $("#lwTitle").hidden = !inWorld;
  $("#lwToLobby").hidden = !inWorld;
  $("#lwNewWorld").hidden = inWorld;
  $("#lwNewAgent").hidden = mode !== "agents";
  $("#lwNewArtifact").hidden = mode !== "artifacts";
  $("#lwNewRoom").hidden = mode !== "rooms";
  $("#lwLive").hidden = !inRoom;
  $("#lwTau").hidden = !inRoom;
  document.querySelectorAll(".lw-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.lwtab === lwTab));
}

// ---- small helpers -------------------------------------------------------
// Mood / pressure values may arrive as 0..1 or 0..100; normalise to a 0..100 int.
function lwPct(v) {
  let n = Number(v);
  if (!Number.isFinite(n)) return 0;
  if (n > 0 && n <= 1) n *= 100;
  return Math.max(0, Math.min(100, Math.round(n)));
}

// wants is either a single [name, pressure] pair or a list of them; pick the one
// under the most pressure as the dominant drive.
function dominantWant(wants) {
  if (!Array.isArray(wants) || !wants.length) return null;
  if (typeof wants[0] === "string") return { name: wants[0], pressure: wants[1] };
  let best = null;
  for (const w of wants) {
    if (!Array.isArray(w)) continue;
    if (!best || Number(w[1]) > Number(best[1])) best = w;
  }
  return best ? { name: best[0], pressure: best[1] } : null;
}

function lwWhen(t) {
  try { const dt = new Date(t); if (!isNaN(dt.getTime())) return dt.toLocaleDateString(); } catch (e) { /* fall through */ }
  return String(t);
}

// The dealable object in a room: a deck if one is placed, else the first prop.
function findDeck(room) {
  const props = (room && room.props) || [];
  return props.find((p) => (p.kind || "").includes("deck"))
    || props.find((p) => (p.kind || "").includes("card"))
    || null;
}

// A seat may carry the human id as human_id or id; failing that, resolve by name
// back to the world's agent registry. Returns the id to use for /human and /act.
function lwHumanId(f) {
  if (f && f.human_id != null) return f.human_id;
  const agents = (lwWorld && lwWorld.agents) || [];
  const byName = agents.find((p) => p.name === (f && f.name));
  if (byName) return byName.id;
  return f && f.id;
}

// Resolve a log 'who' / bond key (id or name) to a display name.
function lwNameOf(v) {
  if (v == null) return "";
  const agents = (lwWorld && lwWorld.agents) || [];
  const p = agents.find((x) => String(x.id) === String(v));
  return p ? (p.name || String(v)) : String(v);
}

function lwBilledCount(room) {
  const log = (room && room.log) || [];
  return log.filter((l) => l.billed || l.tier === 2).length;
}

// Each room's accent, exposed as CSS custom properties the cards/groups inherit.
function lwRoomHue(i) {
  const n = LW_ROOM_HUES.length;
  return LW_ROOM_HUES[(((i % n) + n) % n)];
}
function lwRoomStyle(i) {
  const h = lwRoomHue(i);
  return `--rc:hsl(${h} 58% 42%); --rc-soft:hsl(${h} 48% 95%); --rc-line:hsl(${h} 38% 84%)`;
}
function lwRoomIndex() {
  const idx = {};
  ((lwWorld && lwWorld.rooms) || []).forEach((r, i) => { idx[String(r.id)] = i; });
  return idx;
}

// ---- the world lobby -----------------------------------------------------
async function renderWorldLobby() {
  setLwBar("lobby");
  $("#lwDetail").hidden = true;
  const stage = $("#lwStage");
  let d;
  try { d = await api("/api/lw"); }
  catch (e) { stage.innerHTML = `<p class="empty">Could not load the Lifeworld: ${escapeHtml(e.message || String(e))}</p>`; return; }
  lwWorlds = d.worlds || [];
  if (!lwWorlds.length) {
    stage.innerHTML = `<div class="scene-lobby"><div class="scene-empty">
      <p>No worlds yet. A world is a small society — you fill it with people and objects, then place them into rooms.</p>
      <button class="primary" id="lwNew2">Create your first world</button>
    </div></div>`;
    $("#lwNew2").addEventListener("click", openWorldComposer);
    return;
  }
  const cards = lwWorlds.map((w) => `<button class="scene-card" data-open="${escapeHtml(String(w.id))}">
      <span class="scene-kind">world</span>
      <span class="scene-name">${escapeHtml(w.name || "Untitled world")}</span>
      <span class="scene-foot">${w.updated_at ? `<span>updated ${escapeHtml(lwWhen(w.updated_at))}</span>` : ""}</span>
    </button>`).join("");
  stage.innerHTML = `<div class="scene-lobby">
    <div class="lw-lobby-head"><h3>Your worlds</h3></div>
    <div class="scene-cards">${cards}</div></div>`;
  stage.querySelectorAll("[data-open]").forEach((b) => {
    b.addEventListener("click", () => openWorld(b.dataset.open));
    b.addEventListener("contextmenu", (ev) =>
      lwWorldMenu(ev, lwWorlds.find((x) => String(x.id) === b.dataset.open)));
  });
}

// Create a world with just a NAME — no preset. Inline composer, never a popup.
let lwWorldDraft = null;
function openWorldComposer() { lwWorldDraft = { name: "" }; renderWorldComposer(); }
function renderWorldComposer() {
  const box = $("#lwDetail");
  const d = lwWorldDraft;
  box.hidden = false;
  box.className = "studio-detail composing";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem" style="background:${sigil(d.name || "World", "anthropic")}">
        <span class="fig-initial">🌍</span></div>
      <div style="flex:1">
        <input class="sc-name" id="lwWName" placeholder="Name this world"
               value="${escapeHtml(d.name)}" autocomplete="off">
        <p class="sc-preview">a small society of your making</p>
      </div>
      <button class="sd-close" id="lwWClose">✕</button>
    </div>
    <p class="sc-hint">You'll add people, objects and rooms once it exists.</p>
    <div class="sc-actions"><button class="primary" id="lwWCreate">Create the world</button></div>`;
  $("#lwWName").addEventListener("input", (e) => { d.name = e.target.value; });
  $("#lwWClose").addEventListener("click", () => { lwWorldDraft = null; box.hidden = true; });
  $("#lwWCreate").addEventListener("click", doCreateWorld);
}
async function doCreateWorld() {
  const d = lwWorldDraft;
  try {
    const r = await api("/api/lw", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: d.name.trim() || "New world" }) });
    lwWorldDraft = null; $("#lwDetail").hidden = true;
    const id = r.world && r.world.id != null ? r.world.id : null;
    if (id != null) openWorld(id); else await renderLifeworld();
  } catch (e) { toast(`Could not create the world: ${e.message}`); }
}

function lwWorldMenu(ev, w) {
  ev.preventDefault(); ev.stopPropagation();
  if (!w) return;
  studioMenu(ev.clientX, ev.clientY, [
    { label: `Open ${w.name || "world"}`, act: () => openWorld(w.id) },
    { sep: true },
    { label: `Delete ${w.name || "world"}`, act: async () => {
        try { await api(`/api/lw/${w.id}`, { method: "DELETE" }); await renderWorldLobby(); }
        catch (e) { toast(`Could not delete: ${e.message}`); } } },
  ]);
}

async function openWorld(id) {
  lwWorldId = id; lwTab = "overview"; lwRoomId = null; lwRoom = null; lwWorld = null; lwSeenLog = new Set();
  $("#lwDetail").hidden = true;
  setHash(`#/lifeworld/${id}`);
  await renderWorkspace();
}

// ---- the workspace (Overview · Agents · Artifacts · Rooms) ---------------
async function loadWorld() {
  const d = await api(`/api/lw/${lwWorldId}`);
  lwWorld = d;
  if (Array.isArray(d.room_types) && d.room_types.length) lwRoomTypes = d.room_types;
}

async function renderWorkspace() {
  try { await loadWorld(); }
  catch (e) { $("#lwStage").innerHTML = `<p class="empty">Could not open world: ${escapeHtml(e.message || String(e))}</p>`; return; }
  $("#lwTitle").textContent = (lwWorld.world && lwWorld.world.name) || "World";
  await renderLwTab();
}

function selectLwTab(name) {
  lwDestroyCanvas();   // leaving the room view tears its Konva stage down
  lwTab = name;
  // A tab click always lands on that tab's top view — for Rooms, the list, not
  // whatever room was last open (openRoom is the only path that sets lwRoomId).
  lwRoomId = null; lwRoom = null;
  $("#lwDetail").hidden = true;
  renderLwTab();
}

async function renderLwTab() {
  setLwBar(lwMode());
  paintLwLive(); paintLwTau();
  if (lwTab === "overview") renderOverview();
  else if (lwTab === "agents") renderAgentsTab();
  else if (lwTab === "artifacts") renderArtifactsTab();
  else if (lwTab === "rooms") { if (lwRoomId) await renderRoomView(); else renderRoomsTab(); }
}

// ---- shared card builders ------------------------------------------------
function lwMoodBars(mood) {
  mood = mood || {};
  const bar = (label, v, cls) =>
    `<span class="lw-mbar ${cls}" title="${label} ${lwPct(v)}"><i style="width:${lwPct(v)}%"></i></span>`;
  return `<span class="lw-mood">${bar("confidence", mood.confidence, "conf")}${bar("stress", mood.stress, "stress")}</span>`;
}

function lwAgentCard(a, opts) {
  opts = opts || {};
  const skills = Array.isArray(a.skills)
    ? a.skills.slice().sort((x, y) => Number(y[1]) - Number(x[1])) : [];
  const top = skills[0];
  return `<button class="lw-card lw-agent-card" data-agent="${escapeHtml(String(a.id))}" style="${opts.style || ""}">
    <span class="lw-card-emblem" style="background:${sigil(a.name || "?", "anthropic")}">${escapeHtml((a.name || "?")[0] || "?")}</span>
    <span class="lw-card-body">
      <span class="lw-card-name">${escapeHtml(a.name || "someone")}</span>
      <span class="lw-card-sub">${escapeHtml(trim(a.narrative || "no story yet", 96))}</span>
      <span class="lw-card-foot">
        ${top ? `<span class="lw-skill">${escapeHtml(String(top[0]))} <i>${escapeHtml(String(top[1]))}</i></span>` : ""}
        ${lwMoodBars(a.mood)}
        ${opts.roomTag || ""}
      </span>
    </span>
  </button>`;
}

// An object tile — a deck looks like a small card stack, a card like a single back,
// anything else a lettered chip.
function lwArtifactCard(a, opts) {
  opts = opts || {};
  const kind = a.kind || "prop";
  const isDeck = kind.includes("deck");
  const isCard = kind.includes("card") && !isDeck;
  const visual = isDeck
    ? `<span class="lw-obj lw-obj-deck"><span class="pcard back"><span class="pcard-weave"></span></span><span class="pcard back"><span class="pcard-weave"></span></span><span class="pcard back"><span class="pcard-weave"></span></span></span>`
    : isCard
      ? `<span class="lw-obj"><span class="pcard back"><span class="pcard-weave"></span></span></span>`
      : `<span class="lw-obj lw-obj-tile">${escapeHtml((a.name || "?")[0] || "?")}</span>`;
  return `<button class="lw-card lw-artifact-card" data-artifact="${escapeHtml(String(a.id))}" style="${opts.style || ""}">
    ${visual}
    <span class="lw-card-body">
      <span class="lw-card-name">${escapeHtml(a.name || "object")}</span>
      <span class="lw-card-foot">
        <span class="lw-kind">${escapeHtml(kind)}</span>
        ${a.sealed ? `<span class="lw-sealed">🔒 sealed</span>` : ""}
        ${a.public ? `<span class="lw-pub">public</span>` : ""}
        ${opts.roomTag || ""}
      </span>
    </span>
  </button>`;
}

// ---- Overview: the map, grouped and coloured by room ---------------------
function lwGroupHtml(name, sub, style, agents, artifacts, roomAttr) {
  const cards = [
    ...agents.map((a) => lwAgentCard(a, {})),
    ...artifacts.map((a) => lwArtifactCard(a, {})),
  ].join("");
  const openBtn = roomAttr ? `<button class="lw-group-open" ${roomAttr}>open room →</button>` : "";
  return `<section class="lw-group" style="${style}">
    <header class="lw-group-head">
      <span class="lw-swatch"></span>
      <span class="lw-group-name">${escapeHtml(name)}</span>
      ${sub ? `<span class="lw-group-sub">${escapeHtml(sub)}</span>` : ""}
      <span class="lw-group-count">${agents.length + artifacts.length}</span>
      ${openBtn}
    </header>
    <div class="lw-group-cards">${cards || `<p class="lw-hint">empty — place people and objects here</p>`}</div>
  </section>`;
}

function renderOverview() {
  const stage = $("#lwStage");
  const rooms = (lwWorld && lwWorld.rooms) || [];
  const agents = (lwWorld && lwWorld.agents) || [];
  const artifacts = (lwWorld && lwWorld.artifacts) || [];

  const legend = rooms.length
    ? `<div class="lw-legend">${rooms.map((r, i) => {
        const nA = agents.filter((a) => String(a.room) === String(r.id)).length;
        const nO = artifacts.filter((a) => String(a.room) === String(r.id)).length;
        return `<button class="lw-legend-chip" data-room="${escapeHtml(String(r.id))}" style="${lwRoomStyle(i)}">
          <span class="lw-swatch"></span>
          <span class="lw-legend-name">${escapeHtml(r.name || r.type || "room")}</span>
          <span class="lw-legend-type">${escapeHtml(r.type || r.theme || "")}</span>
          <span class="lw-legend-count">${nA}👤 ${nO}▢</span>
        </button>`;
      }).join("")}</div>`
    : `<p class="lw-hint">No rooms yet — add one in the Rooms tab, then place people and objects into it.</p>`;

  const groups = [];
  rooms.forEach((r, i) => {
    const ga = agents.filter((a) => String(a.room) === String(r.id));
    const go = artifacts.filter((a) => String(a.room) === String(r.id));
    groups.push(lwGroupHtml(r.name || r.type || "room", r.type || r.theme || "", lwRoomStyle(i),
      ga, go, `data-room="${escapeHtml(String(r.id))}"`));
  });
  const upA = agents.filter((a) => a.room == null);
  const upO = artifacts.filter((a) => a.room == null);
  if (upA.length || upO.length)
    groups.push(lwGroupHtml("Unplaced", "not in any room yet",
      "--rc:var(--faint); --rc-soft:var(--card-2); --rc-line:var(--line-soft)", upA, upO, ""));

  stage.innerHTML = `<div class="lw-overview">
    <div class="lw-overview-head">
      <h3>The map</h3>
      <span class="lw-hint">${agents.length} people · ${artifacts.length} objects · ${rooms.length} rooms — coloured by room</span>
    </div>
    ${legend}
    <div class="lw-groups">${groups.join("") || `<p class="lw-hint">Nothing here yet. Create people and objects, add rooms, then place them.</p>`}</div>
  </div>`;

  stage.querySelectorAll(".lw-legend-chip").forEach((b) =>
    b.addEventListener("click", () => openRoom(b.dataset.room)));
  stage.querySelectorAll(".lw-group-open").forEach((b) =>
    b.addEventListener("click", () => openRoom(b.dataset.room)));
  stage.querySelectorAll("[data-agent]").forEach((b) =>
    b.addEventListener("click", () => openPersonDrawer(b.dataset.agent)));
  stage.querySelectorAll("[data-artifact]").forEach((b) =>
    b.addEventListener("click", () => lwOpenArtifactPeek(b.dataset.artifact)));
}

// ---- Agents tab: a gallery + the brief-based composer --------------------
function renderAgentsTab() {
  const stage = $("#lwStage");
  const agents = (lwWorld && lwWorld.agents) || [];
  const rooms = (lwWorld && lwWorld.rooms) || [];
  const idx = lwRoomIndex();
  if (!agents.length) {
    stage.innerHTML = `<div class="lw-gallery-empty">
      <p>No people yet. Everyone here is authored from a short brief — write who they are and the rest is filled in.</p>
      <button class="primary" id="lwAgentNew2">Create your first person</button>
    </div>`;
    $("#lwAgentNew2").addEventListener("click", openLwAgentComposer);
    return;
  }
  const cards = agents.map((a) => {
    const room = a.room != null ? rooms.find((r) => String(r.id) === String(a.room)) : null;
    const style = a.room != null ? lwRoomStyle(idx[String(a.room)] ?? 0) : "";
    const roomTag = room
      ? `<span class="lw-card-room">in ${escapeHtml(room.name || "a room")}</span>`
      : `<span class="lw-card-room unplaced">unplaced</span>`;
    return lwAgentCard(a, { style, roomTag });
  }).join("");
  stage.innerHTML = `<div class="lw-gallery">${cards}</div>`;
  stage.querySelectorAll("[data-agent]").forEach((b) =>
    b.addEventListener("click", () => openPersonDrawer(b.dataset.agent)));
}

let lwAgentDraft = null;
function openLwAgentComposer() { lwAgentDraft = { name: "", brief: "", parentsOn: false, parents: [] }; renderLwAgentComposer(); }
function renderLwAgentComposer() {
  const box = $("#lwDetail");
  const d = lwAgentDraft;
  const others = (lwWorld && lwWorld.agents) || [];
  box.hidden = false;
  box.className = "studio-detail composing";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem" style="background:${sigil(d.name || "New", "anthropic")}">
        <span class="fig-initial">${escapeHtml((d.name || "?")[0] || "?")}</span></div>
      <div style="flex:1">
        <input class="sc-name" id="lwANameInput" placeholder="Name (or leave blank)"
               value="${escapeHtml(d.name)}" autocomplete="off">
        <p class="sc-preview">a new person, authored from your brief</p>
      </div>
      <button class="sd-close" id="lwAClose">✕</button>
    </div>
    <div class="sc-label">Who is this person?</div>
    <textarea class="sc-input" id="lwABrief" rows="4"
      placeholder="A cautious accountant who loves poker on weekends… a sentence or two is plenty; the LLM authors the rest.">${escapeHtml(d.brief)}</textarea>
    <div class="sc-label">Choose parents <span class="dim">(optional — breed from two existing people)</span></div>
    <label class="lw-toggle"><input type="checkbox" id="lwParentsOn" ${d.parentsOn ? "checked" : ""}>
      <span>Breed from existing people</span></label>
    ${d.parentsOn ? (others.length
      ? `<div class="sc-row">${others.map((a) =>
          `<button class="sc-chip${d.parents.includes(String(a.id)) ? " on" : ""}" data-parent="${escapeHtml(String(a.id))}">${escapeHtml(a.name || "someone")}</button>`).join("")}</div>
         <p class="sc-hint">${d.parents.length}/2 chosen</p>`
      : `<p class="sc-hint">No existing people to breed from yet.</p>`) : ""}
    <div class="sc-actions"><button class="primary" id="lwACreate">Bring them to life</button></div>`;
  $("#lwANameInput").addEventListener("input", (e) => {
    d.name = e.target.value;
    box.querySelector(".fig-initial").textContent = (d.name || "?")[0] || "?";
  });
  $("#lwABrief").addEventListener("input", (e) => { d.brief = e.target.value; });
  $("#lwParentsOn").addEventListener("change", (e) => { d.parentsOn = e.target.checked; if (!d.parentsOn) d.parents = []; renderLwAgentComposer(); });
  box.querySelectorAll("[data-parent]").forEach((b) =>
    b.addEventListener("click", () => {
      const id = b.dataset.parent;
      if (d.parents.includes(id)) d.parents = d.parents.filter((x) => x !== id);
      else if (d.parents.length < 2) d.parents = [...d.parents, id];
      renderLwAgentComposer();
    }));
  $("#lwAClose").addEventListener("click", () => { lwAgentDraft = null; box.hidden = true; });
  $("#lwACreate").addEventListener("click", doCreateAgent);
}
async function doCreateAgent() {
  const d = lwAgentDraft;
  const agents = (lwWorld && lwWorld.agents) || [];
  const body = { name: d.name.trim(), brief: d.brief.trim() };
  if (d.parentsOn && d.parents.length) {
    body.parents = d.parents.slice(0, 2).map((pid) => {
      const a = agents.find((x) => String(x.id) === pid);
      return a ? a.id : pid;
    });
  }
  try {
    await api(`/api/lw/${lwWorldId}/human`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    lwAgentDraft = null; $("#lwDetail").hidden = true;
    await renderWorkspace();
  } catch (e) { toast(`Could not create them: ${e.message}`); }
}

// ---- Artifacts tab: a gallery + the brief-based composer -----------------
function renderArtifactsTab() {
  const stage = $("#lwStage");
  const artifacts = (lwWorld && lwWorld.artifacts) || [];
  const rooms = (lwWorld && lwWorld.rooms) || [];
  const idx = lwRoomIndex();
  if (!artifacts.length) {
    stage.innerHTML = `<div class="lw-gallery-empty">
      <p>No objects yet. Describe a thing in a line — "a deck of cards", "a whiteboard" — and it is authored into the world.</p>
      <button class="primary" id="lwArtNew2">Create your first object</button>
    </div>`;
    $("#lwArtNew2").addEventListener("click", openLwArtifactComposer);
    return;
  }
  const cards = artifacts.map((a) => {
    const room = a.room != null ? rooms.find((r) => String(r.id) === String(a.room)) : null;
    const style = a.room != null ? lwRoomStyle(idx[String(a.room)] ?? 0) : "";
    const roomTag = room
      ? `<span class="lw-card-room">in ${escapeHtml(room.name || "a room")}</span>`
      : `<span class="lw-card-room unplaced">unplaced</span>`;
    return lwArtifactCard(a, { style, roomTag });
  }).join("");
  stage.innerHTML = `<div class="lw-gallery">${cards}</div>`;
  stage.querySelectorAll("[data-artifact]").forEach((b) =>
    b.addEventListener("click", () => lwOpenArtifactPeek(b.dataset.artifact)));
}

let lwArtifactDraft = null;
function openLwArtifactComposer() { lwArtifactDraft = { name: "", brief: "" }; renderLwArtifactComposer(); }
function renderLwArtifactComposer() {
  const box = $("#lwDetail");
  const d = lwArtifactDraft;
  box.hidden = false;
  box.className = "studio-detail composing";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem" style="background:${sigil(d.name || "Object", "anthropic")}">
        <span class="fig-initial">▢</span></div>
      <div style="flex:1">
        <input class="sc-name" id="lwArtNameInput" placeholder="Name this object"
               value="${escapeHtml(d.name)}" autocomplete="off">
        <p class="sc-preview">a thing in the world</p>
      </div>
      <button class="sd-close" id="lwArtClose">✕</button>
    </div>
    <div class="sc-label">What is this thing?</div>
    <textarea class="sc-input" id="lwArtBrief" rows="3"
      placeholder="e.g. a deck of cards; a whiteboard; a coffee machine">${escapeHtml(d.brief)}</textarea>
    <div class="sc-actions"><button class="primary" id="lwArtCreate">Make it</button></div>`;
  $("#lwArtNameInput").addEventListener("input", (e) => { d.name = e.target.value; });
  $("#lwArtBrief").addEventListener("input", (e) => { d.brief = e.target.value; });
  $("#lwArtClose").addEventListener("click", () => { lwArtifactDraft = null; box.hidden = true; });
  $("#lwArtCreate").addEventListener("click", doCreateArtifact);
}
async function doCreateArtifact() {
  const d = lwArtifactDraft;
  try {
    await api(`/api/lw/${lwWorldId}/artifact`, { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: d.name.trim(), brief: d.brief.trim() }) });
    lwArtifactDraft = null; $("#lwDetail").hidden = true;
    await renderWorkspace();
  } catch (e) { toast(`Could not make it: ${e.message}`); }
}

// A light peek at an object — name, kind, where it sits, whether it is sealed.
function lwOpenArtifactPeek(aid) {
  const artifacts = (lwWorld && lwWorld.artifacts) || [];
  const a = artifacts.find((x) => String(x.id) === String(aid));
  if (!a) return;
  const rooms = (lwWorld && lwWorld.rooms) || [];
  const room = a.room != null ? rooms.find((r) => String(r.id) === String(a.room)) : null;
  const box = $("#lwDetail");
  box.hidden = false; box.className = "studio-detail";
  box.innerHTML = `
    <div class="sd-head">
      <div class="lw-obj lw-obj-tile lw-head-obj">${escapeHtml((a.name || "?")[0] || "?")}</div>
      <div style="flex:1"><h3>${escapeHtml(a.name || "object")}</h3>
        <p class="sd-persona">${escapeHtml(a.kind || "object")}${room ? ` · in ${escapeHtml(room.name || "a room")}` : " · unplaced"}</p></div>
      <button class="sd-close" id="lwObjClose">✕</button>
    </div>
    <div class="sd-facts">
      <span>${a.sealed ? "🔒 sealed" : "open"}</span>
      <span>${a.public ? "public" : "private"}</span>
      <span>${escapeHtml(a.kind || "object")}</span>
    </div>`;
  $("#lwObjClose").addEventListener("click", () => { box.hidden = true; });
}

// ---- Rooms tab: room cards + the type-picking composer -------------------
function renderRoomsTab() {
  const stage = $("#lwStage");
  const rooms = (lwWorld && lwWorld.rooms) || [];
  if (!rooms.length) {
    stage.innerHTML = `<div class="lw-gallery-empty">
      <p>No rooms yet. A room is a place with a type — a home, a classroom, an office, a casino — where your people gather.</p>
      <button class="primary" id="lwRoomNew2">Add your first room</button>
    </div>`;
    $("#lwRoomNew2").addEventListener("click", openLwRoomComposer);
    return;
  }
  const agents = (lwWorld && lwWorld.agents) || [];
  const artifacts = (lwWorld && lwWorld.artifacts) || [];
  const cards = rooms.map((r, i) => {
    const nA = agents.filter((a) => String(a.room) === String(r.id)).length;
    const nO = artifacts.filter((a) => String(a.room) === String(r.id)).length;
    const blurb = (lwRoomTypes.find((t) => t.type === r.type) || {}).blurb || r.blurb || "";
    return `<button class="lw-room-card" data-room="${escapeHtml(String(r.id))}" style="${lwRoomStyle(i)}">
      <span class="lw-room-card-name">${escapeHtml(r.name || "room")}</span>
      <span class="lw-room-card-type">${escapeHtml(r.type || "")}${r.theme ? ` · ${escapeHtml(r.theme)}` : ""}</span>
      ${blurb ? `<span class="lw-room-card-blurb">${escapeHtml(blurb)}</span>` : ""}
      <span class="lw-room-card-foot">${nA} seated · ${nO} objects</span>
    </button>`;
  }).join("");
  stage.innerHTML = `<div class="lw-room-grid">${cards}</div>`;
  stage.querySelectorAll("[data-room]").forEach((b) => {
    b.addEventListener("click", () => openRoom(b.dataset.room));
    b.addEventListener("contextmenu", (ev) => lwRoomCardMenu(ev, rooms.find((r) => String(r.id) === b.dataset.room)));
  });
}

function lwRoomCardMenu(ev, r) {
  ev.preventDefault(); ev.stopPropagation();
  if (!r) return;
  studioMenu(ev.clientX, ev.clientY, [
    { label: `Open ${r.name || "room"}`, act: () => openRoom(r.id) },
  ]);
}

let lwRoomDraft = null;
async function openLwRoomComposer() {
  lwRoomDraft = { name: "", type: "" };
  if (!lwRoomTypes.length) {
    try { const d = await api(`/api/lw/${lwWorldId}/room-types`); lwRoomTypes = d.types || []; }
    catch (e) { /* fall back to whatever the overview provided */ }
  }
  if (!lwRoomDraft.type && lwRoomTypes[0]) lwRoomDraft.type = lwRoomTypes[0].type;
  renderLwRoomComposer();
}
function renderLwRoomComposer() {
  const box = $("#lwDetail");
  const d = lwRoomDraft;
  const cur = lwRoomTypes.find((t) => t.type === d.type);
  box.hidden = false;
  box.className = "studio-detail composing";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem" style="background:${sigil(d.name || d.type || "Room", "anthropic")}">
        <span class="fig-initial">🚪</span></div>
      <div style="flex:1">
        <input class="sc-name" id="lwRNameInput" placeholder="Name this room"
               value="${escapeHtml(d.name)}" autocomplete="off">
        <p class="sc-preview">${escapeHtml(cur ? (cur.blurb || cur.type) : "a place for your people")}</p>
      </div>
      <button class="sd-close" id="lwRClose">✕</button>
    </div>
    <div class="sc-label">Type</div>
    <div class="sc-row">${lwRoomTypes.length
      ? lwRoomTypes.map((t) =>
          `<button class="sc-chip${d.type === t.type ? " on" : ""}" data-rtype="${escapeHtml(t.type)}" title="${escapeHtml(t.blurb || "")}">${escapeHtml(t.type)}</button>`).join("")
      : `<span class="sc-hint">No room types available.</span>`}</div>
    ${cur && cur.blurb ? `<p class="sc-hint">${escapeHtml(cur.blurb)}</p>` : ""}
    <div class="sc-actions"><button class="primary" id="lwRCreate">Add the room</button></div>`;
  $("#lwRNameInput").addEventListener("input", (e) => { d.name = e.target.value; });
  box.querySelectorAll("[data-rtype]").forEach((b) =>
    b.addEventListener("click", () => { d.type = b.dataset.rtype; renderLwRoomComposer(); }));
  $("#lwRClose").addEventListener("click", () => { lwRoomDraft = null; box.hidden = true; });
  $("#lwRCreate").addEventListener("click", doCreateRoom);
}
async function doCreateRoom() {
  const d = lwRoomDraft;
  try {
    const r = await api(`/api/lw/${lwWorldId}/room`, { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: d.name.trim() || (d.type || "Room"), type: d.type }) });
    lwRoomDraft = null; $("#lwDetail").hidden = true;
    const rid = r.room && r.room.id != null ? r.room.id : null;
    await loadWorld();
    if (rid != null) { lwTab = "rooms"; lwRoomId = rid; lwSeenLog = new Set(); await renderRoomView(); }
    else renderRoomsTab();
  } catch (e) { toast(`Could not add the room: ${e.message}`); }
}

// ---- the room view (themed by room.theme) --------------------------------
async function openRoom(rid) {
  lwTab = "rooms"; lwRoomId = rid; lwRoom = null; lwSeenLog = new Set();
  $("#lwDetail").hidden = true;
  await renderRoomView();
}

// The room is a Konva canvas now, not a themed CSS set-piece. Fetch, then paint.
async function renderRoomView() {
  let d;
  try { d = await api(`/api/lw/${lwWorldId}/room/${lwRoomId}`); }
  catch (e) { $("#lwStage").innerHTML = `<p class="empty">Could not open room: ${escapeHtml(e.message || String(e))}</p>`; return; }
  lwRenderRoom(d.room || d);
}

// Re-fetch this room and repaint — used after a seat/unseat/create/round, where
// the server is the source of truth. The per-room view cache keeps pan/zoom.
async function lwReloadRoom() {
  lwLogOn && lwLog("life", "reload room (refetch + full repaint)", null, "info");
  try { const d = await api(`/api/lw/${lwWorldId}/room/${lwRoomId}`); lwRenderRoom(d.room || d); }
  catch (e) { toast(`Could not refresh the room: ${e.message}`); }
}

// Paint the scene: a full-screen canvas with a floating toolbox, a video-style time
// transport, and a rules button — nothing else on top of the floor. Free strings reach
// innerHTML through escapeHtml; on-canvas labels are Konva text (drawn to canvas).
function lwRenderRoom(room) {
  if (lwKonva && lwKonva.drag) { lwLog("life", "rebuild while dragging — aborting the drag first", null, "warn"); lwForceIdle(); }
  lwDestroyCanvas();
  lwRoom = room;
  const stage = $("#lwStage");
  const agents = room.agents || room.seats || [];
  const props = room.props || [];
  // reflect this scene in the top bar (never clobber the title mid-edit)
  const t = $("#sdTitle"); if (t && document.activeElement !== t) t.textContent = room.name || "untitled";
  paintLwLive();

  stage.innerHTML = `<div class="sd-room">
    <div class="lw-canvas-wrap sd-canvas" id="lwCanvasWrap">
      <div class="lw-konva-host" id="lwKonvaHost"></div>
      <div class="lw-overlay" id="lwOverlay"></div>
      <div class="sd-hint">drag a token to move it · drag empty to select · <b>space</b>-drag to pan · scroll to zoom · <b>F</b> fit</div>
      <div class="lw-dock sd-dock" id="lwDock">${lwDockHtml()}</div>
      <button class="sd-rules-btn" id="sdRulesBtn" title="Scene rules — obeyed on every run">⚖ Rules</button>
      <div class="sd-rules-pop" id="sdRulesPop" hidden></div>
      <div class="sd-activity" id="sdActivity"${sdActOpen ? "" : " hidden"}></div>
      ${sdTimeBarHtml()}
    </div>`;

  lwWireDock();
  sdWireTime();
  paintLwTau();
  sdSavedIdle();
  const rb = $("#sdRulesBtn"); if (rb) rb.addEventListener("click", sdToggleRules);
  if (sdActOpen) sdShowActivity(true);      // refresh the activity panel after a beat
  lwMountCanvas(room, agents, props);

  // Everything on screen is now "seen"; only genuinely new lines animate next time.
  lwSeenLog = new Set((room.log || []).map((l) => String(l.n)));
}

