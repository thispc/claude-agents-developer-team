// agent.js — an agent's decision history, and nothing else competing with it.
//
// The first version of this page had five tabs. Root's verdict was blunt and correct: a tab
// strip is a filing cabinet, and what this page is FOR is exploring how an agent got where it
// is. Genome, mood, bonds — real, but they are facts you glance at, not a place you spend
// time, so they live behind an info button next to the name.
//
// What is left is the graph, full page, and an inspector that answers the question a node
// raises: what did this decision TEACH it? That link is the whole idea. A tree records what
// happened; a cache records what to do next time; connecting them means the next time this
// agent meets a situation of the same shape, it can walk a path somebody already walked
// instead of thinking it through again from nothing.

let agId = 0;
let agData = null;
let agTimer = null;
let agSel = 0;          // the decision node being inspected
let agFilter = "all";   // all | pivots | learned | bad

/** Leave the page and put the Studio back together. */
function agLeave() {
  if (agTimer) { clearInterval(agTimer); agTimer = null; }
  $("#agentPage").hidden = true;
  if (typeof openStudio === "function") openStudio();
  else setHash("#/studio");
}

async function openAgentPage(hid, _tab, skipHash) {
  agId = Number(hid); agSel = 0; agFilter = "all";
  hideScreens("#agentPage");
  const bar = $("#projectBar"); if (bar) bar.hidden = true;
  $("#agentPage").hidden = false;
  if (!skipHash) setHash(`#/agent/${agId}`);
  $("#agentBody").innerHTML = `<p class="dim">reading them…</p>`;
  await agRefresh();
  if (agTimer) clearInterval(agTimer);
  // Poll the header only, and never while a node is being read: repainting the graph under
  // the cursor clears the selection and blanks the inspector mid-sentence.
  agTimer = setInterval(() => {
    if ($("#agentPage").hidden) { clearInterval(agTimer); agTimer = null; return; }
    if (!agSel) agRefresh();
  }, 6000);
}

