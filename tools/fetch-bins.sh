#!/usr/bin/env bash
# Ensure the fleet's two host binaries exist: process-compose (runs the fleet) and
# oasdiff (the CI gate on committed OpenAPI specs). Idempotent — a binary already on
# PATH or in tools/bin/ is left alone, so run-local.sh can call this on every boot.
#
# macOS with Homebrew installs through brew (process-compose lives in the
# f1bonacc1 tap, oasdiff in core). Everywhere else — and on a Mac without brew —
# the PINNED GitHub release tarball for this OS/arch is unpacked into tools/bin/,
# which is gitignored. The pins are the versions this repo is verified against;
# bump them here, deliberately, not by re-running.
set -euo pipefail
cd "$(dirname "$0")/.."

# ---- the pins --------------------------------------------------------------
PC_VERSION="v1.120.0"        # F1bonacc1/process-compose
OASDIFF_VERSION="1.28.0"     # oasdiff/oasdiff (their tags are vX.Y.Z, assets X.Y.Z)

BIN_DIR="$(pwd)/tools/bin"
mkdir -p "$BIN_DIR"
export PATH="$BIN_DIR:$PATH"

have() { command -v "$1" >/dev/null 2>&1; }

os="$(uname -s | tr '[:upper:]' '[:lower:]')"    # darwin | linux
arch="$(uname -m)"
case "$arch" in
  x86_64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
esac

fetch() {  # fetch <url> <member-name> — unpack one file from a tarball into tools/bin
  local url="$1" member="$2" tmp
  tmp="$(mktemp -d)"
  echo "fetch-bins: downloading $url"
  curl -fsSL "$url" -o "$tmp/pkg.tar.gz"
  tar -xzf "$tmp/pkg.tar.gz" -C "$tmp" "$member"
  install -m 0755 "$tmp/$member" "$BIN_DIR/$member"
  rm -rf "$tmp"
}

ensure_process_compose() {
  have process-compose && return 0
  if [ "$os" = "darwin" ] && have brew; then
    brew install f1bonacc1/tap/process-compose && return 0
    echo "fetch-bins: brew install failed — falling back to the pinned release" >&2
  fi
  fetch "https://github.com/F1bonacc1/process-compose/releases/download/${PC_VERSION}/process-compose_${os}_${arch}.tar.gz" \
        "process-compose"
}

ensure_oasdiff() {
  have oasdiff && return 0
  if [ "$os" = "darwin" ] && have brew; then
    brew install oasdiff && return 0
    echo "fetch-bins: brew install failed — falling back to the pinned release" >&2
  fi
  local asset_os_arch="${os}_${arch}"
  [ "$os" = "darwin" ] && asset_os_arch="darwin_all"     # their mac build is universal
  fetch "https://github.com/oasdiff/oasdiff/releases/download/v${OASDIFF_VERSION}/oasdiff_${OASDIFF_VERSION}_${asset_os_arch}.tar.gz" \
        "oasdiff"
}

ensure_process_compose
ensure_oasdiff

# Prove both actually execute — a half-downloaded or wrong-arch binary fails HERE,
# not twenty seconds into a fleet boot.
process-compose version >/dev/null
oasdiff --version >/dev/null
echo "fetch-bins: ok — $(process-compose version 2>/dev/null | head -1) · $(oasdiff --version)"
