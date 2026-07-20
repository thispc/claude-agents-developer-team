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
let me = { signed_in: false };

// --- sign-in / settings -----------------------------------------------------
const PERSONAS = {
  "": "",
  ruthless: "You are a ruthless perfectionist. Nothing ships unless it is genuinely excellent. " +
    "Reject work that is merely adequate, demand rigorous evidence for every claim, and hire " +
    "specialists rather than generalists. You would rather do another round than ship something mediocre.",
  shipper: "You are a pragmatic shipper. Bias hard toward a working end-to-end result over " +
    "polish. Keep the team small, cut scope aggressively when it protects the deadline, and " +
    "accept good-enough work that demonstrably functions. Still verify it actually runs.",
  researcher: "You are a deep researcher. Before building, make sure the approach is sound — " +
    "have the team investigate, compare options, and document findings with sources. Hire " +
    "analytical specialists, insist on reasoning and evidence, and challenge unsupported assumptions hard.",
};

async function loadMe() {
  try { me = await api("/api/me"); } catch { me = { signed_in: false }; }
  $("#loginScreen").hidden = !!me.signed_in;
  document.querySelector("header").hidden = !me.signed_in;
  if (!me.signed_in) { $("#home").hidden = true; $("main").hidden = true; }
  return me.signed_in;
}

$("#loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const err = $("#loginError");
  err.hidden = true;
  try {
    await api("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: f.get("username"), password: f.get("password") }),
    });
    await boot();
  } catch (e) { err.textContent = e.message; err.hidden = false; }
});

$("#signupBtn").addEventListener("click", async () => {
  const f = new FormData($("#loginForm"));
  const err = $("#loginError");
  err.hidden = true;
  try {
    await api("/api/signup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: f.get("username"), password: f.get("password") }),
    });
    await boot();
    $("#settingsBtn").click();   // straight to Settings — they need their own credentials
  } catch (e) { err.textContent = e.message; err.hidden = false; }
});

$("#logoutBtn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  location.reload();
});

$("#settingsBtn").addEventListener("click", async () => {
  await loadMe();
  const s = me.settings || {};
  $("#settingsWho").textContent = `Signed in as ${me.username}${me.is_root ? " (root)" : ""}`;
  $("#ghState").textContent = s.github_token_set ? "— currently set ✓" : "— not set";
  const oa = $("#oaState"), gm = $("#gmState");
  if (oa) oa.textContent = s.openai_api_key_set ? "— currently set ✓" : "— not set";
  if (gm) gm.textContent = s.gemini_api_key_set ? "— currently set ✓" : "— not set";
  $("#keyState").textContent = s.anthropic_api_key_set ? "— currently set ✓" : "— not set";
  $("#subState").textContent = s.claude_oauth_token_set ? "— currently set ✓" : "— not set";
  $("#settingsError").hidden = true;
  $("#settingsForm").reset();
  $("#settingsDialog").showModal();
});
$("#closeSettingsBtn").addEventListener("click", () => $("#settingsDialog").close());
$("#settingsForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const body = {};
  if (f.get("github_token")) body.github_token = f.get("github_token");
  if (f.get("anthropic_api_key")) body.anthropic_api_key = f.get("anthropic_api_key");
  if (f.get("claude_oauth_token")) body.claude_oauth_token = f.get("claude_oauth_token");
  try {
    await api("/api/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    $("#settingsDialog").close();
    loadMe().then(loadRepos);   // token may have just been added
  } catch (e) { $("#settingsError").textContent = e.message; $("#settingsError").hidden = false; }
});

async function loadRepos() {
  // A brand-new user has no GitHub token yet, and the endpoint 400s without one —
  // calling it anyway logged a console error on every first sign-in. The datalist
  // is only a convenience; the repo field still accepts a typed name.
  if (!me.settings || !me.settings.github_token_set) return;
  try {
    const r = await api("/api/github/repos");
    $("#repoList").innerHTML = r.repos.map((x) => `<option value="${x.full_name}">`).join("");
  } catch { /* no token yet — the field still accepts a typed name */ }
}

$("#personaPreset").addEventListener("change", (e) => {
  const t = $("#personaText");
  if (e.target.value === "custom") { t.hidden = false; t.value = ""; t.focus(); }
  else { t.hidden = true; t.value = PERSONAS[e.target.value] || ""; }
});
const taskCost = (t) => (authMode === "subscription" ? "" : ` · $${t.cost_usd.toFixed(2)}`);

