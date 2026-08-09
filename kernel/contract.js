// contract_id = H(interface ⊕ tests ⊕ toolchain_pin)
//
// This file decides what "the same thing" means, and every other guarantee in
// the system rests on it. Two properties, and they pull in opposite directions:
//
//   A FALSE MISS is expensive. Re-deriving a module costs dollars and minutes,
//   and one over-broad glob turns that into a cascade across everything
//   downstream. So the hash must ignore anything that does not change behaviour.
//
//   A FALSE HIT is unrecoverable. If a test file silently falls outside the
//   hashed set, the ledger will serve an artifact that was never judged against
//   it, and no later check catches that — the whole point of the ledger is that
//   a hit is not re-verified.
//
// The asymmetry is why this file is so opinionated: a glob matching zero files
// is an error, a file matching no glob is an error, and any overlap between the
// hashed set and the un-hashed set is an error. Each of those is a way the hash
// could quietly stop describing the thing it names.
//
// THE DECISION THAT KEEPS THE SYSTEM CHANGEABLE: prose is not hashed. The
// description is a search heuristic — it tells an agent what to aim at. It is
// not identity. Reword `behavior.md` all day and nothing rebuilds; change one
// assertion in a test and exactly one module does.

import { createHash } from "node:crypto";
import { readFileSync, statSync, readdirSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { parseToml } from "./toml.js";

/** Files that are never part of any set — editor and OS litter. */
const ALWAYS_IGNORED = [".DS_Store", ".git", "node_modules", ".store", ".runs", ".cache"];

/** The manifest. Always hashed, never declarable, never optional. */
const MANIFEST = "module.toml";

/**
 * @typedef {object} FileEntry
 * @property {string} path   module-relative, "/"-separated
 * @property {string} sha256
 * @property {boolean} exec  the executable bit — a test that stops being runnable is a change
 */

/**
 * @typedef {object} Manifest
 * @property {string} name
 * @property {string[]} interface
 * @property {string[]} tests
 * @property {string[]} toolchain
 * @property {string[]} prose
 * @property {string[]} impl
 * @property {{maxLoc: number, mitosisLoc: number, mitosisOps: number}} limits
 */

/**
 * Read and validate `module.toml`.
 * @param {string} moduleDir
 * @returns {Manifest}
 */
export function loadManifest(moduleDir) {
  const file = join(moduleDir, "module.toml");
  let raw;
  try {
    raw = readFileSync(file, "utf8");
  } catch {
    throw new Error(`${moduleDir}: no module.toml — a module without a manifest has no contract, so there is nothing to verify it against`);
  }
  const t = parseToml(raw, relative(process.cwd(), file) || file);

  const name = t["module"]?.["name"];
  if (typeof name !== "string" || name === "") throw new Error(`${file}: [module] name is required`);

  const contract = t["contract"] ?? {};
  const iface = strings(contract["interface"], `${file}: [contract] interface`);
  const tests = strings(contract["tests"], `${file}: [contract] tests`);
  const toolchain = strings(contract["toolchain"], `${file}: [contract] toolchain`);
  if (iface.length === 0) throw new Error(`${file}: [contract] interface cannot be empty — a module with no declared interface has no front door`);
  if (tests.length === 0) throw new Error(`${file}: [contract] tests cannot be empty — "as long as tests pass" is the whole admission rule, so a module with no tests can never be admitted`);
  if (toolchain.length === 0) throw new Error(`${file}: [contract] toolchain cannot be empty — without a pinned runtime, "it passed" is not reproducible`);

  const prose = strings(t["prose"]?.["files"], `${file}: [prose] files`);
  const impl = strings(t["impl"]?.["files"], `${file}: [impl] files`);
  if (impl.length === 0) throw new Error(`${file}: [impl] files cannot be empty — declare what the agent is allowed to write`);

  const lim = t["limits"] ?? {};
  const limits = {
    maxLoc: num(lim["max_loc"], 2000),
    mitosisLoc: num(lim["mitosis_loc"], 1500),
    mitosisOps: num(lim["mitosis_ops"], 7),
  };
  if (limits.mitosisLoc >= limits.maxLoc) {
    throw new Error(`${file}: [limits] mitosis_loc (${limits.mitosisLoc}) must be below max_loc (${limits.maxLoc}) — the split has to be proposed before the wall, not at it`);
  }

  return { name, interface: iface, tests, toolchain, prose, impl, limits };
}

/**
 * The contract id, plus everything that went into it. The parts are returned so
 * a cache miss can be explained in one line instead of guessed at.
 * @param {string} moduleDir
 * @returns {{id: string, name: string, files: {interface: FileEntry[], tests: FileEntry[], toolchain: FileEntry[]}, canonical: string}}
 */
export function contractId(moduleDir) {
  const m = loadManifest(moduleDir);
  const present = walk(moduleDir);
  const claimed = new Set();

  // module.toml is ALWAYS hashed, and cannot be opted out of.
  //
  // It is the file that says which files are hashed, so leaving it out would be
  // the false-hit catastrophe with a clean face on it: narrow one tests glob,
  // the contract id does not move, and the ledger serves an artifact that was
  // never judged against the tests the glob no longer reaches. Nothing
  // downstream would catch that, because the whole point of a ledger hit is
  // that it is not re-verified.
  const iface = resolve(moduleDir, present, [MANIFEST, ...m.interface], "[contract] interface", claimed);
  const tests = resolve(moduleDir, present, m.tests, "[contract] tests", claimed);
  const toolchain = resolve(moduleDir, present, m.toolchain, "[contract] toolchain", claimed);

  // These two sets are resolved for their side effect: proving every file in the
  // module belongs to a declared set, and that nothing is in two sets at once.
  const proseSet = new Set(resolve(moduleDir, present, m.prose, "[prose] files", claimed).map((f) => f.path));
  const implSet = new Set(resolve(moduleDir, present, m.impl, "[impl] files", claimed).map((f) => f.path));

  // A file nobody declared is a file whose role nobody decided. It might be a
  // test that will never be hashed, or something an agent smuggled in. Either
  // way the manifest has stopped describing the module.
  const undeclared = present.filter((p) => !claimed.has(p));
  if (undeclared.length > 0) {
    throw new Error(
      `${moduleDir}: ${undeclared.length} file(s) belong to no declared set: ${undeclared.slice(0, 5).join(", ")}${undeclared.length > 5 ? " …" : ""}\n` +
      `  Every file must be listed under [contract], [prose] or [impl]. An undeclared file is one whose role nobody decided.`
    );
  }
  void proseSet; void implSet;

  const canonical = canonicalise({
    v: 1,
    name: m.name,
    interface: iface.map(tuple),
    tests: tests.map(tuple),
    toolchain: toolchain.map(tuple),
  });
  return {
    id: "c-" + sha256(Buffer.from(canonical, "utf8")),
    name: m.name,
    files: { interface: iface, tests, toolchain },
    canonical,
  };
}

/**
 * The digest of what the agent actually wrote. Separate from the contract id on
 * purpose: many artifacts may satisfy one contract, which is what makes the
 * ledger a relation rather than a function, and N-version programming free.
 * @param {string} moduleDir
 * @returns {{digest: string, files: FileEntry[], loc: number}}
 */
export function artifactDigest(moduleDir) {
  const m = loadManifest(moduleDir);
  const present = walk(moduleDir);
  const files = resolve(moduleDir, present, m.impl, "[impl] files", new Set());
  const canonical = canonicalise({ v: 1, name: m.name, impl: files.map(tuple) });
  let loc = 0;
  for (const f of files) loc += countLoc(join(moduleDir, f.path));
  return { digest: "a-" + sha256(Buffer.from(canonical, "utf8")), files, loc };
}

// ── the canonical encoding ───────────────────────────────────────────────────
// Deterministic bytes for a structure: keys sorted, no whitespace, no numbers
// that could round-trip differently. Anything ambiguous here is a hash that
// disagrees with itself between two machines.

/** @param {unknown} value @returns {string} */
export function canonicalise(value) {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("cannot canonicalise a non-finite number");
    if (!Number.isInteger(value)) throw new Error(`cannot canonicalise the non-integer ${value} — floats do not round-trip identically everywhere`);
    return String(value);
  }
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonicalise).join(",") + "]";
  if (typeof value === "object") {
    const keys = Object.keys(/** @type {object} */ (value)).sort();
    return "{" + keys.map((k) => JSON.stringify(k) + ":" + canonicalise(/** @type {any} */ (value)[k])).join(",") + "}";
  }
  throw new Error(`cannot canonicalise a ${typeof value}`);
}