async function agRefresh() {
  try {
    if (typeof lwWorldId === "undefined" || !lwWorldId) await agEnsureWorld();
    try {
      agData = await api(`/api/lw/${lwWorldId}/human/${agId}`);
    } catch {
      await agEnsureWorld();
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
 * one" is a guess that is wrong half the time. Ask each until one admits to this agent. */
async function agEnsureWorld() {
  const list = await api("/api/lw");
  const worlds = list.worlds || [];
  if (!worlds.length) throw new Error("no world yet — open the Studio once");
  for (const w of worlds) {
    try {
      await api(`/api/lw/${w.id}/human/${agId}`);
      // The LEXICAL binding, not a window property: `let lwWorldId` in a classic script
      // makes no window slot, so `window.lwWorldId = x` writes to a second, unread one.
      lwWorldId = w.id;
      return;
    } catch { /* not this one */ }
  }
  throw new Error("no world has that agent any more");
}

const AG_FILTERS = [
  { id: "all", label: "everything" },
  { id: "pivots", label: "pivots" },
  { id: "learned", label: "taught it something" },
  { id: "bad", label: "went wrong" },
];

function agVisible(d) {
  const nodes = d.decisions || [];
  const taught = new Set((d.associations || []).flatMap((a) => a.from_decisions || []));
  if (agFilter === "pivots") return nodes.filter((n) => n.canon);
  if (agFilter === "bad") return nodes.filter((n) => n.outcome === "bad" || n.stale);
  if (agFilter === "learned") return nodes.filter((n) => taught.has(n.id));
  return nodes;
}

function agHtml(d) {
  const h = d.human || {};
  const act = d.activity || {};
  const nodes = agVisible(d);
  const assoc = d.associations || [];
  const known = assoc.filter((a) => a.confidence >= 0.5 && a.evidence >= 2).length;
  return `
  <div class="ag-id">
    <div class="fig-emblem lw-av-emblem"><img alt="" src="${lwSvgUri(lwAvatarSvg(lwAvatarSeed({ name: h.name, id: agId, figure: h.figure }), 56))}"></div>
    <div class="ag-id-main">
      <h2>${escapeHtml(h.name || "someone")}
        <button class="ag-info" id="agInfo" title="Who it is — genome, mood, bonds" aria-label="About this agent">i</button></h2>
      <p class="sd-persona">${escapeHtml(d.narrative || h.narrative || "no story yet")}</p>
    </div>
    <div class="sd-facts">
      <span>${act.busy ? "● " + escapeHtml(act.what || act.state) : "idle"}</span>
      <span>${(d.decisions || []).length} decisions</span>
      <span>${known} thing${known === 1 ? "" : "s"} it knows</span>
    </div>
  </div>
  <div class="ag-makeup" id="agMakeup" hidden>${agMakeupHtml(d, h)}</div>

  <div class="ag-filters">
    ${AG_FILTERS.map((f) => `<button class="rp-fchip${f.id === agFilter ? " on" : ""}" data-agfilter="${f.id}">${escapeHtml(f.label)}</button>`).join("")}
    <span class="ag-legend"><span><i class="ag-k canon"></i>pivot</span><span><i class="ag-k good"></i>held up</span>
      <span><i class="ag-k bad"></i>did not</span><span><i class="ag-k open"></i>open</span></span>
  </div>

  <div class="ag-explore">
    <div class="ag-graph">${nodes.length
      ? lwTreeHtml(nodes, d.canon)
      : `<p class="dim">Nothing here yet under this filter. Decisions appear as soon as it acts
         on something, and each one carries the causes we already had.</p>`}</div>
    <aside class="ag-inspect" id="agInspect">${agInspectHtml(d)}</aside>
  </div>
  ${d.withheld ? `<p class="hint">${d.withheld} private decision${d.withheld === 1 ? " is" : "s are"} withheld — only root may read an agent's private reasoning.</p>` : ""}`;
}

/** The inspector. With nothing selected it is the agent's whole knowledge, which is the
 * honest default: the beliefs are what all those nodes were FOR. */
function agInspectHtml(d) {
  const n = (d.decisions || []).find((x) => x.id === agSel);
  if (!n) return agKnowledgeHtml(d);
  const assoc = (d.associations || []).find((a) => (a.from_decisions || []).includes(n.id));
  const why = Object.entries(n.because || {}).filter(([, v]) => v !== "" && v !== null)
    .map(([k, v]) => `<span class="rp-lf">${escapeHtml(k)}=${escapeHtml(String(v))}</span>`).join(" ");
  return `
    <div class="ag-ins-head">
      <span class="rp-sev ${n.canon ? "critical" : n.outcome === "bad" ? "warning" : ""}">${
        n.canon ? "pivot" : n.stale ? "stale" : (n.outcome || "open")}</span>
      ${n.sig ? `<code class="lw-as-sig">${escapeHtml(n.sig)}</code>` : ""}
      <button class="rp-link" id="agInsClose">clear</button>
    </div>
    <div class="ag-ins-body">
      <div><b>saw</b> ${escapeHtml(n.saw || "")}</div>
      <div><b>made of it</b> ${escapeHtml(n.understood || "")}</div>
      <div><b>chose</b> ${escapeHtml(n.chose || "")}</div>
      <div class="ag-ins-why">${why}</div>
      ${n.parents && n.parents.length ? `<div class="dim">follows #${n.parents.join(", #")}</div>` : ""}
    </div>
    ${assoc ? `<div class="ag-taught">
      <div class="sd-label">What it took from this</div>
      <p class="ag-taught-says">${escapeHtml(assoc.says || "")}</p>
      <p class="dim">${assoc.evidence < 2
        ? "not trusted yet — one outcome is a coincidence"
        : `${Math.round(assoc.confidence * 100)}% across ${assoc.evidence} times, keyed on ${escapeHtml(assoc.sig)}`}</p>
      ${(assoc.from_decisions || []).length > 1
        ? `<p class="dim">built from #${assoc.from_decisions.join(", #")} — click one to follow the chain</p>` : ""}
    </div>` : `<p class="dim ag-taught">This one taught it nothing yet. A decision only becomes
      knowledge when its outcome is known.</p>`}`;
}

/** Everything the agent would recall, keyed by the shape of the situation. This is the
 * compiled end of the same material the graph shows raw. */
function agKnowledgeHtml(d) {
  const rows = d.associations || [];
  return `
    <div class="ag-ins-head"><b>What it knows</b>
      <span class="dim">recalled before it thinks — a hit costs nothing</span></div>
    ${rows.length ? `<div class="lw-assoc">${rows.map((a) => `
      <button class="lw-as${a.confidence >= 0.5 && a.evidence >= 2 ? " live" : " weak"}"
              data-agsig="${escapeHtml(String((a.from_decisions || [])[0] || 0))}">
        <code class="lw-as-sig">${escapeHtml(a.sig)}</code>
        <span class="lw-as-says">${escapeHtml(a.says || "")}</span>
        <span class="lw-as-meta">${a.evidence < 2 ? "unproven" : `${Math.round(a.confidence * 100)}% · ${a.evidence}×`}</span>
      </button>`).join("")}</div>`
      : `<p class="dim">Nothing proven yet. A belief needs two agreeing outcomes before it is
         allowed to influence anything — one coincidence is superstition.</p>`}`;
}

function agMakeupHtml(d, h) {
  const traits = h.traits || {};
  const mood = h.mood || {};
  const bonds = (d.bonds && typeof d.bonds === "object") ? d.bonds : {};
  const habits = d.habits || [];
  return `
  <div class="ag-grid">
    <div class="rp-card"><div class="rp-card-h">Genome <span class="dim">how it reacts</span></div>
      <div class="lw-meters">${Object.entries(traits).map(([k, v]) => lwMeter(k, v)).join("")
        || `<p class="dim">neutral on everything</p>`}</div></div>
    <div class="rp-card"><div class="rp-card-h">Mood</div>
      <div class="lw-meters">${lwMeter("confidence", mood.confidence)}${lwMeter("stress", mood.stress)}
        ${lwMeter("hope", mood.hope)}${lwMeter("focus", mood.focus)}</div></div>
  </div>
  ${habits.length ? `<div class="rp-card"><div class="rp-card-h">Compiled reflexes
      <span class="dim">it no longer pays to think about these</span></div>
    <div class="lw-habits">${habits.map((hb) => `<div class="lw-habit">
      <span class="lw-habit-when">when ${lwHabitWhen(hb.when)}</span>
      <span class="lw-habit-meta">conf ${lwPct(hb.confidence)} · fired ${escapeHtml(String(hb.fires ?? 0))}×</span>
    </div>`).join("")}</div></div>` : ""}
  ${Object.keys(bonds).length ? `<div class="rp-card"><div class="rp-card-h">Bonds</div>
    <div class="lw-bonds">${Object.entries(bonds).map(([oid, b]) => `<div class="lw-bond">
      <span class="lw-bond-name">${escapeHtml(typeof lwNameOf === "function" ? lwNameOf(oid) : String(oid))}</span>
      <span class="lw-bond-meta">trust ${lwPct(b && b.trust)} · warmth ${lwPct(b && b.warmth)}</span>
    </div>`).join("")}</div></div>` : ""}
  ${lwAgentLogsHtml(d.logs)}`;
}

function agWire() {
  const body = $("#agentBody");
  const info = $("#agInfo");
  if (info) info.addEventListener("click", () => {
    const box = $("#agMakeup");
    if (box) { box.hidden = !box.hidden; info.classList.toggle("on", !box.hidden); }
  });
  body.querySelectorAll("[data-agfilter]").forEach((b) => b.addEventListener("click", () => {
    agFilter = b.dataset.agfilter; agSel = 0;
    body.innerHTML = agHtml(agData); agWire();
  }));
  // Selecting a node re-renders only the inspector, so the graph does not jump under you.
  body.querySelectorAll("[data-dnode]").forEach((g) => g.addEventListener("click", () => {
    agSel = Number(g.dataset.dnode);
    body.querySelectorAll("[data-dnode]").forEach((x) => x.classList.toggle("sel", x === g));
    $("#agInspect").innerHTML = agInspectHtml(agData);
    agWireInspect();
  }));
  agWireInspect();
  const back = $("#agBack");
  // Not history.back(): the previous entry may be another agent, or a page from before the
  // Studio existed, and either way the canvas is left hidden and half-torn-down — which is
  // exactly what "the scene page is messed up" was.
  if (back) back.onclick = () => agLeave();
}

function agWireInspect() {
  const close = $("#agInsClose");
  if (close) close.addEventListener("click", () => {
    agSel = 0;
    $("#agentBody").querySelectorAll("[data-dnode]").forEach((x) => x.classList.remove("sel"));
    $("#agInspect").innerHTML = agInspectHtml(agData);
    agWireInspect();
  });
  // Clicking a belief jumps to the decision that produced it — the cache back to the tree,
  // which is the direction you want when the question is "why do you think that?".
  $("#agentBody").querySelectorAll("[data-agsig]").forEach((b) => b.addEventListener("click", () => {
    const did = Number(b.dataset.agsig);
    if (!did) return;
    agSel = did;
    $("#agInspect").innerHTML = agInspectHtml(agData);
    agWireInspect();
    const node = $("#agentBody").querySelector(`[data-dnode="${did}"]`);
    if (node) { node.classList.add("sel"); node.scrollIntoView({ block: "center", inline: "center" }); }
  }));
}
