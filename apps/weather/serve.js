#!/usr/bin/env node
// The weather app's shell — the EDGE TIER, and the only part of this app that
// touches the outside world.
//
//   node apps/weather/serve.js --serve           open it in a browser (the usual way)
//   node apps/weather/serve.js london --json      the structured answer, for a script
//   node apps/weather/serve.js --offline --json   the recorded fixture, no network
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

import { readFileSync } from "node:fs";
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

/**
 * One request, start to finish. The only impure steps are the two fetches.
 * @param {string} q
 */
async function lookup(q) {
  const place = await parsePlace.call("parse", { query: q });

  let geo, payload;
  if (flags.has("--offline")) {
    ({ geo, payload } = JSON.parse(readFileSync(join(HERE, "fixtures", "london.json"), "utf8")));
  } else {
    geo = await geocode(place.name, place.region);
    payload = await forecast(geo.latitude, geo.longitude);
  }

  const canonical = await normaliseForecast.call("normalise", { name: geo.name ?? place.name, payload });
  const advice = await adviseClothing.call("advise", { forecast: canonical });
  return { canonical, advice };
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

if (flags.has("--serve")) {
  const { createServer } = await import("node:http");
  const port = Number(process.env["PORT"] ?? 7790);

  /** @type {import("node:http").RequestListener} */
  const handler = (req, res) => {
    (async () => {
      const url = new URL(req.url ?? "/", "http://localhost");
      if (url.pathname === "/favicon.ico") { res.writeHead(204); return res.end(); }
      const q = url.searchParams.get("q") ?? "";

      // An empty box, or a place nobody can find, are ordinary outcomes. The page
      // renders them; nothing 500s and nothing goes blank.
      let out;
      if (q.trim() === "") {
        out = await renderForecast.call("page", { query: q, forecast: { place: {}, days: [] } });
      } else {
        try {
          const { canonical, advice } = await lookup(q);
          out = await renderForecast.call("page", { query: q, forecast: canonical, advice });
        } catch (e) {
          out = await renderForecast.call("page", { query: q, error: /** @type {Error} */ (e).message, forecast: { place: {}, days: [] } });
        }
      }
      res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
      res.end(out.html);
    })().catch((e) => {
      res.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
      res.end(`the weather app could not answer:\n\n${e instanceof Error ? e.message : String(e)}\n`);
    });
  };

  // Both loopback families: macOS resolves `localhost` to ::1 first, and binding
  // only IPv4 makes a browser try IPv6, get refused, and fall back — or not.
  for (const host of ["127.0.0.1", "::1"]) {
    const s = createServer(handler);
    s.on("error", (/** @type {any} */ err) => {
      if (err.code === "EADDRINUSE") {
        console.error(`\n  Port ${port} is in use — the weather app may already be running.\n  Open http://localhost:${port} , or use PORT=7791.\n`);
        process.exit(1);
      }
      if (host === "::1") return;   // a machine without IPv6 keeps the other listener
      throw err;
    });
    s.listen(port, host);
  }
  console.log(`\n  weather → http://localhost:${port}`);
  console.log(`  ${all.length} modules: ${all.map((m) => m.language).join(", ")} — spawned, not imported.`);
  console.log(`\n  This is a server: it holds the terminal until you press Ctrl-C.\n`);
} else {
  const { canonical, advice } = await lookup(query);
  const rendered = await renderForecast.call("page", { query, forecast: canonical, advice });

  if (flags.has("--json")) {
    console.log(JSON.stringify({ place: canonical.place, days: canonical.days, advice }, null, 2));
  } else {
    process.stdout.write(rendered.html);
  }
  for (const m of all) m.close();
}
