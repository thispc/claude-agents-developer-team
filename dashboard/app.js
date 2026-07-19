const $ = (s) => document.querySelector(s);
let currentProject = null;
let ws = null;

const COLS = {
  planned: ["planned"],
  working: ["queued", "running"],
  review: ["pushed", "review", "changes_requested", "failed"],  // failed needs attention, not "done"
  done: ["done"],
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
  return res.json();
}

let authMode = "none";
const taskCost = (t) => (authMode === "subscription" ? "" : ` · $${t.cost_usd.toFixed(2)}`);

async function loadHealth() {
  try {
    const h = await api("/api/health");
    authMode = h.auth || (h.anthropic_key ? "api-key" : "none");
    const b = $("#authBadge");
    if (authMode === "subscription") {
      b.textContent = "auth: Max subscription";
      b.className = "badge ok";
      b.title = "Agents run on your Claude subscription. Dollar figures are ESTIMATES for " +
        "budgeting only — nothing is billed. Usage counts toward your plan's rate limits.";
    } else if (authMode === "api-key") {
      b.textContent = "auth: API key";
      b.className = "badge warn";
      b.title = "Agents bill pay-per-token API credit. Figures shown are the SDK's per-project " +
        "estimate; your authoritative balance is at console.anthropic.com (no API exposes it).";
    } else {
      b.textContent = "auth: none";
      b.className = "badge bad";
      b.title = "Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN in .env";
    }
  } catch { /* server starting */ }
}

const STATUS_CLASS = { done: "ok", failed: "bad", cancelled: "bad", review: "warn", hold: "warn", planning: "run", running: "run" };
const STATUS_LABEL = { hold: "on hold — needs you", review: "in review" };

async function loadProjects() {
  const projects = await api("/api/projects");
  const sel = $("#projectSelect");
  sel.innerHTML = "";
  for (const p of projects) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.dataset.repo = p.repo || "";
    opt.textContent = `#${p.id} ${p.name} [${p.status}]`;
    sel.appendChild(opt);
  }
  if (currentProject) sel.value = currentProject;
  renderHome(projects);
}

function renderHome(projects) {
  const body = $("#projectsBody");
  body.innerHTML = "";
  $("#homeEmpty").hidden = projects.length > 0;
  for (const p of projects) {
    const tr = document.createElement("tr");
    const cls = STATUS_CLASS[p.status] || "";
    const label = STATUS_LABEL[p.status] || p.status;
    const usage = authMode === "subscription"
      ? `${p.runs_used ?? 0}/${p.max_runs ?? 40} runs`
      : `$${(p.cost_usd || 0).toFixed(2)}`;
    const repoCell = p.repo
      ? `<a href="https://github.com/${p.repo}" target="_blank" onclick="event.stopPropagation()">${p.repo}</a>`
      : "—";
    const active = !["done", "failed", "cancelled"].includes(p.status);
    const canRestart = ["failed", "review", "cancelled"].includes(p.status);
    tr.innerHTML = `
      <td>${p.id}</td>
      <td class="pname">${escapeHtml(p.name)}</td>
      <td>${repoCell}</td>
      <td><span class="pill ${cls}">${label}</span></td>
      <td>${p.task_count ?? "—"}</td>
      <td>${usage}</td>
      <td class="row-actions">
        <button data-act="open" data-id="${p.id}">Open</button>
        ${active ? `<button data-act="cancel" data-id="${p.id}" class="danger">Cancel</button>` : ""}
        ${canRestart ? `<button data-act="restart" data-id="${p.id}">↻</button>` : ""}
      </td>`;
    tr.addEventListener("click", () => openProject(p.id));
    body.appendChild(tr);
  }
  body.querySelectorAll(".row-actions button").forEach((b) =>
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const id = b.dataset.id;
      if (b.dataset.act === "open") return openProject(id);
      if (b.dataset.act === "cancel" && confirm("Cancel this project?"))
        await api(`/api/projects/${id}/cancel`, { method: "POST" });
      if (b.dataset.act === "restart")
        await api(`/api/projects/${id}/restart`, { method: "POST" });
      loadProjects();
    }));
}

