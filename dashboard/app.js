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
  applyPermissions();
  return me.signed_in;
}

// Whether the Improve tile is shown depends only on who you are, so it is applied
// wherever that becomes known — not, as before, only inside showHome(). Landing
// straight on a project URL never calls showHome, so the tile stayed hidden until
// something else happened to route you home or you reloaded the page.
function applyPermissions() {
  const improve = $("#modeImprove");
  if (!improve) return;
  // Fall back to is_root when the server predates may_self_repair — otherwise the
  // tile silently vanishes for the one account that definitely has the right.
  improve.hidden = !(me && (me.may_self_repair ?? me.is_root));
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

// Faint grey "— currently set ✓" read as decoration next to the label. A chip
// reads as state. Note it still only means "stored" — "works" needs a Check.
function paintCredChips(s) {
  const chip = (id, isSet) => {
    const el = $(id);
    if (!el) return;
    el.textContent = isSet ? "saved" : "not set";
    el.className = "chip " + (isSet ? "ok" : "off");
  };
  chip("#ghState", s.github_token_set);
  chip("#keyState", s.anthropic_api_key_set);
  chip("#subState", s.claude_oauth_token_set);
  chip("#oaState", s.openai_api_key_set);
  chip("#gmState", s.gemini_api_key_set);
}

$("#aboutBtn").addEventListener("click", () => openAbout());

$("#settingsBtn").addEventListener("click", async () => {
  await loadMe();
  $("#settingsWho").textContent = `Signed in as ${me.username}${me.is_root ? " (root)" : ""}`;
  paintCredChips(me.settings || {});
  $("#settingsError").hidden = true;
  $("#settingsForm").reset();
  // Last visit's verdicts are stale the moment the dialog reopens.
  document.querySelectorAll("#settingsForm .check-result").forEach((el) => {
    el.hidden = true;
    el.textContent = "";
  });
  $("#settingsDialog").showModal();
});
$("#closeSettingsBtn").addEventListener("click", () => $("#settingsDialog").close());
$("#closeSettingsX").addEventListener("click", () => $("#settingsDialog").close());

// --- live credential verification ------------------------------------------
// "saved" only ever meant "we stored the characters you typed". A retired model,
// a Google project without billing, a GitHub token missing a scope and an expired
// subscription token all looked identical to a working setup — and you found out
// hours later when a project died. These check for real, on the spot.

function showCheck(kind, state, detail, hint) {
  const el = document.querySelector(`[data-result="${kind}"]`);
  if (!el) return;
  el.hidden = false;
  el.className = "check-result " + state;
  const icon = state === "ok" ? "✓" : state === "bad" ? "✕" : "…";
  el.innerHTML = `${icon} ${escapeHtml(detail)}` +
    (hint ? `<span class="why">${escapeHtml(hint)}</span>` : "");
}

async function verifyCred(kind) {
  const input = document.querySelector(`#settingsForm [name="${kind}"]`);
  const btn = document.querySelector(`[data-check="${kind}"]`);
  const typed = (input && input.value.trim()) || "";
  showCheck(kind, "working", typed ? "checking what you entered…"
                                   : "checking the saved credential…");
  if (btn) btn.disabled = true;
  try {
    const r = await api("/api/settings/verify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, value: typed }),
    });
    showCheck(kind, r.ok ? "ok" : "bad", r.detail || "", r.hint || "");
    return r.ok;
  } catch (e) {
    showCheck(kind, "bad", e.message || "the check could not run");
    return false;
  } finally {
    if (btn) btn.disabled = false;
  }
}

document.querySelectorAll("[data-check]").forEach((b) =>
  b.addEventListener("click", () => verifyCred(b.dataset.check)));
$("#settingsForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  // Send every credential the form carries. This used to name three fields by
  // hand, so the OpenAI and Gemini inputs were decorative: you could paste a key,
  // hit Save, get a success, and have it silently dropped on the floor.
  const body = {};
  for (const [k, v] of f.entries()) {
    const val = (v || "").trim();
    if (val) body[k] = val;
  }
  const submitBtn = ev.target.querySelector("button[type=submit]");
  try {
    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = "Saving…"; }
    await api("/api/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await loadMe();
    paintCredChips(me.settings || {});
    loadRepos();                       // a GitHub token may have just been added

    const kinds = Object.keys(body);
    if (!kinds.length) { $("#settingsDialog").close(); return; }
    // Verify before the user walks away believing a broken key is fine. Saving
    // still succeeds either way — a key can be valid while the provider is having
    // a bad minute, and refusing to store it would be the wrong call.
    if (submitBtn) submitBtn.textContent = "Verifying…";
    const results = await Promise.all(kinds.map(verifyCred));
    if (results.every(Boolean)) {
      toast("Credentials saved and verified");
      setTimeout(() => $("#settingsDialog").close(), 1200);
    } else {
      toast("Saved, but some credentials did not verify");
    }
  } catch (e) {
    $("#settingsError").textContent = e.message;
    $("#settingsError").hidden = false;
  } finally {
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = "Save & verify"; }
  }
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

function showSandboxBanner() {
  if ($("#sandboxBanner")) return;
  const el = document.createElement("div");
  el.id = "sandboxBanner";
  el.innerHTML = `<b>Sandbox build.</b> Agents are simulated, GitHub is mocked and no
    credentials are loaded — everything here is fake except the code you are testing.`;
  document.body.prepend(el);
}

function showStaleBanner() {
  if ($("#staleBanner")) return;
  const el = document.createElement("div");
  el.id = "staleBanner";
  // Written for someone who did not build this. The first version said "the
  // dashboard files on disk changed after the conductor started" — true, and
  // meaningless unless you already knew what it meant.
  el.innerHTML = `<b>The app is half-updated.</b> This page has newer code than the
    server behind it, so some things may look empty or do nothing — that is not a
    real fault. Restarting the server fixes it:
    <code>PYTHONPATH=conductor .venv/bin/uvicorn app.main:app --port 8000</code>`;
  document.body.prepend(el);
}

// A JavaScript error was only ever visible to someone with DevTools open —
// which, on a platform meant to run unattended, is nobody. Report it once per
// distinct message so a render loop cannot hammer the endpoint.
const _reported = new Set();
function reportClientError(message, stack, url) {
  const key = String(message).slice(0, 200);
  if (_reported.has(key) || _reported.size > 20) return;
  _reported.add(key);
  fetch("/api/client-error", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: String(message).slice(0, 500),
                           stack: String(stack || "").slice(0, 1500),
                           url: location.hash || location.pathname }),
  }).catch(() => { /* if reporting fails, stay quiet: never a loop */ });
}
window.addEventListener("error", (e) =>
  reportClientError(e.message, e.error && e.error.stack, e.filename));
window.addEventListener("unhandledrejection", (e) =>
  reportClientError("unhandled promise rejection: " + (e.reason && e.reason.message || e.reason),
                    e.reason && e.reason.stack));

async function loadHealth() {
  try {
    const h = await api("/api/health");
    authMode = h.auth || "none";
    // The dashboard is served from disk; the API is whatever process is running.
    // Change both and the page runs ahead of the server, which looks like a broken
    // feature — an empty dropdown, a button that does nothing — rather than a
    // conductor that needs restarting. Say which it is.
    if (h.stale_ui) showStaleBanner();
    if (h.demo) showSandboxBanner();
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
const STATUS_LABEL = { hold: "on hold — needs you", review: "in review",
                       idle: "idle — nothing running" };

async function loadProjects() {
  const projects = await api("/api/projects");
  const sel = $("#projectSelect");
  sel.innerHTML = "";
  for (const p of projects) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.dataset.repo = p.repo || "";
    opt.textContent = `${p.name} — ${p.status}`;
    sel.appendChild(opt);
  }
  if (currentProject) sel.value = currentProject;
  renderHome(projects);
}

function renderHome(projects) {
  const body = $("#projectsBody");
  body.innerHTML = "";
  $("#homeEmpty").hidden = projects.length > 0;
  projects.forEach((p, idx) => {
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
      <!-- A position in YOUR list, not the database id. The platform's own
           project takes row 1 and is hidden from this table, so the first
           project anyone creates showed up as "2" and looked like a bug. -->
      <td>${idx + 1}</td>
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
        ${p.is_self ? "" : `<button data-act="delete" data-id="${p.id}" class="danger"
          title="Delete this project and everything under it">🗑</button>`}
      </td>`;
    tr.addEventListener("click", () => openProject(p.id));
    body.appendChild(tr);
  });
  body.querySelectorAll(".row-actions button").forEach((b) =>
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const id = b.dataset.id;
      if (b.dataset.act === "open") return openProject(id);
      if (b.dataset.act === "cancel" && confirm("Cancel this project?"))
        await api(`/api/projects/${id}/cancel`, { method: "POST" });
      if (b.dataset.act === "restart")
        await api(`/api/projects/${id}/restart`, { method: "POST" });
      if (b.dataset.act === "delete") {
        const p2 = (await api("/api/projects")).find((x) => String(x.id) === String(id));
        if (!confirm(`Delete "${p2 ? p2.name : id}" for good?\n\n`
          + "Its tasks, activity and any running agents go with it. The GitHub repo "
          + "and its PRs are NOT touched. This cannot be undone.")) return;
        const r = await api(`/api/projects/${id}`, { method: "DELETE" });
        toast(`Deleted — ${r.tasks} task(s), ${r.events} event(s)`
          + (r.agents_stopped ? `, stopped ${r.agents_stopped} agent(s)` : ""));
      }
      loadProjects();
    }));
}

// The platform working on itself. /api/self creates-or-returns the row for this
// repo, so there is nothing for the user to set up first.
function renderCloudInstance(el, inst) {
  const can = inst.can_self_update || {};
  const busy = can.busy || [];
  el.innerHTML = `
    <div class="env-prod">
      <span class="env-dot"></span><b>This instance</b>
      <code>${escapeHtml(inst.image || "unknown")}</code>
      ${inst.build_commit ? `<span class="hint">built from ${escapeHtml(inst.build_commit)}</span>` : ""}
    </div>
    <p class="hint">Running in Kubernetes (<code>${escapeHtml(inst.namespace)}</code>), so it
      builds nothing itself — CI publishes an image on every merge to main and this
      instance decides when to take it. Updating replaces this pod, which is why it
      waits for the team to be idle.</p>
    ${busy.length ? `<p class="sbx-dirty">Not now — ${busy.length} agent(s) are working:
        ${escapeHtml(busy.slice(0, 3).join("; "))}${busy.length > 3 ? "…" : ""}.
        Updating would throw that work away.</p>` : ""}
    ${(can.reasons || []).length ? `<p class="form-error">${escapeHtml(can.reasons.join("; "))}</p>` : ""}
    <div id="cloudImages"><p class="hint">looking for published versions…</p></div>
    <h4>Ship a new version</h4>
    <div class="sbx-start">
      <label>Image <input id="cloudImage" placeholder="registry.digitalocean.com/…/devteam-conductor:main-abc123"></label>
      <button id="cloudUpdate" class="primary" ${busy.length ? "disabled" : ""}>Update this instance</button>
      <button id="cloudRollback" class="danger">Roll back</button>
    </div>
    <p class="hint" id="cloudMsg">The pod is replaced, so this page will briefly lose its
      connection. If the new image cannot start, Kubernetes keeps the old one running.</p>`;

  // What CI has actually published. Pasting a tag by hand was the last manual
  // step in a loop that is otherwise autonomous.
  api("/api/self/images").then((d) => {
    const box = $("#cloudImages");
    if (!box) return;
    if (!(d.images || []).length) {
      box.innerHTML = `<p class="hint">No published images visible. CI publishes one on
        every merge to main; check DIGITALOCEAN_API_TOKEN and DOCR_REGISTRY are set.</p>`;
      return;
    }
    const c = d.candidate;
    box.innerHTML = `
      ${c ? `<div class="sbx live"><div class="sbx-head"><span class="sbx-dot"></span>
          <b>A newer version is waiting</b><span class="hint">${escapeHtml(c.short)}</span></div>
        <div class="sbx-actions"><button data-take="${escapeHtml(c.tag)}" class="primary"
          ${(d.busy || []).length ? "disabled" : ""}>Take it</button></div>
        ${(d.busy || []).length ? `<p class="hint">Waiting for ${d.busy.length} agent(s) to finish
          — updating now would throw their work away.</p>` : ""}
        ${d.auto_update ? `<p class="hint">AUTO_UPDATE is on, so this happens by itself
          once the team is idle.</p>` : ""}</div>`
        : `<p class="hint">Running the newest published image.</p>`}
      <h4>Published</h4>
      ${d.images.map((i) => `<div class="env-row"><div><code>${escapeHtml(i.short)}</code>
        <span class="hint">${i.running ? "running now" : escapeHtml(i.updated_at || "")}</span></div>
        ${i.running ? "" : `<div class="env-acts"><button data-take="${escapeHtml(i.tag)}"
          ${(d.busy || []).length ? "disabled" : ""}>Use this</button></div>`}</div>`).join("")}`;
    box.querySelectorAll("[data-take]").forEach((b) => b.addEventListener("click", () => {
      $("#cloudImage").value = b.dataset.take;
      $("#cloudUpdate").click();
    }));
  }).catch(() => { const b = $("#cloudImages"); if (b) b.innerHTML = ""; });

  $("#cloudUpdate").addEventListener("click", async () => {
    const image = $("#cloudImage").value.trim();
    if (!image) { $("#cloudMsg").textContent = "Paste the image tag CI published."; return; }
    if (!confirm(`Replace this instance with:\n${image}\n\n`
      + "The pod restarts, so you will be disconnected for a few seconds.")) return;
    $("#cloudMsg").textContent = "Patching the Deployment — this pod is about to be replaced…";
    try {
      await api("/api/self/update", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ image }) });
      $("#cloudMsg").textContent = "Kubernetes is rolling it out. Reload in a few seconds.";
    } catch (e) { $("#cloudMsg").textContent = String(e.message || e); }
  });
  $("#cloudRollback").addEventListener("click", async () => {
    if (!confirm("Roll this instance back to the previous image?")) return;
    try { await api("/api/self/update/rollback", { method: "POST" });
          $("#cloudMsg").textContent = "Rolling back. Reload in a few seconds."; }
    catch (e) { $("#cloudMsg").textContent = String(e.message || e); }
  });
}

const HEAL_ICON = {
  self_healed: "✅", canary_failed: "🛑", auto_update: "⬆️",
  self_update: "⬆️", notified: "📣", digest_filed: "📋", rolled_back: "↩️",
};

async function renderHealing() {
  const el = $("#healBody");
  if (!el) return;
  let d;
  try { d = await api("/api/self/healing"); }
  catch (e) { el.innerHTML = `<p class="empty">${escapeHtml(e.message || e)}</p>`; return; }
  if (!(d.items || []).length) {
    el.innerHTML = `<p class="hint">Nothing yet. Routine fixes will appear here as they
      happen; a build that fails its trial run shows up too, so silence means nothing
      went wrong rather than nothing was checked.</p>`;
    return;
  }
  el.innerHTML = `<div class="heal-list">${d.items.map((i) => {
    const x = i.detail || {};
    // A rejected build is the most valuable line here: it is the platform
    // declining to ship something to itself, which is easy to miss otherwise.
    const bad = i.kind === "canary_failed";
    const detail = x.summary || x.note || x.why || x.reason
      || (x.to ? `now on ${String(x.to).split(":").pop()}` : "")
      || (x.issue ? `issue #${x.issue}` : "");
    const fixed = (x.fixed || []).length
      ? `<ul class="heal-fixed">${x.fixed.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>` : "";
    return `<div class="heal ${bad ? "bad" : ""}">
      <span class="heal-ico">${HEAL_ICON[i.kind] || "•"}</span>
      <div><b>${escapeHtml(i.what)}</b>
        <span class="hint">${ago(i.at)}</span>
        ${detail ? `<div class="heal-detail">${escapeHtml(String(detail).slice(0, 220))}</div>` : ""}
        ${fixed}</div>
    </div>`;
  }).join("")}</div>`;
}

async function renderEnvs() {
  const el = $("#envsBody");
  if (!el) return;
  let d;
  try { d = await api("/api/self/envs"); }
  catch (e) { el.innerHTML = `<p class="empty">${escapeHtml(e.message || e)}</p>`; return; }

  // In a cluster the platform cannot build anything — no Docker daemon, and
  // giving it one would let it build and run arbitrary images as itself. There
  // the loop is: CI builds, and this instance chooses when to take the result.
  let inst = null;
  try { inst = await api("/api/self/instance"); } catch { /* older server */ }
  if (inst && inst.in_cluster) { renderCloudInstance(el, inst); return; }

  // Say plainly what is missing rather than showing dead buttons.
  if (!d.docker || !d.kubernetes) {
    const missing = [!d.docker && "Docker", !d.kubernetes && "a Kubernetes cluster"]
      .filter(Boolean).join(" and ");
    el.innerHTML = `<p class="empty">Environments need ${escapeHtml(missing)}.
      The sandbox below works without either.</p>`;
    return;
  }

  const prod = d.production;
  const envRows = Object.entries(d.envs || {}).map(([name, e]) => `
    <div class="env-row">
      <div><b>${escapeHtml(name)}</b>
        <span class="hint">${escapeHtml(e.host)} · ${escapeHtml(e.tag)} · ${ago(e.at)}</span></div>
      <div class="env-acts">
        <button data-promote="${escapeHtml(e.tag)}" class="primary">Promote to production</button>
        <button data-destroy="${escapeHtml(name)}" class="danger">Destroy</button>
      </div>
    </div>`).join("") || `<p class="hint">No preview environments running.</p>`;

  const imgRows = (d.images || []).map((i) => `
    <div class="env-row">
      <div><code>${escapeHtml(i.tag)}</code>
        <span class="hint">from ${escapeHtml(i.source)} · ${ago(i.built_at)}${
          i.note ? " · " + escapeHtml(i.note) : ""}</span></div>
      <div class="env-acts">
        <button data-try="${escapeHtml(i.tag)}">Try it</button>
        <button data-promote="${escapeHtml(i.tag)}">Promote</button>
      </div>
    </div>`).join("") || `<p class="hint">Nothing built yet.</p>`;

  el.innerHTML = `
    <div class="env-prod">
      <span class="env-dot"></span><b>Production</b>
      <code>${escapeHtml(prod ? prod.tag : "not managed from here yet")}</code>
      ${prod ? `<span class="hint">promoted ${ago(prod.at)}</span>` : ""}
      ${prod ? `<button id="envRollback" class="danger">Roll back</button>` : ""}
    </div>

    <h4>Build a new one</h4>
    <div class="sbx-start">
      <label>From <select id="envSource"></select></label>
      <label>Label <input id="envNote" placeholder="what is in it — e.g. sprint archive"></label>
      <button id="envBuild" class="primary">Build image</button>
    </div>
    <p class="hint" id="envMsg">Building takes about a minute. The tag includes a hash of
      the exact files, so two builds of the same branch are never confused.</p>

    <h4>Preview environments</h4>${envRows}
    <h4>Artifacts</h4>${imgRows}`;

  // reuse the sandbox's source list — same sources, same meaning
  try {
    const sb = await api("/api/self/sandbox");
    $("#envSource").innerHTML = (sb.sources || []).map((s) =>
      `<option value="${escapeHtml(s.id)}">${escapeHtml(s.label)} — ${escapeHtml(s.detail)}</option>`).join("");
  } catch { /* the select stays empty; the button will say why */ }

  const msg = (t) => { $("#envMsg").textContent = t; };
  $("#envBuild").addEventListener("click", async () => {
    const b = $("#envBuild"); b.disabled = true; b.textContent = "building…";
    msg("Building — this takes about a minute.");
    try {
      const r = await api("/api/self/envs/build", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source: $("#envSource").value, note: $("#envNote").value }) });
      toast(`Built ${r.tag}`);
      renderEnvs();
    } catch (e) { msg(String(e.message || e)); b.disabled = false; b.textContent = "Build image"; }
  });

  el.querySelectorAll("[data-try]").forEach((b) => b.addEventListener("click", async () => {
    const name = prompt("Name this environment (it gets its own namespace, database and host):",
                        "try-" + Math.random().toString(36).slice(2, 6));
    if (!name) return;
    b.disabled = true; b.textContent = "deploying…";
    try {
      const r = await api("/api/self/envs/deploy", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: b.dataset.try, env: name }) });
      toast(`${r.env} is up at ${r.host}`);
    } catch (e) { toast(String(e.message || e)); }
    renderEnvs();
  }));

  el.querySelectorAll("[data-promote]").forEach((b) => b.addEventListener("click", async () => {
    // Promotion replaces the platform everyone is using. Name the artifact in the
    // confirm, so this cannot be a reflex click.
    if (!confirm(`Point production at ${b.dataset.promote}?\n\n`
      + "This is the exact image you previewed — nothing is rebuilt. "
      + "If it fails to come up it rolls back automatically.")) return;
    b.disabled = true; b.textContent = "promoting…";
    try {
      const r = await api("/api/self/envs/promote", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: b.dataset.promote }) });
      toast(`Production is now ${r.tag}`);
    } catch (e) { toast(String(e.message || e)); }
    renderEnvs();
  }));

  el.querySelectorAll("[data-destroy]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Destroy ${b.dataset.destroy} and its database?`)) return;
    try { await api(`/api/self/envs/${encodeURIComponent(b.dataset.destroy)}`, { method: "DELETE" }); }
    catch (e) { toast(String(e.message || e)); }
    renderEnvs();
  }));

  const rb = $("#envRollback");
  if (rb) rb.addEventListener("click", async () => {
    if (!confirm("Roll production back to the previous image?")) return;
    try { await api("/api/self/envs/rollback", { method: "POST" }); toast("Rolled back"); }
    catch (e) { toast(String(e.message || e)); }
    renderEnvs();
  });
}

