// Does what this module emits fit what the next one accepts?
//
// This is the check nothing did until now. Every module was judged alone, which
// means the system could be built entirely out of modules that each pass and do
// not fit together — and that is not a hypothetical: it is the measured dominant
// failure of agent-written systems, where features are individually correct and
// fail to share state across the seam between them.
//
// It runs on the SCHEMAS, before anything is started. No containers, no
// processes, milliseconds. That matters because the cheapest gate should catch
// the most, and a shape mismatch is both the commonest composition failure and
// the one that needs no execution to prove.
//
// THE RULE IT FOLLOWS: fail only when a mismatch can be PROVEN. Two schemas that
// simply do not say enough about themselves are reported as unchecked, never as
// broken. A gate that cries wolf on under-specified interfaces would teach
// people to widen their schemas until it went quiet, which is the opposite of
// what it exists to encourage.

/**
 * @typedef {object} Finding
 * @property {"error"|"unchecked"} level
 * @property {string} at    the path within the value, for a message that points somewhere
 * @property {string} says
 */

/**
 * Can every value the producer may emit be accepted by the consumer?
 *
 * Deliberately asymmetric. The producer is the one making promises; the consumer
 * is the one with requirements. A producer that emits MORE than the consumer
 * needs is fine — extra JSON fields are ignored. A producer that might emit less,
 * or something of another type, is not.
 *
 * @param {any} produces  the producer operation's `out` schema
 * @param {any} accepts   the consumer operation's `in` schema (or a slice of it)
 * @param {string} [at]
 * @returns {Finding[]}
 */
export function compatible(produces, accepts, at = "") {
  /** @type {Finding[]} */
  const found = [];
  walk(produces, accepts, at || "the value", found);
  return found;
}

/**
 * @param {any} p @param {any} c @param {string} at @param {Finding[]} found
 */
function walk(p, c, at, found) {
  if (!isSchema(p) || !isSchema(c)) {
    found.push({ level: "unchecked", at, says: "one side does not describe itself, so nothing can be proven either way" });
    return;
  }

  // ── type
  const pt = typesOf(p);
  const ct = typesOf(c);
  if (pt && ct) {
    const shared = pt.filter((t) => ct.includes(t) || (t === "integer" && ct.includes("number")));
    if (shared.length === 0) {
      found.push({ level: "error", at, says: `produces ${pt.join("|")} but ${ct.join("|")} is expected` });
      return;
    }
  }

  // ── enum. The producer's values must all be ones the consumer will accept.
  // This is the check that catches "rainy" versus "rain", which no amount of
  // "both are strings" would.
  if (Array.isArray(p.enum) && Array.isArray(c.enum)) {
    const strays = p.enum.filter((/** @type {any} */ v) => !c.enum.includes(v));
    if (strays.length > 0) {
      found.push({ level: "error", at, says: `may produce ${strays.map((/** @type {any} */ s) => JSON.stringify(s)).join(", ")}, which is not among the accepted values ${c.enum.map((/** @type {any} */ s) => JSON.stringify(s)).join(", ")}` });
    }
  } else if (Array.isArray(c.enum) && !Array.isArray(p.enum)) {
    found.push({ level: "unchecked", at, says: `only ${c.enum.length} particular values are accepted, and the producer does not say which it emits` });
  }

  // ── objects
  const cProps = c.properties;
  if (cProps && typeof cProps === "object") {
    const pProps = p.properties ?? {};
    for (const field of Object.keys(cProps)) {
      const required = Array.isArray(c.required) && c.required.includes(field);
      const producerHas = Object.prototype.hasOwnProperty.call(pProps, field);
      const producerAlways = Array.isArray(p.required) ? p.required.includes(field) : producerHas;

      if (required && !producerHas) {
        found.push({ level: "error", at: `${at}.${field}`, says: `is required here, and the producer never emits it` });
        continue;
      }
      if (required && !producerAlways) {
        found.push({ level: "error", at: `${at}.${field}`, says: `is required here, but the producer only emits it sometimes` });
        continue;
      }
      if (producerHas) walk(pProps[field], cProps[field], `${at}.${field}`, found);
    }
  }

  // ── arrays
  if (c.items && p.items) walk(p.items, c.items, `${at}[]`, found);
  else if (c.items && !p.items) {
    found.push({ level: "unchecked", at: `${at}[]`, says: "the consumer describes its items and the producer does not" });
  }
}

/** @param {any} s */
function isSchema(s) {
  return s !== null && typeof s === "object" && !Array.isArray(s);
}

/** @param {any} s @returns {string[]|null} */
function typesOf(s) {
  if (typeof s.type === "string") return [s.type];
  if (Array.isArray(s.type)) return s.type;
  return null;
}

/**
 * Follow a dotted path into a schema's properties — `in.forecast` picks out the
 * `forecast` field of an operation's input, because an edge usually feeds one
 * argument rather than the whole call.
 * @param {any} schema @param {string} path
 * @returns {{schema: any} | {missing: string}}
 */
export function slice(schema, path) {
  if (!path) return { schema };
  let node = schema;
  const seen = [];
  for (const step of path.split(".")) {
    seen.push(step);
    const props = node?.properties;
    if (!props || !Object.prototype.hasOwnProperty.call(props, step)) {
      return { missing: seen.join(".") };
    }
    node = props[step];
  }
  return { schema: node };
}
