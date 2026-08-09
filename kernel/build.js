// The build loop, which is four lines of actual policy wrapped in bookkeeping:
//
//   ask the ledger first — a hit costs nothing and is worth roughly ten thousand
//     times what the lookup cost, because the alternative is dollars and minutes;
//   on a miss, verify;
//   if it passes, store it and admit it — passing means live, unattended;
//   if it does not, write down that it did not, and CHANGE NOTHING ELSE.
//
// That last line is the one that makes the whole design safe enough to leave
// running overnight. A failed improvement is not rolled back, because it was
// never applied. The artifact went into a content-addressed store under its own
// digest, where it disturbs nothing; the ledger simply never points at it. The
// module that was serving traffic before the attempt is the same module serving
// traffic after it. There is no window during which the system is half-changed,
// and no cleanup step that could itself fail.
//
// Auto-admit — a passing artifact goes live with no human in the loop — is only
// defensible because of that. The decision is cheap to reverse (every previous
// artifact is still in the store, and pointing back is one appended line), and
// its blast radius is one module behind one contract. Compare a topology change,
// which is expensive to reverse and reaches everything downstream: that one is
// human-merged, and this file will not do it.

import { join } from "node:path";
import { existsSync } from "node:fs";
import { contractId, artifactDigest, loadManifest } from "./contract.js";
import { verify } from "./verify.js";
import { now } from "./ledger.js";

/**
 * @typedef {object} BuildResult
 * @property {"hit"|"admitted"|"refused"} status
 * @property {string} contract
 * @property {string} artifact
 * @property {string} module
 * @property {import("./verify.js").Verdict} [verdict]
 * @property {string} [was]  what stayed live when a candidate was refused
 * @property {string} summary
 */

/**
 * `requireHermetic` DEFAULTS TO TRUE, and the default is the whole point.
 *
 * It was false, and false is a hole with a clean face on it: if the docker probe
 * fails for any reason — daemon not running, image not pulled, a typo in the
 * image name — verification silently falls back to the host runner. That means a
 * live network, no read-only mount and no memory cap, and the artifact is
 * admitted anyway. Nothing about the verdict said so, so the next ledger hit
 * would serve an artifact that was never actually judged hermetically, and a hit
 * is never re-verified.
 *
 * A gate that quietly weakens itself when its sandbox is unavailable is not a
 * gate. So it fails closed, `--allow-host` is the explicit way to say "I know,
 * do it anyway", and either way the admit record keeps `runner` and `hermetic`
 * for good.
 *
 * @param {object} args
 * @param {string} args.moduleDir
 * @param {import("./store.js").Store} args.store
 * @param {import("./ledger.js").Ledger} args.ledger
 * @param {string} args.runsRoot
 * @param {string} [args.heldoutRoot]
 * @param {boolean} [args.requireHermetic]
 * @param {boolean} [args.force] re-judge even on a ledger hit
 * @returns {Promise<BuildResult>}
 */
export async function buildModule({ moduleDir, store, ledger, runsRoot, heldoutRoot, requireHermetic = true, force = false }) {
  const manifestName = loadManifest(moduleDir).name;
  const vault = heldoutRoot ? join(heldoutRoot, manifestName) : undefined;
  const hasVault = Boolean(vault && existsSync(vault));

  // The vault is part of the contract, so it is hashed here too. Adding a
  // held-out test has to be a cache MISS — otherwise every artifact already
  // admitted stays admitted having never faced it, and a ledger hit is never
  // re-verified.
  const c = contractId(moduleDir, hasVault ? vault : undefined);
  const a = artifactDigest(moduleDir);

  // THE CACHE QUESTION. Note what it is keyed on: the contract and the artifact,
  // and the contract is a hash of the interface, the tests and the toolchain —
  // never of the prose. Rewording behavior.md cannot reach this line. Changing
  // one assertion in a test changes the contract id and therefore misses, for
  // this module and no other.
  const alreadyAdmitted = ledger.admitted(c.id).some((r) => r.artifact === a.digest);
  if (alreadyAdmitted && !force) {
    return {
      status: "hit",
      contract: c.id,
      artifact: a.digest,
      module: c.name,
      summary: `${c.name}: already admitted for this contract — nothing to do, nothing spent`,
    };
  }

  const verdict = await verify({
    moduleDir,
    runsRoot,
    ...(hasVault ? { heldoutDir: /** @type {string} */ (vault) } : {}),
    requireHermetic,
  });

  if (!verdict.ok) {
    const failed = verdict.gates.find((g) => !g.ok);
    ledger.append({
      t: "reject",
      contract: c.id,
      artifact: a.digest,
      module: c.name,
      at: now(),
      gate: failed?.name ?? "unknown",
      why: failed?.detail ?? "no gate reported a reason",
    });
    // The refused artifact is deliberately NOT stored. Keeping every failed
    // attempt would grow the store without bound for something nothing can ever
    // point at; the ledger line records that it was tried and why it lost.
    const live = ledger.live(c.id);
    return {
      status: "refused",
      contract: c.id,
      artifact: a.digest,
      module: c.name,
      verdict,
      ...(live ? { was: live.artifact } : {}),
      summary: live
        ? `${c.name}: refused at the ${failed?.name} gate. ${live.artifact.slice(0, 12)}… is still live — nothing changed.`
        : `${c.name}: refused at the ${failed?.name} gate. Nothing was live before and nothing is live now.`,
    };
  }

  store.put(moduleDir, a.digest, a.files);
  ledger.append({
    t: "admit",
    contract: c.id,
    artifact: a.digest,
    module: c.name,
    at: now(),
    proved: verdict.gates.filter((g) => g.ok).map((g) => g.name),
    loc: a.loc,
    // Which language filled the slot, and under what runtime. Several languages
    // may satisfy one contract, so without this the relation records that two
    // artifacts passed and loses the only interesting thing about them.
    language: a.language,
    image: a.image,
    // Whether the network was ACTUALLY denied while this was judged. Recorded
    // permanently, because a host-runner pass and a hermetic pass are different
    // claims and a ledger hit is never re-verified — so if the difference is not
    // written down at admission time, it is not recoverable later.
    runner: verdict.runner,
    hermetic: verdict.hermetic,
  });

  return {
    status: "admitted",
    contract: c.id,
    artifact: a.digest,
    module: c.name,
    verdict,
    summary: `${c.name}: admitted — passed ${verdict.gates.map((g) => g.name).join(", ")}${verdict.hermetic ? "" : " (NOT hermetic: host runner)"}`,
  };
}
