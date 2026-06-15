# AIOps Agent — 啟動 / 測試 Runbook

> v3 起架構變了：**agent 跑在 demo-services 的 k3d cluster 內**（不再是 host-side
> + mcp-grafana sidecar）。tools 直接打 Prometheus/Loki/Tempo 的 native API（透過
> cluster 內部 DNS），並用唯讀 ServiceAccount 讀 k8s。plugin 由 `up.sh` 自動
> provision 進 Grafana。
>
> 舊的「四個 terminal（mcp-grafana + host service + plugin watch）」流程已淘汰。

```
demo-services k3d cluster (ns demo)
├─ Grafana :3001  ── AIOps app plugin (chat UI)
├─ webapp :8002 / payment :8001 (direct)
├─ Prometheus / Loki / Tempo / otel-collector
├─ 5 demo services (payment / user / order / api-gateway / webapp)
└─ aiops-agent :8000  ──→ 上面全部 (native API + read-only k8s SA)
```

---

## 一次性 setup（每台機器一次）

```bash
# 1. plugin 依賴 + build（up.sh 會把 dist 塞進 cluster）
cd aiops-agent/plugin && npm install && npm run build   # ⚠️ 不要跑 npm audit fix --force

# 2. agent service 依賴 + 環境變數
cd ../service && uv sync
cp .env.example .env        # 編輯填 GOOGLE_API_KEY

# 3. agent secret（GOOGLE_API_KEY，webhook 要測再加 webhook-secret）
kubectl -n demo create secret generic aiops-agent-secrets \
  --from-literal=google-api-key="$GOOGLE_API_KEY" \
  --from-literal=github-token="${GITHUB_TOKEN:-}" \
  --from-literal=webhook-secret="dev-webhook-secret-1234"
```

---

## Step 1 — 起 cluster + 流量

```bash
cd demo-services
./scripts/up.sh         # k3d + 5 services + Prom/Loki/Tempo + Grafana + agent + plugin
./scripts/load.sh &     # 持續打一般流量
```

健康檢查：

```bash
curl -s http://localhost:3001/api/health    # Grafana
curl -s http://localhost:8000/healthz       # agent  → {"ok":true}
```

## Step 2 — 部署 / 更新 agent

改了 `service/` 的 code 後，用這支把新 image 建好、匯入 k3d、套 manifest（含唯讀
RBAC）、滾動重啟、驗證權限：

```bash
cd aiops-agent && ./scripts/deploy.sh
```

它會驗證 SA 可 list pods/deployments、且**不可 delete**（read/write 權限分離）。

## Step 3 — 在 plugin UI 用

開 **http://localhost:3001 → 左側 Apps → AIOps → Chat**，例如：

- `payment-service 的 pod 健康嗎？跑哪個版本？` → 觸發 `k8s_pod_status`
- `payment-service p95 latency` → render promql 圖
- `payment-service 最近的錯誤 log` → render logql panel（見下面「弄活 demo」才有 decline log）
- `為什麼 payment-service 在 decline？` → 走完整 RCA（metrics→logs→指認版本）

> 改 plugin code 後 node 內的 copy 不會自動同步：
> `docker cp aiops-agent/plugin/dist k3d-demo-services-server-0:/aiops-plugin/tedmax100-aiops-app && kubectl -n demo rollout restart deploy/grafana`

---

## 弄活 demo（製造 payment v2.5.0 decline 事件）

預設 demo 很安靜——服務只在事件時記 log，且要有正確金額才會 decline。兩個坑：

1. **flag 啟動時讀一次**：把 ConfigMap 設 true 後要重啟 payment 才生效。
2. **`load.sh` 金額永遠偶數**（價格 `100*i`），但 decline 條件是奇數分 → load.sh
   無法觸發 decline，要直接打奇數金額的 charge。

