// canvas1.js — Figures (generated avatars/glyphs, shared with canvas v2 via window.*) + the Konva v1 canvas engine (fallback when CANVAS_V2=0), the create flow, context menus, speech bubbles, beats, and the person drawer.
// Split from the old monolithic app.js (order preserved; classic scripts share one global scope; index.html defines load order).

// ============================================================================
// Figures — generated, not emoji. People wear deterministic "beam" avatars (a
// face drawn from a hash of who they are, so everyone looks unique); objects wear
// crisp vector glyphs. Both are pure SVG data-URIs: identical in the HTML palette
// and on the Konva canvas, self-hosted, no assets, no external fetch. The avatar
// maths is adapted from the MIT "boring-avatars" beam generator.
// ============================================================================
const LW_AV_PALETTES = [
  ["#2E6E5B", "#8FC0A9", "#F6D6A8", "#E8896B", "#C05746"],
  ["#3A6EA5", "#9DC3E6", "#F4E7C3", "#E0A96D", "#B5654B"],
  ["#5B4B8A", "#B7A6E0", "#F3D2C1", "#EFA48B", "#8A5A83"],
  ["#4C7A34", "#A7C957", "#F2E8CF", "#E9B44C", "#BC4B51"],
  ["#1D6A70", "#63C7B2", "#F6E7B4", "#F2A15E", "#D46A6A"],
];
function lwHash(name) {
  let h = 0; name = String(name || "?");
  for (let i = 0; i < name.length; i++) { h = (h << 5) - h + name.charCodeAt(i); h |= 0; }
  return Math.abs(h);
}
function lwAvDigit(n, k) { return Math.floor((n / Math.pow(10, k)) % 10); }
function lwAvBool(n, k) { return (lwAvDigit(n, k) % 2) === 0; }
function lwAvUnit(n, range, index) {
  const v = n % range;
  return (index && lwAvDigit(n, index) % 2 === 0) ? -v : v;
}
function lwAvContrast(hex) {
  const c = hex.replace("#", "");
  const r = parseInt(c.slice(0, 2), 16), g = parseInt(c.slice(2, 4), 16), b = parseInt(c.slice(4, 6), 16);
  return (r * 0.299 + g * 0.587 + b * 0.114) > 150 ? "#22303a" : "#ffffff";
}
// The face a seed produces, and its SVG. S=36 canvas, masked to a circle.
function lwAvatarSvg(seed, size) {
  const n = lwHash(seed), S = 36, pal = LW_AV_PALETTES[n % LW_AV_PALETTES.length];
  size = size || 40;
  const wrap = pal[n % pal.length], bg = pal[(n + 13) % pal.length], face = lwAvContrast(wrap);
  const preX = lwAvUnit(n, 10, 1), preY = lwAvUnit(n, 10, 2);
  const wtx = preX < 5 ? preX + S / 9 : preX, wty = preY < 5 ? preY + S / 9 : preY;
  const wrot = lwAvUnit(n, 360), wscale = 1 + lwAvUnit(n, S / 12) / 10;
  const mouthOpen = lwAvBool(n, 2), eye = lwAvUnit(n, 5), mouth = lwAvUnit(n, 3);
  const frot = lwAvUnit(n, 10, 3);
  const ftx = wtx > S / 6 ? wtx / 2 : lwAvUnit(n, 8, 1), fty = wty > S / 6 ? wty / 2 : lwAvUnit(n, 7, 2);
  const mid = S / 2, mid2 = "m" + (n % 1e6);
  return `<svg viewBox="0 0 ${S} ${S}" width="${size}" height="${size}" fill="none" xmlns="http://www.w3.org/2000/svg">`
    + `<mask id="${mid2}" maskUnits="userSpaceOnUse" x="0" y="0" width="${S}" height="${S}">`
    + `<rect width="${S}" height="${S}" rx="${S * 2}" fill="#fff"/></mask>`
    + `<g mask="url(#${mid2})"><rect width="${S}" height="${S}" fill="${bg}"/>`
    + `<rect x="0" y="0" width="${S}" height="${S}" fill="${wrap}" `
    + `transform="translate(${wtx} ${wty}) rotate(${wrot} ${mid} ${mid}) scale(${wscale})"/>`
    + `<g transform="translate(${ftx} ${fty}) rotate(${frot} ${mid} ${mid})">`
    + (mouthOpen
      ? `<path d="M15 ${19 + mouth}c2 1 4 1 6 0" stroke="${face}" fill="none" stroke-linecap="round"/>`
      : `<path d="M13,${19 + mouth} a1,0.75 0 0,0 10,0" fill="${face}"/>`)
    + `<rect x="${14 - eye}" y="14" width="1.5" height="2" rx="1" fill="${face}"/>`
    + `<rect x="${20 + eye}" y="14" width="1.5" height="2" rx="1" fill="${face}"/>`
    + `</g></g></svg>`;
}
function lwAvatarColor(seed) { const n = lwHash(seed); return LW_AV_PALETTES[n % LW_AV_PALETTES.length][n % 5]; }
function lwAvatarBg(seed) { const n = lwHash(seed); return LW_AV_PALETTES[n % LW_AV_PALETTES.length][(n + 13) % 5]; }
function lwSvgUri(svg) { return "data:image/svg+xml," + encodeURIComponent(svg); }

// The seed a token's face is drawn from: variant marker + who they are, so the
// choice in the popover survives, but two people never collide.
function lwAvatarSeed(a) {
  const f = String((a && a.figure) || ""), id = (a && (a.name || ("soul#" + a.id))) || "soul";
  if (f.startsWith("avm:")) return f.slice(4) + "|" + id;
  if (f.startsWith("av:")) return f.slice(3) + "|" + id;
  return id;
}

// ---- object vector glyphs (line icons; mono handled inline as a monogram) ----
function lwObjGlyphSvg(key, size, color) {
  size = size || 40; color = color || "#3a4a44";
  const body = { stroke: color, "stroke-width": 1.7, fill: "none", "stroke-linejoin": "round", "stroke-linecap": "round" };
  const attr = Object.entries(body).map(([k, v]) => `${k}="${v}"`).join(" ");
  const paths = {
    cards: `<rect x="4.5" y="7" width="10" height="13" rx="2" ${attr}/><rect x="9.5" y="4" width="10" height="13" rx="2" ${attr}/>`,
    doc: `<path d="M7 3h7l4 4v14H7z" ${attr}/><path d="M14 3v4h4" ${attr}/><path d="M9.5 12h6M9.5 15.5h6" ${attr}/>`,
    star: `<path d="M12 3.2l2.5 5.6 6.1.6-4.6 4 1.4 6-5.4-3.1-5.4 3.1 1.4-6-4.6-4 6.1-.6z" ${attr}/>`,
    gem: `<path d="M6.5 4h11l3 5-8.5 11L3.5 9z" ${attr}/><path d="M3.5 9h17M9 4l-2 5 5 11 5-11-2-5" ${attr}/>`,
    ring: `<circle cx="12" cy="12" r="8" ${attr}/><circle cx="12" cy="12" r="3.4" ${attr}/>`,
  };
  const inner = paths[key] || paths.ring;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" xmlns="http://www.w3.org/2000/svg">${inner}</svg>`;
}

// ---- dock: tools with vector icons + a keycap ------------------------------
function lwToolIco(id) {
  const a = `stroke="currentColor" stroke-width="1.8" fill="none" stroke-linejoin="round" stroke-linecap="round"`;
  const g = {
    select: `<path d="M5 3l6.5 15 2-6 6-2z" ${a}/>`,
    agent: `<circle cx="12" cy="8.2" r="3.6" ${a}/><path d="M5.5 20c0-3.6 2.9-6.2 6.5-6.2s6.5 2.6 6.5 6.2" ${a}/>`,
    artifact: `<rect x="5" y="5" width="14" height="14" rx="3.2" ${a}/><path d="M9.5 12h5" ${a}/>`,
    shape: `<circle cx="12" cy="12" r="4.4" ${a}/><circle cx="12" cy="4.4" r="1.7" fill="currentColor" stroke="none"/><circle cx="18.8" cy="15.8" r="1.7" fill="currentColor" stroke="none"/><circle cx="5.2" cy="15.8" r="1.7" fill="currentColor" stroke="none"/>`,
  };
  return `<svg viewBox="0 0 24 24" width="20" height="20">${g[id] || g.select}</svg>`;
}
function lwDockHtml() {
  const tools = LW_TOOLS.map((t) =>
    `<button class="lw-tool${t.id === lwTool ? " on" : ""}" data-tool="${escapeHtml(t.id)}" title="${escapeHtml(t.label)} (${escapeHtml(t.key)})">
      <span class="lw-tool-ico">${lwToolIco(t.id)}</span><span class="lw-tool-lb">${escapeHtml(t.label)}</span>
      <span class="lw-tool-key">${escapeHtml(t.key)}</span>
    </button>`).join("");
  // How a connection you draw flows — two explicit choices, not one ambiguous toggle. The active
  // one is highlighted; drag a token's handle onto another to draw a link of that kind.
  const dir = `<span class="lw-dock-sep"></span>
    <button class="lw-dirbtn${lwThreadDir === "both" ? " on" : ""}" data-dir="both" title="Two-way link — both hear each other">⇄ link</button>
    <button class="lw-dirbtn${lwThreadDir === "one" ? " on" : ""}" data-dir="one" title="One-way arrow — only tail → head hears">→ arrow</button>`;
  return tools + dir;
}
function lwWireDock() {
  const dock = $("#lwDock"); if (!dock) return;
  dock.querySelectorAll("[data-tool]").forEach((b) =>
    b.addEventListener("click", () => lwSetTool(b.dataset.tool)));
  dock.querySelectorAll("[data-dir]").forEach((b) =>
    b.addEventListener("click", () => {
      lwThreadDir = b.dataset.dir;
      dock.querySelectorAll("[data-dir]").forEach((x) => x.classList.toggle("on", x.dataset.dir === lwThreadDir));
    }));
}
function lwToolCursor(t) { return t === "select" ? "default" : "cell"; }
function lwSetCursor(c) {
  if (!(lwKonva && lwKonva.host)) return;
  if (lwLogOn && lwKonva.host.style.cursor !== c) lwLog("cursor", "cursor → " + c, null, "debug");
  lwKonva.host.style.cursor = c;
}
function lwSetTool(t) {
  lwTool = t;
  const dock = $("#lwDock");
  if (dock) dock.querySelectorAll("[data-tool]").forEach((b) => b.classList.toggle("on", b.dataset.tool === t));
  if (lwCanvasV2On()) { window.LWCanvas2.setTool(t); return; }
  lwUpdateHandles();                             // handles belong to select mode only
  if (lwKonva && lwKonva.stage) {
    lwKonva.panning = false;                    // a stale pan flag would keep the cursor/marquee dead
    lwKonva.stage.draggable(false);            // pan is space-drag; tokens always drag, empty marquees
    lwSetCursor(lwToolCursor(t));
  }
}

// ---- selection: a SET of tokens, each wearing a tight shape-hugging outline --
// Shift-click adds, marquee and ⌘/Ctrl-A grab many, so a whole arrangement moves as
// one. The cue is a crisp accent outline that hugs each token's own shape plus four
// small handles (a Figma-like "grabbable" mark) — never a big detached ring.
function lwBodyOf(entry) { return entry.node.findOne(".body"); }
function lwSelKey(kind, entry) { return kind + ":" + entry.data.id; }
function lwSelList() { return lwKonva ? [...lwKonva.sel.values()] : []; }
function lwSelHas(entry) { return !!lwKonva && lwSelList().some((s) => s.entry === entry); }
function lwAdorn(entry) {
  const node = entry.node, d = entry.data, A = "#2E6E5B";
  const isAgent = lwKonva.agents.has(String(d.id));
  const slots = Number(d.slots) || 0, shape = String(d.shape || "circle");
  const g = new Konva.Group({ name: "seladorn", listening: false });
  if (isAgent) {
    g.add(new Konva.Circle({ radius: 35, stroke: A, strokeWidth: 2.5 }));
  } else if (slots > 0 && shape === "rect") {
    g.add(new Konva.Rect({ x: -(LW_RECT_W / 2 + 5), y: -(LW_RECT_H / 2 + 5), width: LW_RECT_W + 10, height: LW_RECT_H + 10, cornerRadius: 20, stroke: A, strokeWidth: 2.5 }));
  } else if (slots > 0 && shape === "path" && Array.isArray(d.path) && d.path.length >= 3) {
    g.add(new Konva.Line({ points: lwGrowPath(d.path, 5).flatMap((p) => [p.x, p.y]), closed: true, stroke: A, strokeWidth: 2.5 }));
  } else if (slots > 0) {
    g.add(new Konva.Circle({ radius: LW_TABLE_R + 5, stroke: A, strokeWidth: 2.5 }));
  } else {
    const body = lwBodyOf(entry);
    const rc = body ? body.getClientRect({ relativeTo: node, skipShadow: true }) : { x: -29, y: -29, width: 58, height: 58 };
    g.add(new Konva.Rect({ x: rc.x - 5, y: rc.y - 5, width: rc.width + 10, height: rc.height + 10, cornerRadius: 10, stroke: A, strokeWidth: 2.5 }));
  }
  node.add(g);                                    // attach first so the measure is valid
  const bb = g.getClientRect({ relativeTo: node });
  [[bb.x, bb.y], [bb.x + bb.width, bb.y], [bb.x + bb.width, bb.y + bb.height], [bb.x, bb.y + bb.height]].forEach(([hx, hy]) =>
    g.add(new Konva.Rect({ x: hx - 3, y: hy - 3, width: 6, height: 6, cornerRadius: 1.5, fill: "#fff", stroke: A, strokeWidth: 1.5 })));
  g.moveToTop();
}
function lwSelClear() {
  if (!lwKonva) return;
  if (lwLogOn && lwKonva.sel.size) lwLog("select", "clear", { was: lwKonva.sel.size }, "debug");
  lwKonva.sel.forEach((s) => { const a = s.entry.node.findOne(".seladorn"); if (a) a.destroy(); });
  lwKonva.sel.clear();
  lwUpdateSelFrame();
  lwKonva.worldLayer.batchDraw();
}
function lwSelAdd(kind, entry) {
  if (!lwKonva || lwSelHas(entry)) return;
  lwKonva.sel.set(lwSelKey(kind, entry), { kind, entry });
  lwAdorn(entry);
  lwLogOn && lwLog("select", `add ${kind}#${entry && entry.data && entry.data.id}`, { total: lwKonva.sel.size }, "debug");
  lwUpdateSelFrame();
  lwKonva.worldLayer.batchDraw();
}
function lwSelRemove(entry) {
  if (!lwKonva) return;
  for (const [k, s] of lwKonva.sel) if (s.entry === entry) {
    const a = s.entry.node.findOne(".seladorn"); if (a) a.destroy();
    lwKonva.sel.delete(k);
  }
  lwUpdateSelFrame();
  lwKonva.worldLayer.batchDraw();
}
function lwSelSet(kind, entry) { lwSelClear(); lwSelAdd(kind, entry); }
function lwSelToggle(kind, entry) { if (lwSelHas(entry)) lwSelRemove(entry); else lwSelAdd(kind, entry); }
function lwSelAll() {
  if (!lwKonva) return;
  lwSelClear();
  lwKonva.props.forEach((e) => lwSelAdd("prop", e));
  lwKonva.agents.forEach((e) => lwSelAdd("agent", e));
}
// A draggable frame around a MULTI-selection: press anywhere inside it — token or gap —
// to move the whole group as one. A single selection needs none (its token drags directly).
function lwSelBounds() {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  lwSelList().forEach((s) => {
    const p = s.entry.node.position();
    const r = s.kind === "agent" ? 38 : lwPropRadius(s.entry.data) + 18;
    minX = Math.min(minX, p.x - r); minY = Math.min(minY, p.y - r);
    maxX = Math.max(maxX, p.x + r); maxY = Math.max(maxY, p.y + r);
  });
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}
function lwUpdateSelFrame() {
  if (!lwKonva) return;
  lwUpdateHandles();                                  // a single selection wears 4 connection handles
  const old = lwKonva.worldLayer.findOne(".selframe"); if (old) old.destroy();
  lwKonva.selframe = null;
  if (lwSelList().length < 2) return;                 // single selection drags its own token
  const b = lwSelBounds();
  // PURELY VISUAL. The frame must be `listening:false`: a draggable/listening Rect paints an
  // opaque colorKey across its whole area on the HIT canvas (even at 0.05 fill), which occludes
  // every selected token — so getIntersection returns the frame, not the token, and the tokens'
  // own click/dblclick/hover handlers go dark for the rest of the session. That was the root of
  // the "selection & double-click become inconsistent after grouping" bug. Group DRAG does not
  // need the frame: dragging any selected node moves the whole group (lwDragBegin → group:true).
  const frame = new Konva.Rect({ x: b.x, y: b.y, width: b.w, height: b.h, cornerRadius: 12,
    fill: "rgba(46,110,91,0.05)", stroke: "#2E6E5B", strokeWidth: 1.5, dash: [7, 5], name: "selframe", listening: false });
  lwKonva.worldLayer.add(frame); frame.moveToTop();
  lwKonva.selframe = frame;
  lwKonva.worldLayer.batchDraw();
}
function lwSelect(kind, entry) { lwSelSet(kind, entry); }   // legacy single-focus callers
function lwDeselect() { lwSelClear(); }

// Arrows/flows were retired; this hook now keeps the thread graph's lines and a node's
// connection handles glued to their tokens as they drag. Called from every drag-move.
function lwUpdateArrows() { lwUpdateThreadLines(); lwUpdateHandlePositions(); }
function lwUpdateHandlePositions() {
  if (!lwKonva || !lwKonva.handles || !lwKonva.handles.length || lwKonva.connecting) return;
  const list = lwSelList(); if (list.length !== 1) return;
  const base = list[0].entry.node.position(), reach = lwHandleReach(list[0].entry), off = [[0, -reach], [reach, 0], [0, reach], [-reach, 0]];
  lwKonva.handles.forEach((h, i) => h.position({ x: base.x + off[i][0], y: base.y + off[i][1] }));
}