function showHome() {
  currentProject = null;
  $("#home").hidden = false;
  $("main").hidden = true;
  $("#projectSelect").hidden = true;
  $("#costBadge").hidden = true;
  $("#statusBadge").hidden = true;
  $("#restartBtn").hidden = true;
  $("#cancelBtn").hidden = true;
  loadProjects();
}

function openProject(id) {
  $("#home").hidden = true;
  $("main").hidden = false;
  $("#projectSelect").hidden = false;
  selectProject(id);
}

async function selectProject(id) {
  currentProject = Number(id);
  $("#projectSelect").value = id;
  $("#events").innerHTML = "";
  $("#feedTitle").textContent = `Live activity — project #${id}`;
  await refreshBoard();
  const events = await api(`/api/projects/${id}/events`);
  for (const e of events.slice(-200)) renderEvent(e);
}

let lastTasks = [];

async function refreshBoard() {
  if (!currentProject) return;
  let p;
  try { p = await api(`/api/projects/${currentProject}`); } catch { return; }
  lastTasks = p.tasks;
  currentRepo = p.repo || "";
  if (!$("#dag").hidden) renderDag(p.tasks);
  const runs = p.runs_used ?? 0, maxRuns = p.max_runs ?? 40;
  $("#costBadge").hidden = false;
  if (authMode === "subscription") {
    // No per-token billing — show the meaningful metric (agent runs), not dollars.
    $("#costBadge").textContent = `${runs} / ${maxRuns} agent runs`;
    $("#costBadge").title = "Running on your Claude subscription — no per-token charge. " +
      "The cap counts total worker dispatches; it's the runaway-loop guard.";
  } else {
    $("#costBadge").textContent = `$${p.cost_usd.toFixed(2)} / $${p.budget_usd.toFixed(2)} · ${runs} runs`;
    $("#costBadge").title = "Estimated API spend / budget cap. Authoritative balance is at console.anthropic.com.";
  }
  const badge = $("#statusBadge");
  badge.hidden = false;
  badge.textContent = STATUS_LABEL[p.status] || p.status;
  badge.className = "badge " + (STATUS_CLASS[p.status] || "run");
  badge.title = p.summary || "";
  $("#cancelBtn").hidden = ["done", "failed", "cancelled"].includes(p.status);
  $("#restartBtn").hidden = !["failed", "review", "cancelled"].includes(p.status);
  $("#artifactsBtn").hidden = !p.repo;
  refreshQuestion();
  for (const [col, statuses] of Object.entries(COLS)) {
    const box = document.querySelector(`.col[data-col="${col}"] .cards`);
    box.innerHTML = "";
    for (const t of p.tasks.filter((t) => statuses.includes(t.status))) {
      const card = document.createElement("div");
      card.className = `card ${t.status}`;
      let deps = [];
      try { deps = JSON.parse(t.deps || "[]"); } catch { /* old rows */ }
      const links = [];
      if (deps.length) links.push(`after ${deps.map((d) => "#" + d).join(",")}`);
      if (t.issue_number && p.repo) links.push(`<a target="_blank" href="https://github.com/${p.repo}/issues/${t.issue_number}">#${t.issue_number}</a>`);
      if (t.pr_number && p.repo) links.push(`<a target="_blank" href="https://github.com/${p.repo}/pull/${t.pr_number}">PR ${t.pr_number}</a>`);
      card.innerHTML = `
        <div class="role ${t.role}">${t.role}</div>
        <div class="title">${escapeHtml(t.title)}</div>
        <div class="desc">${escapeHtml(t.description.slice(0, 160))}</div>
        <div class="meta">${t.status} · try ${t.attempts}${taskCost(t)} ${links.join(" ")}</div>`;
      card.addEventListener("click", (ev) => {
        if (ev.target.tagName !== "A") showTask(t.id);
      });
      box.appendChild(card);
    }
  }
}

