// repair.js — self-repair v2: the IT crew. One button; the crew sprints on the platform itself,
// with usage limits always on screen and a manager that sleeps the crew when Claude's limits say so.
// Owns renderSelf() (the #selfPage screen) — moved out of the legacy projects.js quarantine.
// Classic script, one shared global scope; index.html defines load order (after canvas1, before boot).

let rpTimer = null;
let rpChatOpen = false;
// Which tab is open. Module-level, because the status poll re-renders #self every few seconds
// and a tab that reset itself to "Crew" mid-read would be unusable.
let rpTab = "crew";
let rpActView = "story";     // story = the narrative bus feed | logs = the structured pipeline
let rpLogLevel = "";         // a FLOOR: "warn" means warn AND error
let rpLogQ = "";

async function renderSelf() {
  rpEnsureBoard();
  const el = $("#self");
  el.innerHTML = `<p class="dim">reading the crew's state…</p>`;
  await rpRefresh(true);
  if (rpTimer) clearInterval(rpTimer);
  rpTimer = setInterval(() => {
    if ($("#selfPage").hidden) { clearInterval(rpTimer); rpTimer = null; return; }
    rpRefresh(false);
  }, 5000);
}

/** The board panel is created once and lives OUTSIDE #self: the status poll replaces #self's
 * innerHTML every few seconds, which would otherwise close the board under the reader's
 * hands mid-sentence. */
function rpEnsureBoard() {
  if ($("#rpBoard")) return;
  const wrap = document.createElement("div");
  wrap.id = "rpBoard";
  wrap.className = "rp-board";
  wrap.hidden = true;
  wrap.innerHTML = `<div class="rp-board-in" role="dialog" aria-label="Sprint board">
    <div class="rp-board-h"><b id="rpBoardTitle">Sprint</b>
      <button class="rp-mini" id="rpBoardClose">✕ close</button></div>
    <div id="rpBoardBody" class="rp-board-body"></div></div>`;
  document.body.appendChild(wrap);
  const close = () => { wrap.hidden = true; };
  wrap.addEventListener("click", (ev) => { if (ev.target === wrap) close(); });
  $("#rpBoardClose").addEventListener("click", close);
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !wrap.hidden) close(); });
}

async function rpRefresh(force) {
  let d;
  try { d = await api("/api/repair/status"); } catch (e) {
    $("#self").innerHTML = `<div class="self-banner">Could not read self-repair: ${escapeHtml(reportCaught(e, "rpRefresh"))}</div>`;
    return;
  }
  const el = $("#self");
  const sig = JSON.stringify([d.enabled, d.state, d.meters && d.meters.s5h, d.meters && d.meters.w7d,
    d.meters && d.meters.util && [d.meters.util.owner_tok, d.meters.util.repair_tok, d.meters.util.contended],
    rpTab,
    d.factors, d.sprint && d.sprint.tasks && d.sprint.tasks.map((t) => t.status), (d.queue || []).length,
    d.code && [d.code.head, d.code.needs_restart, d.code.needs_reload],
    d.notices && [d.notices.total, d.notices.critical]]);
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
  // Same three-tier vocabulary as the model-health bars (healthy/strained/throttled) —
  // reference the theme vars so a palette change propagates instead of drifting out of sync.
  const color = frac >= 0.85 ? "var(--bad)" : frac >= 0.6 ? "var(--warn)" : "var(--good)";
  return `<span class="rp-bar"><span class="rp-bar-fill" style="width:${Math.round(frac * 100)}%;background:${color}"></span></span>`;
}

/** Tokens, the way a person reads them. 3018222 is not a number anybody parses at a
 * glance; "3.0M" is. */