// ---- keyboard: the canvas listens while a room is open ---------------------
let lwNudgeTimer = null;
function lwOpenSelected() {
  const list = lwSelList(); if (!list.length) return;
  const s = list[0];
  if (s.kind === "agent") openAgentPage(s.entry.data.id);
  else lwOpenArtifactPeek(s.entry.data.id);
}
function lwNudgeSelection(dx, dy) {
  const list = lwSelList().filter((s) => !(s.kind === "agent" && s.entry.seat));   // seated agents are pinned
  if (!list.length) return;
  list.forEach((s) => {
    const p = s.entry.node.position();
    s.entry.node.position({ x: p.x + dx, y: p.y + dy });
    if (s.kind === "prop") lwFollowProp(s.entry);
  });
  lwUpdateArrows(); lwKonva.worldLayer.batchDraw();
  clearTimeout(lwNudgeTimer);
  lwNudgeTimer = setTimeout(() => {
    list.forEach((s) => {
      const p = s.entry.node.position();
      api(`/api/lw/${lwWorldId}/pos`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: s.entry.data.id, x: p.x, y: p.y }) }).catch(() => {});
    });
  }, 350);
}
function lwKeyHandler(e) {
  if (!lwKonva || $("#lifeworld").hidden) return;
  const tag = (document.activeElement && document.activeElement.tagName) || "";
  const typing = tag === "INPUT" || tag === "TEXTAREA" || (document.activeElement && document.activeElement.isContentEditable);
  if (e.key === "Escape") {
    if (lwCreateFlow && lwCreateFlow.drawing) { lwCancelPathDraw(); e.preventDefault(); }
    else if (lwCreateFlow) { lwCancelCreate(); e.preventDefault(); }
    else if (lwSelList().length) { lwSelClear(); e.preventDefault(); }
    return;
  }
  if (typing || lwCreateFlow) return;    // don't hijack keys while a dialog/field has focus
  if (e.key === " ") {                   // hold Space to pan the floor (a drag selects instead)
    if (!lwKonva.panning) { lwKonva.panning = true; lwKonva.stage.draggable(true); lwSetCursor("grab"); lwLog("pan", "space down → pan ON", null, "info"); }
    e.preventDefault(); return;
  }
  const k = e.key.toLowerCase();
  if ((e.metaKey || e.ctrlKey) && k === "a") { lwSelAll(); e.preventDefault(); return; }
  const toolKey = { v: "select", a: "agent", o: "artifact",
                    "1": "select", "2": "agent", "3": "artifact" }[k];
  if (toolKey && !e.metaKey && !e.ctrlKey) { lwSetTool(toolKey); e.preventDefault(); return; }
  if (k === "f") { lwFitView(); lwSaveView(); e.preventDefault(); return; }
  const list = lwSelList(); if (!list.length) return;
  if (e.key === "Enter") { lwOpenSelected(); e.preventDefault(); return; }
  if (e.key === "Delete" || e.key === "Backspace") {
    const seated = list.filter((s) => s.kind === "agent" && s.entry.seat);
    if (seated.length) seated.forEach((s) => lwUnseatAgent(s.entry.data, s.entry.seat.propId));
    else toast("Select a seated agent to pop out, or remove objects in the Artifacts tab.");
    e.preventDefault(); return;
  }
  const step = e.shiftKey ? LW_NUDGE * 5 : LW_NUDGE;
  const move = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
  if (move) { lwNudgeSelection(move[0], move[1]); e.preventDefault(); }
}
function lwKeyUp(e) {   // releasing Space ends the pan and restores the select cursor
  if (!lwKonva) return;
  if (e.key === " " && lwKonva.panning) {
    lwKonva.panning = false; lwKonva.stage.draggable(false);
    lwSetCursor(lwToolCursor(lwTool));
    lwLog("pan", "space up → pan OFF", null, "info");
  }
}

// Panic reset: whenever the window loses focus / is hidden / a gesture is cancelled, drop
// every transient interaction state. Otherwise Space held through an alt-tab, or a mouseup
// released outside the window, strands panning/drag ON — after which a grab pans the floor
// (or dies) and the cursor never recovers. This is the "after a while it stops working"
// class the fresh-boot tests can never see, because they never blur the window.
function lwForceIdle() {
  if (!lwKonva) return;
  lwLogOn && lwLog("idle", "force-idle (blur/hidden/cancel) — dropping transient state",
    { wasPanning: lwKonva.panning, stageDraggable: lwKonva.stage && lwKonva.stage.draggable(), hadDrag: !!lwKonva.drag, marquee: !!lwKonva.marquee }, "warn");
  lwKonva.panning = false;
  try { lwKonva.stage && lwKonva.stage.draggable(false); } catch (e) { /* stage gone */ }
  if (lwKonva.drag) {
    try { lwKonva.drag.leadNode && lwKonva.drag.leadNode.stopDrag(); } catch (e) { /* */ }
    lwKonva.drag = null;
  }
  if (lwKonva.connecting) { lwKonva.connecting = null; try { lwConnRubber && lwConnRubber.destroy(); } catch (e) { /* */ } lwConnRubber = null; }
  if (lwKonva.marquee && lwKonva.endMarquee) { try { lwKonva.endMarquee(); } catch (e) { /* */ } }
  lwPortalShow(false);
  lwHideGrid();
  lwSetCursor(lwToolCursor(lwTool));
}
function lwOnVisChange() { if (document.hidden) lwForceIdle(); }

// ---- Stage + layers: pan, zoom-to-cursor, drag-only grid -----------------
function lwCanvasV2On() { return !!(me && me.canvas_v2 && window.LWCanvas2); }
function lwDestroyCanvas() {
  if (lwCreateFlow) lwCleanupCreate();
  if (window.LWCanvas2 && window.LWCanvas2._inst) { try { window.LWCanvas2.destroy(); } catch (e) { /* */ } }
  if (lwKonva) {
    if (lwKonva.keyHandler) document.removeEventListener("keydown", lwKonva.keyHandler);
    if (lwKonva.keyUpHandler) document.removeEventListener("keyup", lwKonva.keyUpHandler);
    window.removeEventListener("blur", lwForceIdle);
    window.removeEventListener("pointercancel", lwForceIdle, true);
    document.removeEventListener("visibilitychange", lwOnVisChange);
    if (lwKonva.endMarquee) {   // detach any window listeners a mid-gesture marquee left
      window.removeEventListener("mouseup", lwKonva.endMarquee, true);
      window.removeEventListener("touchend", lwKonva.endMarquee, true);
      window.removeEventListener("pointercancel", lwKonva.endMarquee, true);
    }
    try { lwKonva.ro && lwKonva.ro.disconnect(); lwKonva.stage.destroy(); } catch (e) { /* already gone */ }
    lwKonva = null;
  }
}

function lwMountCanvas(room, agents, props) {
  const host = $("#lwKonvaHost");
  if (!host) return;
  // Canvas v2 (SVG/DOM, native hit-testing) when the CANVAS_V2 flag is served. v1 (Konva) below.
  if (lwCanvasV2On()) {
    lwRoom = room;
    window.LWCanvas2.mount(host, {
      worldId: lwWorldId, roomId: lwRoomId, room, agents, props,
      getTool: () => lwTool, getDir: () => lwThreadDir,
    });
    return;
  }
  if (typeof Konva === "undefined") return;
  // The host can read 0×0 for a frame right after innerHTML (layout not settled yet). A
  // small fallback there would leave the canvas smaller than the visible floor, so clicks
  // and drags in the uncovered region silently miss — the inconsistent "won't select"
  // bug. Fall back to the VIEWPORT so the canvas always covers everything; lwSettleSize
  // trims it to the real host size on the next frame.
  const W = host.clientWidth || window.innerWidth || 1400;
  const H = host.clientHeight || window.innerHeight || 800;
  const stage = new Konva.Stage({ container: host, width: W, height: H });
  const gridLayer = new Konva.Layer({ listening: false });
  const worldLayer = new Konva.Layer();
  stage.add(gridLayer); stage.add(worldLayer);

  lwKonva = { stage, gridLayer, worldLayer, host, roomId: lwWorldId + ":" + lwRoomId,   // world-scoped: room ids repeat across worlds, so a bare id bleeds one scene's view (and off-screen tokens) into another
              agents: new Map(), props: new Map(), glowing: new Set(), snap: null,
              menuWorld: null, sel: new Map(), arrows: null, arrowSrc: null, drag: null,
              marquee: null, panning: false, keyHandler: null, keyUpHandler: null, overPortal: false };

  // Which agents are seated, and in which socket, so they render on the rim.
  const seatedMap = {};
  props.forEach((p) => {
    const slots = Number(p.slots) || 0;
    if (slots > 0 && Array.isArray(p.seated))
      p.seated.forEach((aid, i) => { if (aid != null) seatedMap[String(aid)] = { propId: String(p.id), slot: i }; });
  });

  // Cursor is event-driven: Konva fires mouseenter/mouseleave once per token subtree (moving
  // between a token's own parts never re-fires), so the cursor changes only on a real crossing
  // of the token's edge — it can't flicker the way re-polling the hit graph every mousemove did,
  // and it isn't hostage to a stale panning/drag flag beyond the one guard below.
  // Cursor is driven GEOMETRICALLY by the stage mousemove (below), not per-node enter/leave —
  // Konva's hit graph is unreliable at DPR 2 in some states, which made the cursor flicker.
  const hover = () => {};
  // NOTE: tokens carry NO click/drag Konva handlers any more. Selection and dragging are decided
  // entirely by the geometric pointer core (lwStageMousedown / lwArmSingleDrag), which never trusts
  // the hit graph. Nodes stay listening only so right-click routes to their context menu.

  // Objects first (tables sit under their seated agents). A table carries its ring of seated agents.
  props.forEach((p, i) => {
    const pos = lwNodePos(p, i, "prop");
    const node = lwPropNode(p, pos.x, pos.y);
    const entry = { node, data: p, glow: null };
    lwKonva.props.set(String(p.id), entry);
    worldLayer.add(node);
    node.on("contextmenu", (e) => { e.evt.preventDefault(); e.cancelBubble = true; lwPropMenu(e.evt, p); });
    // double-click is handled geometrically at the stage level (works even when the hit graph misses)
  });

  // A full ring reads as one entity — a soft, tight glow behind the table, and only
  // when every seat is taken. Partly-seated tables stay quiet (no big detached ring).
  props.forEach((p) => {
    const slots = Number(p.slots) || 0;
    const seatedN = Array.isArray(p.seated) ? p.seated.filter((x) => x != null).length : 0;
    const entry = lwKonva.props.get(String(p.id));
    if (entry && slots > 0 && seatedN >= slots) lwAddClusterGlow(entry);
  });

  // People on top: free agents at their pos, seated ones snapped to their slot.
  agents.forEach((a, i) => {
    const seat = seatedMap[String(a.id)];
    let pos;
    if (seat) {
      const pe = lwKonva.props.get(seat.propId);
      const base = pe ? pe.node.position() : { x: 0, y: 0 };
      const off = pe ? lwSlotPos(pe.data, seat.slot) : { x: 0, y: 0 };
      pos = { x: base.x + off.x, y: base.y + off.y };
    } else {
      pos = lwNodePos(a, i, "agent");
    }
    const node = lwAgentNode(a, pos.x, pos.y, { manager: lwIsManager(a) });
    const entry = { node, data: a, seat };
    lwKonva.agents.set(String(a.id), entry);
    worldLayer.add(node);
    node.on("contextmenu", (e) => { e.evt.preventDefault(); e.cancelBubble = true; lwAgentMenu(e.evt, a); });
    // selection, drag and double-click are all handled geometrically at the stage level
    if (seat) lwSetSteadyGlow(node, true);
  });

  lwRenderThreads(room.threads || []);          // draw the agent graph beneath the tokens

  // Restore the cached viewport, or frame everything once. `framed` stays false if the
  // host wasn't measured yet, so lwSettleSize/the ResizeObserver re-frames precisely once
  // a real size arrives (the provisional fit here keeps tokens visible meanwhile).
  const view = lwViewCache[lwKonva.roomId];
  if (view) { stage.position({ x: view.x, y: view.y }); stage.scale({ x: view.scale, y: view.scale }); lwKonva.framed = true; }
  else { lwFitView(); lwKonva.framed = host.clientWidth > 0 && host.clientHeight > 0; }

  stage.draggable(false);            // pan is space-drag now; a plain drag on empty marquee-selects
  lwSetCursor(lwToolCursor(lwTool));

  stage.on("dragstart", () => { lwSetCursor("grabbing"); });      // only fires while space-panning
  stage.on("dragmove", () => { lwShowGrid(); });
  stage.on("dragend", () => { lwHideGrid(); lwSaveView(); lwSetCursor(lwKonva.panning ? "grab" : lwToolCursor(lwTool)); });

  stage.on("wheel", (e) => {
    e.evt.preventDefault();
    const oldScale = stage.scaleX();
    const pointer = stage.getPointerPosition(); if (!pointer) return;
    const mp = { x: (pointer.x - stage.x()) / oldScale, y: (pointer.y - stage.y()) / oldScale };
    const dir = e.evt.deltaY > 0 ? -1 : 1;
    let ns = oldScale * (dir > 0 ? 1.08 : 1 / 1.08);
    ns = Math.max(0.3, Math.min(3, ns));
    stage.scale({ x: ns, y: ns });
    stage.position({ x: pointer.x - mp.x * ns, y: pointer.y - mp.y * ns });
    lwShowGrid(); lwSaveView();
  });

  // A plain drag on empty floor rubber-bands a marquee that selects everything it encloses
  // (shift-drag ADDS to the selection). Pan is space-drag instead. The gesture ENDS on a
  // window listener, not the stage, so releasing over the dock/popover/off-canvas can never
  // strand the floor or leave a ghost rect.
  const endMarquee = () => {
    const m = lwKonva && lwKonva.marquee; if (!m) return;
    lwKonva.marquee = null;
    window.removeEventListener("mouseup", endMarquee, true);
    window.removeEventListener("touchend", endMarquee, true);
    window.removeEventListener("pointercancel", endMarquee, true);
    const bx = m.rect.x(), by = m.rect.y(), bw = m.rect.width(), bh = m.rect.height();
    m.rect.destroy();
    if (bw > 4 || bh > 4) {
      if (!m.add) lwSelClear();
      const inside = (n) => { const p = n.position(); return p.x >= bx && p.x <= bx + bw && p.y >= by && p.y <= by + bh; };
      lwKonva.props.forEach((en) => { if (inside(en.node)) lwSelAdd("prop", en); });
      lwKonva.agents.forEach((en) => { if (inside(en.node)) lwSelAdd("agent", en); });
    }
    lwLogOn && lwLog("marquee", "end", { box: { w: Math.round(bw), h: Math.round(bh) }, selected: lwKonva.sel.size }, "debug");
    worldLayer.batchDraw();
  };
  lwKonva.endMarquee = endMarquee;         // so teardown can detach any pending listeners
  stage.on("mousedown touchstart", (e) => {
    if (lwLogOn) {
      const w = lwPointerWorld();
      const data = { tool: lwTool, panning: lwKonva.panning, stageDraggable: stage.draggable(), sel: lwKonva.sel.size, at: lwRoundPt(w) };
      if (e.target === stage && w) {                 // empty-floor press — is a token actually there but unhit?
        data.tokens = `${lwKonva.agents.size}a/${lwKonva.props.size}p`;
        const near = lwNearestToken(w);
        data.nearest = near ? `${near.type}#${near.id}@(${Math.round(near.at.x)},${Math.round(near.at.y)})` : "none";
        if (near) { data.dist = Math.round(near.dist); data.insideBox = near.insideBox; }
        const scr = lwKonva.stage.getPointerPosition();
        if (scr) data.screen = { x: Math.round(scr.x), y: Math.round(scr.y) };
        data.view = { x: Math.round(lwKonva.stage.x()), y: Math.round(lwKonva.stage.y()), scale: +lwKonva.stage.scaleX().toFixed(3) };
        if (near && near.insideBox && scr) {          // PARADOX: a token is under the click but the hit graph said empty
          const idAt = (sx, sy) => { const sh = lwKonva.stage.getIntersection({ x: sx, y: sy }); const t = sh && lwTokenAncestor(sh); return t ? String(t.getAttr("lwId")) : (sh ? ((sh.name && sh.name()) || "?") : "-"); };
          data.vprobe = [-24, -12, 0, 12, 24].map((d) => `${d}:${idAt(scr.x, scr.y + d)}`).join(" ");   // where does the hit region sit, vertically?
          data.hprobe = [-24, -12, 0, 12, 24].map((d) => `${d}:${idAt(scr.x + d, scr.y)}`).join(" ");     // ...and horizontally?
          try {
            const hc = lwKonva.worldLayer.hitCanvas, sc = lwKonva.worldLayer.getCanvas();
            data.ratios = { hit: hc && hc.pixelRatio, scene: sc && sc.pixelRatio };
            if (hc && hc._canvas) data.hitPx = { w: hc._canvas.width, h: hc._canvas.height };
          } catch (er) { /* internals moved */ }
          lwKonva.worldLayer.drawHit();               // force-refresh the hit graph, then retry the exact point
          data.afterRedraw = idAt(scr.x, scr.y);
        }
      }
      lwLog("pointer", "down on " + lwHitDesc(e.target), data, "info");
    }
    lwStageMousedown(e);      // ONE authoritative, fully geometric decision (see the interaction core)
  });
  stage.on("mousemove touchmove", () => {
    if (lwKonva.marquee) {
      const w = lwPointerWorld(); if (!w) return;
      const m = lwKonva.marquee;
      m.rect.position({ x: Math.min(w.x, m.x0), y: Math.min(w.y, m.y0) });
      m.rect.size({ width: Math.abs(w.x - m.x0), height: Math.abs(w.y - m.y0) });
      worldLayer.batchDraw();
      return;
    }
    if (lwKonva.panning || lwKonva.drag || lwKonva.connecting) return;
    // Cursor driven GEOMETRICALLY (node positions), not via the flaky hit graph → no flicker.
    // Exactly the same picks the mousedown makes, so the cursor always predicts what a press will do.
    const w = lwPointerWorld(); if (!w) { lwSetCursor(lwToolCursor(lwTool)); return; }
    if (lwPickHandle(w)) { lwSetCursor("crosshair"); return; }   // over a connection handle → draw a wire
    if (lwPickToken(w)) { lwSetCursor("move"); return; }          // over a token body → move it
    lwSetCursor(lwGeomHitEdge(w) ? "pointer" : lwToolCursor(lwTool));
  });
  // Pointer left the whole canvas (onto the top bar, a dock button, another window): settle the
  // cursor to the tool default so it never gets stranded on 'move' or 'grab'.
  stage.on("mouseleave", () => { if (!lwKonva.panning && !lwKonva.drag && !lwKonva.marquee) lwSetCursor(lwToolCursor(lwTool)); });

  // The click event NO LONGER touches selection — mousedown (lwStageMousedown) is the single
  // authority for select/deselect, which kills the old race where a trailing click cleared a
  // freshly-recovered selection. Click only drives path-drawing and placement-tool drops.
  stage.on("click tap", (e) => {
    if (lwCreateFlow && lwCreateFlow.drawing) { lwPathAddPoint(); return; }
    if (lwCreateFlow) { lwCancelCreate(); return; }
    if (lwTool !== "select" && e.target === stage) { const w = lwPointerWorld(); if (w) lwStartCreate(lwTool, w); }
  });
  stage.on("dblclick dbltap", () => {
    if (lwCreateFlow && lwCreateFlow.drawing) { lwFinishPathDraw(); return; }
    if (lwTool !== "select") return;
    // Double-click GEOMETRICALLY (same pick as mousedown) → select its graph, or open its detail
    // if it's ungrouped. Consistent regardless of what the hit graph thinks.
    const w = lwPointerWorld(); const tk = w && lwPickToken(w);
    if (tk) {
      if (!lwGraphSelect(tk.id)) {
        if (tk.type === "agent") openAgentPage(tk.id);
        else lwOpenArtifactPeek(tk.id);
      }
    }
  });
  stage.on("contextmenu", (e) => {
    e.evt.preventDefault();
    if (e.target !== stage) return;
    lwKonva.menuWorld = lwPointerWorld() || { x: 0, y: 0 };
    lwFloorMenu(e.evt, lwKonva.menuWorld);
  });

  lwKonva.ro = new ResizeObserver(() => {
    if (!lwKonva || lwKonva.stage !== stage) return;
    const w = host.clientWidth, h = host.clientHeight;
    if (w > 0 && h > 0) {                         // never shrink to a fallback when host reads 0
      const sized = stage.width() !== w || stage.height() !== h;
      if (sized) { stage.width(w); stage.height(h); }   // this CLEARS both the scene AND hit canvases
      let fitted = false;
      if (!lwKonva.framed) { lwFitView(); lwKonva.framed = true; fitted = true; }
      // A resize (or a fit) leaves the hit canvas cleared/mis-transformed relative to the scene
      // until a full draw — the source of "the token is right there but the click misses it".
      if (sized || fitted) lwKonva.worldLayer.batchDraw();
    }
  });
  lwKonva.ro.observe(host);
  lwSettleSize(stage, host);          // catch the 0×0-at-mount race by polling a few frames

  lwKonva.keyHandler = lwKeyHandler;
  lwKonva.keyUpHandler = lwKeyUp;
  document.addEventListener("keydown", lwKeyHandler);
  document.addEventListener("keyup", lwKeyUp);
  window.addEventListener("blur", lwForceIdle);                 // alt-tab / another app while dragging or panning
  window.addEventListener("pointercancel", lwForceIdle, true);  // a gesture the OS took away
  document.addEventListener("visibilitychange", lwOnVisChange); // switched tabs / Mission Control

  gridLayer.hide();
  worldLayer.draw();
  lwLogOn && lwLog("life", "canvas mounted", { host: { w: host.clientWidth, h: host.clientHeight }, stage: { w: stage.width(), h: stage.height() },
    scale: +stage.scaleX().toFixed(3), dpr: window.devicePixelRatio, agents: lwKonva.agents.size, props: lwKonva.props.size }, "info");
}