async function loadHealth() {
  try {
    const h = await api("/api/health");
    authMode = h.auth || "none";
    const b = $("#authBadge");
    // Say what it MEANS, not what it is called. "auth: Max subscription" told you
    // nothing about whether you were about to be charged.
    if (authMode === "subscription") {
      b.textContent = "⚡ On your Claude plan · no token charges";
      b.className = "badge ok";
      b.title = "Agents run on your Claude subscription, so nothing is billed per token. "
        + "Any dollar figures are estimates for budgeting only. Your real limit is the "
        + "agent-run cap and your plan's rate limits. Click to change credentials.";
    } else if (authMode === "api-key") {
      b.textContent = "💳 API key · billed per token";
      b.className = "badge warn";
      b.title = "Agents spend pay-per-token API credit — this costs real money. Figures "
        + "shown are the SDK's estimate; the authoritative balance is at "
        + "console.anthropic.com. Click to change credentials.";
    } else {
      b.textContent = "⚠ No AI credentials — agents cannot run";
      b.className = "badge bad";
      b.title = "Add an Anthropic API key or a Claude subscription token before starting "
        + "a project. Click to open Settings.";
    }
    b.style.cursor = "pointer";
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
    if (p.is_self) tr.className = "self-row";
    tr.innerHTML = `
      <td>${p.id}</td>
      <td class="pname">${escapeHtml(p.name)}${p.is_self
        ? ` <span class="self-tag" title="This project is the platform you are using right now">⟲ this platform</span>` : ""}</td>
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

function showHome(skipHash) {
  const pl = $("#plan"); if (pl) pl.hidden = true;
  if (!skipHash) setHash("#/");
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

function openProject(id, view, skipHash) {
  $("#home").hidden = true;
  $("main").hidden = false;
  $("#projectSelect").hidden = false;
  currentProject = Number(id);
  // Must finish before selectProject sets .value, or the assignment lands on an
  // empty <select> and is silently dropped (the dropdown then shows the wrong project).
  const filled = loadProjects();
  if (view) switchView(view, true);
  filled.then(() => { $("#projectSelect").value = String(id); });
  if (!skipHash) setHash(`#/p/${id}${view && view !== "command" ? "/" + view : ""}`);
  selectProject(id);
}

// --- URL routing: the address bar reflects where you are, so refresh (and the
// browser back button) keep your place instead of dumping you on the home page.
let suppressHash = false;
function setHash(h) {
  if (location.hash === h) return;
  suppressHash = true;
  location.hash = h;
  setTimeout(() => { suppressHash = false; }, 0);
}

function switchView(view, skipHash) {
  document.querySelectorAll(".vchip").forEach((c) =>
    c.classList.toggle("active", c.dataset.v === view));
  for (const id of ["command", "board", "dag", "artifacts", "agents", "chat", "blockers", "self"])
    $("#" + id).hidden = id !== view;
  if (view === "dag") renderDag(lastTasks);
  if (view === "command" && lastProject) renderCommand(lastProject);
  if (view === "artifacts") renderArtifacts(true);
  if (view === "agents") { agentsSig = ""; renderAgents(); }
  if (view === "chat") { chatSig = ""; renderChat(); markChatRead(); }
  if (view === "blockers") { blockersSig = ""; renderBlockers(); }
  if (view === "self") renderSelf();
  if (!skipHash && currentProject)
    setHash(`#/p/${currentProject}${view !== "command" ? "/" + view : ""}`);
}

function route() {
  if (suppressHash) return;
  const plan = location.hash.match(/^#\/plan(?:\/(\d+))?/);
  if (plan) {
    openPlan();
    if (plan[1]) {
      $("#planSetup").hidden = true; $("#planStage").hidden = false;
      pollTable(Number(plan[1]));
    }
    return;
  }
  const m = location.hash.match(/^#\/p\/(\d+)(?:\/(\w+))?/);
  if (m) openProject(Number(m[1]), m[2] || "command", true);
  else showHome(true);
}
window.addEventListener("hashchange", route);

async function selectProject(id) {
  currentProject = Number(id);
  $("#projectSelect").value = id;
  $("#events").innerHTML = "";
  $("#feedTitle").textContent = "Activity";
  managerThought = ""; managerThinking = ""; agentActivity = {}; pendingQ = null;
  allEvents = []; chatSig = ""; chatSeen = 0;
  const events = await api(`/api/projects/${id}/events`);
  for (const e of events.slice(-250)) { allEvents.push(e); noteActivity(e); renderEvent(e); }
  markChatRead();          // history is not "unread"
  renderChat();
  await refreshBoard();
}

// Tasks are stored under a globally-unique id but shown under a per-project
// number (seq), so the boss's third project starts at #1 rather than #35.
function seqOf(id) {
  const t = lastTasks.find((x) => x.id === Number(id));
  return t ? (t.seq || t.id) : id;
}
function idOfSeq(n) {
  const t = lastTasks.find((x) => (x.seq || x.id) === Number(n));
  return t ? t.id : Number(n);
}
function depLabels(deps) { return deps.map((d) => "#" + seqOf(d)); }

// --- The platform working on itself. Root raises an issue against this repo,
// --- the team fixes it on a branch, and root deploys the merged result.
let selfInfo = null;

async function renderSelf() {
  const el = $("#self");
  if (!el) return;
  let d;
  try { d = await api("/api/self"); } catch { el.innerHTML = "<p class='empty'>Only the root operator can work on the platform itself.</p>"; return; }
  selfInfo = d;
  const h = d.head || {};
  const dep = d.last_deploy || {};
  el.innerHTML = `
    <div class="self-banner">
      <div class="self-i">⟲</div>
      <div>
        <h2>This is the platform itself</h2>
        <p>Issues you raise here become real work on <b>${escapeHtml(d.repo || "this repo")}</b> —
        the code running this page. Your team fixes it on a branch and opens a PR;
        nothing reaches the live app until you deploy it.</p>
      </div>
    </div>

    <div class="self-grid">
      <div class="self-card">
        <h3>Running right now</h3>
        <div class="kv"><span>commit</span><code>${escapeHtml(h.commit || "?")}</code></div>
        <div class="kv"><span>branch</span><code>${escapeHtml(h.branch || "?")}</code></div>
        <div class="kv"><span>message</span><span>${escapeHtml(trim(h.subject || "", 70))}</span></div>
        <div class="kv"><span>local edits</span><span>${h.dirty === "yes"
          ? "<b class='warn-t'>uncommitted changes present</b>" : "none — clean"}</span></div>
        ${dep.deployed ? `<div class="kv"><span>last deploy</span><span>${escapeHtml(dep.deployed)}
          (rollback to ${escapeHtml(dep.rollback_to || "—")})</span></div>` : ""}
      </div>

      <div class="self-card">
        <h3>Deploy</h3>
        ${d.can_redeploy
          ? `<p>The live tree is clean and no agents are mid-run. Deploying pulls the
             merged code and restarts this app on it.</p>`
          : `<p class="warn-t">Not safe to deploy right now:</p>
             <ul class="why">${(d.blocked_reasons || []).map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>`}
        <div class="self-acts">
          <button id="deployBtn" class="primary" ${d.can_redeploy ? "" : "disabled"}>⬆ Pull &amp; restart</button>
          ${dep.rollback_to ? `<button id="rollbackBtn" class="danger">↩ Roll back to ${escapeHtml(dep.rollback_to)}</button>` : ""}
        </div>
        <p class="hint">The new code must import cleanly or the deploy is refused and reverted automatically.</p>
      </div>
    </div>

    <div class="self-card">
      <h3>Raise an issue against this platform</h3>
      <form id="selfIssueForm">
        <label>What's wrong (or what should be better)
          <input name="title" required placeholder="e.g. Agents tab loses scroll position" autocomplete="off"></label>
        <label>Details <span class="hint">steps to reproduce, what you expected, which page</span>
          <textarea name="body" rows="5" required placeholder="Be specific — this becomes the spec a worker builds against."></textarea></label>
        <label>Kind
          <select name="severity">
            <option value="bug">Bug — something is broken</option>
            <option value="improvement">Improvement — it works but could be better</option>
            <option value="urgent">Urgent — breaking the platform right now</option>
          </select></label>
        <p id="selfErr" class="form-error" hidden></p>
        <button type="submit" class="primary">🔧 Put the team on it</button>
        <p class="hint">Your manager plans the fix, assigns it, and reviews the PR.
        Nothing reaches the running app until you deploy it above.</p>
      </form>
    </div>

    <div class="self-card">
      <h3>Open work on the platform</h3>
      ${(d.open_issues || []).length ? `<table class="self-table">
        <thead><tr><th>#</th><th>Issue</th><th>Status</th><th>PR</th></tr></thead>
        <tbody>${d.open_issues.map((t) => `<tr>
          <td>#${t.seq}</td><td>${escapeHtml(t.title)}</td>
          <td><span class="pill ${t.status === "failed" ? "crit" : "warn"}">${t.status}</span></td>
          <td>${t.pr ? `<a target="_blank" href="https://github.com/${d.repo}/pull/${t.pr}">#${t.pr}</a>` : "—"}</td>
        </tr>`).join("")}</tbody></table>` : "<p class='empty'>Nothing open. The platform has no self-reported issues.</p>"}
      ${(d.shipped || []).length ? `<h4>Shipped</h4><ul class="shipped">${
        d.shipped.map((t) => `<li>#${t.seq} ${escapeHtml(t.title)}${
          t.pr ? ` — PR #${t.pr}` : ""}</li>`).join("")}</ul>` : ""}
    </div>`;

  const form = $("#selfIssueForm");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(form);
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true; btn.textContent = "briefing the team…";
    try {
      const r = await api("/api/self/issue", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: f.get("title"), body: f.get("body"),
                               severity: f.get("severity") }) });
      openProject(r.project_id, "command");
    } catch (e) {
      $("#selfErr").hidden = false; $("#selfErr").textContent = String(e.message || e);
      btn.disabled = false; btn.textContent = "🔧 Put the team on it";
    }
  });

  const dbtn = $("#deployBtn");
  if (dbtn) dbtn.addEventListener("click", async () => {
    if (!confirm("Pull the merged code and restart the platform?\n\n"
      + "This app will be unavailable for a few seconds. If the new code fails to "
      + "import it is reverted automatically.")) return;
    dbtn.disabled = true; dbtn.textContent = "deploying…";
    const r = await api("/api/self/redeploy", { method: "POST" });
    if (!r.ok) { alert("Deploy refused: " + r.error); dbtn.disabled = false;
                 dbtn.textContent = "⬆ Pull & restart"; return; }
    waitForRestart(`Deployed ${r.to.commit} — ${r.to.subject}`);
  });
  const rbtn = $("#rollbackBtn");
  if (rbtn) rbtn.addEventListener("click", async () => {
    if (!confirm("Roll the platform back to the previous commit and restart?")) return;
    const r = await api("/api/self/rollback", { method: "POST" });
    if (!r.ok) { alert("Rollback failed: " + r.error); return; }
    waitForRestart("Rolled back — reconnecting…");
  });
}

// The server replaces its own process, so poll until it answers again.
function waitForRestart(msg) {
  const el = $("#self");
  el.innerHTML = `<div class="self-restart"><div class="spinner"></div>
    <h2>${escapeHtml(msg)}</h2><p>Waiting for the platform to come back up…</p></div>`;
  let tries = 0;
  const t = setInterval(async () => {
    tries++;
    try {
      const r = await fetch("/api/me", { credentials: "same-origin" });
      if (r.ok) { clearInterval(t); location.reload(); }
    } catch { /* still down */ }
    if (tries > 60) { clearInterval(t);
      el.querySelector("p").textContent =
        "It hasn't come back. Check the server logs — the previous commit is recorded for rollback."; }
  }, 2000);
}

// Brief, non-blocking confirmation of something that already happened.
function toast(msg) {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 4000);
}

// ===== PLAN MODE: the round table ==========================================
// A circle of heterogeneous models argues an idea into a blueprint. The seating
// rules are evidence-led — see docs/ROUNDTABLE_DESIGN.md. The one that matters:
// mixing PROVIDERS is what makes deliberation beat just asking one model.

let providerCatalog = { providers: [], available: [] };
let seats = [];
let currentTable = null;
let seatSeq = 0;

const PERSONA_PRESETS = [
  ["Systems architect", "You think in structure: boundaries, data flow, failure modes. You care about what this looks like at 100x the initial scale."],
  ["Pragmatic shipper", "You optimise for the shortest path to something real in a user's hands. You are suspicious of architecture that outruns the problem."],
  ["Domain skeptic", "You interrogate whether the problem is even the right problem, and whether anyone actually wants this. You look for the unstated assumption."],
  ["User advocate", "You speak for the person who has to use this. Confusing flows and unexplained states are defects to you."],
  ["Risk & security", "You look for what breaks, leaks, or gets abused — data, cost, privacy, dependencies."],
  ["Researcher", "You look for prior art and evidence. You would rather cite how this has been solved than reinvent it."],
];

async function openPlan() {
  $("#home").hidden = true; $("main").hidden = true; $("#plan").hidden = false;
  $("#planSetup").hidden = false; $("#planStage").hidden = true;
  $("#blueprintPanel").hidden = true;
  setHash("#/plan");
  try { providerCatalog = await api("/api/providers"); } catch { /* shown below */ }
  if (!seats.length) seedSeats();
  renderSeats();
  renderModSelect();
}

// Default table: deliberately spread across whatever providers you have keys for,
// because homogeneous tables are the case the research says does not work.
function seedSeats() {
  const avail = providerCatalog.available || [];
  const pick = (i) => avail.length ? avail[i % avail.length] : "anthropic";
  const defaults = [
    { persona: PERSONA_PRESETS[0], },
    { persona: PERSONA_PRESETS[1], },
    { persona: PERSONA_PRESETS[2], },
    { persona: PERSONA_PRESETS[4], },
  ];
  seats = defaults.map((d, i) => {
    const prov = pick(i);
    const ms = modelsFor(prov);
    // Spread across providers first; when only one provider has a key, spread
    // across ITS models instead. Identical seats are the configuration the
    // research says does not work, so never seed one by default.
    const model = avail.length > 1
      ? (ms[0]?.id || "")
      : (ms[i % Math.max(ms.length, 1)]?.id || ms[0]?.id || "");
    return { uid: ++seatSeq, name: d.persona[0], provider: prov,
             model, persona: d.persona[1] };
  });
}

function modelsFor(provider) {
  const p = (providerCatalog.providers || []).find((x) => x.id === provider);
  return p ? p.models : [];
}
function providerLabel(id) {
  const p = (providerCatalog.providers || []).find((x) => x.id === id);
  return p ? p.label : id;
}

function renderSeats() {
  const el = $("#seatList");
  const avail = providerCatalog.available || [];
  el.innerHTML = seats.map((s, i) => `
    <div class="seat-row" data-uid="${s.uid}">
      <span class="seat-dot prov-${s.provider}">${i + 1}</span>
      <input class="seat-name" value="${escapeHtml(s.name)}" placeholder="Name" data-f="name">
      <select class="seat-prov" data-f="provider">
        ${(providerCatalog.providers || []).map((p) => `
          <option value="${p.id}" ${p.id === s.provider ? "selected" : ""}
            ${avail.includes(p.id) ? "" : "disabled"}>
            ${escapeHtml(p.label)}${avail.includes(p.id) ? "" : " — no key"}
          </option>`).join("")}
      </select>
      <select class="seat-model" data-f="model">
        ${modelsFor(s.provider).map((m) => `
          <option value="${m.id}" ${m.id === s.model ? "selected" : ""}>${escapeHtml(m.label)}</option>`).join("")}
      </select>
      <input class="seat-persona" value="${escapeHtml(s.persona)}" placeholder="How this seat thinks…" data-f="persona">
      <button class="seat-del" data-del="${s.uid}" title="Remove this seat">✕</button>
    </div>`).join("");

  el.querySelectorAll(".seat-row").forEach((row) => {
    const uid = Number(row.dataset.uid);
    row.querySelectorAll("[data-f]").forEach((inp) =>
      inp.addEventListener("change", () => {
        const s = seats.find((x) => x.uid === uid);
        s[inp.dataset.f] = inp.value;
        if (inp.dataset.f === "provider") s.model = modelsFor(s.provider)[0]?.id || "";
        renderSeats();
      }));
    row.querySelector("[data-del]").addEventListener("click", () => {
      if (seats.length <= 3) { flashSeatWarn("A round table needs at least 3 seats."); return; }
      seats = seats.filter((x) => x.uid !== uid);
      renderSeats();
    });
  });
  updateSeatWarning();
}

function flashSeatWarn(msg) {
  const w = $("#seatWarn"); w.hidden = false; w.textContent = msg;
  setTimeout(() => { w.hidden = true; }, 4000);
}

// The honest warning: identical seats are the case that does NOT work.
function updateSeatWarning() {
  const w = $("#seatWarn");
  const provs = new Set(seats.map((s) => s.provider));
  const combos = new Set(seats.map((s) => s.provider + "/" + s.model));
  const mode = (document.querySelector("input[name=tmode]:checked") || {}).value || "debate";
  let msg = "";
  if (seats.length > 6) {
    msg = `${seats.length} seats — deliberation degrades past about 6.`;
  } else if (mode === "diverge") {
    // Parallel proposals + aggregator is Mixture-of-Agents, where the evidence
    // runs the OPPOSITE way to debate: sampling your best model repeatedly beat
    // mixing in weaker ones. So here, variety is the thing to warn about.
    if (combos.size > 1)
      msg = "Diverge mode: sampling your BEST model several times measurably beat "
          + "mixing weaker ones (quality dominates diversity). Consider one strong "
          + "model in every seat, varying only the persona.";
  } else if (combos.size === 1) {
    msg = "Every seat is the same model. That is the case research found does NOT beat asking one model once.";
  } else if (provs.size === 1) {
    msg = "All seats share one provider. Mixing providers is what measurably improves this.";
  }
  w.hidden = !msg; w.textContent = msg;
}

function renderModSelect() {
  const sel = $("#modSelect");
  const opts = [];
  (providerCatalog.providers || []).forEach((p) => {
    if (!(providerCatalog.available || []).includes(p.id)) return;
    p.models.forEach((m) => opts.push(
      `<option value="${p.id}|${m.id}">${escapeHtml(p.label)} · ${escapeHtml(m.label)}</option>`));
  });
  sel.innerHTML = opts.join("") || `<option value="">no providers configured</option>`;
}

// ---- the circle ----------------------------------------------------------

function renderCircle(seatInfo, activeIds, phase) {
  const el = $("#circle");
  const n = seatInfo.length;
  const R = 40;   // % radius inside the square wrapper
  el.innerHTML = seatInfo.map((s, i) => {
    const ang = (i / n) * 2 * Math.PI - Math.PI / 2;
    const x = 50 + R * Math.cos(ang), y = 50 + R * Math.sin(ang);
    const state = activeIds.includes(s.id) ? "speaking" : (s.done ? "done" : "");
    return `<div class="seat-node ${state} prov-${s.provider}"
        style="left:${x}%; top:${y}%" data-seat="${s.id}" title="${escapeHtml(s.model)}">
      <div class="sn-name">${escapeHtml(s.name)}</div>
      <div class="sn-model">${escapeHtml(providerLabel(s.provider))}</div>
      ${s.skeptic ? `<span class="sn-badge" title="Holds the standing skeptic brief">skeptic</span>` : ""}
    </div>`;
  }).join("") + `
    <div class="mod-node ${phase === "synthesis" ? "speaking" : ""}">
      <div class="sn-name">Moderator</div>
      <div class="sn-model">${phase === "synthesis" ? "writing the blueprint…" : "listening"}</div>
    </div>`;
}

const PHASE_NOTE = {
  propose: "Round 1 — everyone writes independently. Nobody can see anyone else yet, so nobody anchors the group.",
  critique: "Round 2 — structured dissent. Each seat must name a concrete flaw, not summarise.",
  revise: "Round 3 — each seat revises, saying what changed their mind and what did not.",
  synthesis: "The moderator weighs the arguments — not the head-count — and writes the blueprint.",
};

async function startTable() {
  const brief = $("#planBrief").value.trim();
  const err = $("#planError");
  err.hidden = true;
  if (!brief) { err.hidden = false; err.textContent = "Describe the idea first."; return; }
  const mod = ($("#modSelect").value || "").split("|");
  const btn = $("#startTableBtn");
  btn.disabled = true; btn.textContent = "convening…";
  try {
    const r = await api("/api/tables", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        brief, title: brief.slice(0, 60),
        mode: (document.querySelector("input[name=tmode]:checked") || {}).value || "debate",
        mod_provider: mod[0] || "", mod_model: mod[1] || "",
        seats: seats.map((s) => ({ name: s.name, provider: s.provider,
                                   model: s.model, persona: s.persona })),
      }),
    });
    currentTable = r.id;
    $("#planSetup").hidden = true; $("#planStage").hidden = false;
    setHash(`#/plan/${r.id}`);
    await api(`/api/tables/${r.id}/run`, { method: "POST" });
    pollTable(r.id);
  } catch (e) {
    err.hidden = false; err.textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = "▶ Convene the table";
  }
}

let tablePoll = null;
async function pollTable(id) {
  clearInterval(tablePoll);
  const tick = async () => {
    let t;
    try { t = await api(`/api/tables/${id}`); } catch { return; }
    currentTable = id;
    const spoken = {};
    (t.turns || []).forEach((x) => { if (x.seat_id) spoken[x.seat_id] = x; });
    const lastPhase = (t.turns || []).slice(-1)[0]?.phase || "propose";
    const info = (t.seats || []).map((s, i) => ({
      id: s.id, name: s.name, provider: s.provider, model: s.model,
      skeptic: i === t.seats.length - 1,
      done: !!spoken[s.id],
    }));
    renderCircle(info, [], t.status === "running" ? lastPhase : "");
    $("#phasePill").textContent = t.status === "done" ? "blueprint ready"
      : t.status === "failed" ? "failed" : `round ${({propose:1,critique:2,revise:3,synthesis:4})[lastPhase] || 1} · ${lastPhase}`;
    $("#phaseNote").textContent = t.status === "running" ? (PHASE_NOTE[lastPhase] || "") : "";
    renderTurns(t);
    if (t.status === "done" || t.status === "failed") {
      clearInterval(tablePoll);
      if (t.blueprint) renderBlueprint(t);
    }
  };
  await tick();
  tablePoll = setInterval(tick, 3000);
}

function renderTurns(t) {
  const byId = Object.fromEntries((t.seats || []).map((s) => [s.id, s]));
  const el = $("#turnFeed");
  const sig = (t.turns || []).map((x) => x.id).join(",");
  if (el.dataset.sig === sig) return;      // don't repaint unchanged
  el.dataset.sig = sig;
  el.innerHTML = (t.turns || []).map((x) => {
    const s = byId[x.seat_id];
    const who = s ? s.name : "Moderator";
    if (x.phase === "synthesis") return "";
    return `<div class="turn ${x.ok ? "" : "turn-bad"} phase-${x.phase}">
      <div class="turn-head"><b>${escapeHtml(who)}</b>
        <span class="pill">${escapeHtml(x.phase)}</span>
        ${s ? `<span class="hint">${escapeHtml(providerLabel(s.provider))} · ${escapeHtml(s.model)}</span>` : ""}</div>
      <div class="turn-body">${escapeHtml(x.text)}</div>
    </div>`;
  }).join("");
  el.scrollTop = el.scrollHeight;
}

function renderBlueprint(t) {
  const b = t.blueprint || {};
  const el = $("#blueprintPanel");
  el.hidden = false;
  const list = (arr, f) => (arr || []).map(f).join("") || "<li class='dim'>none</li>";
  el.innerHTML = `
    <div class="bp">
      <h3>📐 Blueprint</h3>
      ${b.unparsed ? `<p class="hint">The moderator answered in prose rather than JSON — shown raw below.</p>` : ""}
      ${b.restated_problem ? `<p class="bp-lead">${escapeHtml(b.restated_problem)}</p>` : ""}
      ${b.approach ? `<h4>Approach</h4><p>${escapeHtml(b.approach)}</p>` : ""}
      ${b.why ? `<h4>Why this one</h4><p>${escapeHtml(b.why)}</p>` : ""}
      ${(b.alternatives_rejected || []).length ? `<h4>Considered and rejected</h4><ul>${
        list(b.alternatives_rejected, (a) => `<li><b>${escapeHtml(a.option || "")}</b> — ${escapeHtml(a.why_not || "")}</li>`)}</ul>` : ""}
      ${(b.milestones || []).length ? `<h4>Milestones</h4><ol>${
        list(b.milestones, (m) => `<li>${escapeHtml(m)}</li>`)}</ol>` : ""}
      ${(b.risks || []).length ? `<h4>Risks</h4><ul>${
        list(b.risks, (r) => `<li><b>${escapeHtml(r.risk || "")}</b> → ${escapeHtml(r.mitigation || "")}</li>`)}</ul>` : ""}
      ${b.strongest_objection ? `<div class="bp-objection"><b>Strongest surviving objection</b>
        <p>${escapeHtml(b.strongest_objection)}</p></div>` : ""}
      ${(b.open_questions || []).length ? `<h4>For you to decide</h4><ul>${
        list(b.open_questions, (q) => `<li>${escapeHtml(q)}</li>`)}</ul>` : ""}
      ${(b.team || []).length ? `<h4>Proposed team</h4><div class="bp-team">${
        (b.team || []).map((m) => `<span class="bp-role">${escapeHtml(m.role)} ×${m.count || 1}
          <span class="hint">${escapeHtml(m.why || "")}</span></span>`).join("")}</div>` : ""}
      <div class="bp-build">
        <input id="bpName" placeholder="Project name" value="${escapeHtml((t.title || "").slice(0, 40))}">
        <input id="bpRepo" placeholder="owner/repo (optional)">
        <button id="bpBuildBtn" class="primary">🚀 Build this with a team</button>
      </div>
      <p id="bpErr" class="form-error" hidden></p>
    </div>`;
  $("#bpBuildBtn").addEventListener("click", async () => {
    const btn = $("#bpBuildBtn");
    btn.disabled = true; btn.textContent = "hiring…";
    try {
      const r = await api(`/api/tables/${t.id}/build`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: $("#bpName").value, repo: $("#bpRepo").value }),
      });
      $("#plan").hidden = true;
      openProject(r.project_id, "command");
    } catch (e) {
      const er = $("#bpErr"); er.hidden = false; er.textContent = e.message;
      btn.disabled = false; btn.textContent = "🚀 Build this with a team";
    }
  });
}

