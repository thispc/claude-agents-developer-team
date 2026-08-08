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
# process-compose boots every managed service (the conductor plus knowledge 8881,
# usage 8882 and notify 8883) with readiness probes, restarts, and its REST API
# token-authed on the fleet_api port (8899 — see services.yaml; token in
# data/tokens/fleet-api.token).
#
# --legacy is the FALLBACK for a machine where process-compose misbehaves, and the
# P1 cutover RE-SCOPED it: it means "the conductor outside process-compose, the
# fleet's services still required". It cannot mean "no services". Since P1 the
# knowledge store is a service; since the P2 cutover so are the quota meter and
# the notifier, and all three in-process fallbacks are deleted — a conductor
# missing KNOWLEDGE_URL, USAGE_URL or NOTIFY_URL refuses to boot rather than run
# with no memory, no meter, or no way to tell you something broke.
#
# So this path still runs tools/gen_fleet.py (pure Python — no vendored binaries,
# which is the tooling that was misbehaving) for env and tokens, then starts ALL
# THREE services itself as children, waits for each /health, exports the URLs, and
# execs the conductor in the foreground. A service already listening on its port
# (a half-running fleet, a second terminal) is REUSED, never duplicated.
# The children share this terminal's process group, so Ctrl-C stops everything; a
# plain SIGTERM to the conductor alone can leave them, which the reuse check then
# absorbs on the next boot. Both paths serve http://127.0.0.1:$PORT.
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
  # The registry still generates env + tokens; only process-compose is skipped.
  .venv/bin/python tools/gen_fleet.py >/dev/null

  # Each extracted service, started as a child of this shell. One loop rather than
  # three copies: the next extraction adds a name here and nothing else, and three
  # drifting copies of a readiness wait is exactly how one of them quietly stops
  # waiting. Order does not matter — the conductor is the only caller, and it is
  # started last.
  #
  # The FAILURE here is deliberately fatal. Letting the conductor start anyway
  # would hand it a RuntimeError from init() a second later, which reads as a
  # crash rather than as "this service did not come up" — and on the meter it
  # would be worse than a crash, because a conductor that cannot see the quota
  # must not be guessing about it.
  for svc in knowledge usage notify; do
    svc_port="$(.venv/bin/python -c "import json,sys; print(json.load(open('data/fleet_topology.json'))['services'][sys.argv[1]]['port'])" "${svc}")"
    svc_url="http://127.0.0.1:${svc_port}"
    if curl -fsS --max-time 2 "${svc_url}/health" >/dev/null 2>&1; then
      echo "${svc}: already up on ${svc_port} — reusing it"
    else
      echo "${svc}: starting as a child on ${svc_port} (no process-compose in legacy mode)"
      ( set -a; . "data/env/${svc}.env"; set +a
        exec .venv/bin/uvicorn app:app --app-dir "services/${svc}" \
             --host 127.0.0.1 --port "${svc_port}" ) &
      for _ in $(seq 1 40); do
        curl -fsS --max-time 2 "${svc_url}/health" >/dev/null 2>&1 && break
        sleep 0.25
      done
      if ! curl -fsS --max-time 2 "${svc_url}/health" >/dev/null 2>&1; then
        echo "${svc} did not come up on ${svc_port} — the conductor refuses to boot without it." >&2
        echo "check services/${svc} and data/env/${svc}.env, or run the full fleet: ./run-local.sh" >&2
        exit 1
      fi
    fi
    export "$(echo "${svc}" | tr '[:lower:]-' '[:upper:]_')_URL=${svc_url}"
  done

  echo "devteam conductor (legacy: outside process-compose) → http://127.0.0.1:${PORT}   (login: ${ROOT_USERNAME:-root} / ${ROOT_PASSWORD:-devteam})"
  echo "data: $(pwd)/devteam.db · services: $(pwd)/data/{knowledge,usage,notify}.db · stop with Ctrl-C"
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