// Poll a few frames until the host has a real size, then match the stage to it and do the
// first true fit — so the canvas always covers the whole floor and no token can land in a
// dead zone the pointer never reaches.
function lwSettleSize(stage, host) {
  let tries = 0;
  const step = () => {
    if (!lwKonva || lwKonva.stage !== stage) return;
    const w = host.clientWidth, h = host.clientHeight;
    if (w > 0 && h > 0) {
      if (stage.width() !== w || stage.height() !== h) { stage.width(w); stage.height(h); }
      if (!lwKonva.framed) { lwFitView(); lwKonva.framed = true; }
      lwKonva.worldLayer.draw();          // SYNC (not batchDraw): close the window where the hit graph
      return;                             // is stale between the resize and the next animation frame
    }
    if (tries++ < 60) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function lwPointerWorld() {
  const s = lwKonva && lwKonva.stage; if (!s) return null;
  const p = s.getPointerPosition(); if (!p) return null;
  return s.getAbsoluteTransform().copy().invert().point(p);
}
function lwSaveView() {
  if (!lwKonva) return;
  lwViewCache[lwKonva.roomId] = { x: lwKonva.stage.x(), y: lwKonva.stage.y(), scale: lwKonva.stage.scaleX() };
}
function lwFitView() {
  if (!lwKonva) return;
  const pts = [];
  lwKonva.agents.forEach((e) => pts.push(e.node.position()));
  lwKonva.props.forEach((e) => pts.push(e.node.position()));
  const stage = lwKonva.stage;
  if (!pts.length) { stage.position({ x: 0, y: 0 }); stage.scale({ x: 1, y: 1 }); lwSaveView(); lwKonva.worldLayer.batchDraw(); return; }
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  pts.forEach((p) => { minX = Math.min(minX, p.x); minY = Math.min(minY, p.y); maxX = Math.max(maxX, p.x); maxY = Math.max(maxY, p.y); });
  const pad = 130; minX -= pad; minY -= pad; maxX += pad; maxY += pad;
  const cw = Math.max(1, maxX - minX), ch = Math.max(1, maxY - minY);
  const W = stage.width(), H = stage.height();
  let scale = Math.min(W / cw, H / ch, 1.15); scale = Math.max(0.4, scale);
  stage.scale({ x: scale, y: scale });
  stage.position({ x: (W - cw * scale) / 2 - minX * scale, y: (H - ch * scale) / 2 - minY * scale });
  lwSaveView();
  lwKonva.worldLayer.batchDraw();   // a fit moves the transform — redraw so the HIT graph tracks the scene
}

// The dotted grid is a Miro-style affordance: hidden until a drag or a zoom, then
// drawn in world coords across just the visible region and faded back out.
function lwShowGrid() {
  if (!lwKonva) return;
  lwDrawGrid();
  lwKonva.gridLayer.show(); lwKonva.gridLayer.batchDraw();
  clearTimeout(lwGridTimer);
  lwGridTimer = setTimeout(lwHideGrid, 900);
}
function lwHideGrid() {
  if (!lwKonva) return;
  lwKonva.gridLayer.hide(); lwKonva.gridLayer.batchDraw();
}
function lwDrawGrid() {
  if (!lwKonva) return;
  const { stage, gridLayer } = lwKonva;
  gridLayer.destroyChildren();
  const s = stage.scaleX() || 1;
  const step = 34;
  const left = -stage.x() / s, top = -stage.y() / s;
  const right = left + stage.width() / s, bottom = top + stage.height() / s;
  const x0 = Math.floor(left / step) * step, y0 = Math.floor(top / step) * step;
  let count = 0;
  for (let x = x0; x <= right && count < 5000; x += step)
    for (let y = y0; y <= bottom && count < 5000; y += step) {
      gridLayer.add(new Konva.Circle({ x, y, radius: 1.3, fill: "rgba(120,128,124,.5)", listening: false }));
      count++;
    }
}

// ---- token builders (Konva groups; hitbox = the body shape) --------------
function lwHue(name) {
  let h = 0; for (const c of String(name || "?")) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h % 360;
}
function lwIsManager(a) {
  return !!(a && (a.manager || a.role === "manager" || a.kind === "manager"
    || String(a.figure || "").startsWith("avm:") || a.figure === "👔"));
}
function lwNodePos(t, i, kind) {
  const p = t.pos;
  if (Array.isArray(p) && Number.isFinite(+p[0]) && Number.isFinite(+p[1]) && !(+p[0] === 0 && +p[1] === 0))
    return { x: +p[0], y: +p[1] };
  const cols = 4, gx = 165, gy = 155;
  return { x: (kind === "prop" ? 150 : 120) + (i % cols) * gx, y: 150 + Math.floor(i / cols) * gy };
}
function lwLabelNode(text, y) {
  return new Konva.Text({ text: String(text), fontSize: 12, fontFamily: "sans-serif", fill: "#3a3f3d",
    width: 170, align: "center", offsetX: 85, y, listening: false });
}
// A transparent grab rectangle covering a token's WHOLE visible footprint (disc/body +
// its name label), so a click anywhere on the token grabs it instead of falling through
// to the empty stage (a canvas pan). A near-zero fill alpha keeps it invisible while
// still registering on Konva's hit graph.
function lwAddHit(g, x, y, w, h) {
  const r = new Konva.Rect({ x, y, width: w, height: h, cornerRadius: 10, fill: "rgba(0,0,0,0.002)", name: "hit", listening: true });
  g.add(r); r.moveToBottom();
}
function lwMoodBarNodes(mood, y) {
  mood = mood || {};
  const conf = lwPct(mood.confidence) / 100, stress = lwPct(mood.stress) / 100;
  const w = 26, h = 5, gap = 4, x0 = -(w * 2 + gap) / 2;
  const mk = (bx, val, color) => [
    new Konva.Rect({ x: bx, y, width: w, height: h, cornerRadius: 3, fill: "rgba(0,0,0,.13)", listening: false }),
    new Konva.Rect({ x: bx, y, width: Math.max(1, w * val), height: h, cornerRadius: 3, fill: color, listening: false }),
  ];
  return [...mk(x0, conf, "#3F7A3F"), ...mk(x0 + w + gap, stress, "#8A6A1F")];
}

// An agent = a circular figurine (its emoji figure, else initial), a provider-ish
// name-hued rim, its name and two tiny mood bars. The disc named "body" is the
// hitbox and the thing that glows.
function lwAgentNode(a, x, y, opts) {
  opts = opts || {};
  const g = new Konva.Group({ x, y, draggable: false, name: "token" });   // drag is geometric (lwArmSingleDrag), not Konva
  g.setAttr("lwType", "agent"); g.setAttr("lwId", String(a.id));
  const R = 30, seed = lwAvatarSeed(a);
  // The grab area HUGS the visible person: the disc itself (the rim/body circles below are
  // the hit) plus a band exactly under the name+mood, sized to the name. A big rectangle here
  // used to overhang far past the disc and, in a lived-in scene where agents sit close (a table
  // ring, or hand-clustered), its invisible corners blanketed the neighbour's grab area — so you
  // grabbed the wrong token or nothing. This band overlaps the disc bottom (no gap => no cursor
  // flicker) and widens for long names so their label never has ungrabbable wings.
  const nameW = Math.max(64, Math.min(150, (a.name || "someone").length * 7.2));
  lwAddHit(g, -nameW / 2, R - 4, nameW, 40);   // grab by the disc OR the name band under it
  const rim = opts.manager ? "#2C6A63" : lwAvatarColor(seed);
  g.add(new Konva.Circle({ radius: R + 3, fill: rim, listening: true }));   // colored edge + hitbox
  // the "body" disc is the glow/selection target and the fallback fill until the
  // avatar image decodes; the generated face (a circular SVG) rides on top.
  g.add(new Konva.Circle({ radius: R, name: "body", fill: lwAvatarBg(seed),
    stroke: "rgba(255,255,255,.9)", strokeWidth: 2 }));
  const img = new Image();
  const face = new Konva.Image({ width: R * 2, height: R * 2, offsetX: R, offsetY: R, listening: false });
  img.onload = () => { face.image(img); const l = face.getLayer(); if (l) l.batchDraw(); };
  img.src = lwSvgUri(lwAvatarSvg(seed, R * 2));
  g.add(face);   // image attaches on decode, so Konva never draws a half-loaded bitmap
  g.add(new Konva.Text({ text: a.name || "someone", fontSize: 12.5, fontFamily: "sans-serif", fill: "#1B2021",
    width: 140, align: "center", offsetX: 70, y: R + 8, listening: false }));
  lwMoodBarNodes(a.mood, R + 26).forEach((b) => g.add(b));
  if (opts.manager)
    g.add(new Konva.Text({ text: "★", fontSize: 17, fill: "#E0A93B", width: 24, align: "center", offsetX: 12, y: -R - 18, listening: false }));
  return g;
}

function lwPropNode(p, x, y) {
  const slots = Number(p.slots) || 0;
  const kind = String(p.kind || "").toLowerCase();
  if (slots > 0) return lwTableNode(p, x, y);
  if (kind.includes("deck") || kind.includes("card")) return lwDeckNode(p, x, y);
  return lwTileNode(p, x, y);
}

// A deck = a small stack of offset cards; the top card is the "body" hitbox.
function lwDeckNode(p, x, y) {
  const g = new Konva.Group({ x, y, draggable: false, name: "token" });   // drag is geometric (lwArmSingleDrag), not Konva
  g.setAttr("lwType", "prop"); g.setAttr("lwId", String(p.id));
  const cw = 40, ch = 56;
  lwAddHit(g, -cw / 2 - 8, -ch / 2 - 12, cw + 16, ch + 46);   // grab the stack + its label
  for (let i = 2; i >= 0; i--)
    g.add(new Konva.Rect({ x: -cw / 2 + i * 4, y: -ch / 2 - i * 4, width: cw, height: ch, cornerRadius: 6,
      fill: i === 0 ? "#fbfaf6" : "#efece3", stroke: "#cfc9bd", strokeWidth: 1.5, name: i === 0 ? "body" : "",
      shadowColor: "#000", shadowBlur: i === 0 ? 6 : 0, shadowOpacity: 0.12 }));
  g.add(new Konva.Rect({ x: -cw / 2 + 5, y: -ch / 2 - 3, width: cw - 10, height: ch - 10, cornerRadius: 4,
    stroke: "rgba(46,110,91,.55)", dash: [3, 3], strokeWidth: 1, listening: false }));
  g.add(lwLabelNode(p.name || "deck", ch / 2 + 6));
  return g;
}

// A generic prop = a rounded, name-hued tile with its figure/initial.
function lwTileNode(p, x, y) {
  const g = new Konva.Group({ x, y, draggable: false, name: "token" });   // drag is geometric (lwArmSingleDrag), not Konva
  g.setAttr("lwType", "prop"); g.setAttr("lwId", String(p.id));
  const w = 58, h = 58, hue = lwHue(p.name);
  lwAddHit(g, -w / 2 - 6, -h / 2 - 6, w + 12, h + 40);   // grab the tile + its label
  g.add(new Konva.Rect({ x: -w / 2, y: -h / 2, width: w, height: h, cornerRadius: 12, name: "body",
    fill: `hsl(${hue} 24% 92%)`, stroke: `hsl(${hue} 34% 62%)`, strokeWidth: 2,
    shadowColor: "#000", shadowBlur: 6, shadowOpacity: 0.1 }));
  const fig = String(p.figure || "");
  const key = fig.startsWith("ic:") ? fig.slice(3) : "mono";
  const ink = `hsl(${hue} 42% 38%)`;
  if (key === "mono") {
    g.add(new Konva.Text({ text: ((p.name || "?")[0] || "?").toUpperCase(), fontSize: 26, fontStyle: "bold",
      fontFamily: "Georgia, serif", fill: ink, width: w, height: h, align: "center", verticalAlign: "middle",
      offsetX: w / 2, offsetY: h / 2, listening: false }));
  } else {
    const gimg = new Image(); const D = 34;
    const icon = new Konva.Image({ image: gimg, width: D, height: D, offsetX: D / 2, offsetY: D / 2, listening: false });
    gimg.onload = () => { const l = icon.getLayer(); if (l) l.batchDraw(); };
    gimg.src = lwSvgUri(lwObjGlyphSvg(key, D, ink));
    g.add(icon);
  }
  g.add(lwLabelNode(p.name || "object", h / 2 + 6));
  if (p.sealed) {   // a small vector padlock, not an emoji
    g.add(new Konva.Rect({ x: w / 2 - 16, y: -h / 2 + 3, width: 10, height: 7, cornerRadius: 2, fill: "#8a7f6c", listening: false }));
    g.add(new Konva.Arc({ x: w / 2 - 11, y: -h / 2 + 3, innerRadius: 2.6, outerRadius: 4.2, angle: 180, rotation: 180, fill: "#8a7f6c", listening: false }));
  }
  return g;
}

// A collating table (slots>0) = a ringed felt disc with N seat sockets around the
// rim; filled sockets are solid rings, empty ones dashed. The disc is the "body".
function lwTableNode(p, x, y) {
  const g = new Konva.Group({ x, y, draggable: false, name: "token" });   // drag is geometric (lwArmSingleDrag), not Konva
  g.setAttr("lwType", "prop"); g.setAttr("lwId", String(p.id));
  const slots = Number(p.slots) || 1, hue = lwHue(p.name), shape = String(p.shape || "circle");
  const grad = (rad, cx = 0, cy = 0) => ({
    fillRadialGradientStartPoint: { x: cx, y: cy }, fillRadialGradientStartRadius: 4,
    fillRadialGradientEndPoint: { x: cx, y: cy }, fillRadialGradientEndRadius: rad,
    fillRadialGradientColorStops: [0, `hsl(${hue} 34% 62%)`, 1, `hsl(${hue} 40% 46%)`],
    stroke: `hsl(${hue} 40% 40%)`, strokeWidth: 3, shadowColor: "#000", shadowBlur: 10, shadowOpacity: 0.16 });
  let labelY;
  if (shape === "rect") {
    // a Rect's fill space starts at its own top-left, so the gradient origin must be
    // the rect's local centre — else the felt lights only the top-left corner.
    g.add(new Konva.Rect({ x: -LW_RECT_W / 2, y: -LW_RECT_H / 2, width: LW_RECT_W, height: LW_RECT_H, cornerRadius: 18, name: "body", ...grad(Math.hypot(LW_RECT_W, LW_RECT_H) / 2, LW_RECT_W / 2, LW_RECT_H / 2) }));
    g.add(new Konva.Rect({ x: -LW_RECT_W / 2 + 10, y: -LW_RECT_H / 2 + 10, width: LW_RECT_W - 20, height: LW_RECT_H - 20, cornerRadius: 12, stroke: "rgba(255,255,255,.4)", strokeWidth: 1.5, listening: false }));
    labelY = LW_RECT_H / 2 + 16;
  } else if (shape === "path" && Array.isArray(p.path) && p.path.length >= 3) {
    g.add(new Konva.Line({ points: p.path.flatMap((q) => [+q[0], +q[1]]), closed: true, name: "body", ...grad(lwPropRadius(p)) }));
    labelY = lwPathBottom(p.path) + 18;
  } else {
    const R = LW_TABLE_R;
    g.add(new Konva.Circle({ radius: R, name: "body", ...grad(R) }));
    g.add(new Konva.Circle({ radius: R - 12, stroke: "rgba(255,255,255,.4)", strokeWidth: 1.5, listening: false }));
    labelY = R + 16;
  }
  lwAddHit(g, -70, labelY - 6, 140, 26);   // the big body already grabs; also grab by the label
  const seated = Array.isArray(p.seated) ? p.seated : [], pos = lwSlotPositions(p);
  for (let i = 0; i < slots; i++) {
    const off = pos[i] || { x: 0, y: 0 }, filled = seated[i] != null;
    g.add(new Konva.Circle({ x: off.x, y: off.y, radius: 15,
      fill: filled ? "rgba(255,255,255,.16)" : "rgba(255,255,255,.05)",
      stroke: filled ? "rgba(255,255,255,.75)" : "rgba(255,255,255,.5)", strokeWidth: 2,
      dash: filled ? undefined : [4, 4], listening: false }));
  }
  const nSeated = seated.filter((s) => s != null).length;
  g.add(lwLabelNode(`${p.name || "table"} · ${nSeated}/${slots} seated`, labelY));
  return g;
}

function lwSocketOffset(i, n) {
  const theta = -Math.PI / 2 + (i / Math.max(1, n)) * Math.PI * 2;
  return { x: Math.cos(theta) * LW_SOCKET_R, y: Math.sin(theta) * LW_SOCKET_R };
}
// Seat-socket positions for a collating artifact, in its local frame — distributed
// along whatever outline it wears: a ring, a rectangle's edge, or a hand-drawn path.
function lwSlotPositions(p) {
  const n = Math.max(1, Number(p.slots) || 1), shape = String(p.shape || "circle");
  if (shape === "rect") {
    const out = [];
    for (let i = 0; i < n; i++) out.push(lwRectPerimeter(i / n, LW_RECT_W / 2 + LW_SEAT_OUT, LW_RECT_H / 2 + LW_SEAT_OUT));
    return out;
  }
  if (shape === "path" && Array.isArray(p.path) && p.path.length >= 3) return lwPathSlots(p.path, n);
  const out = [];
  for (let i = 0; i < n; i++) out.push(lwSocketOffset(i, n));
  return out;
}
function lwSlotPos(p, i) { const a = lwSlotPositions(p); return a[i] || a[0] || { x: 0, y: 0 }; }
function lwRectPerimeter(t, hw, hh) {
  const w = 2 * hw, h = 2 * hh, per = 2 * (w + h);
  let d = (t * per + w / 2) % per;             // start at top-centre, walk clockwise
  if (d < w) return { x: -hw + d, y: -hh };
  d -= w; if (d < h) return { x: hw, y: -hh + d };
  d -= h; if (d < w) return { x: hw - d, y: hh };
  d -= w; return { x: -hw, y: hh - d };
}
function lwPathCentroid(path) {
  const n = path.length || 1;
  return { x: path.reduce((s, q) => s + (+q[0]), 0) / n, y: path.reduce((s, q) => s + (+q[1]), 0) / n };
}
function lwPathSlots(path, n) {
  const pts = path.map((q) => ({ x: +q[0], y: +q[1] }));
  const segs = []; let total = 0;
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i], b = pts[(i + 1) % pts.length], len = Math.hypot(b.x - a.x, b.y - a.y);
    segs.push({ a, b, len, acc: total }); total += len;
  }
  if (total < 1) return pts.slice(0, n);
  const c = lwPathCentroid(path), out = [];
  for (let i = 0; i < n; i++) {
    const d = (i / n) * total;
    const seg = segs.find((s) => d >= s.acc && d < s.acc + s.len) || segs[segs.length - 1];
    const f = seg.len ? (d - seg.acc) / seg.len : 0;
    const x = seg.a.x + (seg.b.x - seg.a.x) * f, y = seg.a.y + (seg.b.y - seg.a.y) * f;
    const dx = x - c.x, dy = y - c.y, m = Math.hypot(dx, dy) || 1;
    out.push({ x: x + (dx / m) * LW_SEAT_OUT, y: y + (dy / m) * LW_SEAT_OUT });
  }
  return out;
}
function lwGrowPath(path, out) {
  const c = lwPathCentroid(path);
  return path.map((q) => { const dx = +q[0] - c.x, dy = +q[1] - c.y, m = Math.hypot(dx, dy) || 1; return { x: +q[0] + (dx / m) * out, y: +q[1] + (dy / m) * out }; });
}
function lwPathBottom(path) { return Array.isArray(path) && path.length ? Math.max(...path.map((q) => +q[1])) : LW_TABLE_R; }
function lwPropRadius(p) {
  const slots = Number(p.slots) || 0;
  if (slots <= 0) return 30;
  const shape = String(p.shape || "circle");
  if (shape === "rect") return Math.max(LW_RECT_W, LW_RECT_H) / 2;
  if (shape === "path" && Array.isArray(p.path) && p.path.length) return Math.max(LW_TABLE_R, ...p.path.map((q) => Math.hypot(+q[0], +q[1])));
  return LW_TABLE_R;
}

