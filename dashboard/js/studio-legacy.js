// studio-legacy.js — LEGACY QUARANTINE — the retired round-table Studio, Scenes and Artifact-shelf machinery. The shared helpers the live Studio reuses (sigil/figure language, studioMenu, chips) moved to js/lib.js; do not grow this file.
// Split from the old monolithic app.js (order preserved; classic scripts share one global scope; index.html defines load order).

// ============================================================================
// The Studio — where globally-persistent agents reside between jobs.
//
// Built from the round-table machinery being retired: positioned DOM nodes on a
// warm floor, state driven by a CSS class, no framework and no canvas. An agent
// is a CHARACTER standing in a room — it breathes when idle, glows pine when on a
// job, flushes brick when it needs you (brick means a human is involved here too),
// and carries a generated sigil so the same teammate is recognisable at a glance
// across projects. Positions are the viewer's arrangement, kept in localStorage.
// ============================================================================

let studioAgents = [];

function studioPositions() {
  try { return JSON.parse(localStorage.getItem("studio-pos") || "{}"); }
  catch { return {}; }
}
function saveStudioPos(id, x, y) {
  const p = studioPositions(); p[id] = { x, y };
  localStorage.setItem("studio-pos", JSON.stringify(p));
}

// The Studio is one section with three tabs (Agents / Scenes / Artifacts). This
// brings the shell on screen and hides every sibling; tab selection is separate.
function showStudioShell() {
  $("#home").hidden = true; $("main").hidden = true;
  for (const id of ["plan", "selfPage", "aboutPage", "lifeworld"]) { const e = $("#" + id); if (e) e.hidden = true; }
  $("#projectBar").hidden = true;
  $("#studio").hidden = false;
  currentProject = null;
}

let studioTab = "agents";

// The Studio IS the canvas now. This opens the one full-screen scene view. The old
// tabbed studio (showStudioShell / selectStudioTab, below) is retired but kept defined
// so the #studio section and its route/tab helpers stay present for existing tests.
async function openStudio(skipHash) {
  showLifeworld();
  if (!skipHash) setHash("#/studio");
  await lwEnterStudio();
}

// The bar carries per-tab actions (hire / new scene / new artifact, budget, meter,
// scene title). Show only the ones the active tab owns; each tab's own render pass
// fine-tunes the scene controls (lobby vs open table) afterward.
function setStudioBar(name) {
  $("#studioHire").hidden = name !== "agents";
  $("#studioBudget").hidden = name !== "agents";
  $("#artNew").hidden = name !== "artifacts";
  $("#sceneNew").hidden = name !== "scenes";
  $("#sceneTitle").hidden = name !== "scenes";
  if (name !== "scenes") { $("#sceneMeter").hidden = true; $("#sceneToLobby").hidden = true; }
}

function studioTabHash(name) {
  if (name === "artifacts") return "#/studio/artifacts";
  if (name === "scenes") return openSceneId ? `#/scenes/${openSceneId}` : "#/scenes";
  return "#/studio";
}

async function selectStudioTab(name, skipHash) {
  studioTab = name;
  document.querySelectorAll(".studio-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  $("#tabAgents").hidden = name !== "agents";
  $("#tabScenes").hidden = name !== "scenes";
  $("#tabArtifacts").hidden = name !== "artifacts";
  setStudioBar(name);
  if (!skipHash) setHash(studioTabHash(name));
  if (name === "agents") await renderStudio();
  else if (name === "scenes") await renderScenes();
  else if (name === "artifacts") await renderArtifactLib();
}

document.querySelectorAll(".studio-tab").forEach((b) =>
  b.addEventListener("click", () => selectStudioTab(b.dataset.tab)));

async function renderStudio() {
  const floor = $("#studioFloor");
  if (!floor || $("#studio").hidden) return;
  let d;
  try { d = await api("/api/home"); }
  catch (e) { floor.querySelector(".studio-empty").hidden = true; return; }
  studioAgents = d.agents || [];

  const budget = d.budget || {};
  const bEl = $("#studioBudget");
  if (bEl) bEl.textContent = budget.cap
    ? `${budget.spent}/${budget.cap} tokens today` : "";

  $("#studioEmpty").hidden = studioAgents.length > 0;
  if (!floor.dataset.ctxWired) {
    floor.addEventListener("contextmenu", (ev) => {
      if (ev.target.closest(".figure")) return;   // figures have their own menu
      studioFloorMenu(ev);
    });
    floor.dataset.ctxWired = "1";
  }

  const pos = studioPositions();
  // Keep the empty-state node; replace the figures.
  floor.querySelectorAll(".figure").forEach((n) => n.remove());
  studioAgents.forEach((a, i) => {
    const p = pos[a.id] || { x: 14 + (i % 5) * 17, y: 22 + Math.floor(i / 5) * 30 };
    const el = document.createElement("div");
    el.className = `figure mood-${a.mood}${a.on_project ? " on-job" : ""}`;
    el.style.left = p.x + "%"; el.style.top = p.y + "%";
    el.dataset.id = a.id;
    const done = a.lifetime_tasks || 0;
    el.innerHTML = `
      <div class="fig-aura"></div>
      <div class="fig-emblem prov-${escapeHtml(a.provider)}" style="background:${sigil(a.name, a.provider)}">
        <span class="fig-initial">${escapeHtml((a.name || "?")[0])}</span>
      </div>
      <div class="fig-name">${escapeHtml(a.name)}</div>
      <div class="fig-role">${escapeHtml(a.degree || "generalist")}</div>
      ${done ? `<div class="fig-shelf" title="${done} jobs, ${a.memory_chars} chars of memory">${
        "•".repeat(Math.min(5, Math.ceil(done / 3)))}</div>` : ""}`;
    makeDraggable(el, a.id);
    el.addEventListener("contextmenu", (ev) => studioAgentMenu(ev, a));
    el.addEventListener("click", (ev) => {
      if (el.dataset.dragged === "1") { el.dataset.dragged = ""; return; }
      openAgentDetail(a.id);
    });
    floor.appendChild(el);
  });
}

// Drag with Pointer Events (not HTML5 draggable — clunky for free 2D and poor on
// touch). Move with transform to avoid reflow; commit to left/top % on release.
function makeDraggable(el, id) {
  let sx, sy, ox, oy, moved;
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    el.setPointerCapture(e.pointerId);
    el.classList.add("dragging"); moved = false;
    sx = e.clientX; sy = e.clientY; ox = el.offsetLeft; oy = el.offsetTop;
  });
  el.addEventListener("pointermove", (e) => {
    if (!el.classList.contains("dragging")) return;
    const dx = e.clientX - sx, dy = e.clientY - sy;
    if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
    el.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
  });
  el.addEventListener("pointerup", (e) => {
    if (!el.classList.contains("dragging")) return;
    el.classList.remove("dragging");
    const floor = $("#studioFloor").getBoundingClientRect();
    const nx = Math.max(4, Math.min(96, ((el.offsetLeft + (e.clientX - sx)) / floor.width) * 100));
    const ny = Math.max(8, Math.min(94, ((el.offsetTop + (e.clientY - sy)) / floor.height) * 100));
    el.style.transform = ""; el.style.left = nx + "%"; el.style.top = ny + "%";
    if (moved) { el.dataset.dragged = "1"; saveStudioPos(id, nx, ny); }
  });
}

