// The store: content-addressed artifacts, append-only, nothing ever overwritten.
//
// This is the property that makes a failed improvement a NON-EVENT rather than a
// rollback. An artifact is written under its own digest, so writing it cannot
// disturb any other artifact; if verification then rejects it, the ledger simply
// never points at it and the module keeps serving what it served before. Nothing
// was undone because nothing was overwritten.
//
// It is also what makes mitosis verifiable at all. The old implementation is
// still here, byte for byte, so it can be run as a differential oracle against
// the pair that claims to replace it — the check the parent's own test suite
// cannot do, and one that is free only because nothing was ever deleted.
//
// Blobs are deduplicated by content, so an artifact that changes one file of ten
// costs one file. The layout deliberately mirrors an OCI registry (blobs by
// digest + a manifest referencing them) because the endpoint is `oras` pushing to
// a real registry — at which point this file is the only one that changes.
//
// Files are stored verbatim rather than in a tarball. Hash-named storage is a
// known way to lose a codebase to its own tooling, so `.store` stays something a
// human can `cat` at three in the morning.

import {
  mkdirSync, writeFileSync, readFileSync, existsSync,
  copyFileSync, chmodSync, readdirSync, renameSync, unlinkSync,
} from "node:fs";
import { join, dirname } from "node:path";
import { sha256, canonicalise } from "./contract.js";

/** @typedef {import("./contract.js").FileEntry} FileEntry */

export class Store {
  /** @param {string} root the `.store` directory */
  constructor(root) {
    this.root = root;
    this.blobs = join(root, "blobs", "sha256");
    this.artifacts = join(root, "artifacts");
    mkdirSync(this.blobs, { recursive: true });
    mkdirSync(this.artifacts, { recursive: true });
  }

  /**
   * Put an artifact in the store. Idempotent: the same bytes give the same
   * digest and touch nothing, which is what makes re-running a tactic and
   * getting the identical answer cost nothing downstream (Nix's early cutoff).
   * @param {string} moduleDir
   * @param {string} digest from artifactDigest()
   * @param {FileEntry[]} files
   * @returns {{digest: string, fresh: boolean}}
   */
  put(moduleDir, digest, files) {
    const manifestPath = join(this.artifacts, digest + ".json");
    if (existsSync(manifestPath)) return { digest, fresh: false };

    for (const f of files) {
      const target = this.blobPath(f.sha256);
      if (existsSync(target)) continue;
      mkdirSync(dirname(target), { recursive: true });
      const tmp = `${target}.tmp-${process.pid}`;
      copyFileSync(join(moduleDir, f.path), tmp);
      chmodSync(tmp, 0o444);
      publish(tmp, target);
    }

    writeAtomic(manifestPath, canonicalise({
      v: 1,
      digest,
      files: files.map((f) => [f.path, f.sha256, f.exec ? 1 : 0]),
    }));
    return { digest, fresh: true };
  }

  /** @param {string} digest @returns {FileEntry[]} */
  manifest(digest) {
    const p = join(this.artifacts, digest + ".json");
    if (!existsSync(p)) throw new Error(`store: no artifact ${digest}`);
    const raw = JSON.parse(readFileSync(p, "utf8"));
    return raw.files.map(
      /** @param {[string, string, number]} t */ (t) => ({ path: t[0], sha256: t[1], exec: t[2] === 1 })
    );
  }

  /** @param {string} digest */
  has(digest) {
    return existsSync(join(this.artifacts, digest + ".json"));
  }

  /**
   * Write an artifact's files out to a directory — to run it, diff it, or use it
   * as the oracle a proposed split has to match.
   * @param {string} digest @param {string} destDir
   */
  materialise(digest, destDir) {
    for (const f of this.manifest(digest)) {
      const out = join(destDir, f.path);
      mkdirSync(dirname(out), { recursive: true });
      copyFileSync(this.blobPath(f.sha256), out);
      chmodSync(out, f.exec ? 0o755 : 0o644);
    }
    return destDir;
  }

  /** @param {string} hex */
  blobPath(hex) {
    return join(this.blobs, hex.slice(0, 2), hex);
  }

  /** Every artifact digest the store holds. @returns {string[]} */
  list() {
    if (!existsSync(this.artifacts)) return [];
    return readdirSync(this.artifacts)
      .filter((f) => f.endsWith(".json"))
      .map((f) => f.slice(0, -5))
      .sort();
  }
}

/**
 * Write via a temporary file and rename, so a crash mid-write can never leave a
 * truncated file sitting at a digest that promises full content.
 * @param {string} path @param {string} text
 */
export function writeAtomic(path, text) {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.tmp-${process.pid}`;
  writeFileSync(tmp, text);
  publish(tmp, path);
}

/**
 * Move a finished temporary file into its final name. Two processes can race to
 * write the same blob, but by construction they wrote identical bytes, so the
 * loser discards its copy instead of failing.
 * @param {string} tmp @param {string} target
 */
function publish(tmp, target) {
  try {
    renameSync(tmp, target);
  } catch (err) {
    if (existsSync(target)) {
      try { unlinkSync(tmp); } catch { /* the other process already tidied up */ }
      return;
    }
    throw err;
  }
}

/** Fingerprints are unreadable at full length; this is for humans only. @param {string} hex */
export function shortDigest(hex) {
  return hex.length > 14 ? hex.slice(0, 14) : hex;
}

export { sha256 };