function rpTok(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

function rpAgo(s) {
  if (!s || s >= 1e8) return "";
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

/** Utilization — the signal the sleep decision actually turns on.
 *
 * In TOKENS, never dollars: on a Max subscription there is no bill, so "$3.67" answers no
 * question the owner has, while "18% of this window" is the same thing they can act on. One
 * bar split between your work and the crew's, because the question is not how much each spent
 * but how much of the window is left — and that only reads at a glance on a shared 100%. */
function rpUtilHtml(u) {
  if (!u) return "";
  const pct = (v) => Math.max(0, Math.min(100, Math.round(v * 100)));
  const mine = pct(u.owner_tok / Math.max(1, u.budget_tok));
  const crew = pct(u.repair_tok / Math.max(1, u.budget_tok));
  const quiet = rpAgo(u.quiet_s);
  const verdict = u.contended
    ? `<span class="rp-cool">you are using it — the crew waits</span>`
    : u.allowance_tok <= u.repair_tok
      ? `<span class="rp-cool">the crew has used its share of this window</span>`
      : `<span class="rp-cool rp-backlog">${pct(u.idle_frac)}% idle — the crew may use it</span>`;
  return `<div class="rp-meterrow"><span class="rp-meter-lb">window (${u.window_h}h)</span>
      <span class="rp-bar" title="you ${mine}% · the crew ${crew}%">
        <span class="rp-bar-fill rp-you" style="width:${mine}%"></span>
        <span class="rp-bar-fill rp-crew" style="width:${crew}%;left:${mine}%"></span></span>
      <span class="rp-meter-n">${rpTok(u.used_tok)} / ${rpTok(u.budget_tok)} tokens</span></div>
    <div class="rp-coolrow">${verdict}
      <span class="dim">you ${rpTok(u.owner_tok)}${quiet ? ` (last ${quiet})` : ""}
        · crew ${rpTok(u.repair_tok)} of ${rpTok(u.allowance_tok)} allowed</span></div>`;
}

// ---- the panels -----------------------------------------------------------

const RP_TABS = [
  { id: "notices", label: "Notices" },
  { id: "agents", label: "Agents" },
  { id: "crew", label: "Crew" },
  { id: "board", label: "Board" },
  { id: "activity", label: "Activity" },
  { id: "usage", label: "Usage" },
  { id: "command", label: "Command" },
];

/** The monitoring inbox: what the logs add up to, and what to do about it.
 *
 * Projects have blockers — "why is this not moving?" — and self-repair had nothing of the
 * kind: the answer lived in 3000 log rows nobody reads. Each notice carries its evidence and,
 * where there is an obvious next move, a proposal. The proposal is never taken on its own;
 * Approve is the whole point of the screen. */
function rpNoticesPanel(d) {
  return `
  <div class="rp-card">
    <div class="rp-card-h">Needs you <span class="dim">what the platform noticed about itself</span>
      <button class="rp-link" id="rpNoticesReload">refresh</button></div>
    <label class="rp-autorow" id="rpAutoRow">
      <input type="checkbox" id="rpAuto">
      <span><b>Approve the additive ones for me</b>
        <span class="dim" id="rpAutoWhat">filing bugs and nudging capped knobs happen without asking;
          anything that STOPS work — pausing the crew, aborting a task — always waits for you,
          because a rule misfiring at 3am must not be able to switch the crew off overnight</span></span>
    </label>
    <div id="repairNotices"><p class="dim">…</p></div>
  </div>`;
}

function rpNoticeHtml(n) {
  const when = n.since ? new Date(n.since * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) : "";
  const ev = (n.evidence || []).map((e) =>
    `<div class="rp-actline quiet"><span class="rp-lcat">${escapeHtml(e.cat || "")}/${escapeHtml(e.event || "")}</span>
      <span>${escapeHtml(trim(String(e.msg || ""), 150))}</span></div>`).join("");
  const decided = (n.decision || {}).state;
  return `<div class="rp-notice s-${escapeHtml(n.severity)}${decided ? " decided" : ""}">
    <div class="rp-notice-h">
      <span class="rp-sev ${escapeHtml(n.severity)}">${escapeHtml(n.severity)}</span>
      <b>${escapeHtml(n.title)}</b>
      ${n.count > 1 ? `<span class="dim">×${n.count}</span>` : ""}
      <span class="dim rp-notice-when">${escapeHtml(when)}</span>
    </div>
    <div class="rp-notice-b">${escapeHtml(n.detail)}</div>
    ${n.proposal ? `<div class="rp-proposal"><b>Proposal</b> ${escapeHtml(n.proposal)}</div>` : ""}
    ${decided ? `<div class="rp-notice-b dim">${escapeHtml(decided)}${(n.decision.note ? " — " + n.decision.note : "")}</div>` : `
    <div class="rp-notice-actions">
      <button class="rp-mini" data-approve-fp="${escapeHtml(n.fp)}">${n.action ? "✓ Approve" : "✓ Got it"}</button>
      <button class="rp-mini" data-dismiss-fp="${escapeHtml(n.fp)}">Dismiss</button>
    </div>`}
    ${ev ? `<details class="rp-evidence"><summary>evidence</summary>${ev}</details>` : ""}
  </div>`;
}

async function rpNotices() {
  const el = $("#repairNotices"); if (!el) return;
  try {
    const r = await api("/api/logs/notices");
    const auto = $("#rpAuto");
    if (auto) auto.checked = !!r.auto;
    el.innerHTML = (r.notices || []).map(rpNoticeHtml).join("")
      || `<p class="dim">Nothing needs you. The platform has been behaving.</p>`;
    el.querySelectorAll("[data-approve-fp]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        const out = await api("/api/logs/notices/approve", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fp: b.dataset.approveFp }) });
        toast(out.did ? `Done — ${out.did}` : (out.reason || "done"));
      } catch (e) { toast(e.message); }
      rpNotices(); rpRefresh(true);
    }));
    el.querySelectorAll("[data-dismiss-fp]").forEach((b) => b.addEventListener("click", async () => {
      try {
        await api("/api/logs/notices/dismiss", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fp: b.dataset.dismissFp }) });
      } catch (e) { toast(e.message); }
      rpNotices();
    }));
  } catch (e) { el.innerHTML = `<p class="err">${escapeHtml(reportCaught(e, "repair"))}</p>`; }
}

/** Who is actually working, from the register — the same question the projects Agents tab
 * answers, asked of the crew. "Six specialists exist" is org chart; "two are building and one
 * is asleep on its cap" is what you came to find out. */
function rpAgentsPanel(d) {
  return `
  <div class="rp-card">
    <div class="rp-card-h">Agents <span class="dim" id="rpAgentsNote">who is working right now</span>
      <button class="rp-link" id="rpAgentsReload">refresh</button></div>
    <div id="repairAgents"><p class="dim">…</p></div>
  </div>`;
}

const RP_STATE_ICON = { idle: "○", thinking: "🧠", speaking: "💬", building: "🔨",
                        verifying: "🧪", asleep: "😴" };

async function rpAgents() {
  const el = $("#repairAgents"); if (!el) return;
  try {
    const r = await api("/api/logs/agents");
    const rows = r.agents || [];
    const s = r.summary || {};
    const note = $("#rpAgentsNote");
    if (note) note.textContent = `${s.busy || 0} of ${s.total || 0} working`;
    el.innerHTML = rows.length ? rows.map((a) => {
      const mins = Math.round((a.for_s || 0) / 60);
      return `<div class="rp-agent${a.busy ? " on" : ""}">
        <span class="rp-agent-ico">${RP_STATE_ICON[a.state] || "○"}</span>
        <span class="rp-agent-name">${escapeHtml(a.name || a.key)}</span>
        <span class="rp-agent-state">${escapeHtml(a.state)}${a.busy && mins ? ` · ${mins}m` : ""}</span>
        <span class="rp-agent-what dim">${escapeHtml(trim(a.what || a.means || "", 70))}</span>
        <span class="dim">${escapeHtml(a.where || "")}</span>
        <button class="rp-link" data-agentlogs="${escapeHtml(a.name || "")}">logs</button>
      </div>`;
    }).join("") : `<p class="dim">Nobody has done anything yet. Agents appear here the moment
      they are asked to.</p>`;
    el.querySelectorAll("[data-agentlogs]").forEach((b) => b.addEventListener("click", () =>
      rpAgentLogs(b.dataset.agentlogs)));
  } catch (e) { el.innerHTML = `<p class="err">${escapeHtml(reportCaught(e, "rpAgents"))}</p>`; }
}

/** One agent's own rows out of the pipeline — the reason this tab is worth opening rather
 * than reading the whole log and filtering by eye. */
async function rpAgentLogs(name) {
  const el = $("#repairAgents"); if (!el || !name) return;
  try {
    const r = await api(`/api/logs?q=${encodeURIComponent(name)}&limit=60`);
    el.insertAdjacentHTML("afterbegin",
      `<div class="rp-card rp-agentlogs"><div class="rp-card-h">${escapeHtml(name)}
        <button class="rp-link" id="rpAgentLogsClose">close</button></div>
        <div class="rp-act">${(r.logs || []).map(rpLogLine).join("")
          || `<p class="dim">nothing recorded about them yet</p>`}</div></div>`);
    const x = $("#rpAgentLogsClose");
    if (x) x.addEventListener("click", () => x.closest(".rp-agentlogs").remove());
  } catch (e) { toast(reportCaught(e, "rpAgentLogs")); }
}

