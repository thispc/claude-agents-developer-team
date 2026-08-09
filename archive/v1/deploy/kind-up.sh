#!/usr/bin/env bash
# Rehearsal cluster: same deploy path as DOKS, no cloud bill.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NGINX=https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.3/deploy/static/provider/kind/deploy.yaml

kind get clusters 2>/dev/null | grep -qx devteam || kind create cluster --config "$HERE/kind-cluster.yaml"
kubectl apply -f "$NGINX"
# The kind ingress manifest only schedules onto a node labelled ingress-ready.
kubectl label node devteam-control-plane ingress-ready=true --overwrite
# Pin the controller to the port kind-cluster.yaml maps to the host.
kubectl patch svc ingress-nginx-controller -n ingress-nginx --type=json \
  -p '[{"op":"replace","path":"/spec/ports/0/nodePort","value":30080}]' || true
kubectl wait -n ingress-nginx --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller --timeout=300s
kubectl create namespace devteam --dry-run=client -o yaml | kubectl apply -f -

echo
echo "Cluster ready. Set LAUNCHER=k8s and APPS_DOMAIN=devteam.local, then deploy"
echo "from Artifacts → Full deployment. Reach an app with:"
echo '  curl -H "Host: app-<project>.devteam.local" http://localhost:30080/'
