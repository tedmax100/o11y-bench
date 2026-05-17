#!/usr/bin/env bash
# Bring up the demo-services k3d cluster and apply all manifests.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER="demo-services"

if ! k3d cluster list "${CLUSTER}" >/dev/null 2>&1; then
  echo "[up] creating k3d cluster ${CLUSTER}"
  k3d cluster create --config "${ROOT}/k8s/cluster.yaml"
else
  echo "[up] cluster ${CLUSTER} already exists"
fi

echo "[up] building payment-service image"
"${ROOT}/scripts/build.sh"

echo "[up] applying manifests"
# Apply in numeric prefix order: 00-namespace then everything else.
# cluster.yaml is a k3d config, not a k8s resource — skip it.
for f in "${ROOT}"/k8s/[0-9]*-*.yaml; do
  kubectl apply -f "$f"
done

echo "[up] waiting for pods to become ready (timeout 180s)"
kubectl -n demo wait --for=condition=Ready pod --all --timeout=180s || {
  echo "[up] some pods are not ready; check 'kubectl -n demo get pods'"
  exit 1
}

echo "[up] ready. grafana: http://localhost:3001  payment: http://localhost:8001"