function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function renderEvent(e) {
  if (e.project_id !== currentProject) return;
  const div = document.createElement("div");
  let cls = e.source.startsWith("worker") ? "worker" : e.source;
  if (e.source === "scheduler") cls = "system";
  if (e.source === "lead") cls = "manager"; // events from before the rename
  if (e.kind === "tool_use") cls += " tool";
  if (e.kind === "thinking") cls += " think";
  if (e.source === "manager" && e.kind === "message") cls += " mgrmsg";
  if (e.kind === "boss_question") cls += " question";
  if (e.source === "boss") cls += " bossmsg";
  if (e.kind === "error" || e.kind === "worker_died" || e.kind === "dag_blocked") cls += " error";
  div.className = `ev ${cls}`;
  let text = e.payload;
  try {
    const obj = JSON.parse(e.payload);
    if (typeof obj === "object" && obj !== null) {
      if (e.kind === "tool_use") text = `→ ${obj.tool || ""} ${JSON.stringify(obj.input || obj).slice(0, 300)}`;
      else if (e.kind === "report") text = `[${obj.status}] cost $${(obj.cost_usd || 0).toFixed(2)}\n${obj.summary || ""}`;
      else text = JSON.stringify(obj);
    }
  } catch { /* plain text payload */ }
  const when = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : "";
  const who = e.source === "manager" ? "👔 Manager" : e.source === "boss" ? "🫵 You"
    : e.source.startsWith("worker") ? "🛠 " + e.source.replace("worker:", "") : e.source;
  div.innerHTML = `<div class="src">${escapeHtml(who)} · ${escapeHtml(e.kind)} · ${when}</div>${escapeHtml(text)}`;
  const box = $("#events");
  box.appendChild(div);
  while (box.children.length > 400) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (m) => {
    const e = JSON.parse(m.data);
    if (currentProject) { renderEvent(e); refreshBoard(); }
    else loadProjects();  // on the home page, keep the table live
    if (e.kind === "boss_question" || e.kind === "answered") refreshQuestion();
    if (["project_created", "project_finished", "project_cancelled", "boss_question"].includes(e.kind)) loadProjects();
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
}

// --- boss controls ----------------------------------------------------------
async function refreshQuestion() {
  if (!currentProject) return;
  let q;
  try { q = await api(`/api/projects/${currentProject}/question`); } catch { return; }
  const banner = $("#askBanner");
  if (!q.question) { banner.hidden = true; return; }
  banner.hidden = false;
  $("#askText").textContent = "⏸ Manager needs your call: " + q.question;
  const box = $("#askOpts");
  box.innerHTML = "";
  for (const opt of q.options || []) {
    const b = document.createElement("button");
    b.textContent = opt;
    b.addEventListener("click", () => answerQuestion(q.id, opt));
    box.appendChild(b);
  }
  const custom = document.createElement("input");
  custom.placeholder = "or type your own answer + Enter";
  custom.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && custom.value.trim()) answerQuestion(q.id, custom.value.trim());
  });
  box.appendChild(custom);
}

async function answerQuestion(qid, answer) {
  await api(`/api/questions/${qid}/answer`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answer }),
  });
  $("#askBanner").hidden = true;
}

