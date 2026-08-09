#!/usr/bin/env node
// The weather app's shell — the EDGE TIER, and the only part of this app that
// touches the outside world.
//
//   node apps/weather/serve.js london            print the page to stdout
//   node apps/weather/serve.js london --open     write it to a file and say where
//   node apps/weather/serve.js --offline         use the recorded fixture, no network
//
// EVERYTHING BELOW THE FETCH IS A MODULE, and nothing above it is. That split is
// the whole demonstration:
//
//   verify() runs every module in a container with `--network=none`. A module
//   that fetched a forecast could therefore never be verified — and a gate that
//   has to be switched off for one module is not a gate. So the I/O lives here,
//   in about thirty readable lines a person owns, and the four modules are pure
//   functions of what they are handed.
//
// That is not a workaround for a limitation. It is the arrangement that makes
// those four modules swappable between languages, cacheable by content, and
// checkable for determinism — none of which is true of anything that calls out
// to a network.
//
// This file is NOT a module. It has no contract and no conformance suite,
// deliberately, because it is exactly the part that cannot have one. Keeping it
// small is the only control available: if it grows, the honest response is to
// move logic down into a module, not to write tests for the shell.

import { readFileSync, writeFileSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Store, Ledger, materialiseRunnable, openModule } from "../../kernel/index.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "../..");
const STORE = join(REPO, ".store");
const RUNS = join(REPO, ".runs");

const argv = process.argv.slice(2);
const flags = new Set(argv.filter((a) => a.startsWith("--")));
const query = argv.filter((a) => !a.startsWith("--"))[0] ?? "London";

const store = new Store(STORE);
const ledger = new Ledger(join(STORE, "ledger.jsonl"));

/**
 * Start a module's ADMITTED artifact as a process and talk to it over the wire.
 *
 * SPAWNED, not imported, and that is the difference between an app that allows
 * polyglot modules and one that actually is polyglot. An import only works when
 * the composer happens to share the module's language — so importing would mean
 * every module here had to be JavaScript, silently, forever. Spawning has no
 * opinion, which is why two of the five below are Python and this file does not
 * know which two.
 */
const open = (name) => openModule({
  runnable: materialiseRunnable({
    moduleDir: join(HERE, "modules", name),
    store, ledger, runsRoot: RUNS,
    heldoutDir: join(HERE, "heldout", name),
  }),
});

const parsePlace = open("parse-place");
const matchPlace = open("match-place");
const normaliseForecast = open("normalise-forecast");
const adviseClothing = open("advise-clothing");
const renderForecast = open("render-forecast");
const all = [parsePlace, matchPlace, normaliseForecast, adviseClothing, renderForecast];

// ── the edge: everything from here to the next comment is impure ────────────

const place = await parsePlace.call("parse", { query });

let geo, payload;
if (flags.has("--offline")) {
  const fixture = JSON.parse(readFileSync(join(HERE, "fixtures", "london.json"), "utf8"));
  ({ geo, payload } = fixture);
} else {
  geo = await geocode(place.name, place.region);
  payload = await forecast(geo.latitude, geo.longitude);
}

/**
 * The region FILTERS the results; it is not part of the query.
 *
 * Sending "London, UK" to this provider's search returns nothing at all — it
 * matches on the place name alone. So the shell asks for candidates and picks
 * among them, which is precisely the kind of provider-shaped knowledge that
 * belongs out here rather than inside a module.
 * @param {string} name @param {string} region
 */
async function geocode(name, region) {
  const url = `https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(name)}&count=10&format=json`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`geocoding failed: HTTP ${r.status}`);
  const candidates = (await r.json()).results ?? [];

  // Choosing among candidates is a pure function of the candidates, so it is a
  // MODULE — see modules/match-place. It started as three lines here and they
  // were wrong: this provider answers "United Kingdom" / "GB", so "London, UK"
  // found nothing. The rule this file states about itself is that when the edge
  // grows, logic moves down rather than the edge growing tests.
  try {
    const { index } = await matchPlace.call("match", { candidates, region });
    return candidates[index];
  } catch (e) {
    const near = candidates.slice(0, 4).map((/** @type {any} */ x) => `${x.name}, ${x.admin1 ?? x.country}`).join(" · ");
    throw new Error(`${/** @type {Error} */ (e).message}${near ? `. Did you mean: ${near}` : ""}`);
  }
}

/** @param {number} lat @param {number} lon */
async function forecast(lat, lon) {
  const url = new URL("https://api.open-meteo.com/v1/forecast");
  url.searchParams.set("latitude", String(lat));
  url.searchParams.set("longitude", String(lon));
  url.searchParams.set("daily", "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum");
  url.searchParams.set("timezone", "UTC");
  url.searchParams.set("forecast_days", "5");
  const r = await fetch(url);
  if (!r.ok) throw new Error(`forecast failed: HTTP ${r.status}`);
  return r.json();
}

// ── back to pure. Four modules, each handed exactly what it needs ───────────

const canonical = await normaliseForecast.call("normalise", { name: geo.name ?? place.name, payload });
const advice = await adviseClothing.call("advise", { forecast: canonical });
const rendered = await renderForecast.call("page", { forecast: canonical, advice });

if (flags.has("--json")) {
  console.log(JSON.stringify({ place: canonical.place, days: canonical.days, advice }, null, 2));
} else if (flags.has("--open")) {
  const out = join(REPO, ".runs", "weather.html");
  writeFileSync(out, rendered.html);
  console.log(`\n  ${advice.summary}\n`);
  console.log(`  wrote ${out}`);
  console.log(`  modules used:`);
  for (const m of all) {
    console.log(`    ${m.language.padEnd(3)}  ${m.artifact.slice(0, 16)}…`);
  }
  console.log("");
} else {
  process.stdout.write(rendered.html);
}

// Five child processes; nothing waits for them.
for (const m of all) m.close();