async function renderSandbox() {
  const el = $("#sandboxBody");
  if (!el) return;
  let d;
  try { d = await api("/api/self/sandbox"); }
  catch (e) { el.innerHTML = `<p class="empty">${escapeHtml(e.message || e)}</p>`; return; }

  if (d.running) {
    const mins = Math.max(0, Math.round((Date.now() / 1000 - d.started_at) / 60));
    el.innerHTML = `
      <div class="sbx live">
        <div class="sbx-head"><span class="sbx-dot"></span>
          <b>Sandbox running</b><span class="hint">${escapeHtml(d.origin || d.ref)} · ${escapeHtml(d.commit)} · up ${mins}m</span></div>
        ${d.dirty ? `<p class="sbx-dirty">Includes ${d.dirty} uncommitted change(s) — this is code that exists nowhere else yet.</p>` : ""}
        <p class="sbx-sub">${escapeHtml(d.subject || "")}</p>
        <div class="sbx-actions">
          <a class="btn-like primary" href="${escapeHtml(d.url)}" target="_blank" rel="noopener">↗ Open the sandbox</a>
          <button id="sbxStop" class="danger">Stop &amp; clean up</button>
        </div>
        <p class="hint">Sign in there as <code>root</code> / <code>sandbox</code>. It has
          its own database seeded with demo data — nothing you do in it touches this app.</p>
      </div>`;
    $("#sbxStop").addEventListener("click", async () => {
      const b = $("#sbxStop"); b.disabled = true; b.textContent = "stopping…";
      try { await api("/api/self/sandbox", { method: "DELETE" }); } catch (e) { toast(String(e.message || e)); }
      renderSandbox();
  renderEnvs();
  renderHealing();
    });
    return;
  }

  // Sources are ordered by immediacy: this working tree, then agent workspaces,
  // then branches. Nothing here needs a commit — waiting on one is what made
  // trying a change slower than making it.
  const opts = (d.sources || d.branches || []).map((s) =>
    `<option value="${escapeHtml(s.id || ("ref:" + s.ref))}">${escapeHtml(s.label || s.name)} — ${escapeHtml(s.detail || s.subject || "")}</option>`).join("");
  el.innerHTML = `
    ${d.died ? `<p class="form-error">The last sandbox exited on its own.
       <details><summary>log</summary><pre class="sbx-log">${escapeHtml(d.log_tail || "")}</pre></details></p>` : ""}
    <div class="sbx-start">
      <label>What to run <select id="sbxRef">${opts || "<option value=''>nothing to run</option>"}</select></label>
      <button id="sbxStart" class="primary">▶ Boot the sandbox</button>
    </div>`;
  $("#sbxStart").addEventListener("click", async () => {
    const ref = $("#sbxRef").value;
    if (!ref) return;
    const b = $("#sbxStart"); b.disabled = true; b.textContent = "booting…";
    try {
      await api("/api/self/sandbox", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ref }) });
      toast("Sandbox is up");
    } catch (e) { toast(String(e.message || e)); }
    renderSandbox();
  });
}

async function openSelfRepair(skipHash) {
  $("#home").hidden = true; $("main").hidden = true;
  const pl = $("#plan"); if (pl) pl.hidden = true;
  const ab = $("#aboutPage"); if (ab) ab.hidden = true;
  const scn = $("#scenes"); if (scn) scn.hidden = true;
  const lw = $("#lifeworld"); if (lw) lw.hidden = true;
  $("#projectBar").hidden = true;
  $("#selfPage").hidden = false;
  if (!skipHash) setHash("#/improve");
  currentProject = null;
  await renderSelf();
}

function openAbout(skipHash) {
  $("#home").hidden = true; $("main").hidden = true;
  const pl = $("#plan"); if (pl) pl.hidden = true;
  const sp = $("#selfPage"); if (sp) sp.hidden = true;
  const scn = $("#scenes"); if (scn) scn.hidden = true;
  const lw = $("#lifeworld"); if (lw) lw.hidden = true;
  $("#projectBar").hidden = true;
  $("#aboutPage").hidden = false;
  if (!skipHash) setHash("#/about");
  currentProject = null;
  window.scrollTo(0, 0);
}

function showHome(skipHash) {
  const st = $("#studio"); if (st) st.hidden = true;
  const scn = $("#scenes"); if (scn) scn.hidden = true;
  const lw = $("#lifeworld"); if (lw) lw.hidden = true;
  const pl = $("#plan"); if (pl) pl.hidden = true;
  const sp = $("#selfPage"); if (sp) sp.hidden = true;
  const ab = $("#aboutPage"); if (ab) ab.hidden = true;
  if (!skipHash) setHash("#/");
  currentProject = null;
  applyPermissions();
  $("#home").hidden = false;
  $("main").hidden = true;
  // The whole project bar goes away with the project — none of it means anything
  // on the home screen, which is why it never belonged in the global header.
  $("#projectBar").hidden = true;
  $("#costBadge").hidden = true;
  $("#statusBadge").hidden = true;
  $("#sprintBadge").hidden = true;
  $("#restartBtn").hidden = true;
  $("#cancelBtn").hidden = true;
  loadProjects();
}

function openProject(id, view, skipHash) {
  const sp = $("#selfPage"); if (sp) sp.hidden = true;
  const ab = $("#aboutPage"); if (ab) ab.hidden = true;
  const st = $("#studio"); if (st) st.hidden = true;
  const scn = $("#scenes"); if (scn) scn.hidden = true;
  const lw = $("#lifeworld"); if (lw) lw.hidden = true;
  $("#home").hidden = true;
  $("main").hidden = false;
  $("#projectBar").hidden = false;
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
  for (const id of ["command", "board", "dag", "artifacts", "agents", "chat", "blockers",
                    "notices", "self"])
    $("#" + id).hidden = id !== view;
  if (view === "dag") renderDag(lastTasks);
  if (view === "command" && lastProject) renderCommand(lastProject);
  if (view === "artifacts") renderArtifacts(true);
  if (view === "agents") { agentsSig = ""; renderAgents(); }
  if (view === "chat") { chatSig = ""; renderChat(); markChatRead(); }
  if (view === "blockers") { blockersSig = ""; renderBlockers(); }
  if (view === "notices") { blockersSig = ""; renderBlockers(); }
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
  if (location.hash.startsWith("#/studio")) { openStudio(true); return; }
  {
    const sc = location.hash.match(/^#\/scenes(?:\/([\w-]+))?/);
    if (sc) { openScenes(true, sc[1] || null); return; }
  }
  if (location.hash.startsWith("#/lifeworld")) { openStudio(true); return; }   // legacy link
  if (location.hash.startsWith("#/about")) { openAbout(true); return; }
  if (location.hash.startsWith("#/improve")) { openSelfRepair(true); return; }
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
      <!-- Say it however you'd say it out loud; the draft step turns that into a
           ticket. A vague ticket is the cheapest way to waste a sprint — the team
           builds the wrong thing, competently. -->
      <div class="rough-row">
        <textarea id="roughIssue" rows="2"
          placeholder="Describe it however you'd say it — e.g. &quot;the blockers tab looks broken and I can't read the text&quot;"></textarea>
        <button type="button" id="refineBtn">✨ Draft the ticket</button>
      </div>
      <p class="hint" id="refineNote">Drafting reads your words and fills in the form
        below — what happens now, what should happen, where it probably lives, and how
        to check the fix. You edit it before anything is filed.</p>

      <form id="selfIssueForm">
        <label>Title
          <input name="title" required placeholder="e.g. Agents tab loses scroll position" autocomplete="off"></label>
        <label>Details <span class="hint">steps to reproduce, what you expected, which page</span>
          <textarea name="body" rows="8" required placeholder="Be specific — this becomes the spec a worker builds against."></textarea></label>
        <div class="issue-opts">
          <label>Kind
            <select name="severity">
              <option value="bug">Bug — something is broken</option>
              <option value="improvement">Improvement — it works but could be better</option>
              <option value="urgent">Urgent — breaking the platform right now</option>
            </select></label>
          <label>Sprints <span class="hint">cycles it may take to get this right</span>
            <input name="sprints" type="number" min="1" max="10" value="1"></label>
        </div>
        <p id="triageBox" class="triage" hidden></p>
        <p id="selfErr" class="form-error" hidden></p>
        <button type="submit" class="primary">🔧 Put the team on it</button>
        <p class="hint">This runs <b>autonomously</b>: the team plans the fix, writes it,
        runs the tests and opens a PR without stopping to ask. Nothing reaches the
        running app until you deploy it above.</p>
      </form>
    </div>

    <div class="self-card" id="healCard">
      <h3>What the platform has done on its own</h3>
      <p class="hint">Small fixes are made, verified and shipped without asking you.
        They appear here so "you were not interrupted" never means "you cannot find
        out" — including the builds it tried and rejected.</p>
      <div id="healBody"><p class="hint">loading…</p></div>
    </div>

    <div class="self-card" id="envsCard">
      <h3>Environments</h3>
      <p class="hint">An environment is an <b>image</b>, not a folder. Build one from
        any source — including work you haven't committed — try it on its own
        namespace and database, then promote <i>that exact image</i> to production.
        Nothing is rebuilt on the way, so what you tested is what ships.</p>
      <div id="envsBody"><p class="hint">loading…</p></div>
    </div>

    <div class="self-card" id="sandboxCard">
      <h3>Try it before it goes live</h3>
      <p class="hint">A diff tells you the code is plausible. Only running it tells you
        the app still works. This boots the candidate build beside the live one — its
        own database, no credentials, agents simulated — so you can click through the
        fix before deploying it over the app you are using.</p>
      <div id="sandboxBody" class="sandbox-body"><p class="hint">loading…</p></div>
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

  renderSandbox();

  const form = $("#selfIssueForm");

  const refine = $("#refineBtn");
  if (refine) refine.addEventListener("click", async () => {
    const rough = $("#roughIssue").value.trim();
    const note = $("#refineNote");
    if (rough.length < 8) { note.textContent = "Say a little more about what's wrong."; return; }
    refine.disabled = true; refine.textContent = "drafting…";
    note.textContent = "Reading what you wrote and drafting a ticket…";
    try {
      const d = await api("/api/self/refine", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rough }) });
      form.querySelector("[name=title]").value = d.title || "";
      form.querySelector("[name=body]").value = d.body || "";
      if (d.severity) form.querySelector("[name=severity]").value = d.severity;
      // Say plainly when the draft is just the user's own words echoed back —
      // otherwise a silent fallback looks like the AI agreed it was already perfect.
      note.textContent = d.refined
        ? "Draft ready — edit anything that's wrong, then file it."
        : "No provider could draft this, so your own words were kept. Add detail by hand.";
      form.querySelector("[name=body]").focus();
    } catch (e) {
      note.textContent = `Could not draft it: ${e.message || e}. Write the ticket by hand.`;
    } finally {
      refine.disabled = false; refine.textContent = "✨ Draft the ticket";
    }
  });

  // Say what will happen BEFORE they file it. Learning afterwards that the
  // platform merged something you expected to review is the bad outcome here.
  let triageTimer = null;
  const previewTriage = () => {
    clearTimeout(triageTimer);
    triageTimer = setTimeout(async () => {
      const rough = (form.querySelector("[name=title]").value + " " +
                     form.querySelector("[name=body]").value).trim();
      const box = $("#triageBox");
      if (!box) return;
      if (rough.length < 10) { box.hidden = true; return; }
      try {
        const t = await api("/api/self/triage", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rough }) });
        const cls = t.tier === "routine" ? "ok" : t.tier === "substantial" ? "warn" : "bad";
        box.hidden = false;
        box.className = "triage " + cls;
        box.innerHTML = `<b>${escapeHtml(t.policy.label)}</b> — ${escapeHtml(t.policy.note)}
          ${(t.why || []).length ? `<span class="why">${escapeHtml(t.why.join("; "))}</span>` : ""}`;
      } catch { box.hidden = true; }
    }, 700);
  };
  form.querySelector("[name=title]").addEventListener("input", previewTriage);
  form.querySelector("[name=body]").addEventListener("input", previewTriage);

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(form);
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true; btn.textContent = "briefing the team…";
    try {
      const r = await api("/api/self/issue", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: f.get("title"), body: f.get("body"),
                               severity: f.get("severity"),
                               sprints: Number(f.get("sprints")) || 1 }) });
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
  { const ab = $("#aboutPage"); if (ab) ab.hidden = true; }
  { const scn = $("#scenes"); if (scn) scn.hidden = true; }
  { const lw = $("#lifeworld"); if (lw) lw.hidden = true; }
  $("#projectBar").hidden = true;
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
  const mode = (document.querySelector("input[name=tmode]:checked") || {}).value || "diverge";
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

// --- the table as a conversation, not a log --------------------------------
// Turns arrive from the poll in batches of whole 250-word essays, which is
// unreadable while it is happening. Each seat opens with a `GIST:` line, and the
// circle plays those back one speaker at a time so the round can be followed at
// a glance. The full text stays one click away.

let tableState = null;       // last payload from /api/tables/:id
let seenTurns = new Set();   // turn ids already played
let speechQueue = [];        // turns waiting to be played
let speechTimer = null;
let bubbles = {};            // seat id (or "mod") -> {text, phase, ok}
let speaking = null;

