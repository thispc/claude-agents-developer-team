// agent.js — the agent's own page, and the small popup that precedes it.
//
// The drawer this replaces was 340px wide with `overflow-y: auto`, and the decision DAG
// inside it was capped at 340px tall — so a graph five siblings wide (660px by its own
// layout) was being read through a keyhole, with two nested scrollbars. Its close button was
// not buggy: it sat inside the scrolling container and scrolled off the top, Escape was
// never wired, and #lwDetail is a shared singleton that eight different renderers overwrite.
//
// So the split is: SINGLE click gets a popup that physically cannot scroll (five facts,
// `overflow: hidden`, so the promise is structural), and DOUBLE click opens a page where
// the graph finally has room.
//
// The order of the tabs is the order the questions get asked: what is it doing (Now), why
// did it do that (Why), what has it learned (Learned), can I trust it (Record), and only
// then who is it (Makeup) — configuration last, because it changes almost never and putting
// a character sheet first makes an operations screen read like a game.

const AG_TABS = [
  { id: "now", label: "Now" },
  { id: "why", label: "Why" },
  { id: "learned", label: "Learned" },
  { id: "record", label: "Record" },
  { id: "makeup", label: "Makeup" },
];

let agTab = "now";          // module-level: a poll that reset the tab mid-read is unusable
let agId = 0;
let agData = null;
let agTimer = null;

async function openAgentPage(hid, tab, skipHash) {
  agId = Number(hid); agTab = tab || "now";
  hideScreens("#agentPage");
  const bar = $("#projectBar"); if (bar) bar.hidden = true;
  $("#agentPage").hidden = false;
  if (!skipHash) setHash(`#/agent/${agId}${agTab === "now" ? "" : "/" + agTab}`);
  lwPeekClose();
  $("#agentBody").innerHTML = `<p class="dim">reading them…</p>`;
  await agRefresh();
  if (agTimer) clearInterval(agTimer);
  // Poll only while the page is open AND the reader is on Now — repainting the Why tab under
  // the cursor clears the selected node and blanks the explainer.
  agTimer = setInterval(() => {
    if ($("#agentPage").hidden) { clearInterval(agTimer); agTimer = null; return; }
    if (agTab === "now") agRefresh();
  }, 5000);
}

async function agRefresh() {
  try {
    if (typeof lwWorldId === "undefined" || !lwWorldId) await agEnsureWorld();
    try {
      agData = await api(`/api/lw/${lwWorldId}/human/${agId}`);
    } catch {
      await agEnsureWorld();                    // the open world is not this agent's
      agData = await api(`/api/lw/${lwWorldId}/human/${agId}`);
    }
  } catch (e) {
    $("#agentBody").innerHTML = `<p class="dim">${escapeHtml(reportCaught(e, "agentPage"))}</p>`;
    return;
  }
  const h = agData.human || {};
  $("#agBarName").textContent = h.name || "agent";
  const act = agData.activity || {};
  $("#agBarState").textContent = act.busy ? `● ${act.state}${act.what ? " — " + act.what : ""}` : "idle";
  $("#agentBody").innerHTML = agHtml(agData);
  agWire();
}

/** A deep link arrives with no world loaded, so find the one this agent lives in.
 *
 * There can be more than one world (the Studio's, and the self-repair crew's), so "the first
 * one" is a guess that is wrong half the time. Ask each until one admits to this agent —
 * usually one call, always correct. */
async function agEnsureWorld() {
  const list = await api("/api/lw");
  const worlds = list.worlds || [];
  if (!worlds.length) throw new Error("no world yet — open the Studio once");
  for (const w of worlds) {
    try {
      await api(`/api/lw/${w.id}/human/${agId}`);
      // Assign the LEXICAL binding, not a window property. `lwWorldId` is declared with
      // `let` at the top of a classic script, which does not create a window property — so
      // `window.lwWorldId = x` makes a second, unrelated slot that every reader ignores,
      // and every later URL is built from a `lwWorldId` that is still null.
      lwWorldId = w.id;
      return;
    } catch { /* not this one */ }
  }
  throw new Error("no world has that agent any more");
}

function agHtml(d) {
  const h = d.human || {};
  const act = d.activity || {};
  const u = h.usage || d.usage || {};
  const panels = {
    now: agNowHtml(d, h, act, u),
    why: agWhyHtml(d),
    learned: agLearnedHtml(d),
    record: agRecordHtml(d),
    makeup: agMakeupHtml(d, h),
  };
  return `
  <div class="ag-id">
    <div class="fig-emblem lw-av-emblem"><img alt="" src="${lwSvgUri(lwAvatarSvg(lwAvatarSeed({ name: h.name, id: agId, figure: h.figure }), 56))}"></div>
    <div class="ag-id-main">
      <h2>${escapeHtml(h.name || "someone")}</h2>
      <p class="sd-persona">${escapeHtml(d.narrative || h.narrative || "no story yet")}</p>
    </div>
    <div class="sd-facts">
      <span>${act.busy ? "● " + escapeHtml(act.state) : "idle"}</span>
      <span>${escapeHtml(h.model || "the scene's model")}</span>
      <span>τ ${escapeHtml(String(h.tau ?? 0))}</span>
    </div>
  </div>
  <div class="rp-tabs" role="tablist">
    ${AG_TABS.map((t) => `<button class="rp-tab${t.id === agTab ? " on" : ""}" role="tab"
      aria-selected="${t.id === agTab}" data-agtab="${t.id}">${escapeHtml(t.label)}</button>`).join("")}
    <span class="rp-tabnote">${(d.decisions || []).length} decisions · ${(d.canon || []).length} pivots</span>
  </div>
  ${AG_TABS.map((t) => `<div class="rp-panel" data-agpanel="${t.id}"${t.id === agTab ? "" : " hidden"}>${panels[t.id]}</div>`).join("")}`;
}

