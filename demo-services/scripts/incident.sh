#!/usr/bin/env bash
# Start or stop one of the demo's incident scenarios.
#
#   ./scripts/incident.sh start session-cache
#   ./scripts/incident.sh stop  session-cache
#   ./scripts/incident.sh status
#
# Two scenarios, deliberately different shapes:
#
#   bad-validator   payment-service declines odd-cent charges. Cause and symptom
#                   live in the same service, and the reason is on a Prometheus
#                   label, so it can be found without leaving the metric.
#   session-cache   user-service's auth check falls through to a slow session
#                   store. The alert fires on ORDER-service; the cause is one
#                   hop upstream. Nothing about it is visible from order-service
#                   metrics alone — that is what it is for.
set -euo pipefail

NS="${NAMESPACE:-demo}"
ACTION="${1:-status}"
SCENARIO="${2:-}"

flag() {  # flag <configmap> <key> <true|false>
  local cm="$1" key="$2" value="$3"
  kubectl -n "${NS}" patch configmap "${cm}" --type merge \
    -p "{\"data\":{\"flags.json\":\"{\\\"${key}\\\": ${value}}\"}}" >/dev/null
  echo "[incident] ${cm}: ${key}=${value}"
}

case "${ACTION}:${SCENARIO}" in
  start:session-cache)
    flag user-flags user_session_cache_disabled true
    echo "[incident] user-service auth checks now reach the session store."
    echo "[incident] The flag file is projected from the ConfigMap, so it takes"
    echo "[incident] up to ~60s to land. No restart — a rollout in the same"
    echo "[incident] minute would give the latency chart a second explanation."
    ;;
  stop:session-cache)
    flag user-flags user_session_cache_disabled false
    ;;
  start:bad-validator)
    flag payment-flags payment_use_new_validator true
    kubectl -n "${NS}" rollout restart deployment/payment-service
    echo "[incident] payment-service reads this flag at startup, hence the restart."
    ;;
  stop:bad-validator)
    flag payment-flags payment_use_new_validator false
    kubectl -n "${NS}" rollout restart deployment/payment-service
    ;;
  status:*)
    for cm in payment-flags user-flags; do
      printf '%-16s %s\n' "${cm}" \
        "$(kubectl -n "${NS}" get configmap "${cm}" -o jsonpath='{.data.flags\.json}' 2>/dev/null || echo '(missing)')"
    done
    ;;
  *)
    echo "usage: $0 {start|stop} {session-cache|bad-validator} | $0 status" >&2
    exit 2
    ;;
esac
