#!/usr/bin/env bash
# Build the aiops-agent image, import it into the demo-services k3d cluster, and
# (re)apply the manifest — including the read-only RBAC + runbooks added in v3.
# Use this after changing service code to get it running in-cluster.
#
# Prereqs: the demo-services cluster is up (demo-services/scripts/up.sh) and the
# aiops-agent-secrets secret exists (GOOGLE_API_KEY). This script does NOT create
# the secret; if it's missing, create it first:
#   kubectl -n demo create secret generic aiops-agent-secrets \
#     --from-literal=google-api-key="$GOOGLE_API_KEY" \
#     --from-literal=github-token="${GITHUB_TOKEN:-}"
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"          # aiops-agent/
REPO="$(cd "${ROOT}/.." && pwd)"                  # repo root
CLUSTER="demo-services"
IMAGE="aiops-agent/service:dev"
MANIFEST="${REPO}/demo-services/k8s/15-aiops-agent.yaml"

echo "[deploy] building ${IMAGE}"
docker build -t "${IMAGE}" "${ROOT}/service"

echo "[deploy] importing ${IMAGE} into k3d cluster ${CLUSTER}"
k3d image import "${IMAGE}" -c "${CLUSTER}"

echo "[deploy] applying manifest (Deployment + read-only SA/Role/RoleBinding + Service)"
kubectl apply -f "${MANIFEST}"

echo "[deploy] restarting to pick up the freshly imported image"
kubectl -n demo rollout restart deploy/aiops-agent
kubectl -n demo rollout status deploy/aiops-agent --timeout=120s

echo "[deploy] verifying the read-only RBAC the k8s tools need"
kubectl -n demo auth can-i list pods \
  --as="system:serviceaccount:demo:aiops-agent" && echo "  pods: OK"
kubectl -n demo auth can-i get deployments.apps \
  --as="system:serviceaccount:demo:aiops-agent" && echo "  deployments: OK"
# Negative check: it must NOT be able to mutate (write actions stay out of this SA).
if kubectl -n demo auth can-i delete pods \
     --as="system:serviceaccount:demo:aiops-agent" | grep -q yes; then
  echo "  WARN: SA can delete pods — RBAC is too broad" >&2
else
  echo "  delete pods: correctly DENIED (read-only)"
fi

echo "[deploy] done. agent: http://localhost:8000/healthz"
