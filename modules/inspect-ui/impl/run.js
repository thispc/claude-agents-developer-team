// The page, as a pure function of the Atlas payload.
//
// It touches nothing. No filesystem, no clock, no network, no state between
// calls — the same input gives the same bytes, which is what lets a conformance
// case pin it at all. The server that serves this page does the impure work.

const STATE_ORDER = ["refused", "unbuilt", "live"];

/**
 * @param {any} input
 * @returns {{html: string, stats: {nodes: number, live: number, refused: number, unbuilt: number, edges: number, dropped: number}}}
 */
export function page(input) {
  if (input === null || typeof input !== "object" || Array.isArray(input)) throw bad("input must be an object");
  const nodes = input.nodes;
  if (!Array.isArray(nodes)) throw bad("nodes must be an array");
  const edges = Array.isArray(input.edges) ? input.edges : [];
  const dropped = Array.isArray(input.dropped) ? input.dropped : [];
  const verdicts = Array.isArray(input.verdicts) ? input.verdicts : [];
  const title = typeof input.title === "string" ? input.title : "devteam";

  const stats = {
    nodes: nodes.length,
    live: nodes.filter((/** @type {any} */ n) => n.status === "live").length,
    refused: nodes.filter((/** @type {any} */ n) => n.status === "refused").length,
    unbuilt: nodes.filter((/** @type {any} */ n) => n.status === "unbuilt").length,
    edges: edges.length,
    dropped: dropped.length,
  };

  // Trouble first. A list sorted by name makes you hunt for the one thing that
  // needs attention; a list sorted by state puts it at the top where a glance
  // finds it.
  const ordered = [...nodes].sort((a, b) => {
    const s = STATE_ORDER.indexOf(a.status) - STATE_ORDER.indexOf(b.status);
    return s !== 0 ? s : String(a.id).localeCompare(String(b.id));
  });

  const html = [
    "<!doctype html>",
    '<html lang="en"><head><meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width,initial-scale=1">',
    `<title>${esc(title)}</title>`,
    `<style>${CSS}</style></head><body>`,
    header(title, stats),
    verdicts.length ? verdictPanel(verdicts) : "",
    `<main class="grid">${ordered.map(card).join("")}</main>`,
    edges.length ? edgeList(edges) : "",
    dropped.length ? droppedList(dropped) : "",
    footer(),
    `<script>${SCRIPT}</script>`,
    "</body></html>",
  ].join("");

  return { html, stats };
}

/** @param {string} title @param {any} s */
function header(title, s) {
  return `<header>
    <div class="masthead">
      <h1>${esc(title)}</h1>
      <button id="verify" type="button">Verify all</button>
    </div>
    <div class="counts">
      ${chip("live", s.live, "ok")}
      ${s.refused ? chip("refused", s.refused, "no") : ""}
      ${s.unbuilt ? chip("not built", s.unbuilt, "idle") : ""}
      ${chip("edges", s.edges, "idle")}
      ${s.dropped ? chip("dropped", s.dropped, "no") : ""}
    </div>
    <p id="status" class="status" role="status">Nothing has been run from here yet.</p>
  </header>`;
}

/** @param {string} label @param {number} n @param {string} kind */
function chip(label, n, kind) {
  return `<span class="chip ${kind}"><b>${n}</b>${esc(label)}</span>`;
}

/** @param {any} n */
function card(n) {
  const alts = Array.isArray(n.alternatives) ? n.alternatives : [];
  return `<article class="card ${esc(n.status)}">
    <div class="card-top">
      <h2>${esc(n.label ?? n.id)}</h2>
      <span class="state ${esc(n.status)}">${esc(n.status)}</span>
    </div>
    <dl>
      ${row("language", n.language ? esc(n.language) : "&mdash;")}
      ${row("size", n.loc != null ? `${n.loc} lines` : "&mdash;")}
      ${row("surface", n.surface != null ? String(n.surface) : "&mdash;")}
      ${row("contract", n.contract ? `<code>${esc(short(n.contract))}</code>` : "&mdash;")}
      ${row("artifact", n.artifact ? `<code>${esc(short(n.artifact))}</code>` : "&mdash;")}
    </dl>
    ${n.proved?.length ? `<p class="proved">proved: ${n.proved.map((/** @type {string} */ g) => `<span>${esc(g)}</span>`).join("")}</p>` : ""}
    ${n.note ? `<p class="note">${esc(n.note)}</p>` : ""}
    ${alts.length > 1 ? alternatives(alts) : ""}
    ${evidence(n.evidence)}
  </article>`;
}