// --- task detail panel ------------------------------------------------------
let editTaskTarget = null;
async function showTask(id) {
  const t = lastTasks.find((x) => x.id === id);
  if (!t) return;
  editTaskTarget = id;
  $("#editTaskBtn").hidden = ["done", "running", "queued"].includes(t.status);
  let deps = [];
  try { deps = JSON.parse(t.deps || "[]"); } catch { /* noop */ }
  const repo = currentRepo;
  const links = [];
  if (t.issue_number && repo) links.push(`<a target="_blank" href="https://github.com/${repo}/issues/${t.issue_number}">issue #${t.issue_number}</a>`);
  if (t.pr_number && repo) links.push(`<a target="_blank" href="https://github.com/${repo}/pull/${t.pr_number}">PR #${t.pr_number}</a>`);
  if (t.branch && repo) links.push(`<a target="_blank" href="https://github.com/${repo}/tree/${t.branch}">${t.branch}</a>`);
  const canRetry = ["failed", "review", "done"].includes(t.status);
  const canSkip = !["done"].includes(t.status);
  $("#taskDetail").innerHTML = `
    <div class="role ${t.role}">${t.role}</div>
    <h2>#${t.id} ${escapeHtml(t.title)}</h2>
    <div class="meta">${t.status} · attempt ${t.attempts}${t.attempts >= 2 ? " (escalated to Sonnet)" : ""}${taskCost(t)}
      ${t.origin === "runtime" ? "· added at runtime" : ""}
      ${deps.length ? "· depends on " + deps.map((d) => "#" + d).join(", ") : ""} ${links.join(" · ")}</div>
    <div class="task-actions">
      ${canRetry ? `<button data-act="retry" data-id="${t.id}">↻ Re-run this task</button>` : ""}
      ${canSkip ? `<button data-act="skip" data-id="${t.id}">✓ Mark done / skip</button>` : ""}
    </div>
    <h3>Specification (written by the manager)</h3>
    <pre>${escapeHtml(t.description)}</pre>
    ${t.feedback ? `<h3>Latest review feedback</h3><pre>${escapeHtml(t.feedback)}</pre>` : ""}
    ${t.report ? `<h3>Final report</h3><pre>${escapeHtml(t.report)}</pre>` : ""}
    <h3>Agent log — full transcript (start to end)</h3>
    <div id="taskLog"><pre class="dim">loading…</pre></div>`;
  // Fetch the complete per-agent event stream for this task.
  api(`/api/tasks/${t.id}/events`).then((evs) => {
    const log = $("#taskLog");
    if (!evs.length) { log.innerHTML = `<pre class="dim">No activity recorded (task hasn't run yet).</pre>`; return; }
    log.innerHTML = evs.map((e) => {
      let text = e.payload;
      try {
        const o = JSON.parse(e.payload);
        if (typeof o === "object" && o !== null)
          text = e.kind === "tool_use" ? `$ ${o.tool || o.name || ""} ${JSON.stringify(o.input || o).slice(0, 500)}` : JSON.stringify(o);
      } catch { /* plain text */ }
      const when = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : "";
      return `<div class="logline log-${e.kind}"><span class="lt">${when} · ${escapeHtml(e.kind)}</span>${escapeHtml(text)}</div>`;
    }).join("");
  }).catch(() => { $("#taskLog").innerHTML = `<pre class="dim">could not load log</pre>`; });
  $("#taskDetail").querySelectorAll(".task-actions button").forEach((b) =>
    b.addEventListener("click", async () => {
      await api(`/api/tasks/${b.dataset.id}/${b.dataset.act}`, { method: "POST" });
      $("#taskDialog").close();
      refreshBoard();
    }));
  $("#taskDialog").showModal();
}

let currentRepo = "";

// --- DAG view ---------------------------------------------------------------
const DAG_COLORS = {
  planned: "#7d8aa5", queued: "#5eead4", running: "#5eead4",
  pushed: "#fbbf24", review: "#fbbf24", done: "#4ade80", failed: "#f87171",
};
const NODE_W = 200, NODE_H = 66, GAP_X = 90, GAP_Y = 26, PAD = 30;

