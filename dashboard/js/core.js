// core.js — Core — sign-in, settings, credential checks, URL routing. Shared plumbing every screen uses ($ and api live here; toast/escapeHtml and the rest of the shared kit live in js/lib.js, the deploy/ops screens in js/ops.js — all scripts share one global scope).
// Split from the old monolithic app.js (order preserved; classic scripts share one global scope; index.html defines load order).

const $ = (s) => document.querySelector(s);

// Every full-screen section. Six functions used to each carry their own copy of this list,
// which is how a new screen ends up visible underneath another one.
const SCREENS = ["#home", "main", "#plan", "#studio", "#scenes", "#lifeworld",
                 "#aboutPage", "#selfPage", "#agentPage"];
function hideScreens(except) {
  for (const sel of SCREENS) {
    if (sel === except) continue;
    const e = document.querySelector(sel);
    if (e) e.hidden = true;
  }
}
let currentProject = null;
let ws = null;

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
  // Self-repair squash-merges to main, so what it produces is a COMMIT, not a pull request —
  // and until this existed there was no way to click through to anything it had shipped.
  if (kind === "commit") return `${gitWeb}/${repo}/commit/${value}`;
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
/** Report an error the UI CAUGHT, and hand back the message to show.
 *
 * window.onerror only sees what nobody caught. Every `catch (e) { show(e.message) }` in this
 * dashboard was therefore invisible to the error pipeline — which is exactly how a
 * "rpLogLine is not defined" sat on screen for an hour without ever becoming a log row, a
 * notice, or a bug. Catching an error to render it nicely must not also hide it.
 */
function reportCaught(e, where) {
  const msg = errText(e);
  try {
    reportClientError(`${msg} [caught in ${where}]`,
                      (e && e.stack) || "", location.hash || location.pathname);
  } catch { /* reporting must never be the thing that breaks */ }
  return msg;
}

/** Something a person can read, out of whatever was thrown.
 *
 * `String(e)` on a plain object is "[object Object]", and that is precisely what the error
 * pipeline recorded for a week: a critical notice whose entire content was the word Object.
 * An error report that does not say what went wrong is a false sense of coverage. */
function errText(e) {
  if (!e) return "unknown error";
  if (typeof e === "string") return e;
  if (e.message) return String(e.message);
  if (e.detail) return typeof e.detail === "string" ? e.detail : JSON.stringify(e.detail);
  if (e.error) return String(e.error);
  try {
    const j = JSON.stringify(e);
    return j && j !== "{}" ? j.slice(0, 300) : (e.constructor && e.constructor.name) || "unknown error";
  } catch { return "unserialisable error"; }
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

async function openSelfRepair(skipHash) {
  $("#home").hidden = true; $("main").hidden = true;
  const pl = $("#plan"); if (pl) pl.hidden = true;
  const ab = $("#aboutPage"); if (ab) ab.hidden = true;
  const scn = $("#scenes"); if (scn) scn.hidden = true;
  const lw = $("#lifeworld"); if (lw) lw.hidden = true;
  const agp = $("#agentPage"); if (agp) agp.hidden = true;
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
  hideScreens("#home");
  $("#home").hidden = false;
  paintHomeStats();
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
  if (typeof projSrc !== "undefined" && projSrc) projSrc.leave();   // back from Devteam HQ
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
                    "notices", "usage", "self"])
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
    setHash(currentProject === "devteam"
      ? `#/hq${view !== "command" ? "/" + view : ""}`
      : `#/p/${currentProject}${view !== "command" ? "/" + view : ""}`);
}

/** Put the live state on the doors.
 *
 * "Teams: arrange your agents" is a brochure. "3 teams · 6 agents working" is a reason to
 * click, and on a bad day it is the fastest way to notice something is wrong — which is what
 * a landing page is actually for. */
async function paintHomeStats() {
  const set = (id, text) => { const e = $(id); if (e) e.textContent = text; };
  try {
    const ps = await api("/api/projects");
    const live = ps.filter((p) => ["running", "planning"].includes(p.status)).length;
    set("#statProjects", ps.length
      ? `${ps.length} project${ps.length === 1 ? "" : "s"}${live ? ` · ${live} running` : ""}`
      : "none yet");
  } catch { set("#statProjects", ""); }
  try {
    const w = await api("/api/lw");
    const n = (w.worlds || []).length;
    set("#statTeams", n ? `${n} team${n === 1 ? "" : "s"}` : "none yet — make one");
  } catch { set("#statTeams", ""); }
  try {
    const d = await api("/api/repair/status");
    const busy = (d.meters && d.meters.team || []).length;
    const phase = d.enabled ? ((d.state || {}).phase || "idle") : "off";
    set("#statDevteam", `${phase}${busy ? ` · ${busy} on the crew` : ""}`
      + (d.notices && d.notices.total ? ` · ${d.notices.total} need you` : ""));
  } catch { set("#statDevteam", ""); }
}

function route() {
  if (suppressHash) return;
  // Any route that is not the agent page must first put it away. Screens are siblings that
  // hide each other, so a section left visible sits on top of whatever comes next — the
  // "messed up" studio was this page still open behind it.
  if (!location.hash.startsWith("#/agent/")) {
    const ag = $("#agentPage");
    if (ag && !ag.hidden) { ag.hidden = true; if (typeof agTimer !== "undefined" && agTimer) { clearInterval(agTimer); agTimer = null; } }
  }
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
  if (location.hash.startsWith("#/teams")) { openStudio(true); return; }   // its name now
  if (location.hash.startsWith("#/devteam")) { openDevteam(true); return; }
  {
    const sc = location.hash.match(/^#\/scenes(?:\/([\w-]+))?/);
    if (sc) { openScenes(true, sc[1] || null); return; }
  }
  if (location.hash.startsWith("#/lifeworld")) { openStudio(true); return; }   // legacy link
  if (location.hash.startsWith("#/about")) { openAbout(true); return; }
  if (location.hash.startsWith("#/improve")) { openSelfRepair(true); return; }
  {
    const hq = location.hash.match(/^#\/hq(?:\/(\w+))?/);
    if (hq) { openDevteamHQ(hq[1] || "command", true); return; }
  }
  {
    const ag = location.hash.match(/^#\/agent\/(\d+)(?:\/(\w+))?/);
    if (ag) { openAgentPage(Number(ag[1]), ag[2] || "now", true); return; }
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