function rpCrewPanel(d, m) {
  const factors = (d.factors || []).map((f) =>
    `<button class="rp-chip${f.enabled ? " on" : ""}" data-fid="${escapeHtml(f.id)}" title="${escapeHtml(f.brief)}">${escapeHtml(f.name)}</button>`).join("");
  const team = (m.team || []).map((a) => {
    const u = a.usage || {}; return `<span class="rp-mate${u.asleep ? " asleep" : ""}" title="${escapeHtml(a.factor)} · ${u.used || 0}/${u.cap || "?"} session uses">
      ${escapeHtml(a.name)}${u.asleep ? " 😴" : ""} ${rpBar({ used: u.used || 0, cap: u.cap || 1 })}</span>`;
  }).join("") || `<span class="dim">the crew forms when the first sprint starts</span>`;
  return `
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
      ${d.world ? `<button class="rp-link" id="rpOpenTeam">See the team ↗</button>` : ""}</div>
    <div class="rp-team" id="repairTeam">${team}</div>
    <div class="rp-chat" id="repairChat">
      <div class="rp-chat-log" id="repairChatLog"><p class="dim">…</p></div>
      <form id="repairChatForm"><input id="repairChatText" placeholder="Ask the manager anything — what it is working on, why it slept…" autocomplete="off">
        <button class="rp-mini">Send</button></form>
    </div>
  </div>`;
}

// What kind of change, and how much it matters — two different questions that one
// undifferentiated list was answering as neither. "7 things" tells you nothing; "4 bugs, one
// of them P1, 2 features and an idea" is a status you can act on.
const RP_TYPE = { bug: "🐞", feature: "✨", refactor: "♻️", chore: "🧹", idea: "💡" };

function rpTag(t) {
  if (!t || !t.type) return "";
  const p = t.priority || "";
  return `<span class="rp-tag t-${escapeHtml(t.type)}" title="${escapeHtml(t.type)}">${RP_TYPE[t.type] || ""} ${escapeHtml(t.type)}</span>${
    p ? `<span class="rp-tag p-${escapeHtml(p)}">${escapeHtml(p.toUpperCase())}</span>` : ""}`;
}