function renderDag(tasks) {
  const svg = $("#dagSvg");
  if (!tasks.length) {
    svg.innerHTML = `<text x="20" y="40" fill="#7d8aa5" font-size="13">No tasks yet — the manager is planning.</text>`;
    svg.setAttribute("width", 400); svg.setAttribute("height", 80);
    return;
  }
  const byId = Object.fromEntries(tasks.map((t) => [t.id, t]));
  const depsOf = (t) => { try { return JSON.parse(t.deps || "[]").filter((d) => byId[d]); } catch { return []; } };
  const levels = {};
  const level = (t, seen = new Set()) => {
    if (levels[t.id] !== undefined) return levels[t.id];
    if (seen.has(t.id)) return 0; // cycle guard — shouldn't happen
    seen.add(t.id);
    const d = depsOf(t);
    return (levels[t.id] = d.length ? 1 + Math.max(...d.map((x) => level(byId[x], seen))) : 0);
  };
  tasks.forEach((t) => level(t));
  const columns = {};
  tasks.forEach((t) => (columns[levels[t.id]] ||= []).push(t));
  const pos = {};
  const maxRows = Math.max(...Object.values(columns).map((c) => c.length));
  for (const [lvl, col] of Object.entries(columns)) {
    col.forEach((t, row) => {
      const offset = ((maxRows - col.length) * (NODE_H + GAP_Y)) / 2;
      pos[t.id] = { x: PAD + lvl * (NODE_W + GAP_X), y: PAD + offset + row * (NODE_H + GAP_Y) };
    });
  }
  const width = PAD * 2 + (Object.keys(columns).length) * (NODE_W + GAP_X) - GAP_X;
  const height = PAD * 2 + maxRows * (NODE_H + GAP_Y) - GAP_Y;

  let edges = "", nodes = "";
  for (const t of tasks) {
    for (const d of depsOf(t)) {
      const a = pos[d], b = pos[t.id];
      const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
      const mid = (x1 + x2) / 2;
      const done = byId[d].status === "done";
      edges += `<path d="M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}"
        fill="none" stroke="${done ? "#4ade80" : "#2a3245"}" stroke-width="1.6"
        marker-end="url(#arrow)" ${done ? "" : 'stroke-dasharray="5 4"'}/>`;
    }
  }
  for (const t of tasks) {
    const { x, y } = pos[t.id];
    const c = DAG_COLORS[t.status] || "#7d8aa5";
    const active = ["queued", "running"].includes(t.status);
    const title = t.title.length > 24 ? t.title.slice(0, 23) + "…" : t.title;
    const runtime = t.origin === "runtime";
    // History on the node: retries and model escalation are visible at a glance.
    const escalated = t.attempts >= 2;
    const line3 = `${t.status} · try ${t.attempts}${escalated ? " ⬆sonnet" : ""}`;
    const badge = runtime
      ? `<g><rect x="${x + NODE_W - 74}" y="${y - 9}" width="70" height="18" rx="9" fill="#f0abfc"/>
         <text x="${x + NODE_W - 39}" y="${y + 3}" font-size="9" fill="#0b0f14" text-anchor="middle"
           style="text-transform:uppercase;letter-spacing:.5px">added</text></g>` : "";
    nodes += `<g class="dag-node${active ? " active" : ""}" data-task="${t.id}">
      <rect x="${x}" y="${y}" width="${NODE_W}" height="${NODE_H}" rx="10"
        fill="#1e2532" stroke="${c}" stroke-width="${runtime ? 2 : 1.6}"
        ${runtime ? 'stroke-dasharray="2 0"' : ""}/>
      <text x="${x + 12}" y="${y + 19}" font-size="9.5" letter-spacing="1"
        fill="${c}" style="text-transform:uppercase">#${t.id} ${t.role.toUpperCase()}</text>
      <text x="${x + 12}" y="${y + 37}" font-size="12" fill="#dbe2ef">${escapeHtml(title)}</text>
      <text x="${x + 12}" y="${y + 54}" font-size="10" fill="#7d8aa5">${line3}</text>
      ${badge}
      <title>${escapeHtml(t.title)}${runtime ? " (added at runtime)" : ""}\n\n${escapeHtml(t.description.slice(0, 600))}</title>
    </g>`;
  }
  svg.setAttribute("width", Math.max(width, 400));
  svg.setAttribute("height", Math.max(height, 120));
  svg.innerHTML = `
    <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#3d485f"/></marker></defs>
    ${edges}${nodes}`;
}