/** The relation, made visible: one contract, several admitted implementations. @param {any[]} alts */
function alternatives(alts) {
  return `<div class="alts"><span class="alts-label">${alts.length} artifacts satisfy this contract</span>${
    alts.map((a) => `<span class="alt${a.live ? " live" : ""}">${esc(a.language ?? "?")} <code>${esc(short(a.artifact ?? ""))}</code>${a.loc != null ? ` ${a.loc}ln` : ""}</span>`).join("")
  }</div>`;
}

/** @param {any[]|undefined} ev */
function evidence(ev) {
  if (!Array.isArray(ev) || ev.length === 0) {
    // A node with no evidence should never have reached this module — render-graph
    // drops those. Saying so is better than rendering a card that looks complete.
    return `<p class="noev">no evidence — this node should not have been drawn</p>`;
  }
  return `<ul class="ev">${ev.map((e) => `<li><code>${esc(e.file)}:${esc(String(e.line))}</code></li>`).join("")}</ul>`;
}

/** @param {any[]} edges */
function edgeList(edges) {
  return `<section class="panel"><h3>Wiring</h3><p class="hint">A human writes these. Agents never do.</p><ul class="edges">${
    edges.map((e) => `<li><code>${esc(e.from)}</code> &rarr; <code>${esc(e.to)}</code>${e.why ? `<span>${esc(e.why)}</span>` : ""}</li>`).join("")
  }</ul></section>`;
}

/** @param {any[]} dropped */
function droppedList(dropped) {
  return `<section class="panel bad"><h3>Dropped</h3><p class="hint">Claimed, but with no line in a real file behind it, so it was not drawn.</p><ul class="edges">${
    dropped.map((d) => `<li><code>${esc(d.what)}</code><span>${esc(d.why)}</span></li>`).join("")
  }</ul></section>`;
}

/** @param {any[]} verdicts */
function verdictPanel(verdicts) {
  return `<section class="panel"><h3>Last run</h3><ul class="verdicts">${
    verdicts.map((v) => `<li class="${v.status === "refused" ? "no" : "ok"}">
      <b>${esc(v.module)}</b> <span>${esc(v.summary ?? v.status)}</span>
      ${Array.isArray(v.gates) ? `<ul class="gates">${v.gates.map((/** @type {any} */ g) => `<li class="${g.ok ? "ok" : "no"}"><b>${esc(g.name)}</b> ${esc(g.detail ?? "")}</li>`).join("")}</ul>` : ""}
    </li>`).join("")
  }</ul></section>`;
}

function footer() {
  return `<footer>Rendered by <code>inspect-ui</code>, which is itself a module: contracted, size-capped, and admitted only after passing its own conformance suite.</footer>`;
}

/** @param {string} k @param {string} v */
function row(k, v) {
  return `<div><dt>${k}</dt><dd>${v}</dd></div>`;
}

/** @param {string} h */
function short(h) {
  return h.length > 16 ? h.slice(0, 16) + "…" : h;
}

/**
 * Everything user- or planner-authored goes through here.
 *
 * A module named `<img onerror=alert(1)>` must render as its own name. This is
 * not hypothetical caution: module names, notes and edge reasons are written by
 * planners and agents, which makes every one of them untrusted text arriving in
 * a document. The conformance suite asserts the absence of the live tag, because
 * absence is the only form the claim can take.
 * @param {unknown} s
 */
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** @param {string} message */
function bad(message) {
  return Object.assign(new Error(message), { code: "EBADINPUT" });
}