function stripMd(line) {
  return (line || "")
    .replace(/^[#>\s]*/, "")            // headings, quotes
    .replace(/^[-*+]\s+/, "")           // bullets
    .replace(/^\d+[.)]\s*/, "")         // "1." / "1)"
    .replace(/\*\*|__|`|\*/g, "")       // emphasis
    .trim();
}

function gistOf(text) {
  const t = (text || "").trim();
  const m = t.match(/^[ \t]*GIST:[ \t]*(.+)$/im);
  if (m) return stripMd(m[1]).slice(0, 160);
  // No GIST line — an older turn, or a model that ignored the rule. Taking the
  // first line gave bubbles reading "## Systems Architect — opening proposal",
  // which is a heading, not a point. Skip the scaffolding and find the first line
  // that is actually a sentence: long enough to say something, or ending in
  // sentence punctuation.
  for (const raw of t.split("\n")) {
    const line = stripMd(raw);
    if (!line) continue;
    const looksLikeHeading = line.length < 46 && !/[.!?:][)"']?$/.test(line);
    if (looksLikeHeading) continue;
    const stop = line.search(/[.!?](\s|$)/);
    const sentence = (stop > 0 ? line.slice(0, stop + 1) : line).trim();
    if (sentence.length >= 12) return sentence.slice(0, 160);
  }
  const any = t.split("\n").map(stripMd).find(Boolean) || "";
  return any.slice(0, 160) || "…";
}

/** A provider error in words a person can act on, not a stack of HTTP prose. */
function shortErr(text) {
  const t = (text || "").toLowerCase();
  if (t.includes("high demand") || t.includes("503")) return "the model was busy";
  if (t.includes("limit is 0") || t.includes("not entitled")) return "not on your plan";
  if (t.includes("429") || t.includes("quota") || t.includes("rate")) return "rate limited";
  if (t.includes("no credentials")) return "no key for this provider";
  return "a provider error";
}

function seatAngles(n) {
  return Array.from({ length: n }, (_, i) => (i / n) * 2 * Math.PI - Math.PI / 2);
}

function renderCircle(seatInfo, phase) {
  const el = $("#circle");
  if (!el) return;
  const n = seatInfo.length;
  const R = 38;
  const angs = seatAngles(n);
  el.innerHTML = seatInfo.map((s, i) => {
    const x = 50 + R * Math.cos(angs[i]), y = 50 + R * Math.sin(angs[i]);
    const isSpeaking = String(speaking) === String(s.id);
    const state = isSpeaking ? "speaking" : (s.done ? "done" : "");
    const b = bubbles[s.id];
    // Push the bubble outward from the centre so it never covers another seat.
    const side = Math.cos(angs[i]) >= 0 ? "right" : "left";
    const bubble = b ? `<div class="sn-bubble ${side} ${b.ok ? "" : "bad"}"
        data-full="${escapeHtml(s.id)}">${escapeHtml(b.text)}</div>` : "";
    return `<div class="seat-node ${state} prov-${s.provider}"
        style="left:${x}%; top:${y}%" data-seat="${s.id}" title="${escapeHtml(s.model)}">
      <div class="sn-dot">${escapeHtml((s.name || "?").slice(0, 1).toUpperCase())}</div>
      <div class="sn-name">${escapeHtml(s.name)}</div>
      <div class="sn-model">${escapeHtml(providerLabel(s.provider))}</div>
      ${s.skeptic ? `<span class="sn-badge" title="Holds the standing skeptic brief">skeptic</span>` : ""}
      ${bubble}
    </div>`;
  }).join("") + `
    <div class="mod-node ${phase === "synthesis" ? "speaking" : ""}">
      <div class="sn-name">Moderator</div>
      <div class="sn-model">${phase === "synthesis" ? "writing the blueprint…" : "listening"}</div>
    </div>`;
  // Clicking a bubble opens that seat's full turn in the transcript.
  el.querySelectorAll(".sn-bubble").forEach((b) =>
    b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const tr = $("#turnDetails");
      if (tr) { tr.open = true; tr.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
    }));
}

function seatInfoFrom(t) {
  const spoken = {};
  (t.turns || []).forEach((x) => { if (x.seat_id) spoken[x.seat_id] = x; });
  return (t.seats || []).map((s, i) => ({
    id: s.id, name: s.name, provider: s.provider, model: s.model,
    skeptic: i === (t.seats || []).length - 1,
    done: !!spoken[s.id],
  }));
}

function paintTable() {
  const t = tableState;
  if (!t) return;
  const phase = (t.turns || []).slice(-1)[0]?.phase || "propose";
  renderCircle(seatInfoFrom(t), t.status === "running" ? phase : "");
}

/** Play new turns one speaker at a time so it reads as a conversation. */
function playTurns(t) {
  const fresh = (t.turns || []).filter((x) => !seenTurns.has(x.id));
  fresh.forEach((x) => seenTurns.add(x.id));
  speechQueue.push(...fresh);
  if (speechTimer) return;
  const step = () => {
    const x = speechQueue.shift();
    if (!x) { speechTimer = null; speaking = null; paintTable(); return; }
    if (x.phase === "synthesis") { speaking = "mod"; } else {
      // A new round starts a clean slate — old gists belong to the last round.
      if (bubbles.__phase && bubbles.__phase !== x.phase) bubbles = {};
      bubbles.__phase = x.phase;
      // A seat that couldn't speak must SAY so. Rendering nothing left two of the
      // four seats permanently blank with no hint that anything had gone wrong.
      const text = x.ok ? gistOf(x.text) : `couldn't speak — ${shortErr(x.text)}`;
      bubbles[x.seat_id] = { text, phase: x.phase, ok: x.ok };
      speaking = x.seat_id;
    }
    paintTable();
    speechTimer = setTimeout(step, 1200);
  };
  speechTimer = setTimeout(step, 80);
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
        // Diverge, not debate, when the radio is somehow missing: the API defaults
        // to debate for compatibility with tables already recorded, so the UI has
        // to name the mode it wants every time or it silently buys 3× the calls.
        mode: (document.querySelector("input[name=tmode]:checked") || {}).value || "diverge",
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
    tableState = t;
    const lastPhase = (t.turns || []).slice(-1)[0]?.phase || "propose";
    playTurns(t);              // animates the circle; paintTable does the drawing
    paintTable();
    const spokenN = (t.turns || []).filter((x) => x.phase === lastPhase).length;
    const seatN = (t.seats || []).length;
    $("#phasePill").textContent = t.status === "done" ? "blueprint ready"
      : t.status === "failed" ? "failed"
      : `round ${({ propose: 1, critique: 2, revise: 3, synthesis: 4 })[lastPhase] || 1} · ${lastPhase}`;
    const prog = $("#phaseProgress");
    if (prog) {
      prog.hidden = t.status !== "running" || lastPhase === "synthesis";
      prog.textContent = `${Math.min(spokenN, seatN)} of ${seatN} have spoken`;
    }
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
  // Collapsed by design: the circle is how you follow the conversation, and this
  // is the record you open when a gist makes you want the argument behind it.
  const rows = (t.turns || []).filter((x) => x.phase !== "synthesis").map((x) => {
    const s = byId[x.seat_id];
    const who = s ? s.name : "Moderator";
    const body = (x.text || "").replace(/^[ \t]*GIST:[ \t]*.+\n?/im, "").trim();
    return `<div class="turn ${x.ok ? "" : "turn-bad"} phase-${x.phase}">
      <div class="turn-head"><b>${escapeHtml(who)}</b>
        <span class="pill">${escapeHtml(x.phase)}</span>
        ${s ? `<span class="hint">${escapeHtml(providerLabel(s.provider))} · ${escapeHtml(s.model)}</span>` : ""}</div>
      <div class="turn-gist">${escapeHtml(gistOf(x.text))}</div>
      <div class="turn-body">${escapeHtml(body || x.text)}</div>
    </div>`;
  }).join("");
  el.innerHTML = `<details id="turnDetails"><summary>Full transcript
      <span class="hint">${(t.turns || []).length} turns</span></summary>
    <div class="turn-list">${rows}</div></details>`;
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
          obj.options.map((o) => escapeHtml(optText(o))).join(" · ")}</div>` : ""}
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

// snake_case role names are long. Offer the browser a break point after each
// underscore so they wrap as WORDS rather than mid-syllable.
// An option may arrive as a plain string, or as {label, detail} from a model that
// answered structurally. Never let a raw object reach the DOM.
function optText(o) {
  if (o && typeof o === "object") {
    const label = o.label || o.option || o.title || "";
    const detail = o.detail || o.description || o.why || "";
    return label && detail ? `${label} — ${detail}` : (label || detail || JSON.stringify(o));
  }
  const s = String(o ?? "");
  // legacy rows stored Python reprs like "{'label': 'x', 'detail': 'y'}"
  const m = s.match(/^\{'label':\s*'([^']*)'/);
  return m ? m[1] : s;
}

function wrapRole(role) {
  return escapeHtml(String(role || "")).replace(/_/g, "_<wbr>");
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

// Two different questions, split onto two tabs.
//
// "Is anything holding my project up" and "is there anything I should know" were
// answered by one list, so a project running perfectly on schedule showed a
// 🚧 badge because nothing was running its tests. That is worth telling someone;
// it is not a blocker, and putting it under that heading made every genuine
// blocker easier to ignore.
const HOLDS_WORK_UP = new Set(["stopped", "pace", "waiting"]);
const isBlocker = (b) => HOLDS_WORK_UP.has(b.impact || (b.severity === "critical" ? "stopped" : "heads_up"));

// The label describes what a blocker DOES, not how urgent it is. Both used to come
// from severity, so a project running perfectly on schedule with no test command
// was told it was "slowing down". It was not — it was running blind, and saying
// the wrong one sends you looking for a performance problem that is not there.
// Older servers send no impact; fall back to the severity wording.
const IMPACT_LABEL = {
  stopped:  "Stopping the project",
  pace:     "Slowing it down",
  waiting:  "Waiting on you",
  setup:    "Not set up yet",
  evidence: "Running unverified",
  heads_up: "Worth knowing",
};
const labelFor = (b) => IMPACT_LABEL[b.impact]
  || (b.severity === "critical" ? "Stopping the project" : "Worth knowing");

function renderNoticeList(el, notices) {
  if (!notices.length) {
    el.innerHTML = `<div class="bl-head"><h2>📋 Notices</h2></div>
      <div class="empty">Nothing to mention. Anything that needs your attention but
      is not holding work up will appear here.</div>`;
    return;
  }
  el.innerHTML = `
    <div class="bl-head">
      <h2>📋 Notices</h2>
      <div class="bl-tally"><span class="pill quiet">${notices.length} worth knowing —
        none of it is blocking</span></div>
    </div>
    ${notices.map((b) => `
      <div class="bl-card notice">
        <div class="bl-top">
          <span class="pill quiet">${labelFor(b)}</span>
          ${b.since ? `<span class="bl-since">${ago(b.since)}</span>` : ""}
        </div>
        <h3>${escapeHtml(b.title)}</h3>
        <p class="bl-detail">${inlineCode(b.detail)}</p>
        ${b.fix ? `<p class="bl-fix"><b>What clears it:</b> ${inlineCode(b.fix)}</p>` : ""}
        <div class="bl-acts">
          ${b.task_id ? `<button data-blview="${b.task_id}">Open task #${b.task_seq}</button>` : ""}
          ${b.action === "settings" ? `<button data-blset="1" class="primary">Open settings</button>` : ""}
        </div>
      </div>`).join("")}`;
  el.querySelectorAll("[data-blview]").forEach((btn) =>
    btn.addEventListener("click", () => showTask(Number(btn.dataset.blview))));
  el.querySelectorAll("[data-blset]").forEach((btn) =>
    btn.addEventListener("click", () => $("#settingsBtn").click()));
}

async function renderBlockers() {
  const el = $("#blockers");
  const nel = $("#notices");
  if (!el || !currentProject) return;
  let data;
  try { data = await api(`/api/projects/${currentProject}/blockers`); } catch { return; }
  const all = data.blockers || [];
  const items = all.filter(isBlocker);
  const notices = all.filter((b) => !isBlocker(b));

  // Badges update even when neither tab is open. The blocker badge is loud
  // because it means work is affected; the notices badge is quiet because it
  // does not, and a badge that cries wolf stops being read.
  const badge = $("#blockerCount");
  if (badge) {
    badge.hidden = items.length === 0;
    badge.textContent = String(items.length);
    badge.className = items.some((b) => b.severity === "critical") ? "crit" : "warn";
  }
  const nbadge = $("#noticeCount");
  if (nbadge) {
    nbadge.hidden = notices.length === 0;
    nbadge.textContent = String(notices.length);
    nbadge.className = "quiet";
  }

  if (nel && !nel.hidden) renderNoticeList(nel, notices);
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
  const slowing = items.filter((b) => b.impact === "pace").length;
  el.innerHTML = `
    <div class="bl-head">
      <h2>🚧 Blockers</h2>
      <div class="bl-tally">${data.critical
        ? `<span class="pill crit">${data.critical} stopping work</span>` : ""}
        ${slowing ? `<span class="pill warn">${slowing} slowing it down</span>` : ""}
        ${!data.critical && !slowing && data.warning
          ? `<span class="pill warn">${data.warning} to look at — nothing is blocked</span>` : ""}</div>
    </div>
    ${items.map((b) => `
      <div class="bl-card ${b.severity}">
        <div class="bl-top">
          <span class="pill ${b.severity === "critical" ? "crit" : "warn"}">${labelFor(b)}</span>
          ${b.since ? `<span class="bl-since">${ago(b.since)}</span>` : ""}
        </div>
        <h3>${escapeHtml(b.title)}</h3>
        <p class="bl-detail">${inlineCode(b.detail)}</p>
        ${b.fix ? `<p class="bl-fix"><b>What clears it:</b> ${inlineCode(b.fix)}</p>` : ""}
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
  // The roster, which is not part of the project payload. Without it the command
  // view could only draw people who happened to have a task open, so six of the
  // eight teammates on a real run were invisible and the header counted TASKS
  // and called them team members.
  try {
    const t = await api(`/api/projects/${currentProject}/team`);
    lastTeam = t.team || [];
  } catch { /* an older server has no roster; the view falls back to tasks */ }
  // Self-repair is its own page now, reached from the landing tile — it is not a
  // tab on an ordinary project, which is what let it linger after switching.
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
  // Which delivery cycle we're in. Only shown when there's more than one, so a
  // one-shot project doesn't carry sprint vocabulary it never uses.
  const sb = $("#sprintBadge");
  if (sb) {
    const total = p.sprints ?? 1;
    sb.hidden = total <= 1;
    sb.textContent = `sprint ${p.sprint ?? 1}/${total}`;
    sb.title = `The manager plans and ships ${total} cycles on its own, deciding each ` +
      "sprint's scope itself. It only stops for you if it's genuinely blocked.";
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

// Backticks in server-written copy were reaching the page as literal characters,
// so advice read "a `test` script in package.json". Escape FIRST, then promote
// the spans: doing it the other way round would let a backticked fragment carry
// markup through the escape.
function inlineCode(text) {
  return escapeHtml(text).replace(/`([^`]+)`/g, "<code>$1</code>");
}

function renderEvent(e) {
  if (e.project_id !== currentProject) return;
  // Legacy 'answer' events are the manager echoing what the boss had just said,
  // so every boss message appeared twice — once as typed, once prefixed "The boss
  // replied: ". The emit is gone; this hides the ones already in the history.
  if (e.kind === "answer") return;
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
  // Outcomes, as distinct from narration. Everything in the feed used to carry
  // the same weight, so "merged PR #12" and a worker musing about a file looked
  // equally important — which means neither did.
  if (["pr_merged", "task_accepted", "project_finished", "winner_picked",
       "verified"].includes(e.kind)) cls += " outcome good";
  if (["changes_requested", "task_failed", "worker_stalled"].includes(e.kind))
    cls += " outcome bad";
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
    pendingQ = { id: q.id, text: q.question, topic: q.topic || "decision",
                 options: q.options || [] };
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

/** The delivery cycles, current one open and finished ones archived.
 *
 * Tasks accumulate across sprints, so by sprint 4 the board is a wall of work
 * mostly finished three cycles ago. This separates "what the team is doing now"
 * from "what it has already shipped", without hiding the latter. */
function renderSprints(p) {
  const total = p.sprints ?? 1;
  if (total <= 1) return "";
  const cur = p.sprint ?? 1;
  const bySprint = {};
  for (const t of p.tasks || []) (bySprint[t.sprint || 1] ||= []).push(t);

  const line = (n) => {
    const ts = bySprint[n] || [];
    const done = ts.filter((t) => t.status === "done").length;
    const failed = ts.filter((t) => t.status === "failed").length;
    const live = ts.filter((t) => ["running", "queued"].includes(t.status)).length;
    const state = n < cur ? "shipped" : n === cur ? "current" : "upcoming";
    const titles = ts.map((t) =>
      `<li class="sp-task ${t.status}"><b>#${t.seq}</b> ${escapeHtml(t.title)}
         <span class="hint">${escapeHtml(t.role)} · ${t.status}</span></li>`).join("");
    const body = ts.length
      ? `<ul class="sp-tasks">${titles}</ul>`
      : `<p class="hint">Not planned yet — the manager decides this sprint's scope
           when the previous one ships.</p>`;
    // Only the current sprint is open by default; finished ones are archive.
    return `<details class="sp ${state}" ${state === "current" ? "open" : ""}>
      <summary>
        <span class="sp-n">Sprint ${n}</span>
        <span class="sp-state">${state === "current" ? "in progress"
          : state === "shipped" ? "shipped" : "not started"}</span>
        <span class="sp-counts">${ts.length ? `${done}/${ts.length} done` : "—"}${
          live ? ` · ${live} running` : ""}${failed ? ` · ${failed} failed` : ""}</span>
      </summary>${body}</details>`;
  };

  return `<div class="sprints-card">
    <div class="bl">🗓 Sprints <span class="hint">${cur} of ${total}</span></div>
    <p class="hint sp-lede">The manager plans each cycle itself and rolls straight
      into the next — it only stops for you if it is genuinely blocked.</p>
    ${Array.from({ length: total }, (_, i) => line(i + 1)).join("")}
  </div>`;
}

// What to show between "you pressed create" and "there are tasks on screen".
//
// This was one centred grey sentence — "Assembling the team…" — with no motion,
// no detail and no end. Planning legitimately takes half a minute or more, so the
// most common first impression of the product was a screen that looked frozen.
//
// Three things fix that, and none of them is a spinner. The roster the boss just
// chose is already known, so the shape of the team can be drawn before anyone is
// hired. The manager's current thought is already streaming in, so the screen can
// say what is actually happening rather than a canned phrase. And if it really
// has been too long, saying so — with the thing to press — beats leaving someone
// to wonder.
function assemblingHtml(p, roster, thought) {
  const waited = p.created_at ? (Date.now() / 1000 - p.created_at) : 0;
  const stalled = waited > 210;

  const ghosts = (roster || []).flatMap((m) =>
    Array.from({ length: Math.max(1, Number(m.count) || 1) }, () => m.role))
    .slice(0, 8)
    .map((role) => `<div class="ghost-agent">
        <div class="top"><span class="role">${wrapRole(role)}</span><span class="st">hiring</span></div>
        <div class="gline w70"></div><div class="gline w45"></div>
      </div>`).join("");

  const said = (thought || "").trim();
  return `<div class="assembling ${stalled ? "stalled" : ""}">
    <div class="assembling-head">
      <span class="pulse" aria-hidden="true"></span>
      <span>${stalled
        ? "The manager has not produced a plan yet"
        : "Planning the work and hiring the team"}</span>
    </div>
    <p class="assembling-say">${said
      ? escapeHtml(trim(said, 220))
      : "Reading your brief, working out the pieces, and deciding who is needed. "
        + "This usually takes under a minute."}</p>
    ${ghosts ? `<div class="ghost-team">${ghosts}</div>` : ""}
    ${stalled ? `<p class="assembling-stuck">It has been
      ${Math.round(waited / 60)} minutes. If nothing has moved, use
      <b>↻ Restart manager</b> in the bar above — it picks up where this left off
      rather than starting the project again.</p>` : ""}
  </div>`;
}

// Manager models offered in the picker. A short list on purpose: this is the
// model that plans and reviews, so the meaningful choice is how much judgement
// you want to pay for, not which of a dozen ids you prefer.
let lastTeam = [];

// A model the picker does not know about is still the model that is running, and
// showing "server default" for it was a lie the card told confidently. Unknown
// ids are displayed as themselves and offered as an option, so changing something
// else never silently reassigns the manager.
function modelName(id) {
  if (!id) return "server default";
  const known = MANAGER_MODELS.find((m) => m.id === id);
  return known ? known.label.split(" — ")[0] : id;
}

function managerOptions(current) {
  const opts = MANAGER_MODELS.slice();
  if (current && !opts.some((m) => m.id === current)) {
    opts.push({ id: current, label: `${current} (set outside this list)` });
  }
  return opts.map((m) => `<option value="${escapeHtml(m.id)}"${
    m.id === (current || "") ? " selected" : ""}>${escapeHtml(m.label)}</option>`).join("");
}

const MANAGER_MODELS = [
  { id: "", label: "server default" },
  { id: "claude-haiku-4-5", label: "Haiku — fastest, cheapest" },
  { id: "claude-sonnet-5", label: "Sonnet — balanced" },
  { id: "claude-opus-4-8", label: "Opus — most careful" },
];

// Everyone who has nothing on right now.
//
// The view drew task cards grouped by status, so a teammate only appeared once
// they had work. On a real run eight people were hired and six were invisible —
// and the header counted TASKS and called them "on the team", so it said 2.
// Idle is a real and useful state: it is who could take the next thing.
function benchHtml(tasks) {
  if (!lastTeam.length) return "";
  const working = new Set(tasks
    .filter((t) => ["running", "queued", "pushed", "review"].includes(t.status))
    .map((t) => t.agent_id).filter(Boolean));
  const idle = lastTeam.filter((a) => !working.has(a.id));
  if (!idle.length) return "";
  return `<div class="bench">
    <div class="bench-head">Free right now · ${idle.length}
      <span class="bench-note">hired and waiting for work they can start</span></div>
    <div class="bench-list">${idle.map((a) => `
      <span class="bench-person" title="${escapeHtml(a.persona || "no particular brief")}">
        <b>${escapeHtml(a.name)}</b> ${escapeHtml(a.role)}${
          a.tasks_done ? ` · ${a.tasks_done} done` : ""}</span>`).join("")}</div>
  </div>`;
}

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

  // How the moment is framed depends on what sort of moment it is. An interview
  // before any work starts is not a problem to be unblocked, and calling it "your
  // manager needs a decision" made the first thing a new project did look like
  // something had already gone wrong.
  const ASK_FRAME = {
    interview: { icon: "💬", head: "Before your manager plans this",
                 note: "Answer what you care about — it will decide the rest and tell you what it assumed." },
    sprint_review: { icon: "🔄", head: "Sprint finished — anything to change?",
                     note: "The cheapest moment to redirect: nothing for the next round is built yet." },
    decision: { icon: "👔", head: "Your manager needs a decision", note: "" },
  };
  const frame = ASK_FRAME[pendingQ && pendingQ.topic] || ASK_FRAME.decision;
  const askHtml = pendingQ ? `
    <div class="ask-card ${pendingQ.topic || "decision"}">
      <div class="bl">${frame.icon} ${frame.head}</div>
      <div class="qtext">${escapeHtml(pendingQ.text)}</div>
      ${frame.note ? `<div class="qnote">${frame.note}</div>` : ""}
      <div class="qbtns">
        ${(pendingQ.options || []).map((o, i) =>
          `<button data-qopt="${i}">${escapeHtml(optText(o))}</button>`).join("")}
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
        <span class="role" title="${escapeHtml(t.role)}">${wrapRole(t.role)}</span>
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
      if (!names.length) return "";
      // Three long snake_case names in a 268px card is a wall of text. Show two,
      // count the rest, and keep the full list in the tooltip.
      const shown = names.slice(0, 2).join(", ");
      const extra = names.length - 2;
      return { text: extra > 0 ? `waiting for ${shown} +${extra} more` : `waiting for ${shown}`,
               full: "waiting for " + names.join(", ") };
    };
    return `<div class="group">
      <div class="group-head"><span class="glabel">${label}</span>
        <span class="gcount">${inGroup.length}</span>
        ${note ? `<span class="gnote">${note}</span>` : ""}</div>
      <div class="agents">${inGroup.map((t) => {
        const bn = key === "blocked" ? blockedNote(t) : "";
        return card(t).replace('<div class="deps">',
          bn ? `<div class="blocked-note" title="${escapeHtml(bn.full)}">⏳ ${
            escapeHtml(bn.text)}</div><div class="deps">` : '<div class="deps">');
      }).join("")}</div>
    </div>`;
  }).join("");

  el.innerHTML = `
    <div class="chain">
      ${attnHtml}
      ${askHtml}
      ${renderSprints(p)}
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
          <!-- The model is the headline again. Replacing it with a bare dropdown
               reading "server default" lost the one fact the card existed to show —
               and worse, said the wrong thing: a manager running on a model the
               list did not contain fell back to displaying the first option. -->
          <div class="name" id="mgrModelName" title="Click to change which model runs the manager">
            ${escapeHtml(modelName(p.manager_model))}<span class="mgr-edit">change</span>
          </div>
          <select class="mgr-model" id="mgrModelSel" hidden
                  title="Applies when the manager next starts.">
            ${managerOptions(p.manager_model)}
          </select>
          <div class="sub">
            <button class="mode-toggle ${p.autonomy === "autonomous" ? "auton" : ""}"
              id="autonomyBtn"
              title="${p.autonomy === "autonomous"
                ? "Full autonomy: decides everything itself. Click to start checking with you."
                : "Checks with you on the important calls. Click to give it full autonomy."}">
              ${p.autonomy === "autonomous" ? "⚡ full autonomy" : "🧑‍💼 checks with you"}</button>
            · ${(lastTeam.length || tasks.length)} on the team</div>
        </div>
        ${bubbleHtml("Manager says", managerThought)}
        ${bubbleHtml("thinking", managerThinking, "thinking")}
      </div>
      <div class="fan"></div>
      <div class="section-label">The team</div>
      ${agents || assemblingHtml(p, roster, managerThought || managerThinking)}
      ${benchHtml(tasks)}
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
  const mname = $("#mgrModelName");
  const msel = $("#mgrModelSel");
  if (mname && msel) mname.addEventListener("click", (ev) => {
    ev.stopPropagation();
    mname.hidden = true; msel.hidden = false; msel.focus();
  });

  const ms = $("#mgrModelSel");
  if (ms) ms.addEventListener("change", async (ev) => {
    ev.stopPropagation();
    const model = ms.value;
    ms.disabled = true;
    try {
      const r = await api(`/api/projects/${currentProject}/manager-model`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model }),
      });
      // Say what actually happened. A model is bound when a session starts, so a
      // running manager keeps the old one until it restarts — reporting success
      // without that would have someone waiting for a change that has not
      // happened and cannot until they act.
      toast(r.restart_needed
        ? `Manager set to ${model || "the server default"}. The one running now keeps its `
          + `current model — use ↻ Restart manager to switch it over.`
        : `Manager set to ${model || "the server default"}.`);
    } catch (e) {
      toast(`Could not change it: ${e.message}`);
      ms.value = (lastProject && lastProject.manager_model) || "";
    } finally {
      ms.disabled = false;
    }
  });

  const ab = $("#autonomyBtn");
  if (ab) ab.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    const now = lastProject && lastProject.autonomy === "autonomous";
    const next = now ? "supervised" : "autonomous";
    const msg = next === "autonomous"
      ? "Give the manager FULL AUTONOMY?\n\nIt will stop asking you to approve things — "
        + "including merges and finishing — and decide for itself. Any question it is "
        + "currently waiting on gets answered by its own judgement.\n\n"
        + "You can switch back at any time."
      : "Put the manager back under SUPERVISION?\n\nIt will check with you before "
        + "merging substantial work, before finishing, and on real product decisions.";
    if (!confirm(msg)) return;
    ab.disabled = true;
    try {
      const r = await api(`/api/projects/${currentProject}/autonomy`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ autonomy: next }),
      });
      toast(r.autonomy === "autonomous"
        ? "Full autonomy — it will stop asking and decide for itself."
        : "Supervised — it will check with you on the important calls.");
      refreshBoard();
    } catch (e) { alert(e.message); ab.disabled = false; }
  });

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
      answerQuestion(pendingQ.id, optText(pendingQ.options[Number(b.dataset.qopt)]));
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
$("#closeFileBtn").addEventListener("click", () => $("#fileDialog").close());
$("#closeTaskBtn").addEventListener("click", () => $("#taskDialog").close());
$("#taskDialog").addEventListener("click", (ev) => {
  if (ev.target === $("#taskDialog")) $("#taskDialog").close();
});

// --- ARTIFACTS TAB: the deliverable, documented ------------------------------
let artifactsSig = "";      // only repaint when the content actually changed
const FILE_ICON = { doc: "📄", code: "⌨", test: "✓", image: "🖼" };

async function loadProjectFiles() {
  const box = $("#filesPane");
  if (!box) return;
  let d;
  try { d = await api(`/api/projects/${currentProject}/files`); }
  catch (e) { box.innerHTML = `<p class="dim">${escapeHtml(e.message || e)}</p>`; return; }
  if (!(d.files || []).length) {
    box.innerHTML = `<p class="dim">${escapeHtml(d.reason || "Nothing has been merged yet.")}</p>`;
    return;
  }
  // Grouped by what a reader would call them, not by directory: "the docs" and
  // "the code" are different kinds of thing even though git treats them alike.
  const order = ["doc", "code", "test", "image"];
  const label = { doc: "Documents", code: "Code", test: "Tests", image: "Images" };
  const groups = {};
  d.files.forEach((f) => (groups[f.kind] ||= []).push(f));
  box.innerHTML = order.filter((k) => groups[k]).map((k) => `
    <div class="fgroup"><h4>${label[k]} <span class="hint">${groups[k].length}</span></h4>
      <ul class="flist">${groups[k].map((f) => `
        <li><button class="fopen" data-path="${escapeHtml(f.path)}">
          ${FILE_ICON[k] || "•"} ${escapeHtml(f.path)}</button>
          <span class="hint">${f.size > 1024 ? Math.round(f.size/1024)+" KB" : f.size+" B"}</span></li>`).join("")}
      </ul></div>`).join("");
  box.querySelectorAll(".fopen").forEach((b) =>
    b.addEventListener("click", () => openProjectFile(b.dataset.path)));
}

// >>> markdown renderer -------------------------------------------------------
// Hand-rolled, because the pod has neither npm nor guaranteed egress, and because
// every byte below was written by an AI agent working in a repository nobody on
// this side controls. Treat the document as hostile input aimed at the operator's
// browser, not as content: it is the one place where a repo the team cloned gets
// to put characters in front of a session that can start deployments.
//
// One rule keeps this honest, and it is mechanical rather than clever: markup is
// only ever produced by the code in this section. Every fragment of the document
// passes through escapeHtml before it is concatenated, and every URL passes
// through mdSafeUrl before it becomes an href. There is no path where document
// text reaches innerHTML unescaped, so raw <script>, <img onerror=…>, stray
// quotes closing an attribute and so on are all the same, already-solved case.

const MD_MAX_DEPTH = 8;   // "> > > > …" nested 20k deep is a stack overflow otherwise

// An allowlist, not a "block javascript:" test, because the blocklist version
// keeps losing: vbscript:, data:text/html;base64,…, and the endless spellings a
// browser forgives while a regex does not. Anything not plainly a document link
// is refused.
const MD_SAFE_SCHEME = /^(?:https?:|mailto:)/i;

function mdSafeUrl(raw) {
  // Browsers skip leading and embedded whitespace and C0 controls while parsing a
  // scheme, so "java\nscript:alert(1)" runs. Strip those before deciding, never
  // after — deciding on the pretty version and emitting the raw one is the bug.
  const u = String(raw == null ? "" : raw).replace(/[\u0000-\u0020\u007f]/g, "");
  if (MD_SAFE_SCHEME.test(u)) return u;
  if (/^\/\//.test(u)) return "";              // protocol-relative: inherits ours, goes anywhere
  if (/^[^/?#]*:/.test(u)) return "";          // a colon before the first slash is a scheme we did not allow
  return u;                                    // fragment, absolute or repo-relative path
}

function mdLink(url, label) {
  const href = mdSafeUrl(url);
  // A refused link is shown as refused rather than quietly turned into text: the
  // operator is reviewing agent output, and "this document tried to link to
  // javascript:" is exactly the thing they want to notice.
  if (!href) return `<span class="md-blocked" title="link refused">${label} [blocked link]</span>`;
  // noopener because target=_blank hands window.opener to whatever we just opened;
  // nofollow because we are rendering somebody else's repo.
  return `<a href="${href}" target="_blank" rel="noopener noreferrer nofollow">${label}</a>`;
}

function mdInline(s) {
  // Code spans come out first and are never looked at again: ** inside backticks
  // is a literal, and a span that has already been escaped cannot later be talked
  // into being markup by a rule that runs after it.
  return String(s == null ? "" : s).split(/(`+[^`]*?`+)/).map((part, i) => {
    if (i % 2) return `<code>${escapeHtml(part.replace(/^`+|`+$/g, ""))}</code>`;
    let t = escapeHtml(part);
    // Images are rendered as links on purpose. Fetching a remote asset named by an
    // untrusted document leaks the operator's address to whoever wrote the repo and
    // makes the dashboard issue requests on their behalf; the caption and the URL
    // carry the same information without doing that.
    t = t.replace(/!\[([^\]]*)\]\(\s*([^()\s]*)[^)]*\)/g,
                  (m, alt, url) => mdLink(url, `🖼 ${alt || url}`));
    // The URL stops at the first space or bracket, so a title string — the classic
    // place to smuggle attributes — is dropped rather than reproduced.
    t = t.replace(/\[([^\]]*)\]\(\s*([^()\s]*)[^)]*\)/g,
                  (m, txt, url) => mdLink(url, txt || url));
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    t = t.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    t = t.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    t = t.replace(/(^|[^\w`])_([^_\n]+)_(?![\w])/g, "$1<em>$2</em>");
    t = t.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    return t;
  }).join("");
}

// Info strings that are not a bare word become no class at all: the fence language
// is attacker-chosen and would otherwise close the class attribute and open an
// event handler.
// Null-prototype because the key is the document's to choose: a plain object would
// answer ```constructor and ```__proto__ with something truthy from the prototype.
const MD_DIAGRAM = Object.assign(Object.create(null),
  { mermaid: "mermaid", plantuml: "PlantUML", dot: "Graphviz", graphviz: "Graphviz" });

function mdCodeBlock(lang, body) {
  const tag = /^[\w+-]{1,20}$/.test(lang) ? lang.toLowerCase() : "";
  const pre = `<pre class="md-code${tag ? " lang-" + tag : ""}"><code>${escapeHtml(body)}</code></pre>`;
  const diagram = MD_DIAGRAM[tag];
  if (!diagram) return pre;
  // No diagram library is shipped and none is coming — see the top of this section.
  // Drawing nothing while a heading promises a diagram reads as a broken page; the
  // source with a label says what actually happened.
  return `<figure class="md-figure"><figcaption>${escapeHtml(diagram)} diagram — `
       + `source shown, not drawn</figcaption>${pre}</figure>`;
}

function mdCells(row) {
  return row.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
}

function renderMarkdown(src, depth) {
  depth = depth || 0;
  const text = String(src == null ? "" : src).replace(/\r\n?/g, "\n");
  // Past the nesting ceiling we stop parsing and show what is left verbatim. Wrong
  // shape beats a hung tab.
  if (depth > MD_MAX_DEPTH) return `<pre class="md-code"><code>${escapeHtml(text)}</code></pre>`;
  const lines = text.split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    const fence = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (fence) {
      // A ~~~ block ends at ~~~ only: a ``` line inside it is content, which is how
      // a document showing markdown examples stays readable instead of exploding.
      const close = fence[1][0] === "~" ? /^ {0,3}~{3,}\s*$/ : /^ {0,3}`{3,}\s*$/;
      const body = [];
      i++;
      while (i < lines.length && !close.test(lines[i])) body.push(lines[i++]);
      i++;   // step over the closing fence; an unterminated block simply runs to EOF
      out.push(mdCodeBlock(fence[2].trim().split(/\s+/)[0] || "", body.join("\n")));
      continue;
    }

    if (!line.trim()) { i++; continue; }

    const h = /^ {0,3}(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const n = h[1].length;
      out.push(`<h${n}>${mdInline(h[2].replace(/\s+#+\s*$/, ""))}</h${n}>`);
      i++; continue;
    }

    if (/^ {0,3}([-*_])\s*(?:\1\s*){2,}$/.test(line)) { out.push("<hr>"); i++; continue; }

    if (/^ {0,3}>/.test(line)) {
      const quoted = [];
      while (i < lines.length && (/^ {0,3}>/.test(lines[i]) || (quoted.length && lines[i].trim())))
        quoted.push(lines[i++].replace(/^ {0,3}> ?/, ""));
      out.push(`<blockquote>${renderMarkdown(quoted.join("\n"), depth + 1)}</blockquote>`);
      continue;
    }

    // A table is only a table if the row under the header is the separator; a line
    // of prose containing a pipe is prose.
    if (line.includes("|") && i + 1 < lines.length
        && /^[\s|:-]+$/.test(lines[i + 1]) && /-/.test(lines[i + 1]) && lines[i + 1].includes("|")) {
      const head = mdCells(line);
      const align = mdCells(lines[i + 1]).map((c) =>
        /^:-+:$/.test(c) ? "center" : /-+:$/.test(c) ? "right" : "");
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(mdCells(lines[i++]));
      const cell = (t, j, tag) => {
        const a = align[j] ? ` class="md-${align[j]}"` : "";
        return `<${tag}${a}>${mdInline(t)}</${tag}>`;
      };
      out.push(`<table class="md-table"><thead><tr>${head.map((c, j) => cell(c, j, "th")).join("")}`
        + `</tr></thead><tbody>${rows.map((r) =>
            `<tr>${r.map((c, j) => cell(c, j, "td")).join("")}</tr>`).join("")}</tbody></table>`);
      continue;
    }

    const item = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/.exec(line);
    if (item) {
      const indent = item[1].length;
      const ordered = /\d/.test(item[2]);
      const items = [];
      while (i < lines.length) {
        const m = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/.exec(lines[i]);
        if (m && m[1].length <= indent && /\d/.test(m[2]) === ordered) {
          items.push([m[3]]); i++;
        } else if (items.length && lines[i].trim() && /^\s{2,}/.test(lines[i])) {
          items[items.length - 1].push(lines[i].slice(indent + 2)); i++;   // nested block, dedented
        } else break;
      }
      const body = items.map((parts) => parts.length === 1
        ? mdInline(parts[0])
        : renderMarkdown(parts.join("\n"), depth + 1));
      out.push(`<${ordered ? "ol" : "ul"}>${body.map((b) => `<li>${b}</li>`).join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }

    const para = [];
    while (i < lines.length && lines[i].trim() && !/^ {0,3}(#{1,6}\s|>|```|~~~)/.test(lines[i])
           && !/^(\s*)([-*+]|\d{1,9}[.)])\s+/.test(lines[i])) para.push(lines[i++]);
    // A paragraph reflows: hard-wrapped source must not become a wall of forced
    // breaks. Only markdown's own line break — two trailing spaces — makes a <br>.
    out.push("<p>" + para.map((l, n) =>
      (n ? (/ {2,}$/.test(para[n - 1]) ? "<br>\n" : "\n") : "") + mdInline(l.replace(/\s+$/, ""))
    ).join("") + "</p>");
  }
  return out.join("\n");
}
// <<< markdown renderer -------------------------------------------------------

// Rendered as prose, as code, or as markdown — decided by extension, because the
// alternative (sniffing the content) means a .py file whose docstring starts with
// "# " gets reflowed into a heading.
const MD_EXT = /\.(md|markdown)$/i;
const DOC_EXT = /\.(txt|rst|text)$/i;

async function openProjectFile(path) {
  const dlg = $("#fileDialog");
  const body = $("#fileBody");
  $("#fileTitle").textContent = path;
  body.className = "file-code";
  body.textContent = "loading…";
  dlg.showModal();
  try {
    const d = await api(`/api/projects/${currentProject}/file?path=${encodeURIComponent(path)}`);
    if (MD_EXT.test(path)) {
      // The only innerHTML in this file that touches repository content, and the
      // only reason it is allowed: renderMarkdown escapes every fragment it did
      // not write itself. Do not hand it anything else.
      body.className = "file-md";
      body.innerHTML = renderMarkdown(d.text);
    } else {
      // Prose reflows; everything else keeps its whitespace, because reflowing code
      // is how you make it unreadable.
      body.className = DOC_EXT.test(path) ? "file-doc" : "file-code";
      body.textContent = d.text;
    }
  } catch (e) {
    body.className = "file-doc";
    body.textContent = String(e.message || e);
  }
}

// Per-teammate output. The endpoint existed from the day teammates became real
// rows and nothing ever called it, so auditing a 17-task run meant reading pull
// request titles and guessing who wrote them.
async function renderByAgent() {
  const pane = $("#byAgentPane");
  if (!pane) return;
  let d;
  try { d = await api(`/api/projects/${currentProject}/by-agent`); }
  catch (e) { pane.innerHTML = `<p class="dim">could not load: ${escapeHtml(e.message)}</p>`; return; }
  const groups = d.agents || d.groups || [];
  if (!groups.length) {
    pane.innerHTML = `<p class="dim">Nothing attributed yet.</p>`;
    return;
  }
  pane.innerHTML = groups.map((g) => {
    const items = g.artifacts || g.work || g.tasks || [];
    // Rework is the number worth surfacing per person: it says who kept being
    // handed work they could not finish first time, which is a briefing problem
    // invisible in any project-wide average.
    const redone = items.filter((t) => (t.attempts || 1) > 1).length;
    return `<div class="agent-group">
      <div class="agent-group-head">
        <b>${escapeHtml(g.name || g.role || "unattributed")}</b>
        <span class="dim">${escapeHtml(g.role || "")}</span>
        <span class="tag">${items.length} delivered</span>
        ${redone ? `<span class="tag warn-t">${redone} needed more than one attempt</span>` : ""}
      </div>
      <ul class="agent-work">${items.map((t) => `
        <li>${escapeHtml(t.title || "(untitled)")}
          ${t.attempts > 1 ? `<span class="dim">· ${t.attempts} attempts</span>` : ""}
          ${t.pr_number ? `<span class="dim">· PR #${t.pr_number}</span>` : ""}
        </li>`).join("")}</ul>
    </div>`;
  }).join("");
}

// A preview served as an in-server subprocess answers on http://localhost:PORT —
// reachable only from the machine the server runs on. On a hosted instance that URL
// is a dead link in your browser, so we must never hand it over as if it worked:
// clicking it just opened localhost and nothing happened. Say where it actually
// lives instead, and point at the deploy that would give a public URL.
function previewReachable(url) {
  if (!url) return false;
  const isLocal = /^https?:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/i.test(url);
  const hereIsLocal = ["localhost", "127.0.0.1"].includes(location.hostname);
  return !isLocal || hereIsLocal;
}
function previewButton(url, label) {
  if (previewReachable(url)) {
    const sep = url.includes("?") ? "&" : "?";
    return `<a class="demo-btn" href="${escapeHtml(url)}${sep}t=${Date.now()}"
      target="_blank" rel="noopener">${label}</a>`;
  }
  return `<span class="hint preview-note">This build is running — but only inside the server,
    so there is no public link to open it with yet.</span>`;
}

async function renderArtifacts(force) {
  const el = $("#artifacts");
  if (!currentProject || el.hidden) return;
  if (force || !el.innerHTML) el.innerHTML = `<div class="pane"><p class="dim">Loading…</p></div>
    <div class="pane" id="byAgentCard">
      <h3>Who produced what</h3>
      <p class="hint">Every teammate's output, and how often their work came back.
        An audit reads by person; the pull request list reads by accident.</p>
      <div id="byAgentPane"><p class="dim">loading…</p></div>
    </div>
    <div class="pane" id="filesCard">
      <h3>Files</h3>
      <p class="hint">Everything the team merged, as things you can open — not a list
        of the pull requests that produced them.</p>
      <div id="filesPane"><p class="dim">loading…</p></div>
    </div>`;
  let a;
  try { a = await api(`/api/projects/${currentProject}/artifacts`); }
  catch (e) { el.innerHTML = `<div class="pane"><p class="dim">${escapeHtml(e.message)}</p></div>`; return; }
  const sig = JSON.stringify(a);
  if (!force && sig === artifactsSig) return;
  artifactsSig = sig;

  // Two objects, because that is what a person came here for: the thing you can
  // RUN, and the things you can READ. The old page listed pull requests and task
  // rows — a record of activity, not of output.
  const dep = a.deployment || {};
  const live = dep.url || a.preview_url;
  const demo = live
    ? `${previewButton(live, "▶ Open it")}
       <button id="buildPreviewBtn">↻ Rebuild</button>
       <span class="hint">${escapeHtml(a.preview_synced || "built earlier")}</span>`
    : `<button id="buildPreviewBtn" class="primary">▶ Build &amp; run it</button>
       <span class="hint">clones the repo, installs it, and serves it here</span>`;

  // Files, loaded separately so a large repo never delays the deployable object.
  loadProjectFiles();
  renderByAgent();

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
        ${previewButton(d.live.url, "▶ Open the running app")}
        <button id="redeployBtn">↻ Rebuild &amp; restart</button>
        <button id="stopDeployBtn" class="danger">■ Stop</button>
      </div>
      <p class="hint">${previewReachable(d.live.url) ? escapeHtml(d.live.url) + " · " : ""}${escapeHtml(d.live.kind)} ·
         up ${Math.floor(d.live.uptime / 60)}m${d.live.uptime % 60}s</p>
      ${d.log ? `<details><summary>Build &amp; runtime log</summary><pre class="deploy-log">${
        escapeHtml(d.log)}</pre></details>` : ""}`;
  } else {
    const runnable = spec.kind && !["static", "unknown", "node-static"].includes(spec.kind);
    // A static site has nothing to run, and that is not a problem to report — it
    // is a different button. Offering a greyed-out Deploy next to "use the static
    // preview instead" makes a project that works perfectly look broken, and
    // leaves you to find the preview yourself.
    const isStatic = spec.kind === "static" || spec.kind === "node-static";
    box.innerHTML = isStatic ? `
      <p class="detected"><b>Detected:</b> a static site — HTML, CSS and JavaScript
        with no server behind it. Nothing needs building; it just opens.</p>
      <button id="openStaticBtn" class="primary">▶ Open it</button>
      <span class="hint">Serves the merged default branch straight from your repository.</span>
      ${d.log ? `<details><summary>Last build log</summary><pre class="deploy-log">${
        escapeHtml(d.log)}</pre></details>` : ""}` : `
      ${spec.kind ? `<p class="detected"><b>Detected:</b> ${escapeHtml(spec.kind)} —
         ${escapeHtml(spec.why || "")}</p>` : ""}
      <button id="deployAppBtn" class="primary" ${spec.kind && !runnable ? "disabled" : ""}>
        🚀 Build &amp; deploy${d.default_mode === "k8s" ? " to the cluster" : ""}</button>
      <span class="hint">${modeNote}</span>
      ${spec.kind && !runnable
        ? `<p class="hint warn-t">Nothing to run — this project is ${escapeHtml(spec.kind)}.
           ${spec.kind === "unknown"
             ? "Nothing here looked like an entrypoint. If the team has just merged something, this re-checks itself within a couple of minutes."
             : "The static preview is the right tool for it."}</p>` : ""}
      ${d.log ? `<details><summary>Last build log</summary><pre class="deploy-log">${
        escapeHtml(d.log)}</pre></details>` : ""}`;
  }

  const openStatic = $("#openStaticBtn");
  if (openStatic) openStatic.addEventListener("click", async () => {
    openStatic.disabled = true;
    try {
      const r = await api(`/api/projects/${currentProject}/preview`, { method: "POST" });
      if (previewReachable(r.url)) window.open(r.url, "_blank", "noopener");
      else toast(`The preview is running inside the server (${r.url}) — it isn't reachable `
        + `from your browser. Deploy it to the cluster for a public URL.`);
    } catch (e) {
      toast(`Could not open it: ${e.message}`);
    } finally {
      openStatic.disabled = false;
    }
  });

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
            `<button data-q="${it.question_id}" data-opt="${i}"
               title="${escapeHtml(optText(o))}">${escapeHtml(trim(optText(o), 60))}</button>`).join("")}
          <button class="link" data-open="${it.project_id}">Open project →</button>
        </div>
      </div>`).join("");
  panel.querySelectorAll("[data-opt]").forEach((b) =>
    b.addEventListener("click", async () => {
      const item = n.items.find((x) => String(x.question_id) === b.dataset.q);
      await answerQuestion(Number(b.dataset.q), optText(item.options[Number(b.dataset.opt)]));
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
  wireSprintChips();
};
$("#projectSelect").addEventListener("change", (e) => selectProject(e.target.value));
$("#homeLink").addEventListener("click", () => showHome());
$("#homeLink").style.cursor = "pointer";
$("#modeBuild").addEventListener("click", openDialog);
$("#modeStudio").addEventListener("click", () => openStudio());
$("#modeImprove").addEventListener("click", () => openSelfRepair());
$("#selfBackBtn").addEventListener("click", () => showHome());
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
// The sprint count is the setting people most misjudge, because "6" says nothing
// about what six means. Say the consequence in runs and rough wall-clock instead.
function wireSprintChips() {
  const chips = document.querySelectorAll("#sprintChips .schip");
  const n = document.querySelector('#step3 [name=sprints]');
  const cap = document.querySelector('#step3 [name=max_runs]');
  const says = $("#sprintSays");
  if (!chips.length || !n || !says) return;
  const paint = () => {
    const v = Number(n.value) || 1;
    cap.value = Math.min(400, 40 * v);
    const hrs = v * 1.5;
    says.innerHTML = v === 1
      ? `<b>One pass.</b> It plans, builds and verifies once, then stops and shows you
         the result. Up to <b>${cap.value} agent runs</b>.`
      : `<b>${v} rounds.</b> After each one it looks at what exists, decides for itself
         what is still missing, and starts again — <b>without asking you</b>. Up to
         <b>${cap.value} agent runs</b>, roughly <b>${hrs < 24 ? hrs + "h" : Math.round(hrs/24) + " days"}</b>
         of unattended work.`;
    chips.forEach((c) => c.classList.toggle("active", Number(c.dataset.n) === v));
  };
  chips.forEach((c) => c.addEventListener("click", () => { n.value = c.dataset.n; paint(); }));
  n.addEventListener("input", paint);
  paint();
}

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
        sprints: Number(f.get("sprints")) || 1,
        // Whether time or quality is the constraint. Sent explicitly rather than
        // relying on the server default, so the form is the single source of what
        // was chosen.
        ambition: f.get("ambition") || "standard",
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


// The quality-versus-time slider.
//
// This was three tall tiles of prose, and the whole step read as a form to
// survive rather than two decisions to make. A slider fits how the choice
// actually feels — "how far along the fast-to-thorough line am I" — but a bare
// slider only moves a word, so the strip underneath names what CHANGES at that
// position. That is the part that makes it a trade rather than a vibe.
const AMBITION_STOPS = [
  {
    id: "draft", name: "Draft",
    says: "The smallest thing that shows the idea. Rough is fine, and you will "
        + "see something running quickly.",
    changes: ["fast model", "no rival attempts", "manager reviews alone"],
  },
  {
    id: "standard", name: "Standard",
    says: "Work you would be comfortable showing someone — finished, with the "
        + "obvious failure cases handled.",
    changes: ["cheap first, stronger on failure", "rivals if the manager asks",
              "one teammate reviews"],
  },
  {
    id: "exacting", name: "Exacting",
    says: "Time is not the constraint. It plans in depth, refuses to accept a "
        + "first attempt just because it works, and keeps going until the result "
        + "is genuinely good. Expect it to take much longer and cost more.",
    changes: ["strong model from the start", "rival attempts where approach matters",
              "two teammates review", "escalates after one failure"],
  },
];

function paintAmbition() {
  const range = $("#ambitionRange");
  if (!range) return;
  const stop = AMBITION_STOPS[Number(range.value)] || AMBITION_STOPS[1];
  $("#ambitionValue").value = stop.id;
  $("#ambitionName").textContent = stop.name;
  $("#ambitionSays").textContent = stop.says;
  $("#ambitionChanges").innerHTML = stop.changes
    .map((c) => `<span class="qtag">${escapeHtml(c)}</span>`).join("");
  // Colour the filled part of the track, so the position reads before the words do.
  range.style.setProperty("--fill", `${(Number(range.value) / 2) * 100}%`);
}

const AUTONOMY_SAYS = {
  supervised: "Runs on its own, but stops and waits when a decision could hide a "
    + "problem — approving work nobody delivered, merging past failing tests, giving "
    + "up on a task. Other questions wait up to an hour, then it decides and tells "
    + "you what it assumed.",
  autonomous: "Never blocks on you. It still recognises those same decisions and "
    + "records each one for you to audit — it just does not wait.",
};

function paintAutonomy() {
  const picked = document.querySelector("[name=autonomy]:checked");
  const el = $("#autonomySays");
  if (picked && el) el.textContent = AUTONOMY_SAYS[picked.value] || "";
}

document.addEventListener("input", (ev) => {
  if (ev.target.id === "ambitionRange") paintAmbition();
  if (ev.target.name === "autonomy") paintAutonomy();
});


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

// A deterministic sigil from the name: same name → same emblem forever, so a
// teammate looks the same everywhere. Two conic wedges seeded from a hash, tinted
// within the agent's provider hue — generative geometry, never stock avatar art.
function sigil(name, provider) {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const a = h % 360, b = (h >> 3) % 360;
  const tint = { anthropic: 18, openai: 158, google: 214 }[provider] ?? 220;
  return `conic-gradient(from ${a}deg at 40% 40%, `
       + `hsl(${tint} 45% 62%) 0deg, hsl(${(tint + 40) % 360} 40% 55%) ${120 + b % 120}deg, `
       + `hsl(${tint} 35% 48%) 360deg)`;
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

function chip(label, on, attr) {
  return `<button class="sc-chip${on ? " on" : ""}" ${attr}>${escapeHtml(label)}</button>`;
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
function studioMenu(x, y, items) {
  document.querySelectorAll(".ctx-menu").forEach((m) => m.remove());
  const m = document.createElement("div");
  m.className = "ctx-menu";
  m.style.left = x + "px"; m.style.top = y + "px";
  // Built with createElement + textContent, never innerHTML — a menu label can
  // carry an agent's name, which is free text, and textContent cannot be an
  // injection the way an interpolated innerHTML string can.
  items.forEach((it) => {
    if (it.sep) { const d = document.createElement("div"); d.className = "ctx-sep"; m.appendChild(d); return; }
    const b = document.createElement("button");
    b.className = "ctx-item" + (it.soon ? " soon" : "");
    b.textContent = it.label;
    if (it.soon) { b.disabled = true; const s = document.createElement("span"); s.textContent = "soon"; b.appendChild(s); }
    else b.addEventListener("click", () => { m.remove(); it.act(); });
    m.appendChild(b);
  });
  document.body.appendChild(m);
  const close = (e) => { if (!m.contains(e.target)) { m.remove(); document.removeEventListener("pointerdown", close); } };
  setTimeout(() => document.addEventListener("pointerdown", close), 0);
}

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

const reduceMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const sceneSleep = (ms) => reduceMotion() ? Promise.resolve()
  : new Promise((r) => setTimeout(r, ms));

function suitInfo(suit) {
  const map = { s: "♠", h: "♥", d: "♦", c: "♣" };
  return { glyph: map[suit] || "?", red: suit === "h" || suit === "d" };
}

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


// ============================================================================
// The Lifeworld — the owner's private society. A WORLD holds three registries:
//  AGENTS (people), ARTIFACTS (objects) and ROOMS. People and objects are authored
//  once from a short free-text BRIEF (the LLM fills in the internals), then PLACED
//  into rooms. A room has a THEME (open/home/classroom/campus/office/casino); only a
//  casino renders the poker felt — every other theme gets a tasteful, CSS-only
//  setting. The overview is a MAP: every person and object shown as a card, grouped
//  and colour-coded by the room it sits in, with an "unplaced" group for the rest.
//
//  Reuses the Studio's figure/sigil() language, the inline composer drawers (never a
//  browser popup), the createElement/textContent context menus (studioMenu), the
//  poker felt + .pcard cards and the shared control-padding tokens. All free text
//  reaches innerHTML only through escapeHtml; every model call in Live mode is billed
//  and surfaced in the room's cost-aware ticker.
// ============================================================================
let lwWorlds = [];
let lwWorld = null;            // GET /api/lw/{id} — the overview payload
let lwWorldId = null;
let lwTab = "overview";        // overview | agents | artifacts | rooms
let lwRoom = null;             // the open ROOM (GET .../room/{rid})
let lwRoomId = null;
let lwLive = false;            // Live = agents actually think (spends tokens)
let lwSeenLog = new Set();     // room-log 'n's already shown, so only new lines animate
let lwRoomTypes = [];          // [{type,theme,blurb}] from the API

// Distinct, well-spaced accent hues so each room reads as its own colour on the
// map. Indexed by the room's position in the world's room list.
const LW_ROOM_HUES = [162, 26, 214, 276, 128, 336, 46, 194];
const lwLiveQ = () => (lwLive ? "?live=1" : "");

// --- Konva room canvas: the infinite paintable room -------------------------
// One Stage per open room, mounted into #lwKonvaHost. Tokens live in worldLayer;
// the dotted grid (worldLayer's sibling) is revealed only while something drags.
// Pan/zoom is applied to the Stage (so it transforms every layer) and cached per
// room so a rebuild after a drag/round/create never yanks the viewport.
let lwKonva = null;          // { stage, gridLayer, worldLayer, host, agents:Map, props:Map, ... }
let lwTool = "select";       // select | agent | artifact | manager
let lwCreateFlow = null;     // active single-flight creation, or null
let lwGridTimer = null;
const lwViewCache = {};      // roomId -> {x,y,scale}
const LW_TABLE_R = 58;       // collating-table disc radius (world units)
const LW_SOCKET_R = 80;      // radius of the ring the seat sockets sit on
const LW_SNAP_DIST = 46;     // drop within this of a free socket → magnetic seat
const LW_NUDGE = 6;          // world units an arrow-key press moves the selection
const LW_RECT_W = 150;       // rectangular-table footprint
const LW_RECT_H = 92;
const LW_SEAT_OUT = 20;      // how far outside a shape's edge its seat sockets sit
// Tools each carry a one-key shortcut (shown in the dock, wired on the canvas).
// Icons are drawn as inline vector — the whole canvas is off the emoji look.
const LW_TOOLS = [
  { id: "select",   key: "V", label: "Select" },
  { id: "agent",    key: "A", label: "Agent" },
  { id: "artifact", key: "O", label: "Object" },
];
let lwThreadDir = "both";      // "both" (bidirectional) | "one" (unidirectional, tail→head)
// The style variants a new person can wear; the final face blends the variant with
// the agent's own identity, so two people who pick the same variant still differ.
const LW_AV_VARIANTS = ["a", "b", "c", "d", "e", "f"];
// The object "figure" choices — geometric vector glyphs (mono = a monogram letter).
const LW_OBJ_ICONS = ["mono", "cards", "doc", "star", "gem", "ring"];

// --- screen switch: bring the section on, hide every sibling (mirrors the Studio).
function showLifeworld() {
  $("#home").hidden = true; $("main").hidden = true;
  for (const id of ["plan", "selfPage", "aboutPage", "studio", "scenes"]) {
    const e = $("#" + id); if (e) e.hidden = true;
  }
  $("#projectBar").hidden = true;
  $("#lifeworld").hidden = false;
  currentProject = null;
}

// ============================================================================
// The Studio — one implicit world per user; scenes are canvases; the canvas is the
// hero. openStudio() brings the section on and calls lwEnterStudio() to open a scene.
// ============================================================================
let sdTau = 0;

async function lwEnterStudio() {
  let worlds = [];
  try { worlds = (await api("/api/lw")).worlds || []; }
  catch (e) { $("#lwStage").innerHTML = `<p class="empty">Could not open the Studio: ${escapeHtml(e.message || String(e))}</p>`; return; }
  if (worlds.length) lwWorldId = worlds[0].id;
  else {
    try { lwWorldId = (await api("/api/lw", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "Studio" }) })).world.id; }
    catch (e) { toast(`Could not start the Studio: ${e.message}`); return; }
  }
  let ov = { rooms: [] };
  try { ov = await api(`/api/lw/${lwWorldId}`); } catch (e) { /* a fresh world */ }
  sdTau = (ov.world && ov.world.tau) || 0;
  const rooms = ov.rooms || [];
  let rid = rooms.length ? rooms[rooms.length - 1].id : null;
  if (rid == null) {
    try { rid = (await api(`/api/lw/${lwWorldId}/room`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "untitled", type: "freeplay" }) })).room.id; }
    catch (e) { toast(`Could not create a scene: ${e.message}`); return; }
  }
  await lwOpenScene(rid);
}

async function lwOpenScene(rid) {
  sdPause();
  lwRoomId = rid; lwSeenLog = new Set();
  let d;
  try { d = await api(`/api/lw/${lwWorldId}/room/${rid}`); }
  catch (e) { $("#lwStage").innerHTML = `<p class="empty">Could not open the scene: ${escapeHtml(e.message || String(e))}</p>`; return; }
  lwRenderRoom(d.room || d);
}

// --- top bar: editable title, autosave indicator, scenes, rename -----------
let sdSaveT = null;
// The whole scene autosaves in the DB on every action; this indicator just tells the
// truth about it. Idle rests on "Saved"; a change flashes the spinner, then back to Saved.
function sdFlash() {
  const el = $("#sdSave"); if (!el) return;
  el.className = "sd-save saving"; el.textContent = "";
  clearTimeout(sdSaveT);
  sdSaveT = setTimeout(() => { if ($("#sdSave") !== el) return; el.className = "sd-save saved"; el.textContent = "Saved"; }, 350);
}
function sdSavedIdle() { const el = $("#sdSave"); if (el && !el.classList.contains("saving")) { el.className = "sd-save saved"; el.textContent = "Saved"; } }
async function sdSaveNow() {
  if (!lwWorldId) return;
  sdFlash();
  try { await api(`/api/lw/${lwWorldId}/touch`, { method: "POST" }); } catch (e) { /* the indicator already reassured */ }
}
async function sdRenameScene(name) {
  if (!lwWorldId || !lwRoomId) return;
  sdFlash();
  try {
    const d = await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/scene`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    if (lwRoom) lwRoom.name = (d.room && d.room.name) || name;
  } catch (e) { toast(`Could not rename: ${e.message}`); }
}
async function sdToggleScenes() {
  const menu = $("#sdScenesMenu"), btn = $("#sdScenesBtn"); if (!menu) return;
  if (!menu.hidden) { menu.hidden = true; if (btn) btn.setAttribute("aria-expanded", "false"); return; }
  let rooms = [];
  try { rooms = (await api(`/api/lw/${lwWorldId}`)).rooms || []; } catch (e) { toast(`Could not list scenes: ${e.message}`); return; }
  menu.innerHTML = rooms.map((r) =>
    `<div class="sd-menu-row${r.id === lwRoomId ? " on" : ""}">
      <button class="sd-menu-item" data-scene="${escapeHtml(String(r.id))}">
        <span class="sd-menu-name">${escapeHtml(r.name || "untitled")}</span>
        <span class="sd-menu-sub">${(r.agents || []).length} cast · ${(r.props || []).length} props</span>
      </button>
      <button class="sd-menu-del" data-del="${escapeHtml(String(r.id))}" data-name="${escapeHtml(r.name || "untitled")}" title="Delete scene" aria-label="Delete this scene">🗑</button>
    </div>`).join("")
    + `<button class="sd-menu-item sd-menu-new" id="sdNewScene">＋ New scene</button>`;
  menu.hidden = false; if (btn) btn.setAttribute("aria-expanded", "true");
  menu.querySelectorAll("[data-scene]").forEach((b) => b.addEventListener("click", () => { menu.hidden = true; lwOpenScene(Number(b.dataset.scene)); }));
  menu.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", (ev) => {
    ev.stopPropagation(); menu.hidden = true; sdDeleteScene(Number(b.dataset.del), b.dataset.name);
  }));
  $("#sdNewScene").addEventListener("click", async () => {
    menu.hidden = true;
    try { const r = await api(`/api/lw/${lwWorldId}/room`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "untitled", type: "freeplay" }) }); sdFlash(); lwOpenScene(r.room.id); }
    catch (e) { toast(`Could not add a scene: ${e.message}`); }
  });
}