/** @param {FileEntry} f @returns {[string, string, number]} */
function tuple(f) {
  return [f.path, f.sha256, f.exec ? 1 : 0];
}

/** @param {Buffer} buf */
export function sha256(buf) {
  return createHash("sha256").update(buf).digest("hex");
}

// ── globbing ─────────────────────────────────────────────────────────────────

/**
 * Resolve glob patterns against the module's actual files.
 * @param {string} moduleDir
 * @param {string[]} present  every file in the module, "/"-separated relative paths
 * @param {string[]} patterns
 * @param {string} label      where the patterns came from, for errors
 * @param {Set<string>} claimed  accumulates every path any set has taken
 * @returns {FileEntry[]} sorted by path
 */
function resolve(moduleDir, present, patterns, label, claimed) {
  /** @type {FileEntry[]} */
  const out = [];
  const seen = new Set();
  for (const pattern of patterns) {
    const re = globToRegExp(pattern);
    const hits = present.filter((p) => re.test(p));
    // A glob matching nothing is almost always a typo, and a typo in a tests
    // glob is the false-hit failure: the ledger would serve an artifact that
    // was never judged against those tests, and nothing downstream notices.
    if (hits.length === 0) {
      throw new Error(`${moduleDir}: ${label} pattern ${JSON.stringify(pattern)} matches no file.\n  A pattern that matches nothing silently shrinks what gets hashed, so it is refused rather than ignored.`);
    }
    for (const p of hits) {
      if (claimed.has(p) && !seen.has(p)) {
        throw new Error(`${moduleDir}: ${p} is claimed by two different sets (one of them ${label}).\n  A file that is both hashed and not hashed makes the contract id meaningless.`);
      }
      if (seen.has(p)) continue;
      seen.add(p);
      claimed.add(p);
      const abs = join(moduleDir, p);
      const st = statSync(abs);
      out.push({ path: p, sha256: sha256(readFileSync(abs)), exec: (st.mode & 0o111) !== 0 });
    }
  }
  out.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  return out;
}

