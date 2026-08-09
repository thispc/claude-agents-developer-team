// A deliberately small TOML reader for the two files humans hand-write:
// `wiring.toml` (the composition graph) and each module's `module.toml`.
//
// WHY HAND-ROLLED, when `smol-toml` exists and is fine. This is the trusted
// kernel — the component that decides whether agent-written code is admitted.
// Everything it imports is inside that trust boundary, and the TrapDoor campaign
// (npm + PyPI + crates.io, from May 2026) made supply-chain reach into an agent's
// own toolchain a live threat rather than a theoretical one. The kernel therefore
// has zero dependencies, and this is the price: about 200 lines.
//
// It is a SUBSET, and the omissions are enforced rather than ignored. Inline
// tables, datetimes, multi-line strings, hex/octal/binary integers and dotted
// keys inside a table header all throw. A parser that guesses at syntax it does
// not know is how a wiring file comes to mean something other than it reads,
// which is the one thing wiring must never do.

export class TomlError extends Error {
  /** @param {string} msg @param {string} where @param {number} line */
  constructor(msg, where, line) {
    super(`${where}:${line}: ${msg}`);
    this.name = "TomlError";
    this.line = line;
  }
}

// A recursive alias — `string|number|boolean|TomlValue[]` — is what this type
// actually is, and JSDoc cannot express it (unlike a .ts `type`, which can).
// Rather than pretend, the element type stops at `any` here: nothing consumes a
// TOML value without checking its shape first, because a config file is
// untrusted input regardless of what a type says about it. `loadManifest` and
// `loadWiring` both validate every field explicitly, and the errors they raise
// are the ones a person actually sees.
/** @typedef {string|number|boolean|Array<any>} TomlValue */
/** @typedef {Record<string, any>} TomlTable */

const BARE_KEY = /^[A-Za-z0-9_-]+$/;

/**
 * Parse a TOML subset into a plain object.
 * @param {string} text
 * @param {string} [where] filename, for error messages
 * @returns {TomlTable}
 */
export function parseToml(text, where = "<toml>") {
  /** @type {TomlTable} */
  const root = {};
  /** @type {TomlTable} */
  let current = root;
  // Table paths already opened by a header, so a duplicate header is an error
  // rather than a silent merge that loses half the file.
  /** @type {Set<string>} */
  const opened = new Set();

  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    let raw = lines[i] ?? "";
    const lineNo = i + 1;
    const trimmed = raw.trim();
    if (trimmed === "" || trimmed.startsWith("#")) continue;

    // [[array of tables]]
    if (trimmed.startsWith("[[")) {
      if (!trimmed.endsWith("]]")) throw new TomlError("unterminated [[table]] header", where, lineNo);
      const path = splitHeader(trimmed.slice(2, -2), where, lineNo);
      const parent = descend(root, path.slice(0, -1), where, lineNo);
      const leaf = /** @type {string} */ (path[path.length - 1]);
      const existing = parent[leaf];
      if (existing !== undefined && !Array.isArray(existing)) {
        throw new TomlError(`"${path.join(".")}" is already a table, cannot also be [[${path.join(".")}]]`, where, lineNo);
      }
      /** @type {TomlTable[]} */
      const arr = existing ?? (parent[leaf] = []);
      current = {};
      arr.push(current);
      continue;
    }

    // [table]
    if (trimmed.startsWith("[")) {
      if (!trimmed.endsWith("]")) throw new TomlError("unterminated [table] header", where, lineNo);
      const path = splitHeader(trimmed.slice(1, -1), where, lineNo);
      const key = path.join(".");
      if (opened.has(key)) throw new TomlError(`table [${key}] declared twice`, where, lineNo);
      opened.add(key);
      const parent = descend(root, path.slice(0, -1), where, lineNo);
      const leaf = /** @type {string} */ (path[path.length - 1]);
      if (parent[leaf] !== undefined && typeof parent[leaf] !== "object") {
        throw new TomlError(`"${key}" is already a value, cannot also be a table`, where, lineNo);
      }
      current = parent[leaf] ?? (parent[leaf] = {});
      continue;
    }

    // key = value
    const m = /^\s*([A-Za-z0-9_-]+|"[^"]*")\s*=\s*(.*)$/.exec(raw);
    if (!m) throw new TomlError(`cannot read this line — expected "key = value" or a [table] header`, where, lineNo);
    const key = (m[1] ?? "").startsWith('"') ? JSON.parse(/** @type {string} */ (m[1])) : m[1];
    let rest = m[2] ?? "";

    // An array may run over several lines. Rather than pre-scanning brackets
    // (which miscounts brackets inside strings), let the scanner tell us it ran
    // out of input and feed it the next line.
    let scanned = null;
    for (;;) {
      try {
        scanned = scanValue(rest, 0, where, lineNo);
        break;
      } catch (e) {
        if (e instanceof TomlError && /** @type {any} */ (e).incomplete && i + 1 < lines.length) {
          i++;
          rest += "\n" + (lines[i] ?? "");
          continue;
        }
        throw e;
      }
    }
    const tail = rest.slice(scanned.next).trim();
    if (tail !== "" && !tail.startsWith("#")) {
      throw new TomlError(`unexpected text after the value: ${JSON.stringify(tail.slice(0, 30))}`, where, lineNo);
    }
    if (Object.prototype.hasOwnProperty.call(current, key)) {
      throw new TomlError(`key "${key}" set twice in the same table`, where, lineNo);
    }
    current[key] = scanned.value;
  }
  return root;
}

/**
 * @param {string} inner @param {string} where @param {number} line
 * @returns {string[]}
 */