async function openAgentDetail(id) {
  const box = $("#studioDetail");
  box.hidden = false;
  box.innerHTML = `<p class="dim">loading…</p>`;
  let d;
  try { d = await api(`/api/home/${id}`); }
  catch (e) { box.innerHTML = `<p class="dim">could not load: ${escapeHtml(e.message)}</p>`; return; }
  const a = d.agent, mem = d.memory || {}, ev = d.evolution || [];
  const memText = Object.entries(mem).filter(([, v]) => (v || "").trim())
    .map(([k, v]) => `<div class="mem-sec"><b>${escapeHtml(k)}</b>${escapeHtml(v)}</div>`).join("")
    || `<p class="dim">No memory yet — it forms as they work.</p>`;
  box.className = "studio-detail";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem prov-${escapeHtml(a.provider)}" style="background:${sigil(a.name, a.provider)}">
        <span class="fig-initial">${escapeHtml(a.name[0])}</span></div>
      <div style="flex:1">
        <h3>${escapeHtml(a.name)}</h3>
        <p class="sd-persona" id="sdPersona" title="click to edit">${escapeHtml(a.persona || "click to give them a character")}</p>
      </div>
      <button class="sd-close" id="sdClose">✕</button>
    </div>
    <div class="sd-facts">
      <span class="sd-edit" id="sdDegree" title="click to change">${escapeHtml(a.degree || "generalist")}</span>
      <span class="sd-edit" id="sdModel" title="click to change">on ${escapeHtml(a.model || "role default")}${a.model_locked ? " 🔒" : ""}</span>
      <span>${a.lifetime_tasks} jobs · ${a.lifetime_accepted} accepted · ${a.lifetime_rework} reworked</span>
    </div>
    <div class="sd-dials"><div class="sd-label">Personality dials <span class="dim">(50 is neutral)</span></div>
      ${dialBankHtml(a.traits)}</div>
    <div class="sd-mem"><div class="sd-label">Memory</div>${memText}</div>
    ${ev.length ? `<div class="sd-evo"><div class="sd-label">Model history</div>${
      ev.map((e) => `<div class="evo-row">${e.direction === "up" ? "↑" : "↓"}
        ${escapeHtml(e.from_model)} → ${escapeHtml(e.to_model)}
        <span class="dim">${escapeHtml(e.reason)}</span></div>`).join("")}</div>` : ""}`;
  $("#sdClose").addEventListener("click", () => box.hidden = true);

  // Personality dials save on release via PATCH — a light write that does not
  // rebuild the whole card, so the slider you just dragged keeps focus.
  const dialTraits = { ...(a.traits || {}) };
  const dialBox = box.querySelector(".sd-dials");
  if (dialBox) wireDialBank(dialBox, dialTraits, async (traits) => {
    try {
      await api(`/api/home/${id}`, { method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ traits }) });
    } catch (e) { toast(`Could not save dials: ${e.message}`); }
  });

  // Everything editable in place — click the thing, a control replaces it.
  $("#sdPersona").addEventListener("click", () => {
    const cur = a.persona || "";
    const ta = document.createElement("textarea");
    ta.className = "sc-input"; ta.rows = 2; ta.value = cur;
    $("#sdPersona").replaceWith(ta); ta.focus();
    ta.addEventListener("blur", () => { if (ta.value !== cur) patchAgent(id, { persona: ta.value }); else openAgentDetail(id); });
  });
  $("#sdModel").addEventListener("click", () => {
    const sel = document.createElement("select");
    sel.className = "mgr-model";
    sel.innerHTML = TIERS.map((t) => `<option value="${t.id}"${t.id === a.model ? " selected" : ""}>${t.label} — ${t.note}</option>`).join("");
    $("#sdModel").replaceWith(sel); sel.focus();
    sel.addEventListener("change", () => patchAgent(id, { model: sel.value }));
  });
  $("#sdDegree").addEventListener("click", () => {
    const sel = document.createElement("select");
    sel.className = "mgr-model";
    sel.innerHTML = DISCIPLINES.map((x) => `<option${x === a.degree ? " selected" : ""}>${x}</option>`).join("");
    $("#sdDegree").replaceWith(sel); sel.focus();
    sel.addEventListener("change", () => patchAgent(id, { degree: sel.value }));
  });
}

// The controlled way to build a person — assembled inline, never in a browser
// dialog. A composer
// drawer where a character is ASSEMBLED from parts: a discipline, a model tier
// (shown as what it means, not a model id), temperament traits you toggle, an
// optional reference, and free elaboration. The persona string is composed live
// from those choices so you can see who you are making.
const DISCIPLINES = ["backend", "frontend", "design", "product", "research",
                     "qa", "writer", "law", "finance", "strategy"];
const TRAITS = ["confident", "meticulous", "skeptical", "warm", "terse", "bold",
                "cautious", "playful", "relentless", "diplomatic"];
const TIERS = [
  { id: "claude-haiku-4-5", label: "Quick", note: "fast & cheap" },
  { id: "claude-sonnet-5",  label: "Balanced", note: "the default" },
  { id: "claude-opus-4-8",  label: "Careful", note: "most capable" },
];

// The biological personality dials live on the agent — set at hire and edited in
// the detail view. Each is 0..100, neutral at 50. They ride the same slider bank
// look the scenes composer used to carry.
const PERSONALITY_DIALS = ["willpower", "risk_appetite", "addiction_proneness",
                           "composure", "sociability", "empathy", "curiosity"];

// A 0..100 slider bank over PERSONALITY_DIALS, pre-loaded from an agent's traits
// object (missing dials default to 50). Names are static, so escapeHtml on the
// human label is belt-and-braces.
function dialBankHtml(traits) {
  traits = traits || {};
  return `<div class="eq-bank">${PERSONALITY_DIALS.map((key) => {
    const raw = Number(traits[key]);
    const val = Number.isFinite(raw) ? raw : 50;
    const label = key.replace(/_/g, " ");
    return `<div class="eq-row">
      <span class="eq-name">${escapeHtml(label)}</span>
      <input class="eq-slider" type="range" min="0" max="100" step="1"
             value="${val}" data-dial="${key}" aria-label="${escapeHtml(label)}">
      <span class="eq-val" data-dialval="${key}">${val}</span>
    </div>`;
  }).join("")}</div>`;
}

// Wire a dial bank's sliders to write into `traits` and update the readout live.
// onCommit (optional) fires on release, for persisting an existing agent's change.
function wireDialBank(container, traits, onCommit) {
  container.querySelectorAll(".eq-slider[data-dial]").forEach((r) => {
    r.addEventListener("input", (e) => {
      const key = e.target.dataset.dial;
      const v = Number(e.target.value);
      traits[key] = v;
      const out = container.querySelector(`[data-dialval="${key}"]`);
      if (out) out.textContent = String(v);
    });
    if (onCommit) r.addEventListener("change", () => onCommit(traits));
  });
}

// draft holds the composer state; null when the drawer is closed.
let hireDraft = null;

function composePersona(d) {
  const bits = [];
  if (d.traits.length) bits.push(d.traits.join(", "));
  if (d.reference.trim()) bits.push(`in the mould of ${d.reference.trim()}`);
  const lead = bits.join(", ");
  return [lead ? lead.charAt(0).toUpperCase() + lead.slice(1) + "." : "",
          d.elaboration.trim()].filter(Boolean).join(" ");
}

function renderHirePanel() {
  const box = $("#studioDetail");
  const d = hireDraft;
  box.hidden = false;
  box.className = "studio-detail composing";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem" style="background:${sigil(d.name || "New", "anthropic")}">
        <span class="fig-initial">${escapeHtml((d.name || "?")[0])}</span></div>
      <div style="flex:1">
        <input class="sc-name" id="scName" placeholder="Name (or leave blank)"
               value="${escapeHtml(d.name)}" autocomplete="off">
        <p class="sc-preview">${escapeHtml(composePersona(d) || "a blank slate — add a few traits")}</p>
      </div>
      <button class="sd-close" id="scClose">✕</button>
    </div>

    <div class="sc-label">Discipline</div>
    <div class="sc-row">${DISCIPLINES.map((x) => chip(x, d.degree === x, `data-deg="${x}"`)).join("")}</div>

    <div class="sc-label">How careful should they be?</div>
    <div class="seg sc-seg">${TIERS.map((t) =>
      `<label class="seg-opt"><input type="radio" name="sctier" ${t.id === d.model ? "checked" : ""}
        data-model="${t.id}"><span>${t.label}</span></label>`).join("")}</div>
    <p class="sc-hint" id="scTierNote">${escapeHtml((TIERS.find((t) => t.id === d.model) || {}).note || "")}</p>

    <div class="sc-label">Temperament</div>
    <div class="sc-row">${TRAITS.map((x) => chip(x, d.traits.includes(x), `data-trait="${x}"`)).join("")}</div>

    <div class="sc-label">A reference, if it helps <span class="dim">(optional)</span></div>
    <input class="sc-input" id="scRef" placeholder="e.g. Mike Ross from Suits"
           value="${escapeHtml(d.reference)}" autocomplete="off">

    <div class="sc-label">Anything else <span class="dim">(optional)</span></div>
    <textarea class="sc-input" id="scElab" rows="2"
              placeholder="a confident junior lawyer who has never lost a case…">${escapeHtml(d.elaboration)}</textarea>

    <div class="sc-label">Personality dials <span class="dim">(50 is neutral)</span></div>
    ${dialBankHtml(d.dials)}

    <div class="sc-actions">
      <button class="primary" id="scHire">Bring them in</button>
    </div>`;

  const rerender = () => renderHirePanel();
  $("#scName").addEventListener("input", (e) => { d.name = e.target.value; d._nameEdited = true;
    box.querySelector(".sc-preview").textContent = composePersona(d) || "a blank slate — add a few traits"; });
  $("#scRef").addEventListener("input", (e) => { d.reference = e.target.value; });
  $("#scElab").addEventListener("input", (e) => { d.elaboration = e.target.value; });
  box.querySelectorAll("[data-deg]").forEach((b) =>
    b.addEventListener("click", () => { d.degree = d.degree === b.dataset.deg ? "" : b.dataset.deg; rerender(); }));
  box.querySelectorAll("[data-trait]").forEach((b) =>
    b.addEventListener("click", () => {
      const t = b.dataset.trait;
      d.traits = d.traits.includes(t) ? d.traits.filter((x) => x !== t) : [...d.traits, t];
      rerender();
    }));
  box.querySelectorAll("[data-model]").forEach((r) =>
    r.addEventListener("change", () => { d.model = r.dataset.model; rerender(); }));
  // Dials write straight into the draft; no re-render needed on drag.
  wireDialBank(box, d.dials);
  $("#scClose").addEventListener("click", () => { hireDraft = null; box.hidden = true; });
  $("#scHire").addEventListener("click", doHire);
}