// ===== MANAGER CHAT ========================================================
// A conversation deserves its own surface. The activity feed auto-scrolls, which
// is right for logs and wrong for reading a reply — so this view only follows the
// bottom when you are already there, and never yanks the page while you read.

let chatSig = "";
let allEvents = [];        // kept so the chat view can filter without refetching
let chatSeen = 0;          // highest event id the user has actually looked at

const CHAT_KINDS = ["directive", "boss_reply", "boss_question", "answered"];
const DECISION_KINDS = {
  task_created: "planned a task", pr_merged: "merged a PR",
  changes_requested: "sent work back", task_accepted: "accepted a task",
  winner_picked: "picked a contest winner", reassigned: "moved a task to another model",
  project_done: "finished the project", needs_attention: "flagged a problem",
};

function chatEvents() {
  return (allEvents || []).filter((e) =>
    CHAT_KINDS.includes(e.kind) || DECISION_KINDS[e.kind]);
}

function renderChat() {
  const el = $("#chatLog");
  if (!el || $("#chat").hidden) return;
  const evs = chatEvents();
  const sig = evs.map((e) => e.id).join(",");
  if (sig === chatSig) return;
  chatSig = sig;

  // Only stay pinned to the bottom if the reader is already there.
  const wasTailing = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  const prevTop = el.scrollTop;

  el.innerHTML = evs.map((e) => {
    let obj = {};
    try { obj = typeof e.payload === "string" ? JSON.parse(e.payload) : (e.payload || {}); }
    catch { obj = {}; }
    const when = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : "";

    if (e.kind === "directive") {
      const text = typeof e.payload === "string" ? e.payload : (obj.text || "");
      return `<div class="cmsg you"><div class="cbubble">${escapeHtml(text)}</div>
        <div class="cmeta">you · ${when}</div></div>`;
    }
    if (e.kind === "boss_reply") {
      const running = (obj.running || []).length
        ? `<div class="crun">Running then: ${escapeHtml(obj.running.join("; "))}</div>` : "";
      return `<div class="cmsg mgr"><div class="cbubble">${escapeHtml(obj.message || "")}${running}</div>
        <div class="cmeta">manager · ${when}</div></div>`;
    }
    if (e.kind === "boss_question") {
      return `<div class="cmsg mgr"><div class="cbubble cask">
        <b>Needs your decision</b><br>${escapeHtml(obj.question || obj.text || "")}
        ${(obj.options || []).length ? `<div class="copts">${
          obj.options.map((o) => escapeHtml(o)).join(" · ")}</div>` : ""}
        <div class="chint">Answer it on the Command tab or in the bell.</div></div>
        <div class="cmeta">manager · ${when}</div></div>`;
    }
    if (e.kind === "answered") {
      return `<div class="cmsg you"><div class="cbubble">${escapeHtml(obj.answer || "")}</div>
        <div class="cmeta">you answered · ${when}</div></div>`;
    }
    // a compact status line so the conversation has context without the flood
    const what = DECISION_KINDS[e.kind];
    const detail = obj.reason || obj.verdict || obj.title || obj.role || "";
    return `<div class="cnote">· ${escapeHtml(what)}${detail ? ": " + escapeHtml(trim(String(detail), 90)) : ""}
      <span class="chint">${when}</span></div>`;
  }).join("") || `<p class="empty">No conversation yet. Ask your manager something below —
    it answers with what is actually running.</p>`;

  el.scrollTop = wasTailing ? el.scrollHeight : prevTop;
}

