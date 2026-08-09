// ops.js — The deploy/ops screens, split out of js/core.js so core stays the
// shell (auth, router, api, ws): the sandbox and stale-UI banners, the cloud
// instance panel, the healing log, preview environments and the sandbox screen.
// Everything here is dispatched at runtime — core.js's loadHealth() raises the
// banners and the self screens call the render*s once they are visible — so
// nothing runs at eval time beyond declaring functions and consts.
// Split from the old monolithic app.js via js/core.js (classic scripts share one
// global scope; index.html defines load order).

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
  // The command here was for a port this setup stopped using, so the one instruction on the
  // banner was wrong — and being told to run something that does not apply is worse than
  // being told nothing. Root can just press the button; anyone else gets the command.
  el.innerHTML = `<b>The app is half-updated.</b> This page has newer code than the
    server behind it, so some things may look empty or do nothing — that is not a
    real fault. Restart the server to fix it:
    <button class="rp-mini" id="staleRestart">⟳ Restart now</button>
    <code>./run-local.sh</code>`;
  document.body.prepend(el);
  const btn = $("#staleRestart");
  if (btn) btn.addEventListener("click", async () => {
    btn.disabled = true; btn.textContent = "restarting…";
    try {
      await api("/api/repair/restart", { method: "POST" });
      if (typeof waitForRestart === "function") waitForRestart();
      else setTimeout(() => location.reload(), 4000);
    } catch (e) {
      btn.disabled = false; btn.textContent = "⟳ Restart now";
      if (typeof toast === "function") toast(e.message);
    }
  });
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