function openHirePanel(at) {
  hireDraft = { name: "", degree: "", model: "claude-sonnet-5", traits: [],
                reference: "", elaboration: "", dials: {}, at: at || null };
  renderHirePanel();
}

async function doHire() {
  const d = hireDraft;
  try {
    const r = await api("/api/home", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: d.name.trim(), degree: d.degree,
                             model: d.model, persona: composePersona(d),
                             traits: d.dials }) });
    // Drop the new figure where the world was right-clicked, if anywhere.
    if (d.at && r.agent) saveStudioPos(r.agent.id, d.at.x, d.at.y);
    hireDraft = null;
    $("#studioDetail").hidden = true;
    await renderStudio();
  } catch (e) { toast(`Could not hire: ${e.message}`); }
}

// A patch to one agent, re-rendering both the detail card and the floor.
async function patchAgent(id, fields) {
  try {
    await api(`/api/home/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields) });
    await renderStudio();
    await openAgentDetail(id);
  } catch (e) { toast(`Could not update: ${e.message}`); }
}

$("#studioBack") && $("#studioBack").addEventListener("click", () => showHome());
$("#studioHire") && $("#studioHire").addEventListener("click", () => openHirePanel());
$("#studioHire2") && $("#studioHire2").addEventListener("click", () => openHirePanel());

// The world is right-clickable. A menu on empty floor to bring someone in where
// you clicked or to tidy the room; a menu on a person to talk, edit or bench them
// (talk/scenes arrive in the next sprint and are shown as what is coming).
function studioFloorMenu(ev) {
  ev.preventDefault();
  const floor = $("#studioFloor").getBoundingClientRect();
  const at = { x: ((ev.clientX - floor.left) / floor.width) * 100,
               y: ((ev.clientY - floor.top) / floor.height) * 100 };
  studioMenu(ev.clientX, ev.clientY, [
    { label: "＋ Bring someone in here", act: () => openHirePanel(at) },
    { label: "Tidy the room", act: () => { localStorage.removeItem("studio-pos"); renderStudio(); } },
    { sep: true },
    { label: "Set a scene", soon: true },
    { label: "Ask the manager to run it", soon: true },
  ]);
}

function studioAgentMenu(ev, a) {
  ev.preventDefault(); ev.stopPropagation();
  studioMenu(ev.clientX, ev.clientY, [
    { label: `Open ${a.name}`, act: () => openAgentDetail(a.id) },
    { label: "Talk to them", soon: true },
    { sep: true },
    { label: "Send home (archive)", act: async () => {
        await api(`/api/home/${a.id}`, { method: "DELETE" }); renderStudio(); } },
  ]);
}


// ============================================================================
//  Scenes. A sibling of the Studio: the same figures, the same emblems, the same
//  right-click world and inline composers (never a browser prompt). A Scene is a
//  generic setting the owner names, gives rules, and seats their agents in; the
//  cards/deck/pot are CODE that runs deterministically, and every model call is
//  billed and shown.
// ============================================================================
let scenesList = [];
let openSceneId = null;
let sceneView = null;          // the public_view of the open scene
let sceneEvents = [];          // events from the last load
let sceneHomeAgents = [];      // Studio agents, for provider lookup + seating
const peekedHands = {};        // seatId -> your_hand (an agent's secret, revealed on request)
let playingBack = false;

const sceneSleep = (ms) => reduceMotion() ? Promise.resolve()
  : new Promise((r) => setTimeout(r, ms));

// A card is code. Face-down looks like a card back; face-up shows rank + suit,
// red for hearts/diamonds. aid, when present, makes the card flippable (owner tool).
function sceneCardHtml(state, aid) {
  const at = aid ? ` data-aid="${escapeHtml(String(aid))}"` : "";
  if (!state || state.facedown) {
    return `<span class="pcard back"${at}><span class="pcard-weave"></span></span>`;
  }
  const s = suitInfo(state.suit);
  const r = escapeHtml(String(state.rank || "?"));
  return `<span class="pcard up${s.red ? " red" : ""}"${at}>
    <span class="pc-c tl">${r}<b>${s.glyph}</b></span>
    <span class="pc-pip">${s.glyph}</span>
    <span class="pc-c br">${r}<b>${s.glyph}</b></span>
  </span>`;
}

// Scenes is now a tab of the Studio; the #/scenes routes still land here and open
// the Studio shell on that tab, so old links keep working.
async function openScenes(skipHash, wantId) {
  showStudioShell();
  openSceneId = wantId || null;
  await selectStudioTab("scenes", true);
  if (!skipHash) setHash(studioTabHash("scenes"));
}

// The list route also tells us whether Scenes are enabled at all.
async function renderScenes() {
  if ($("#tabScenes").hidden) return;
  try {
    const d = await api("/api/scene");
    scenesList = d.scenes || [];
    if (d.enabled === false) { renderScenesDisabled(); return; }
  } catch (e) {
    $("#sceneStage").innerHTML = `<p class="empty">Could not load scenes: ${escapeHtml(e.message || String(e))}</p>`;
    return;
  }
  try { sceneHomeAgents = (await api("/api/home")).agents || []; } catch { sceneHomeAgents = []; }
  if (openSceneId) await renderSceneTable();
  else renderSceneLobby();
}

function renderScenesDisabled() {
  $("#sceneToLobby").hidden = true; $("#sceneMeter").hidden = true;
  $("#sceneTitle").textContent = "Scenes";
  $("#sceneStage").innerHTML =
    `<p class="empty">Scenes are switched off on this deployment.</p>`;
}

// --- The lobby: the owner's scenes as cards, plus a way to set a new one. ---
function renderSceneLobby() {
  $("#sceneToLobby").hidden = true;
  $("#sceneMeter").hidden = true;
  $("#sceneNew").hidden = false;
  $("#sceneTitle").textContent = "Scenes";
  const stage = $("#sceneStage");
  if (!scenesList.length) {
    stage.innerHTML = `<div class="scene-lobby"><div class="scene-empty">
      <p>No scenes yet — set one and sit your people down.</p>
      <button class="primary" id="sceneNew2">Set your first scene</button>
    </div></div>`;
    $("#sceneNew2").addEventListener("click", openSceneComposer);
    return;
  }
  const cards = scenesList.map((s) => {
    const seats = Array.isArray(s.seats) ? s.seats.length : (s.seat_count ?? null);
    const status = escapeHtml(s.status || "new");
    return `<button class="scene-card" data-open="${escapeHtml(String(s.id))}">
      <span class="scene-kind">${escapeHtml(s.kind || "scene")}</span>
      <span class="scene-name">${escapeHtml(s.title || "Untitled scene")}</span>
      ${s.goal ? `<span class="scene-goal">${escapeHtml(s.goal)}</span>` : ""}
      <span class="scene-foot">
        <span class="scene-status st-${status}">${status}</span>
        ${s.phase ? `<span class="scene-phase">${escapeHtml(s.phase)}</span>` : ""}
        ${seats != null ? `<span class="scene-seats">${seats} seated</span>` : ""}
      </span>
    </button>`;
  }).join("");
  stage.innerHTML = `<div class="scene-lobby"><div class="scene-cards">${cards}</div></div>`;
  stage.querySelectorAll("[data-open]").forEach((b) =>
    b.addEventListener("click", () => openScene(b.dataset.open)));
}

// --- The composer: assembled inline, never a browser prompt. Mirrors openHirePanel.
// A scene is generic — the owner names it, writes rules everyone in it can read,
// picks which of their agents are seated, and can roll a random key. The game
// "kind" is an implementation detail the backend still needs, so it is sent as a
// fixed "poker" but never surfaced as a chooser. ---
let sceneDraft = null;
let sceneComposerAgents = [];   // the owner's agents, offered as seatable chips
async function openSceneComposer() {
  sceneDraft = { title: "", rules: "", seed: "", agents: [] };
  try { sceneComposerAgents = (await api("/api/home")).agents || []; }
  catch { sceneComposerAgents = sceneHomeAgents || []; }
  renderSceneComposer();
}
function renderSceneComposer() {
  const box = $("#sceneDetail");
  const d = sceneDraft;
  box.hidden = false;
  box.className = "studio-detail composing";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem" style="background:${sigil(d.title || "Scene", "anthropic")}">
        <span class="fig-initial">🎴</span></div>
      <div style="flex:1">
        <input class="sc-name" id="scnTitle" placeholder="A name for the scene"
               value="${escapeHtml(d.title)}" autocomplete="off">
        <p class="sc-preview">a scene of your own making</p>
      </div>
      <button class="sd-close" id="scnClose">✕</button>
    </div>

    <div class="sc-label">Rules <span class="dim">(everyone in the scene can read these)</span></div>
    <textarea class="sc-input" id="scnRules" rows="3"
      placeholder="rules everyone in the scene can read">${escapeHtml(d.rules)}</textarea>

    <div class="sc-label">Agents in this scene</div>
    <div class="sc-row" id="scnAgents">${
      sceneComposerAgents.length
        ? sceneComposerAgents.map((a) => `<button class="sc-chip${
            d.agents.some((x) => String(x) === String(a.id)) ? " on" : ""}"
            data-agent="${escapeHtml(String(a.id))}">${escapeHtml(a.name || "unnamed")}${
            a.degree ? ` · ${escapeHtml(a.degree)}` : ""}</button>`).join("")
        : `<span class="dim">No agents yet — hire some in the Agents tab.</span>`
    }</div>

    <div class="sc-label">Key <span class="dim">(optional — same key deals the same scene)</span></div>
    <div class="sc-keyrow">
      <input class="sc-input" id="scnSeed" placeholder="leave blank for a fresh shuffle"
             value="${escapeHtml(d.seed)}" autocomplete="off">
      <button class="sc-chip" id="scnRandomKey" type="button">🎲 Generate random key</button>
    </div>

    <div class="sc-actions"><button class="primary" id="scnCreate">Set the scene</button></div>`;

  $("#scnTitle").addEventListener("input", (e) => { d.title = e.target.value; });
  $("#scnRules").addEventListener("input", (e) => { d.rules = e.target.value; });
  $("#scnSeed").addEventListener("input", (e) => { d.seed = e.target.value; });
  box.querySelectorAll("[data-agent]").forEach((b) => b.addEventListener("click", () => {
    const raw = b.dataset.agent;
    const agent = sceneComposerAgents.find((a) => String(a.id) === raw);
    const aid = agent ? agent.id : raw;
    const idx = d.agents.findIndex((x) => String(x) === raw);
    if (idx >= 0) d.agents.splice(idx, 1); else d.agents.push(aid);
    b.classList.toggle("on");
  }));
  $("#scnRandomKey").addEventListener("click", () => {
    d.seed = String(Math.floor(Math.random() * 1e9));
    $("#scnSeed").value = d.seed;
  });
  $("#scnClose").addEventListener("click", () => { sceneDraft = null; box.hidden = true; });
  $("#scnCreate").addEventListener("click", doCreateScene);
}
async function doCreateScene() {
  const d = sceneDraft;
  try {
    // seed is an integer server-side; hash any non-numeric text into a stable int.
    let seed = 0;
    const raw = (d.seed || "").trim();
    if (raw) {
      let n = Number.parseInt(raw, 10);
      if (Number.isNaN(n)) { n = 0; for (const c of raw) n = (n * 31 + c.charCodeAt(0)) >>> 0; }
      seed = n;
    }
    // kind stays "poker" for the backend/table rendering — never shown in the UI.
    const body = { kind: "poker", title: d.title.trim() || "Untitled scene",
                   rules: d.rules.trim(), seed };
    const r = await api("/api/scene", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    const id = r.scene && (r.scene.id != null) ? r.scene.id : null;
    // Seat each chosen agent into the new scene.
    if (id != null) {
      for (const aid of d.agents) {
        try {
          await api(`/api/scene/${id}/seat`, { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ home_id: aid }) });
        } catch (e) { toast(`Could not seat an agent: ${e.message}`); }
      }
    }
    sceneDraft = null;
    $("#sceneDetail").hidden = true;
    if (id != null) openScene(id); else await renderScenes();
  } catch (e) { toast(`Could not set the scene: ${e.message}`); }
}