// ---- glow: vicinity (transient, while dragging) + cluster/steady (at rest) --
function lwSetGlow(node, on, color, blur, opacity) {
  const body = node && node.findOne(".body"); if (!body) return;
  if (on) { body.shadowColor(color); body.shadowBlur(blur); body.shadowOpacity(opacity); body.shadowEnabled(true); }
  else body.shadowEnabled(false);
}
function lwVicinityGlow(node, color) {
  lwSetGlow(node, true, color || "hsl(150 72% 45%)", 22, 0.9);
  lwKonva.glowing.add(node);
}
function lwSetSteadyGlow(node, on) { lwSetGlow(node, on, "hsl(150 60% 45%)", 14, 0.55); }

// ---- threads: the agent/object graph, drawn + wired on the canvas ----------
// A node in a graph. Its 4 handles let you drag a connection to another node (MS-Paint style).
function lwNodeById(id) { return lwKonva.agents.get(String(id)) || lwKonva.props.get(String(id)); }
function lwNodeKind(id) { return lwKonva.agents.has(String(id)) ? "agent" : "prop"; }

// ============================================================================
//  GEOMETRIC INTERACTION CORE  (rebuilt from scratch)
//  One authoritative pointer system. Picking (what's under the pointer) is decided
//  purely by geometry — the Konva hit graph is NEVER consulted, because it is
//  offset/stale at devicePixelRatio 2 with a panned view and negative world-y, which
//  is what made selection/clicks/handles inconsistent. No node click/drag handlers,
//  no getIntersection, no setTimeout-based click suppression. See lwStageMousedown.
// ============================================================================
let lwPtrSeq = 0;
function lwPtrLog(seq, msg, extra) {
  if (!lwLogOn) return;
  lwLog("pick", (seq ? `#${seq} ` : "") + msg, extra || null, "info");
}
// Convert raw client coords → world coords. Reliable during window-level (capture) drag
// listeners, where stage.getPointerPosition() is not refreshed.
function lwClientToWorld(clientX, clientY) {
  if (!lwKonva || clientX == null) return null;
  const rect = lwKonva.stage.container().getBoundingClientRect();
  const t = lwKonva.stage.getAbsoluteTransform().copy().invert();
  return t.point({ x: clientX - rect.left, y: clientY - rect.top });
}
const lwEvX = (ev) => ev && (ev.clientX != null ? ev.clientX : (ev.touches && ev.touches[0] ? ev.touches[0].clientX : null));
const lwEvY = (ev) => ev && (ev.clientY != null ? ev.clientY : (ev.touches && ev.touches[0] ? ev.touches[0].clientY : null));

// The pointer's grab radius for a token — its drawn body plus a comfortable slop.
function lwTokenRadius(entry) {
  return lwKonva.props.has(String(entry.data.id)) ? lwPropRadius(entry.data) + 12 : 40;
}
// Is the world point on a token? Returns the nearest token whose body (within slop) contains it.
function lwPickToken(w) {
  const near = lwNearestToken(w); if (!near) return null;
  const entry = lwNodeById(near.id); if (!entry) return null;
  const r = lwTokenRadius(entry);
  return near.dist <= r ? Object.assign({}, near, { entry, radius: r }) : null;
}
// The 4 connection handles of the CURRENT single selection, in world coords.
function lwHandleReach(entry) { return lwTokenRadius(entry) + 8; }   // always OUTSIDE the body guard, so big tables can be wired too
function lwHandlePoints() {
  const list = lwSelList();
  if (list.length !== 1 || lwTool !== "select") return null;
  const entry = list[0].entry, b = entry.node.position(), reach = lwHandleReach(entry);
  return { entry, pts: [[0, -reach], [reach, 0], [0, reach], [-reach, 0]].map(([dx, dy]) => ({ x: b.x + dx, y: b.y + dy })) };
}
// Is the world point on a connection handle? Only OUTSIDE the body (so a body press still drags).
function lwPickHandle(w) {
  const h = lwHandlePoints(); if (!h) return null;
  const c = h.entry.node.position();
  if (Math.hypot(w.x - c.x, w.y - c.y) < lwTokenRadius(h.entry) - 4) return null;   // inside the body → it's a drag
  let best = null;
  h.pts.forEach((p) => { const d = Math.hypot(p.x - w.x, p.y - w.y); if (d < 16 && (!best || d < best.d)) best = { d, at: p }; });
  return best ? { entry: h.entry, at: best.at, d: best.d } : null;
}