/** "4 bugs · 2 features · 1 idea" — the shape of a pile of work, at a glance. */
function rpMix(tasks) {
  const by = {};
  for (const t of tasks || []) if (t.type) by[t.type] = (by[t.type] || 0) + 1;
  const p1 = (tasks || []).filter((t) => t.priority === "p1").length;
  const parts = Object.entries(by).sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${n} ${k}${n > 1 && k !== "chore" ? "s" : ""}`);
  if (p1) parts.push(`${p1} at P1`);
  return parts.join(" · ");
}

function rpBoardPanel(d) {
  const tasks = ((d.sprint && d.sprint.tasks) || []).map((t) => {
    const icon = { pending: "○", building: "🔨", verifying: "🧪", green: "✓", landed: "✓", queued: "⏸", failed: "✕", aborted: "✕" }[t.status] || "○";
    const extra = t.status === "landed" && t.landed_sha
      ? ` <button class="rp-mini" data-revert="${escapeHtml(t.landed_sha)}">↩ revert</button>`
      : (t.verification && t.verification.headline ? ` <span class="dim">${escapeHtml(trim(t.verification.headline, 60))}</span>` : "");
    return `<div class="rp-task s-${escapeHtml(t.status)}"><span class="rp-task-ico">${icon}</span>
      ${rpTag(t)}<span class="rp-task-t">${escapeHtml(t.title)}</span>
      <span class="dim">${escapeHtml(t.factor || "")}</span>${extra}</div>`;
  }).join("") || (d.enabled
    ? `<p class="dim">${escapeHtml({ scout: "scouting the repo — nothing planned yet",
        plan: "the crew is deciding what to work on", idle: "waiting for quota" }[(d.state || {}).phase]
        || "no tasks in this sprint yet")}</p>`
    : `<p class="dim">no sprint yet — flip the switch</p>`);
  const queue = (d.queue || []).map((q) => {
    const st = (d.queue_state || {})[q.branch] || { ok: true };
    return `<div class="rp-task s-queued"><span class="rp-task-ico">⏸</span>
      <span class="rp-task-t">${escapeHtml(q.title)}</span>
      ${st.ok ? "" : `<span class="rp-cool">${escapeHtml(st.why)} — ${escapeHtml(st.fix)}</span>`}
      <button class="rp-mini" data-approve="${escapeHtml(q.branch)}"${st.ok ? "" : " disabled"}>✓ approve</button>
      ${st.remedy === "rebuild" ? `<button class="rp-mini" data-rebuild="${escapeHtml(q.branch)}">⟳ rebuild onto main</button>` : ""}
      <button class="rp-mini danger" data-discard="${escapeHtml(q.branch)}">✕ discard</button></div>`;
  }).join("");
  return `
  <div class="rp-card">
    <div class="rp-card-h">Sprint ${d.sprint ? d.sprint.no : "—"} <span class="dim">${d.sprint ? escapeHtml(d.sprint.retro || "") : ""}</span>
      ${d.sprint ? `<button class="rp-link" data-board="${d.sprint.no}">open the board ↗</button>` : ""}</div>
    <div id="repairFeed">${tasks}</div>
    ${queue ? `<div class="rp-card-h rp-qh">Review queue</div><div id="repairQueue">${queue}</div>` : ""}
  </div>

  <div class="rp-card">
    <div class="rp-card-h">Up next <span class="dim">${escapeHtml(rpMix(d.backlog) || "the planned backlog")} — drained one sprint at a time</span></div>
    <div id="repairBacklog">${(d.backlog || []).map((t) =>
      `<div class="rp-task"><span class="rp-task-ico">·</span>${rpTag(t)}
        <span class="rp-task-t">${escapeHtml(t.title)}</span>
        <span class="dim">${escapeHtml(t.factor || "")}</span></div>`).join("")
      || `<p class="dim">empty — the crew will scout and plan a fresh backlog next sprint</p>`}</div>
  </div>

  <div class="rp-card">
    <div class="rp-card-h">Shipped <span class="dim">${escapeHtml(rpMix(d.landed))} — landed on main, so a commit is the reviewable unit; there are no pull requests to wait on</span></div>
    <div id="repairLanded">${(d.landed || []).map((L) => {
      const when = L.at ? new Date(L.at * 1000).toLocaleDateString([], { month: "short", day: "numeric" }) : "";
      const repo = (d.head || {}).repo || "";
      const href = repo ? gitWebUrl(repo, "commit", L.sha) : "";
      return `<div class="rp-task"><span class="rp-task-ico">✓</span>${rpTag(L)}
        <span class="rp-task-t">${escapeHtml(L.title)}</span>
        <span class="dim">s${L.sprint} · ${escapeHtml(when)}</span>
        ${href ? `<a class="rp-link" href="${escapeHtml(href)}" target="_blank" rel="noopener">${escapeHtml(L.sha.slice(0, 8))} ↗</a>`
               : `<code class="dim">${escapeHtml(L.sha.slice(0, 8))}</code>`}
        <button class="rp-mini" data-revert="${escapeHtml(L.sha)}">↩ revert</button></div>`;
    }).join("") || `<p class="dim">nothing landed yet</p>`}</div>
  </div>

  <div class="rp-card">
    <div class="rp-card-h">Your tickets <span class="dim">what you handed the crew from Command — these run as ordinary projects</span></div>
    <div id="repairTickets">${(d.tickets || []).map((t) =>
      `<div class="rp-task s-${escapeHtml(t.status || "")}"><span class="rp-task-ico">·</span>
        <span class="rp-task-t">#${t.seq} ${escapeHtml(t.title)}</span>
        <span class="dim">${escapeHtml(t.status || "")}</span>
        <button class="rp-mini" data-open-project="${t.project_id}">open ↗</button></div>`).join("")
      || `<p class="dim">none yet — the Command tab files them</p>`}</div>
  </div>

  <div class="rp-card">
    <div class="rp-card-h">Every sprint <span class="dim">open one for its board</span></div>
    <div id="repairHistory"><p class="dim">…</p></div>
  </div>`;
}

function rpUsagePanel(d, m) {
  const u = m.util || {};
  const cools = Object.entries(m.cooldowns || {}).map(([mod, s]) =>
    `<span class="rp-cool">${escapeHtml(mod.replace("claude-", ""))} cooling · ${Math.ceil(s / 60)}m</span>`).join("");
  const WHO = { repair: "Self-repair", manager: "Project managers", worker: "Agents", studio: "Studio seats" };
  const rows = Object.entries(u.by_source || {}).sort((a, b) => b[1].tok - a[1].tok).map(([k, v]) =>
    `<tr><td>${escapeHtml(WHO[k] || k)}</td><td class="rp-num">${rpTok(v.tok)}</td>
      <td class="rp-num dim">${rpTok(v.cache)}</td><td class="rp-num dim">${v.calls}</td></tr>`).join("")
    || `<tr><td colspan="4" class="dim">nothing used this window</td></tr>`;
  const resets = u.resets_at ? new Date(u.resets_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  return `
  <div class="rp-card">
    <div class="rp-card-h">This window <span class="dim">one subscription, shared — the crew only spends what you are not</span></div>
    ${rpUtilHtml(u)}
    ${cools ? `<div class="rp-coolrow">${cools}</div>` : ""}
    <p class="hint rp-usage-note">Counted in input+output tokens. Cache reads are shown apart because a
      single build reads millions of them and would drown everything else.${resets ? ` This window rolls at ${escapeHtml(resets)}.` : ""}</p>
  </div>

  <div class="rp-card">
    <div class="rp-card-h">Who used it</div>
    <table class="rp-table"><thead><tr><th>source</th><th class="rp-num">tokens</th>
      <th class="rp-num">cache</th><th class="rp-num">calls</th></tr></thead><tbody>${rows}</tbody></table>
  </div>

  ${(u.used_tok || 0) > 0 ? "" : `<div class="rp-card">
    <div class="rp-card-h">Session counters <span class="dim">deciding, because nothing reported tokens this window</span></div>
    <div class="rp-meterrow"><span class="rp-meter-lb">calls (5h)</span>${rpBar(m.s5h)}<span class="rp-meter-n">${m.s5h.used}/${m.s5h.cap}</span></div>
    <div class="rp-meterrow"><span class="rp-meter-lb">week</span>${rpBar(m.w7d)}<span class="rp-meter-n">${m.w7d.used}/${m.w7d.cap}</span></div>
    <p class="hint rp-usage-note">A crude count of sessions, kept only for a machine whose SDK
      reports no token counts at all. It is shown ONLY while it is actually the thing deciding —
      a red 34/14 sitting beside a plan with plenty left is not information, it is alarm.</p>
  </div>`}
  <div class="rp-card">
    <div class="rp-card-h">What this is <span class="dim">and what it is not</span></div>
    <p class="hint rp-usage-note">These are the tokens THIS PLATFORM spent, measured from what
      the SDK reports per session — the crew's work, your projects, your Studio agents. It is
      not your Claude account's usage: anything you do in an editor, a browser or another tool
      is invisible here, so a plugin that watches your account will always show more. The
      budget is a dial you set (<code>usage_budget_tokens</code>), not a limit read from
      Anthropic — there is no endpoint that reports one. It exists so the crew can decide when
      to stand back, and the only unarguable signal is a real rate limit, which overrides it.</p>
  </div>`;
}

/** Two views of the same question, because they answer different halves of it.
 *
 * STORY is the narrative bus feed: what the crew did, in order, in sentences. LOGS is the
 * structured pipeline — levelled and categorised — which is what you want the moment the
 * story stops making sense and you need to know which part broke. Filtering by level is a
 * FLOOR, so "warnings" means warnings and errors: nobody looking for trouble wants errors
 * hidden behind a stricter filter. */
function rpActivityPanel() {
  return `
  <div class="rp-card">
    <div class="rp-card-h">Activity
      <span class="dim" id="rpActNote">everything the crew does, as it does it</span>
      <button class="rp-link" id="rpActReload">refresh</button></div>
    <div class="rp-filters">
      <span class="rp-seg2">
        <button class="rp-fchip${rpActView === "story" ? " on" : ""}" data-view="story">Story</button>
        <button class="rp-fchip${rpActView === "logs" ? " on" : ""}" data-view="logs">Logs</button>
      </span>
      <span class="rp-logonly"${rpActView === "logs" ? "" : " hidden"}>
        ${["", "info", "warn", "error"].map((lv) =>
          `<button class="rp-fchip${rpLogLevel === lv ? " on" : ""}" data-level="${lv}">${lv || "all"}</button>`).join("")}
        <select id="rpLogCat" class="rp-select"><option value="">every category</option></select>
        <input id="rpLogQ" class="rp-search" placeholder="search…" value="${escapeHtml(rpLogQ)}">
      </span>
    </div>
    <div id="repairActivity" class="rp-act rp-act-tall"><p class="dim">…</p></div>
  </div>`;
}

function rpCommandPanel(d) {
  // The same visual language as a project's Command tab — the shared ui* builders from
  // projects.js — because after the handoff one kind of crew drives both screens, and the
  // person should learn ONE way to read "who is working, who needs me, what happened".
  const code = d.code || {};
  // The devteam's one step a project never has: its output is merged CODE, and the running
  // app does not run it until restarted (conductor/) or reloaded (dashboard-only).
  const updateHtml = code.needs_restart
    ? uiAttnCard("⬆️ Update available",
        `The app is running code from ${code.behind} landed change(s) ago. Restart to run what the crew shipped.`,
        `<button data-rp-update>⟳ Update &amp; restart the app</button>`)
    : code.needs_reload
      ? uiAttnCard("⬆️ Fresh dashboard landed",
          "Only UI files changed since this page loaded — reloading it is enough.",
          `<button data-rp-reload>⟳ Reload the page</button>`)
      : "";
  const nn = d.notices || {};
  const askHtml = nn.total ? uiAskCard({
    icon: "🛰️", head: "The monitor needs a decision",
    text: `${nn.total} notice${nn.total === 1 ? "" : "s"} open${nn.critical ? " — one is critical" : ""}. Each proposes one concrete action; approving runs it.`,
    buttonsHtml: `<button data-rp-gonotices>Review the notices</button>`,
  }) : "";

  const tasks = ((d.sprint || {}).tasks || []);
  const phase = (d.state || {}).phase || "";
  const WORD = { pending: phase === "verify" ? "Checking" : phase === "build" ? "Working" : "Queued up",
                 green: "In review", landed: "Done", queued: "Waiting for you",
                 failed: "Failed", aborted: "Stopped" };
  const factorName = (f) => escapeHtml((f || "crew").replace(/-/g, " "));
  const cardOf = (t) => uiAgentCard({
    cls: t.status === "landed" ? "done" : t.status === "pending" ? "running" : t.status,
    roleHtml: factorName(t.factor), roleTitle: t.factor || "",
    statusWord: WORD[t.status] || t.status, title: t.title,
    doing: trim(t.evidence || t.error || (t.verification && t.verification.headline)
                || (t.status === "pending" ? "In this sprint's plan." : ""), 150),
    metaHtml: `${rpTag(t)}${t.attempts > 1 ? ` · attempt ${t.attempts}` : ""}${
      t.sent_back ? " · sent back once by a reviewer" : ""}`,
  });
  const qs = d.queue_state || {};
  const qCard = (q) => {
    const st = qs[q.branch] || { ok: true };
    return uiAgentCard({
      cls: "review", roleHtml: "finished change", roleTitle: q.branch,
      statusWord: "Waiting for you", title: q.title,
      doing: st.ok ? "Green and ready to land."
                   : `${st.why} — ${st.fix}.`,
      metaHtml: `s${q.sprint_no || "?"}`,
      extraHtml: `<div class="rp-qbtns">
        ${st.remedy === "rebuild" ? `<button class="rp-mini" data-rebuild="${escapeHtml(q.branch)}">⟳ Rebuild onto today's main &amp; re-test</button>` : ""}
        <button class="rp-mini" data-approve="${escapeHtml(q.branch)}"${st.ok ? "" : ` disabled title="${escapeHtml(st.why + " — " + st.fix)}"`}>✓ Approve &amp; land</button>
        <button class="rp-mini danger" data-discard="${escapeHtml(q.branch)}">✕ Discard</button></div>`,
    });
  };
  const lanes = [
    ["⚡ Working right now", tasks.filter((t) => t.status === "pending").map(cardOf),
     "the current sprint, one task at a time"],
    ["🔍 Waiting for you", (d.queue || []).map(qCard),
     "finished green work that needs your approve or discard"],
    ["✅ Finished this sprint", tasks.filter((t) => t.status === "landed").map(cardOf), ""],
    ["✕ Failed", tasks.filter((t) => ["failed", "aborted"].includes(t.status)).map(cardOf),
     "the board records why"],
  ].map(([label, cards, note]) => cards.length ? uiLane(label, cards.length, note, cards.join("")) : "")
   .join("");

  return `
  ${updateHtml}${askHtml}
  ${lanes ? `<div class="chain rp-chain">${lanes}</div>`
          : `<p class="dim">no sprint running — flip the switch, or file a ticket below</p>`}
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
  </div>`;
}

function rpHtml(d) {
  const m = d.meters || { s5h: { used: 0, cap: 1 }, w7d: { used: 0, cap: 1 }, cooldowns: {}, team: [], util: null };
  const err = d.last_error && d.enabled
    ? `<div class="rp-err">last error · ${escapeHtml(d.last_error.phase || "?")}: ${escapeHtml(trim(d.last_error.detail || "", 140))}</div>` : "";
  const u = m.util || {};
  // One line of context on the tab strip, so switching tabs never hides the two facts that
  // decide everything: what the crew is doing, and whether it is allowed to.
  const badge = u.budget_tok
    ? `<span class="rp-tabnote">${Math.round((u.frac || 0) * 100)}% of this window used</span>` : "";
  const panels = { notices: rpNoticesPanel(d), agents: rpAgentsPanel(d),
                   crew: rpCrewPanel(d, m), board: rpBoardPanel(d),
                   activity: rpActivityPanel(),
                   usage: rpUsagePanel(d, m), command: rpCommandPanel(d) };

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

  <div class="rp-tabs" role="tablist">
    ${RP_TABS.map((t) => {
      const nn = t.id === "notices" ? (d.notices || {}) : null;
      const dot = nn && nn.total ? `<span class="rp-dot${nn.critical ? " bad" : ""}">${nn.total}</span>` : "";
      return `<button class="rp-tab${t.id === rpTab ? " on" : ""}" role="tab"
        aria-selected="${t.id === rpTab}" data-tab="${t.id}">${escapeHtml(t.label)}${dot}</button>`;
    }).join("")}
    ${badge}
  </div>
  ${RP_TABS.map((t) => `<div class="rp-panel" data-panel="${t.id}"${t.id === rpTab ? "" : " hidden"}>${panels[t.id]}</div>`).join("")}`;
}