async function openScene(id) {
  openSceneId = id;
  for (const k of Object.keys(peekedHands)) delete peekedHands[k];
  setHash(`#/scenes/${id}`);
  $("#sceneDetail").hidden = true;
  await renderScenes();
}

async function loadSceneView(seatId) {
  const q = seatId ? `?seat=${encodeURIComponent(seatId)}` : "";
  const d = await api(`/api/scene/${openSceneId}${q}`);
  if (!seatId) { sceneView = d.view; sceneEvents = d.events || []; }
  return d;
}

function sceneSeatPos(i, n) {
  // Players ring an oval, first seat at the near edge (bottom), going clockwise.
  const theta = (Math.PI / 2) + (i / Math.max(1, n)) * Math.PI * 2;
  return { x: 50 + Math.cos(theta) * 41, y: 52 + Math.sin(theta) * 41 };
}

function providerOfSeat(seat) {
  const a = sceneHomeAgents.find((x) => String(x.id) === String(seat.home_id));
  return a ? a.provider : "anthropic";
}

async function renderSceneTable() {
  let d;
  try { d = await loadSceneView(); }
  catch (e) { $("#sceneStage").innerHTML = `<p class="empty">Could not open scene: ${escapeHtml(e.message || String(e))}</p>`; return; }
  const v = sceneView || {};
  $("#sceneNew").hidden = true;
  $("#sceneToLobby").hidden = false;
  $("#sceneTitle").textContent = v.title || "Scene";
  paintSceneMeter(v);

  const seats = (v.seats || []).slice();
  const players = seats.filter((s) => s.role === "player")
    .sort((a, b) => (a.seat ?? 0) - (b.seat ?? 0));
  const manager = seats.find((s) => s.role === "manager");
  const dealer = seats.find((s) => s.role === "dealer");
  const arts = v.artifacts || [];
  const isBoard = (a) => a.type === "card" && (!a.holder || a.holder === "board" || a.holder === "community");
  const board = arts.filter(isBoard);
  // Props are library artifacts placed into the scene — anything that is not a
  // playing card. A sealed prop hides its secret value behind a lock.
  const props = arts.filter((a) => a.type !== "card");

  const stage = $("#sceneStage");
  const paused = v.status === "paused";
  stage.innerHTML = `
    ${v.rules ? `<div class="scene-rules"><span class="scene-rules-tag">house rules</span>
      <span class="scene-rules-text">${escapeHtml(v.rules)}</span></div>` : ""}
    <div class="scene-table-wrap" id="sceneTableWrap">
      <div class="poker-table">
        <div class="table-rail"></div>
        <div class="table-felt">
          <div class="table-center">
            <div class="scene-board" id="sceneBoard">${
              board.length ? board.map((a) => sceneCardHtml(a.state, a.id)).join("")
                           : `<span class="board-empty">no cards on the board yet</span>`}</div>
            <div class="scene-pot">🪙 <b>${escapeHtml(String(v.pot ?? 0))}</b><span>pot</span></div>
            ${v.phase ? `<div class="scene-phasepill" id="scenePhase">${escapeHtml(v.phase)}</div>` : ""}
          </div>
        </div>
      </div>
      <div class="seat-layer" id="seatLayer"></div>
      ${manager ? `<div class="manager-figure" id="managerFigure"></div>` : ""}
    </div>
    <div class="scene-controls">
      ${paused ? `<span class="scene-paused">⏸ Paused — the token budget was reached.</span>` : ""}
      <button class="sc-ctl" id="sceneDeal">Deal</button>
      <button class="sc-ctl" id="scenePlay">Play the hand</button>
      <button class="sc-ctl primary" id="sceneRun">▶ Ask the manager to run it</button>
      <button class="sc-ctl" id="sceneAddProp">＋ Add a prop</button>
      <div class="ctl-spacer"></div>
      <button class="sc-ctl danger" id="sceneDelete">Clear the table</button>
    </div>
    ${props.length ? `<div class="scene-props" id="sceneProps">
      <div class="sc-label">Props on the table</div>
      ${props.map((a) => {
        const pub = a.public && typeof a.public === "object" ? a.public : {};
        const name = pub.name || a.name || a.kind || "prop";
        const vars = Object.entries(pub).filter(([k]) => k !== "name")
          .map(([k, val]) => `<span class="prop-var">${escapeHtml(k)}: ${escapeHtml(String(val))}</span>`).join("");
        return `<div class="scene-prop">
          <span class="prop-chip-kind">${escapeHtml(a.kind || a.type || "prop")}</span>
          <span class="scene-prop-name">${escapeHtml(String(name))}</span>
          ${a.sealed ? `<span class="prop-sealed" title="secret sealed until a key holder reveals it">🔒 sealed</span>` : ""}
          ${vars ? `<span class="prop-vars">${vars}</span>` : ""}
        </div>`;
      }).join("")}
    </div>` : ""}
    <div class="scene-log" id="sceneLog" hidden></div>`;

  // Seat figures around the oval.
  const layer = $("#seatLayer");
  players.forEach((s, i) => {
    const p = sceneSeatPos(i, players.length);
    layer.appendChild(sceneSeatFigure(s, p, arts));
  });
  // Manager stands off to the side until asked to walk the room.
  if (manager) {
    const mf = $("#managerFigure");
    mf.dataset.seat = manager.id;
    mf.style.left = "6%"; mf.style.top = "50%";
    mf.innerHTML = `
      <div class="fig-emblem prov-${escapeHtml(providerOfSeat(manager))}" style="background:${sigil(manager.name || "Manager", providerOfSeat(manager))}">
        <span class="fig-initial">${escapeHtml((manager.name || "M")[0])}</span></div>
      <div class="fig-name">${escapeHtml(manager.name || "Manager")}</div>
      <div class="fig-role">manager</div>`;
  }
  if (dealer) {
    const dv = document.createElement("div");
    dv.className = "dealer-chip"; dv.textContent = "D";
    dv.title = `Dealer: ${dealer.name || ""}`;
    $("#sceneTableWrap").appendChild(dv);
  }

  // Right-click the felt to seat someone (createElement/textContent menu).
  const wrap = $("#sceneTableWrap");
  wrap.addEventListener("contextmenu", (ev) => {
    if (ev.target.closest(".seat-figure") || ev.target.closest(".pcard")) return;
    sceneFloorMenu(ev);
  });
  // Flip a card face-up/down — an owner tool, a card is code.
  wrap.querySelectorAll(".pcard[data-aid]").forEach((c) =>
    c.addEventListener("contextmenu", (ev) => sceneCardMenu(ev, c.dataset.aid)));

  $("#sceneDeal").addEventListener("click", () => sceneAction("deal"));
  $("#scenePlay").addEventListener("click", () => sceneAction("play"));
  $("#sceneRun").addEventListener("click", () => sceneAction("run"));
  $("#sceneAddProp").addEventListener("click", openScenePropPicker);
  $("#sceneDelete").addEventListener("click", deleteScene);

  if (!players.length && !manager)
    stage.querySelector(".scene-table-wrap").insertAdjacentHTML("beforeend",
      `<div class="table-hint">Right-click the felt to seat an agent.</div>`);
}

