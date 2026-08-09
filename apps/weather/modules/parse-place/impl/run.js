const MAX = 120;

/** @param {any} input */
export function parse(input) {
  if (input === null || typeof input !== "object" || Array.isArray(input)) throw err("EEMPTY", "input must be an object with a query");
  const { query } = input;
  if (typeof query !== "string") throw err("EEMPTY", "query must be a string");
  if (query.length > MAX) throw err("ETOOLONG", `at most ${MAX} characters; got ${query.length}`);

  // Split on the FIRST comma only. "Paris, Texas, USA" is a place called Paris
  // in a region called "Texas, USA" — splitting on every comma would invent a
  // third field nobody asked for.
  const at = query.indexOf(",");
  const rawName = at === -1 ? query : query.slice(0, at);
  const rawRegion = at === -1 ? "" : query.slice(at + 1);

  const name = squash(rawName);
  const region = squash(rawRegion);
  if (name === "") throw err("EEMPTY", "a place name is required");
  return { name, region };
}

/**
 * Trim and collapse internal whitespace. Deliberately does NOT change case.
 *
 * Case-folding looks like an obvious kindness and is a trap: it is
 * locale-sensitive, and in Turkish "i" upper-cases to "İ" rather than "I", so a
 * module that title-cased would give different answers under different locales —
 * which the determinism gate would catch, correctly, as a module that reads its
 * surroundings. The provider's search is case-insensitive anyway.
 * @param {string} s
 */
function squash(s) {
  return s.replace(/\s+/g, " ").trim();
}

/** @param {string} code @param {string} message */
function err(code, message) {
  return Object.assign(new Error(message), { code });
}