// ---- wiring ---------------------------------------------------------------

/** The review queue's buttons (approve / rebuild / discard), bindable on ANY container —
 * the Improve screen's board and Devteam HQ's board both carry them. After acting it
 * refreshes whichever screen it is actually on. */
function rpWireQueue(el) {
  const redo = () => {
    if (typeof projSrc !== "undefined" && projSrc) refreshBoard();
    else rpRefresh(true);
  };
  el.querySelectorAll("[data-approve]").forEach((b) => b.addEventListener("click", async () => {
    try { await api("/api/repair/queue/approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ branch: b.dataset.approve }) }); toast("Landed."); }
    catch (e) { toast(`Could not land: ${e.message}`); }
    redo();
  }));
  el.querySelectorAll("[data-rebuild]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "rebuilding + re-running the suite…";
    try {
      const r = await api("/api/repair/queue/rebuild", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ branch: b.dataset.rebuild }) });
      toast(r.verified ? "Rebuilt onto main and still green — approve it now."
                       : `Rebuilt, but the suite is red: ${r.headline || "see the board"}`);
    } catch (e) { toast(`Could not rebuild: ${e.message}`); }
    redo();
  }));
  el.querySelectorAll("[data-discard]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Discard this change?")) return;
    try { await api("/api/repair/queue/discard", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ branch: b.dataset.discard }) }); }
    catch (e) { toast(e.message); }
    redo();
  }));
}

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
  el.querySelectorAll("[data-rp-update]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "restarting…";
    try { await api("/api/repair/restart", { method: "POST" });
          waitForRestart("Updating to the latest landed code…"); }
    catch (e) { toast(e.message); b.disabled = false; b.textContent = "⟳ Update & restart the app"; }
  }));
  el.querySelectorAll("[data-rp-reload]").forEach((b) => b.addEventListener("click", () => location.reload()));
  el.querySelectorAll("[data-rp-gonotices]").forEach((b) => b.addEventListener("click", () => {
    rpTab = "notices"; rpRefresh(true);
  }));
  rpWireQueue(el);
  el.querySelectorAll("[data-revert]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Revert this landed change?")) return;
    try { await api("/api/repair/revert", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sha: b.dataset.revert }) }); toast("Reverted."); }
    catch (e) { toast(`Could not revert: ${e.message}`); }
    rpRefresh(true);
  }));

  // The chat had no history and started hidden, so it looked like a dead input box: you
  // typed, waited, and had no way to tell whether anything was happening. It now shows the
  // conversation as soon as the tab opens.
  rpChatHistory();
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
      rpRenderChat(r.chat || []);
    } catch (e) {
      log.lastElementChild.outerHTML = `<div class="rp-msg them err">${escapeHtml(e.message)}</div>`;
    }
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

  el.querySelectorAll("[data-board]").forEach((b) =>
    b.addEventListener("click", () => rpBoard(b.dataset.board)));
  const areload = $("#rpActReload");
  if (areload) areload.addEventListener("click", rpActivity);
  const openTeam = $("#rpOpenTeam");
  if (openTeam) openTeam.addEventListener("click", () => openDevteam());
  const nreload = $("#rpNoticesReload");
  if (nreload) nreload.addEventListener("click", rpNotices);
  const areload2 = $("#rpAgentsReload");
  if (areload2) areload2.addEventListener("click", rpAgents);
  const auto = $("#rpAuto");
  if (auto) auto.addEventListener("change", async () => {
    try {
      await api("/api/logs/notices/auto", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on: auto.checked }) });
      toast(auto.checked ? "Additive proposals will run without asking."
                         : "Proposals will wait for you again.");
    } catch (e) { auto.checked = !auto.checked; toast(reportCaught(e, "rpAuto")); }
    rpNotices();
  });
  el.querySelectorAll("[data-open-project]").forEach((b) => b.addEventListener("click", () =>
    openProject(Number(b.dataset.openProject), "board")));
  el.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => {
    rpActView = b.dataset.view;
    el.querySelectorAll("[data-view]").forEach((x) => x.classList.toggle("on", x.dataset.view === rpActView));
    const only = el.querySelector(".rp-logonly");
    if (only) only.hidden = rpActView !== "logs";
    rpActivity();
  }));
  el.querySelectorAll("[data-level]").forEach((b) => b.addEventListener("click", () => {
    rpLogLevel = b.dataset.level;
    el.querySelectorAll("[data-level]").forEach((x) => x.classList.toggle("on", x.dataset.level === rpLogLevel));
    rpActivity();
  }));
  const lcat = $("#rpLogCat");
  if (lcat) lcat.addEventListener("change", rpActivity);
  const lq = $("#rpLogQ");
  if (lq) {
    let t = null;
    lq.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => { rpLogQ = lq.value.trim(); rpActivity(); }, 350);
    });
  }

  // Tabs. Every panel is in the DOM and inactive ones are `hidden`, so switching is instant,
  // form state survives, and nothing has to be re-fetched to look at it again.
  el.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => {
    rpTab = b.dataset.tab;
    el.querySelectorAll("[data-tab]").forEach((x) => {
      x.classList.toggle("on", x.dataset.tab === rpTab);
      x.setAttribute("aria-selected", String(x.dataset.tab === rpTab));
    });
    el.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== rpTab; });
    const act = $("#repairActivity");
    if (rpTab === "activity" && act) act.scrollTop = act.scrollHeight;
  }));

  rpHistory();
  rpActivity();
  rpNotices();
  rpAgents();
}

