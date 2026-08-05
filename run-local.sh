#!/usr/bin/env zsh
# Run the platform LOCALLY — the primary environment while cloud credits are paused.
#
#   ./run-local.sh            # http://127.0.0.1:8787  (login: root / devteam unless overridden)
#   PORT=9000 ./run-local.sh  # another port
#
# Data lives in ./devteam.db (the repo root) and persists across restarts.
# Live mode (agents genuinely think) uses, in order: your Settings-page key, the
# ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN env vars, or the local Claude CLI login.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8787}"
export LAUNCHER="${LAUNCHER:-local}"

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "no .venv — create it first:  python3 -m venv .venv && .venv/bin/pip install -r conductor/requirements.txt" >&2
  exit 1
fi

echo "devteam conductor → http://127.0.0.1:${PORT}   (login: ${ROOT_USERNAME:-root} / ${ROOT_PASSWORD:-devteam})"
echo "data: $(pwd)/devteam.db · stop with Ctrl-C"
exec .venv/bin/uvicorn app.main:app --app-dir conductor --host 127.0.0.1 --port "${PORT}"