$("#dagSvg").addEventListener("click", (ev) => {
  const g = ev.target.closest("[data-task]");
  if (g) showTask(Number(g.dataset.task));
});
$("#directiveForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("#directiveInput");
  const text = input.value.trim();
  if (!text || !currentProject) return;
  input.value = "";
  try {
    await api(`/api/projects/${currentProject}/directive`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
  } catch (e) { alert(e.message); }
});
$("#closeTaskBtn").addEventListener("click", () => $("#taskDialog").close());
$("#taskDialog").addEventListener("click", (ev) => {
  if (ev.target === $("#taskDialog")) $("#taskDialog").close();
});

// --- artifacts / public link ------------------------------------------------
async function showArtifacts() {
  if (!currentProject) return;
  $("#artifactsBody").innerHTML = `<pre class="dim">loading…</pre>`;
  $("#artifactsDialog").showModal();
  let a;
  try { a = await api(`/api/projects/${currentProject}/artifacts`); }
  catch (e) { $("#artifactsBody").innerHTML = `<pre class="dim">${escapeHtml(e.message)}</pre>`; return; }
  const prs = (a.prs || []).map((p) =>
    `<li><a href="${p.url}" target="_blank">PR #${p.number}</a> ${p.merged ? "✓ merged" : p.state} — ${escapeHtml(p.title)}</li>`).join("");
  const publicLink = a.pages_url
    ? `<div class="public-link">🌐 Public site: <a href="${a.pages_url}" target="_blank">${a.pages_url}</a>
        <span class="hint">(may take a minute to go live after first publish)</span></div>`
    : `<button id="publishBtn" class="primary">🌐 Publish to a public link</button>
       <p class="hint">Enables GitHub Pages. Works when the built site's index.html is at the repo root or /docs.</p>`;
  $("#artifactsBody").innerHTML = `
    <div class="art-repo">📁 <a href="${a.repo_url}" target="_blank">${escapeHtml(a.repo || "no repo")}</a></div>
    ${publicLink}
    <h3>Pull requests</h3><ul class="art-list">${prs || "<li class='dim'>none yet</li>"}</ul>
    <h3>Branches</h3><div class="art-branches">${(a.branches || []).map((b) => `<span class="tag">${escapeHtml(b)}</span>`).join("") || "<span class='dim'>none</span>"}</div>`;
  const pub = $("#publishBtn");
  if (pub) pub.addEventListener("click", async () => {
    pub.disabled = true; pub.textContent = "Publishing…";
    try {
      const r = await api(`/api/projects/${currentProject}/publish`, { method: "POST" });
      showArtifacts();  // reload to show the live link
      window.open(r.url, "_blank");
    } catch (e) { pub.disabled = false; pub.textContent = "🌐 Publish to a public link"; alert(e.message); }
  });
}
$("#artifactsBtn").addEventListener("click", showArtifacts);
$("#closeArtifactsBtn").addEventListener("click", () => $("#artifactsDialog").close());

// --- editable DAG: add + edit tasks -----------------------------------------
let editingTaskId = null;