function rpRenderChat(chat) {
  const log = $("#repairChatLog"); if (!log) return;
  log.innerHTML = (chat || []).slice(-40).map((m) =>
    `<div class="rp-msg ${m.role === "user" ? "me" : "them"}">${escapeHtml(m.text || "")}</div>`).join("")
    || `<p class="dim">Nothing said yet. The manager knows the sprint board, the crew and why it last slept.</p>`;
  log.scrollTop = log.scrollHeight;
}

/** The conversation so far, read without spending anything: an empty POST returns the log. */
async function rpChatHistory() {
  const log = $("#repairChatLog"); if (!log) return;
  try {
    const r = await api("/api/repair/chat", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: "" }) });
    rpRenderChat(r.chat || []);
  } catch (e) { log.innerHTML = `<p class="err">${escapeHtml(reportCaught(e, "rpChatHistory"))}</p>`; }
}

async function rpHistory() {
  const el = $("#repairHistory"); if (!el) return;
  try {
    const r = await api("/api/repair/sprints?limit=12");
    const rows = (r.sprints || []).slice().reverse().map((s) =>
      `<button class="rp-hist" data-board="${s.no}"><b>s${s.no}</b> <span>${escapeHtml(s.retro || "…")}</span>
        <span class="dim">${s.landed || 0} landed · ${s.failed || 0} failed</span></button>`).join("");
    el.innerHTML = rows || `<p class="dim">no sprints yet</p>`;
    el.querySelectorAll("[data-board]").forEach((b) =>
      b.addEventListener("click", () => rpBoard(b.dataset.board)));
  } catch { el.innerHTML = `<p class="err">—</p>`; }
}

// ---- the sprint board -----------------------------------------------------

/** One sprint, opened out: every issue it took on and how each ended.
 *
 * The list view can only afford "2 landed · 1 failed", and the question people actually ask
 * is *which* one failed and what the suite said — an answer that used to exist only in the
 * server's terminal. Grouped by outcome, because "what shipped" and "what didn't" are two
 * different questions and mixing them makes both harder to read. */
const RP_LANES = [
  { key: "landed", label: "Landed", match: (t) => t.status === "landed" },
  { key: "failed", label: "Failed", match: (t) => ["failed", "aborted", "too_big"].includes(t.status) },
  { key: "queued", label: "Awaiting review", match: (t) => ["queued", "green"].includes(t.status) },
  { key: "open", label: "Still open", match: (t) => !["landed", "failed", "aborted", "too_big", "queued", "green"].includes(t.status) },
];

