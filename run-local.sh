#!/usr/bin/env zsh
# Run the platform LOCALLY — the primary environment while cloud credits are paused.
#
#   ./run-local.sh              # the FLEET: services.yaml → gen_fleet → process-compose
#   ./run-local.sh --legacy     # the old single-uvicorn path (also RUN_LEGACY=1)
#   PORT=9000 ./run-local.sh    # another port — advertised correctly either way
#
# Fleet path: tools/fetch-bins.sh vendors process-compose + oasdiff (pinned versions
# live in that script), tools/gen_fleet.py regenerates process-compose.yaml +
# data/env + data/tokens + data/fleet_topology.json from services.yaml, then
# process-compose boots every managed service (today: the conductor alone) with
# readiness probes, restarts, and its REST API token-authed on the fleet_api port
# (8899 — see services.yaml; token in data/tokens/fleet-api.token).
#
# --legacy is the FALLBACK until P1 lands (and after, for a machine where the fleet
# tooling misbehaves): one uvicorn, no generator, no process-compose — exactly the
# pre-fleet behaviour. Both paths serve http://127.0.0.1:$PORT.
#
# Data lives in ./devteam.db (the repo root) and persists across restarts.
# Live mode (agents genuinely think) uses, in order: your Settings-page key, the
# ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN env vars, or the local Claude CLI login.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8787}"
export PORT
export LAUNCHER="${LAUNCHER:-local}"
# Defect-1 fix: config.py defaults CONDUCTOR_URL to port 8000 and no dotenv loader
# exists, so locally-spawned workers reported into the void unless the operator
# remembered to export this. Derive it from the port we ACTUALLY bind.
export CONDUCTOR_URL="${CONDUCTOR_URL:-http://127.0.0.1:${PORT}}"

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "no .venv — create it first:  python3 -m venv .venv && .venv/bin/pip install -r conductor/requirements.txt" >&2
  exit 1
fi

if [[ "${1:-}" == "--legacy" || "${RUN_LEGACY:-}" == "1" ]]; then
  echo "devteam conductor (legacy single-process) → http://127.0.0.1:${PORT}   (login: ${ROOT_USERNAME:-root} / ${ROOT_PASSWORD:-devteam})"
  echo "data: $(pwd)/devteam.db · stop with Ctrl-C"
  exec .venv/bin/uvicorn app.main:app --app-dir conductor --host 127.0.0.1 --port "${PORT}"
fi

bash tools/fetch-bins.sh >/dev/null
export PATH="$(pwd)/tools/bin:$PATH"
.venv/bin/python tools/gen_fleet.py

FLEET_API_PORT="$(.venv/bin/python -c "import json; print(json.load(open('data/fleet_topology.json'))['fleet_api']['port'])")"
echo "devteam fleet → http://127.0.0.1:${PORT}   (login: ${ROOT_USERNAME:-root} / ${ROOT_PASSWORD:-devteam})"
echo "fleet API: http://127.0.0.1:${FLEET_API_PORT}  (token: data/tokens/fleet-api.token) · log: data/logs/fleet.log · stop with Ctrl-C"
# Flags pinned against process-compose v1.120.0:
#   up -f <file>    run this compose file
#   -p <port>       its REST API port (env PC_PORT_NUM)
#   --token-file    require this token on every REST call (env PC_API_TOKEN_PATH)
#   -e /dev/null    do NOT load ./.env into services — env comes from data/env/*.env,
#                   exactly like the legacy path never read .env either
#   -t=false        no TUI when stdout is not a terminal (CI, background boots)
tui_flag=()
[[ -t 1 ]] || tui_flag=("-t=false")
exec process-compose up -f process-compose.yaml \
  -p "${FLEET_API_PORT}" \
  --token-file data/tokens/fleet-api.token \
  -e /dev/null "${tui_flag[@]}"
