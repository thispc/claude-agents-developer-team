// names.json — the human index into a hash-named store.
//
// This exists on day one rather than "when we need it", because the failure it
// prevents is not a bug, it is a slow loss of the ability to work on your own
// system. Content-addressing gives correctness properties nothing else does, and
// takes readability away in exchange: after a fortnight the store is a few
// hundred directories called things like a-9f3c2e81b0. Unison spent years
// building tooling to stay habitable under exactly this trade, with a team. One
// person cannot pay that later.
//
// So every digest that is ever written down also gets a sentence: which module,
// when, and what changed about it. It is not part of any hash and nothing
// depends on it — it is allowed to be wrong, incomplete, or edited by hand. Its
// only job is that `devteam lookup a-9f3c` answers in words.

import { existsSync, readFileSync } from "node:fs";
import { writeAtomic } from "./store.js";

/**
 * @typedef {object} NameEntry
 * @property {string} module
 * @property {string} at
 * @property {string} note
 */

export class Names {
  /** @param {string} path the names.json file */
  constructor(path) {
    this.path = path;
    /** @type {Record<string, NameEntry>} */
    this.map = existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : {};
  }

  /** @param {string} digest @param {string} module @param {string} note @param {string} at */
  set(digest, module, note, at) {
    this.map[digest] = { module, at, note };
    writeAtomic(this.path, JSON.stringify(this.map, null, 2) + "\n");
  }

  /**
   * Resolve a digest, or any unambiguous prefix of one — nobody types 64
   * hex characters, and a prefix that matches two things should say so rather
   * than pick one.
   * @param {string} prefix
   * @returns {{digest: string, entry: NameEntry} | {ambiguous: string[]} | null}
   */
  lookup(prefix) {
    const hits = Object.keys(this.map).filter((k) => k.startsWith(prefix));
    if (hits.length === 0) return null;
    if (hits.length > 1) return { ambiguous: hits };
    const digest = /** @type {string} */ (hits[0]);
    return { digest, entry: /** @type {NameEntry} */ (this.map[digest]) };
  }
}