function markChatRead() {
  const evs = chatEvents();
  chatSeen = evs.length ? Math.max(...evs.map((e) => e.id || 0)) : chatSeen;
  const b = $("#chatUnread"); if (b) b.hidden = true;
}

function updateChatUnread() {
  const evs = chatEvents().filter((e) => e.kind === "boss_reply" || e.kind === "boss_question");
  const unread = evs.filter((e) => (e.id || 0) > chatSeen).length;
  const b = $("#chatUnread");
  if (!b) return;
  b.hidden = unread === 0 || !$("#chat").hidden;
  b.textContent = unread;
}

function ago(ts) {
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 24 ? `${h}h ago` : `${Math.round(h / 24)}d ago`;
}

// --- Blockers: every current obstacle on this project, with the fix for each. ---
let blockersSig = "";

async function renderBlockers() {
  const el = $("#blockers");
  if (!el || !currentProject) return;
  let data;
  try { data = await api(`/api/projects/${currentProject}/blockers`); } catch { return; }
  const items = data.blockers || [];

  // Badge on the tab is always updated, even when the tab isn't open.
  const badge = $("#blockerCount");
  if (badge) {
    badge.hidden = items.length === 0;
    badge.textContent = data.critical || items.length;
    badge.className = data.critical ? "crit" : "warn";
  }
  if (el.hidden) return;

  const sig = JSON.stringify(items.map((b) => [b.kind, b.task_id, b.title]));
  if (sig === blockersSig) return;   // don't repaint an unchanged list
  blockersSig = sig;

  if (!items.length) {
    el.innerHTML = `<div class="bl-clear"><div class="bl-clear-i">✓</div>
      <h3>Nothing is blocking this project</h3>
      <p>No failed work, no unanswered questions, no capacity or credential problems.</p></div>`;
    return;
  }
  const label = { critical: "Stopping the project", warning: "Slowing it down", info: "Worth knowing" };
  el.innerHTML = `
    <div class="bl-head">
      <h2>🚧 Blockers</h2>
      <div class="bl-tally">${data.critical
        ? `<span class="pill crit">${data.critical} stopping work</span>` : ""}
        ${data.warning ? `<span class="pill warn">${data.warning} slowing it down</span>` : ""}</div>
    </div>
    ${items.map((b) => `
      <div class="bl-card ${b.severity}">
        <div class="bl-top">
          <span class="pill ${b.severity === "critical" ? "crit" : "warn"}">${label[b.severity]}</span>
          ${b.since ? `<span class="bl-since">${ago(b.since)}</span>` : ""}
        </div>
        <h3>${escapeHtml(b.title)}</h3>
        <p class="bl-detail">${escapeHtml(b.detail)}</p>
        ${b.fix ? `<p class="bl-fix"><b>What clears it:</b> ${escapeHtml(b.fix)}</p>` : ""}
        <div class="bl-acts">
          ${b.task_id ? `<button data-blview="${b.task_id}">Open task #${b.task_seq}</button>` : ""}
          ${b.action === "retry" ? `<button data-blretry="${b.task_id}" class="primary">↻ Re-run it</button>` : ""}
          ${b.action === "settings" ? `<button data-blset="1" class="primary">Open settings</button>` : ""}
          ${b.action === "answer" ? `<button data-blask="1" class="primary">Answer the manager</button>` : ""}
        </div>
      </div>`).join("")}`;

  el.querySelectorAll("[data-blview]").forEach((btn) =>
    btn.addEventListener("click", () => showTask(Number(btn.dataset.blview))));
  el.querySelectorAll("[data-blretry]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      btn.disabled = true; btn.textContent = "re-running…";
      await api(`/api/tasks/${btn.dataset.blretry}/retry`, { method: "POST" });
      blockersSig = ""; renderBlockers();
    }));
  el.querySelectorAll("[data-blset]").forEach((btn) =>
    btn.addEventListener("click", () => $("#settingsBtn").click()));
  el.querySelectorAll("[data-blask]").forEach((btn) =>
    btn.addEventListener("click", () => switchView("command")));
}

let lastTasks = [];
let lastProject = null;

// Events can arrive many times a second; coalesce refreshes so we don't hammer
// the API (and re-run question checks) on every single one.
let refreshTimer = null;
function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; refreshBoard(); }, 600);
}