function sceneSeatFigure(s, pos, arts) {
  const el = document.createElement("div");
  el.className = `seat-figure figure-status-${escapeHtml(s.status || "idle")}`;
  el.style.left = pos.x + "%"; el.style.top = pos.y + "%";
  el.dataset.seat = s.id;
  const prov = providerOfSeat(s);
  const hole = arts.filter((a) => a.type === "card" && String(a.holder) === String(s.id));
  const peek = peekedHands[s.id];
  const cardsHtml = hole.map((a, idx) => {
    const st = (peek && peek[idx]) ? peek[idx] : a.state;
    return sceneCardHtml(st, a.id);
  }).join("") || "";
  el.innerHTML = `
    <div class="seat-cards">${cardsHtml}</div>
    <div class="fig-emblem prov-${escapeHtml(prov)}" style="background:${sigil(s.name || "?", prov)}">
      <span class="fig-initial">${escapeHtml((s.name || "?")[0])}</span></div>
    <div class="fig-name">${escapeHtml(s.name || "seat")}</div>
    <div class="seat-meta">
      <span class="seat-stack">🪙 ${escapeHtml(String(s.stack ?? 0))}</span>
      ${s.committed ? `<span class="seat-bet">bet ${escapeHtml(String(s.committed))}</span>` : ""}
    </div>
    <div class="seat-status st-${escapeHtml(s.status || "idle")}">${escapeHtml(s.status || "")}</div>`;
  el.title = peek ? "Click to hide their hand" : "Click to peek — an agent's cards are its secret";
  el.addEventListener("click", () => peekSeat(s));
  el.addEventListener("contextmenu", (ev) => sceneSeatMenu(ev, s));
  return el;
}