function agNowHtml(d, h, act, u) {
  const mood = h.mood || {};
  const want = typeof dominantWant === "function" ? dominantWant(h.wants) : null;
  return `
  <div class="ag-grid">
    <div class="rp-card">
      <div class="rp-card-h">Doing <span class="dim">${escapeHtml(act.means || "nothing right now")}</span></div>
      ${act.busy
        ? `<p class="ag-doing">${escapeHtml(act.what || act.state)}<span class="dim"> · for ${Math.max(1, Math.round((act.for_s || 0) / 60))}m</span></p>`
        : `<p class="dim">Idle. It will pick something up when its graph next runs.</p>`}
      ${u.cap ? `<div class="rp-meterrow"><span class="rp-meter-lb">session</span>
        ${rpBar({ used: u.used || 0, cap: u.cap || 1 })}
        <span class="rp-meter-n">${u.used || 0}/${u.cap}</span></div>` : ""}
      ${u.asleep ? `<p class="rp-cool">out of quota — resting until its window rolls</p>` : ""}
    </div>
    <div class="rp-card">
      <div class="rp-card-h">Mood</div>
      <div class="lw-meters">
        ${lwMeter("confidence", mood.confidence)}${lwMeter("stress", mood.stress)}
        ${lwMeter("hope", mood.hope)}${lwMeter("focus", mood.focus)}
      </div>
      ${want ? `<div class="sd-label">Wants</div>
        <div class="lw-want-big">▸ ${escapeHtml(want.name)}${want.pressure != null ? ` <span class="dim">pressure ${lwPct(want.pressure)}</span>` : ""}</div>` : ""}
    </div>
  </div>`;
}