// ---- geometric drag (single token; agents seat into slots, props carry their ring) ----
function lwAgentDragStep(a, node) {                    // snap-scan: extracted from the old node dragmove
  lwShowGrid(); lwClearGlows();
  const gp = node.position();
  let near = false, best = null, bestD = Infinity;
  lwKonva.props.forEach((entry) => {
    const p = entry.data, base = entry.node.position(), slots = Number(p.slots) || 0;
    if (slots > 0) {
      const seated = Array.isArray(p.seated) ? p.seated : [], sp = lwSlotPositions(p);
      for (let i = 0; i < slots; i++) {
        if (seated[i] != null && String(seated[i]) !== String(a.id)) continue;
        const off = sp[i] || { x: 0, y: 0 }, sx = base.x + off.x, sy = base.y + off.y;
        const d = Math.hypot(gp.x - sx, gp.y - sy);
        if (d < LW_SNAP_DIST && d < bestD) { bestD = d; best = { entry, slot: i, x: sx, y: sy }; }
      }
    }
    if (Math.hypot(gp.x - base.x, gp.y - base.y) < lwPropRadius(p) + 36) { lwVicinityGlow(entry.node); near = true; }
  });
  if (best) { lwVicinityGlow(best.entry.node, "hsl(150 78% 45%)"); near = true; }
  if (near) lwVicinityGlow(node);
  lwKonva.snap = best ? { propId: String(best.entry.data.id), slot: best.slot, x: best.x, y: best.y } : null;
  lwUpdateArrows(); lwPortalOver(); lwKonva.worldLayer.batchDraw();
}
function lwPropDragStep(p, entry, node) {
  lwShowGrid(); lwFollowProp(entry); lwUpdateArrows(); lwPortalOver(); lwKonva.worldLayer.batchDraw();
}
async function lwPropDrop(p, entry, node) {
  lwFollowProp(entry);
  const gp = node.position();
  lwLog("drag", `drop prop#${p.id}`, { at: lwRoundPt(gp) }, "info");
  try {
    await api(`/api/lw/${lwWorldId}/pos`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: p.id, x: gp.x, y: gp.y }) });
    lwSaveView();
  } catch (e) { toast(`Could not move it: ${e.message}`); await lwReloadRoom(); }
}
// Arm a single-token drag: nothing moves until the pointer travels >4px, so a plain press
// stays a clean select. Fully geometric (window listeners + client→world), no Konva drag.
function lwArmSingleDrag(kind, entry, downEvt) {
  const node = entry.node, sx = lwEvX(downEvt), sy = lwEvY(downEvt);
  if (sx == null) return;
  const w0 = lwClientToWorld(sx, sy), x0 = node.x(), y0 = node.y();
  let dragging = false, done = false;
  const cleanup = () => { if (done) return; done = true; ["mousemove", "mouseup", "touchmove", "touchend"].forEach((t, i) => window.removeEventListener(t, i < 2 ? (i ? onUp : onMove) : (i === 2 ? onMove : onUp), true)); };
  const onMove = (ev) => {
    const x = lwEvX(ev), y = lwEvY(ev); if (x == null) return;
    if (dragging && ((ev.buttons != null && ev.buttons === 0) || !lwKonva || !lwKonva.drag)) { onUp(); return; }  // missed mouseup / force-idle → end
    if (!dragging) {
      if (Math.hypot(x - sx, y - sy) <= 4) return;
      dragging = true; lwKonva.drag = { lead: entry, single: true }; lwSetCursor("grabbing"); lwShowGrid(); lwPortalShow(true);
      lwLog("drag", `begin ${kind}#${entry.data.id}`, { at: lwRoundPt(node.position()) }, "info");
    }
    const w = lwClientToWorld(x, y); if (!w) { cleanup(); return; }   // canvas torn down mid-gesture
    node.position({ x: x0 + (w.x - w0.x), y: y0 + (w.y - w0.y) });
    if (kind === "agent") lwAgentDragStep(entry.data, node); else lwPropDragStep(entry.data, entry, node);
    lwLogOn && lwLogThr("dm" + entry.data.id, 120, "drag", `move ${kind}#${entry.data.id}`, { at: lwRoundPt(node.position()), snap: lwKonva.snap ? lwKonva.snap.propId : null, portal: lwKonva.overPortal }, "debug");
  };
  const onUp = async () => {
    cleanup();
    if (!dragging) return;                             // released without moving → it was a click; selection stands
    const wasPortal = lwKonva.overPortal; lwKonva.drag = null; lwHideGrid();
    if (wasPortal) { lwLog("drag", `${kind}#${entry.data.id} → PORTAL delete`, null, "info"); await lwPortalSink([{ kind, entry }]); return; }
    lwPortalShow(false); lwSetCursor("move");
    lwLog("drag", `end ${kind}#${entry.data.id} → drop`, { at: lwRoundPt(node.position()), snapSeat: lwKonva.snap ? lwKonva.snap.propId : null }, "info");
    if (kind === "agent") await lwOnAgentDrop(node, entry.data); else await lwPropDrop(entry.data, entry, node);
  };
  window.addEventListener("mousemove", onMove, true); window.addEventListener("mouseup", onUp, true);
  window.addEventListener("touchmove", onMove, true); window.addEventListener("touchend", onUp, true);
}
// Drag from a connection handle to another token → connect. Geometric target pick on release.
function lwBeginConnect(fromEntry, downEvt) {
  const sx = lwEvX(downEvt), sy = lwEvY(downEvt); if (sx == null) return;
  lwKonva.connecting = { from: fromEntry.data.id };
  const p = fromEntry.node.position();
  lwConnRubber = new Konva.Arrow({ points: [p.x, p.y, p.x, p.y], stroke: "hsl(160 62% 42%)", fill: "hsl(160 62% 42%)",
    strokeWidth: 2, dash: [6, 4], listening: false, pointerLength: lwThreadDir === "one" ? 9 : 0, pointerWidth: 9 });
  lwKonva.worldLayer.add(lwConnRubber); lwSetCursor("crosshair"); lwClearHandles();
  let done = false;
  const cleanup = () => { if (done) return; done = true; window.removeEventListener("mousemove", onMove, true); window.removeEventListener("mouseup", onUp, true); window.removeEventListener("touchmove", onMove, true); window.removeEventListener("touchend", onUp, true); };
  const onMove = (ev) => {
    const x = lwEvX(ev), y = lwEvY(ev); if (x == null || !lwConnRubber) return;
    if (ev.buttons != null && ev.buttons === 0) { onUp(ev); return; }   // missed mouseup → resolve the connect
    const w = lwClientToWorld(x, y); if (!w) { cleanup(); return; }
    lwConnRubber.points([p.x, p.y, w.x, w.y]); lwKonva.worldLayer.batchDraw();
  };
  const onUp = async (ev) => {
    cleanup();
    const conn = lwKonva.connecting; lwKonva.connecting = null;
    if (lwConnRubber) { lwConnRubber.destroy(); lwConnRubber = null; }
    const x = lwEvX(ev), y = lwEvY(ev), w = x != null ? lwClientToWorld(x, y) : null;
    let targetId = null, bestD = Infinity;
    if (w) [lwKonva.agents, lwKonva.props].forEach((map) => map.forEach((en) => {
      const q = en.node.position(), d = Math.hypot(q.x - w.x, q.y - w.y);
      if (en.data.id !== (conn && conn.from) && d < Math.max(lwTokenRadius(en) + 8, 46) && d < bestD) { bestD = d; targetId = en.data.id; }
    }));
    lwSetCursor(lwToolCursor(lwTool)); lwUpdateHandles(); lwKonva.worldLayer.batchDraw();
    if (!conn || !targetId || targetId === conn.from) { lwPtrLog(0, "connect cancelled", { target: targetId }); return; }
    lwPtrLog(0, `connect #${conn.from} → #${targetId}`, { dir: lwThreadDir });
    try {
      await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/thread/connect`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ a: conn.from, b: targetId, dir: lwThreadDir === "one" ? "a2b" : "both" }) });
      sdFlash(); await lwReloadRoom();
    } catch (e) { toast(`Could not connect: ${e.message}`); }
  };
  window.addEventListener("mousemove", onMove, true); window.addEventListener("mouseup", onUp, true);
  window.addEventListener("touchmove", onMove, true); window.addEventListener("touchend", onUp, true);
}
// The single, authoritative pointer-down decision. Attached to the stage; runs for EVERY press.
function lwStageMousedown(e) {
  if (lwTool !== "select" || lwKonva.panning) return;      // placement tools & space-pan handled elsewhere
  const w = lwPointerWorld(); if (!w) return;
  const shift = !!(e && e.evt && e.evt.shiftKey), seq = ++lwPtrSeq, ev = e && e.evt;
  // 1) a connection handle of the single selection → start a connection
  const hp = lwPickHandle(w);
  if (hp) { lwPtrLog(seq, `handle → connect from #${hp.entry.data.id}`, { d: Math.round(hp.d) }); lwBeginConnect(hp.entry, ev); return; }
  // 2) a token (body + slop) → select (or extend/toggle) and arm a drag
  const tk = lwPickToken(w);
  if (tk) {
    const entry = tk.entry, already = lwSelHas(entry), grouped = lwSelList().length >= 2 && already && !shift;
    lwPtrLog(seq, `token #${tk.id}`, { d: Math.round(tk.dist), r: tk.radius, grouped, shift });
    lwClearGraphUI();
    // Press on a member of a 2+ selection: DRAG moves the whole group, a plain CLICK collapses to
    // just this one (anchor passed so the click can re-select it).
    if (grouped) { lwArmGroupGrab(ev, { kind: tk.type, entry }); return; }
    if (shift) lwSelToggle(tk.type, entry);
    else if (!already) lwSelSet(tk.type, entry);            // fresh single-select (keep it if already the sole selection)
    lwArmSingleDrag(tk.type, entry, ev);
    return;
  }
  // 3) a connection line → select the edge (checked BEFORE the group gap, since a wire can lie
  //    inside a multi-selection's bounding box and must still be grabbable).
  const edge = lwGeomHitEdge(w);
  if (edge) { lwPtrLog(seq, `edge tid=${edge.tid}`, { a: edge.a, b: edge.b }); lwSelectEdge(edge.line, edge.tid, edge.a, edge.b); return; }
  // 4) inside a 2+ selection's bounds but not on a token/wire → drag the whole group (frameless), never marquee
  if (lwSelList().length >= 2 && !shift) {
    const bb = lwSelBounds();
    if (bb && w.x >= bb.x - 8 && w.x <= bb.x + bb.w + 8 && w.y >= bb.y - 8 && w.y <= bb.y + bb.h + 8) {
      lwPtrLog(seq, "group gap → drag group", { n: lwSelList().length }); lwArmGroupGrab(ev); return;
    }
  }
  // 5) empty floor → deselect (unless shift-extending) and rubber-band a marquee
  lwPtrLog(seq, "empty → marquee", { hadSel: lwKonva.sel.size, shift });
  if (!shift) { lwSelClear(); lwClearGraphUI(); }
  const rect = new Konva.Rect({ x: w.x, y: w.y, width: 0, height: 0, fill: "rgba(46,110,91,.08)",
    stroke: "#2E6E5B", strokeWidth: 1, dash: [4, 4], listening: false, name: "marquee" });
  lwKonva.worldLayer.add(rect); rect.moveToTop();
  lwKonva.marquee = { x0: w.x, y0: w.y, rect, add: shift };
  window.addEventListener("mouseup", lwKonva.endMarquee, true);
  window.addEventListener("touchend", lwKonva.endMarquee, true);
  window.addEventListener("pointercancel", lwKonva.endMarquee, true);
}

// Group drag WITHOUT a draggable frame. The selection frame is now hit-transparent (so it can't
// poison the hit graph), so pressing inside a 2+ selection — on a gap between tokens, or on a token
// the hit graph missed — is caught here and drives the whole group. Movement >4px starts the drag;
// a release without moving is a plain click (selection preserved). Mirrors the node group-drag path.
function lwArmGroupGrab(downEvt, anchor) {
  const cx = (ev) => ev.clientX != null ? ev.clientX : (ev.touches && ev.touches[0] ? ev.touches[0].clientX : null);
  const cy = (ev) => ev.clientY != null ? ev.clientY : (ev.touches && ev.touches[0] ? ev.touches[0].clientY : null);
  const sx = downEvt && cx(downEvt), sy = downEvt && cy(downEvt);
  if (sx == null || sy == null) return;
  const members = lwSelList().map((s) => ({ kind: s.kind, entry: s.entry, node: s.entry.node, x0: s.entry.node.x(), y0: s.entry.node.y() }));
  if (members.length < 2) return;
  const scale = () => (lwKonva.stage.scaleX() || 1);
  let dragging = false, done = false;
  const cleanup = () => { if (done) return; done = true; window.removeEventListener("mousemove", onMove, true); window.removeEventListener("mouseup", onUp, true); window.removeEventListener("touchmove", onMove, true); window.removeEventListener("touchend", onUp, true); };
  const onMove = (ev) => {
    const x = cx(ev), y = cy(ev); if (x == null) return;
    if (dragging && ((ev.buttons != null && ev.buttons === 0) || !lwKonva || !lwKonva.drag)) { onUp(); return; }  // missed mouseup / force-idle → end
    if (!dragging) {
      if (Math.hypot(x - sx, y - sy) <= 4) return;
      dragging = true; lwKonva.drag = { group: true, members }; lwSetCursor("grabbing"); lwShowGrid(); lwPortalShow(true);
      lwLog("drag", "begin group (frameless)", { members: members.length }, "info");
    }
    const dx = (x - sx) / scale(), dy = (y - sy) / scale();
    members.forEach((m) => { m.node.position({ x: m.x0 + dx, y: m.y0 + dy }); if (m.kind === "prop") lwFollowProp(m.entry); });
    lwFitSelFrame(); lwUpdateArrows(); lwShowGrid(); lwPortalOver(); lwKonva.worldLayer.batchDraw();
  };
  const onUp = async () => {
    cleanup();
    if (!dragging) {                               // released without moving → it was a CLICK
      if (anchor) { lwClearGraphUI(); lwSelSet(anchor.kind, anchor.entry); }   // on a member → collapse to just it
      return;                                       // on a gap → keep the whole selection
    }
    lwKonva.drag = null; lwHideGrid();
    if (lwKonva.overPortal) { lwLog("drag", "group (frameless) → PORTAL delete", { n: members.length }, "info"); await lwPortalSink(members); return; }
    lwPortalShow(false); lwSetCursor("move");
    lwLog("drag", "group (frameless) → move", { n: members.length }, "info");
    await lwDragGroupPersist(members); lwFitSelFrame(); lwKonva.worldLayer.batchDraw();
  };
  window.addEventListener("mousemove", onMove, true);
  window.addEventListener("mouseup", onUp, true);
  window.addEventListener("touchmove", onMove, true);
  window.addEventListener("touchend", onUp, true);
}
// Geometric edge pick: which thread line (if any) the world point is on — the arrows' own hit
// routing misses under the same DPR glitch, so a press near a line selects the edge deterministically.
function lwPointToSeg(p, a, b) {
  const dx = b.x - a.x, dy = b.y - a.y, l2 = dx * dx + dy * dy;
  if (l2 === 0) return Math.hypot(p.x - a.x, p.y - a.y);
  let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / l2; t = Math.max(0, Math.min(1, t));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}
function lwGeomHitEdge(w) {
  if (!lwKonva || !lwKonva.threadLines || !w) return null;
  for (const tl of lwKonva.threadLines) {
    const A = lwNodeById(tl.a), B = lwNodeById(tl.b); if (!A || !B) continue;
    if (lwPointToSeg(w, A.node.position(), B.node.position()) < 12) {
      const t = ((lwRoom && lwRoom.threads) || []).find((th) => (th.edges || []).some((e) => (e[0] === tl.a && e[1] === tl.b) || (e[0] === tl.b && e[1] === tl.a)));
      if (t) return { line: tl.line, tid: t.id, a: tl.a, b: tl.b };
    }
  }
  return null;
}

let lwConnRubber = null;
function lwClearHandles() {
  if (lwKonva && lwKonva.handles) { lwKonva.handles.forEach((h) => h.destroy()); lwKonva.handles = []; }
}
function lwUpdateHandles() {
  if (!lwKonva) return;
  lwClearHandles();
  const list = lwSelList();
  if (list.length !== 1 || lwTool !== "select") { lwKonva.worldLayer.batchDraw(); return; }
  // Purely VISUAL affordances — the 4 dots just show WHERE to press to draw a wire. Pressing one is
  // detected geometrically (lwPickHandle) and the drag runs through lwBeginConnect. They are
  // listening:false so they can never occlude the token underneath on the hit canvas.
  const h = lwHandlePoints(); if (!h) { lwKonva.worldLayer.batchDraw(); return; }
  lwKonva.handles = [];
  h.pts.forEach((p) => {
    const dot = new Konva.Circle({ x: p.x, y: p.y, radius: 6, fill: "#fff",
      stroke: "hsl(160 62% 42%)", strokeWidth: 2, listening: false, name: "handle" });
    lwKonva.worldLayer.add(dot); dot.moveToTop();
    lwKonva.handles.push(dot);
  });
  lwKonva.worldLayer.batchDraw();
}
// ---- the contextual action bar (top of the canvas) ------------------------
function lwActionBarEl() {
  let el = $("#lwActionBar");
  if (!el) { const o = $("#lwOverlay"); if (!o) return null; el = document.createElement("div"); el.id = "lwActionBar"; el.className = "lw-actionbar"; el.hidden = true; o.appendChild(el); }
  return el;
}
function lwShowActions(title, items) {
  const el = lwActionBarEl(); if (!el) return;
  el.innerHTML = `<span class="lw-act-title">${escapeHtml(title)}</span>`;
  items.forEach((it) => {
    const b = document.createElement("button");
    b.className = "lw-act-btn" + (it.danger ? " danger" : "");
    b.textContent = it.label;
    b.addEventListener("click", it.onClick);
    el.appendChild(b);
  });
  el.hidden = false;
}
function lwHideActions() { const el = $("#lwActionBar"); if (el) { el.hidden = true; el.innerHTML = ""; } }

let lwSelEdge = null;   // the currently selected connection {tid, a, b}
function lwClearGraphUI() {                       // drop any edge/graph selection chrome
  lwSelEdge = null;
  lwHideActions();
  if (lwKonva) { lwKonva.worldLayer.find(".thread").forEach((l) => { l.strokeWidth(2); l.opacity(0.55); }); lwKonva.worldLayer.batchDraw(); }
}
function lwSelectEdge(line, tid, a, b) {
  lwSelClear();                                   // an edge selection isn't a node selection
  lwSelEdge = { tid, a, b };
  lwKonva.worldLayer.find(".thread").forEach((l) => { l.strokeWidth(2); l.opacity(0.35); });
  line.strokeWidth(4); line.opacity(1); lwKonva.worldLayer.batchDraw();
  lwShowActions("connection", [{ label: "✕ Remove", danger: true, onClick: async () => {
    try {
      await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/thread/disconnect`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ a, b }) });
      sdFlash(); lwClearGraphUI(); await lwReloadRoom();
    } catch (e) { toast(`Could not remove: ${e.message}`); }
  } }]);
}
// Double-click a node → select its whole graph and surface a manage button. Returns false if ungrouped.
function lwGraphSelect(id) {
  const t = ((lwRoom && lwRoom.threads) || []).find((th) => (th.edges || []).some((e) => e[0] === id || e[1] === id));
  if (!t) return false;
  lwClearGraphUI();
  lwSelClear();
  [...new Set(t.edges.flatMap((e) => [e[0], e[1]]))].forEach((nid) => {
    const en = lwNodeById(nid); if (en) lwSelAdd(lwNodeKind(nid), en);
  });
  lwShowActions("graph", [
    { label: "▶ Run", onClick: () => sdRunGraph(t.id) },
    { label: "💬 Chat", onClick: () => sdOpenChat(t.id) },
    { label: "⚙ Rules", onClick: () => sdOpenThreads(t.id) },
    { label: "✕ Delete graph", danger: true, onClick: async () => {
      if (!confirm("Delete this graph? The tokens stay; only the connections go.")) return;
      try { await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/thread/${t.id}`, { method: "DELETE" }); sdFlash(); lwClearGraphUI(); await lwReloadRoom(); }
      catch (e) { toast(`Could not delete: ${e.message}`); }
    } },
  ]);
  return true;
}
function lwRenderThreads(threads) {
  if (!lwKonva) return;
  lwKonva.worldLayer.find(".thread").forEach((n) => n.destroy());
  lwKonva.threadLines = [];
  (threads || []).forEach((t) => (t.edges || []).forEach((e) => {
    const A = lwNodeById(e[0]), B = lwNodeById(e[1]);      // agents OR objects
    if (!A || !B) return;
    const pa = A.node.position(), pb = B.node.position(), dir = e[2] || "both";
    // listening:false — a wire never intercepts a press. Edge selection & the pointer cursor are
    // geometric (lwGeomHitEdge), so a listening wire (which used to swallow the press as e.target and
    // block the whole mousedown decision — the "arrow single-click does nothing" bug) is not needed.
    const line = new Konva.Arrow({ points: [pa.x, pa.y, pb.x, pb.y], stroke: "hsl(160 45% 42%)",
      fill: "hsl(160 45% 42%)", strokeWidth: 2, opacity: 0.55, dash: t.closed ? [] : [8, 5], name: "thread",
      pointerLength: dir === "both" ? 0 : 9, pointerWidth: dir === "both" ? 0 : 9,
      pointerAtBeginning: dir === "b2a", listening: false });
    lwKonva.worldLayer.add(line); line.moveToBottom();
    lwKonva.threadLines.push({ line, a: e[0], b: e[1] });
  }));
  lwKonva.worldLayer.batchDraw();
}
function lwUpdateThreadLines() {
  if (!lwKonva || !lwKonva.threadLines) return;
  lwKonva.threadLines.forEach((tl) => {
    const A = lwNodeById(tl.a), B = lwNodeById(tl.b);      // agents OR objects (props), not just agents
    if (A && B) { const pa = A.node.position(), pb = B.node.position(); tl.line.points([pa.x, pa.y, pb.x, pb.y]); }
  });
}
function lwClearGlows() {
  if (!lwKonva) return;
  lwKonva.glowing.forEach((n) => lwSetGlow(n, false));
  lwKonva.glowing.clear();
}
function lwAddClusterGlow(entry) {
  // A soft, low-key halo that hugs the seated ring — signals "one entity" without a
  // big bordered circle. Only drawn for a full table.
  const pos = entry.node.position();
  const rad = lwPropRadius(entry.data) + LW_SEAT_OUT + 22;
  const ring = new Konva.Circle({ x: pos.x, y: pos.y, radius: rad,
    fillRadialGradientStartPoint: { x: 0, y: 0 }, fillRadialGradientStartRadius: rad * 0.6,
    fillRadialGradientEndPoint: { x: 0, y: 0 }, fillRadialGradientEndRadius: rad,
    fillRadialGradientColorStops: [0, "rgba(46,110,91,0)", 1, "rgba(46,110,91,0.14)"],
    listening: false, name: "clusterGlow" });
  lwKonva.worldLayer.add(ring);
  ring.moveToBottom();
  entry.glow = ring;
}

// ---- dragging: move one token, a whole selection, or magnetically seat --------
// (single & group drag are armed geometrically in the interaction core: lwArmSingleDrag /
//  lwArmGroupGrab. The old Konva node-drag wiring — lwDragBegin/lwDragGroupMove/lwWire*Drag — is gone.)
function lwFitSelFrame() {                             // reposition the visual frame without destroy/recreate
  const f = lwKonva && lwKonva.selframe; if (!f) return;
  const b = lwSelBounds(); f.position({ x: b.x, y: b.y }); f.size({ width: b.w, height: b.h });
}
async function lwDragGroupPersist(members) {
  members = members || (lwKonva.drag && lwKonva.drag.members);
  if (!members) return;
  await Promise.all(members.map((m) => {
    const pos = m.node.position();
    return api(`/api/lw/${lwWorldId}/pos`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: m.entry.data.id, x: pos.x, y: pos.y }) }).catch(() => {});
  }));
  lwSaveView();
}

// ---- the delete portal: drag a token or a group to the far right to dump it -------
// A screen-fixed sink on the right edge that appears while dragging, glows when the
// pointer enters it, and swallows what you drop — animating the sink, then deleting on
// the server. If a delete fails the reload brings that item back, so it's safe.
function lwPortalEl() {
  let el = $("#sdPortal");
  if (!el) {
    const overlay = $("#lwOverlay"); if (!overlay) return null;
    el = document.createElement("div"); el.id = "sdPortal"; el.className = "sd-portal";
    el.innerHTML = `<span class="sd-portal-icon" aria-hidden="true">🗑</span><span class="sd-portal-lbl">drop to delete</span>`;
    overlay.appendChild(el);
  }
  return el;
}
function lwPortalShow(on) {
  const el = lwPortalEl(); if (!el) return;
  el.classList.toggle("active", !!on);
  if (!on) { el.classList.remove("hot"); if (lwKonva) lwKonva.overPortal = false; }
}
function lwPortalOver() {
  if (!lwKonva) return;
  const el = $("#sdPortal"); if (!el) return;
  const pos = lwKonva.stage.getPointerPosition(); if (!pos) return;
  const over = pos.x >= (lwKonva.host.clientWidth || 900) - 130;   // the pull zone: right 130px
  if (lwLogOn && over !== lwKonva.overPortal) lwLog("portal", over ? "entered delete zone" : "left delete zone", null, "debug");
  el.classList.toggle("hot", over);
  lwKonva.overPortal = over;
}
async function lwPortalSink(members) {
  if (!members || !members.length) { lwPortalShow(false); return; }
  lwLog("portal", "sink → delete", { ids: members.map((m) => m.entry.data.id) }, "info");
  const el = lwPortalEl(); if (el) el.classList.add("ingest");
  lwPortalShow(false);
  const stage = lwKonva.stage;
  const zx = (lwKonva.host.clientWidth || 900) - 44, zy = (lwKonva.host.clientHeight || 600) / 2;
  const world = stage.getAbsoluteTransform().copy().invert().point({ x: zx, y: zy });
  const ids = members.map((m) => m.entry.data.id);
  await Promise.all(members.map((m) => new Promise((res) => {
    const node = m.entry.node;
    if (reduceMotion()) { node.hide(); res(); return; }
    new Konva.Tween({ node, x: world.x, y: world.y, scaleX: 0.05, scaleY: 0.05, opacity: 0, rotation: 200,
      duration: 0.45, easing: Konva.Easings.EaseIn, onFinish: res }).play();
  })));
  if (lwKonva) lwKonva.worldLayer.batchDraw();
  let ok = 0;
  await Promise.all(ids.map((id) => api(`/api/lw/${lwWorldId}/entity/${id}`, { method: "DELETE" }).then(() => { ok++; }).catch(() => {})));
  if (el) el.classList.remove("ingest");
  toast(ok === ids.length ? `Deleted ${ok} ${ok === 1 ? "thing" : "things"}` : `Deleted ${ok} of ${ids.length} — the rest rolled back`);
  sdFlash(); lwSelClear();
  if (lwKonva) lwSetCursor(lwToolCursor(lwTool));   // the token is gone; the pointer is over floor now
  await lwReloadRoom();      // server truth: deleted ones are gone, any that failed reappear
}

