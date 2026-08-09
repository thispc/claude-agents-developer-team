// The ledger: contract_id → the artifacts that have satisfied it.
//
// It is a RELATION, not a function, and that is the honest formalisation of
// "the agent is not a compiler." A compiler maps input to one output. A tactic
// searches, so the same contract can be satisfied several different ways, and
// keeping all of them is what makes N-version programming free rather than a
// project: run the tactic three times, keep every artifact that passed, promote
// whichever is fastest or smallest or clearest.
//
// It is APPEND-ONLY, which buys three things at once. Pointing back to an older
// artifact is a new line, not a restore. History — what was tried, what was
// refused, and what it had to prove — is a by-product rather than a feature.
// And a crash can only ever lose the last line, which parses as garbage and is
// dropped, never as a plausible wrong answer.
//
// Rejections are recorded too, in the same file. They change nothing about what
// is live — that is the whole point of a failed improvement being a non-event —
// but they are the memory `recall-outcomes` reads so the system stops paying to
// rediscover that a particular approach does not work.

import { appendFileSync, readFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

/**
 * @typedef {object} AdmitRecord
 * @property {"admit"} t
 * @property {string} contract
 * @property {string} artifact
 * @property {string} module
 * @property {string} at        ISO timestamp
 * @property {string[]} proved  the gates this artifact had to pass, by name
 * @property {number} [loc]
 */

/**
 * @typedef {object} RejectRecord
 * @property {"reject"} t
 * @property {string} contract
 * @property {string} artifact
 * @property {string} module
 * @property {string} at
 * @property {string} gate   which gate refused it
 * @property {string} why    one line, human-readable
 */

/**
 * @typedef {object} PinRecord
 * @property {"pin"} t
 * @property {string} contract
 * @property {string} artifact
 * @property {string} module
 * @property {string} at
 * @property {string} by     who chose, since a pin is a human act
 * @property {string} [why]
 */

/** @typedef {AdmitRecord | RejectRecord | PinRecord} Record */

export class Ledger {
  /** @param {string} path the ledger.jsonl file */
  constructor(path) {
    this.path = path;
    mkdirSync(dirname(path), { recursive: true });
  }

  /**
   * Every well-formed record, in the order it was written.
   * @returns {Record[]}
   */
  all() {
    if (!existsSync(this.path)) return [];
    const text = readFileSync(this.path, "utf8");
    /** @type {Record[]} */
    const out = [];
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i++) {
      const line = (lines[i] ?? "").trim();
      if (line === "") continue;
      try {
        out.push(JSON.parse(line));
      } catch {
        // Only the final line can be torn, and only by a crash mid-append. A
        // torn line anywhere else means the file was edited by hand or damaged,
        // and silently continuing past that would make the ledger lie.
        if (i === lines.length - 1) continue;
        throw new Error(`${this.path}:${i + 1}: this line is not valid JSON. The ledger is append-only and machine-written; a damaged line in the middle means the file was edited or corrupted, and it is not safe to read past it.`);
      }
    }
    return out;
  }

  /** @param {Record} rec */
  append(rec) {
    appendFileSync(this.path, JSON.stringify(rec) + "\n");
    return rec;
  }

  /**
   * Every artifact admitted for a contract, oldest first. This is the relation.
   * @param {string} contract
   * @returns {AdmitRecord[]}
   */
  admitted(contract) {
    return /** @type {AdmitRecord[]} */ (this.all().filter((r) => r.t === "admit" && r.contract === contract));
  }

  /**
   * What is live for a contract right now: the human's pin if there is one,
   * otherwise the most recent artifact that passed.
   *
   * A pin wins over anything admitted later on purpose. Auto-admit means the
   * system can put a new implementation live unattended; the pin is how a human
   * says "no, this one" and has it stay said.
   * @param {string} contract
   * @returns {{artifact: string, via: "pin"|"admit", at: string} | null}
   */
  live(contract) {
    let admitted = null;
    let pinned = null;
    for (const r of this.all()) {
      if (r.contract !== contract) continue;
      if (r.t === "admit") admitted = r;
      if (r.t === "pin") pinned = r;
    }
    if (pinned) return { artifact: pinned.artifact, via: "pin", at: pinned.at };
    if (admitted) return { artifact: admitted.artifact, via: "admit", at: admitted.at };
    return null;
  }

  /**
   * A cache hit — the question the build asks before spending anything. Cheap
   * enough to ask always, and worth roughly ten thousand times a normal build's
   * lookup, because the alternative is dollars and minutes rather than
   * milliseconds.
   * @param {string} contract
   */
  hit(contract) {
    return this.live(contract) !== null;
  }

  /**
   * What has already been tried for this contract and refused. The point of
   * paying to store failures is not to pay twice to discover the same one.
   * @param {string} contract
   * @returns {RejectRecord[]}
   */
  rejected(contract) {
    return /** @type {RejectRecord[]} */ (this.all().filter((r) => r.t === "reject" && r.contract === contract));
  }

  /** Every contract the ledger knows about. @returns {string[]} */
  contracts() {
    return [...new Set(this.all().map((r) => r.contract))].sort();
  }
}

/** ISO-8601 to the second — timestamps go in records, never into a hash. */
export function now() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}