function paintSceneMeter(v) {
  const m = $("#sceneMeter");
  m.hidden = false;
  const spent = v.tokens_spent ?? 0, budget = v.token_budget ?? 0;
  const paused = v.status === "paused";
  m.className = "scene-meter" + (paused ? " paused" : "");
  m.textContent = `${v.utterances ?? 0} calls · ${spent}/${budget || "∞"} tokens`
    + (paused ? " · paused" : "");
}

// Peek reveals ONE seat's hand via ?seat= — the only way to see it.
async function peekSeat(seat) {
  if (playingBack) return;
  if (peekedHands[seat.id]) { delete peekedHands[seat.id]; await renderSceneTable(); return; }
  try {
    const d = await loadSceneView(seat.id);
    peekedHands[seat.id] = d.your_hand || [];
    await renderSceneTable();
  } catch (e) { toast(`Could not peek: ${e.message}`); }
}

async function flipCard(aid) {
  try { await api(`/api/scene/${openSceneId}/flip/${aid}`, { method: "POST" }); await renderSceneTable(); }
  catch (e) { toast(`Could not flip: ${e.message}`); }
}

async function deleteScene() {
  if (!openSceneId) return;
  try {
    await api(`/api/scene/${openSceneId}`, { method: "DELETE" });
    openSceneId = null; setHash("#/scenes"); await renderScenes();
  } catch (e) { toast(`Could not clear: ${e.message}`); }
}

// Deal (free), Play (bills), Run (manager briefs, then plays). All replay events.
async function sceneAction(kind) {
  if (playingBack) return;
  const btn = { deal: "#sceneDeal", play: "#scenePlay", run: "#sceneRun" }[kind];
  const b = $(btn); if (b) { b.disabled = true; b.classList.add("busy"); }
  try {
    const r = await api(`/api/scene/${openSceneId}/${kind}`, { method: "POST" });
    await renderSceneTable();
    if (r.events && r.events.length) await playbackEvents(r.events);
    else await renderSceneTable();
  } catch (e) { toast(`${kind} failed: ${e.message}`); }
  finally { const bb = $(btn); if (bb) { bb.disabled = false; bb.classList.remove("busy"); } }
}

// Replay events so the scene feels alive: the manager walks to each player it
// briefs; speech and actions bubble near the figure; the result reveals the win.
async function playbackEvents(events) {
  playingBack = true;
  const log = $("#sceneLog");
  if (log) { log.hidden = false; log.innerHTML = ""; }
  const wrap = $("#sceneTableWrap");
  for (const e of events) {
    const seatEl = e.seat_id ? wrap && wrap.querySelector(`[data-seat="${CSS.escape(String(e.seat_id))}"]`) : null;
    if (e.kind === "brief" && $("#managerFigure") && seatEl) {
      const mf = $("#managerFigure");
      mf.style.left = seatEl.style.left; mf.style.top = `calc(${seatEl.style.top} + 6%)`;
      mf.classList.add("walking");
    }
    if (e.kind === "phase" && $("#scenePhase")) $("#scenePhase").textContent = e.text || "";
    if (seatEl && (e.kind === "say" || e.kind === "act" || e.kind === "brief"))
      sceneBubble(seatEl, e.text, e.kind);
    if (e.kind === "result") sceneResult(e.text);
    if (log) {
      const row = document.createElement("div");
      row.className = `slog-row slog-${escapeHtml(e.kind || "")}`;
      const k = document.createElement("span"); k.className = "slog-kind"; k.textContent = e.kind || "";
      const t = document.createElement("span"); t.className = "slog-text"; t.textContent = e.text || "";
      row.appendChild(k); row.appendChild(t);
      if (e.billed) { const bd = document.createElement("span"); bd.className = "slog-billed"; bd.textContent = "billed"; row.appendChild(bd); }
      log.appendChild(row); log.scrollTop = log.scrollHeight;
    }
    await sceneSleep(650);
  }
  const mf = $("#managerFigure");
  if (mf) { mf.classList.remove("walking"); mf.style.left = "6%"; mf.style.top = "50%"; }
  playingBack = false;
  // Refresh the view once the story is told, so stacks/pot/board settle to truth.
  await renderSceneTable();
}

function sceneBubble(seatEl, text, kind) {
  if (!text) return;
  const b = document.createElement("div");
  b.className = `scene-bubble bub-${escapeHtml(kind || "")}`;
  b.textContent = text;              // free text — textContent, never innerHTML
  seatEl.appendChild(b);
  setTimeout(() => b.remove(), reduceMotion() ? 4000 : 2600);
}

function sceneResult(text) {
  const wrap = $("#sceneTableWrap"); if (!wrap) return;
  const banner = document.createElement("div");
  banner.className = "scene-result";
  banner.textContent = text || "Hand over.";
  wrap.appendChild(banner);
  if (!reduceMotion()) setTimeout(() => banner.classList.add("fade"), 4200);
}

// --- Seating: right-click the felt, pick a role, pick an agent. No popups. ---
function sceneFloorMenu(ev) {
  ev.preventDefault();
  studioMenu(ev.clientX, ev.clientY, [
    { label: "Seat an agent here", act: () => openSeatPicker("player") },
    { label: "Seat a manager", act: () => openSeatPicker("manager") },
    { label: "Seat a dealer", act: () => openSeatPicker("dealer") },
    { sep: true },
    { label: "Clear the table", act: deleteScene },
  ]);
}

function sceneSeatMenu(ev, s) {
  ev.preventDefault(); ev.stopPropagation();
  studioMenu(ev.clientX, ev.clientY, [
    { label: peekedHands[s.id] ? `Hide ${s.name}'s hand` : `Peek ${s.name}'s hand`, act: () => peekSeat(s) },
    { label: `Talk to ${s.name}`, act: () => openSeatTalk(s) },
  ]);
}

function sceneCardMenu(ev, aid) {
  ev.preventDefault(); ev.stopPropagation();
  studioMenu(ev.clientX, ev.clientY, [
    { label: "Flip this card", act: () => flipCard(aid) },
  ]);
}

// An inline picker of the owner's Studio agents to seat. Reuses the composer drawer.
function openSeatPicker(role) {
  const box = $("#sceneDetail");
  box.hidden = false;
  box.className = "studio-detail composing";
  const roleLabel = { player: "a player", manager: "the manager", dealer: "the dealer" }[role] || role;
  const list = sceneHomeAgents.length
    ? sceneHomeAgents.map((a) => `<button class="seatpick-row" data-home="${escapeHtml(String(a.id))}"
        data-name="${escapeHtml(a.name || "")}">
        <span class="fig-emblem prov-${escapeHtml(a.provider)}" style="background:${sigil(a.name, a.provider)}">
          <span class="fig-initial">${escapeHtml((a.name || "?")[0])}</span></span>
        <span class="seatpick-name">${escapeHtml(a.name || "unnamed")}</span>
        <span class="seatpick-role">${escapeHtml(a.degree || "generalist")}</span>
      </button>`).join("")
    : `<p class="dim">No one lives in the Studio yet. Hire someone there first.</p>`;
  box.innerHTML = `
    <div class="sd-head">
      <div style="flex:1"><h3>Seat ${escapeHtml(roleLabel)}</h3>
        <p class="sc-preview">Pick who takes the seat.</p></div>
      <button class="sd-close" id="spClose">✕</button>
    </div>
    <div class="seatpick-list">${list}</div>`;
  $("#spClose").addEventListener("click", () => box.hidden = true);
  box.querySelectorAll("[data-home]").forEach((b) =>
    b.addEventListener("click", () => seatAgent(b.dataset.home, role, b.dataset.name)));
}