async function rpBoard(no) {
  let s;
  try { s = (await api(`/api/repair/sprint/${encodeURIComponent(no)}`)).sprint; }
  catch (e) { toast(`Could not open sprint ${no}: ${reportCaught(e, "rpBoard")}`); return; }
  const tasks = s.tasks || [];
  const card = (t) => {
    const v = t.verification || {};
    const why = t.error || v.headline || "";
    return `<div class="rp-bcard s-${escapeHtml(t.status || "pending")}">
      <div class="rp-bcard-t">${escapeHtml(t.title || "(untitled)")}</div>
      <div class="rp-bcard-m"><span class="rp-chip on">${escapeHtml(t.factor || "?")}</span>
        <span class="dim">${escapeHtml(t.status || "pending")}${t.attempts ? ` · ${t.attempts} attempt${t.attempts > 1 ? "s" : ""}` : ""}</span></div>
      ${t.brief ? `<div class="rp-bcard-b">${escapeHtml(trim(t.brief, 260))}</div>` : ""}
      ${why ? `<div class="rp-bcard-w">${escapeHtml(trim(why, 300))}</div>` : ""}
      ${t.landed_sha ? `<div class="rp-bcard-m"><code>${escapeHtml(t.landed_sha.slice(0, 8))}</code>
        <button class="rp-mini" data-revert="${escapeHtml(t.landed_sha)}">↩ revert</button></div>` : ""}
      ${t.branch && !t.landed_sha ? `<div class="rp-bcard-m dim"><code>${escapeHtml(t.branch)}</code></div>` : ""}
    </div>`;
  };
  const lanes = tasks.length
    ? RP_LANES.map((ln) => {
        const mine = tasks.filter(ln.match);
        if (!mine.length) return "";
        return `<div class="rp-lane"><div class="rp-lane-h">${ln.label} <span class="dim">${mine.length}</span></div>
          ${mine.map(card).join("")}</div>`;
      }).join("")
    : `<p class="dim">This sprint has not taken on any work yet.</p>`;
  const memo = s.memo || {};
  const positions = (memo.positions || []).map((p) =>
    `<div class="rp-msg them"><b>${escapeHtml(p.name || memo.names?.[String(p.who)] || "crew")}</b>
      ${escapeHtml(trim(p.position || p.text || "", 400))}</div>`).join("");
  $("#rpBoardTitle").textContent = `Sprint ${s.no} — ${tasks.length} issue${tasks.length === 1 ? "" : "s"}`;
  $("#rpBoardBody").innerHTML = `
    ${s.retro ? `<p class="rp-board-retro">${escapeHtml(s.retro)}</p>` : ""}
    <div class="rp-lanes">${lanes}</div>
    ${memo.recommendation ? `<div class="rp-card-h rp-qh">What the crew decided</div>
      <div class="rp-board-memo">${escapeHtml(memo.recommendation)}</div>` : ""}
    ${positions ? `<div class="rp-card-h rp-qh">and how they argued it</div>
      <div class="rp-board-pos">${positions}</div>` : ""}
    ${s.scout && s.scout.digest ? `<div class="rp-card-h rp-qh">Scout findings</div>
      <div class="rp-board-memo dim">${escapeHtml(s.scout.digest)}</div>` : ""}`;
  $("#rpBoardBody").querySelectorAll("[data-revert]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Revert this landed change?")) return;
    try { await api("/api/repair/revert", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sha: b.dataset.revert }) }); toast("Reverted."); }
    catch (e) { toast(`Could not revert: ${e.message}`); }
  }));
  $("#rpBoard").hidden = false;
}

// ---- the activity feed ----------------------------------------------------

// Phase events read as prose; the verify_* stream is the worker's own chatter, which is the
// noisy majority and only interesting when something is wrong — so it stays but reads quieter.
const RP_ACT = {
  repair_toggled: (p) => (p.on ? "the crew clocked in" : "the crew clocked off"),
  repair_team_ready: (p) => `crew formed — ${p.agents} specialists`,
  repair_sprint_started: (p) => `Sprint ${p.sprint} started${p.from_backlog ? " from the backlog" : ""}`,
  repair_scouted: (p) => `scouted the repo — ${p.candidates ?? "?"} candidates found`,
  repair_deliberated: (p) => `the crew agreed a plan${p.positions ? ` — ${p.positions.length} positions` : ""}`,
  repair_planned: (p) => `planned ${p.tasks} task${p.tasks === 1 ? "" : "s"}`,
  repair_building: (p) => `building: ${p.task}`,
  repair_landed: (p) => `landed: ${p.task}`,
  repair_queued: (p) => `queued for your review: ${p.task}${p.why ? ` — ${p.why}` : ""}`,
  repair_task_failed: (p) => `failed: ${p.task}${p.why ? ` — ${p.why}` : ""}`,
  repair_sprint_done: (p) => `Sprint ${p.sprint} done — ${p.landed || 0} landed, ${p.failed || 0} failed`,
  repair_sleeping: (p) => `sleeping — ${p.reason}`,
  repair_restarting: () => "restarting to apply landed changes",
  repair_aborted: () => "you aborted the task in flight",
  engine_error: (p) => `engine error — ${p.detail}`,
};

// The look the projects feed already earned: a coloured left edge for WHO, an uppercase
// source line, and outcomes weighted differently from narration. Before this, "landed a fix"
// and a worker musing about a file looked equally important — which means neither did.
const RP_EV_CLASS = {
  repair_landed: "outcome good", repair_sprint_done: "outcome good",
  repair_task_failed: "outcome bad", engine_error: "error",
  repair_queued: "decision", repair_sleeping: "ratelimit",
  repair_toggled: "decision", repair_aborted: "decision",
  repair_restarting: "system", repair_team_ready: "manager",
  repair_deliberated: "manager", repair_planned: "manager",
  repair_sprint_started: "manager", repair_scouted: "manager",
  repair_building: "worker",
};

const RP_EV_WHO = {
  repair_deliberated: "🧠 The crew", repair_planned: "👔 Manager", repair_scouted: "🔎 Scout",
  repair_sprint_started: "👔 Manager", repair_team_ready: "👔 Manager",
  repair_building: "🛠 Builder", repair_landed: "🛠 Builder", repair_task_failed: "🛠 Builder",
  repair_sleeping: "👔 Manager", repair_sprint_done: "👔 Manager", repair_queued: "👔 Manager",
  repair_toggled: "🫵 You", repair_aborted: "🫵 You", engine_error: "⚙️ Engine",
  repair_restarting: "⚙️ Engine",
};

