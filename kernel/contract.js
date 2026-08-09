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
import { readFileSync, statSync, readdirSync, existsSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { parseToml } from "./toml.js";

/** Files that are never part of any set — editor and OS litter. */
// `.kernel` is where the transport shims land inside a sandbox tree. A module
// directory must never contain one, and never claim one.
const ALWAYS_IGNORED = [".DS_Store", ".git", "node_modules", ".store", ".runs", ".cache", ".kernel"];

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
 * @property {string[]} conformance  language-neutral cases. A module judged ONLY by these is
 *                                   replaceable by an implementation in any language.
 * @property {string[]} tests        language-specific. Their presence locks the module to that language.
 * @property {string} toolchain      ARTIFACT-side: the image, the language, how to start it
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
  const conformance = strings(contract["conformance"], `${file}: [contract] conformance`);
  const tests = strings(contract["tests"], `${file}: [contract] tests`);
  if (iface.length === 0) throw new Error(`${file}: [contract] interface cannot be empty — a module with no declared interface has no front door`);
  if (conformance.length === 0 && tests.length === 0) {
    throw new Error(`${file}: declare [contract] conformance, [contract] tests, or both. "As long as tests pass" is the entire admission rule, so a module with neither can never be admitted.`);
  }

  const prose = strings(t["prose"]?.["files"], `${file}: [prose] files`);
  const impl = strings(t["impl"]?.["files"], `${file}: [impl] files`);
  if (impl.length === 0) throw new Error(`${file}: [impl] files cannot be empty — declare what the agent is allowed to write`);

  // THE TOOLCHAIN BELONGS TO THE ARTIFACT, NOT THE CONTRACT, and moving it here
  // is what makes a module replaceable by one written in another language.
  //
  // While it sat in the contract, a Python implementation of the same interface
  // hashed to a DIFFERENT contract_id — so it was not an alternative way to fill
  // the same slot, it was a different slot. Two implementations that pass the
  // identical conformance suite would have been unable to say so.
  //
  // Nothing is lost by the move. The toolchain is hashed into the ARTIFACT, so
  // changing the image still produces a new digest and still forces a fresh
  // verification; "it passed" remains a reproducible claim about a pinned
  // runtime. What changes is only whose property the runtime is: it describes
  // this implementation, not the thing every implementation must satisfy.
  const toolchain = t["impl"]?.["toolchain"];
  if (typeof toolchain !== "string" || toolchain === "") {
    throw new Error(`${file}: [impl] toolchain must name one file (e.g. toolchain = "toolchain.json"). Without a pinned runtime and a declared language, "it passed" is not reproducible and nothing knows how to start this module.`);
  }

  const lim = t["limits"] ?? {};
  const limits = {
    maxLoc: num(lim["max_loc"], 2000),
    mitosisLoc: num(lim["mitosis_loc"], 1500),
    mitosisOps: num(lim["mitosis_ops"], 7),
  };
  if (limits.mitosisLoc >= limits.maxLoc) {
    throw new Error(`${file}: [limits] mitosis_loc (${limits.mitosisLoc}) must be below max_loc (${limits.maxLoc}) — the split has to be proposed before the wall, not at it`);
  }

  return { name, interface: iface, conformance, tests, toolchain, prose, impl, limits };
}

/**
 * The contract id, plus everything that went into it. The parts are returned so
 * a cache miss can be explained in one line instead of guessed at.
 *
 * `heldoutDir` is hashed in even though the implementing agent never sees its
 * contents, and leaving it out was a real hole: the vault is part of what an
 * artifact has to prove, so adding a held-out test without moving the contract
 * id means every already-admitted artifact stays admitted having never faced it.
 * A ledger hit is not re-verified, so nothing downstream would ever notice.
 * Hashing the vault costs the secret nothing — a digest reveals no test.
 *
 * @param {string} moduleDir
 * @param {string} [heldoutDir]
 * @returns {{id: string, name: string, portable: boolean, files: {interface: FileEntry[], conformance: FileEntry[], tests: FileEntry[], heldout: FileEntry[]}, canonical: string}}
 */