/**
 * `**` crosses directories, `*` and `?` do not. Everything else is literal.
 * @param {string} pattern
 */
export function globToRegExp(pattern) {
  if (pattern.includes("\\")) throw new Error(`glob ${JSON.stringify(pattern)}: use "/" as the separator, not "\\"`);
  let re = "";
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i];
    if (c === "*") {
      if (pattern[i + 1] === "*") {
        // "a/**/b" must also match "a/b", so the slash is swallowed with the star.
        if (pattern[i + 2] === "/") { re += "(?:[^/]+/)*"; i += 2; }
        else { re += ".*"; i += 1; }
      } else {
        re += "[^/]*";
      }
      continue;
    }
    if (c === "?") { re += "[^/]"; continue; }
    re += /** @type {string} */ (c).replace(/[.+^${}()|[\]\\]/g, "\\$&");
  }
  return new RegExp("^" + re + "$");
}

/**
 * Every file under a directory, as sorted "/"-separated relative paths.
 * @param {string} dir
 * @returns {string[]}
 */
export function walk(dir) {
  /** @type {string[]} */
  const out = [];
  /** @param {string} d */
  const rec = (d) => {
    for (const ent of readdirSync(d, { withFileTypes: true })) {
      if (ALWAYS_IGNORED.includes(ent.name)) continue;
      const abs = join(d, ent.name);
      if (ent.isDirectory()) rec(abs);
      else if (ent.isFile()) out.push(relative(dir, abs).split(sep).join("/"));
    }
  };
  rec(dir);
  out.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  return out;
}

/** Non-blank, non-comment-only lines. The size gate's unit. @param {string} file */
export function countLoc(file) {
  let n = 0;
  for (const line of readFileSync(file, "utf8").split("\n")) {
    const t = line.trim();
    if (t === "") continue;
    if (t.startsWith("//") || t.startsWith("#") || t.startsWith("*") || t.startsWith("/*")) continue;
    n++;
  }
  return n;
}

// ── small validators ─────────────────────────────────────────────────────────

/** @param {unknown} v @param {string} label @returns {string[]} */
function strings(v, label) {
  if (v === undefined) return [];
  if (!Array.isArray(v) || v.some((x) => typeof x !== "string")) {
    throw new Error(`${label} must be an array of strings`);
  }
  return /** @type {string[]} */ (v);
}

/** @param {unknown} v @param {number} fallback */
function num(v, fallback) {
  if (v === undefined) return fallback;
  if (typeof v !== "number" || !Number.isInteger(v) || v <= 0) throw new Error(`expected a positive whole number, got ${JSON.stringify(v)}`);
  return v;
}