const CSS = `
:root{--bg:#f4f5f1;--fg:#171c18;--dim:#68756a;--line:#d3dbd2;--card:#fff;--ok:#2c6e4b;--no:#a63a1d;--idle:#7d8a80;--accent:#2c3e7a}
@media (prefers-color-scheme:dark){:root{--bg:#0e1211;--fg:#dee5df;--dim:#8b998d;--line:#242c26;--card:#141a17;--ok:#63c08d;--no:#e08966;--idle:#7d8a80;--accent:#93a8e6}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:2rem 1.5rem 4rem}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.86em}
header{max-width:72rem;margin:0 auto 2rem}
.masthead{display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}
h1{font-size:1.5rem;margin:0;letter-spacing:-.01em}
button{font:inherit;font-size:.86rem;padding:.5rem 1rem;border:1.5px solid var(--accent);background:transparent;color:var(--accent);cursor:pointer;border-radius:3px}
button:hover{background:var(--accent);color:var(--bg)}
button[disabled]{opacity:.5;cursor:progress}
.counts{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:1rem}
.chip{font-size:.78rem;color:var(--dim);border:1px solid var(--line);padding:.2rem .6rem;border-radius:99px;display:inline-flex;gap:.4rem;align-items:baseline}
.chip b{font-variant-numeric:tabular-nums;color:var(--fg)}
.chip.ok b{color:var(--ok)}.chip.no b{color:var(--no)}
.status{font-size:.82rem;color:var(--dim);margin:.9rem 0 0}
.grid{max-width:72rem;margin:0 auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(20rem,1fr));gap:1rem}
.card{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--idle);border-radius:4px;padding:1rem 1.1rem;min-width:0}
.card.live{border-left-color:var(--ok)}.card.refused{border-left-color:var(--no)}
.card-top{display:flex;justify-content:space-between;align-items:baseline;gap:.6rem}
h2{font-size:1rem;margin:0;font-family:ui-monospace,Menlo,monospace}
.state{font-size:.7rem;letter-spacing:.08em;text-transform:uppercase;color:var(--idle)}
.state.live{color:var(--ok)}.state.refused{color:var(--no)}
dl{margin:.8rem 0 0;display:grid;gap:.15rem;font-size:.8rem}
dl>div{display:flex;gap:.6rem}
dt{color:var(--dim);min-width:5rem}
dd{margin:0;min-width:0;overflow-wrap:anywhere}
.proved{font-size:.75rem;color:var(--dim);margin:.7rem 0 0}
.proved span{color:var(--ok);margin-right:.5rem}
.note{font-size:.78rem;color:var(--dim);margin:.5rem 0 0;font-style:italic}
.alts{margin-top:.7rem;padding-top:.6rem;border-top:1px dashed var(--line);font-size:.75rem}
.alts-label{display:block;color:var(--dim);margin-bottom:.35rem}
.alt{display:inline-flex;gap:.35rem;align-items:baseline;border:1px solid var(--line);padding:.15rem .5rem;border-radius:3px;margin:0 .35rem .35rem 0;color:var(--dim)}
.alt.live{border-color:var(--ok);color:var(--fg)}
.ev{list-style:none;margin:.7rem 0 0;padding:0;font-size:.74rem}
.ev li{color:var(--dim)}
.noev{font-size:.75rem;color:var(--no);margin:.7rem 0 0}
.panel{max-width:72rem;margin:2rem auto 0;border:1px solid var(--line);border-radius:4px;padding:1rem 1.1rem;background:var(--card)}
.panel.bad{border-color:var(--no)}
h3{font-size:.86rem;margin:0;text-transform:uppercase;letter-spacing:.08em;color:var(--dim)}
.hint{font-size:.8rem;color:var(--dim);margin:.35rem 0 .8rem}
.edges,.verdicts{list-style:none;margin:0;padding:0;font-size:.82rem;display:grid;gap:.5rem}
.edges span{color:var(--dim);margin-left:.6rem}
.verdicts>li>b{margin-right:.5rem}
.verdicts>li.no>b{color:var(--no)}.verdicts>li.ok>b{color:var(--ok)}
.gates{list-style:none;margin:.4rem 0 0 1rem;padding:0;font-size:.76rem;color:var(--dim);display:grid;gap:.2rem}
.gates li.ok b{color:var(--ok)}.gates li.no b{color:var(--no)}
footer{max-width:72rem;margin:3rem auto 0;padding-top:1rem;border-top:1px solid var(--line);font-size:.76rem;color:var(--dim)}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
`;

const SCRIPT = `
const b=document.getElementById("verify"),s=document.getElementById("status");
b&&b.addEventListener("click",async()=>{
  b.disabled=true;s.textContent="Running the real gates in a network-denied container. This takes a few seconds.";
  try{
    const r=await fetch("/api/verify",{method:"POST"});
    if(!r.ok)throw new Error("verify returned "+r.status);
    location.reload();
  }catch(e){b.disabled=false;s.textContent="Could not run: "+e.message}
});
`;