function openAddTask(prefill) {
  const sel = $("#addTaskRole");
  sel.innerHTML = knownRoles.map((r) => `<option>${r}</option>`).join("") +
    `<option value="__c">+ custom…</option>`;
  const form = $("#addTaskForm");
  form.reset();
  $("#addTaskError").hidden = true;
  editingTaskId = prefill ? prefill.id : null;
  $("#addTaskTitle").textContent = prefill ? `Edit task #${prefill.id}` : "Add a task to the DAG";
  $("#addTaskSubmit").textContent = prefill ? "Save changes" : "Add to DAG";
  if (prefill) {
    if (!knownRoles.includes(prefill.role)) sel.insertAdjacentHTML("afterbegin", `<option>${prefill.role}</option>`);
    sel.value = prefill.role;
    sel.disabled = true;
    form.title.value = prefill.title;
    form.description.value = prefill.description;
    let deps = []; try { deps = JSON.parse(prefill.deps || "[]"); } catch { /* */ }
    form.depends_on.value = deps.join(", ");
  } else {
    sel.disabled = false;
  }
  $("#addTaskDialog").showModal();
}

$("#addTaskRole").addEventListener("change", (e) => {
  if (e.target.value === "__c") {
    const name = prompt("Custom role name:");
    const clean = (name || "").trim().toLowerCase().replace(/\s+/g, "-");
    if (clean) {
      if (!knownRoles.includes(clean)) knownRoles.push(clean);
      e.target.insertAdjacentHTML("afterbegin", `<option>${clean}</option>`);
      e.target.value = clean;
    } else e.target.selectedIndex = 0;
  }
});
$("#addTaskBtn").addEventListener("click", () => openAddTask(null));
$("#closeAddTaskBtn").addEventListener("click", () => $("#addTaskDialog").close());
$("#editTaskBtn").addEventListener("click", () => {
  const t = lastTasks.find((x) => x.id === editTaskTarget);
  if (t) { $("#taskDialog").close(); openAddTask(t); }
});

$("#addTaskForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const deps = String(f.get("depends_on") || "").split(",").map((s) => Number(s.trim())).filter(Boolean);
  const err = $("#addTaskError");
  err.hidden = true;
  try {
    if (editingTaskId) {
      await api(`/api/tasks/${editingTaskId}/edit`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: f.get("title"), description: f.get("description"), depends_on: deps }),
      });
    } else {
      await api(`/api/projects/${currentProject}/tasks`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: f.get("role"), title: f.get("title"),
          description: f.get("description"), depends_on: deps }),
      });
    }
    $("#addTaskDialog").close();
    refreshBoard();
  } catch (e) { err.textContent = e.message; err.hidden = false; }
});

document.querySelectorAll(".vchip").forEach((chip) =>
  chip.addEventListener("click", () => {
    document.querySelectorAll(".vchip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    const dagMode = chip.dataset.v === "dag";
    $("#dag").hidden = !dagMode;
    $("#board").hidden = dagMode;
    if (dagMode) renderDag(lastTasks);
  }),
);

document.querySelectorAll(".chips .chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    document.querySelectorAll(".chips .chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    $("#feed").dataset.filter = chip.dataset.f;
  }),
);

// --- recruiting wizard ------------------------------------------------------
let knownRoles = ["backend", "frontend", "tester"];

function rosterRow(member) {
  const row = document.createElement("div");
  row.className = "roster-row";
  // Role is a free-text field with suggestions — type any custom role directly.
  row.innerHTML = `
    <input class="r-role" list="rolesList" placeholder="role (e.g. designer)"
      value="${(member.role || "").replace(/"/g, "&quot;")}" title="role — type anything">
    <input class="r-count" type="number" min="1" max="6" value="${member.count || 1}" title="how many to hire">
    <select class="r-model" title="model tier">
      <option value="worker" ${member.model !== "lead" ? "selected" : ""}>cheap (Haiku)</option>
      <option value="lead" ${member.model === "lead" ? "selected" : ""}>pro (Sonnet)</option>
    </select>
    <button type="button" class="r-fire" title="remove">✕</button>`;
  row.querySelector(".r-fire").addEventListener("click", () => {
    const rows = [...row.parentNode.children];
    if (rows.length > 1) row.remove();
  });
  return row;
}

