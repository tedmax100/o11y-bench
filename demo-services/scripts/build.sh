#!/usr/bin/env bash
# Build payment-service image and import into the k3d cluster.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER="demo-services"
IMAGE="demo-services/payment:dev"

docker build -t "${IMAGE}" -f "${ROOT}/services/payment/Dockerfile" "${ROOT}"
k3d image import "${IMAGE}" -c "${CLUSTER}"
