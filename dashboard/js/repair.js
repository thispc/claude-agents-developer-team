// repair.js — self-repair v2: the IT crew. One button; the crew sprints on the platform itself,
// with usage limits always on screen and a manager that sleeps the crew when Claude's limits say so.
// Owns renderSelf() (the #selfPage screen) — moved out of the legacy projects.js quarantine.
// Classic script, one shared global scope; index.html defines load order (after canvas1, before boot).

let rpTimer = null;
let rpChatOpen = false;

async function renderSelf() {
  const el = $("#self");
  el.innerHTML = `<p class="dim">reading the crew's state…</p>`;
  await rpRefresh(true);
  if (rpTimer) clearInterval(rpTimer);
  rpTimer = setInterval(() => {
    if ($("#selfPage").hidden) { clearInterval(rpTimer); rpTimer = null; return; }
    rpRefresh(false);
  }, 5000);
}

async function rpRefresh(force) {
  let d;
  try { d = await api("/api/repair/status"); } catch (e) {
    $("#self").innerHTML = `<div class="self-banner">Could not read self-repair: ${escapeHtml(e.message)}</div>`;
    return;
  }
  const el = $("#self");
  const sig = JSON.stringify([d.enabled, d.state, d.meters && d.meters.s5h, d.meters && d.meters.w7d,
    d.factors, d.sprint && d.sprint.tasks && d.sprint.tasks.map((t) => t.status), (d.queue || []).length]);
  if (!force && el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  el.innerHTML = rpHtml(d);
  rpWire(d);
}

// ---- render ---------------------------------------------------------------

function rpPhaseLine(d) {
  const st = d.state || {};
  if (!d.enabled) return "Off — the crew is standing by.";
  if (st.phase === "sleeping") {
    const when = st.sleep_until ? new Date(st.sleep_until * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "…";
    return `😴 Sleeping until ${when} — ${st.sleep_reason || "resting"}`;
  }
  if (st.phase === "restarting") return "⟳ Restarting to apply landed changes…";
  const t = d.sprint && d.sprint.tasks && d.sprint.tasks[st.task_idx];
  const doing = { idle: "waiting for headroom", scout: "scouting the repo", plan: "the crew is deliberating the plan",
                  build: t ? `building: ${t.title}` : "building", verify: t ? `verifying: ${t.title}` : "running the suite",
                  land: "landing", retro: "writing the retro" }[st.phase] || st.phase;
  return `● Sprint ${st.sprint_no || "…"} — ${escapeHtml(doing)}`;
}

function rpBar(m) {
  const frac = Math.min(1, m.used / Math.max(1, m.cap));
  const color = frac >= 1 ? "#b23b3b" : frac >= 0.85 ? "#c9721f" : frac >= 0.6 ? "#c9a11f" : "#3F7A3F";
  return `<span class="rp-bar"><span class="rp-bar-fill" style="width:${Math.round(frac * 100)}%;background:${color}"></span></span>`;
}

function rpHtml(d) {
  const m = d.meters || { s5h: { used: 0, cap: 1 }, w7d: { used: 0, cap: 1 }, cooldowns: {}, team: [] };
  const cools = Object.entries(m.cooldowns || {}).map(([mod, s]) =>
    `<span class="rp-cool">${escapeHtml(mod.replace("claude-", ""))} cooling · ${Math.ceil(s / 60)}m</span>`).join("");
  const factors = (d.factors || []).map((f) =>
    `<button class="rp-chip${f.enabled ? " on" : ""}" data-fid="${escapeHtml(f.id)}" title="${escapeHtml(f.brief)}">${escapeHtml(f.name)}</button>`).join("");
  const team = (m.team || []).map((a) => {
    const u = a.usage || {}; return `<span class="rp-mate${u.asleep ? " asleep" : ""}" title="${escapeHtml(a.factor)} · ${u.used || 0}/${u.cap || "?"} session uses">
      ${escapeHtml(a.name)}${u.asleep ? " 😴" : ""} ${rpBar({ used: u.used || 0, cap: u.cap || 1 })}</span>`;
  }).join("") || `<span class="dim">the crew forms when the first sprint starts</span>`;
  const tasks = ((d.sprint && d.sprint.tasks) || []).map((t) => {
    const icon = { pending: "○", building: "🔨", verifying: "🧪", green: "✓", landed: "✓", queued: "⏸", failed: "✕", aborted: "✕" }[t.status] || "○";
    const extra = t.status === "landed" && t.landed_sha
      ? ` <button class="rp-mini" data-revert="${escapeHtml(t.landed_sha)}">↩ revert</button>`
      : (t.verification && t.verification.headline ? ` <span class="dim">${escapeHtml(trim(t.verification.headline, 60))}</span>` : "");
    return `<div class="rp-task s-${escapeHtml(t.status)}"><span class="rp-task-ico">${icon}</span>
      <span class="rp-task-t">[${escapeHtml(t.factor || "?")}] ${escapeHtml(t.title)}</span>${extra}</div>`;
  }).join("") || `<p class="dim">no sprint yet — flip the switch</p>`;
  const queue = (d.queue || []).map((q) =>
    `<div class="rp-task s-queued"><span class="rp-task-ico">⏸</span>
      <span class="rp-task-t">${escapeHtml(q.title)}${q.note ? ` <span class="dim">(${escapeHtml(q.note)})</span>` : ""}</span>
      <button class="rp-mini" data-approve="${escapeHtml(q.branch)}">✓ approve</button>
      <button class="rp-mini danger" data-discard="${escapeHtml(q.branch)}">✕ discard</button></div>`).join("");
  const err = d.last_error && d.enabled
    ? `<div class="rp-err">last error · ${escapeHtml(d.last_error.phase || "?")}: ${escapeHtml(trim(d.last_error.detail || "", 140))}</div>` : "";

  return `
  <div class="rp-card rp-head-card${d.enabled && d.state.phase !== "sleeping" ? " rp-live" : ""}">
    <div class="rp-switchrow">
      <label class="seg rp-seg">
        <input type="radio" name="rpOn" value="off" ${d.enabled ? "" : "checked"}><span>Off</span>
      </label>
      <label class="seg rp-seg">
        <input type="radio" name="rpOn" value="on" ${d.enabled ? "checked" : ""}><span>Self-repairing</span>
      </label>
      <span class="rp-phase" id="repairPhase">${rpPhaseLine(d)}</span>
      ${d.enabled ? `<button class="rp-mini danger" id="rpAbort" title="Abort the current task">■ abort task</button>` : ""}
    </div>
    ${err}
  </div>

  <div class="rp-card">
    <div class="rp-card-h">Usage <span class="dim">every model call, not just the big ones — the manager sleeps before these run dry</span></div>
    <div class="rp-meterrow"><span class="rp-meter-lb">calls (5h)</span>${rpBar(m.s5h)}<span class="rp-meter-n">${m.s5h.used}/${m.s5h.cap}${m.s5h.usd ? ` · $${m.s5h.usd.toFixed(2)}` : ""}</span></div>
    <div class="rp-meterrow"><span class="rp-meter-lb">week</span>${rpBar(m.w7d)}<span class="rp-meter-n">${m.w7d.used}/${m.w7d.cap}${m.w7d.usd ? ` · $${m.w7d.usd.toFixed(2)}` : ""}</span></div>
    ${cools ? `<div class="rp-coolrow">${cools}</div>` : ""}
    ${(d.backlog || []).length ? `<div class="rp-coolrow"><span class="rp-cool rp-backlog">${(d.backlog || []).length} planned ahead — next sprints build without re-planning</span></div>` : ""}
  </div>

  <div class="rp-card">
    <div class="rp-card-h">Factors <span class="dim">what the crew cares about — click to toggle</span></div>
    <div class="rp-chips" id="repairFactors">${factors}</div>
    <div class="rp-addrow">
      <input id="rpNewFactor" placeholder="add a factor, e.g. accessibility" maxlength="40">
      <input id="rpNewBrief" placeholder="what its specialist hunts for" maxlength="200">
      <button class="rp-mini" id="rpAddFactor">+ add</button>
    </div>
  </div>

  <div class="rp-card">
    <div class="rp-card-h">The crew <span class="dim">one specialist per factor + a hidden manager</span>
      ${d.world ? `<a class="rp-link" href="#/studio">Open in Studio ↗</a>` : ""}</div>
    <div class="rp-team" id="repairTeam">${team}</div>
    <div class="rp-chat" id="repairChat">
      <div class="rp-chat-log" id="repairChatLog"${rpChatOpen ? "" : " hidden"}></div>
      <form id="repairChatForm"><input id="repairChatText" placeholder="Message the manager…" autocomplete="off">
        <button class="rp-mini">Send</button></form>
    </div>
  </div>

  <div class="rp-card">
    <div class="rp-card-h">Sprint ${d.sprint ? d.sprint.no : "—"} <span class="dim">${d.sprint ? escapeHtml(d.sprint.retro || "") : ""}</span></div>
    <div id="repairFeed">${tasks}</div>
    ${queue ? `<div class="rp-card-h rp-qh">Review queue</div><div id="repairQueue">${queue}</div>` : ""}
  </div>

  <div class="rp-card">
    <div class="rp-card-h">Point the crew at something specific <span class="dim">a hand-written ticket, routed through the projects flow</span></div>
    <div class="rough-row">
      <textarea id="roughIssue" rows="2" placeholder="Rough words: what's wrong, or what should be better?"></textarea>
      <button type="button" id="refineBtn">✨ Draft the ticket</button>
    </div>
    <div id="refineNote" class="hint"></div>
    <form id="selfIssueForm">
      <label>Title <input name="title" required maxlength="140"></label>
      <label>Detail <textarea name="body" rows="3"></textarea></label>
      <div class="rp-formrow">
        <label>Severity <select name="severity"><option>normal</option><option>high</option><option>low</option></select></label>
        <label>Sprints <input name="sprints" type="number" min="1" max="10" value="1"></label>
        <button class="primary">File it</button>
      </div>
      <div id="triageBox" class="hint"></div>
      <div id="selfErr" class="form-error"></div>
    </form>
  </div>

  <div class="rp-card">
    <div class="rp-card-h">Up next <span class="dim">the planned backlog — drained one sprint at a time</span></div>
    <div id="repairBacklog">${(d.backlog || []).map((t) =>
      `<div class="rp-task"><span class="rp-task-ico">·</span><span class="rp-task-t">[${escapeHtml(t.factor || "?")}] ${escapeHtml(t.title)}</span></div>`).join("")
      || `<p class="dim">empty — the crew will scout and plan a fresh backlog next sprint</p>`}</div>
  </div>

  <div class="rp-card">
    <div class="rp-card-h">History</div>
    <div id="repairHistory"><p class="dim">…</p></div>
  </div>`;
}

// ---- wiring ---------------------------------------------------------------

function rpWire(d) {
  const el = $("#self");
  el.querySelectorAll('input[name="rpOn"]').forEach((r) => r.addEventListener("change", async () => {
    try { await api("/api/repair/toggle", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on: r.value === "on" }) }); }
    catch (e) { toast(`Could not toggle: ${e.message}`); }
    rpRefresh(true);
  }));
  const abort = $("#rpAbort");
  if (abort) abort.addEventListener("click", async () => {
    if (!confirm("Abort the task the crew is building right now?")) return;
    try { await api("/api/repair/abort", { method: "POST" }); } catch (e) { toast(e.message); }
    rpRefresh(true);
  });
  el.querySelectorAll("[data-fid]").forEach((b) => b.addEventListener("click", async () => {
    const f = (d.factors || []).find((x) => x.id === b.dataset.fid);
    try { await api("/api/repair/factors", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ set: [{ id: b.dataset.fid, enabled: !(f && f.enabled) }] }) }); }
    catch (e) { toast(e.message); }
    rpRefresh(true);
  }));
  const add = $("#rpAddFactor");
  if (add) add.addEventListener("click", async () => {
    const name = $("#rpNewFactor").value.trim(); if (!name) return;
    try { await api("/api/repair/factors", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ add: { name, brief: $("#rpNewBrief").value.trim() } }) }); }
    catch (e) { toast(e.message); }
    rpRefresh(true);
  });
  el.querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", async () => {
    try { await api("/api/repair/queue/approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ branch: b.dataset.approve }) }); toast("Landed."); }
    catch (e) { toast(`Could not land: ${e.message}`); }
    rpRefresh(true);
  }));
  el.querySelectorAll("[data-discard]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Discard this change?")) return;
    try { await api("/api/repair/queue/discard", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ branch: b.dataset.discard }) }); }
    catch (e) { toast(e.message); }
    rpRefresh(true);
  }));
  el.querySelectorAll("[data-revert]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Revert this landed change?")) return;
    try { await api("/api/repair/revert", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sha: b.dataset.revert }) }); toast("Reverted."); }
    catch (e) { toast(`Could not revert: ${e.message}`); }
    rpRefresh(true);
  }));

  // the manager chat
  const cform = $("#repairChatForm");
  if (cform) cform.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const inp = $("#repairChatText"), text = inp.value.trim(); if (!text) return;
    inp.value = ""; rpChatOpen = true;
    const log = $("#repairChatLog"); log.hidden = false;
    log.insertAdjacentHTML("beforeend", `<div class="rp-msg me">${escapeHtml(text)}</div><div class="rp-msg them dim">…</div>`);
    log.scrollTop = log.scrollHeight;
    try {
      const r = await api("/api/repair/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
      const last = (r.chat || []).slice(-1)[0];
      log.lastElementChild.outerHTML = `<div class="rp-msg them">${escapeHtml(last ? last.text : "…")}</div>`;
    } catch (e) { log.lastElementChild.outerHTML = `<div class="rp-msg them">${escapeHtml(e.message)}</div>`; }
    log.scrollTop = log.scrollHeight;
  });

  // the migrated manual ticket (pinned behavior: refine → d.refined note → file via projects flow)
  const refine = $("#refineBtn");
  if (refine) refine.addEventListener("click", async () => {
    const rough = $("#roughIssue").value.trim();
    const note = $("#refineNote");
    if (rough.length < 8) { note.textContent = "Say a little more first."; return; }
    refine.disabled = true; refine.textContent = "drafting…";
    try {
      const d2 = await api("/api/self/refine", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rough }) });
      const form = $("#selfIssueForm");
      form.querySelector("[name=title]").value = d2.title || "";
      form.querySelector("[name=body]").value = d2.body || "";
      if (d2.severity) form.querySelector("[name=severity]").value = d2.severity;
      note.textContent = d2.refined
        ? "Drafted by the team's model — edit anything before filing."
        : "No model available — these are just your own words, tidied.";
    } catch (e) { note.textContent = `Could not draft: ${e.message}`; }
    refine.disabled = false; refine.textContent = "✨ Draft the ticket";
  });
  // the triage verdict, previewed while writing — filing a RESTRICTED ticket blind is the
  // outcome this exists to prevent
  let triageT = null;
  const previewTriage = () => {
    clearTimeout(triageT);
    triageT = setTimeout(async () => {
      const form2 = $("#selfIssueForm"), box = $("#triageBox");
      if (!form2 || !box) return;
      const title = form2.querySelector("[name=title]").value.trim();
      if (title.length < 4) { box.textContent = ""; return; }
      try {
        const t = await api("/api/self/triage", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, body: form2.querySelector("[name=body]").value }) });
        box.textContent = t.tier ? `Triage: ${t.tier} — ${t.reason || ""}` : "";
      } catch { box.textContent = ""; }
    }, 700);
  };
  const formEl = $("#selfIssueForm");
  if (formEl) {
    formEl.querySelector("[name=title]").addEventListener("input", previewTriage);
    formEl.querySelector("[name=body]").addEventListener("input", previewTriage);
  }
  const form = $("#selfIssueForm");
  if (form) form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(form);
    try {
      const r = await api("/api/self/issue", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: f.get("title"), body: f.get("body"), severity: f.get("severity"),
                               sprints: Number(f.get("sprints")) || 1 }) });
      toast("Filed — the projects team has it.");
      if (r.project_id) openProject(r.project_id, "command");
    } catch (e) { $("#selfErr").textContent = e.message; }
  });

  rpHistory();
}

async function rpHistory() {
  const el = $("#repairHistory"); if (!el) return;
  try {
    const r = await api("/api/repair/sprints?limit=12");
    const rows = (r.sprints || []).slice().reverse().map((s) =>
      `<div class="rp-hist"><b>s${s.no}</b> <span>${escapeHtml(s.retro || "…")}</span>
        <span class="dim">${s.landed || 0} landed · ${s.failed || 0} failed</span></div>`).join("");
    el.innerHTML = rows || `<p class="dim">no sprints yet</p>`;
  } catch { el.innerHTML = `<p class="dim">—</p>`; }
}
