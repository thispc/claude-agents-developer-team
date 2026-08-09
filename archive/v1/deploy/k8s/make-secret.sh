#!/usr/bin/env bash
# Create the devteam-secrets Secret in the cluster, straight from .env.
#
# Deliberately never writes a filled-in secrets.yaml. A plaintext copy of every
# credential sitting in the repo directory is the easiest way to commit one by
# accident, and `kubectl create secret` reads values as arguments — so the file
# never needs to exist at all.
#
#   ./deploy/k8s/make-secret.sh [namespace]      # default: devteam
set -euo pipefail

NS="${1:-devteam}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$ROOT/.env"

[ -f "$ENV_FILE" ] || { echo "no .env at $ENV_FILE"; exit 1; }
set -a; . "$ENV_FILE"; set +a

# Only these reach the cluster. Everything else in .env is either a local path,
# a tuning knob that belongs in the manifest, or a credential the cluster has no
# business holding (DIGITALOCEAN_API_TOKEN can create and destroy clusters — the
# conductor never needs it, so it is not in this list. DOCR_READ_TOKEN is the
# scoped alternative: it can list image tags and nothing else, verified by it
# returning 403 on /v2/account.)
KEYS=(
  CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY
  GITHUB_TOKEN GITHUB_REPO
  WORKER_TOKEN
  ROOT_USERNAME ROOT_PASSWORD
  GEMINI_API_KEY OPENAI_API_KEY
  SELFREPAIR_USERS
  DOCR_READ_TOKEN DOCR_REGISTRY AUTO_UPDATE
  SELF_REPO
)

# A conductor with neither is not merely misconfigured — every task it dispatches
# will fail at launch, which reads as "the agents are broken".
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "refusing: neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set in .env"
  exit 1
fi
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "warning: both are set. The API key wins, so you will be billed per token"
  echo "         even though a subscription token is present. Blank one of them."
fi
if [ -z "${WORKER_TOKEN:-}" ] || [ ${#WORKER_TOKEN} -lt 16 ]; then
  echo "refusing: WORKER_TOKEN is missing or too short. Anyone holding it can post"
  echo "          a report as any worker. Generate one: openssl rand -hex 32"
  exit 1
fi

# config.py accepts GEMINI_KEY as an alias for GEMINI_API_KEY because that is
# what people actually write in a .env. Honour it here too, or a key that is
# demonstrably present locally silently fails to reach the cluster.
: "${GEMINI_API_KEY:=${GEMINI_KEY:-}}"

ARGS=()
INCLUDED=()
for k in "${KEYS[@]}"; do
  v="${!k:-}"
  [ -z "$v" ] && continue           # absent beats empty: config.py has defaults
  ARGS+=("--from-literal=$k=$v")
  INCLUDED+=("$k")
done

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$NS" delete secret devteam-secrets --ignore-not-found >/dev/null
kubectl -n "$NS" create secret generic devteam-secrets "${ARGS[@]}" >/dev/null

echo "devteam-secrets created in namespace '$NS' with ${#INCLUDED[@]} keys:"
printf '  %s\n' "${INCLUDED[@]}"      # names only — never the values
echo
echo "keys NOT copied (absent from .env): $(
  for k in "${KEYS[@]}"; do [ -z "${!k:-}" ] && printf '%s ' "$k"; done)"