async function refreshBoard() {
  if (!currentProject) return;
  let p;
  try { p = await api(`/api/projects/${currentProject}`); } catch { return; }
  lastTasks = p.tasks;
  const st = $("#selfTab");
  if (st) st.hidden = !p.is_self;
  renderBlockers();          // keeps the 🚧 badge honest even when the tab is closed
  currentRepo = p.repo || "";
  lastProject = p;
  if (!$("#dag").hidden) renderDag(p.tasks);
  renderCommand(p);
  const runs = p.runs_used ?? 0, maxRuns = p.max_runs ?? 40;
  $("#costBadge").hidden = false;
  if (authMode === "subscription") {
    // No per-token billing — show the meaningful metric (agent runs), not dollars.
    $("#costBadge").textContent = `${runs}/${maxRuns} agent runs used`;
    $("#costBadge").title =
      `This project has dispatched ${runs} agent runs out of a ${maxRuns} cap.\n` +
      "One run = one teammate working one task once (a retry counts again).\n" +
      "It is a runaway-loop guard, NOT a limit on how many agents work at the same time " +
      "— that's the max-parallel setting. Nothing is billed on a subscription.";
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
    if (!$("#artifacts").hidden) renderArtifacts();
  if (!$("#agents").hidden) renderAgents();
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
      if (deps.length) links.push(`after ${depLabels(deps).join(",")}`);
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
  if (s === null || s === undefined) return "";
  if (typeof s !== "string") s = String(s);
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
  if (e.kind === "boss_reply") cls += " bossreply";
  if (e.kind === "rate_limited" || e.kind === "reassigned") cls += " ratelimit";
  // Scaling / routing machinery — surfaced on its own tab so the boss can audit
  // exactly when a model was upscaled, swapped, or a contest was run.
  if (["dispatched", "rate_limited", "reassigned", "contest_started", "contest_ready",
       "winner_picked", "rival_finished", "worker_stalled", "worker_died",
       "dag_blocked", "reopened", "needs_attention", "consult"].includes(e.kind))
    cls += " decision";
  // Simple mode hides mechanical chatter; these are the "noise" kinds.
  if (["result", "agent_status", "dispatched", "repo_ready", "resumed_after_restart",
       "task_edited", "push_retry"].includes(e.kind)) cls += " sys-noise";
  // Simple should read like a status board, not a transcript. A worker narrating
  // its own steps is the bulk of the volume and almost never what the boss needs.
  if (e.source.startsWith("worker") && ["message", "consult", "consult_reply"].includes(e.kind))
    cls += " chatter";
  if (e.kind === "error" || e.kind === "worker_died" || e.kind === "dag_blocked") cls += " error";
  div.className = `ev ${cls}`;
  let text = e.payload;
  try {
    const obj = JSON.parse(e.payload);
    if (typeof obj === "object" && obj !== null) {
      if (e.kind === "tool_use") text = `→ ${obj.tool || ""} ${JSON.stringify(obj.input || obj).slice(0, 300)}`;
      else if (e.kind === "report") {
        // On subscription there's no per-token charge — the $ figure is meaningless, so omit it.
        const cost = authMode === "subscription" ? "" : ` · est. $${(obj.cost_usd || 0).toFixed(2)}`;
        text = `[${obj.status}]${cost}\n${obj.summary || ""}`;
      } else if (e.kind === "result" && authMode === "subscription") text = "(turn complete)";
      else if (e.kind === "boss_question") text = "❓ " + (obj.question || "");
      else if (e.kind === "task_created") text = `📋 New task for ${obj.role}: ${obj.title}`;
      else if (e.kind === "pr_merged") text = `✅ Merged PR #${obj.pr}`;
      else if (e.kind === "pr_opened") text = `🔀 Opened PR #${obj.pr} for review`;
      else if (e.kind === "task_accepted") text = `✅ Accepted: ${obj.verdict || ""}`;
      else if (e.kind === "changes_requested") text = `↩ Sent back for changes: ${obj.feedback || ""}`;
      else if (e.kind === "project_finished") text = `🏁 Project ${obj.status}`;
      else if (e.kind === "rate_limited") text = `⏳ ${obj.model || "a model"} hit a rate limit — the manager will move this work`;
      else if (e.kind === "boss_reply") {
        text = `💬 Answering you: ${obj.message}`
          + (obj.running && obj.running.length ? `\n\nRunning right now: ${obj.running.join("; ")}` : "");
      }
      else if (e.kind === "reassigned") text = `🔄 Moved to ${obj.model}: ${obj.reason || ""}`;
      else if (e.kind === "dispatched") text = `🚀 ${obj.role} started on ${obj.model}`
        + (obj.attempt > 1 ? ` — attempt ${obj.attempt}${obj.attempt >= 3 ? " (upscaled model)" : ""}` : "");
      else if (e.kind === "contest_started") text = `🥊 Contest: ${obj.rivals} rivals on the same task — ${(obj.detail || []).join(", ")}`;
      else if (e.kind === "contest_ready") text = `🥊 All ${obj.rivals} rivals finished (${obj.finished_ok} usable) — manager judging`;
      else if (e.kind === "winner_picked") text = `🏆 Rival #${obj.rival} (${obj.model}) won: ${obj.reason || ""}`;
      else if (e.kind === "worker_stalled") text = `⚠️ Agent stalled after ${obj.idle_seconds}s — restarting it`;
      else if (e.kind === "worker_died") text = `⚠️ Agent exited without reporting (code ${obj.exit_code})`;
      else if (e.kind === "dag_blocked") text = `🚧 Blocked: tasks ${(obj.blocked_tasks||[]).join(", ")} waiting on failed ${(obj.failed_deps||[]).join(", ")}`;
      else if (e.kind === "reopened") text = `↩ Reopened: ${obj.reason || ""}`;
      else if (e.kind === "needs_attention") text = `❗ ${obj.reason} — ${obj.tasks || ""}`;
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
    if (currentProject) {
      if (e.project_id === currentProject) {
        allEvents.push(e);
        if (allEvents.length > 800) allEvents = allEvents.slice(-600);
        renderChat(); updateChatUnread();
      }
      noteActivity(e); renderEvent(e); scheduleRefresh();
    }
    else loadProjects();  // on the home page, keep the table live
    if (e.kind === "boss_question" || e.kind === "answered") { refreshQuestion(); refreshBell(); }
    if (e.kind === "boss_question") notifyBoss(e);
    if (e.kind === "project_finished") notify("Project finished", `Project #${e.project_id} is done.`);
    if (["project_created", "project_finished", "project_cancelled", "boss_question"].includes(e.kind)) loadProjects();
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
}

// --- push notifications -----------------------------------------------------
function notify(title, body) {
  try {
    if (window.Notification && Notification.permission === "granted")
      new Notification(title, { body, icon: "" });
  } catch { /* not supported */ }
}
function notifyBoss(e) {
  let q = "";
  try { q = JSON.parse(e.payload).question || ""; } catch { /* */ }
  notify("👔 Manager needs your decision", q.slice(0, 140));
  // also flash the tab title until focused
  const orig = document.title.replace(/^🔔 Needs you — /, "");
  document.title = "🔔 Needs you — " + orig;
  window.addEventListener("focus", function once() {
    document.title = orig; window.removeEventListener("focus", once);
  });
}

// --- boss controls ----------------------------------------------------------
let currentQuestionId = null;

async function refreshQuestion() {
  if (!currentProject) return;
  let q;
  try { q = await api(`/api/projects/${currentProject}/question`); } catch { return; }
  if (!q.question) {
    currentQuestionId = null;
    pendingQ = null;
  } else {
    currentQuestionId = q.id;
    pendingQ = { id: q.id, text: q.question, options: q.options || [] };
  }
  // Questions are shown INLINE in the Command view (the amber ask-card).
  // No popup: modals covered the org chart and felt intrusive.
  if (lastProject) renderCommand(lastProject);
}


async function answerQuestion(qid, answer) {
  if (!answer || !answer.trim()) return;
  await api(`/api/questions/${qid}/answer`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answer }),
  });
  currentQuestionId = null;
  pendingQ = null;
  if (lastProject) renderCommand(lastProject);
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
    <h2>#${t.seq || t.id} ${escapeHtml(t.title)}</h2>
    <div class="meta">${t.status} · attempt ${t.attempts}${t.attempts >= 2 ? " (escalated to Sonnet)" : ""}${taskCost(t)}
      ${t.origin === "runtime" ? "· added at runtime" : ""}
      ${deps.length ? "· depends on " + depLabels(deps).join(", ") : ""} ${links.join(" · ")}</div>
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

// --- COMMAND VIEW: the org chart (BOSS → MANAGER → agents) -------------------
let managerThought = "";      // latest manager message
let managerThinking = "";     // latest manager internal thought
let agentActivity = {};       // task_id -> last human-readable activity line
let pendingQ = null;

const STATUS_WORD = {
  planned: "waiting", queued: "starting", running: "working",
  pushed: "submitted", review: "in review", done: "done", failed: "needs attention",
};

function trim(s, n) { s = (s || "").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

function renderCommand(p) {
  const el = $("#command");
  if (!el || el.hidden) return;
  const tasks = p.tasks || [];
  let roster = []; try { roster = JSON.parse(p.team || "[]"); } catch { /* */ }
  const busy = tasks.some((t) => ["running", "queued"].includes(t.status));
  const mgrModel = p.manager_model || "Sonnet 5";
  const mode = p.autonomy === "autonomous" ? "full autonomy" : "checks with you";

  // "Needs attention" must state the actual story, not just colour a badge.
  const attnHtml = (["review", "failed"].includes(p.status) && p.summary) ? `
    <div class="attn-card">
      <div class="bl">❗ Needs your attention</div>
      <div class="qtext">${escapeHtml(p.summary)}</div>
      <div class="qbtns"><button data-attn="fix">Tell the manager to fix it</button></div>
    </div>` : "";

  const askHtml = pendingQ ? `
    <div class="ask-card">
      <div class="bl">👔 Your manager needs a decision</div>
      <div class="qtext">${escapeHtml(pendingQ.text)}</div>
      <div class="qbtns">
        ${(pendingQ.options || []).map((o, i) =>
          `<button data-qopt="${i}">${escapeHtml(o)}</button>`).join("")}
      </div>
      <div class="qreply">
        <input id="askReply" placeholder="…or tell the manager what to do in your own words">
        <button class="primary" data-qopt="send">Send</button>
      </div>
    </div>` : "";

  const bubbleHtml = (label, text, cls) => text
    ? `<div class="bubble ${cls || ""}"><div class="bl">${label}</div>${escapeHtml(trim(text, 420))}</div>` : "";

  // Group the team the way a person reads it: who's working now, who is blocked and
  // on what, who's finished. A lone "waiting" card next to a busy one is confusing.
  const doneIds = new Set(tasks.filter((t) => t.status === "done").map((t) => t.id));
  const groupOf = (t) => {
    if (["queued", "running"].includes(t.status)) return "working";
    if (["pushed", "review"].includes(t.status)) return "review";
    if (t.status === "done") return "done";
    if (t.status === "failed") return "review";
    let d = []; try { d = JSON.parse(t.deps || "[]"); } catch { /* */ }
    return d.some((x) => !doneIds.has(x)) ? "blocked" : "ready";
  };
  const card = (t) => {
    let deps = []; try { deps = JSON.parse(t.deps || "[]"); } catch { /* */ }
    const doing = agentActivity[t.id] || (t.status === "planned"
      ? "Waiting for their turn." : t.status === "done" ? "Finished and handed in."
      : t.status === "failed" ? "Hit a problem — manager is on it." : "Getting started…");
    // Show the model this agent ran on. For tasks that haven't run yet (or predate
    // model tracking), show the model they're slated to use, from the recruited roster.
    const pretty = (m) => m.replace("claude-", "").replace("-4-5", " 4.5")
      .replace("-4-8", " 4.8").replace(/-5$/, " 5");
    let modelLabel;
    if (t.model) modelLabel = pretty(t.model);
    else {
      const hired = (roster || []).find((r) => r.role === t.role);
      modelLabel = hired
        ? pretty(hired.model === "lead" ? "claude-sonnet-5" : "claude-haiku-4-5") + " (planned)"
        : "—";
    }
    return `<div class="agent ${t.status}" data-task="${t.id}">
      <div class="top">
        <span class="role">${escapeHtml(t.role)}</span>
        <span class="st">${STATUS_WORD[t.status] || t.status}</span>
      </div>
      <div class="title">${escapeHtml(t.title)}</div>
      <div class="doing">${escapeHtml(trim(doing, 150))}</div>
      ${(t.rivals || []).length ? `<div class="rivals">🥊 contest: ${
        t.rivals.map((r) => `<span class="rival ${r.status}">#${r.idx} ${
          escapeHtml((r.model || "").replace("claude-", ""))} · ${r.status}</span>`).join("")
      }</div>` : ""}
      <div class="deps">🧠 ${escapeHtml(modelLabel)}${t.attempts > 1 ? ` · attempt ${t.attempts}` : ""}
        ${deps.length ? ` · after ${depLabels(deps).join(", ")}` : ""}</div>
    </div>`;
  };

  const GROUPS = [
    ["working", "⚡ Working right now", "these are running in parallel"],
    ["review", "🔍 Needs attention", "submitted for review, or failed and not yet redone"],
    ["ready", "▶ Ready to start", "unblocked, waiting for a free slot"],
    ["blocked", "⏳ Blocked on teammates", "waiting for the work they build on"],
    ["done", "✅ Finished", ""],
  ];
  const agents = GROUPS.map(([key, label, note]) => {
    const inGroup = tasks.filter((t) => groupOf(t) === key);
    if (!inGroup.length) return "";
    const blockedNote = (t) => {
      let d = []; try { d = JSON.parse(t.deps || "[]"); } catch { /* */ }
      const waiting = d.filter((x) => !doneIds.has(x));
      const names = waiting.map((id) => {
        const dep = tasks.find((x) => x.id === id);
        return dep ? dep.role : "#" + id;
      });
      return names.length ? `waiting for ${names.join(" & ")}` : "";
    };
    return `<div class="group">
      <div class="group-head"><span class="glabel">${label}</span>
        <span class="gcount">${inGroup.length}</span>
        ${note ? `<span class="gnote">${note}</span>` : ""}</div>
      <div class="agents">${inGroup.map((t) => {
        const bn = key === "blocked" ? blockedNote(t) : "";
        return card(t).replace('<div class="deps">',
          bn ? `<div class="blocked-note">${escapeHtml(bn)}</div><div class="deps">` : '<div class="deps">');
      }).join("")}</div>
    </div>`;
  }).join("");

  el.innerHTML = `
    <div class="chain">
      ${attnHtml}
      ${askHtml}
      <div class="node boss" id="bossNode" title="Click to read your full request">
        <div class="who">👑 Boss</div>
        <div class="name">${escapeHtml(me.username || "You")}</div>
        <div class="sub">${escapeHtml(trim(p.brief, 90))}</div>
        <div class="more">click to read the full request →</div>
      </div>
      <div class="connector"></div>
      <div class="node-row">
        <div class="node manager ${busy ? "busy" : ""}">
          <div class="who">👔 Manager</div>
          <div class="name">${escapeHtml(mgrModel)}</div>
          <div class="sub">${mode} · ${tasks.length} on the team</div>
        </div>
        ${bubbleHtml("Manager says", managerThought)}
        ${bubbleHtml("thinking", managerThinking, "thinking")}
      </div>
      <div class="fan"></div>
      <div class="section-label">The team</div>
      ${agents || '<div class="empty">Assembling the team…</div>'}
    </div>`;

  const attnBtn = el.querySelector("[data-attn]");
  if (attnBtn) attnBtn.addEventListener("click", () => {
    const inp = $("#directiveInput");
    const msg = `This is not done: ${p.summary} Please redo the failed work and `
      + `only finish when it actually works.`;
    inp.value = msg;
    inp.focus();
    api(`/api/projects/${currentProject}/directive`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: msg }),
    }).then(() => { inp.value = ""; toast("Sent to your manager."); })
      .catch((e) => alert(e.message));
  });
  el.querySelectorAll(".agent").forEach((a) =>
    a.addEventListener("click", () => showTask(Number(a.dataset.task))));
  const bossNode = $("#bossNode");
  if (bossNode) bossNode.addEventListener("click", () => {
    $("#taskDetail").innerHTML = `
      <div class="who" style="color:var(--boss)">👑 YOUR REQUEST</div>
      <h2>${escapeHtml(p.name)}</h2>
      <div class="meta">${escapeHtml(p.repo || "no repo")} · manager: ${
        escapeHtml(p.manager_model || "Sonnet 5")} · ${
        p.autonomy === "autonomous" ? "full autonomy" : "checks with you"}</div>
      <h3>What you asked for</h3><pre>${escapeHtml(p.brief)}</pre>
      ${p.summary ? `<h3>Where it stands</h3><pre>${escapeHtml(p.summary)}</pre>` : ""}`;
    $("#editTaskBtn").hidden = true;
    $("#taskDialog").showModal();
  });
  el.querySelectorAll("[data-qopt]").forEach((b) =>
    b.addEventListener("click", () => {
      if (b.dataset.qopt === "send") {
        answerQuestion(pendingQ.id, $("#askReply").value);
        return;
      }
      answerQuestion(pendingQ.id, pendingQ.options[Number(b.dataset.qopt)]);
    }));
  const reply = el.querySelector("#askReply");
  if (reply) reply.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") answerQuestion(pendingQ.id, ev.target.value);
  });
}