// Delete a scene (from the dropdown or the title-bar trash). The cast is world-level, so
// agents/props survive; only this canvas goes. Afterwards we open another scene, or mint a
// fresh untitled one so the Studio is never empty.
async function sdDeleteScene(rid, name) {
  if (!lwWorldId || rid == null) return;
  if (!confirm(`Delete the scene "${name || "untitled"}"?\n\nIts agents and props stay in your cast — only this canvas is removed.`)) return;
  sdPause();
  try { await api(`/api/lw/${lwWorldId}/room/${rid}`, { method: "DELETE" }); }
  catch (e) { toast(`Could not delete the scene: ${e.message}`); return; }
  sdFlash(); toast("Scene deleted");
  let rooms = [];
  try { rooms = (await api(`/api/lw/${lwWorldId}`)).rooms || []; } catch (e) { /* fall through to a fresh scene */ }
  const next = rooms.find((r) => r.id !== rid);
  if (next) { lwOpenScene(next.id); return; }
  try { const r = await api(`/api/lw/${lwWorldId}/room`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "untitled", type: "freeplay" }) }); lwOpenScene(r.room.id); }
  catch (e) { toast(`Could not open a scene: ${e.message}`); }
}
function sdDeleteCurrentScene() { if (lwRoomId != null) sdDeleteScene(lwRoomId, (lwRoom && lwRoom.name) || "untitled"); }

