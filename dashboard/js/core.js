// core.js — Core — sign-in, settings, credential checks, URL routing. Shared plumbing every screen uses ($ and api live here; toast/escapeHtml live in js/projects.js — all six scripts share one global scope).
// Split from the old monolithic app.js (order preserved; classic scripts share one global scope; index.html defines load order).

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
// GIT_WEB/GIT_PROVIDER, filled in by loadHealth() below. Default to github.com so
// a page render that races the first /api/health call still links somewhere sane.
let gitWeb = "https://github.com";
let gitProvider = "github";

// The one place that turns a repo into a link — every screen calls this instead
// of string-building "https://github.com/..." itself, so a self-hosted GIT_WEB
// (Gitea, GitHub Enterprise) gets correct links everywhere, not just wherever
// someone remembered to parametrize it. `kind` picks the browser path Gitea
// diverges on ('pulls' not 'pull', 'src/branch' not 'tree'), mirroring
// conductor/app/github_client.py's repo_url/issue_url/pr_url/branch_url.
function gitWebUrl(repo, kind, value) {
  if (kind === "issue") return `${gitWeb}/${repo}/issues/${value}`;
  if (kind === "pr") return `${gitWeb}/${repo}/${gitProvider === "gitea" ? "pulls" : "pull"}/${value}`;
  if (kind === "branch") return `${gitWeb}/${repo}/${gitProvider === "gitea" ? "src/branch" : "tree"}/${value}`;
  return `${gitWeb}/${repo}`;
}

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
    gitWeb = h.git_web || gitWeb;
    gitProvider = h.git_provider || gitProvider;
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
      ? `<a href="${gitWebUrl(p.repo)}" target="_blank" onclick="event.stopPropagation()">${p.repo}</a>`
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