// Track what each agent is doing, in plain language, from the event stream.
function noteActivity(e) {
  if (e.source === "manager" && e.kind === "message") managerThought = e.payload;
  if (e.source === "manager" && e.kind === "thinking") managerThinking = e.payload;
  if (!e.task_id) return;
  if (e.kind === "message") agentActivity[e.task_id] = e.payload;
  else if (e.kind === "tool_use") {
    let s = e.payload;
    try { const o = JSON.parse(e.payload); s = `${o.tool || ""} ${JSON.stringify(o.input || {})}`; } catch { /* */ }
    agentActivity[e.task_id] = "Working: " + trim(s.replace(/[{}"]/g, " "), 120);
  } else if (e.kind === "report") {
    try { const o = JSON.parse(e.payload); agentActivity[e.task_id] = trim(o.summary || "", 150); } catch { /* */ }
  }
}

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
        fill="${c}" style="text-transform:uppercase">#${t.seq || t.id} ${t.role.toUpperCase()}</text>
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

// --- ARTIFACTS TAB: the deliverable, documented ------------------------------
let artifactsSig = "";      // only repaint when the content actually changed
async function renderArtifacts(force) {
  const el = $("#artifacts");
  if (!currentProject || el.hidden) return;
  if (force || !el.innerHTML) el.innerHTML = `<div class="pane"><p class="dim">Loading…</p></div>`;
  let a;
  try { a = await api(`/api/projects/${currentProject}/artifacts`); }
  catch (e) { el.innerHTML = `<div class="pane"><p class="dim">${escapeHtml(e.message)}</p></div>`; return; }
  // Repainting identical HTML on every event is what made this flicker.
  const sig = JSON.stringify(a);
  if (!force && sig === artifactsSig) return;
  artifactsSig = sig;

  const demo = a.preview_url
    ? `<a class="demo-btn" href="${a.preview_url}?t=${Date.now()}" target="_blank">▶ Open the demo app</a>
       <button id="buildPreviewBtn">↻ Rebuild from latest main</button>
       <span class="hint">${a.preview_synced || "built earlier"} · served here, sandboxed</span>`
    : `<button id="buildPreviewBtn" class="primary">▶ Build the demo app</button>
       <span class="hint">runs the built site here (static apps only)</span>`;

  const prs = (a.prs || []).map((p) =>
    `<li><a href="${p.url}" target="_blank">PR #${p.number}</a>
      <span class="pill ${p.merged ? "ok" : ""}">${p.merged ? "merged" : p.state}</span>
      ${escapeHtml(p.title)}</li>`).join("");

  const work = (a.work || []).map((w) => `
    <div class="work-item">
      <div class="work-head">
        <span class="role">${escapeHtml(w.role)}</span>
        <span class="pill ${w.status === "done" ? "ok" : w.status === "failed" ? "bad" : "warn"}">${w.status}</span>
        ${w.pr ? `<a href="https://github.com/${a.repo}/pull/${w.pr}" target="_blank">PR #${w.pr}</a>` : ""}
        <span class="hint">${escapeHtml(w.model || "")}${w.attempts > 1 ? ` · ${w.attempts} attempts` : ""}</span>
      </div>
      <div class="work-title">${escapeHtml(w.title)}</div>
      ${w.outcome ? `<details><summary>What they delivered</summary><pre>${escapeHtml(w.outcome)}</pre></details>` : ""}
    </div>`).join("");

  el.innerHTML = `
    <div class="pane">
      <h2>${escapeHtml(a.project)}</h2>
      <p class="brief">${escapeHtml(a.brief)}</p>
      <div class="run-cards">
        <div class="run-card">
          <h4>📄 Static preview</h4>
          <p class="run-why">Serves the built files only. Fast, sandboxed — but any
             call the app makes to its own backend will 404.</p>
          <div class="demo-row">${demo}</div>
        </div>
        <div class="run-card primary-card" id="fullDeployCard">
          <h4>🚀 Full deployment</h4>
          <p class="run-why">Builds the latest main and <b>runs the real app</b> —
             backend, API routes and all.</p>
          <div id="deployBody"><span class="hint">checking…</span></div>
        </div>
      </div>
      ${a.conclusion ? `<h3>Conclusion</h3><div class="conclusion">${escapeHtml(a.conclusion)}</div>` : ""}
      <h3>What the team did</h3>
      <div class="work-list">${work || '<p class="dim">No work yet.</p>'}</div>
      <h3>Pull requests</h3>
      <ul class="art-list">${prs || '<li class="dim">none yet</li>'}</ul>
      <h3>Source</h3>
      <p>📁 <a href="${a.repo_url}" target="_blank">${escapeHtml(a.repo || "no repo")}</a>
         &nbsp; <span class="hint">branches: ${(a.branches || []).length}</span></p>
    </div>`;

  renderDeploy();

  const bp = $("#buildPreviewBtn");
  if (bp) bp.addEventListener("click", async () => {
    const label = bp.textContent;
    bp.disabled = true; bp.textContent = "Pulling latest…";
    try { await api(`/api/projects/${currentProject}/preview`, { method: "POST" }); renderArtifacts(true); }
    catch (e) { alert(e.message); bp.disabled = false; bp.textContent = label; }
  });
}

// --- Full deployment: build and run the real app, backend included ----------
async function renderDeploy() {
  const box = $("#deployBody");
  if (!box || !currentProject) return;
  let d;
  try { d = await api(`/api/projects/${currentProject}/deploy`); }
  catch (e) { box.innerHTML = `<span class="hint">${escapeHtml(e.message)}</span>`; return; }

  const spec = d.spec || {};
  const modeNote = d.default_mode === "k8s"
    ? "deploys as a pod on the cluster"
    : "runs locally on its own port";

  if (d.live) {
    box.innerHTML = `
      <div class="live-row">
        <span class="pill ok">live</span>
        <a class="demo-btn" href="${d.live.url}" target="_blank">▶ Open the running app</a>
        <button id="redeployBtn">↻ Rebuild &amp; restart</button>
        <button id="stopDeployBtn" class="danger">■ Stop</button>
      </div>
      <p class="hint">${escapeHtml(d.live.url)} · ${escapeHtml(d.live.kind)} ·
         up ${Math.floor(d.live.uptime / 60)}m${d.live.uptime % 60}s</p>
      ${d.log ? `<details><summary>Build &amp; runtime log</summary><pre class="deploy-log">${
        escapeHtml(d.log)}</pre></details>` : ""}`;
  } else {
    const runnable = spec.kind && !["static", "unknown", "node-static"].includes(spec.kind);
    box.innerHTML = `
      ${spec.kind ? `<p class="detected"><b>Detected:</b> ${escapeHtml(spec.kind)} —
         ${escapeHtml(spec.why || "")}</p>` : ""}
      <button id="deployAppBtn" class="primary" ${spec.kind && !runnable ? "disabled" : ""}>
        🚀 Build &amp; deploy${d.default_mode === "k8s" ? " to the cluster" : ""}</button>
      <span class="hint">${modeNote}</span>
      ${spec.kind && !runnable
        ? `<p class="hint warn-t">Nothing to run — this project is ${escapeHtml(spec.kind)}.
           The static preview is the right tool for it.</p>` : ""}
      ${d.log ? `<details><summary>Last build log</summary><pre class="deploy-log">${
        escapeHtml(d.log)}</pre></details>` : ""}`;
  }

  const go = $("#deployAppBtn") || $("#redeployBtn");
  if (go) go.addEventListener("click", async () => {
    const label = go.textContent;
    go.disabled = true;
    go.textContent = "building… (first build can take a minute)";
    try {
      const r = await api(`/api/projects/${currentProject}/deploy`, { method: "POST" });
      if (!r.ok) {
        box.innerHTML = `<p class="deploy-err"><b>Deploy failed:</b> ${escapeHtml(r.error || "unknown")}</p>
          ${r.log ? `<pre class="deploy-log">${escapeHtml(r.log)}</pre>` : ""}
          <button id="deployAppBtn" class="primary">↻ Try again</button>`;
        const again = $("#deployAppBtn");
        if (again) again.addEventListener("click", () => renderDeploy());
        return;
      }
      artifactsSig = ""; renderDeploy();
    } catch (e) {
      alert(e.message); go.disabled = false; go.textContent = label;
    }
  });
  const stop = $("#stopDeployBtn");
  if (stop) stop.addEventListener("click", async () => {
    stop.disabled = true;
    await api(`/api/projects/${currentProject}/deploy`, { method: "DELETE" });
    renderDeploy();
  });
}

// --- AGENTS TAB: the actual machines ----------------------------------------
// The pane re-renders on every event; remember which log is open (and its text)
// so it survives the re-render instead of vanishing.
let openLogTask = null;
let openLogText = "";

async function loadMachineLogs(taskId) {
  openLogTask = Number(taskId);
  const box = $("#machineLogs");
  if (box && !box.textContent) box.textContent = openLogText || "loading…";
  let next;
  try {
    const r = await api(`/api/tasks/${taskId}/machine-logs`);
    next = `— ${r.source} · task #${taskId} —\n\n${r.logs}`;
  } catch (e) { next = String(e.message); }
  const b = $("#machineLogs");
  if (!b || openLogTask !== Number(taskId)) return;
  // Writing textContent resets scrollTop to 0, which is what kept yanking the view
  // back to the top. Only touch the DOM when the text actually changed, and put the
  // reader's scroll position back exactly where it was (unless they were tailing).
  if (next === openLogText) return;
  const prevTop = b.scrollTop;
  const wasTailing = b.scrollHeight - b.scrollTop - b.clientHeight < 40;
  openLogText = next;
  b.textContent = next;
  b.scrollTop = wasTailing ? b.scrollHeight : prevTop;
}

let agentsSig = "";
async function renderAgents() {
  const el = $("#agents");
  if (el.hidden) return;
  let a;
  // Scope to the project you're looking at — otherwise other projects' agents show up here.
  const scope = currentProject ? `?project_id=${currentProject}` : "";
  try { a = await api("/api/agents" + scope); }
  catch (e) { el.innerHTML = `<div class="pane"><p class="dim">${escapeHtml(e.message)}</p></div>`; return; }

  const row = (g, live) => `
    <tr class="${live ? "live" : "past"}">
      <td><span class="role">${escapeHtml(g.role)}</span></td>
      <td>${escapeHtml(trim(g.title, 46))}<div class="hint">${escapeHtml(g.project || "")}</div></td>
      <td>${live ? `<code>${escapeHtml(g.ref)}</code>` : `<span class="pill">${escapeHtml(g.status)}</span>`}</td>
      <td>${escapeHtml(g.model)}</td>
      <td>${live ? `${Math.floor(g.uptime_s / 60)}m ${g.uptime_s % 60}s` : "\u2014"}</td>
      <td><button data-logs="${g.task_id}">logs</button>${live
        ? ` <button class="danger" data-kill="${g.task_id}" title="Stop this agent now">\u25a0 stop</button>` : ""}</td>
    </tr>`;
  const rows = (a.agents || []).map((g) => row(g, true)).join("")
    + ((a.finished || []).length
      ? `<tr class="sep"><td colspan="6">Recently finished \u2014 logs still available</td></tr>`
        + a.finished.map((g) => row(g, false)).join("")
      : "");

  // Build the shell ONCE. Rebuilding it on every refresh recreated the <pre> and
  // threw away the reader's scroll position.
  if (!$("#agentsShell")) {
    el.innerHTML = `
      <div class="pane" id="agentsShell">
        <h2>\ud83d\udda5 Machines</h2>
        <p class="hint" id="agentsMode"></p>
        <div id="modelHealth"></div>
        <table class="agents-table">
          <thead><tr><th>Role</th><th>Task</th><th id="refCol">Process</th><th>Model</th><th>Uptime</th><th></th></tr></thead>
          <tbody id="agentsBody"></tbody>
        </table>
        <div class="logs-head" id="logsHead" hidden>
          <span id="logsTitle"></span><button id="closeLogsBtn">close</button>
        </div>
        <pre id="machineLogs" hidden></pre>
      </div>`;
    $("#closeLogsBtn").addEventListener("click", () => {
      openLogTask = null; openLogText = "";
      $("#machineLogs").hidden = true; $("#machineLogs").textContent = "";
      $("#logsHead").hidden = true;
    });
  }

  const sig = JSON.stringify({ rows, mode: a.mode, running: a.running });
  if (sig !== agentsSig) {                 // only repaint the table when it changed
    agentsSig = sig;
    $("#agentsMode").innerHTML = `Execution mode: <b>${a.mode === "k8s"
      ? `Kubernetes (namespace ${escapeHtml(a.namespace)})` : "local processes"}</b>
      \u00b7 running now: <b>${a.running}</b> \u00b7 max at once: ${a.max_parallel}`;
    $("#refCol").textContent = a.mode === "k8s" ? "Pod / Job" : "Process";
    $("#agentsBody").innerHTML = rows ||
      '<tr><td colspan="6" class="dim">No agents running right now.</td></tr>';
    $("#agentsBody").querySelectorAll("[data-kill]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm("Stop this agent now?\n\nIts task is marked failed and any "
          + "unpushed work is lost. You can re-run the task afterwards.")) return;
        b.disabled = true; b.textContent = "stopping\u2026";
        const r = await api(`/api/tasks/${b.dataset.kill}/kill`, { method: "POST" });
        toast(r.stopped ? `Stopped ${r.stopped} agent process(es).`
                        : "That agent had already exited.");
        agentsSig = ""; renderAgents(); refreshBoard();
      }));
    $("#agentsBody").querySelectorAll("[data-logs]").forEach((b) =>
      b.addEventListener("click", () => {
        openLogText = ""; openLogTask = Number(b.dataset.logs);
        $("#machineLogs").textContent = "loading\u2026";
        $("#machineLogs").hidden = false;
        $("#logsHead").hidden = false;
        $("#logsTitle").textContent = `Machine logs \u2014 task #${openLogTask}`;
        loadMachineLogs(openLogTask);
      }));
  }
  if (openLogTask) loadMachineLogs(openLogTask);   // keep tailing, scroll preserved
  renderModelHealth();
}

