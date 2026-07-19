const $ = (s) => document.querySelector(s);
let currentProject = null;
let ws = null;

const COLS = {
  planned: ["planned"],
  working: ["queued", "running"],
  review: ["pushed", "review", "changes_requested"],
  done: ["done", "failed"],
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

async function loadProjects() {
  const projects = await api("/api/projects");
  const sel = $("#projectSelect");
  sel.innerHTML = "";
  for (const p of projects) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = `#${p.id} ${p.name} [${p.status}]`;
    sel.appendChild(opt);
  }
  if (projects.length && !currentProject) selectProject(projects[0].id);
  else if (currentProject) sel.value = currentProject;
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
  badge.textContent = p.status;
  badge.className = "badge " +
    ({ done: "ok", failed: "bad", cancelled: "bad", review: "warn" }[p.status] || "run");
  badge.title = p.summary || "";
  $("#cancelBtn").hidden = ["done", "failed", "cancelled"].includes(p.status);
  $("#restartBtn").hidden = !["failed", "review", "cancelled"].includes(p.status);
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
  div.innerHTML = `<div class="src">${escapeHtml(e.source)} · ${escapeHtml(e.kind)} · ${when}</div>${escapeHtml(text)}`;
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
    renderEvent(e);
    refreshBoard();
    if (e.kind === "boss_question" || e.kind === "answered") refreshQuestion();
    if (["project_created", "project_finished", "project_cancelled"].includes(e.kind)) loadProjects();
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
function showTask(id) {
  const t = lastTasks.find((x) => x.id === id);
  if (!t) return;
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
    ${t.report ? `<h3>Worker report</h3><pre>${escapeHtml(t.report)}</pre>` : ""}`;
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

const dialog = $("#newProjectDialog");
$("#projectSelect").addEventListener("change", (e) => selectProject(e.target.value));
$("#newProjectBtn").addEventListener("click", () => {
  $("#formError").hidden = true;
  dialog.showModal();
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
  const errBox = $("#formError");
  errBox.hidden = true;
  if (!form.reportValidity()) return;
  const f = new FormData(form);
  const btn = $("#createBtn");
  btn.disabled = true;
  btn.textContent = "Starting…";
  try {
    const res = await api("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: f.get("name"), brief: f.get("brief"), repo: f.get("repo"),
        budget_usd: Number(f.get("budget_usd")), max_workers: Number(f.get("max_workers")),
      }),
    });
    dialog.close();
    form.reset();
    await loadProjects();
    selectProject(res.id);
  } catch (e) {
    errBox.textContent = e.message;   // keep the dialog open, keep the typed values
    errBox.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Start team";
  }
});

loadHealth();
loadProjects();
connectWs();
setInterval(refreshBoard, 10000);
