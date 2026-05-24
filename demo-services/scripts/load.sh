#!/usr/bin/env bash
# Continuously hit webapp's /api/* endpoints to generate telemetry.
# Usage: ./scripts/load.sh [rps] [duration_seconds]
# Defaults: 5 rps, run until Ctrl-C.
set -euo pipefail

RPS="${1:-5}"
DURATION="${2:-0}"  # 0 = forever
BASE="${WEBAPP_URL:-http://localhost:8002}"

INTERVAL_MS=$(awk -v r="${RPS}" 'BEGIN { printf "%d", 1000 / r }')
DEADLINE=$(( $(date +%s) + DURATION ))

users=(u-1 u-2 u-3 u-4 u-5 u-7 u-12 u-99)   # u-99 doesn't exist → 401 / 404 path
products=(p-1 p-2 p-3 p-5 p-9 p-99)         # p-99 doesn't exist → 404 path

echo "[load] hitting ${BASE} at ~${RPS} rps (Ctrl-C to stop)"
while true; do
  if [ "${DURATION}" -ne 0 ] && [ "$(date +%s)" -ge "${DEADLINE}" ]; then
    echo "[load] duration ${DURATION}s reached"
    exit 0
  fi

  # Pick a random endpoint mix
  case $(( RANDOM % 5 )) in
    0) curl -sS -o /dev/null "${BASE}/api/users" || true ;;
    1) curl -sS -o /dev/null "${BASE}/api/products" || true ;;
    2) curl -sS -o /dev/null "${BASE}/api/cart?user_id=${users[$RANDOM % ${#users[@]}]}" || true ;;
    3|4)
      u="${users[$RANDOM % ${#users[@]}]}"
      p="${products[$RANDOM % ${#products[@]}]}"
      curl -sS -o /dev/null -X POST "${BASE}/api/orders" \
        -H 'content-type: application/json' \
        -d "{\"user_id\":\"${u}\",\"product_id\":\"${p}\",\"quantity\":$((RANDOM % 3 + 1))}" || true
      ;;
  esac

  sleep "$(awk -v ms="${INTERVAL_MS}" 'BEGIN { printf "%.3f", ms / 1000 }')"
done