// --- the Cast: every agent, grouped by the scene they are in ---------------
async function sdOpenRoster() {
  const host = $("#sdRosterHost"); if (!host || !lwWorldId) return;
  host.hidden = false;
  host.innerHTML = `<div class="sd-roster-card"><p class="dim">gathering the cast…</p></div>`;
  let d;
  try { d = await api(`/api/lw/${lwWorldId}`); }
  catch (e) { host.innerHTML = `<div class="sd-roster-card"><p class="dim">Could not read the cast: ${escapeHtml(e.message)}</p></div>`; return; }
  const rooms = d.rooms || [], agents = d.agents || [];
  const byRoom = {}; agents.forEach((a) => { const k = a.room == null ? "loose" : String(a.room); (byRoom[k] = byRoom[k] || []).push(a); });
  const nameOf = (k) => k === "loose" ? "Not in a scene" : ((rooms.find((r) => String(r.id) === k) || {}).name || "untitled");
  const card = (a) => {
    const seed = lwAvatarSeed({ name: a.name, id: a.id, figure: a.figure });
    const want = (dominantWant(a.wants) || {}).name || "";
    return `<button class="sd-cast" data-room="${a.room == null ? "" : escapeHtml(String(a.room))}" data-agent="${escapeHtml(String(a.id))}">
      <span class="sd-cast-face"><img alt="" src="${lwSvgUri(lwAvatarSvg(seed, 40))}"></span>
      <span class="sd-cast-name">${escapeHtml(a.name || "someone")}</span>
      ${want ? `<span class="sd-cast-want">wants ${escapeHtml(want)}</span>` : ""}
      ${lwMoodBars(a.mood || {})}
    </button>`;
  };
  const groups = Object.keys(byRoom).sort((x, y) => (x === "loose") - (y === "loose"));
  host.innerHTML = `<div class="sd-roster-card">
    <div class="sd-roster-head"><h3>The Cast</h3>
      <span class="dim">${agents.length} agent${agents.length === 1 ? "" : "s"} · ${rooms.length} scene${rooms.length === 1 ? "" : "s"}</span>
      <button class="sd-close" id="sdRosterClose">✕</button></div>
    ${groups.length ? groups.map((k) => `<div class="sd-roster-group">
      <div class="sd-roster-scene">${escapeHtml(nameOf(k))} <span class="dim">· ${byRoom[k].length}</span></div>
      <div class="sd-roster-grid">${byRoom[k].map(card).join("")}</div></div>`).join("")
      : `<p class="dim">No agents yet. Pick the Agent tool and click the canvas.</p>`}
  </div>`;
  $("#sdRosterClose").addEventListener("click", () => { host.hidden = true; });
  host.onclick = (e) => { if (e.target === host) host.hidden = true; };   // click backdrop to close
  host.querySelectorAll("[data-agent]").forEach((b) => b.addEventListener("click", async () => {
    host.hidden = true;
    const rid = b.dataset.room;
    if (rid) await lwOpenScene(Number(rid));
    setTimeout(() => { const e = lwKonva && lwKonva.agents.get(String(b.dataset.agent)); if (e) lwSelSet("agent", e); }, 450);
  }));
}

// --- the time transport: play/pause, single-step, speed --------------------
let sdPlaying = false, sdTimer = null, sdSpeed = 2, sdMax = 10, sdRun = 0;   // sec between beats, cap, done
function sdTimeBarHtml() {
  const maxLbl = sdMax === Infinity ? "∞" : sdMax;
  return `<div class="sd-time" id="sdTime">
    <button class="sd-play" id="sdPlay" title="Play — run beats automatically until the cap">▶</button>
    <button class="sd-step" id="sdStep" title="Run one beat now">⏭</button>
    <div class="sd-progress" id="sdProg" title="beats run in this play">
      <i class="sd-prog-fill" id="sdProgFill"></i><span class="sd-prog-lbl" id="sdProgLbl">0 / ${maxLbl}</span></div>
    <label class="sd-speed" title="seconds between beats">⏱<input type="range" id="sdSpeedRange" min="1" max="10" step="1" value="${sdSpeed}"><b id="sdSpeedLbl">${sdSpeed}s</b></label>
    <label class="sd-maxwrap" title="stop after this many beats">cap
      <select id="sdMaxSel">${[5, 10, 25, 50].map((v) => `<option value="${v}"${v === sdMax ? " selected" : ""}>${v}</option>`).join("")}<option value="inf"${sdMax === Infinity ? " selected" : ""}>∞</option></select></label>
    <span class="sd-tau" id="lwTau"></span>
  </div>`;
}
function sdWireTime() {
  const p = $("#sdPlay"); if (p) p.addEventListener("click", sdTogglePlay);
  const s = $("#sdStep"); if (s) s.addEventListener("click", () => { if (!sdPlaying) lwPlayRound(); });
  const r = $("#sdSpeedRange");
  if (r) r.addEventListener("input", (e) => { sdSpeed = Number(e.target.value); const l = $("#sdSpeedLbl"); if (l) l.textContent = sdSpeed + "s"; if (sdPlaying) sdArm(); });
  const m = $("#sdMaxSel");
  if (m) m.addEventListener("change", (e) => { sdMax = e.target.value === "inf" ? Infinity : Number(e.target.value); sdPaintProg(); });
  paintPlay(); sdPaintProg();
}
function paintPlay() { const b = $("#sdPlay"); if (b) { b.textContent = sdPlaying ? "⏸" : "▶"; b.classList.toggle("on", sdPlaying); } }
function sdPaintProg() {
  const fill = $("#sdProgFill"), lbl = $("#sdProgLbl"); if (!fill) return;
  const inf = sdMax === Infinity;
  fill.style.width = inf ? "0%" : Math.min(100, (sdRun / sdMax) * 100) + "%";
  fill.classList.toggle("indet", inf && sdPlaying);
  if (lbl) lbl.textContent = `${sdRun} / ${inf ? "∞" : sdMax}`;
}
function sdTogglePlay() { sdPlaying ? sdPause() : sdPlay(); }
function sdPlay() { sdRun = 0; sdPlaying = true; paintPlay(); sdPaintProg(); sdArm(); }
function sdPause() { sdPlaying = false; clearTimeout(sdTimer); sdTimer = null; paintPlay(); sdPaintProg(); }
function sdArm() {
  clearTimeout(sdTimer);
  const tick = async () => {
    if (!sdPlaying || !lwKonva) { sdPause(); return; }
    if (lwKonva.drag || (typeof Konva !== "undefined" && Konva.isDragging())) {   // don't rebuild the stage
      lwLogOn && lwLogThr("beatdefer", 500, "life", "beat deferred — a drag is in flight", null, "debug");
      sdTimer = setTimeout(tick, 400); return;                                    // mid-drag — wait for the drop
    }
    sdPulse();
    await lwPlayRound();
    if (!lwKonva) { sdPause(); return; }
    sdRun++; sdPaintProg();
    if (sdRun >= sdMax) { sdPause(); return; }        // bounded — stop at the cap
    if (sdPlaying) sdTimer = setTimeout(tick, sdSpeed * 1000);
  };
  sdTimer = setTimeout(tick, sdSpeed * 1000);
}
function sdPulse() { const f = $("#sdProgFill"); if (!f || reduceMotion()) return; f.classList.remove("beat"); void f.offsetWidth; f.classList.add("beat"); }

// --- activity: a categorised log of what each beat did ---------------------
let sdActOpen = false, sdActTab = "beats";
// A directed graph of who acted on whom this scene, built from the log's frm→who edges.
// Self-hosted SVG; manager ("manage") beats only fold in for root — a black box otherwise.
function sdFlowHtml() {
  const root = lwCanRootDebug();
  const agents = (lwRoom && lwRoom.agents) || [];
  if (!agents.length) return `<p class="sd-act-empty">No agents in this scene yet.</p>`;
  const log = ((lwRoom && lwRoom.log) || []).filter((l) => root || l.kind !== "manage");
  const edges = {};
  log.forEach((l) => {
    if (l.frm == null || l.who == null || l.frm === l.who) return;
    const k = l.frm + ">" + l.who; edges[k] = edges[k] || { c: 0, manage: false };
    edges[k].c++; if (l.kind === "manage") edges[k].manage = true;
  });
  const N = agents.length, R = N > 1 ? 96 : 0, cx = 130, cy = 118, pos = {};
  agents.forEach((a, i) => { const ang = -Math.PI / 2 + i * 2 * Math.PI / N; pos[a.id] = { x: cx + R * Math.cos(ang), y: cy + R * Math.sin(ang), name: a.name || "" }; });
  const maxC = Math.max(1, ...Object.values(edges).map((e) => e.c));
  const arcs = Object.entries(edges).map(([k, e]) => {
    const [f, t] = k.split(">").map(Number), A = pos[f], B = pos[t]; if (!A || !B) return "";
    const dx = B.x - A.x, dy = B.y - A.y, len = Math.hypot(dx, dy) || 1, ox = -dy / len * 16, oy = dx / len * 16;
    const w = (1 + 3 * (e.c / maxC)).toFixed(1), col = e.manage ? "#d64545" : "var(--accent)";
    return `<path d="M${A.x.toFixed(0)} ${A.y.toFixed(0)} Q${(A.x + dx / 2 + ox).toFixed(0)} ${(A.y + dy / 2 + oy).toFixed(0)} ${B.x.toFixed(0)} ${B.y.toFixed(0)}" stroke="${col}" stroke-width="${w}" fill="none" opacity="0.55" marker-end="url(#flowArrow)"/>`;
  }).join("");
  const nodes = agents.map((a) => { const p = pos[a.id]; return `<g><circle cx="${p.x.toFixed(0)}" cy="${p.y.toFixed(0)}" r="15" fill="var(--accent-soft)" stroke="var(--accent)" stroke-width="1.5"/><text x="${p.x.toFixed(0)}" y="${(p.y + 27).toFixed(0)}" text-anchor="middle" font-size="9" fill="var(--text)">${escapeHtml(p.name.slice(0, 9))}</text></g>`; }).join("");
  const hasManage = Object.values(edges).some((e) => e.manage);
  return `<div class="sd-flow"><svg viewBox="0 0 260 250" width="100%" role="img" aria-label="conversation flow">
    <defs><marker id="flowArrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L8 4L0 8z" fill="var(--accent)"/></marker></defs>
    ${arcs}${nodes}</svg>
    <p class="sd-flow-cap">who acted on whom · line weight = how much${hasManage ? ` · <span style="color:#d64545">red = host</span>` : ""}</p></div>`;
}

