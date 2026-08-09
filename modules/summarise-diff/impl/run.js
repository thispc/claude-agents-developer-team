// A JavaScript implementation of the summarise-diff contract.
//
// Nothing here is privileged. It is one artifact that satisfies the contract in
// ../interface.json by answering the cases in ../conformance.json, and the
// Python implementation beside it in the ledger is exactly as valid. Neither is
// "the" implementation.
//
// Only `summarise` is exported. The interface is the whole front door, so a
// helper that leaked out of it would be a second door nobody declared — and the
// conformance driver fails a module that exposes an operation the interface does
// not name.

const MAX_LINES = 5000;

/**
 * @param {unknown} input
 * @returns {{added: number, removed: number, unchanged: number, changed: boolean, summary: string}}
 */
export function summarise(input) {
  if (input === null || typeof input !== "object" || Array.isArray(input)) {
    throw refuse("EBADINPUT", "input must be an object with before and after");
  }
  const { before, after } = /** @type {{before?: unknown, after?: unknown}} */ (input);
  // No coercion. A caller passing a number has a bug, and stringifying it
  // silently would hide the bug behind a plausible answer.
  if (typeof before !== "string") throw refuse("EBADINPUT", "before must be a string");
  if (typeof after !== "string") throw refuse("EBADINPUT", "after must be a string");

  const a = toLines(before);
  const b = toLines(after);
  if (a.length > MAX_LINES || b.length > MAX_LINES) {
    // The comparison is quadratic. Without a ceiling a large input is a hang
    // rather than an answer, and a hang is the worst failure available: nothing
    // downstream can tell it apart from slow.
    throw refuse("ETOOBIG", `at most ${MAX_LINES} lines a side; got ${a.length} and ${b.length}`);
  }

  const unchanged = commonLength(a, b);
  const removed = a.length - unchanged;
  const added = b.length - unchanged;
  const changed = added > 0 || removed > 0;

  return { added, removed, unchanged, changed, summary: changed ? `+${added} -${removed}` : "no change" };
}

/**
 * Text to lines. Every rule here is pinned by a conformance case, because two
 * implementations that split differently disagree about everything downstream.
 * @param {string} text
 */
function toLines(text) {
  if (text === "") return [];                 // zero lines, not one empty line
  const out = text.split("\n");
  if (out[out.length - 1] === "") out.pop();  // one trailing newline is a terminator, not a line
  return out;
}

/**
 * The length of the longest common subsequence.
 *
 * Length rather than the subsequence itself, and that is the whole reason this
 * contract can be satisfied twice: an LCS is not unique, but its length is. Two
 * implementations may walk to different answers about WHICH lines survived and
 * must still agree on how many.
 *
 * One rolling row instead of the full table, so memory is O(min(n,m)) — at the
 * 5,000-line ceiling the square table would be 25M cells for no reason.
 *
 * @param {string[]} a @param {string[]} b
 */
function commonLength(a, b) {
  if (a.length === 0 || b.length === 0) return 0;
  const [short, long] = a.length <= b.length ? [a, b] : [b, a];
  let prev = new Array(short.length + 1).fill(0);
  let cur = new Array(short.length + 1).fill(0);

  for (let i = 1; i <= long.length; i++) {
    for (let j = 1; j <= short.length; j++) {
      cur[j] = long[i - 1] === short[j - 1]
        ? /** @type {number} */ (prev[j - 1]) + 1
        : Math.max(/** @type {number} */ (prev[j]), /** @type {number} */ (cur[j - 1]));
    }
    [prev, cur] = [cur, prev];
  }
  return /** @type {number} */ (prev[short.length]);
}

/** @param {string} code @param {string} message */
function refuse(code, message) {
  return Object.assign(new Error(message), { code });
}
