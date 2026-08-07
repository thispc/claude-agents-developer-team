// lib.js — Utilities + the design system every screen shares: toasts and browser
// notifications, escaping and the markdown renderer, time/text helpers, the ui*
// cards and lanes, chips/sigils/menus, generated figures (avatars and glyphs) and
// the manager-model vocabulary. Loads right after js/core.js — it may call $ and
// api at runtime — and before every consumer; nothing here runs at eval time
// beyond declaring functions and consts.
// canvas2/ reads several of these off window.* (toast, lwHue, lwAvatarSvg, …).
// That works because only top-level `function name()` declarations auto-create
// window properties: keep them as declarations, never convert one to a const/arrow.
// Split from the old monolithic app.js via js/projects.js, js/studio-legacy.js and
// js/canvas1.js (classic scripts share one global scope; index.html defines load order).

// ---- small utilities -------------------------------------------------------

// The server replaces its own process, so poll until it answers again.
// MAX_TRIES * POLL_MS bounds the wait (2 minutes); the paragraph counts it
// down so a long restart still reads as progress, not a hang.
function waitForRestart(msg) {
  const POLL_MS = 2000, MAX_TRIES = 60;
  const el = $("#self");
  el.innerHTML = `<div class="self-restart"><div class="spinner"></div>
    <h2>${escapeHtml(msg)}</h2><p>Waiting for the platform to come back up… (up to ${MAX_TRIES * POLL_MS / 1000}s)</p></div>`;
  const p = el.querySelector("p");
  let tries = 0;
  const t = setInterval(async () => {
    tries++;
    try {
      const r = await fetch("/api/me", { credentials: "same-origin" });
      if (r.ok) { clearInterval(t); location.reload(); return; }
    } catch { /* still down */ }
    if (tries >= MAX_TRIES) { clearInterval(t);
      p.textContent =
        "It hasn't come back. Check the server logs — the previous commit is recorded for rollback.";
    } else {
      const remaining = (MAX_TRIES - tries) * POLL_MS / 1000;
      p.textContent = `Waiting for the platform to come back up… (${remaining}s remaining)`;
    }
  }, POLL_MS);
}

// Brief, non-blocking confirmation of something that already happened —
// or, with isError, a non-blocking error notice. Never blocks the page.
function toast(msg, isError) {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.toggle("error", !!isError);
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 4000);
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

function ago(ts) {
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 24 ? `${h}h ago` : `${Math.round(h / 24)}d ago`;
}

function trim(s, n) { s = (s || "").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

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

// ---- the design system: cards, lanes, chips, sigils, menus -----------------

// ---- the command tab's building blocks, shared with the Devteam screen ----------------
// One crew of agents will drive projects AND the platform's own repair after the handoff,
// so both screens must speak one visual language: the same attention card, the same ask
// card, the same agent cards in the same lanes. These build the HTML only — each screen
// wires its own buttons.
function uiAttnCard(label, text, buttonsHtml) {
  return `<div class="attn-card">
      <div class="bl">${label}</div>
      <div class="qtext">${escapeHtml(text)}</div>
      ${buttonsHtml ? `<div class="qbtns">${buttonsHtml}</div>` : ""}
    </div>`;
}

function uiAskCard(o) {
  return `<div class="ask-card ${o.cls || "decision"}">
      <div class="bl">${o.icon || "👔"} ${o.head}</div>
      <div class="qtext">${escapeHtml(o.text)}</div>
      ${o.note ? `<div class="qnote">${o.note}</div>` : ""}
      ${o.buttonsHtml ? `<div class="qbtns">${o.buttonsHtml}</div>` : ""}
      ${o.replyHtml ? `<div class="qreply">${o.replyHtml}</div>` : ""}
    </div>`;
}

function uiAgentCard(o) {
  return `<div class="agent ${o.cls || ""}"${o.data ? ` data-task="${o.data}"` : ""}>
      <div class="top">
        <span class="role" title="${escapeHtml(o.roleTitle || "")}">${o.roleHtml}</span>
        <span class="st">${o.statusWord}</span>
      </div>
      <div class="title">${escapeHtml(o.title)}</div>
      <div class="doing">${escapeHtml(o.doing)}</div>
      ${o.extraHtml || ""}
      <div class="deps">${o.metaHtml || ""}</div>
    </div>`;
}

function uiLane(label, count, note, cardsHtml) {
  return `<div class="group">
      <div class="group-head"><span class="glabel">${label}</span>
        <span class="gcount">${count}</span>
        ${note ? `<span class="gnote">${note}</span>` : ""}</div>
      <div class="agents">${cardsHtml}</div>
    </div>`;
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

function chip(label, on, attr) {
  return `<button class="sc-chip${on ? " on" : ""}" ${attr}>${escapeHtml(label)}</button>`;
}

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

const reduceMotion = () =>
  window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function suitInfo(suit) {
  const map = { s: "♠", h: "♥", d: "♦", c: "♣" };
  return { glyph: map[suit] || "?", red: suit === "h" || suit === "d" };
}

// A model the picker does not know about is still the model that is running, and
// showing "server default" for it was a lie the card told confidently. Unknown
// ids are displayed as themselves and offered as an option, so changing something
// else never silently reassigns the manager.
function modelName(id) {
  if (!id) return "server default";
  const known = MANAGER_MODELS.find((m) => m.id === id);
  return known ? known.label.split(" — ")[0] : id;
}

const MANAGER_MODELS = [
  { id: "", label: "server default" },
  { id: "claude-haiku-4-5", label: "Haiku — fastest, cheapest" },
  { id: "claude-sonnet-5", label: "Sonnet — balanced" },
  { id: "claude-opus-4-8", label: "Opus — most careful" },
];

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

function lwHue(name) {
  let h = 0; for (const c of String(name || "?")) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return h % 360;
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