function sdActivityHtml(log) {
  const root = lwCanRootDebug();
  const tab = (id, label) => `<button class="sd-act-tab${sdActTab === id ? " on" : ""}" data-acttab="${id}">${label}</button>`;
  const tabs = `<div class="sd-act-tabs">${tab("beats", "Beats")}${tab("flow", "Flow")}${root ? tab("canvas", `Canvas${lwLogOn ? ` <span class="sd-log-live" title="capturing"></span>` : ""}`) : ""}</div>`;
  const head = `<div class="sd-act-head">${tabs}<button class="sd-act-x" id="sdActX" aria-label="Close">✕</button></div>`;
  if (root && sdActTab === "canvas") return head + lwLogPanelHtml();
  if (sdActTab === "flow") return head + sdFlowHtml();
  log = (log || []).filter((l) => root || l.kind !== "manage");   // the manager's beats are a black box unless root
  if (!log.length) return head + `<p class="sd-act-empty">Nothing yet. Run a beat to see what happens.</p>`;
  const rows = log.slice(-100).reverse().map((l) => {
    const thought = l.tier === 2 || l.billed, who = l.who != null ? lwNameOf(l.who) : "";
    return `<div class="sd-act-row${thought ? " thought" : ""}">
      <span class="sd-act-kind k-${escapeHtml(String(l.kind || "x"))}">${escapeHtml(String(l.kind || ""))}</span>
      <span class="sd-act-text">${who ? `<b>${escapeHtml(who)}</b> ` : ""}${escapeHtml(String(l.text || ""))}</span>
      ${thought ? `<span class="sd-act-badge" title="a model actually thought — billed">💭</span>` : ""}
    </div>`;
  }).join("");
  return head + `<div class="sd-act-list">${rows}</div>`;
}
function sdShowActivity(open) {
  sdActOpen = open;
  const panel = $("#sdActivity"); if (!panel) return;
  panel.hidden = !open;
  const btn = $("#sdActBtn"); if (btn) btn.classList.toggle("on", open);
  if (!open) return;
  panel.classList.toggle("wide", sdActTab === "flow" || (lwCanRootDebug() && sdActTab === "canvas"));
  panel.innerHTML = sdActivityHtml(lwRoom && lwRoom.log);
  const x = $("#sdActX"); if (x) x.addEventListener("click", () => sdShowActivity(false));
  panel.querySelectorAll(".sd-act-tab").forEach((b) => b.addEventListener("click", () => { sdActTab = b.dataset.acttab; sdShowActivity(true); }));
  if (lwCanRootDebug() && sdActTab === "canvas") lwLogWireCanvasPanel();
}
function sdToggleActivity() { sdShowActivity(!sdActOpen); }

// --- canvas debug logs (ROOT ONLY): a fine-grained, filterable ring buffer of every
// interaction event, shown in the Activity panel's "Canvas" tab. Off by default, gated on
// me.is_root — these are control-plane diagnostics, not user-facing, and cost nothing when
// capture is off (lwLog returns on the first line). Turn Capture on, reproduce the issue on
// the canvas, filter by category/level, then Copy the lines out.
const LW_LOG_CATS = ["pointer", "hit", "cursor", "drag", "pan", "marquee", "select", "portal", "life", "net", "idle"];
const LW_LOG_LEVELS = { debug: 0, info: 1, warn: 2 };
const LW_LOG_MAX = 3000;
let lwLogOn = false, lwLogBuf = [], lwLogFilter = new Set(LW_LOG_CATS), lwLogLevel = "debug";
const lwLogThrTs = {};
function lwCanRootDebug() { return !!(me && me.is_root); }
function lwLog(cat, msg, data, level) {
  if (!lwLogOn) return;
  lwLogBuf.push({ wall: Date.now(), cat, level: level || "info", msg, data });
  if (lwLogBuf.length > LW_LOG_MAX) lwLogBuf.shift();
  lwLogSchedule();
}
function lwLogThr(key, ms, cat, msg, data, level) {   // throttle a hot event (drag/pointer move)
  if (!lwLogOn) return;
  const now = Date.now();
  if (lwLogThrTs[key] && now - lwLogThrTs[key] < ms) return;
  lwLogThrTs[key] = now; lwLog(cat, msg, data, level);
}
function lwLogTime(wall) {
  const d = new Date(wall), p = (n, w) => String(n).padStart(w || 2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}
function lwLogFmt(data) {
  if (data == null) return "";
  try { return typeof data === "string" ? data : JSON.stringify(data); } catch (e) { return String(data); }
}
function lwLogVisible() {
  const min = LW_LOG_LEVELS[lwLogLevel] ?? 0;
  return lwLogBuf.filter((e) => lwLogFilter.has(e.cat) && (LW_LOG_LEVELS[e.level] ?? 1) >= min);
}
let lwLogRenderT = null;
function lwLogSchedule() {                          // batch live re-renders so a firehose can't thrash the DOM
  if (!(sdActOpen && sdActTab === "canvas") || lwLogRenderT) return;
  lwLogRenderT = setTimeout(() => { lwLogRenderT = null; lwLogRender(); }, 140);
}
function lwLogRowHtml(e) {
  const d = e.data != null ? ` <span class="sd-log-data">${escapeHtml(lwLogFmt(e.data))}</span>` : "";
  return `<div class="sd-log-row lvl-${e.level}"><span class="sd-log-t">${lwLogTime(e.wall)}</span>`
    + `<span class="sd-log-cat c-${e.cat}">${e.cat}</span><span class="sd-log-msg">${escapeHtml(e.msg)}${d}</span></div>`;
}
function lwLogRender() {
  const list = $("#lwLogList"); if (!list) return;
  const vis = lwLogVisible();
  list.innerHTML = vis.length ? vis.slice(-500).reverse().map(lwLogRowHtml).join("")
    : `<p class="sd-act-empty">${lwLogOn ? "No lines match the filter yet — interact with the canvas." : "Capture is off. Turn it on, then reproduce the issue."}</p>`;
  const n = $("#lwLogN"); if (n) n.textContent = `${vis.length}/${lwLogBuf.length}`;
}
function lwLogPanelHtml() {
  const chips = LW_LOG_CATS.map((c) => `<button class="sd-log-chip${lwLogFilter.has(c) ? " on" : ""}" data-logcat="${c}">${c}</button>`).join("");
  const lvls = Object.keys(LW_LOG_LEVELS).map((l) => `<option value="${l}"${lwLogLevel === l ? " selected" : ""}>${l}+</option>`).join("");
  return `<div class="sd-log-ctl">
      <button class="sd-log-cap${lwLogOn ? " on" : ""}" id="lwLogCap">${lwLogOn ? "● Capturing" : "○ Capture"}</button>
      <select class="sd-log-lvl" id="lwLogLvl" title="minimum level">${lvls}</select>
      <button class="sd-log-btn" id="lwLogCopy" title="Copy the shown lines">Copy</button>
      <button class="sd-log-btn" id="lwLogClear" title="Clear the buffer">Clear</button>
      <span class="sd-log-n" id="lwLogN" title="shown / captured"></span>
    </div>
    <div class="sd-log-cats">${chips}</div>
    <div class="sd-log-list" id="lwLogList"></div>
    <p class="sd-log-hint">Control-plane diagnostics · root only. Capture on → reproduce on the canvas → Copy → paste back.</p>`;
}
function lwLogWireCanvasPanel() {
  const cap = $("#lwLogCap");
  if (cap) cap.addEventListener("click", () => {
    lwLogOn = !lwLogOn;
    if (lwLogOn) lwLogBuf.push({ wall: Date.now(), cat: "life", level: "info", msg: "capture started",
      data: { dpr: window.devicePixelRatio, vw: window.innerWidth, vh: window.innerHeight, tool: lwTool,
        scale: lwKonva ? +lwKonva.stage.scaleX().toFixed(3) : null, agents: lwKonva ? lwKonva.agents.size : 0, props: lwKonva ? lwKonva.props.size : 0 } });
    cap.classList.toggle("on", lwLogOn); cap.textContent = lwLogOn ? "● Capturing" : "○ Capture";
    lwLogRender();
    const live = document.querySelector(".sd-act-tab .sd-log-live"), tab = document.querySelector('.sd-act-tab[data-acttab="canvas"]');
    if (tab) { if (lwLogOn && !live) tab.insertAdjacentHTML("beforeend", ` <span class="sd-log-live"></span>`); else if (!lwLogOn && live) live.remove(); }
  });
  const lvl = $("#lwLogLvl"); if (lvl) lvl.addEventListener("change", (e) => { lwLogLevel = e.target.value; lwLogRender(); });
  $("#lwLogCopy") && $("#lwLogCopy").addEventListener("click", lwLogCopy);
  $("#lwLogClear") && $("#lwLogClear").addEventListener("click", () => { lwLogBuf = []; lwLogRender(); });
  const panel = $("#sdActivity");
  panel && panel.querySelectorAll(".sd-log-chip").forEach((b) => b.addEventListener("click", () => {
    const c = b.dataset.logcat; lwLogFilter.has(c) ? lwLogFilter.delete(c) : lwLogFilter.add(c);
    b.classList.toggle("on", lwLogFilter.has(c)); lwLogRender();
  }));
  lwLogRender();
}
function lwLogCopy() {
  const vis = lwLogVisible();
  const text = vis.map((e) => `${lwLogTime(e.wall)} [${e.cat}/${e.level}] ${e.msg}${e.data != null ? " " + lwLogFmt(e.data) : ""}`).join("\n");
  const done = () => toast(`Copied ${vis.length} log lines`);
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done).catch(() => lwLogCopyFallback(text, done));
  else lwLogCopyFallback(text, done);
}
function lwLogCopyFallback(text, done) {
  const ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { toast("Copy failed — select the text manually"); }
  ta.remove();
}
// name what a press landed on, for the pointer log
function lwTokenAncestor(node) { return node && node.findAncestor ? (node.findAncestor(".token", true) || node.findAncestor(".selframe", true)) : null; }
function lwHitDesc(node) {
  if (!node || !lwKonva) return "none";
  if (node === lwKonva.stage) return "stage(empty floor)";
  const tok = lwTokenAncestor(node);
  const self = (node.name && node.name()) || node.className || "?";
  if (tok && tok === lwKonva.selframe) return "selframe";
  if (tok) return `${tok.getAttr("lwType")}#${tok.getAttr("lwId")} [${self}]`;
  return String(self);
}
function lwRoundPt(p) { return p ? { x: Math.round(p.x), y: Math.round(p.y) } : null; }
// When a press lands on empty floor, find the token nearest the click and whether the click
// was inside its box — so we can tell "a token is right there but the hit graph missed it"
// from "nothing is actually there / it's displaced".
function lwNearestToken(w) {
  if (!lwKonva || !w) return null;
  let best = null;
  const scan = (map, type) => map.forEach((entry) => {
    const p = entry.node.position();
    const d = Math.hypot(p.x - w.x, p.y - w.y);
    let insideBox = false;
    try { const r = entry.node.getClientRect({ relativeTo: lwKonva.worldLayer }); insideBox = w.x >= r.x && w.x <= r.x + r.width && w.y >= r.y && w.y <= r.y + r.height; } catch (e) { /* */ }
    if (!best || d < best.dist) best = { id: entry.data.id, type, dist: d, at: p, insideBox };
  });
  scan(lwKonva.agents, "agent"); scan(lwKonva.props, "prop");
  return best;
}

// --- scene rules: a small popover; saved to the scene, obeyed each run ------
// Scene rules as ordered "ingress rows": each row is a typed effect (gate or shaper) with an
// optional when-match, evaluated top-to-bottom. Reused for a thread's own rule table too.
const LW_RULE_EFFECTS = ["deny", "allow", "clamp", "bias", "annotate"];
const LW_RULE_KINDS = ["", "greet", "say", "scold", "praise", "deal", "see"];
const LW_RULE_FIELDS = ["mood.stress", "mood.confidence", "mood.hope", "mood.focus", "vitals.energy", "drives.social", "drives.esteem", "drives.curiosity"];

function sdRuleRowHtml(r, i) {
  const eff = r.effect || "annotate", isShape = eff === "clamp" || eff === "bias";
  const kind = (r.when && r.when.kind) || "";
  const opt = (list, cur, lbl) => list.map((v) => `<option value="${escapeHtml(String(v))}"${String(v) === String(cur) ? " selected" : ""}>${escapeHtml(lbl ? lbl(v) : v)}</option>`).join("");
  return `<div class="sd-rule" data-i="${i}">
    <select class="sd-rule-eff" data-k="effect">${opt(LW_RULE_EFFECTS, eff)}</select>
    <span class="sd-rule-when">when <select data-k="kind">${opt(LW_RULE_KINDS, kind, (k) => k || "any")}</select></span>
    ${isShape ? `<select class="sd-rule-field" data-k="field">${opt(LW_RULE_FIELDS, r.field || LW_RULE_FIELDS[0])}</select>
      <input class="sd-rule-val" data-k="value" type="number" step="0.05" value="${r.value ?? 0}">` : ""}
    <input class="sd-rule-note" data-k="note" placeholder="${eff === "annotate" ? "text the model reads" : "note (optional)"}" value="${escapeHtml(r.note || "")}">
    <button class="sd-rule-del" title="Delete rule" aria-label="Delete rule">✕</button>
  </div>`;
}

function sdRenderRules(draft, host) {
  host = host || $("#sdRulesRows"); if (!host) return;
  host.innerHTML = draft.length ? draft.map(sdRuleRowHtml).join("")
    : `<p class="sd-rules-empty">No rules — like an empty security group. Add one below.</p>`;
  host.querySelectorAll(".sd-rule").forEach((row) => {
    const i = Number(row.dataset.i);
    row.querySelectorAll("[data-k]").forEach((el) => {
      el.addEventListener(el.tagName === "SELECT" ? "change" : "input", () => {
        const k = el.dataset.k, r = draft[i];
        if (k === "kind") { r.when = r.when || {}; if (el.value) r.when.kind = el.value; else delete r.when.kind; }
        else if (k === "value") r.value = Number(el.value);
        else r[k] = el.value;
        if (k === "effect") sdRenderRules(draft, host);   // reveal/hide the field+value for shaper effects
      });
    });
    row.querySelector(".sd-rule-del").addEventListener("click", () => { draft.splice(i, 1); sdRenderRules(draft, host); });
  });
}

// ---- the Rulebook panel: one free-text rulebook per graph + its hidden manager ----
async function sdOpenThreads(focusId) {
  const host = $("#sdRosterHost"); if (!host || !lwWorldId) return;
  host.hidden = false;
  host.innerHTML = `<div class="sd-roster-card"><p class="dim">reading the graph…</p></div>`;
  let room;
  try { room = (await api(`/api/lw/${lwWorldId}/room/${lwRoomId}`)).room; }
  catch (e) { host.innerHTML = `<div class="sd-roster-card"><p class="dim">Could not read graphs: ${escapeHtml(e.message)}</p></div>`; return; }
  const nameOf = (id) => { const a = (room.agents || []).find((x) => x.id === id) || (room.props || []).find((x) => x.id === id); return a ? a.name : `#${id}`; };
  const models = LW_MODELS.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join("");
  let threads = (room.threads || []).slice();
  if (focusId != null) threads.sort((a, b) => (a.id === focusId ? -1 : b.id === focusId ? 1 : 0));   // focused graph first
  // ONE rules field for the whole graph — no thread/arrow/edge plumbing exposed. A graph's rules
  // ARE its manager's brief; the manager (model + a bounded per-round budget) runs them.
  const card = (t) => `<div class="sd-thread" data-tid="${t.id}">
      <div class="sd-thread-members">${escapeHtml([...new Set((t.edges || []).flatMap((e) => [e[0], e[1]]))].map(nameOf).join(" · ") || "no members")}</div>
      <div class="sc-label">Rules for this graph <span class="dim">plain language — the manager makes it happen</span></div>
      <textarea class="sc-input sd-thread-book" rows="5" placeholder="e.g. anyone can start — I want a debate on the most sustainable route from A to B.">${escapeHtml(t.rulebook || "")}</textarea>
      <div class="sd-book-actions"><button class="sd-book-refine">✨ Refine with AI</button></div>
      <div class="sc-label">Hidden manager <span class="dim">orchestrates it · a black box unless you're root</span></div>
      <div class="sd-thread-mgr">
        <label>Model <select class="sd-mgr-model">${models}</select></label>
        <label>Budget <input class="sd-mgr-budget" type="number" min="0" max="4" value="${(t.manager && t.manager.budget) ?? 2}"></label>
      </div>
      <div class="sc-actions"><button class="sc-ctl primary sd-thread-save">Save rules</button></div>
    </div>`;
  host.innerHTML = `<div class="sd-roster-card sd-threads-card">
    <div class="sd-roster-head"><h3>Graph rules</h3><span class="dim">the manager runs these across the graph</span>
      <button class="sd-close" id="sdThreadsClose">✕</button></div>
    <div class="sd-threads-list">${threads.length ? threads.map(card).join("") : `<p class="dim">No graph yet. Select a token, then drag one of its four handles onto another token to connect them.</p>`}</div>
  </div>`;
  $("#sdThreadsClose").addEventListener("click", () => { host.hidden = true; });
  threads.forEach((t) => {
    const el = host.querySelector(`.sd-thread[data-tid="${t.id}"]`); if (!el) return;
    const mm = el.querySelector(".sd-mgr-model"); if (mm) mm.value = (t.manager && t.manager.model) || "";
    const book = el.querySelector(".sd-thread-book");
    el.querySelector(".sd-book-refine").addEventListener("click", async (ev) => {
      const btn = ev.currentTarget; btn.disabled = true; btn.textContent = "refining…";
      try { const r = await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/thread/${t.id}/refine`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: book.value }) }); if (r.text) book.value = r.text; }
      catch (e) { toast(`Could not refine: ${e.message}`); }
      btn.disabled = false; btn.textContent = "✨ Refine with AI";
    });
    el.querySelector(".sd-thread-save").addEventListener("click", async () => {
      const body = { rulebook: book.value,
        manager: { model: mm ? mm.value : "", budget: Number(el.querySelector(".sd-mgr-budget").value) || 0 } };
      sdFlash();
      try { await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/thread/${t.id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); toast("rules saved"); await lwReloadRoom(); }
      catch (e) { toast(`Could not save: ${e.message}`); }
    });
  });
}

// ---- the graph chat: talk to any agent, or to the pinned manager (the main use case) ----
let sdChatState = null;
async function sdOpenChat(tid, peer) {
  const host = $("#sdRosterHost"); if (!host || !lwWorldId) return;
  host.hidden = false;
  host.innerHTML = `<div class="sd-roster-card"><p class="dim">opening chat…</p></div>`;
  let room;
  try { room = (await api(`/api/lw/${lwWorldId}/room/${lwRoomId}`)).room; }
  catch (e) { host.innerHTML = `<div class="sd-roster-card"><p class="dim">Could not open chat: ${escapeHtml(e.message)}</p></div>`; return; }
  const threads = room.threads || [];
  const t = (tid != null && threads.find((x) => x.id === tid)) || threads[0];
  if (!t) { host.innerHTML = `<div class="sd-roster-card"><div class="sd-roster-head"><h3>Chat</h3><button class="sd-close" onclick="this.closest('#sdRosterHost').hidden=true">✕</button></div><p class="dim">Connect some tokens into a graph first.</p></div>`; return; }
  const agentById = new Map((room.agents || []).map((a) => [a.id, a]));
  const members = [...new Set((t.edges || []).flatMap((e) => [e[0], e[1]]))].map((id) => agentById.get(id)).filter(Boolean);
  sdChatState = { tid: t.id, peer: peer || (sdChatState && sdChatState.tid === t.id ? sdChatState.peer : "manager"), members, chats: t.chats || {}, search: "" };
  sdRenderChat();
}
function sdScrollChat() { const m = $("#sdChatMsgs"); if (m) m.scrollTop = m.scrollHeight; }
function sdRenderChat() {
  const host = $("#sdRosterHost"), st = sdChatState; if (!host || !st) return;
  const peers = [{ id: "manager", name: "Manager", manager: true }, ...st.members.map((a) => ({ id: a.id, name: a.name, agent: a }))];
  const q = (st.search || "").toLowerCase();
  const shown = peers.filter((p) => p.manager || p.name.toLowerCase().includes(q));   // the manager is always pinned + shown
  const av = (p) => p.manager ? `<div class="sd-chat-av mgr">★</div>`
    : `<img class="sd-chat-av" alt="" src="${lwSvgUri(lwAvatarSvg(lwAvatarSeed(p.agent), 36))}">`;
  const peerRow = (p) => {
    const on = String(p.id) === String(st.peer), last = (st.chats[String(p.id)] || []).slice(-1)[0];
    return `<button class="sd-chat-peer${on ? " on" : ""}${p.manager ? " pinned" : ""}" data-peer="${escapeHtml(String(p.id))}">
      ${av(p)}<span class="sd-chat-peer-main"><span class="sd-chat-peer-name">${escapeHtml(p.name)}${p.manager ? ` <span class="sd-chat-pin">pinned</span>` : (p.agent && p.agent.usage && p.agent.usage.asleep ? ` <span class="sd-chat-zzz">z</span>` : "")}</span>
      <span class="sd-chat-peer-last">${last ? escapeHtml(trim(last.text, 40)) : `<span class="dim">no messages yet</span>`}</span></span></button>`;
  };
  const active = peers.find((p) => String(p.id) === String(st.peer)) || peers[0];
  const convo = st.chats[String(st.peer)] || [];
  const msgs = convo.length ? convo.map((m) => `<div class="sd-msg ${m.role === "user" ? "me" : "them"}"><div class="sd-msg-b">${escapeHtml(m.text)}</div></div>`).join("")
    : `<p class="dim sd-chat-empty">Say hello to ${escapeHtml(active.name)}.${active.manager ? " The manager mediates the whole graph." : ""}</p>`;
  host.innerHTML = `<div class="sd-roster-card sd-chat-card">
    <div class="sd-roster-head"><h3>Chat</h3><span class="dim">graph ${st.tid}</span><button class="sd-close" id="sdChatClose">✕</button></div>
    <div class="sd-chat-body">
      <div class="sd-chat-list">
        <input class="sd-chat-search" placeholder="Search people…" value="${escapeHtml(st.search)}">
        <div class="sd-chat-peers">${shown.map(peerRow).join("")}</div>
      </div>
      <div class="sd-chat-pane">
        <div class="sd-chat-pane-head">${av(active)}<span>${escapeHtml(active.name)}${active.manager ? ` · <span class="dim">mediator</span>` : ""}</span></div>
        <div class="sd-chat-msgs" id="sdChatMsgs">${msgs}</div>
        <div class="sd-chat-compose"><input id="sdChatText" placeholder="Message ${escapeHtml(active.name)}…" autocomplete="off"><button id="sdChatSend" class="sc-ctl primary">Send</button></div>
      </div>
    </div></div>`;
  $("#sdChatClose").addEventListener("click", () => { host.hidden = true; });
  const search = host.querySelector(".sd-chat-search");
  search.addEventListener("input", (e) => { st.search = e.target.value; const at = e.target.selectionStart; sdRenderChat(); const s2 = host.querySelector(".sd-chat-search"); if (s2) { s2.focus(); s2.setSelectionRange(at, at); } });
  host.querySelectorAll(".sd-chat-peer").forEach((b) => b.addEventListener("click", () => { st.peer = b.dataset.peer; sdRenderChat(); }));
  const send = async () => {
    const inp = $("#sdChatText"), text = inp.value.trim(); if (!text) return;
    inp.value = "";
    (st.chats[String(st.peer)] = st.chats[String(st.peer)] || []).push({ role: "user", text });   // optimistic
    sdRenderChat(); sdScrollChat();
    const busy = $("#sdChatMsgs"); if (busy) busy.insertAdjacentHTML("beforeend", `<div class="sd-msg them" id="sdChatBusy"><div class="sd-msg-b dim">…</div></div>`); sdScrollChat();
    try {
      const r = await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/thread/${st.tid}/chat`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ to: st.peer, text }) });
      if (r.chat) st.chats[String(st.peer)] = r.chat;
    } catch (e) { toast(`Could not send: ${e.message}`); }
    sdRenderChat(); sdScrollChat();
  };
  $("#sdChatSend").addEventListener("click", send);
  $("#sdChatText").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send(); } });
  $("#sdChatText").focus();
  sdScrollChat();
}

