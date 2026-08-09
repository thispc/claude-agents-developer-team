const GLYPH = { clear: "○", cloud: "◍", fog: "◌", rain: "◆", snow: "✻", storm: "▲", unknown: "·" };

/** @param {any} input */
export function page(input) {
  if (input === null || typeof input !== "object" || Array.isArray(input)) throw bad("input must be an object");
  const forecast = input.forecast;
  if (forecast === null || typeof forecast !== "object" || Array.isArray(forecast)) throw bad("forecast must be an object");
  const days = Array.isArray(forecast.days) ? forecast.days : null;
  if (!days) throw bad("forecast.days must be an array");
  const place = forecast.place ?? {};
  const advice = input.advice ?? null;

  const html = [
    "<!doctype html>",
    '<html lang="en"><head><meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width,initial-scale=1">',
    `<title>${esc(place.name ?? "Weather")}</title>`,
    `<style>${CSS}</style></head><body>`,
    `<h1>${esc(place.name ?? "Weather")}</h1>`,
    place.lat !== undefined ? `<p class="at">${esc(fmt(place.lat))}, ${esc(fmt(place.lon))}</p>` : "",
    advice ? adviceBlock(advice) : "",
    days.length ? `<ul class="days">${days.map(dayCard).join("")}</ul>` : `<p class="empty">No days in this forecast.</p>`,
    "</body></html>",
  ].join("");

  return { html, days: days.length };
}

/** @param {any} a */
function adviceBlock(a) {
  const items = Array.isArray(a.items) ? a.items : [];
  return `<section class="advice"><p>${esc(a.summary ?? "")}</p>${
    items.length ? `<ul>${items.map((/** @type {string} */ i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""
  }</section>`;
}

/** @param {any} d */
function dayCard(d) {
  const sky = typeof d.sky === "string" ? d.sky : "unknown";
  const glyph = /** @type {any} */ (GLYPH)[sky] ?? GLYPH.unknown;
  return `<li class="day ${esc(sky)}">
    <span class="glyph" aria-hidden="true">${glyph}</span>
    <span class="date">${esc(d.date ?? "")}</span>
    <span class="temp"><b>${esc(fmt(d.highC))}°</b> <i>${esc(fmt(d.lowC))}°</i></span>
    <span class="rain">${esc(fmt(d.precipMm))}mm</span>
    <span class="sky">${esc(sky)}</span>
  </li>`;
}

/** Numbers are formatted by hand, never with toLocaleString — that reads LANG. @param {unknown} n */
function fmt(n) {
  return typeof n === "number" && Number.isFinite(n) ? String(n) : "–";
}

/**
 * A place name arrives from a search provider, which makes it untrusted text
 * landing in a document. It renders as its own name or not at all.
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
:root{--bg:#f6f7f4;--fg:#161a17;--dim:#6b776d;--line:#d6ded4;--card:#fff;--accent:#2c3e7a}
@media (prefers-color-scheme:dark){:root{--bg:#0e1211;--fg:#dee5df;--dim:#8b998d;--line:#242c26;--card:#141a17;--accent:#93a8e6}}
*{box-sizing:border-box}
body{margin:0;padding:2.5rem 1.5rem;background:var(--bg);color:var(--fg);font:16px/1.6 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:44rem;margin-inline:auto}
h1{font-size:1.9rem;margin:0;letter-spacing:-.02em}
.at{color:var(--dim);font-size:.82rem;margin:.2rem 0 0;font-variant-numeric:tabular-nums}
.advice{margin:1.6rem 0;padding:1rem 1.2rem;background:var(--card);border-left:3px solid var(--accent);border-radius:3px}
.advice p{margin:0;font-size:1.02rem}
.advice ul{margin:.6rem 0 0;padding-left:1.1rem;color:var(--dim);font-size:.92rem}
.days{list-style:none;margin:0;padding:0;display:grid;gap:.4rem}
.day{display:grid;grid-template-columns:1.6rem 7rem 1fr auto auto;gap:.9rem;align-items:baseline;background:var(--card);border:1px solid var(--line);border-radius:3px;padding:.6rem .9rem;font-variant-numeric:tabular-nums}
.glyph{font-size:1.05rem;color:var(--dim)}
.date{color:var(--dim);font-size:.86rem}
.temp b{font-weight:600}
.temp i{font-style:normal;color:var(--dim)}
.rain{color:var(--dim);font-size:.86rem}
.sky{color:var(--dim);font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}
.empty{color:var(--dim)}
@media (max-width:32rem){.day{grid-template-columns:1.6rem 1fr auto}.rain,.sky{display:none}}
`;
