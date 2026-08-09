// The size gate. Fifty lines, and the largest single correctness effect in the
// whole design.
//
// The measurement it encodes: agent-written code holds up while it is small and
// stops holding up as it grows, and the collapse is not gradual. SpecBench put
// numbers on it — every frontier agent saturates the tests it can see, and the
// gap between those and a held-out suite widens by roughly 28 points for every
// tenfold increase in size. Under about 10k lines that gap is survivable. Past
// 25k it reaches 100 points, which is another way of saying the visible suite
// has stopped carrying any information at all. SWE-bench-Live agrees from the
// other direction: repositories under ~20k lines are solved over 20% of the
// time, projects past 500 files rarely clear 5%, and patches touching seven or
// more files were never solved at all.
//
// So the cap is not a style preference, it is the boundary of the regime where
// "the tests pass" means something. A module that outgrows it has not become
// slightly less trustworthy; it has left the range where the admission rule
// works.
//
// TWO THRESHOLDS, and the gap between them is deliberate:
//
//   mitosis_loc (1500) — a PROPOSAL is requested. The module still works, still
//     serves traffic, and nothing is forced. It has simply grown enough that
//     someone should look at splitting it.
//
//   max_loc (2000) — CI refuses. Past here the module cannot be admitted at all.
//
// The proposal must fire well before the wall, because a split is verified
// against the parent's own behaviour and that verification is itself weakest at
// large sizes. Waiting until the cap to ask for a division means asking for it
// exactly where it is hardest to check.
//
// CONTRACT SURFACE IS THE OTHER AXIS, and often the more honest one. Lines of
// code measure how much was written; operations and distinct error cases measure
// how much a single test suite is being asked to pin down. A 400-line module
// exposing twelve operations is doing more separable jobs than a 1,400-line
// module exposing one.

/**
 * @typedef {object} SizeVerdict
 * @property {boolean} ok            false means CI refuses this module
 * @property {boolean} proposeSplit  true means a mitosis proposal is requested
 * @property {number} loc
 * @property {number} operations
 * @property {number} errorCases
 * @property {number} surface        operations + distinct error cases
 * @property {string[]} reasons
 */

/**
 * @param {object} args
 * @param {number} args.loc
 * @param {{operations?: Record<string, {errors?: string[]}>}} args.iface  parsed interface.json
 * @param {{maxLoc: number, mitosisLoc: number, mitosisOps: number}} args.limits
 * @returns {SizeVerdict}
 */
export function sizeGate({ loc, iface, limits }) {
  const ops = Object.keys(iface.operations ?? {});
  const errors = new Set();
  for (const op of Object.values(iface.operations ?? {})) {
    for (const e of op.errors ?? []) errors.add(e);
  }
  const surface = ops.length + errors.size;

  /** @type {string[]} */
  const reasons = [];
  let ok = true;
  let proposeSplit = false;

  if (loc > limits.maxLoc) {
    ok = false;
    reasons.push(`${loc} lines exceeds the ${limits.maxLoc}-line cap. This is refused, not warned about: past the cap a passing test suite stops being evidence that the module works.`);
  } else if (loc > limits.mitosisLoc) {
    proposeSplit = true;
    reasons.push(`${loc} lines is past the ${limits.mitosisLoc}-line mark where a split should be proposed (the ${limits.maxLoc}-line cap is still ahead).`);
  }

  if (ops.length > limits.mitosisOps) {
    proposeSplit = true;
    reasons.push(`${ops.length} exported operations is past ${limits.mitosisOps}. One test suite is being asked to pin down too many separable jobs.`);
  }

  return { ok, proposeSplit, loc, operations: ops.length, errorCases: errors.size, surface, reasons };
}

/**
 * Parsimony pressure for a proposed split.
 *
 * A cap on its own is known to fail, and the genetic-programming literature has
 * the receipts: size limits alone actually ACCELERATE growth early on, because
 * they bias a population toward small individuals that then proliferate. The cap
 * needs a pressure term beside it, or the system learns to sit just under the
 * line and multiply instead.
 *
 * So a split has to pay for itself. Two modules whose combined surface is no
 * smaller than the parent's have not divided the work, they have copied it —
 * which is the failure GitClear measured in the wild as AI-era code duplication
 * rising 81% while refactoring line-moves fell 70%.
 *
 * @param {number} parentSurface
 * @param {number[]} childSurfaces
 * @returns {{ok: boolean, why: string}}
 */
export function parsimony(parentSurface, childSurfaces) {
  const total = childSurfaces.reduce((a, b) => a + b, 0);
  if (total >= parentSurface) {
    return {
      ok: false,
      why: `the split does not reduce contract surface (${parentSurface} → ${total}). Two modules that together expose as much as the parent did have copied the work rather than divided it.`,
    };
  }
  return { ok: true, why: `contract surface ${parentSurface} → ${total}` };
}