function sdToggleRules() {
  const pop = $("#sdRulesPop"); if (!pop) return;
  if (!pop.hidden) { pop.hidden = true; return; }
  pop.innerHTML = `<div class="sd-rules-head">Scene rules <span class="dim">the whole room · a graph has its own rulebook</span></div>
    <textarea class="sc-input" id="sdRulesText" rows="4" placeholder="e.g. everyone stays in character; keep it civil; no one reveals their card."></textarea>
    <div class="sc-actions"><button class="sc-ctl primary" id="sdRulesSave">Save</button>
      <button class="sc-ctl" id="sdRulesClose">Close</button></div>`;
  pop.hidden = false;
  const ta = $("#sdRulesText"); if (ta) { ta.value = (lwRoom && lwRoom.rules) || ""; ta.focus(); }
  $("#sdRulesClose").addEventListener("click", () => { pop.hidden = true; });
  $("#sdRulesSave").addEventListener("click", async () => {
    const val = $("#sdRulesText").value || "";
    sdFlash();
    try {
      await api(`/api/lw/${lwWorldId}/room/${lwRoomId}/scene`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rules: val }) });
      if (lwRoom) lwRoom.rules = val;
      pop.hidden = true;
    } catch (e) { toast(`Could not save rules: ${e.message}`); }
  });
}

async function openLifeworld(skipHash, worldId) {
  showLifeworld();
  lwWorldId = worldId || null;
  lwTab = "overview"; lwRoomId = null; lwRoom = null; lwWorld = null; lwSeenLog = new Set();
  if (!skipHash) setHash(worldId ? `#/lifeworld/${worldId}` : "#/lifeworld");
  await renderLifeworld();
}

async function renderLifeworld() {
  if ($("#lifeworld").hidden) return;
  if (lwWorldId) await renderWorkspace();
  else await renderWorldLobby();
}

// The mode the bar and stage are in: the lobby, one of the workspace tabs, or an
// open room (which lives under the Rooms tab but reveals the Live toggle + clock).
function lwMode() {
  if (!lwWorldId) return "lobby";
  if (lwTab === "rooms" && lwRoomId) return "room";
  return lwTab;
}

// The bar carries the tabs, the world title, the per-context "＋ New" buttons and —
// only inside a room — the Live toggle and world clock. Show only what the context
// owns; highlight the active tab (Rooms stays lit while a room is open).
function setLwBar(mode) {
  const inWorld = mode !== "lobby";
  const inRoom = mode === "room";
  $("#lwTabs").hidden = !inWorld;
  $("#lwTitle").hidden = !inWorld;
  $("#lwToLobby").hidden = !inWorld;
  $("#lwNewWorld").hidden = inWorld;
  $("#lwNewAgent").hidden = mode !== "agents";
  $("#lwNewArtifact").hidden = mode !== "artifacts";
  $("#lwNewRoom").hidden = mode !== "rooms";
  $("#lwLive").hidden = !inRoom;
  $("#lwTau").hidden = !inRoom;
  document.querySelectorAll(".lw-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.lwtab === lwTab));
}

// ---- small helpers -------------------------------------------------------
// Mood / pressure values may arrive as 0..1 or 0..100; normalise to a 0..100 int.
function lwPct(v) {
  let n = Number(v);
  if (!Number.isFinite(n)) return 0;
  if (n > 0 && n <= 1) n *= 100;
  return Math.max(0, Math.min(100, Math.round(n)));
}

// wants is either a single [name, pressure] pair or a list of them; pick the one
// under the most pressure as the dominant drive.
function dominantWant(wants) {
  if (!Array.isArray(wants) || !wants.length) return null;
  if (typeof wants[0] === "string") return { name: wants[0], pressure: wants[1] };
  let best = null;
  for (const w of wants) {
    if (!Array.isArray(w)) continue;
    if (!best || Number(w[1]) > Number(best[1])) best = w;
  }
  return best ? { name: best[0], pressure: best[1] } : null;
}

function lwWhen(t) {
  try { const dt = new Date(t); if (!isNaN(dt.getTime())) return dt.toLocaleDateString(); } catch (e) { /* fall through */ }
  return String(t);
}

// The dealable object in a room: a deck if one is placed, else the first prop.
function findDeck(room) {
  const props = (room && room.props) || [];
  return props.find((p) => (p.kind || "").includes("deck"))
    || props.find((p) => (p.kind || "").includes("card"))
    || null;
}

// A seat may carry the human id as human_id or id; failing that, resolve by name
// back to the world's agent registry. Returns the id to use for /human and /act.
function lwHumanId(f) {
  if (f && f.human_id != null) return f.human_id;
  const agents = (lwWorld && lwWorld.agents) || [];
  const byName = agents.find((p) => p.name === (f && f.name));
  if (byName) return byName.id;
  return f && f.id;
}

// Resolve a log 'who' / bond key (id or name) to a display name.
function lwNameOf(v) {
  if (v == null) return "";
  const agents = (lwWorld && lwWorld.agents) || [];
  const p = agents.find((x) => String(x.id) === String(v));
  return p ? (p.name || String(v)) : String(v);
}

function lwBilledCount(room) {
  const log = (room && room.log) || [];
  return log.filter((l) => l.billed || l.tier === 2).length;
}

// Each room's accent, exposed as CSS custom properties the cards/groups inherit.
function lwRoomHue(i) {
  const n = LW_ROOM_HUES.length;
  return LW_ROOM_HUES[(((i % n) + n) % n)];
}
function lwRoomStyle(i) {
  const h = lwRoomHue(i);
  return `--rc:hsl(${h} 58% 42%); --rc-soft:hsl(${h} 48% 95%); --rc-line:hsl(${h} 38% 84%)`;
}
function lwRoomIndex() {
  const idx = {};
  ((lwWorld && lwWorld.rooms) || []).forEach((r, i) => { idx[String(r.id)] = i; });
  return idx;
}

// ---- the world lobby -----------------------------------------------------
async function renderWorldLobby() {
  setLwBar("lobby");
  $("#lwDetail").hidden = true;
  const stage = $("#lwStage");
  let d;
  try { d = await api("/api/lw"); }
  catch (e) { stage.innerHTML = `<p class="empty">Could not load the Lifeworld: ${escapeHtml(e.message || String(e))}</p>`; return; }
  lwWorlds = d.worlds || [];
  if (!lwWorlds.length) {
    stage.innerHTML = `<div class="scene-lobby"><div class="scene-empty">
      <p>No worlds yet. A world is a small society — you fill it with people and objects, then place them into rooms.</p>
      <button class="primary" id="lwNew2">Create your first world</button>
    </div></div>`;
    $("#lwNew2").addEventListener("click", openWorldComposer);
    return;
  }
  const cards = lwWorlds.map((w) => `<button class="scene-card" data-open="${escapeHtml(String(w.id))}">
      <span class="scene-kind">world</span>
      <span class="scene-name">${escapeHtml(w.name || "Untitled world")}</span>
      <span class="scene-foot">${w.updated_at ? `<span>updated ${escapeHtml(lwWhen(w.updated_at))}</span>` : ""}</span>
    </button>`).join("");
  stage.innerHTML = `<div class="scene-lobby">
    <div class="lw-lobby-head"><h3>Your worlds</h3></div>
    <div class="scene-cards">${cards}</div></div>`;
  stage.querySelectorAll("[data-open]").forEach((b) => {
    b.addEventListener("click", () => openWorld(b.dataset.open));
    b.addEventListener("contextmenu", (ev) =>
      lwWorldMenu(ev, lwWorlds.find((x) => String(x.id) === b.dataset.open)));
  });
}

// Create a world with just a NAME — no preset. Inline composer, never a popup.
let lwWorldDraft = null;
function openWorldComposer() { lwWorldDraft = { name: "" }; renderWorldComposer(); }
function renderWorldComposer() {
  const box = $("#lwDetail");
  const d = lwWorldDraft;
  box.hidden = false;
  box.className = "studio-detail composing";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem" style="background:${sigil(d.name || "World", "anthropic")}">
        <span class="fig-initial">🌍</span></div>
      <div style="flex:1">
        <input class="sc-name" id="lwWName" placeholder="Name this world"
               value="${escapeHtml(d.name)}" autocomplete="off">
        <p class="sc-preview">a small society of your making</p>
      </div>
      <button class="sd-close" id="lwWClose">✕</button>
    </div>
    <p class="sc-hint">You'll add people, objects and rooms once it exists.</p>
    <div class="sc-actions"><button class="primary" id="lwWCreate">Create the world</button></div>`;
  $("#lwWName").addEventListener("input", (e) => { d.name = e.target.value; });
  $("#lwWClose").addEventListener("click", () => { lwWorldDraft = null; box.hidden = true; });
  $("#lwWCreate").addEventListener("click", doCreateWorld);
}
async function doCreateWorld() {
  const d = lwWorldDraft;
  try {
    const r = await api("/api/lw", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: d.name.trim() || "New world" }) });
    lwWorldDraft = null; $("#lwDetail").hidden = true;
    const id = r.world && r.world.id != null ? r.world.id : null;
    if (id != null) openWorld(id); else await renderLifeworld();
  } catch (e) { toast(`Could not create the world: ${e.message}`); }
}

function lwWorldMenu(ev, w) {
  ev.preventDefault(); ev.stopPropagation();
  if (!w) return;
  studioMenu(ev.clientX, ev.clientY, [
    { label: `Open ${w.name || "world"}`, act: () => openWorld(w.id) },
    { sep: true },
    { label: `Delete ${w.name || "world"}`, act: async () => {
        try { await api(`/api/lw/${w.id}`, { method: "DELETE" }); await renderWorldLobby(); }
        catch (e) { toast(`Could not delete: ${e.message}`); } } },
  ]);
}

async function openWorld(id) {
  lwWorldId = id; lwTab = "overview"; lwRoomId = null; lwRoom = null; lwWorld = null; lwSeenLog = new Set();
  $("#lwDetail").hidden = true;
  setHash(`#/lifeworld/${id}`);
  await renderWorkspace();
}

// ---- the workspace (Overview · Agents · Artifacts · Rooms) ---------------
async function loadWorld() {
  const d = await api(`/api/lw/${lwWorldId}`);
  lwWorld = d;
  if (Array.isArray(d.room_types) && d.room_types.length) lwRoomTypes = d.room_types;
}

async function renderWorkspace() {
  try { await loadWorld(); }
  catch (e) { $("#lwStage").innerHTML = `<p class="empty">Could not open world: ${escapeHtml(e.message || String(e))}</p>`; return; }
  $("#lwTitle").textContent = (lwWorld.world && lwWorld.world.name) || "World";
  await renderLwTab();
}

function selectLwTab(name) {
  lwDestroyCanvas();   // leaving the room view tears its Konva stage down
  lwTab = name;
  // A tab click always lands on that tab's top view — for Rooms, the list, not
  // whatever room was last open (openRoom is the only path that sets lwRoomId).
  lwRoomId = null; lwRoom = null;
  $("#lwDetail").hidden = true;
  renderLwTab();
}

async function renderLwTab() {
  setLwBar(lwMode());
  paintLwLive(); paintLwTau();
  if (lwTab === "overview") renderOverview();
  else if (lwTab === "agents") renderAgentsTab();
  else if (lwTab === "artifacts") renderArtifactsTab();
  else if (lwTab === "rooms") { if (lwRoomId) await renderRoomView(); else renderRoomsTab(); }
}

// ---- shared card builders ------------------------------------------------
function lwMoodBars(mood) {
  mood = mood || {};
  const bar = (label, v, cls) =>
    `<span class="lw-mbar ${cls}" title="${label} ${lwPct(v)}"><i style="width:${lwPct(v)}%"></i></span>`;
  return `<span class="lw-mood">${bar("confidence", mood.confidence, "conf")}${bar("stress", mood.stress, "stress")}</span>`;
}

function lwAgentCard(a, opts) {
  opts = opts || {};
  const skills = Array.isArray(a.skills)
    ? a.skills.slice().sort((x, y) => Number(y[1]) - Number(x[1])) : [];
  const top = skills[0];
  return `<button class="lw-card lw-agent-card" data-agent="${escapeHtml(String(a.id))}" style="${opts.style || ""}">
    <span class="lw-card-emblem" style="background:${sigil(a.name || "?", "anthropic")}">${escapeHtml((a.name || "?")[0] || "?")}</span>
    <span class="lw-card-body">
      <span class="lw-card-name">${escapeHtml(a.name || "someone")}</span>
      <span class="lw-card-sub">${escapeHtml(trim(a.narrative || "no story yet", 96))}</span>
      <span class="lw-card-foot">
        ${top ? `<span class="lw-skill">${escapeHtml(String(top[0]))} <i>${escapeHtml(String(top[1]))}</i></span>` : ""}
        ${lwMoodBars(a.mood)}
        ${opts.roomTag || ""}
      </span>
    </span>
  </button>`;
}

// An object tile — a deck looks like a small card stack, a card like a single back,
// anything else a lettered chip.
function lwArtifactCard(a, opts) {
  opts = opts || {};
  const kind = a.kind || "prop";
  const isDeck = kind.includes("deck");
  const isCard = kind.includes("card") && !isDeck;
  const visual = isDeck
    ? `<span class="lw-obj lw-obj-deck"><span class="pcard back"><span class="pcard-weave"></span></span><span class="pcard back"><span class="pcard-weave"></span></span><span class="pcard back"><span class="pcard-weave"></span></span></span>`
    : isCard
      ? `<span class="lw-obj"><span class="pcard back"><span class="pcard-weave"></span></span></span>`
      : `<span class="lw-obj lw-obj-tile">${escapeHtml((a.name || "?")[0] || "?")}</span>`;
  return `<button class="lw-card lw-artifact-card" data-artifact="${escapeHtml(String(a.id))}" style="${opts.style || ""}">
    ${visual}
    <span class="lw-card-body">
      <span class="lw-card-name">${escapeHtml(a.name || "object")}</span>
      <span class="lw-card-foot">
        <span class="lw-kind">${escapeHtml(kind)}</span>
        ${a.sealed ? `<span class="lw-sealed">🔒 sealed</span>` : ""}
        ${a.public ? `<span class="lw-pub">public</span>` : ""}
        ${opts.roomTag || ""}
      </span>
    </span>
  </button>`;
}

// ---- Overview: the map, grouped and coloured by room ---------------------
function lwGroupHtml(name, sub, style, agents, artifacts, roomAttr) {
  const cards = [
    ...agents.map((a) => lwAgentCard(a, {})),
    ...artifacts.map((a) => lwArtifactCard(a, {})),
  ].join("");
  const openBtn = roomAttr ? `<button class="lw-group-open" ${roomAttr}>open room →</button>` : "";
  return `<section class="lw-group" style="${style}">
    <header class="lw-group-head">
      <span class="lw-swatch"></span>
      <span class="lw-group-name">${escapeHtml(name)}</span>
      ${sub ? `<span class="lw-group-sub">${escapeHtml(sub)}</span>` : ""}
      <span class="lw-group-count">${agents.length + artifacts.length}</span>
      ${openBtn}
    </header>
    <div class="lw-group-cards">${cards || `<p class="lw-hint">empty — place people and objects here</p>`}</div>
  </section>`;
}

function renderOverview() {
  const stage = $("#lwStage");
  const rooms = (lwWorld && lwWorld.rooms) || [];
  const agents = (lwWorld && lwWorld.agents) || [];
  const artifacts = (lwWorld && lwWorld.artifacts) || [];

  const legend = rooms.length
    ? `<div class="lw-legend">${rooms.map((r, i) => {
        const nA = agents.filter((a) => String(a.room) === String(r.id)).length;
        const nO = artifacts.filter((a) => String(a.room) === String(r.id)).length;
        return `<button class="lw-legend-chip" data-room="${escapeHtml(String(r.id))}" style="${lwRoomStyle(i)}">
          <span class="lw-swatch"></span>
          <span class="lw-legend-name">${escapeHtml(r.name || r.type || "room")}</span>
          <span class="lw-legend-type">${escapeHtml(r.type || r.theme || "")}</span>
          <span class="lw-legend-count">${nA}👤 ${nO}▢</span>
        </button>`;
      }).join("")}</div>`
    : `<p class="lw-hint">No rooms yet — add one in the Rooms tab, then place people and objects into it.</p>`;

  const groups = [];
  rooms.forEach((r, i) => {
    const ga = agents.filter((a) => String(a.room) === String(r.id));
    const go = artifacts.filter((a) => String(a.room) === String(r.id));
    groups.push(lwGroupHtml(r.name || r.type || "room", r.type || r.theme || "", lwRoomStyle(i),
      ga, go, `data-room="${escapeHtml(String(r.id))}"`));
  });
  const upA = agents.filter((a) => a.room == null);
  const upO = artifacts.filter((a) => a.room == null);
  if (upA.length || upO.length)
    groups.push(lwGroupHtml("Unplaced", "not in any room yet",
      "--rc:var(--faint); --rc-soft:var(--card-2); --rc-line:var(--line-soft)", upA, upO, ""));

  stage.innerHTML = `<div class="lw-overview">
    <div class="lw-overview-head">
      <h3>The map</h3>
      <span class="lw-hint">${agents.length} people · ${artifacts.length} objects · ${rooms.length} rooms — coloured by room</span>
    </div>
    ${legend}
    <div class="lw-groups">${groups.join("") || `<p class="lw-hint">Nothing here yet. Create people and objects, add rooms, then place them.</p>`}</div>
  </div>`;

  stage.querySelectorAll(".lw-legend-chip").forEach((b) =>
    b.addEventListener("click", () => openRoom(b.dataset.room)));
  stage.querySelectorAll(".lw-group-open").forEach((b) =>
    b.addEventListener("click", () => openRoom(b.dataset.room)));
  stage.querySelectorAll("[data-agent]").forEach((b) =>
    b.addEventListener("click", () => openPersonDrawer(b.dataset.agent)));
  stage.querySelectorAll("[data-artifact]").forEach((b) =>
    b.addEventListener("click", () => lwOpenArtifactPeek(b.dataset.artifact)));
}

// ---- Agents tab: a gallery + the brief-based composer --------------------
function renderAgentsTab() {
  const stage = $("#lwStage");
  const agents = (lwWorld && lwWorld.agents) || [];
  const rooms = (lwWorld && lwWorld.rooms) || [];
  const idx = lwRoomIndex();
  if (!agents.length) {
    stage.innerHTML = `<div class="lw-gallery-empty">
      <p>No people yet. Everyone here is authored from a short brief — write who they are and the rest is filled in.</p>
      <button class="primary" id="lwAgentNew2">Create your first person</button>
    </div>`;
    $("#lwAgentNew2").addEventListener("click", openLwAgentComposer);
    return;
  }
  const cards = agents.map((a) => {
    const room = a.room != null ? rooms.find((r) => String(r.id) === String(a.room)) : null;
    const style = a.room != null ? lwRoomStyle(idx[String(a.room)] ?? 0) : "";
    const roomTag = room
      ? `<span class="lw-card-room">in ${escapeHtml(room.name || "a room")}</span>`
      : `<span class="lw-card-room unplaced">unplaced</span>`;
    return lwAgentCard(a, { style, roomTag });
  }).join("");
  stage.innerHTML = `<div class="lw-gallery">${cards}</div>`;
  stage.querySelectorAll("[data-agent]").forEach((b) =>
    b.addEventListener("click", () => openPersonDrawer(b.dataset.agent)));
}

let lwAgentDraft = null;
function openLwAgentComposer() { lwAgentDraft = { name: "", brief: "", parentsOn: false, parents: [] }; renderLwAgentComposer(); }
function renderLwAgentComposer() {
  const box = $("#lwDetail");
  const d = lwAgentDraft;
  const others = (lwWorld && lwWorld.agents) || [];
  box.hidden = false;
  box.className = "studio-detail composing";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem" style="background:${sigil(d.name || "New", "anthropic")}">
        <span class="fig-initial">${escapeHtml((d.name || "?")[0] || "?")}</span></div>
      <div style="flex:1">
        <input class="sc-name" id="lwANameInput" placeholder="Name (or leave blank)"
               value="${escapeHtml(d.name)}" autocomplete="off">
        <p class="sc-preview">a new person, authored from your brief</p>
      </div>
      <button class="sd-close" id="lwAClose">✕</button>
    </div>
    <div class="sc-label">Who is this person?</div>
    <textarea class="sc-input" id="lwABrief" rows="4"
      placeholder="A cautious accountant who loves poker on weekends… a sentence or two is plenty; the LLM authors the rest.">${escapeHtml(d.brief)}</textarea>
    <div class="sc-label">Choose parents <span class="dim">(optional — breed from two existing people)</span></div>
    <label class="lw-toggle"><input type="checkbox" id="lwParentsOn" ${d.parentsOn ? "checked" : ""}>
      <span>Breed from existing people</span></label>
    ${d.parentsOn ? (others.length
      ? `<div class="sc-row">${others.map((a) =>
          `<button class="sc-chip${d.parents.includes(String(a.id)) ? " on" : ""}" data-parent="${escapeHtml(String(a.id))}">${escapeHtml(a.name || "someone")}</button>`).join("")}</div>
         <p class="sc-hint">${d.parents.length}/2 chosen</p>`
      : `<p class="sc-hint">No existing people to breed from yet.</p>`) : ""}
    <div class="sc-actions"><button class="primary" id="lwACreate">Bring them to life</button></div>`;
  $("#lwANameInput").addEventListener("input", (e) => {
    d.name = e.target.value;
    box.querySelector(".fig-initial").textContent = (d.name || "?")[0] || "?";
  });
  $("#lwABrief").addEventListener("input", (e) => { d.brief = e.target.value; });
  $("#lwParentsOn").addEventListener("change", (e) => { d.parentsOn = e.target.checked; if (!d.parentsOn) d.parents = []; renderLwAgentComposer(); });
  box.querySelectorAll("[data-parent]").forEach((b) =>
    b.addEventListener("click", () => {
      const id = b.dataset.parent;
      if (d.parents.includes(id)) d.parents = d.parents.filter((x) => x !== id);
      else if (d.parents.length < 2) d.parents = [...d.parents, id];
      renderLwAgentComposer();
    }));
  $("#lwAClose").addEventListener("click", () => { lwAgentDraft = null; box.hidden = true; });
  $("#lwACreate").addEventListener("click", doCreateAgent);
}
async function doCreateAgent() {
  const d = lwAgentDraft;
  const agents = (lwWorld && lwWorld.agents) || [];
  const body = { name: d.name.trim(), brief: d.brief.trim() };
  if (d.parentsOn && d.parents.length) {
    body.parents = d.parents.slice(0, 2).map((pid) => {
      const a = agents.find((x) => String(x.id) === pid);
      return a ? a.id : pid;
    });
  }
  try {
    await api(`/api/lw/${lwWorldId}/human`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    lwAgentDraft = null; $("#lwDetail").hidden = true;
    await renderWorkspace();
  } catch (e) { toast(`Could not create them: ${e.message}`); }
}

// ---- Artifacts tab: a gallery + the brief-based composer -----------------
function renderArtifactsTab() {
  const stage = $("#lwStage");
  const artifacts = (lwWorld && lwWorld.artifacts) || [];
  const rooms = (lwWorld && lwWorld.rooms) || [];
  const idx = lwRoomIndex();
  if (!artifacts.length) {
    stage.innerHTML = `<div class="lw-gallery-empty">
      <p>No objects yet. Describe a thing in a line — "a deck of cards", "a whiteboard" — and it is authored into the world.</p>
      <button class="primary" id="lwArtNew2">Create your first object</button>
    </div>`;
    $("#lwArtNew2").addEventListener("click", openLwArtifactComposer);
    return;
  }
  const cards = artifacts.map((a) => {
    const room = a.room != null ? rooms.find((r) => String(r.id) === String(a.room)) : null;
    const style = a.room != null ? lwRoomStyle(idx[String(a.room)] ?? 0) : "";
    const roomTag = room
      ? `<span class="lw-card-room">in ${escapeHtml(room.name || "a room")}</span>`
      : `<span class="lw-card-room unplaced">unplaced</span>`;
    return lwArtifactCard(a, { style, roomTag });
  }).join("");
  stage.innerHTML = `<div class="lw-gallery">${cards}</div>`;
  stage.querySelectorAll("[data-artifact]").forEach((b) =>
    b.addEventListener("click", () => lwOpenArtifactPeek(b.dataset.artifact)));
}

let lwArtifactDraft = null;
function openLwArtifactComposer() { lwArtifactDraft = { name: "", brief: "" }; renderLwArtifactComposer(); }
function renderLwArtifactComposer() {
  const box = $("#lwDetail");
  const d = lwArtifactDraft;
  box.hidden = false;
  box.className = "studio-detail composing";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem" style="background:${sigil(d.name || "Object", "anthropic")}">
        <span class="fig-initial">▢</span></div>
      <div style="flex:1">
        <input class="sc-name" id="lwArtNameInput" placeholder="Name this object"
               value="${escapeHtml(d.name)}" autocomplete="off">
        <p class="sc-preview">a thing in the world</p>
      </div>
      <button class="sd-close" id="lwArtClose">✕</button>
    </div>
    <div class="sc-label">What is this thing?</div>
    <textarea class="sc-input" id="lwArtBrief" rows="3"
      placeholder="e.g. a deck of cards; a whiteboard; a coffee machine">${escapeHtml(d.brief)}</textarea>
    <div class="sc-actions"><button class="primary" id="lwArtCreate">Make it</button></div>`;
  $("#lwArtNameInput").addEventListener("input", (e) => { d.name = e.target.value; });
  $("#lwArtBrief").addEventListener("input", (e) => { d.brief = e.target.value; });
  $("#lwArtClose").addEventListener("click", () => { lwArtifactDraft = null; box.hidden = true; });
  $("#lwArtCreate").addEventListener("click", doCreateArtifact);
}
async function doCreateArtifact() {
  const d = lwArtifactDraft;
  try {
    await api(`/api/lw/${lwWorldId}/artifact`, { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: d.name.trim(), brief: d.brief.trim() }) });
    lwArtifactDraft = null; $("#lwDetail").hidden = true;
    await renderWorkspace();
  } catch (e) { toast(`Could not make it: ${e.message}`); }
}

