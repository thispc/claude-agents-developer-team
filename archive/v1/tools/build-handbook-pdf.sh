#!/usr/bin/env bash
# Rebuild docs/devteam-handbook.pdf from tools/handbook-print.html.
#
# The published web page draws its diagrams through a library that only exists in
# the artifact runtime, so printing that file directly would put raw diagram
# source on the page. tools/handbook-print.html is the same content with those
# three figures redrawn in plain HTML, plus print rules: no contents rail, no
# viewport-height column, and page breaks told to keep a heading with what
# follows it and never to split a table row.
set -euo pipefail
cd "$(dirname "$0")/.."
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
[ -x "$CHROME" ] || { echo "Set CHROME to a Chrome/Chromium binary"; exit 1; }
"$CHROME" --headless --disable-gpu --no-pdf-header-footer --no-sandbox \
  --print-to-pdf="$PWD/docs/devteam-handbook.pdf" \
  --virtual-time-budget=8000 "file://$PWD/tools/handbook-print.html"
echo "wrote docs/devteam-handbook.pdf"