function agWhyHtml(d) {
  const nodes = d.decisions || [];
  if (!nodes.length) {
    return `<div class="rp-card"><p class="dim">No decisions recorded yet. They appear as soon
      as it acts on something — every one carries the causes we already had.</p></div>`;
  }
  return `<div class="rp-card">
    <div class="ag-legend"><span><i class="ag-k canon"></i>pivot</span><span><i class="ag-k good"></i>held up</span>
      <span><i class="ag-k bad"></i>did not</span><span><i class="ag-k open"></i>still open</span>
      <span class="dim">stale = it rested on something later shown wrong</span></div>
    <div class="ag-why">${lwTreeHtml(nodes, d.canon)}</div>
    ${d.withheld ? `<p class="hint">${d.withheld} private decision${d.withheld === 1 ? " is" : "s are"} withheld — only root may read an agent's private reasoning.</p>` : ""}
  </div>`;
}

function agLearnedHtml(d) {
  const habits = d.habits || [];
  return `
  <div class="rp-card">
    <div class="rp-card-h">What it expects <span class="dim">looked up before it thinks — a hit costs nothing</span></div>
    ${lwAssocHtml(d.associations) || `<p class="dim">Nothing proven yet. An association needs two
      agreeing outcomes before it is allowed to influence anything — one coincidence is superstition.</p>`}
  </div>
  <div class="rp-card">
    <div class="rp-card-h">Compiled habits <span class="dim">reflexes it no longer pays to think about</span></div>
    ${habits.length ? `<div class="lw-habits">${habits.map((hb) => `<div class="lw-habit">
      <span class="lw-habit-when">when ${lwHabitWhen(hb.when)}</span>
      <span class="lw-habit-meta">conf ${lwPct(hb.confidence)} · fired ${escapeHtml(String(hb.fires ?? 0))}×</span>
    </div>`).join("")}</div>` : `<p class="dim">none yet</p>`}
  </div>`;
}

function agRecordHtml(d) {
  const nodes = d.decisions || [];
  const tally = { good: 0, bad: 0, open: 0, stale: 0 };
  for (const n of nodes) { tally[n.outcome === "good" ? "good" : n.outcome === "bad" ? "bad" : "open"]++; if (n.stale) tally.stale++; }
  const r = (d.human || {}).resume || {};
  return `
  <div class="rp-card">
    <div class="rp-card-h">Its record ${r.intact ? `<span class="lw-verified">chain verified ✓</span>` : `<span class="lw-broken">chain broken</span>`}</div>
    <table class="rp-table"><tbody>
      <tr><td>held up</td><td class="rp-num">${tally.good}</td></tr>
      <tr><td>did not</td><td class="rp-num">${tally.bad}</td></tr>
      <tr><td>still open</td><td class="rp-num">${tally.open}</td></tr>
      <tr><td>resting on something later shown wrong</td><td class="rp-num">${tally.stale}</td></tr>
    </tbody></table>
  </div>
  ${lwAgentLogsHtml(d.logs) || `<div class="rp-card"><p class="dim">No backend log rows for
    them${d.logs ? "" : " — or you are not root, and the server did not send any"}.</p></div>`}`;
}

function agMakeupHtml(d, h) {
  const traits = h.traits || {};
  const bonds = (d.bonds && typeof d.bonds === "object") ? d.bonds : {};
  return `
  <div class="rp-card">
    <div class="rp-card-h">Genome <span class="dim">how it reacts — the dials, not the goals</span></div>
    <div class="lw-meters">${Object.entries(traits).map(([k, v]) => lwMeter(k, v)).join("")
      || `<p class="dim">neutral on everything</p>`}</div>
  </div>
  ${Object.keys(bonds).length ? `<div class="rp-card">
    <div class="rp-card-h">Bonds</div>
    <div class="lw-bonds">${Object.entries(bonds).map(([oid, b]) => `<div class="lw-bond">
      <span class="lw-bond-name">${escapeHtml(typeof lwNameOf === "function" ? lwNameOf(oid) : String(oid))}</span>
      <span class="lw-bond-meta">trust ${lwPct(b && b.trust)} · warmth ${lwPct(b && b.warmth)}</span>
    </div>`).join("")}</div></div>` : ""}`;
}

function agWire() {
  const body = $("#agentBody");
  body.querySelectorAll("[data-agtab]").forEach((b) => b.addEventListener("click", () => {
    agTab = b.dataset.agtab;
    body.querySelectorAll("[data-agtab]").forEach((x) => {
      x.classList.toggle("on", x.dataset.agtab === agTab);
      x.setAttribute("aria-selected", String(x.dataset.agtab === agTab));
    });
    body.querySelectorAll("[data-agpanel]").forEach((p) => { p.hidden = p.dataset.agpanel !== agTab; });
    setHash(`#/agent/${agId}${agTab === "now" ? "" : "/" + agTab}`);
    if (agTab === "why") lwWireDag(body);
  }));
  if (agTab === "why") lwWireDag(body);
  const back = $("#agBack");
  if (back) back.onclick = () => { history.length > 1 ? history.back() : openStudio(); };
}

// ---- the popup: five facts, and it cannot scroll --------------------------

/** Its own element, not the shared #lwDetail — which eight renderers overwrite, each
 * re-minting an element with the same id. `overflow: hidden` in the stylesheet is what makes
 * "no scrolling needed" a structural fact rather than a promise about content length. */
function lwPeekEnsure() {
  if ($("#lwPeek")) return $("#lwPeek");
  const el = document.createElement("div");
  el.id = "lwPeek";
  el.className = "sd-peek";
  el.hidden = true;
  document.body.appendChild(el);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") lwPeekClose(); });
  document.addEventListener("pointerdown", (e) => {
    if (!el.hidden && !el.contains(e.target)) lwPeekClose();
  }, true);
  return el;
}

function lwPeekClose() { const el = $("#lwPeek"); if (el) el.hidden = true; }

/** Single click. Everything here comes from the room payload the canvas already holds, so it
 * can never show a spinner. */
function lwPeekOpen(agent) {
  if (!agent) return;
  const el = lwPeekEnsure();
  const u = agent.usage || {}, act = agent.activity || {};
  const mood = agent.mood || {};
  el.innerHTML = `
    <div class="sd-peek-head">
      <img alt="" src="${lwSvgUri(lwAvatarSvg(lwAvatarSeed(agent), 30))}">
      <b>${escapeHtml(agent.name || "someone")}</b>
      <button class="sd-close" id="lwPeekX" title="Close">✕</button>
    </div>
    <div class="sd-peek-row"><span>now</span><b>${escapeHtml(act.busy ? (act.what || act.state) : "idle")}</b></div>
    <div class="sd-peek-row"><span>wants</span><b>${escapeHtml(String(agent.wants || "—"))}</b></div>
    <div class="sd-peek-row"><span>mood</span>${lwMeter("confidence", mood.confidence)}</div>
    <div class="sd-peek-row"><span>session</span><b>${u.asleep ? "resting" : `${u.used || 0}/${u.cap || "?"}`}</b></div>
    <button class="rp-mini sd-peek-open" id="lwPeekOpenPage">Open full page ⤢</button>`;
  el.hidden = false;
  $("#lwPeekX").addEventListener("click", lwPeekClose);
  $("#lwPeekOpenPage").addEventListener("click", () => { lwPeekClose(); openAgentPage(agent.id); });
}