async function seatAgent(homeId, role, name) {
  try {
    await api(`/api/scene/${openSceneId}/seat`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ home_id: homeId, role, name }) });
    $("#sceneDetail").hidden = true;
    await renderSceneTable();
  } catch (e) { toast(`Could not seat them: ${e.message}`); }
}

// Pull the owner's artifact library and let them place one into the scene as a
// prop. Reuses the composer drawer. If the artifact declares secret fields, placing
// it seals them (the scene POST returns sealed:true and hides the value).
async function openScenePropPicker() {
  const box = $("#sceneDetail");
  box.hidden = false;
  box.className = "studio-detail composing";
  box.innerHTML = `
    <div class="sd-head">
      <div style="flex:1"><h3>Add a prop</h3>
        <p class="sc-preview">Place one of your shelf props into the scene.</p></div>
      <button class="sd-close" id="ppClose">✕</button>
    </div>
    <div class="seatpick-list" id="ppList"><p class="dim">loading your library…</p></div>`;
  $("#ppClose").addEventListener("click", () => box.hidden = true);
  let arts = [];
  try { arts = (await api("/api/artifacts")).artifacts || []; }
  catch (e) { $("#ppList").innerHTML = `<p class="dim">${escapeHtml(e.message || String(e))}</p>`; return; }
  if (!arts.length) {
    $("#ppList").innerHTML = `<p class="dim">Your shelf is empty. Make a prop in the Artifacts tab first.</p>`;
    return;
  }
  $("#ppList").innerHTML = arts.map((a) => {
    const sealed = Array.isArray(a.secret_schema) && a.secret_schema.length;
    return `<button class="seatpick-row" data-def="${escapeHtml(String(a.id))}">
      <span class="prop-chip-kind">${escapeHtml(a.kind || "prop")}</span>
      <span class="seatpick-name">${escapeHtml(a.name || "unnamed")}</span>
      <span class="seatpick-role">${a.dormant ? "dormant" : "active"}${sealed ? " · 🔒" : ""}</span>
    </button>`;
  }).join("");
  $("#ppList").querySelectorAll("[data-def]").forEach((b) =>
    b.addEventListener("click", () => placeArtifactInScene(arts.find((a) => String(a.id) === b.dataset.def))));
}