// A light peek at an object — name, kind, where it sits, whether it is sealed.
function lwOpenArtifactPeek(aid) {
  const artifacts = (lwWorld && lwWorld.artifacts) || [];
  const a = artifacts.find((x) => String(x.id) === String(aid));
  if (!a) return;
  const rooms = (lwWorld && lwWorld.rooms) || [];
  const room = a.room != null ? rooms.find((r) => String(r.id) === String(a.room)) : null;
  const box = $("#lwDetail");
  box.hidden = false; box.className = "studio-detail";
  box.innerHTML = `
    <div class="sd-head">
      <div class="lw-obj lw-obj-tile lw-head-obj">${escapeHtml((a.name || "?")[0] || "?")}</div>
      <div style="flex:1"><h3>${escapeHtml(a.name || "object")}</h3>
        <p class="sd-persona">${escapeHtml(a.kind || "object")}${room ? ` · in ${escapeHtml(room.name || "a room")}` : " · unplaced"}</p></div>
      <button class="sd-close" id="lwObjClose">✕</button>
    </div>
    <div class="sd-facts">
      <span>${a.sealed ? "🔒 sealed" : "open"}</span>
      <span>${a.public ? "public" : "private"}</span>
      <span>${escapeHtml(a.kind || "object")}</span>
    </div>`;
  $("#lwObjClose").addEventListener("click", () => { box.hidden = true; });
}

// ---- Rooms tab: room cards + the type-picking composer -------------------
function renderRoomsTab() {
  const stage = $("#lwStage");
  const rooms = (lwWorld && lwWorld.rooms) || [];
  if (!rooms.length) {
    stage.innerHTML = `<div class="lw-gallery-empty">
      <p>No rooms yet. A room is a place with a type — a home, a classroom, an office, a casino — where your people gather.</p>
      <button class="primary" id="lwRoomNew2">Add your first room</button>
    </div>`;
    $("#lwRoomNew2").addEventListener("click", openLwRoomComposer);
    return;
  }
  const agents = (lwWorld && lwWorld.agents) || [];
  const artifacts = (lwWorld && lwWorld.artifacts) || [];
  const cards = rooms.map((r, i) => {
    const nA = agents.filter((a) => String(a.room) === String(r.id)).length;
    const nO = artifacts.filter((a) => String(a.room) === String(r.id)).length;
    const blurb = (lwRoomTypes.find((t) => t.type === r.type) || {}).blurb || r.blurb || "";
    return `<button class="lw-room-card" data-room="${escapeHtml(String(r.id))}" style="${lwRoomStyle(i)}">
      <span class="lw-room-card-name">${escapeHtml(r.name || "room")}</span>
      <span class="lw-room-card-type">${escapeHtml(r.type || "")}${r.theme ? ` · ${escapeHtml(r.theme)}` : ""}</span>
      ${blurb ? `<span class="lw-room-card-blurb">${escapeHtml(blurb)}</span>` : ""}
      <span class="lw-room-card-foot">${nA} seated · ${nO} objects</span>
    </button>`;
  }).join("");
  stage.innerHTML = `<div class="lw-room-grid">${cards}</div>`;
  stage.querySelectorAll("[data-room]").forEach((b) => {
    b.addEventListener("click", () => openRoom(b.dataset.room));
    b.addEventListener("contextmenu", (ev) => lwRoomCardMenu(ev, rooms.find((r) => String(r.id) === b.dataset.room)));
  });
}

function lwRoomCardMenu(ev, r) {
  ev.preventDefault(); ev.stopPropagation();
  if (!r) return;
  studioMenu(ev.clientX, ev.clientY, [
    { label: `Open ${r.name || "room"}`, act: () => openRoom(r.id) },
  ]);
}

let lwRoomDraft = null;
async function openLwRoomComposer() {
  lwRoomDraft = { name: "", type: "" };
  if (!lwRoomTypes.length) {
    try { const d = await api(`/api/lw/${lwWorldId}/room-types`); lwRoomTypes = d.types || []; }
    catch (e) { /* fall back to whatever the overview provided */ }
  }
  if (!lwRoomDraft.type && lwRoomTypes[0]) lwRoomDraft.type = lwRoomTypes[0].type;
  renderLwRoomComposer();
}
function renderLwRoomComposer() {
  const box = $("#lwDetail");
  const d = lwRoomDraft;
  const cur = lwRoomTypes.find((t) => t.type === d.type);
  box.hidden = false;
  box.className = "studio-detail composing";
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem" style="background:${sigil(d.name || d.type || "Room", "anthropic")}">
        <span class="fig-initial">🚪</span></div>
      <div style="flex:1">
        <input class="sc-name" id="lwRNameInput" placeholder="Name this room"
               value="${escapeHtml(d.name)}" autocomplete="off">
        <p class="sc-preview">${escapeHtml(cur ? (cur.blurb || cur.type) : "a place for your people")}</p>
      </div>
      <button class="sd-close" id="lwRClose">✕</button>
    </div>
    <div class="sc-label">Type</div>
    <div class="sc-row">${lwRoomTypes.length
      ? lwRoomTypes.map((t) =>
          `<button class="sc-chip${d.type === t.type ? " on" : ""}" data-rtype="${escapeHtml(t.type)}" title="${escapeHtml(t.blurb || "")}">${escapeHtml(t.type)}</button>`).join("")
      : `<span class="sc-hint">No room types available.</span>`}</div>
    ${cur && cur.blurb ? `<p class="sc-hint">${escapeHtml(cur.blurb)}</p>` : ""}
    <div class="sc-actions"><button class="primary" id="lwRCreate">Add the room</button></div>`;
  $("#lwRNameInput").addEventListener("input", (e) => { d.name = e.target.value; });
  box.querySelectorAll("[data-rtype]").forEach((b) =>
    b.addEventListener("click", () => { d.type = b.dataset.rtype; renderLwRoomComposer(); }));
  $("#lwRClose").addEventListener("click", () => { lwRoomDraft = null; box.hidden = true; });
  $("#lwRCreate").addEventListener("click", doCreateRoom);
}
async function doCreateRoom() {
  const d = lwRoomDraft;
  try {
    const r = await api(`/api/lw/${lwWorldId}/room`, { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: d.name.trim() || (d.type || "Room"), type: d.type }) });
    lwRoomDraft = null; $("#lwDetail").hidden = true;
    const rid = r.room && r.room.id != null ? r.room.id : null;
    await loadWorld();
    if (rid != null) { lwTab = "rooms"; lwRoomId = rid; lwSeenLog = new Set(); await renderRoomView(); }
    else renderRoomsTab();
  } catch (e) { toast(`Could not add the room: ${e.message}`); }
}

// ---- the room view (themed by room.theme) --------------------------------
async function openRoom(rid) {
  lwTab = "rooms"; lwRoomId = rid; lwRoom = null; lwSeenLog = new Set();
  $("#lwDetail").hidden = true;
  await renderRoomView();
}

// The room is a Konva canvas now, not a themed CSS set-piece. Fetch, then paint.
async function renderRoomView() {
  let d;
  try { d = await api(`/api/lw/${lwWorldId}/room/${lwRoomId}`); }
  catch (e) { $("#lwStage").innerHTML = `<p class="empty">Could not open room: ${escapeHtml(e.message || String(e))}</p>`; return; }
  lwRenderRoom(d.room || d);
}

// Re-fetch this room and repaint — used after a seat/unseat/create/round, where
// the server is the source of truth. The per-room view cache keeps pan/zoom.
async function lwReloadRoom() {
  lwLogOn && lwLog("life", "reload room (refetch + full repaint)", null, "info");
  try { const d = await api(`/api/lw/${lwWorldId}/room/${lwRoomId}`); lwRenderRoom(d.room || d); }
  catch (e) { toast(`Could not refresh the room: ${e.message}`); }
}

// Paint the scene: a full-screen canvas with a floating toolbox, a video-style time
// transport, and a rules button — nothing else on top of the floor. Free strings reach
// innerHTML through escapeHtml; on-canvas labels are Konva text (drawn to canvas).
function lwRenderRoom(room) {
  if (lwKonva && lwKonva.drag) { lwLog("life", "rebuild while dragging — aborting the drag first", null, "warn"); lwForceIdle(); }
  lwDestroyCanvas();
  lwRoom = room;
  const stage = $("#lwStage");
  const agents = room.agents || room.seats || [];
  const props = room.props || [];
  // reflect this scene in the top bar (never clobber the title mid-edit)
  const t = $("#sdTitle"); if (t && document.activeElement !== t) t.textContent = room.name || "untitled";
  paintLwLive();

  stage.innerHTML = `<div class="sd-room">
    <div class="lw-canvas-wrap sd-canvas" id="lwCanvasWrap">
      <div class="lw-konva-host" id="lwKonvaHost"></div>
      <div class="lw-overlay" id="lwOverlay"></div>
      <div class="sd-hint">drag a token to move it · drag empty to select · <b>space</b>-drag to pan · scroll to zoom · <b>F</b> fit</div>
      <div class="lw-dock sd-dock" id="lwDock">${lwDockHtml()}</div>
      <button class="sd-rules-btn" id="sdRulesBtn" title="Scene rules — obeyed on every run">⚖ Rules</button>
      <div class="sd-rules-pop" id="sdRulesPop" hidden></div>
      <div class="sd-activity" id="sdActivity"${sdActOpen ? "" : " hidden"}></div>
      ${sdTimeBarHtml()}
    </div>`;

  lwWireDock();
  sdWireTime();
  paintLwTau();
  sdSavedIdle();
  const rb = $("#sdRulesBtn"); if (rb) rb.addEventListener("click", sdToggleRules);
  if (sdActOpen) sdShowActivity(true);      // refresh the activity panel after a beat
  lwMountCanvas(room, agents, props);

  // Everything on screen is now "seen"; only genuinely new lines animate next time.
  lwSeenLog = new Set((room.log || []).map((l) => String(l.n)));
}

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
  if (s.kind === "agent") openPersonDrawer(s.entry.data.id, s.entry.data.name);
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
        if (tk.type === "agent") openPersonDrawer(tk.id, tk.entry.data.name);
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
  const items = [{ label: `Peek ${a.name || "them"}`, act: () => openPersonDrawer(a.id, a.name) }];
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
async function openPersonDrawer(hid, name) {
  const box = $("#lwDetail");
  box.hidden = false;
  box.className = "studio-detail";
  box.innerHTML = `<p class="dim">reading ${escapeHtml(name || "them")}…</p>`;
  let d;
  try { d = await api(`/api/lw/${lwWorldId}/human/${hid}`); }
  catch (e) {
    box.innerHTML = `<p class="dim">Could not read them: ${escapeHtml(e.message || String(e))}</p>
      <div class="sc-actions"><button id="lwDClose">Close</button></div>`;
    $("#lwDClose") && $("#lwDClose").addEventListener("click", () => { box.hidden = true; });
    return;
  }
  const h = d.human || {};
  const mood = h.mood || {};
  const want = dominantWant(h.wants);
  const skills = Array.isArray(h.skills)
    ? h.skills.slice().sort((a, b) => Number(b[1]) - Number(a[1])).slice(0, 5) : [];
  const resume = h.resume || {};
  const pins = Array.isArray(resume.pins) ? resume.pins : [];
  const habits = Array.isArray(d.habits) ? d.habits : [];
  const bonds = (d.bonds && typeof d.bonds === "object") ? d.bonds : {};
  const hand = Array.isArray(d.hand) ? d.hand : [];
  const pinText = (p) => typeof p === "object"
    ? (p.text || p.what || p.name || JSON.stringify(p)) : String(p);
  box.innerHTML = `
    <div class="sd-head">
      <div class="fig-emblem lw-av-emblem"><img alt="" src="${lwSvgUri(lwAvatarSvg(lwAvatarSeed({ name: h.name || name, id: hid, figure: h.figure }), 48))}"></div>
      <div style="flex:1"><h3>${escapeHtml(h.name || name || "someone")}</h3>
        <p class="sd-persona">${escapeHtml(d.narrative || h.narrative || "no story yet")}</p></div>
      <button class="sd-close" id="lwDClose">✕</button>
    </div>
    <div class="sd-facts">
      <span>τ ${escapeHtml(String(h.tau ?? 0))}</span>
      <span>${escapeHtml(String(h.memories ?? 0))} memories</span>
      <span>${escapeHtml(String(h.habits ?? habits.length))} habits</span>
    </div>

    <div class="sd-label">Mood</div>
    <div class="lw-meters">
      ${lwMeter("confidence", mood.confidence)}
      ${lwMeter("stress", mood.stress)}
      ${lwMeter("hope", mood.hope)}
      ${lwMeter("focus", mood.focus)}
    </div>

    ${want ? `<div class="sd-label">Wants</div>
      <div class="lw-want-big">▸ ${escapeHtml(want.name)}${
        want.pressure != null ? ` <span class="dim">pressure ${lwPct(want.pressure)}</span>` : ""}</div>` : ""}

    ${skills.length ? `<div class="sd-label">Top skills</div>
      <div class="sc-row">${skills.map((s) =>
        `<span class="sc-chip on">${escapeHtml(String(s[0]))} <i>${escapeHtml(String(s[1]))}</i></span>`).join("")}</div>` : ""}

    <div class="sd-label">Résumé ledger ${resume.intact
      ? `<span class="lw-verified">verified ✓</span>` : `<span class="lw-broken">unverified</span>`}</div>
    ${resume.head ? `<p class="lw-resume-head">${escapeHtml(String(resume.head))}</p>` : ""}
    ${pins.length
      ? `<div class="lw-pins">${pins.map((p) => `<span class="lw-pin">📌 ${escapeHtml(pinText(p))}</span>`).join("")}</div>`
      : `<p class="dim">no pinned achievements yet</p>`}

    ${habits.length ? `<div class="sd-label">Compiled habits</div>
      <div class="lw-habits">${habits.map((hb) => `<div class="lw-habit">
        <span class="lw-habit-when">when ${escapeHtml(String(hb.when))}</span>
        <span class="lw-habit-meta">conf ${lwPct(hb.confidence)} · fired ${escapeHtml(String(hb.fires ?? 0))}×</span>
      </div>`).join("")}</div>` : ""}

    ${Object.keys(bonds).length ? `<div class="sd-label">Bonds</div>
      <div class="lw-bonds">${Object.entries(bonds).map(([oid, b]) => `<div class="lw-bond">
        <span class="lw-bond-name">${escapeHtml(lwNameOf(oid))}</span>
        <span class="lw-bond-meta">trust ${lwPct(b && b.trust)} · warmth ${lwPct(b && b.warmth)}</span>
      </div>`).join("")}</div>` : ""}

    <div class="sd-label">Their hand <span class="dim">(the operator's privilege — a card's value is a secret only its holder can read)</span></div>
    <div class="lw-hand">${hand.length
      ? hand.map((c) => lwHandCardHtml(c.value)).join("")
      : `<span class="dim">empty-handed</span>`}</div>`;
  $("#lwDClose").addEventListener("click", () => { box.hidden = true; });
}

// --- Lifeworld wiring (elements exist: app.js loads at the end of <body>). ---
$("#modeLifeworld") && $("#modeLifeworld").addEventListener("click", () => openLifeworld());
$("#lwBack") && $("#lwBack").addEventListener("click", () => { sdPause(); lwDestroyCanvas(); showHome(); });
$("#lwToLobby") && $("#lwToLobby").addEventListener("click", () => {
  lwDestroyCanvas();
  lwWorldId = null; lwWorld = null; lwRoomId = null; lwRoom = null; lwTab = "overview";
  setHash("#/lifeworld"); renderLifeworld();
});
$("#lwNewWorld") && $("#lwNewWorld").addEventListener("click", openWorldComposer);
$("#lwNewAgent") && $("#lwNewAgent").addEventListener("click", openLwAgentComposer);
$("#lwNewArtifact") && $("#lwNewArtifact").addEventListener("click", openLwArtifactComposer);
$("#lwNewRoom") && $("#lwNewRoom").addEventListener("click", openLwRoomComposer);
$("#lwLive") && $("#lwLive").addEventListener("click", () => {
  lwLive = !lwLive; paintLwLive();
  const n = document.querySelector(".lw-cost-note");
  if (n) n.textContent = lwLive
    ? "🧠 Live — a round asks real models to think and spends tokens"
    : "💤 Deterministic — free, reproducible reflexes";
});
document.querySelectorAll(".lw-tab").forEach((b) =>
  b.addEventListener("click", () => selectLwTab(b.dataset.lwtab)));

// --- Studio top-bar wiring (the bar elements are static inside #lifeworld) ---
(() => {
  const title = $("#sdTitle");
  if (title) {
    const commit = () => {
      const v = (title.textContent || "").replace(/\s+/g, " ").trim() || "untitled";
      title.textContent = v;
      if (lwRoom && v !== (lwRoom.name || "")) sdRenameScene(v);
    };
    title.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); title.blur(); } });
    title.addEventListener("blur", commit);
  }
  $("#sdScenesBtn") && $("#sdScenesBtn").addEventListener("click", (e) => { e.stopPropagation(); sdToggleScenes(); });
  $("#sdSceneDel") && $("#sdSceneDel").addEventListener("click", (e) => { e.stopPropagation(); sdDeleteCurrentScene(); });
  $("#sdActBtn") && $("#sdActBtn").addEventListener("click", sdToggleActivity);
  $("#sdThreadsBtn") && $("#sdThreadsBtn").addEventListener("click", () => sdOpenThreads());
  $("#sdRoster") && $("#sdRoster").addEventListener("click", sdOpenRoster);
  document.addEventListener("click", (e) => {          // click-away closes the scenes menu
    const menu = $("#sdScenesMenu");
    if (menu && !menu.hidden && !e.target.closest("#sdScenesMenu") && !e.target.closest("#sdScenesBtn")) menu.hidden = true;
  });
  document.addEventListener("keydown", (e) => {        // ⌘/Ctrl+S saves; Esc closes the Cast
    if ($("#lifeworld").hidden) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") { e.preventDefault(); sdSaveNow(); return; }
    if (e.key === "Escape") { const rh = $("#sdRosterHost"); if (rh && !rh.hidden) { rh.hidden = true; return; } const sm = $("#sdScenesMenu"); if (sm && !sm.hidden) sm.hidden = true; }
  });
})();


async function boot() {
  if (!(await loadMe())) return;      // show login screen until signed in
  await loadHealth();
  loadRepos();
  route();                    // restore whatever the URL points at
  paintAmbition();
  paintAutonomy();
  refreshBell();
  setInterval(refreshBell, 20000);
  if (!ws) connectWs();
  if (window.Notification && Notification.permission === "default")
    setTimeout(() => Notification.requestPermission(), 1500);
}
boot();
setInterval(() => { if (currentProject) refreshBoard(); else loadProjects(); }, 10000);
