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
  await refreshBoard();
  const events = await api(`/api/projects/${id}/events`);
  for (const e of events.slice(-200)) renderEvent(e);
}

async function refreshBoard() {
  if (!currentProject) return;
  let p;
  try { p = await api(`/api/projects/${currentProject}`); } catch { return; }
  $("#costBadge").textContent = `$${p.cost_usd.toFixed(2)} / $${p.budget_usd.toFixed(2)}`;
  $("#statusBadge").textContent = p.status;
  $("#cancelBtn").hidden = ["done", "failed", "cancelled"].includes(p.status);
  for (const [col, statuses] of Object.entries(COLS)) {
    const box = document.querySelector(`.col[data-col="${col}"] .cards`);
    box.innerHTML = "";
    for (const t of p.tasks.filter((t) => statuses.includes(t.status))) {
      const card = document.createElement("div");
      card.className = `card ${t.status}`;
      const links = [];
      if (t.issue_number && p.repo) links.push(`<a target="_blank" href="https://github.com/${p.repo}/issues/${t.issue_number}">#${t.issue_number}</a>`);
      if (t.pr_number && p.repo) links.push(`<a target="_blank" href="https://github.com/${p.repo}/pull/${t.pr_number}">PR ${t.pr_number}</a>`);
      card.innerHTML = `
        <div class="role ${t.role}">${t.role}</div>
        <div class="title">${escapeHtml(t.title)}</div>
        <div class="meta">${t.status} · try ${t.attempts} · $${t.cost_usd.toFixed(2)} ${links.join(" ")}</div>`;
      card.title = t.description;
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
  if (e.kind === "tool_use") cls += " tool";
  if (e.kind === "error" || e.kind === "worker_died") cls += " error";
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
    if (["project_created", "project_finished", "project_cancelled"].includes(e.kind)) loadProjects();
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
}

$("#projectSelect").addEventListener("change", (e) => selectProject(e.target.value));
$("#newProjectBtn").addEventListener("click", () => $("#newProjectDialog").showModal());
$("#cancelBtn").addEventListener("click", async () => {
  if (currentProject && confirm("Cancel this project?"))
    await api(`/api/projects/${currentProject}/cancel`, { method: "POST" });
});
$("#newProjectForm").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  const f = new FormData(ev.target);
  try {
    const res = await api("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: f.get("name"), brief: f.get("brief"), repo: f.get("repo"),
        budget_usd: Number(f.get("budget_usd")), max_workers: Number(f.get("max_workers")),
      }),
    });
    await loadProjects();
    selectProject(res.id);
  } catch (e) { alert(e.message); }
});

loadProjects();
connectWs();
setInterval(refreshBoard, 10000);