async function placeArtifactInScene(art) {
  if (!art) return;
  try {
    const body = { def_id: art.id, kind: art.kind, dormant: !!art.dormant,
                   public: (art.public && typeof art.public === "object") ? art.public : {} };
    // Named secret fields are sealed on placement — hand them over as the secret object.
    if (Array.isArray(art.secret_schema) && art.secret_schema.length)
      body.secret = Object.fromEntries(art.secret_schema.map((f) => [f, ""]));
    await api(`/api/scene/${openSceneId}/artifact`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("#sceneDetail").hidden = true;
    await renderSceneTable();
  } catch (e) { toast(`Could not place it: ${e.message}`); }
}

// Talk to one seat — an inline exchange, replies billed and shown.
function openSeatTalk(s) {
  const box = $("#sceneDetail");
  box.hidden = false;
  box.className = "studio-detail composing";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem prov-${escapeHtml(providerOfSeat(s))}" style="background:${sigil(s.name || "?", providerOfSeat(s))}">
        <span class="fig-initial">${escapeHtml((s.name || "?")[0])}</span></div>
      <div style="flex:1"><h3>${escapeHtml(s.name || "seat")}</h3>
        <p class="sc-preview">${escapeHtml(s.role || "")}</p></div>
      <button class="sd-close" id="stClose">✕</button>
    </div>
    <div class="seat-talk" id="seatTalk"></div>
    <div class="sc-label">Say something</div>
    <textarea class="sc-input" id="stMsg" rows="2" placeholder="Ask them how they're reading the table…"></textarea>
    <div class="sc-actions"><button class="primary" id="stSend">Send</button></div>`;
  $("#stClose").addEventListener("click", () => box.hidden = true);
  $("#stSend").addEventListener("click", async () => {
    const msg = $("#stMsg").value.trim();
    if (!msg) return;
    const feed = $("#seatTalk");
    const mine = document.createElement("div"); mine.className = "st-you";
    mine.textContent = msg; feed.appendChild(mine);
    $("#stMsg").value = "";
    try {
      const r = await api(`/api/scene/${openSceneId}/talk`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seat_id: s.id, message: msg }) });
      const rep = document.createElement("div"); rep.className = "st-them";
      rep.textContent = r.reply || "…"; feed.appendChild(rep);
      feed.scrollTop = feed.scrollHeight;
      const v = await loadSceneView(); paintSceneMeter(v);
    } catch (e) { toast(`Could not reach them: ${e.message}`); }
  });
}

$("#scenesBack") && $("#scenesBack").addEventListener("click", () => showHome());
$("#sceneToLobby") && $("#sceneToLobby").addEventListener("click", () => {
  openSceneId = null; sceneView = null; setHash("#/scenes"); renderScenes();
});
$("#sceneNew") && $("#sceneNew").addEventListener("click", openSceneComposer);


// ============================================================================
//  Artifacts — the prop shelf. Reusable objects (a card, a deck, a chair, a
//  table, a prop) with public variables everyone can read and secret fields that
//  seal when the prop is placed in a scene. Dormant props are inert; active ones
//  act when interacted with. Inline composer only, same drawer chrome as the rest.
// ============================================================================
const ARTIFACT_KINDS = ["card", "deck", "chair", "table", "prop"];
let artifactsList = [];
let artDraft = null;

async function renderArtifactLib() {
  if ($("#tabArtifacts").hidden) return;
  const shelf = $("#artifactShelf");
  if (!shelf) return;
  let d;
  try { d = await api("/api/artifacts"); }
  catch (e) { shelf.innerHTML = `<p class="empty">Could not load artifacts: ${escapeHtml(e.message || String(e))}</p>`; return; }
  artifactsList = d.artifacts || [];
  if (!artifactsList.length) {
    shelf.innerHTML = `<div class="artifact-empty">
      <p>The shelf is bare. Props are reusable objects — a card, a deck, a chair —
        that carry public variables and sealed secrets, and get placed into scenes.</p>
      <button class="primary" id="artNew2">Make your first prop</button>
    </div>`;
    $("#artNew2").addEventListener("click", () => openArtifactComposer());
    return;
  }
  shelf.innerHTML = `<div class="prop-grid">${artifactsList.map((a) => {
    const pub = (a.public && typeof a.public === "object") ? a.public : {};
    const nVars = Object.keys(pub).length;
    const nSecret = Array.isArray(a.secret_schema) ? a.secret_schema.length : 0;
    return `<button class="prop-card kind-${escapeHtml(a.kind || "prop")} ${a.dormant ? "dormant" : "active"}"
      data-id="${escapeHtml(String(a.id))}">
      <span class="prop-kind">${escapeHtml(a.kind || "prop")}</span>
      <span class="prop-name">${escapeHtml(a.name || "unnamed prop")}</span>
      ${a.description ? `<span class="prop-desc">${escapeHtml(a.description)}</span>` : ""}
      <span class="prop-foot">
        <span class="prop-state">${a.dormant ? "dormant" : "active"}</span>
        ${nVars ? `<span class="prop-badge">${nVars} public</span>` : ""}
        ${nSecret ? `<span class="prop-badge sealed">🔒 ${nSecret} sealed</span>` : ""}
      </span>
    </button>`;
  }).join("")}</div>`;
  shelf.querySelectorAll("[data-id]").forEach((b) => {
    const a = artifactsList.find((x) => String(x.id) === b.dataset.id);
    b.addEventListener("click", () => openArtifactComposer(a));
    b.addEventListener("contextmenu", (ev) => artifactCardMenu(ev, a));
  });
}

function openArtifactComposer(existing) {
  if (existing) {
    const pub = (existing.public && typeof existing.public === "object") ? existing.public : {};
    artDraft = {
      id: existing.id,
      name: existing.name || "",
      kind: existing.kind || "prop",
      dormant: existing.dormant !== false,
      pubRows: Object.entries(pub).map(([k, v]) => ({ k, v: String(v) })),
      secretText: Array.isArray(existing.secret_schema) ? existing.secret_schema.join(", ") : "",
      description: existing.description || "",
    };
  } else {
    artDraft = { id: null, name: "", kind: "card", dormant: true,
                 pubRows: [{ k: "", v: "" }], secretText: "", description: "" };
  }
  renderArtifactComposer();
}

function renderArtifactComposer() {
  const box = $("#artifactDetail");
  const d = artDraft;
  box.hidden = false;
  box.className = "studio-detail composing";
  const rows = d.pubRows.length ? d.pubRows : [{ k: "", v: "" }];
  const stateNote = d.dormant ? "dormant — inert like a chair"
                              : "active — acts when interacted with";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem" style="background:${sigil(d.name || "prop", "anthropic")}">
        <span class="fig-initial">${escapeHtml((d.name || "?")[0] || "?")}</span></div>
      <div style="flex:1">
        <input class="sc-name" id="artName" placeholder="Name this prop"
               value="${escapeHtml(d.name)}" autocomplete="off">
        <p class="sc-preview">${escapeHtml(stateNote)}</p>
      </div>
      <button class="sd-close" id="artClose">✕</button>
    </div>

    <div class="sc-label">Kind</div>
    <div class="sc-row">${ARTIFACT_KINDS.map((k) => chip(k, d.kind === k, `data-artkind="${k}"`)).join("")}</div>

    <div class="sc-label">State</div>
    <div class="seg sc-seg" id="artStateSeg">
      <label class="seg-opt"><input type="radio" name="artstate" ${d.dormant ? "checked" : ""} data-dormant="1"><span>Dormant</span></label>
      <label class="seg-opt"><input type="radio" name="artstate" ${d.dormant ? "" : "checked"} data-dormant="0"><span>Active</span></label>
    </div>
    <p class="sc-hint">dormant = inert like a chair; active = acts when interacted with</p>

    <div class="sc-label">Public variables <span class="dim">(visible to everyone)</span></div>
    <div id="artPubRows">${rows.map((r) => `
      <div class="kv-row">
        <input class="sc-input kv-k" placeholder="key" value="${escapeHtml(r.k)}" autocomplete="off">
        <input class="sc-input kv-v" placeholder="value" value="${escapeHtml(r.v)}" autocomplete="off">
        <button class="kv-del" title="Remove">✕</button>
      </div>`).join("")}</div>
    <button class="sc-chip" id="artAddVar">＋ add variable</button>

    <div class="sc-label">Secret fields <span class="dim">(sealed when placed in a scene)</span></div>
    <input class="sc-input" id="artSecret" placeholder="comma-separated, e.g. hole_cards, pin"
           value="${escapeHtml(d.secretText)}" autocomplete="off">

    <div class="sc-label">Description <span class="dim">(optional)</span></div>
    <textarea class="sc-input" id="artDesc" rows="2"
      placeholder="what this prop is, and how it behaves…">${escapeHtml(d.description)}</textarea>

    <div class="sc-actions">
      <button class="primary" id="artSave">${d.id ? "Save changes" : "Add to the shelf"}</button>
      ${d.id ? `<button class="danger" id="artDelete">Delete</button>` : ""}
    </div>`;

  // Read the live row inputs back into the draft before any re-render.
  const syncRows = () => {
    d.pubRows = Array.from(box.querySelectorAll(".kv-row")).map((row) => ({
      k: row.querySelector(".kv-k").value, v: row.querySelector(".kv-v").value }));
  };
  $("#artName").addEventListener("input", (e) => {
    d.name = e.target.value;
    box.querySelector(".fig-initial").textContent = (d.name || "?")[0] || "?";
  });
  $("#artSecret").addEventListener("input", (e) => { d.secretText = e.target.value; });
  $("#artDesc").addEventListener("input", (e) => { d.description = e.target.value; });
  box.querySelectorAll("[data-artkind]").forEach((b) =>
    b.addEventListener("click", () => { syncRows(); d.kind = b.dataset.artkind; renderArtifactComposer(); }));
  box.querySelectorAll("[data-dormant]").forEach((r) =>
    r.addEventListener("change", () => {
      d.dormant = r.dataset.dormant === "1";
      box.querySelector(".sc-preview").textContent = d.dormant
        ? "dormant — inert like a chair" : "active — acts when interacted with";
    }));
  box.querySelectorAll(".kv-del").forEach((b, i) =>
    b.addEventListener("click", () => {
      syncRows(); d.pubRows.splice(i, 1);
      if (!d.pubRows.length) d.pubRows = [{ k: "", v: "" }];
      renderArtifactComposer();
    }));
  $("#artAddVar").addEventListener("click", () => { syncRows(); d.pubRows.push({ k: "", v: "" }); renderArtifactComposer(); });
  $("#artClose").addEventListener("click", () => { artDraft = null; box.hidden = true; });
  $("#artSave").addEventListener("click", () => { syncRows(); doSaveArtifact(); });
  if (d.id) $("#artDelete").addEventListener("click", () => deleteArtifact(d.id));
}

async function doSaveArtifact() {
  const d = artDraft;
  const publicObj = {};
  for (const r of d.pubRows) { const k = r.k.trim(); if (k) publicObj[k] = r.v; }
  const secret_schema = d.secretText.split(",").map((s) => s.trim()).filter(Boolean);
  const body = { name: d.name.trim() || "Untitled prop", kind: d.kind, dormant: !!d.dormant,
                 public: publicObj, secret_schema, description: d.description.trim() };
  try {
    if (d.id) await api(`/api/artifacts/${d.id}`, { method: "PATCH",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    else await api("/api/artifacts", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    artDraft = null;
    $("#artifactDetail").hidden = true;
    await renderArtifactLib();
  } catch (e) { toast(`Could not save the prop: ${e.message}`); }
}

async function deleteArtifact(id) {
  try {
    await api(`/api/artifacts/${id}`, { method: "DELETE" });
    artDraft = null;
    $("#artifactDetail").hidden = true;
    await renderArtifactLib();
  } catch (e) { toast(`Could not delete: ${e.message}`); }
}

// Right-click a prop to edit or delete it — createElement/textContent menu (labels
// carry the prop's free-text name), matching studioMenu everywhere else.
function artifactCardMenu(ev, a) {
  ev.preventDefault(); ev.stopPropagation();
  studioMenu(ev.clientX, ev.clientY, [
    { label: `Edit ${a.name || "prop"}`, act: () => openArtifactComposer(a) },
    { sep: true },
    { label: `Delete ${a.name || "prop"}`, act: () => deleteArtifact(a.id) },
  ]);
}

$("#artNew") && $("#artNew").addEventListener("click", () => openArtifactComposer());