```bash
# 1. 打開 new validator + 確認版本 v2.5.0，然後重啟讓 flag 生效
kubectl -n demo create configmap payment-flags \
  --from-literal=flags.json='{"payment_use_new_validator": true}' \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n demo rollout restart deploy/payment-service

# 2. 直接對 payment 打奇/偶混合的 charge（奇數 → 402 declined）
while true; do
  for n in $(seq 5); do
    base=$(( (RANDOM % 50 + 1) * 100 ))
    [ $(( RANDOM % 5 )) -lt 2 ] && amt=$((base+1)) || amt=$base   # ~40% odd
    curl -s -o /dev/null -X POST http://localhost:8001/charge -H 'content-type: application/json' \
      -d "{\"order_id\":\"o-$RANDOM\",\"user_id\":\"u-$((RANDOM%5+1))\",\"amount_cents\":$amt}"
  done; sleep 1
done &
```

約 1 分鐘後就能查到 `payment_charges_total{status="declined",reason="new_validator",git_version="v2.5.0"}`
與 `event="payment.declined"` 的 log。

---

## 測試 headless 路徑（webhook → runbook → governance）

需要 secret 裡有 `webhook-secret`（見 setup）。

```bash
curl -s -X POST 'http://localhost:8000/webhook/alert?token=dev-webhook-secret-1234' \
  -H 'content-type: application/json' \
  -d '{"alerts":[{"status":"firing",
       "labels":{"alertname":"payment-decline-rate-high","service_name":"payment-service","git_version":"v2.5.0","severity":"warning"},
       "annotations":{"summary":"decline spike","runbook_id":"payment-bad-deploy"},
       "startsAt":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}]}'

# 看 runbook 匹配 → Tier-1 診斷 → findings → 治理決策
kubectl -n demo logs deploy/aiops-agent -f | grep -E 'headless|governance|runbook'
```

事件活躍時，結論應為高信心並指認 v2.5.0 validator；治理決策為
`k8s.rollout_undo -> PROPOSE`（須核准、不自動執行）。
同一 alertname+service+git_version 在 10 分鐘 cooldown 內只跑一次（改 alertname 可強制重跑，runbook 仍靠 `runbook_id` 匹配）。

## 校準（CE harness）

```bash
kubectl -n demo exec deploy/aiops-agent -- python -m app.calibration report
kubectl -n demo exec deploy/aiops-agent -- python -m app.calibration label <fingerprint> --correct   # 或 --wrong
```

`label` 的 fingerprint = webhook 回傳的 `accepted[]` 值（也是 headless log 的 `fp=`）。

---

## 單元測試

```bash
cd aiops-agent/service && uv run pytest -q     # k8s / calibration / runbook / governance
```

## 關閉 / 清理

```bash
pkill -f load.sh; pkill -f 'charge'            # 停流量產生器
cd demo-services && ./scripts/down.sh          # 刪 k3d cluster
```

---

## Troubleshooting

| 症狀 | 原因 | 修法 |
|------|------|------|
| 錯誤 log 查詢回 `No data` | 用了 `{service=...}` 或 `\| level="ERROR"` | selector 是 **`service_name`**；logs 全 INFO **沒有 `level` 欄位**，用 `\| event="payment.declined"` |
| decline 一直是 0 | flag 沒生效 / 金額是偶數 | 重啟 payment；直接打奇數金額 charge（見「弄活 demo」）|
| `/webhook/alert` 回 503 | 沒設 `webhook-secret` | secret 加 `webhook-secret` key 後 `deploy.sh` |
| `/webhook/alert` 回 401 | token 不對 | `?token=` 要等於 secret 的 `webhook-secret` |
| k8s 工具回 `unavailable` | SA/RBAC 沒套 | 跑 `deploy.sh`（會建 SA/Role/RoleBinding 並驗證）|
| plugin 看不到 AIOps app | dist 沒進 node 或沒 enable | 重跑 `up.sh`；或 UI `/plugins/tedmax100-aiops-app` 手動 Enable |
| chat 氣泡空白 | SSE CRLF / multipart content | 見 [../doc/aiops-agent-mvp-notes.md](../doc/aiops-agent-mvp-notes.md) 第 11、12 條 |

## 相關文件

- ARE 落差分析 + 路線：[../doc/agents/aiops-agent-ARE-gap-analysis.md](../doc/agents/aiops-agent-ARE-gap-analysis.md)
- v3 設計：[../doc/aiops-agent-design-v3.md](../doc/aiops-agent-design-v3.md)
- 開發踩坑：[../doc/aiops-agent-mvp-notes.md](../doc/aiops-agent-mvp-notes.md)
