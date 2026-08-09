// The kernel's entire public surface.
//
// `exports` in package.json points here and nowhere else, which makes this list
// the gateway: with `moduleResolution: nodenext`, an import of
// `@devteam/kernel/verify.js` is not a lint warning to be argued about, it is
// TS2307 "Cannot find module" — a red squiggle while typing, a failed
// `npm run check`, and a failed CI gate, all from the same resolver Node uses at
// runtime. There is no custom tooling to keep in sync because there is no custom
// tooling.
//
// Everything below is hand-written and stays hand-written. The kernel cannot be
// agent-built without circularity: it is the thing that decides whether
// agent-built code is admitted.

export { contractId, artifactDigest, loadManifest, readToolchain, canonicalise, sha256, walk, countLoc, globToRegExp } from "./contract.js";
export { Store, writeAtomic, shortDigest } from "./store.js";
export { Ledger, now } from "./ledger.js";
export { verify } from "./verify.js";
export { sizeGate, parsimony } from "./sizegate.js";
export { buildModule } from "./build.js";
export { resolveLive, materialiseRunnable } from "./compose.js";
export { openModule } from "./client.js";
export { parseToml, TomlError } from "./toml.js";
export { loadWiring } from "./wiring.js";
export { checkFit } from "./fit.js";
export { compatible } from "./compat.js";
export { Names } from "./names.js";