// Honest capacity view: Anthropic exposes no remaining-quota API, so this shows how
// this app's own recent runs on each model fared (throttled vs clean).
async function renderModelHealth() {
  const box = $("#modelHealth");
  if (!box) return;
  let h;
  try { h = await api("/api/model-health"); } catch { return; }
  if (!h.models.length) { box.innerHTML = ""; return; }
  const mmss = (s) => s >= 3600 ? `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`
    : s >= 60 ? `${Math.floor(s/60)}m ${s%60}s` : `${s}s`;
  // Exact numbers when Anthropic gives them to us (API keys), observed health otherwise.
  const quota = (h.quota || []).map((q) => `
    <div class="quota-box">
      ${q.requests_limit ? `<div>Requests left: <b>${q.requests_remaining}</b> / ${q.requests_limit}
        ${q.requests_reset ? `· resets ${escapeHtml(String(q.requests_reset))}` : ""}</div>` : ""}
      ${q.tokens_limit ? `<div>Tokens left: <b>${q.tokens_remaining}</b> / ${q.tokens_limit}
        ${q.tokens_reset ? `· resets ${escapeHtml(String(q.tokens_reset))}` : ""}</div>` : ""}
    </div>`).join("");
  box.innerHTML = `<div class="health-title">Model capacity — last ${h.window_hours}h</div>` + quota +
    h.models.map((m) => `
      <div class="health-row">
        <span class="hm">${escapeHtml(m.model.replace("claude-", ""))}</span>
        <span class="bar"><i class="${m.state}" style="width:${m.cooldown_s ? 8 : m.health}%"></i></span>
        <span class="hs ${m.state}">${m.cooldown_s ? "cooling" : m.state}</span>
        <span class="hint">${m.cooldown_s
          ? `usable again in ${mmss(m.cooldown_s)}`
          : `${m.ok}/${m.runs} runs clean${m.throttled ? ` · ${m.throttled} throttled` : ""}`}</span>
      </div>`).join("") +
    `<div class="hint health-note">${escapeHtml(h.note)}</div>`;
}