// A table carries its ring of seated agents and its cluster glow as it moves, so a
// cluster drags as one single entity.
function lwFollowProp(entry) {
  const p = entry.data, base = entry.node.position(), slots = Number(p.slots) || 0, sp = lwSlotPositions(p);
  if (entry.glow) entry.glow.position(base);
  if (slots > 0 && Array.isArray(p.seated))
    p.seated.forEach((aid, i) => {
      if (aid == null) return;
      const ae = lwKonva.agents.get(String(aid));
      if (ae) { const off = sp[i] || { x: 0, y: 0 }; ae.node.position({ x: base.x + off.x, y: base.y + off.y }); }
    });
}
async function lwOnAgentDrop(node, a) {
  lwHideGrid();
  const snap = lwKonva && lwKonva.snap; lwKonva.snap = null;
  const entry = lwKonva.agents.get(String(a.id));
  const wasSeated = entry && entry.seat;
  const gp = node.position();
  lwClearGlows(); lwKonva.worldLayer.batchDraw();

  if (snap) {
    const seat = async () => {
      try {
        await api(`/api/lw/${lwWorldId}/artifact/${snap.propId}/seat`, { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slot: snap.slot, human_id: a.id }) });
        await api(`/api/lw/${lwWorldId}/pos`, { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: a.id, x: snap.x, y: snap.y }) });
        lwLogOn && lwLog("net", `seated agent#${a.id} at prop#${snap.propId} slot ${snap.slot}`, null, "info");
      } catch (e) { lwLog("net", `seat agent#${a.id} FAILED`, { err: e.message }, "warn"); toast(`Could not seat them: ${e.message}`); }
      await lwReloadRoom();
    };
    if (reduceMotion()) { node.position({ x: snap.x, y: snap.y }); lwKonva.worldLayer.batchDraw(); seat(); }
    else new Konva.Tween({ node, x: snap.x, y: snap.y, duration: 0.2, easing: Konva.Easings.EaseOut, onFinish: seat }).play();
    return;
  }

  try {
    if (wasSeated)
      await api(`/api/lw/${lwWorldId}/artifact/${wasSeated.propId}/unseat?human_id=${encodeURIComponent(a.id)}`, { method: "POST" });
    await api(`/api/lw/${lwWorldId}/pos`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: a.id, x: gp.x, y: gp.y }) });
    lwLogOn && lwLog("net", `pos agent#${a.id} saved${wasSeated ? " (unseated)" : ""}`, { at: lwRoundPt(gp) }, "debug");
    if (wasSeated) await lwReloadRoom();   // cluster changed → repaint
    else { lwUpdateArrows(); lwSaveView(); }   // token is already where it was dropped
  } catch (e) { lwLog("net", `pos agent#${a.id} FAILED`, { err: e.message }, "warn"); toast(`Could not move them: ${e.message}`); await lwReloadRoom(); }
}

// ---- creation: single-flight, a pending token + an inline figure popover ---
function lwStartCreate(tool, world) {
  if (lwCreateFlow) return;                        // ignore extra clicks until resolved
  const v2 = lwCanvasV2On();
  if (!v2 && !lwKonva) return;
  const isShape = tool === "shape";               // a shape is a collating artifact with slots
  const kind = (tool === "agent") ? "agent" : "artifact";
  // v2 draws no Konva shimmer — the popover opens straight at the click point.
  let shimmer = null, anim = null;
  if (!v2) {
    shimmer = lwPendingNode(world.x, world.y, kind);
    lwKonva.worldLayer.add(shimmer);
    lwKonva.worldLayer.batchDraw();
    if (!reduceMotion()) {
      anim = new Konva.Animation((frame) => {
        const s = 1 + 0.08 * Math.sin(frame.time / 180);
        shimmer.scale({ x: s, y: s });
      }, lwKonva.worldLayer);
      anim.start();
    }
  }
  const figure = kind === "artifact" ? "ic:" + LW_OBJ_ICONS[0] : "av:" + LW_AV_VARIANTS[0];
  lwCreateFlow = { tool, kind, isShape, world, shimmer, anim, figure, seats: isShape ? 3 : 0,
                   name: "", brief: "", shape: "circle", path: [], pathPts: [], drawing: false,
                   model: "", dials: {}, drive: "", libType: "", spec: null,
                   mode: "new", pop: null, preview: null, busy: false, dragged: false };
  lwOpenCreatePopover();
}
function lwPendingNode(x, y, kind) {
  const g = new Konva.Group({ x, y, listening: false, name: "pending" });
  g.add(new Konva.Circle({ radius: 30, fill: "rgba(46,110,91,.10)", stroke: "hsl(160 45% 50%)", strokeWidth: 2, dash: [5, 5] }));
  g.add(new Konva.Text({ text: kind === "artifact" ? "▢" : "＋", fontSize: 22, fontFamily: "sans-serif",
    fill: "hsl(160 45% 40%)", width: 60, height: 60, align: "center", verticalAlign: "middle", offsetX: 30, offsetY: 30 }));
  return g;
}
function lwSetPendingLabel(node, txt, small) {
  const t = node && node.findOne("Text"); if (!t) return;
  t.text(txt); t.fontSize(small ? 11 : 22);
  lwKonva && lwKonva.worldLayer.batchDraw();
}
function lwPositionOverlayAt(el, world, dy) {
  if (lwCreateFlow && lwCreateFlow.dragged && el.classList.contains("lw-create-pop")) return;  // user moved it
  let p, host;
  if (lwCanvasV2On()) {
    const inst = window.LWCanvas2._inst; if (!inst) return;
    host = inst.host; const r = host.getBoundingClientRect(), s = inst.world.toScreen(world.x, world.y);
    p = { x: s.x - r.left, y: s.y - r.top };       // host-relative (toScreen returns client coords)
  } else {
    if (!lwKonva) return;
    p = lwKonva.stage.getAbsoluteTransform().point(world);
    host = lwKonva.host;
  }
  const hw = host.clientWidth || 900, hh = host.clientHeight || 460;
  const w = el.offsetWidth || 258, h = el.offsetHeight || 260;
  // the popover is translateX(-50%); keep it fully inside the canvas, never off-edge.
  let left = Math.min(Math.max(p.x, w / 2 + 8), hw - w / 2 - 8);
  let top = Math.min(Math.max(p.y + (dy || 0), 8), Math.max(8, hh - h - 8));
  el.style.left = left + "px";
  el.style.top = top + "px";
}

// The figure chooser: generated avatar swatches for people (re-rendered as the name
// is typed so the preview is the real face), vector-glyph swatches for objects.
function lwFigPaletteHtml(flow) {
  if (flow.kind === "artifact") {
    return LW_OBJ_ICONS.map((key) => {
      const on = flow.figure === "ic:" + key;
      const inner = key === "mono" ? `<span class="lw-mono">A</span>` : lwObjGlyphSvg(key, 26, "currentColor");
      return `<button class="lw-figbtn${on ? " on" : ""}" data-fig="ic:${escapeHtml(key)}" title="${escapeHtml(key === "mono" ? "monogram" : key)}">${inner}</button>`;
    }).join("");
  }
  const marker = "av:";
  const base = (flow.name || "").trim() || "new soul";
  return LW_AV_VARIANTS.map((tag) => {
    const on = flow.figure === marker + tag;
    return `<button class="lw-figbtn lw-avbtn${on ? " on" : ""}" data-fig="${marker}${escapeHtml(tag)}" title="face ${escapeHtml(tag.toUpperCase())}">`
      + `<img alt="" src="${lwSvgUri(lwAvatarSvg(tag + "|" + base, 30))}"></button>`;
  }).join("");
}

// Let the operator slide the popover anywhere by its header — a smooth, real dialog.
function lwMakeDraggable(pop, handle, flow) {
  if (!handle) return;
  let sx = 0, sy = 0, ox = 0, oy = 0, dragging = false;
  handle.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".lw-pop-x")) return;
    dragging = true;
    const r = pop.getBoundingClientRect();
    const base = pop.offsetParent ? pop.offsetParent.getBoundingClientRect() : { left: 0, top: 0 };
    pop.style.transform = "none";              // drop the centering transform once grabbed
    ox = r.left - base.left; oy = r.top - base.top;
    pop.style.left = ox + "px"; pop.style.top = oy + "px";
    if (flow) flow.dragged = true;
    sx = e.clientX; sy = e.clientY;
    try { handle.setPointerCapture(e.pointerId); } catch (_) { /* older engines */ }
    e.preventDefault();
  });
  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    pop.style.left = (ox + e.clientX - sx) + "px";
    pop.style.top = (oy + e.clientY - sy) + "px";
  });
  const stop = () => { dragging = false; };
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
}

// The mind an agent possesses, and the clean base-DNA questions that set its psyche.
const LW_MODELS = [
  { id: "", name: "Auto", sub: "world default" },
  { id: "claude-opus-4-8", name: "Opus 4.8", sub: "deepest" },
  { id: "claude-sonnet-5", name: "Sonnet 5", sub: "balanced" },
  { id: "claude-haiku-4-5-20251001", name: "Haiku 4.5", sub: "fast · cheap" },
  { id: "claude-fable-5", name: "Fable 5", sub: "creative" },
];
const LW_DNA = [
  { trait: "composure", q: "Under pressure", opts: [["cracks", 20], ["steady", 50], ["ice-cold", 85]] },
  { trait: "risk_appetite", q: "With risk", opts: [["cautious", 20], ["balanced", 50], ["bold", 85]] },
  { trait: "sociability", q: "Around people", opts: [["reserved", 20], ["easy", 50], ["magnetic", 85]] },
  { trait: "empathy", q: "Toward others", opts: [["cool", 20], ["fair", 50], ["warm", 85]] },
  { trait: "willpower", q: "Chasing a goal", opts: [["drifts", 20], ["steady", 55], ["relentless", 85]] },
];
const LW_MOTIVES = [["belonging", "social"], ["winning", "esteem"], ["knowing", "curiosity"], ["meaning", "purpose"]];

function lwDnaHtml(flow) {
  const models = LW_MODELS.map((m) =>
    `<button class="lw-mind${(flow.model || "") === m.id ? " on" : ""}" data-mind="${escapeHtml(m.id)}" title="${escapeHtml(m.sub)}">
       <b>${escapeHtml(m.name)}</b><span>${escapeHtml(m.sub)}</span></button>`).join("");
  const qs = LW_DNA.map((d) => {
    const cur = flow.dials[d.trait];
    const segs = d.opts.map(([label, val]) =>
      `<button class="lw-seg${cur === val ? " on" : ""}" data-trait="${d.trait}" data-val="${val}">${escapeHtml(label)}</button>`).join("");
    return `<div class="lw-dna-q"><span class="lw-dna-lbl">${escapeHtml(d.q)}</span><div class="lw-seg-row">${segs}</div></div>`;
  }).join("");
  const motives = LW_MOTIVES.map(([label, drive]) =>
    `<button class="lw-seg${flow.drive === drive ? " on" : ""}" data-drive="${drive}">${escapeHtml(label)}</button>`).join("");
  return `<div class="sc-label">Mind <span class="dim">the model it thinks with</span></div>
    <div class="lw-mind-row" id="lwCMind">${models}</div>
    <div class="sc-label">Base DNA <span class="dim">a few strokes; the rest is authored from the brief</span></div>
    <div class="lw-dna" id="lwCDna">${qs}
      <div class="lw-dna-q"><span class="lw-dna-lbl">Driven by</span><div class="lw-seg-row" id="lwCMotive">${motives}</div></div>
    </div>`;
}

// The generic artifact builder: recombine vetted components into a spec (no code, no exec).
const LW_COMPONENTS = [
  { kind: "multiset", label: "Deck / multiset", param: "builder" },
  { kind: "sealable", label: "Sealed value · holder-only" },
  { kind: "flippable", label: "Flippable" },
  { kind: "rollable", label: "Rollable · dice", param: "faces" },
  { kind: "countable", label: "Counter / pot" },
  { kind: "slotted", label: "Seats agents", param: "slots" },
];
function lwBuildHtml(flow) {
  const b = flow.build;
  const rows = LW_COMPONENTS.map((c) => {
    const cur = b.comps[c.kind], on = !!cur;
    let param = "";
    if (c.param === "builder") param = `<select data-param="${c.kind}:builder" class="lw-bparam"${on ? "" : " disabled"}>
      <option value="standard52"${cur && cur.builder === "standard52" ? " selected" : ""}>52 cards</option>
      <option value="dice6"${cur && cur.builder === "dice6" ? " selected" : ""}>6 dice faces</option></select>`;
    if (c.param === "faces") param = `<input data-param="${c.kind}:faces" class="lw-bparam" type="number" min="2" max="20" value="${(cur && cur.faces) || 6}"${on ? "" : " disabled"}>`;
    if (c.param === "slots") param = `<input data-param="${c.kind}:slots" class="lw-bparam" type="number" min="1" max="8" value="${(cur && cur.slots) || 4}"${on ? "" : " disabled"}>`;
    return `<label class="lw-bcomp"><input type="checkbox" data-comp="${c.kind}"${on ? " checked" : ""}> <span>${escapeHtml(c.label)}</span> ${param}</label>`;
  }).join("");
  return `<input class="sc-name" id="lwBType" placeholder="type name (e.g. coin)" value="${escapeHtml(b.type || "")}">
    <div class="sc-label">Components <span class="dim">recombine vetted parts — never code</span></div>
    <div class="lw-bcomps">${rows}</div>
    <label class="lw-bsave"><input type="checkbox" id="lwBSaveOn"${b.saveAs ? " checked" : ""}> save to Custom as
      <input class="sc-name lw-bsavename" id="lwBSaveName" placeholder="name" value="${escapeHtml(b.saveAs || "")}"></label>`;
}
function lwBuildSpec(flow) {
  const comps = [];
  Object.entries(flow.build.comps).forEach(([kind, p]) => { if (p) comps.push(Object.assign({ kind }, p)); });
  return comps.length ? { type: (flow.build.type || "object").trim() || "object", components: comps } : null;
}
async function lwLoadLib(flow) {
  const host = $("#lwLibList"); if (!host) return;
  let lib;
  try { lib = await api(`/api/lw/${lwWorldId}/artifact-lib`); }
  catch (e) { host.innerHTML = `<p class="dim">Could not load the library.</p>`; return; }
  const entries = [...Object.keys(lib.shipped || {}).map((k) => [k, true]), ...Object.keys(lib.custom || {}).map((k) => [k, false])];
  host.innerHTML = entries.map(([k, shipped]) =>
    `<button class="lw-lib-item${flow.libType === k ? " on" : ""}" data-lib="${escapeHtml(k)}">${escapeHtml(k)}${shipped ? "" : ` <span class="dim">· yours</span>`}</button>`).join("")
    || `<p class="dim">Nothing saved yet — build one and save it.</p>`;
  host.querySelectorAll("[data-lib]").forEach((b) => b.addEventListener("click", () => {
    flow.libType = b.dataset.lib;
    host.querySelectorAll("[data-lib]").forEach((x) => x.classList.toggle("on", x === b));
  }));
}