function rpActText(e) {
  let p = e.payload;
  try { p = typeof p === "string" ? JSON.parse(p) : (p || {}); } catch { p = { detail: String(p) }; }
  const fn = RP_ACT[e.kind];
  if (fn) return String(fn(p));
  if (e.kind.startsWith("verify_")) {
    const body = typeof p === "object" ? (p.detail || p.text || JSON.stringify(p)) : String(p);
    return `${e.kind.replace("verify_", "")}: ${body}`;
  }
  return `${e.kind}: ${typeof p === "object" ? JSON.stringify(p) : p}`;
}

/** One beat, in the same visual language as a project's feed. */
function rpActLine(e, n = 1) {
  let p = e.payload;
  try { p = typeof p === "string" ? JSON.parse(p) : (p || {}); } catch { p = {}; }
  const verify = e.kind.startsWith("verify_");
  const cls = RP_EV_CLASS[e.kind] || (verify ? "worker chatter" : "system");
  const who = RP_EV_WHO[e.kind] || (verify ? "🧪 Verifier" : "⚙️ Engine");
  const when = e.ts ? new Date(e.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }) : "";
  const sha = p && p.sha ? String(p.sha).slice(0, 8) : "";
  // No newlines or indentation inside the element: .ev is `white-space: pre-wrap`, so a
  // prettily-indented template renders its own indentation as blank space and every row
  // ends up three times taller than its content.
  const src = `<div class="src">${escapeHtml(who)} · ${escapeHtml(e.kind.replace(/^repair_/, ""))} · ${escapeHtml(when)}${n > 1 ? ` · ×${n}` : ""}</div>`;
  const body = `${escapeHtml(trim(rpActText(e), 400))}${sha ? ` <code>${escapeHtml(sha)}</code>` : ""}`;
  return `<div class="ev ${cls}">${src}${body}</div>`;
}

// A log row is denser than a story beat: level colour, the cat/event slug you filter on, the
// message, then every extra field the call site attached. Different shape from `.ev` on
// purpose — one is a narrative, the other is a table you scan.
const RP_LOGCOL = { debug: "quiet", info: "", warn: "warnline", error: "errline" };

function rpLogLine(r) {
  // 24-hour, because "12:07:37 PM" does not fit the column and wrapped onto two lines —
  // and an operational log is one of the few places nobody wants am/pm.
  const when = r.ts ? new Date(r.ts * 1000).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }) : "";
  const skip = { ts: 1, level: 1, cat: 1, event: 1, msg: 1, repeats: 1 };
  const fields = Object.entries(r).filter(([k, v]) => !skip[k] && v !== null && v !== "")
    .map(([k, v]) => `<span class="rp-lf">${escapeHtml(k)}=${escapeHtml(trim(String(v), 70))}</span>`).join(" ");
  return `<div class="rp-actline ${RP_LOGCOL[r.level] || ""}"><span class="rp-actwhen">${escapeHtml(when)}</span>`
    + `<span class="rp-lcat">${escapeHtml(r.cat)}/${escapeHtml(r.event)}</span>`
    + `<span>${escapeHtml(trim(String(r.msg || ""), 200))}${r.repeats > 1 ? ` <span class="dim">×${r.repeats}</span>` : ""} ${fields}</span></div>`;
}

/** Consecutive identical beats collapse to one line with a count.
 *
 * A sleeping engine used to emit the same sentence every twenty seconds; it no longer does,
 * but months of that are already in the log and any future repeat would push everything
 * interesting off the screen the same way. */
function rpCollapse(events) {
  const out = [];
  for (const e of events) {
    const last = out[out.length - 1];
    if (last && last.e.kind === e.kind && last.text === rpActText(e)) { last.n++; last.e = e; continue; }
    out.push({ e, text: rpActText(e), n: 1 });
  }
  return out;
}

async function rpActivity() {
  const el = $("#repairActivity"); if (!el) return;
  try {
    if (rpActView === "logs") {
      const qs = new URLSearchParams({ limit: "300" });
      if (rpLogLevel) qs.set("level", rpLogLevel);
      const cat = $("#rpLogCat"); if (cat && cat.value) qs.set("cat", cat.value);
      if (rpLogQ) qs.set("q", rpLogQ);
      const r = await api(`/api/logs?${qs}`);
      rpFillCats(r.categories || {});
      const note = $("#rpActNote");
      if (note) note.textContent = `${(r.logs || []).length} lines — what the system did, and whether it worked`;
      el.innerHTML = (r.logs || []).map(rpLogLine).join("")
        || `<p class="dim">nothing matches that filter</p>`;
    } else {
      const r = await api("/api/repair/activity?limit=200");
      const note = $("#rpActNote");
      if (note) note.textContent = "everything the crew does, as it does it";
      el.innerHTML = rpCollapse(r.events || []).map((g) => rpActLine(g.e, g.n)).join("")
        || `<p class="dim">nothing yet — the crew has not run</p>`;
    }
    el.scrollTop = el.scrollHeight;
  } catch (e) {
    el.innerHTML = `<p class="err">${escapeHtml(reportCaught(e, "rpActivity"))}</p>`;
  }
}

/** The category list comes from the server, so the vocabulary has exactly one home. */
function rpFillCats(cats) {
  const sel = $("#rpLogCat"); if (!sel || sel.dataset.filled) return;
  for (const [k, why] of Object.entries(cats)) {
    const o = document.createElement("option");
    o.value = k; o.textContent = k; o.title = why;
    sel.appendChild(o);
  }
  sel.dataset.filled = "1";
}

/** Called from the shared WS handler so the feed is live, not a 5s poll. */
function rpOnEvent(e) {
  if (!e || e.source !== "repair") return;
  const el = $("#repairActivity");
  if (!el || $("#selfPage").hidden || rpActView !== "story") return;
  if (el.querySelector("p.dim")) el.innerHTML = "";
  const last = el.lastElementChild;
  if (last && last.dataset.k === e.kind && last.dataset.t === rpActText(e)) {
    const n = Number(last.dataset.n || 1) + 1;
    last.outerHTML = rpActLine(e, n);
    const now = el.lastElementChild;
    now.dataset.k = e.kind; now.dataset.t = rpActText(e); now.dataset.n = n;
    el.scrollTop = el.scrollHeight;
    return;
  }
  el.insertAdjacentHTML("beforeend", rpActLine(e));
  const added = el.lastElementChild;
  added.dataset.k = e.kind; added.dataset.t = rpActText(e); added.dataset.n = 1;
  while (el.children.length > 200) el.removeChild(el.firstElementChild);
  el.scrollTop = el.scrollHeight;
}