// --- artifacts / public link ------------------------------------------------

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
  $("#addTaskTitle").textContent = prefill ? `Edit task #${prefill.seq || prefill.id}` : "Add a task to the DAG";
  $("#addTaskSubmit").textContent = prefill ? "Save changes" : "Add to DAG";
  if (prefill) {
    if (!knownRoles.includes(prefill.role)) sel.insertAdjacentHTML("afterbegin", `<option>${prefill.role}</option>`);
    sel.value = prefill.role;
    sel.disabled = true;
    form.title.value = prefill.title;
    form.description.value = prefill.description;
    let deps = []; try { deps = JSON.parse(prefill.deps || "[]"); } catch { /* */ }
    form.depends_on.value = deps.map(seqOf).join(", ");
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
  // The boss types per-project numbers; the API stores real task ids.
  const deps = String(f.get("depends_on") || "").split(",")
    .map((s) => Number(s.trim())).filter(Boolean).map(idOfSeq);
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
  chip.addEventListener("click", () => switchView(chip.dataset.v)));

// --- notification centre -----------------------------------------------------
async function refreshBell() {
  let n;
  try { n = await api("/api/notifications"); } catch { return; }
  const badge = $("#bellCount");
  badge.hidden = n.count === 0;
  badge.textContent = n.count;
  $("#bellBtn").classList.toggle("has-items", n.count > 0);
  const panel = $("#bellPanel");
  if (panel.hidden) return;                 // only rebuild when it's open
  panel.innerHTML = n.count === 0
    ? `<div class="bell-empty">Nothing needs you right now.</div>`
    : n.items.map((it) => `
      <div class="bell-item" data-q="${it.question_id}">
        <div class="bell-proj">${escapeHtml(it.project)} · #${it.project_id}</div>
        <div class="bell-q">${escapeHtml(trim(it.question, 220))}</div>
        <div class="bell-opts">
          ${(it.options || []).slice(0, 4).map((o, i) =>
            `<button data-q="${it.question_id}" data-opt="${i}">${escapeHtml(trim(o, 60))}</button>`).join("")}
          <button class="link" data-open="${it.project_id}">Open project →</button>
        </div>
      </div>`).join("");
  panel.querySelectorAll("[data-opt]").forEach((b) =>
    b.addEventListener("click", async () => {
      const item = n.items.find((x) => String(x.question_id) === b.dataset.q);
      await answerQuestion(Number(b.dataset.q), item.options[Number(b.dataset.opt)]);
      refreshBell();
    }));
  panel.querySelectorAll("[data-open]").forEach((b) =>
    b.addEventListener("click", () => {
      panel.hidden = true;
      openProject(Number(b.dataset.open));
    }));
}

$("#bellBtn").addEventListener("click", () => {
  const panel = $("#bellPanel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) refreshBell();
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".bell-wrap")) $("#bellPanel").hidden = true;
});

document.querySelectorAll(".vb").forEach((chip) =>
  chip.addEventListener("click", () => {
    document.querySelectorAll(".vb").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    $("#feed").dataset.verbosity = chip.dataset.verb;
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
  [1, 2, 3].forEach((i) => { $("#step" + i).hidden = i !== n; });
  document.querySelectorAll(".wstep").forEach((w) =>
    w.classList.toggle("active", Number(w.dataset.s) <= n));
}
const openDialog = () => {
  $("#newProjectForm").reset();
  ["#formError", "#formError2", "#formError3"].forEach((s) => ($(s).hidden = true));
  $("#roster").innerHTML = "";
  showStep(1);
  dialog.showModal();
};
$("#projectSelect").addEventListener("change", (e) => selectProject(e.target.value));
$("#homeLink").addEventListener("click", () => showHome());
$("#homeLink").style.cursor = "pointer";
$("#newProjectBtn").addEventListener("click", openDialog);
$("#homePlanBtn").addEventListener("click", openPlan);
$("#planBackBtn").addEventListener("click", () => {
  clearInterval(tablePoll); $("#plan").hidden = true; showHome();
});
$("#addSeatBtn").addEventListener("click", () => {
  if (seats.length >= 8) { flashSeatWarn("Eight seats is the hard ceiling."); return; }
  const avail = providerCatalog.available || ["anthropic"];
  const prov = avail[seats.length % avail.length];
  const preset = PERSONA_PRESETS[seats.length % PERSONA_PRESETS.length];
  const ms = modelsFor(prov);
  const model = avail.length > 1 ? (ms[0]?.id || "")
    : (ms[seats.length % Math.max(ms.length, 1)]?.id || ms[0]?.id || "");
  seats.push({ uid: ++seatSeq, name: preset[0], provider: prov, model, persona: preset[1] });
  renderSeats();
});
document.querySelectorAll("input[name=tmode]").forEach((r) =>
  r.addEventListener("change", updateSeatWarning));
$("#authBadge").addEventListener("click", () => $("#settingsBtn").click());
$("#chatForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const inp = $("#chatInput");
  const text = inp.value.trim();
  if (!text || !currentProject) return;
  inp.value = "";
  try {
    await api(`/api/projects/${currentProject}/directive`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    $("#chatStatus").textContent = "sent — your manager reads it at its next decision point";
    setTimeout(() => { $("#chatStatus").textContent = ""; }, 6000);
  } catch (e) {
    inp.value = text;      // don't lose what they typed
    alert(e.message);
  }
});
$("#startTableBtn").addEventListener("click", startTable);
$("#whyCircle").addEventListener("click", (e) => {
  e.preventDefault();
  alert("The seating rules come from research, not taste — including the "
    + "uncomfortable part:\n\n"
    + "\u2022 Debate does NOT reliably beat one good model on benchmarks with a "
    + "single right answer. On GSM8k, self-consistency (95.7%) still edges the best "
    + "mixed-model debate (95.0%). Use a table for OPEN-ENDED planning, where there "
    + "is no right answer to vote on \u2014 not to get a more correct answer.\n\n"
    + "\u2022 Mixing PROVIDERS is the one lever shown to reliably help debate. "
    + "Identical seats are the worst setup: more expensive, no measured upside.\n\n"
    + "\u2022 Round 1 is independent so nobody anchors the group (first speakers win far above chance).\n\n"
    + "\u2022 Round 2 forces dissent \u2014 structured conflict beats consensus on decision quality.\n\n"
    + "\u2022 Equal turn-taking predicts group intelligence; every seat speaks exactly once per round.\n\n"
    + "\u2022 3-6 seats. Past ~7, deliberation degrades.\n\n"
    + "See docs/ROUNDTABLE_DESIGN.md for the papers.");
});
$("#homeNewBtn").addEventListener("click", openDialog);
$("#backToBriefBtn").addEventListener("click", () => showStep(1));
$("#backToTeamBtn").addEventListener("click", () => showStep(2));
$("#addRoleBtn").addEventListener("click", () =>
  renderRoster(readRoster().concat([{ role: "", count: 1, model: "worker" }])));
$("#toGoBtn").addEventListener("click", () => {
  if (readRoster().length === 0) {
    $("#formError2").textContent = "Add at least one role."; $("#formError2").hidden = false; return;
  }
  $("#formError2").hidden = true;
  showStep(3);
});

const INGEST_STEPS = [
  "Reading your idea…", "Understanding the scope…", "Identifying the skills needed…",
  "Deciding how many of each role…", "Assembling your A-team…",
];
let ingestTimer = null;
function runIngestAnimation() {
  const bar = $("#progressBar"), msg = $("#ingestMsg");
  let i = 0, pct = 8;
  bar.style.width = pct + "%";
  msg.textContent = INGEST_STEPS[0];
  clearInterval(ingestTimer);
  ingestTimer = setInterval(() => {
    pct = Math.min(pct + Math.random() * 14, 92);   // creep toward 92, finish on response
    bar.style.width = pct + "%";
    i = Math.min(i + 1, INGEST_STEPS.length - 1);
    msg.textContent = INGEST_STEPS[i];
  }, 700);
}
function finishIngest() {
  clearInterval(ingestTimer);
  $("#progressBar").style.width = "100%";
}

$("#toRecruitBtn").addEventListener("click", async () => {
  const form = $("#newProjectForm");
  const err = $("#formError");
  err.hidden = true;
  if (!form.name.value.trim() || !form.brief.value.trim()) {
    err.textContent = "Project name and brief are required."; err.hidden = false; return;
  }
  showStep(2);
  $("#ingest").hidden = false;      // show progress, hide roster until ingested
  $("#rosterWrap").hidden = true;
  runIngestAnimation();
  let team, roles;
  try {
    const res = await api("/api/suggest-team", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brief: form.brief.value }),
    });
    team = res.team.length ? res.team : [{ role: "backend", count: 1, model: "worker" }];
    roles = res.known_roles || knownRoles;
    $("#recruitNote").textContent = "Your manager sized up the idea and drafted this team — hire, fire, bump counts, or add anyone.";
  } catch (e) {
    team = [{ role: "backend", count: 1, model: "worker" }, { role: "tester", count: 1, model: "worker" }];
    roles = knownRoles;
    $("#recruitNote").textContent = "Couldn't auto-suggest — here's a default team to edit.";
  }
  finishIngest();
  knownRoles = [...new Set([...roles, ...team.map((m) => m.role)])];
  setTimeout(() => {   // let the bar hit 100% before revealing
    $("#ingest").hidden = true;
    $("#rosterWrap").hidden = false;
    renderRoster(team);
  }, 450);
});
$("#closeDialogBtn").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
$("#cancelBtn").addEventListener("click", async () => {
  if (!currentProject) return;
  if (!confirm("Cancel this project?\n\nEvery agent still working on it is stopped "
    + "immediately, their unpushed work is lost, and open GitHub issues are closed."))
    return;
  const r = await api(`/api/projects/${currentProject}/cancel`, { method: "POST" });
  if (r.agents_stopped) toast(`Cancelled — stopped ${r.agents_stopped} running agent(s).`);
  refreshBoard();
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
  const errBox = $("#formError3");
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
        max_workers: Number(f.get("max_workers")) || 3, max_runs: Number(f.get("max_runs")) || 40,
        team: readRoster(), autonomy: f.get("autonomy") || "supervised",
        manager_model: f.get("manager_model") || "",
        manager_persona: f.get("manager_persona") || "",
      }),
    });
    dialog.close();
    openProject(res.id);
  } catch (e) {
    errBox.textContent = e.message;
    errBox.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "🚀 Hire team & start";
  }
});

async function boot() {
  if (!(await loadMe())) return;      // show login screen until signed in
  await loadHealth();
  loadRepos();
  route();                    // restore whatever the URL points at
  refreshBell();
  setInterval(refreshBell, 20000);
  if (!ws) connectWs();
  if (window.Notification && Notification.permission === "default")
    setTimeout(() => Notification.requestPermission(), 1500);
}
boot();
setInterval(() => { if (currentProject) refreshBoard(); else loadProjects(); }, 10000);
