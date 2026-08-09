// Where the live artifact of a module actually is on disk.
//
// Note what this file does NOT do: it never imports a module. It resolves a
// path and stops. The kernel is the thing that judges agent-written code, so it
// cannot also be a thing that runs agent-written code — a broken module must
// never be able to stop `devteam build` from being able to fix it.
//
// Callers take the last step themselves, and there are two kinds of caller with
// very different risk. A composer that spawns the module as a process (the way
// verification does) is properly isolated. A composer that imports it into its
// own process is not, and that is a known gap: `devteam ui` does exactly that
// today, which puts agent-written code in the same process as the ledger.

import { existsSync, mkdirSync, writeFileSync, copyFileSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { contractId, loadManifest, readToolchain } from "./contract.js";

/**
 * @param {object} args
 * @param {string} args.moduleDir
 * @param {import("./store.js").Store} args.store
 * @param {import("./ledger.js").Ledger} args.ledger
 * @param {string} args.runsRoot
 * @param {string} [args.heldoutDir]
 * @returns {{path: string, dir: string, artifact: string, via: "pin"|"admit", language: string, entry: string}}
 */
export function resolveLive({ moduleDir, store, ledger, runsRoot, heldoutDir }) {
  const manifest = loadManifest(moduleDir);
  const c = contractId(moduleDir, heldoutDir);
  const live = ledger.live(c.id);
  if (!live) {
    throw new Error(
      `${manifest.name}: nothing is admitted for the current contract.\n` +
      `  Run \`devteam build\` — and if it is refused, the refusal is the answer, not an obstacle to work around.`
    );
  }

  const dir = join(runsRoot, "live", live.artifact);
  if (!existsSync(join(dir, manifest.toolchain))) {
    mkdirSync(dir, { recursive: true });
    store.materialise(live.artifact, dir);
    // The sandbox writes this marker into every tree it assembles. A module
    // loaded here must get the same treatment, or `export` would mean something
    // different at composition time than it did when the module was judged.
    const marker = join(dir, "package.json");
    if (!existsSync(marker)) writeFileSync(marker, JSON.stringify({ type: "module" }) + "\n");
  }

  // THE TOOLCHAIN IS READ FROM THE ARTIFACT, not from the working copy.
  //
  // It says which language this implementation is and which file starts it, and
  // those are properties of the artifact — that is the whole reason it lives on
  // the artifact side of the hash. Reading it from the module directory looks
  // equivalent and is not: pin an older artifact, or swap the working copy to
  // another language, and the two disagree. The failure is loud (a missing
  // entry file) but the cause is not, so it is worth being exact here.
  //
  // The PATH to it comes from module.toml, which is contract and therefore the
  // same for every artifact. Only the contents differ.
  const toolchain = readToolchain(dir, manifest);

  return {
    path: join(dir, toolchain.entry),
    dir,
    artifact: live.artifact,
    via: live.via,
    language: toolchain.language,
    entry: toolchain.entry,
  };
}

/**
 * @typedef {object} Runnable
 * @property {string} name
 * @property {string} dir      a tree that can be started as-is
 * @property {string[]} serve  the command that starts it, relative to that tree
 * @property {string} language
 * @property {string} artifact
 * @property {"pin"|"admit"} via
 */

/** How each language is started. The same shims the sandbox uses. */
const SHIMS = {
  js: ["node", ".kernel/serve.mjs"],
  py: ["python3", ".kernel/serve.py"],
};

/**
 * Materialise a module into a tree that can be STARTED, not merely imported.
 *
 * The difference matters more than it looks. `resolveLive` gives you a path to a
 * file, which is only useful if your own language can load it — so a composer
 * built on it silently requires every module to be JavaScript, and the whole
 * contract/artifact separation buys nothing. This assembles what the module
 * actually needs to run as a process: the artifact, its interface (the serve
 * shim reads `impure` from it, so the clock is frozen at composition time
 * exactly as it was during verification), and the transport shims themselves.
 *
 * @param {object} args
 * @param {string} args.moduleDir
 * @param {import("./store.js").Store} args.store
 * @param {import("./ledger.js").Ledger} args.ledger
 * @param {string} args.runsRoot
 * @param {string} [args.heldoutDir]
 * @returns {Runnable}
 */
export function materialiseRunnable({ moduleDir, store, ledger, runsRoot, heldoutDir }) {
  const manifest = loadManifest(moduleDir);
  const live = resolveLive({ moduleDir, store, ledger, runsRoot, ...(heldoutDir ? { heldoutDir } : {}) });

  const iface = manifest.interface[0] ?? "interface.json";
  const target = join(live.dir, iface);
  if (!existsSync(target)) {
    mkdirSync(dirname(target), { recursive: true });
    copyFileSync(join(moduleDir, iface), target);
  }

  const shimDir = join(live.dir, ".kernel");
  if (!existsSync(shimDir)) {
    mkdirSync(shimDir, { recursive: true });
    const src = join(dirname(fileURLToPath(import.meta.url)), "transport");
    for (const f of readdirSync(src)) {
      if (!f.endsWith(".md")) copyFileSync(join(src, f), join(shimDir, f));
    }
  }

  const shim = /** @type {any} */ (SHIMS)[live.language];
  if (!shim) throw new Error(`${manifest.name}: no kernel shim for language ${JSON.stringify(live.language)}`);

  return {
    name: manifest.name,
    dir: live.dir,
    serve: [...shim, live.entry, manifest.name],
    language: live.language,
    artifact: live.artifact,
    via: live.via,
  };
}
