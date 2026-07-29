// boot.js — Wiring + boot — top-level event listeners and the boot() entry point. Loads last.
// Split from the old monolithic app.js (order preserved; classic scripts share one global scope; index.html defines load order).

// --- Lifeworld wiring (elements exist: the js/*.js classic scripts load at the end of <body>). ---
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