function splitHeader(inner, where, line) {
  const parts = inner.split(".").map((p) => p.trim());
  if (parts.length === 0 || parts.some((p) => p === "")) {
    throw new TomlError(`empty table name in header`, where, line);
  }
  for (const p of parts) {
    if (!BARE_KEY.test(p)) {
      throw new TomlError(`table name ${JSON.stringify(p)} must be letters, digits, "_" or "-"`, where, line);
    }
  }
  return parts;
}

/**
 * @param {TomlTable} root @param {string[]} path @param {string} where @param {number} line
 * @returns {TomlTable}
 */
function descend(root, path, where, line) {
  let node = root;
  for (const p of path) {
    let next = node[p];
    if (Array.isArray(next)) next = next[next.length - 1];   // [[a]] then [a.b]
    if (next === undefined) next = node[p] = {};
    if (typeof next !== "object") throw new TomlError(`"${p}" is a value, not a table`, where, line);
    node = next;
  }
  return node;
}

/**
 * Scan one value starting at `i`.
 * @param {string} s @param {number} i @param {string} where @param {number} line
 * @returns {{value: TomlValue, next: number}}
 */
function scanValue(s, i, where, line) {
  i = skipBlank(s, i);
  if (i >= s.length) throw incomplete("value is missing", where, line);
  const c = s[i];

  if (c === '"' || c === "'") return scanString(s, i, where, line);
  if (c === "[") return scanArray(s, i, where, line);

  if (s.startsWith("true", i)) return { value: true, next: i + 4 };
  if (s.startsWith("false", i)) return { value: false, next: i + 5 };

  // Reject the things this subset does not implement, by name, so the failure
  // says what to do instead of just "syntax error".
  if (c === "{") throw new TomlError("inline tables { } are not supported — use a [table] header", where, line);
  if (/^\d{4}-\d{2}-\d{2}/.test(s.slice(i))) throw new TomlError("datetimes are not supported — use a string", where, line);
  if (/^0[xob]/.test(s.slice(i))) throw new TomlError("hex/octal/binary integers are not supported — use decimal", where, line);

  const num = /^[+-]?(?:\d[\d_]*)(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?/.exec(s.slice(i));
  if (num && num[0] !== "") {
    const text = num[0].replace(/_/g, "");
    const value = Number(text);
    if (!Number.isFinite(value)) throw new TomlError(`${JSON.stringify(num[0])} is not a finite number`, where, line);
    return { value, next: i + num[0].length };
  }
  throw new TomlError(`cannot read a value at ${JSON.stringify(s.slice(i, i + 20))}`, where, line);
}

/** @returns {{value: string, next: number}} */
function scanString(/** @type {string} */ s, /** @type {number} */ i, /** @type {string} */ where, /** @type {number} */ line) {
  const quote = s[i];
  if (s.startsWith(/** @type {string} */ (quote).repeat(3), i)) {
    throw new TomlError("multi-line strings are not supported — keep it on one line", where, line);
  }
  let out = "";
  let j = i + 1;
  while (j < s.length) {
    const ch = s[j];
    if (ch === "\n") throw new TomlError("unterminated string — a newline reached before the closing quote", where, line);
    if (ch === quote) return { value: out, next: j + 1 };
    if (ch === "\\" && quote === '"') {
      const esc = s[j + 1];
      const simple = /** @type {Record<string,string>} */ ({ n: "\n", t: "\t", r: "\r", '"': '"', "\\": "\\", b: "\b", f: "\f" });
      if (esc !== undefined && simple[esc] !== undefined) { out += simple[esc]; j += 2; continue; }
      if (esc === "u" || esc === "U") {
        const width = esc === "u" ? 4 : 8;
        const hex = s.slice(j + 2, j + 2 + width);
        if (!new RegExp(`^[0-9a-fA-F]{${width}}$`).test(hex)) {
          throw new TomlError(`bad \\${esc} escape`, where, line);
        }
        out += String.fromCodePoint(parseInt(hex, 16));
        j += 2 + width;
        continue;
      }
      throw new TomlError(`unknown escape \\${esc ?? ""}`, where, line);
    }
    out += ch;
    j++;
  }
  throw new TomlError("unterminated string", where, line);
}

/** @returns {{value: TomlValue[], next: number}} */
function scanArray(/** @type {string} */ s, /** @type {number} */ i, /** @type {string} */ where, /** @type {number} */ line) {
  /** @type {TomlValue[]} */
  const out = [];
  let j = i + 1;
  for (;;) {
    j = skipBlank(s, j);
    if (j >= s.length) throw incomplete("unterminated array", where, line);
    if (s[j] === "]") return { value: out, next: j + 1 };
    const item = scanValue(s, j, where, line);
    out.push(item.value);
    j = skipBlank(s, item.next);
    if (j >= s.length) throw incomplete("unterminated array", where, line);
    if (s[j] === ",") { j++; continue; }
    if (s[j] === "]") return { value: out, next: j + 1 };
    throw new TomlError(`expected "," or "]" in array, found ${JSON.stringify(s[j])}`, where, line);
  }
}

/** Whitespace, newlines and comments all separate tokens inside an array. */
function skipBlank(/** @type {string} */ s, /** @type {number} */ i) {
  for (;;) {
    while (i < s.length && /\s/.test(/** @type {string} */ (s[i]))) i++;
    if (s[i] === "#") { while (i < s.length && s[i] !== "\n") i++; continue; }
    return i;
  }
}

/** A parse failure that more input could fix — the caller feeds it the next line. */
function incomplete(/** @type {string} */ msg, /** @type {string} */ where, /** @type {number} */ line) {
  const e = new TomlError(msg, where, line);
  /** @type {any} */ (e).incomplete = true;
  return e;
}