function lwOpenCreatePopover() {
  const flow = lwCreateFlow, overlay = $("#lwOverlay");
  if (!flow || !overlay) return;
  overlay.querySelectorAll(".lw-create-pop").forEach((n) => n.remove());
  const isShape = flow.isShape, isAgent = flow.kind === "agent", isObj = flow.kind === "artifact" && !isShape;
  const pop = document.createElement("div");
  pop.className = "lw-create-pop" + (reduceMotion() ? "" : " lw-pop-in");

  let body;
  if (isAgent) {
    // create-new OR place someone already in the cast
    body = `
      <div class="lw-mode" id="lwCMode">
        <button class="lw-mode-tab on" data-mode="new">Create new</button>
        <button class="lw-mode-tab" data-mode="cast">From cast</button>
      </div>
      <div id="lwCNew">
        <input class="sc-name" id="lwCName" placeholder="Name (optional)" autocomplete="off">
        <div class="sc-label">Who are they?</div>
        <textarea class="sc-input" id="lwCBrief" rows="2" placeholder="a cautious accountant who loves poker…"></textarea>
        <div class="sc-label">Face</div>
        <div class="lw-figpalette" id="lwCFig">${lwFigPaletteHtml(flow)}</div>
        ${lwDnaHtml(flow)}
      </div>
      <div id="lwCCast" hidden><div class="lw-cast-list" id="lwCCastList"><p class="dim">loading cast…</p></div></div>`;
  } else if (isObj) {
    if (!flow.build) flow.build = { type: "", comps: {}, saveAs: "" };
    if (!flow.omode) flow.omode = "describe";
    body = `
      <div class="lw-mode" id="lwOMode">
        <button class="lw-mode-tab${flow.omode === "describe" ? " on" : ""}" data-omode="describe">Describe</button>
        <button class="lw-mode-tab${flow.omode === "custom" ? " on" : ""}" data-omode="custom">Custom</button>
        <button class="lw-mode-tab${flow.omode === "build" ? " on" : ""}" data-omode="build">Build</button>
      </div>
      <div id="lwODescribe"${flow.omode === "describe" ? "" : " hidden"}>
        <input class="sc-name" id="lwCName" placeholder="Name (optional)" autocomplete="off">
        <div class="sc-label">What is it?</div>
        <textarea class="sc-input" id="lwCBrief" rows="2" placeholder="a deck of cards; a key; a note…"></textarea>
        <div class="sc-label">Icon <span class="dim">how it looks on the canvas</span></div>
        <div class="lw-figpalette" id="lwCFig">${lwFigPaletteHtml(flow)}</div>
      </div>
      <div id="lwOCustom"${flow.omode === "custom" ? "" : " hidden"}>
        <div class="sc-label">Pick a type <span class="dim">shipped + your saved ones</span></div>
        <div class="lw-lib" id="lwLibList"><p class="dim">loading library…</p></div>
      </div>
      <div id="lwOBuild"${flow.omode === "build" ? "" : " hidden"}>${lwBuildHtml(flow)}</div>`;
  } else {   // shape — a collating sticker; always has slots, never a glyph
    body = `
      <input class="sc-name" id="lwCName" placeholder="Name (optional)" autocomplete="off">
      <div class="sc-label">What is it? <span class="dim">(optional)</span></div>
      <textarea class="sc-input" id="lwCBrief" rows="2" placeholder="a poker table; a meeting circle…"></textarea>
      <div class="sc-label">Slots <span class="dim">how many agents can gather</span></div>
      <div class="lw-slots"><input type="range" id="lwCSlots" min="1" max="8" step="1" value="${flow.seats}">
        <b id="lwCSlotsN">${flow.seats}</b></div>
      <div class="sc-label">Shape</div>
      <div class="sc-row" id="lwCShape">
        <button class="sc-chip${flow.shape === "circle" ? " on" : ""}" data-shape="circle">◯ Circle</button>
        <button class="sc-chip${flow.shape === "rect" ? " on" : ""}" data-shape="rect">▭ Rect</button>
        <button class="sc-chip${flow.shape === "path" ? " on" : ""}" data-shape="draw">${flow.shape === "path" ? "✎ Custom ✓" : "✎ Draw"}</button>
      </div>`;
  }

  pop.innerHTML = `
    <div class="lw-pop-head" id="lwPopHead">
      <span class="lw-pop-grip" aria-hidden="true"></span>
      <span class="lw-pop-title">${escapeHtml(isShape ? "New shape" : isObj ? "New object" : "New agent")}</span>
      <button class="lw-pop-x" id="lwCX" title="Close (Esc)" aria-label="Close"><svg viewBox="0 0 24 24" width="13" height="13"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg></button>
    </div>
    ${body}
    <div class="sc-actions">
      <button class="sc-ctl primary" id="lwCGo">Create</button>
      <button class="sc-ctl" id="lwCCancel">Cancel</button>
    </div>
    <div class="lw-pop-hint">↵ create · esc cancel · drag the header to move</div>`;
  overlay.appendChild(pop);
  flow.pop = pop;
  lwPositionOverlayAt(pop, flow.world, 18);
  lwMakeDraggable(pop, pop.querySelector("#lwPopHead"), flow);

  const wireFig = () => pop.querySelectorAll("#lwCFig [data-fig]").forEach((b) =>
    b.addEventListener("click", () => {
      flow.figure = b.dataset.fig;
      pop.querySelectorAll("#lwCFig [data-fig]").forEach((x) => x.classList.toggle("on", x === b));
    }));
  wireFig();

  const nameEl = pop.querySelector("#lwCName");
  if (nameEl) {
    nameEl.focus();
    nameEl.addEventListener("input", (e) => {
      flow.name = e.target.value;
      if (isAgent) { const box = pop.querySelector("#lwCFig"); if (box) { box.innerHTML = lwFigPaletteHtml(flow); wireFig(); } }
    });
  }
  const briefEl = pop.querySelector("#lwCBrief");
  if (briefEl) briefEl.addEventListener("input", (e) => { flow.brief = e.target.value; });

  // agent: the mind it possesses + the base-DNA questions
  pop.querySelectorAll("#lwCMind [data-mind]").forEach((b) => b.addEventListener("click", () => {
    flow.model = b.dataset.mind;
    pop.querySelectorAll("#lwCMind [data-mind]").forEach((x) => x.classList.toggle("on", x === b));
  }));
  pop.querySelectorAll("#lwCDna [data-trait]").forEach((b) => b.addEventListener("click", () => {
    const tr = b.dataset.trait;
    flow.dials[tr] = Number(b.dataset.val);
    pop.querySelectorAll(`#lwCDna [data-trait="${tr}"]`).forEach((x) => x.classList.toggle("on", x === b));
  }));
  pop.querySelectorAll("#lwCMotive [data-drive]").forEach((b) => b.addEventListener("click", () => {
    flow.drive = flow.drive === b.dataset.drive ? "" : b.dataset.drive;      // one motive, toggleable
    pop.querySelectorAll("#lwCMotive [data-drive]").forEach((x) => x.classList.toggle("on", x.dataset.drive === flow.drive));
  }));

  // shape: slots slider + shape chooser
  const slots = pop.querySelector("#lwCSlots");
  if (slots) slots.addEventListener("input", (e) => { flow.seats = Number(e.target.value); const n = pop.querySelector("#lwCSlotsN"); if (n) n.textContent = flow.seats; });
  pop.querySelectorAll("[data-shape]").forEach((b) => b.addEventListener("click", () => {
    const s = b.dataset.shape;
    if (s === "draw") { lwBeginPathDraw(); return; }   // enter the paint-a-shape mode
    flow.shape = s; flow.path = []; flow.pathPts = [];
    lwSyncShapeChips();
  }));

  // agent: switch between Create-new and From-cast
  pop.querySelectorAll("#lwCMode [data-mode]").forEach((b) => b.addEventListener("click", () => {
    const mode = b.dataset.mode; flow.mode = mode;
    pop.querySelectorAll("#lwCMode [data-mode]").forEach((x) => x.classList.toggle("on", x === b));
    const nw = pop.querySelector("#lwCNew"), ct = pop.querySelector("#lwCCast");
    if (nw) nw.hidden = mode !== "new";
    if (ct) ct.hidden = mode !== "cast";
    pop.querySelector("#lwCGo").style.display = mode === "cast" ? "none" : "";
    if (mode === "cast") lwFillCast(pop);
  }));

  // object create: Describe / Custom / Build tabs + the generic builder
  pop.querySelectorAll("#lwOMode [data-omode]").forEach((b) => b.addEventListener("click", () => {
    flow.omode = b.dataset.omode;
    pop.querySelectorAll("#lwOMode [data-omode]").forEach((x) => x.classList.toggle("on", x === b));
    for (const [id, m] of [["lwODescribe", "describe"], ["lwOCustom", "custom"], ["lwOBuild", "build"]]) {
      const el = pop.querySelector("#" + id); if (el) el.hidden = flow.omode !== m;
    }
    if (flow.omode === "custom") lwLoadLib(flow);
  }));
  pop.querySelectorAll("#lwOBuild [data-comp]").forEach((cb) => cb.addEventListener("change", () => {
    const kind = cb.dataset.comp;
    if (cb.checked) flow.build.comps[kind] = flow.build.comps[kind] || {};
    else delete flow.build.comps[kind];
    const param = pop.querySelector(`#lwOBuild [data-param^="${kind}:"]`);
    if (param) {
      param.disabled = !cb.checked;
      if (cb.checked) { const [k, key] = param.dataset.param.split(":"); flow.build.comps[k][key] = param.type === "number" ? Number(param.value) : param.value; }
    }
  }));
  pop.querySelectorAll("#lwOBuild [data-param]").forEach((el) => el.addEventListener("input", () => {
    const [kind, key] = el.dataset.param.split(":");
    if (flow.build.comps[kind]) flow.build.comps[kind][key] = el.type === "number" ? Number(el.value) : el.value;
  }));
  const bType = pop.querySelector("#lwBType");
  if (bType) bType.addEventListener("input", (e) => { flow.build.type = e.target.value; });
  const bSaveName = pop.querySelector("#lwBSaveName"), bSaveOn = pop.querySelector("#lwBSaveOn");
  const syncSave = () => { flow.build.saveAs = (bSaveOn && bSaveOn.checked) ? (bSaveName ? bSaveName.value : "") : ""; };
  if (bSaveOn) bSaveOn.addEventListener("change", syncSave);
  if (bSaveName) bSaveName.addEventListener("input", syncSave);
  if (flow.omode === "custom") lwLoadLib(flow);       // preload if reopened on the Custom tab

  pop.querySelector("#lwCX").addEventListener("click", lwCancelCreate);
  pop.querySelector("#lwCCancel").addEventListener("click", lwCancelCreate);
  pop.querySelector("#lwCGo").addEventListener("click", () => lwDoCreate(pop));
  pop.addEventListener("keydown", (e) => {   // Enter submits, except inside the multi-line brief
    if (e.key === "Enter" && e.target.id !== "lwCBrief") { e.preventDefault(); lwDoCreate(pop); }
  });
}

// Place an agent that already exists in the world's cast, instead of authoring a new one.
async function lwFillCast(pop) {
  const box = pop.querySelector("#lwCCastList"); if (!box) return;
  let agents = [];
  try { agents = (await api(`/api/lw/${lwWorldId}`)).agents || []; }
  catch (e) { box.innerHTML = `<p class="dim">Could not load the cast.</p>`; return; }
  const here = new Set(((lwRoom && lwRoom.agents) || []).map((a) => String(a.id)));
  const avail = agents.filter((a) => !here.has(String(a.id)));
  if (!avail.length) { box.innerHTML = `<p class="dim">Everyone is already in this scene.</p>`; return; }
  box.innerHTML = avail.map((a) => {
    const seed = lwAvatarSeed({ name: a.name, id: a.id, figure: a.figure });
    return `<button class="lw-cast-item" data-cast="${escapeHtml(String(a.id))}">
      <img alt="" src="${lwSvgUri(lwAvatarSvg(seed, 30))}"><span>${escapeHtml(a.name || "someone")}</span></button>`;
  }).join("");
  box.querySelectorAll("[data-cast]").forEach((b) => b.addEventListener("click", () => lwPlaceExisting(Number(b.dataset.cast))));
}
async function lwPlaceExisting(hid) {
  const flow = lwCreateFlow; if (!flow || flow.busy) return;
  flow.busy = true;
  try {
    await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/seat?human_id=${encodeURIComponent(hid)}`, { method: "POST" });
    await api(`/api/lw/${lwWorldId}/pos`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: hid, x: flow.world.x, y: flow.world.y }) });
    lwTool = "select"; lwCleanupCreate(); sdFlash(); await lwReloadRoom();
  } catch (e) { toast(`Could not add them: ${e.message}`); flow.busy = false; }
}
function lwSyncShapeChips() {
  const flow = lwCreateFlow; if (!flow || !flow.pop) return;
  const map = { circle: "circle", rect: "rect", path: "draw" };
  flow.pop.querySelectorAll("[data-shape]").forEach((x) => x.classList.toggle("on", map[flow.shape] === x.dataset.shape));
  const draw = flow.pop.querySelector('[data-shape="draw"]');
  if (draw) draw.textContent = flow.shape === "path" ? "✎ Custom ✓" : "✎ Draw";
}

// ---- paint-a-shape: click points on the canvas to outline a collating table ---
function lwBeginPathDraw() {
  const flow = lwCreateFlow; if (!flow || !lwKonva) return;
  flow.drawing = true; flow.pathPts = [];
  if (flow.pop) flow.pop.style.display = "none";        // hide the dialog while painting
  lwShowDrawBanner();
  flow.preview = new Konva.Group({ x: flow.world.x, y: flow.world.y, listening: false, name: "pathPreview" });
  flow.previewLine = new Konva.Line({ points: [], closed: false, stroke: "#2E6E5B", strokeWidth: 2, dash: [5, 4], fill: "rgba(46,110,91,.10)" });
  flow.preview.add(flow.previewLine);
  lwKonva.worldLayer.add(flow.preview);
  lwSetCursor("crosshair");
}
function lwPathAddPoint() {
  const flow = lwCreateFlow; if (!flow || !flow.drawing) return;
  const w = lwPointerWorld(); if (!w) return;
  flow.pathPts.push([w.x - flow.world.x, w.y - flow.world.y]);   // store relative to the token centre
  lwDrawPreview();
}
function lwDrawPreview() {
  const flow = lwCreateFlow; if (!flow || !flow.preview) return;
  flow.previewLine.points(flow.pathPts.flatMap((p) => p));
  flow.previewLine.closed(flow.pathPts.length >= 3);
  flow.preview.find(".ppt").forEach((n) => n.destroy());
  flow.pathPts.forEach((p) => flow.preview.add(new Konva.Circle({ x: p[0], y: p[1], radius: 3.5, fill: "#2E6E5B", name: "ppt", listening: false })));
  lwKonva.worldLayer.batchDraw();
}
function lwFinishPathDraw() {
  const flow = lwCreateFlow; if (!flow || !flow.drawing) return;
  flow.drawing = false;
  // the finishing double-click lands two coincident clicks — collapse near-duplicates
  const s = (lwKonva && lwKonva.stage.scaleX()) || 1, eps = 6 / s, pts = [];
  for (const p of flow.pathPts) {
    const last = pts[pts.length - 1];
    if (!last || Math.hypot(p[0] - last[0], p[1] - last[1]) > eps) pts.push(p);
  }
  if (pts.length >= 3) { flow.path = pts; flow.shape = "path"; }
  else { flow.path = []; flow.shape = "circle"; toast("A shape needs at least 3 points — kept it a circle."); }
  if (flow.preview) { flow.preview.destroy(); flow.preview = null; }
  lwHideDrawBanner();
  if (flow.pop) flow.pop.style.display = "";
  lwSyncShapeChips();
  lwSetCursor(lwToolCursor(lwTool));
  lwKonva.worldLayer.batchDraw();
}
function lwCancelPathDraw() {
  const flow = lwCreateFlow; if (!flow) return;
  flow.drawing = false;
  flow.shape = (flow.path && flow.path.length >= 3) ? "path" : "circle";
  if (flow.preview) { flow.preview.destroy(); flow.preview = null; }
  lwHideDrawBanner();
  if (flow.pop) flow.pop.style.display = "";
  lwSyncShapeChips();
  lwSetCursor(lwToolCursor(lwTool));
  if (lwKonva) lwKonva.worldLayer.batchDraw();
}
function lwShowDrawBanner() {
  const overlay = $("#lwOverlay"); if (!overlay) return;
  lwHideDrawBanner();
  const b = document.createElement("div");
  b.className = "lw-draw-banner"; b.id = "lwDrawBanner";
  b.innerHTML = `<span class="lw-draw-tip">✎ Click to drop points · double-click to finish</span>
    <button class="sc-ctl primary" id="lwDrawDone">Finish</button>
    <button class="sc-ctl" id="lwDrawCancel">Cancel</button>`;
  overlay.appendChild(b);
  b.querySelector("#lwDrawDone").addEventListener("click", lwFinishPathDraw);
  b.querySelector("#lwDrawCancel").addEventListener("click", lwCancelPathDraw);
}
function lwHideDrawBanner() { const b = $("#lwDrawBanner"); if (b) b.remove(); }

function lwCreatedId(resp, keys) {
  if (!resp) return null;
  for (const k of keys) if (resp[k] && resp[k].id != null) return resp[k].id;
  return resp.id != null ? resp.id : null;
}
async function lwDoCreate(pop) {
  const flow = lwCreateFlow;
  if (!flow || flow.busy) return;                 // single-flight: one call, not three
  if (flow.mode === "cast") return;               // "From cast" places via lwPlaceExisting, not Create
  flow.busy = true;
  const go = pop.querySelector("#lwCGo");
  if (go) { go.disabled = true; go.textContent = "creating…"; }
  lwSetPendingLabel(flow.shimmer, "creating…", true);
  try {
    let newId = null;
    if (flow.kind === "agent") {
      const drives = flow.drive ? { [flow.drive]: 0.9 } : {};
      const r = await api(`/api/lw/${lwWorldId}/human`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: (flow.name || "").trim(), brief: (flow.brief || "").trim(), figure: flow.figure,
          model: flow.model || "", dials: flow.dials || {}, drives }) });
      newId = lwCreatedId(r, ["human", "person", "agent"]);
      if (newId != null)
        await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/seat?human_id=${encodeURIComponent(newId)}`, { method: "POST" });
    } else {
      const shape = (flow.path && flow.path.length >= 3) ? "path" : (flow.shape === "rect" ? "rect" : "circle");
      const name = (flow.name || "").trim();
      let payload;
      if (!flow.isShape && flow.omode === "custom") {
        if (!flow.libType) throw new Error("pick a type");
        payload = { type: flow.libType, name, figure: flow.figure };
      } else if (!flow.isShape && flow.omode === "build") {
        const spec = lwBuildSpec(flow);
        if (!spec) throw new Error("pick at least one component");
        payload = { spec, save_as: flow.build.saveAs || "", name: name || spec.type, figure: flow.figure };
      } else {
        payload = { name, brief: (flow.brief || "").trim(), figure: flow.figure,
          slots: flow.seats || 0, shape, path: shape === "path" ? flow.path : [] };
      }
      const r = await api(`/api/lw/${lwWorldId}/artifact`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload) });
      newId = lwCreatedId(r, ["artifact", "prop", "object"]);
      if (newId != null)
        await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/place?artifact_id=${encodeURIComponent(newId)}`, { method: "POST" });
    }
    if (newId != null)
      await api(`/api/lw/${lwWorldId}/pos`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: newId, x: flow.world.x, y: flow.world.y }) });
    lwCleanupCreate();
    lwSetTool("select");      // one-shot: apply the tool now, so a failed reload can't leave it stuck
    await lwReloadRoom();
  } catch (e) {
    toast(`Could not create it: ${e.message}`);
    flow.busy = false;
    if (go) { go.disabled = false; go.textContent = "Create"; }
    lwSetPendingLabel(flow.shimmer, flow.kind === "artifact" ? "▢" : "＋", false);
  }
}
function lwCleanupCreate() {
  const flow = lwCreateFlow; if (!flow) return;
  if (flow.anim) flow.anim.stop();
  if (flow.shimmer) flow.shimmer.destroy();
  if (flow.preview) flow.preview.destroy();
  lwHideDrawBanner();
  const overlay = $("#lwOverlay"); if (overlay) overlay.querySelectorAll(".lw-create-pop").forEach((n) => n.remove());
  lwCreateFlow = null;
  if (lwKonva) lwKonva.worldLayer.batchDraw();
}
function lwCancelCreate() { lwCleanupCreate(); lwSetTool("select"); }   // dismiss → pointer returns to drag

// ---- right-click menus (labels via studioMenu's createElement+textContent) --
function lwFloorMenu(evt, world) {
  studioMenu(evt.clientX, evt.clientY, [
    { label: "＋ New agent here", act: () => lwStartCreate("agent", world) },
    { label: "＋ New object here", act: () => lwStartCreate("artifact", world) },
    { label: "＋ New shape here", act: () => lwStartCreate("shape", world) },
    { sep: true },
    { label: "Seat an existing agent", act: lwOpenSeatPicker },
    { label: "Place an existing object", act: lwOpenPlacePicker },
    { label: "Select everything", act: lwSelAll },
    { sep: true },
    { label: "▶ Run one beat", act: lwPlayRound },
  ]);
}
function lwAgentMenu(evt, a) {
  const items = [{ label: `Open ${a.name || "them"} ⤢`, act: () => openAgentPage(a.id) }];
  const entry = lwKonva && lwKonva.agents.get(String(a.id));
  if (entry && entry.seat)
    items.push({ label: `Unseat ${a.name || "them"}`, act: () => lwUnseatAgent(a, entry.seat.propId) });
  const deck = findDeck(lwRoom);
  if (deck) items.push({ sep: true }, { label: `Deal a card to ${a.name || "them"}`, act: () => lwActOne(a.id, "draw", deck.id) });
  studioMenu(evt.clientX, evt.clientY, items);
}
async function lwUnseatAgent(a, propId) {
  try { await api(`/api/lw/${lwWorldId}/artifact/${propId}/unseat?human_id=${encodeURIComponent(a.id)}`, { method: "POST" }); await lwReloadRoom(); }
  catch (e) { toast(`Could not unseat: ${e.message}`); }
}
function lwPropMenu(evt, p) {
  studioMenu(evt.clientX, evt.clientY, [{ label: `Peek ${p.name || "object"}`, act: () => lwOpenArtifactPeek(p.id) }]);
}

// ---- speech bubbles: animate the round log over the acting agent's token ----
async function lwPlayBubbles(lines) {
  if (!lwKonva || !lines || !lines.length) return;
  const overlay = $("#lwOverlay"); if (!overlay) return;
  const reduce = reduceMotion();
  for (const l of lines) {
    if (!lwKonva) return;                 // the room may close or reload mid-playback
    if (l.who == null || !l.text) continue;
    const entry = lwKonva.agents.get(String(l.who));
    if (!entry) continue;
    const el = document.createElement("div");
    el.className = "lw-bubble" + ((l.tier === 2 || l.billed) ? " lw-bubble-thought" : "");
    el.textContent = String(l.text);   // free text via textContent — never innerHTML
    overlay.appendChild(el);
    const abs = entry.node.getAbsolutePosition();
    el.style.left = abs.x + "px"; el.style.top = abs.y + "px";
    if (reduce) {
      el.classList.add("in");
      await new Promise((res) => setTimeout(res, 550));
      el.remove();
    } else {
      requestAnimationFrame(() => el.classList.add("in"));
      await new Promise((res) => setTimeout(res, 1400));
      el.classList.remove("in");
      await new Promise((res) => setTimeout(res, 320));
      el.remove();
    }
  }
}

// Cost is visible: a billed / tier-2 line carries a 💭 thought marker; a free
// deterministic reflex is labelled as such. Only unseen lines animate in.
function lwLogHtml(log, prevSeen) {
  if (!log || !log.length)
    return `<div class="lw-log-empty">The ticker is quiet. Step the scene to stir them.</div>`;
  let newIdx = 0;
  return log.map((l) => {
    const isNew = !prevSeen.has(String(l.n));
    const thought = l.tier === 2 || !!l.billed;
    const delay = isNew ? Math.min(newIdx++, 8) * 55 : 0;
    return `<div class="slog-row lw-slog${isNew ? " lw-new" : ""}${thought ? " lw-thought" : ""}"${
      isNew ? ` style="animation-delay:${delay}ms"` : ""}>
      <span class="slog-kind">${escapeHtml(String(l.kind || ""))}</span>
      <span class="slog-text">${l.who != null ? `<b class="lw-who">${escapeHtml(lwNameOf(l.who))}</b> ` : ""}${escapeHtml(String(l.text || ""))}</span>
      ${thought
        ? `<span class="lw-thought-tag" title="a model actually thought — this call was billed">💭 thought</span>`
        : `<span class="lw-reflex-tag" title="a free deterministic reflex — no tokens spent">reflex</span>`}
    </div>`;
  }).join("");
}

/** A habit's trigger is a DICT of the fields it matches on. `String(dict)` is
 * "[object Object]", which is what every habit row has said until now. */
function lwHabitWhen(when) {
  if (!when || typeof when !== "object") return escapeHtml(String(when ?? "anything"));
  const parts = Object.entries(when).filter(([, v]) => v !== null && v !== "")
    .map(([k, v]) => `${escapeHtml(k)}=${escapeHtml(String(v))}`);
  return parts.join(" · ") || "anything";
}

function paintLwLive() {
  const b = $("#lwLive");
  if (!b) return;
  b.classList.toggle("on", lwLive);
  b.setAttribute("aria-pressed", lwLive ? "true" : "false");
  b.textContent = lwLive ? "🧠 Live — agents think (costs tokens)" : "💤 Deterministic (free)";
  b.title = lwLive
    ? "Live: each act and round asks real models to think. This spends tokens — every billed thought shows in the ticker."
    : "Deterministic: free, reproducible reflexes, no tokens. Flip to Live to spend tokens on real thinking.";
}

function paintLwTau() {
  const m = $("#lwTau");
  if (!m) return;
  const calls = lwBilledCount(lwRoom);
  m.textContent = `τ ${sdTau}` + (calls ? ` · ${calls} thought${calls === 1 ? "" : "s"}` : "");
}

// ---- run one beat --------------------------------------------------------
async function lwPlayRound() {
  const prevSeen = new Set(lwSeenLog);   // capture before the repaint marks all lines seen
  try {
    const r = await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/round${lwLiveQ()}`, { method: "POST" });
    if (r && r.world_tau != null) sdTau = r.world_tau;
    const room = r.room || (await api(`/api/lw/${lwWorldId}/room/${lwRoomId}`)).room;
    lwRenderRoom(room);
    sdFlash();
    // Cloud bubbles over each acting agent, for the lines this round added (fire-and-forget).
    lwPlayBubbles((room.log || []).filter((l) => !prevSeen.has(String(l.n)))).catch(() => {});
  } catch (e) { toast(`Round failed: ${e.message}`); }
}

