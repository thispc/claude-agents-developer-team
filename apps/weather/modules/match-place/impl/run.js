// Common ways people write a country that no gazetteer returns verbatim.
// Deliberately short: this is a courtesy list, not an attempt at a world atlas,
// and every entry is a judgement someone can disagree with — which is why it
// lives in one module with a conformance suite rather than scattered in a shell.
const ALIASES = {
  uk: ["united kingdom", "gb"],
  gb: ["united kingdom", "gb"],
  england: ["united kingdom", "gb", "england"],
  usa: ["united states", "us"],
  us: ["united states", "us"],
  uae: ["united arab emirates", "ae"],
  holland: ["netherlands", "nl"],
  korea: ["south korea", "kr"],
};

const FIELDS = ["country_code", "country", "admin1", "admin2"];

/** @param {any} input */
export function match(input) {
  if (input === null || typeof input !== "object" || Array.isArray(input)) throw err("ENOCANDIDATES", "input must be an object");
  const { candidates, region } = input;
  if (!Array.isArray(candidates)) throw err("ENOCANDIDATES", "candidates must be an array");
  if (candidates.length === 0) throw err("ENOCANDIDATES", "the search returned nothing");
  if (typeof region !== "string") throw err("ENOMATCH", "region must be a string, empty if unspecified");

  // No region means no opinion, and the provider already ranked them.
  if (region.trim() === "") return { index: 0, why: "no region was given, so the provider's first result stands" };

  const wanted = new Set([region.trim().toLowerCase(), ...(/** @type {any} */ (ALIASES)[region.trim().toLowerCase()] ?? [])]);

  // Fields are tried in order of how specific they are, and ALL candidates are
  // checked at each level before moving on — otherwise a country match on the
  // first candidate would beat a county match on the second, which is backwards.
  for (const field of FIELDS) {
    for (const [i, c] of candidates.entries()) {
      const value = c && typeof c[field] === "string" ? c[field].toLowerCase() : null;
      if (value && wanted.has(value)) return { index: i, why: `${field} is ${c[field]}` };
    }
  }

  throw err("ENOMATCH", `nothing among ${candidates.length} result(s) is in "${region}"`);
}

/** @param {string} code @param {string} message */
function err(code, message) {
  return Object.assign(new Error(message), { code });
}