function readRoster() {
  return [...document.querySelectorAll("#roster .roster-row")].map((row) => ({
    role: row.querySelector(".r-role").value.trim().toLowerCase().replace(/\s+/g, "-"),
    count: Number(row.querySelector(".r-count").value) || 1,
    model: row.querySelector(".r-model").value,
  })).filter((m) => m.role);
}

function renderRoster(members) {
  const box = $("#roster");
  box.innerHTML = "";
  for (const m of members) box.appendChild(rosterRow(m));
  const dl = $("#rolesList");
  if (dl) dl.innerHTML = knownRoles.map((r) => `<option value="${r}">`).join("");
}

const dialog = $("#newProjectDialog");
function showStep(n) {
  $("#step1").hidden = n !== 1;
  $("#step2").hidden = n !== 2;
}
const openDialog = () => { $("#formError").hidden = true; showStep(1); dialog.showModal(); };
$("#projectSelect").addEventListener("change", (e) => selectProject(e.target.value));
$("#homeLink").addEventListener("click", showHome);
$("#homeLink").style.cursor = "pointer";
$("#newProjectBtn").addEventListener("click", openDialog);
$("#homeNewBtn").addEventListener("click", openDialog);
$("#backToBriefBtn").addEventListener("click", () => showStep(1));
$("#addRoleBtn").addEventListener("click", () =>
  renderRoster(readRoster().concat([{ role: knownRoles[0] || "backend", count: 1, model: "worker" }])));

$("#toRecruitBtn").addEventListener("click", async () => {
  const form = $("#newProjectForm");
  const err = $("#formError");
  err.hidden = true;
  if (!form.name.value.trim() || !form.brief.value.trim()) {
    err.textContent = "Project name and brief are required."; err.hidden = false; return;
  }
  const btn = $("#toRecruitBtn");
  btn.disabled = true; btn.textContent = "Sizing up the work…";
  $("#recruitNote").textContent = "Sizing up your brief…";
  showStep(2);
  renderRoster([]);
  try {
    const res = await api("/api/suggest-team", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brief: form.brief.value }),
    });
    knownRoles = [...new Set([...(res.known_roles || knownRoles), ...res.team.map((m) => m.role)])];
    renderRoster(res.team.length ? res.team : [{ role: "backend", count: 1, model: "worker" }]);
    $("#recruitNote").textContent = "Suggested from your brief — hire, fire, bump the count, or add whoever you want.";
  } catch (e) {
    renderRoster([{ role: "backend", count: 1, model: "worker" }, { role: "tester", count: 1, model: "worker" }]);
    $("#recruitNote").textContent = "Couldn't auto-suggest — here's a default team to edit.";
  } finally {
    btn.disabled = false; btn.textContent = "Recruit team →";
  }
});
$("#closeDialogBtn").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
$("#cancelBtn").addEventListener("click", async () => {
  if (currentProject && confirm("Cancel this project?"))
    await api(`/api/projects/${currentProject}/cancel`, { method: "POST" });
});
$("#restartBtn").addEventListener("click", async () => {
  if (!currentProject) return;
  try {
    await api(`/api/projects/${currentProject}/restart`, { method: "POST" });
    refreshBoard();
  } catch (e) { alert(e.message); }
});
$("#newProjectForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const errBox = $("#formError2");
  errBox.hidden = true;
  const f = new FormData(form);
  const btn = $("#createBtn");
  btn.disabled = true;
  btn.textContent = "Hiring…";
  try {
    const res = await api("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: f.get("name"), brief: f.get("brief"), repo: f.get("repo"),
        max_workers: Number(f.get("max_workers")), max_runs: Number(f.get("max_runs")),
        team: readRoster(),
      }),
    });
    dialog.close();
    form.reset();
    openProject(res.id);
  } catch (e) {
    errBox.textContent = e.message;
    errBox.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Hire team & start ▶";
  }
});

(async () => {
  await loadHealth();
  showHome();
  connectWs();
})();
setInterval(() => { if (currentProject) refreshBoard(); else loadProjects(); }, 10000);