export function contractId(moduleDir, heldoutDir) {
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
  const conformance = resolve(moduleDir, present, m.conformance, "[contract] conformance", claimed);
  const tests = resolve(moduleDir, present, m.tests, "[contract] tests", claimed);

  // These are resolved for their side effect: proving every file in the module
  // belongs to a declared set, and that nothing is in two sets at once. The
  // toolchain is on the artifact side now, so it is claimed here but not hashed
  // into the contract.
  const proseSet = new Set(resolve(moduleDir, present, m.prose, "[prose] files", claimed).map((f) => f.path));
  const implSet = new Set(resolve(moduleDir, present, [...m.impl, m.toolchain], "[impl] files", claimed).map((f) => f.path));

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

  // The vault. Hashed by path and content, never read into anything an agent
  // can see. The key is always present — including when the vault is empty — so
  // that adding the first held-out test is a visible change to the contract
  // rather than the appearance of a field.
  const heldout = heldoutDir && existsSync(heldoutDir)
    ? walk(heldoutDir).map((p) => ({ path: p, sha256: sha256(readFileSync(join(heldoutDir, p))), exec: false }))
    : [];

  const canonical = canonicalise({
    v: 2,
    name: m.name,
    interface: iface.map(tuple),
    conformance: conformance.map(tuple),
    tests: tests.map(tuple),
    heldout: heldout.map(tuple),
  });
  return {
    id: "c-" + sha256(Buffer.from(canonical, "utf8")),
    name: m.name,
    files: { interface: iface, conformance, tests, heldout },
    // A module judged only by language-neutral conformance cases can be filled by
    // an implementation in any language. One that also carries language-specific
    // tests is locked to that language, because those tests cannot judge anything
    // else. That is a real, visible property of a module rather than a footnote.
    portable: tests.length === 0,
    canonical,
  };
}

/**
 * The digest of what the agent actually wrote. Separate from the contract id on
 * purpose: many artifacts may satisfy one contract, which is what makes the
 * ledger a relation rather than a function, and N-version programming free.
 * @param {string} moduleDir
 * @returns {{digest: string, files: FileEntry[], loc: number, language: string, image: string}}
 */
export function artifactDigest(moduleDir) {
  const m = loadManifest(moduleDir);
  const present = walk(moduleDir);
  // The toolchain rides with the code it describes. An artifact is "this
  // implementation, and the runtime it claims to work under" — judged as one
  // thing, because either half changing makes "it passed" a different claim.
  const files = resolve(moduleDir, present, [...m.impl, m.toolchain], "[impl] files", new Set());
  const canonical = canonicalise({ v: 2, name: m.name, impl: files.map(tuple) });

  const tool = readToolchain(moduleDir, m);
  // Lines of the implementation only. The toolchain is configuration, and
  // counting it against the size cap would penalise a module for declaring its
  // runtime carefully.
  let loc = 0;
  for (const f of files) {
    if (f.path === m.toolchain) continue;
    loc += countLoc(join(moduleDir, f.path));
  }
  return { digest: "a-" + sha256(Buffer.from(canonical, "utf8")), files, loc, language: tool.language, image: tool.image };
}

/**
 * The artifact's runtime declaration.
 * @param {string} moduleDir
 * @param {Manifest} m
 * @returns {{image: string, language: string, entry: string, test?: string[], timeout_sec?: number, memory?: string, pids?: number}}
 */
export function readToolchain(moduleDir, m) {
  const path = join(moduleDir, m.toolchain);
  let t;
  try {
    t = JSON.parse(readFileSync(path, "utf8"));
  } catch (err) {
    throw new Error(`${path}: not readable JSON — ${err instanceof Error ? err.message : String(err)}`);
  }
  if (typeof t.image !== "string" || t.image === "") throw new Error(`${path}: needs an "image"`);
  if (typeof t.language !== "string" || t.language === "") {
    throw new Error(`${path}: needs a "language" — it selects the kernel shim that starts this module and speaks to it. Supported: ${SUPPORTED_LANGUAGES.join(", ")}.`);
  }
  if (!SUPPORTED_LANGUAGES.includes(t.language)) {
    throw new Error(`${path}: language ${JSON.stringify(t.language)} has no kernel shim. Supported: ${SUPPORTED_LANGUAGES.join(", ")}. Adding one is two small files in kernel/transport/ — a serve shim and a drive shim.`);
  }
  if (typeof t.entry !== "string" || t.entry === "") {
    throw new Error(`${path}: needs an "entry" — the file the kernel's serve shim loads to find this module's operations (e.g. "impl/run.js").`);
  }
  return t;
}

/** Languages the kernel can start and talk to. Each needs a serve + drive shim. */
export const SUPPORTED_LANGUAGES = ["js", "py"];

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