async function lwActOne(hid, verb, target, extra) {
  try {
    const body = Object.assign({ human_id: hid, verb, target }, extra || {});
    await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/act${lwLiveQ()}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    await renderRoomView();
  } catch (e) { toast(`Could not act: ${e.message}`); }
}


// Seat an agent: pick from the world's people not already in this room.
function lwOpenSeatPicker() {
  const box = $("#lwDetail");
  const agents = (lwWorld && lwWorld.agents) || [];
  const seated = new Set(((lwRoom && lwRoom.seats) || []).map((s) => String(lwHumanId(s))));
  const avail = agents.filter((a) => !seated.has(String(a.id)));
  box.hidden = false; box.className = "studio-detail";
  box.innerHTML = `
    <div class="sd-head"><div style="flex:1"><h3>Seat an agent</h3>
      <p class="sd-persona">place one of the world's people into this room</p></div>
      <button class="sd-close" id="lwSeatClose">✕</button></div>
    ${avail.length ? `<div class="seatpick-list">${avail.map((a) =>
      `<button class="seatpick-row" data-seat="${escapeHtml(String(a.id))}">
        <span class="fig-emblem" style="background:${sigil(a.name || "?", "anthropic")}"><span class="fig-initial">${escapeHtml((a.name || "?")[0] || "?")}</span></span>
        <span class="seatpick-name">${escapeHtml(a.name || "someone")}</span>
        <span class="seatpick-role">${escapeHtml((dominantWant(a.wants) || {}).name || "")}</span>
      </button>`).join("")}</div>`
      : `<p class="sc-hint">Everyone is already here. Create more people in the Agents tab.</p>`}`;
  $("#lwSeatClose").addEventListener("click", () => { box.hidden = true; });
  box.querySelectorAll("[data-seat]").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/seat?human_id=${encodeURIComponent(b.dataset.seat)}`, { method: "POST" });
        box.hidden = true; await loadWorld(); await renderRoomView();
      } catch (e) { toast(`Could not seat them: ${e.message}`); }
    }));
}

// Place an object: pick from the world's objects not already in this room.
function lwOpenPlacePicker() {
  const box = $("#lwDetail");
  const artifacts = (lwWorld && lwWorld.artifacts) || [];
  const placed = new Set(((lwRoom && lwRoom.props) || []).map((p) => String(p.id)));
  const avail = artifacts.filter((a) => !placed.has(String(a.id)));
  box.hidden = false; box.className = "studio-detail";
  box.innerHTML = `
    <div class="sd-head"><div style="flex:1"><h3>Place an object</h3>
      <p class="sd-persona">place one of the world's objects into this room</p></div>
      <button class="sd-close" id="lwPlaceClose">✕</button></div>
    ${avail.length ? `<div class="seatpick-list">${avail.map((a) =>
      `<button class="seatpick-row" data-place="${escapeHtml(String(a.id))}">
        <span class="lw-obj lw-obj-tile">${escapeHtml((a.name || "?")[0] || "?")}</span>
        <span class="seatpick-name">${escapeHtml(a.name || "object")}</span>
        <span class="seatpick-role">${escapeHtml(a.kind || "")}</span>
      </button>`).join("")}</div>`
      : `<p class="sc-hint">Every object is already placed. Create more in the Artifacts tab.</p>`}`;
  $("#lwPlaceClose").addEventListener("click", () => { box.hidden = true; });
  box.querySelectorAll("[data-place]").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/place?artifact_id=${encodeURIComponent(b.dataset.place)}`, { method: "POST" });
        box.hidden = true; await loadWorld(); await renderRoomView();
      } catch (e) { toast(`Could not place it: ${e.message}`); }
    }));
}

// ---- peek a person: the operator's drawer over their whole inner state ----
function lwMeter(label, v) {
  const p = lwPct(v);
  return `<div class="lw-meter-row">
    <span class="lw-meter-name">${escapeHtml(label)}</span>
    <span class="lw-meter-track"><i class="lw-meter-fill m-${escapeHtml(label)}" style="width:${p}%"></i></span>
    <span class="lw-meter-val">${p}</span></div>`;
}
function lwHandCardHtml(value) {
  if (!value) return `<span class="pcard back"><span class="pcard-weave"></span></span>`;
  const s = suitInfo(value.suit);
  const r = escapeHtml(String(value.rank ?? "?"));
  return `<span class="pcard up${s.red ? " red" : ""}">
    <span class="pc-c tl">${r}<b>${s.glyph}</b></span>
    <span class="pc-pip">${s.glyph}</span>
    <span class="pc-c br">${r}<b>${s.glyph}</b></span>
  </span>`;
}
// openPersonDrawer is retired. Its 340px drawer was the thing root asked to be rid of: two
// windows for one agent, a close button that scrolled out of reach, and the decision graph
// squeezed into a keyhole. Everything it rendered now lives on the agent's own page, and the
// helpers below are shared with it.
/** What this agent has learned to expect — the part that makes it faster next time.
 *
 * A signature is a coarse fingerprint of a situation ("http:505"), so the same lesson is
 * found again when the wording, the host and the day are all different. Confidence is the
 * share of times the conclusion preceded a good outcome, and anything under two agreeing
 * outcomes is not shown as knowledge at all — one coincidence is superstition. */
function lwAssocHtml(assoc) {
  const rows = Array.isArray(assoc) ? assoc : [];
  if (!rows.length) return "";
  return `<div class="sd-label">What it expects <span class="dim">recalled instantly, before it thinks — a hit costs nothing</span></div>
    <div class="lw-assoc">${rows.map((a) => `<div class="lw-as${a.confidence >= 0.5 ? " live" : " weak"}">
      <code class="lw-as-sig">${escapeHtml(a.sig)}</code>
      <span class="lw-as-says">${escapeHtml(a.says || "")}</span>
      <span class="lw-as-meta">${a.evidence < 2 ? "not yet trusted" : `${Math.round(a.confidence * 100)}% · ${a.evidence} times`}</span>
    </div>`).join("")}</div>`;
}

/** The decision DAG, laid out.
 *
 * A chronological list answers "what happened"; this answers "what led to what", which is
 * the question a tree is for. Layered by depth from the roots (a node sits one level below
 * its deepest parent), so an edge always points downward and the eye can follow a lineage
 * without untangling anything.
 *
 * Hand-rolled SVG, no library: the platform loads no CDN and takes no build step, and a
 * layered DAG of a couple of hundred nodes is a morning's arithmetic rather than a reason to
 * add a dependency to a self-hosted app.
 *
 * CANON is drawn bigger — it is a pivot because things turned on it. STALE is dimmed: it
 * rested on something later shown to be wrong, and it is kept rather than deleted because
 * the mistake is the interesting part. Outcome colours the fill: green held up, red did not,
 * hollow is still open.
 */
function lwLayoutDag(nodes) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const depth = new Map();
  // Depth = one below the deepest parent. Iterative rather than recursive, so a cycle from
  // corrupted data cannot blow the stack — it just stops improving.
  for (let pass = 0; pass < 12; pass++) {
    let moved = false;
    for (const n of nodes) {
      const ps = (n.parents || []).filter((p) => byId.has(p));
      const d = ps.length ? Math.max(...ps.map((p) => (depth.get(p) ?? 0) + 1)) : 0;
      if (d !== (depth.get(n.id) ?? -1)) { depth.set(n.id, d); moved = true; }
    }
    if (!moved) break;
  }
  const rows = new Map();
  for (const n of nodes) {
    const d = depth.get(n.id) ?? 0;
    if (!rows.has(d)) rows.set(d, []);
    rows.get(d).push(n);
  }
  const COL = 132, ROW = 66, PAD = 26;
  const pos = new Map();
  for (const [d, row] of [...rows.entries()].sort((a, b) => a[0] - b[0])) {
    row.forEach((n, i) => pos.set(n.id, { x: PAD + i * COL, y: PAD + d * ROW }));
  }
  const width = PAD * 2 + Math.max(1, ...[...rows.values()].map((r) => r.length)) * COL - (COL - 90);
  const height = PAD * 2 + rows.size * ROW;
  return { pos, width, height, depth };
}

// The nodes the drawer is currently showing. Held in memory rather than serialised into the
// page: a <script type="application/json"> block is RAW TEXT, so escaping it for safety
// corrupts the JSON and not escaping it is an injection — and neither is necessary when the
// renderer and the click handler are ten lines apart.
let lwDagRows = [];

function lwTreeHtml(nodes, canon) {
  const rows = Array.isArray(nodes) ? nodes.slice(-60) : [];
  lwDagRows = rows;
  if (!rows.length) return "";
  const canonN = rows.filter((n) => n.canon).length;
  const { pos, width, height } = lwLayoutDag(rows);
  const byId = new Map(rows.map((n) => [n.id, n]));
  const edges = rows.flatMap((n) => (n.parents || []).filter((p) => byId.has(p)).map((p) => {
    const a = pos.get(p), b = pos.get(n.id);
    const mid = (a.y + b.y) / 2;
    return `<path class="lw-dedge${byId.get(p).stale ? " stale" : ""}"
      d="M${a.x} ${a.y + 9} C${a.x} ${mid}, ${b.x} ${mid}, ${b.x} ${b.y - 9}"/>`;
  })).join("");
  const dots = rows.map((n) => {
    const q = pos.get(n.id);
    const label = (n.chose || n.understood || "").replace(/^build:\s*/, "");
    return `<g class="lw-dg o-${escapeHtml(n.outcome || "open")}${n.canon ? " canon" : ""}${n.stale ? " stale" : ""}"
        data-dnode="${n.id}" transform="translate(${q.x},${q.y})">
      <circle class="lw-ddot" r="${n.canon ? 9 : 6}"></circle>
      <text class="lw-dlabel" x="13" y="4">${escapeHtml(trim(label, 16))}</text>
    </g>`;
  }).join("");
  return `<div class="sd-label">How it got here
      <span class="dim">${rows.length} decisions · ${canonN} turned out to be pivots — click one</span></div>
    <div class="lw-dagwrap"><svg class="lw-dag" viewBox="0 0 ${width} ${height}"
      width="${width}" height="${height}" role="img" aria-label="decision graph">
      <g class="lw-dedges">${edges}</g>${dots}</svg></div>
    <div class="lw-dpanel" id="lwDPanel" hidden></div>`;
}

/** Clicking a node explains it: what arrived, what it made of it, and the causes we recorded
 * at that instant. Kept out of the graph itself — a DAG with paragraphs on it is unreadable,
 * and the whole reason to lay it out is to be able to scan the shape first. */
function lwWireDag(box) {
  const panel = box.querySelector("#lwDPanel");
  if (!panel) return;
  const byId = new Map(lwDagRows.map((n) => [n.id, n]));
  box.querySelectorAll("[data-dnode]").forEach((g) => g.addEventListener("click", () => {
    const n = byId.get(Number(g.dataset.dnode));
    if (!n) return;
    box.querySelectorAll("[data-dnode]").forEach((x) => x.classList.remove("sel"));
    g.classList.add("sel");
    const why = Object.entries(n.because || {}).filter(([, v]) => v !== "" && v !== null)
      .map(([k, v]) => `<span class="rp-lf">${escapeHtml(k)}=${escapeHtml(String(v))}</span>`).join(" ");
    panel.hidden = false;
    panel.innerHTML = `<div class="lw-dhead">
        <span class="lw-dmark">${n.canon ? "★ pivot" : n.stale ? "⚠ rested on something wrong" : "·"}</span>
        <span class="lw-dout">${escapeHtml(n.outcome || "still open")}</span>
        ${n.sig ? `<code class="lw-as-sig">${escapeHtml(n.sig)}</code>` : ""}
      </div>
      <div class="lw-dwhy">
        <div><b>saw</b> ${escapeHtml(n.saw || "")}</div>
        <div><b>made of it</b> ${escapeHtml(n.understood || "")}</div>
        <div><b>chose</b> ${escapeHtml(n.chose || "")}</div>
        <div>${why}</div>
        ${n.parents && n.parents.length ? `<div class="dim">after #${n.parents.join(", #")}</div>` : ""}
      </div>`;
  }));
}

/** The backend's own record of this agent. Root only — the server decides that, not this
 * function; if the field is absent, you are not root and there is nothing to hide badly. */
function lwAgentLogsHtml(rows) {
  if (!Array.isArray(rows) || !rows.length) return "";
  return `<div class="sd-label">Backend log <span class="dim">what the server recorded about them</span></div>
    <div class="lw-alogs">${rows.slice(-25).map((r) => {
      const when = r.ts ? new Date(r.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) : "";
      return `<div class="rp-actline ${r.level === "error" ? "errline" : r.level === "warn" ? "warnline" : ""}">`
        + `<span class="rp-actwhen">${escapeHtml(when)}</span>`
        + `<span class="rp-lcat">${escapeHtml(r.cat)}/${escapeHtml(r.event)}</span>`
        + `<span>${escapeHtml(String(r.msg || "").slice(0, 160))}</span></div>`;
    }).join("")}</div>`;
}

