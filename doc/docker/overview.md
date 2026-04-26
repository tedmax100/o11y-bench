# docker/ 目錄說明

o11y-bench 的 sidecar 容器（`o11y-bench-o11y-stack`），負責在每次 benchmark trial 時提供一個完整、有合成資料的 observability stack。

---

## 檔案對照

```
docker/
  Dockerfile                              # sidecar image 定義
  entrypoint.sh                           # 容器啟動腳本，控制服務啟動順序
  prometheus.yml                          # Prometheus 設定
  loki-config.yaml                        # Loki 設定
  tempo.yaml                              # Tempo 設定
  datasources.yaml                        # Grafana datasource 預設設定
  python/src/o11y_stack/
    generate_data.py                      # 合成遙測資料產生器（metrics + logs + traces）
    provision_task_resources.py           # task 專屬 Grafana 資源建立器
```

---

## 容器啟動流程（entrypoint.sh）

每次 Harbor 起一個新 trial，這個容器就從頭啟動，順序是：

```
1. 啟動 Loki、Tempo、Grafana（三個同時背景啟動）
       ↓
2. 等 Loki ready（:3100/ready）
3. 等 Tempo ready（:3200/ready + OTLP :4318）
       ↓
4. 執行 generate_data.py（產生 24 小時的合成資料）
   → 寫入 Prometheus TSDB block（用 promtool）
   → 推送 traces 到 Tempo（OTLP HTTP）
   → 推送 logs 到 Loki（push API）
       ↓
5. 啟動 Prometheus（在資料寫好後才啟動，確保 TSDB 能讀到資料）
       ↓
6. 等 Prometheus ready（:9090/-/ready）
7. 等 Grafana ready（:3000/api/health）
       ↓
8. 執行 provision_task_resources.py（建立 task 需要的 Grafana dashboard）
       ↓
9. 啟動 mcp-grafana（:8080，streamable-http 模式）
       ↓
10. 等 mcp-grafana ready
       ↓
=== Environment Ready ===
```

Prometheus 故意在資料產生完才啟動，因為 TSDB 的 block 必須在啟動前就存在才能被讀到。

---

## generate_data.py — 合成遙測資料

### 模擬的系統

一個電商平台，5 個微服務：

```
webapp → api-gateway → user-service
                     → order-service → user-service
                                     → payment-service
                     → payment-service
```

5 個 HTTP endpoint：

| method | path | 對應服務 |
|---|---|---|
| GET | /api/users | user-service |
| GET | /api/products | order-service |
| POST | /api/orders | order-service |
| POST | /api/payments | payment-service |
| GET | /api/cart | order-service |

### 資料規格

- **時間範圍**：24 小時（`HOURS_OF_HISTORY = 24`）
- **Metrics 間隔**：30 秒一筆（`METRICS_INTERVAL = 30`）
- **時間基準**：從 `O11Y_SCENARIO_TIME_ISO` 環境變數讀取，確保每次跑同一題資料完全一樣
- **隨機種子**：`random.seed(42)`，deterministic

### 三個刻意埋入的 incident

這是整個 benchmark 的核心，資料不是隨機產生的，而是刻意設計了三段故障：

#### Incident 1：Error Spike（資料結尾前 3 小時，持續 30 分鐘）

payment-service 大量出錯，cascading 到上游：

| 服務 | 故障期間 error rate | 正常 error rate |
|---|---|---|
| payment-service | **70%** | 2% |
| order-service | **15%**（cascading）| 2% |
| api-gateway | **8%**（cascading）| 2% |
| 其他 | 2% | 2% |

故障前 2 分鐘：Loki 有 `payment-service` 和 `order-service` 的 deployment log（v2.4.1 → v2.5.0），暗示是部署引發的。

#### Incident 2：Latency 劣化（資料結尾前 6 小時，持續 45 分鐘）

order-service 回應變慢，upstream 連帶受影響：

| 服務 | latency 倍數 |
|---|---|
| order-service | **5x** |
| api-gateway | 2x |
| webapp | 2x |

`/api/orders` 有 60% 機率被標成 slow request（duration_ms 500–3000ms）。

#### Incident 3：Cache Refresh Lag（資料結尾前 9 小時，持續 40 分鐘）

user-service 的 auth cache 更新卡住：

- `/api/users` 有 40% 機率慢
- `service_cache_refresh_lag_seconds` metric 最高到 520 秒
- Loki 有 `cache refresh lag elevated` warn log，附 `lag_seconds` 和 `stale_keys` 欄位
- 故障前 3 分鐘：user-service 的 deployment log

### Prometheus metrics

| metric | 說明 |
|---|---|
| `http_requests_total{job, instance, status}` | 各服務 request 數，按 HTTP status code 分 |
| `http_request_duration_seconds` | latency histogram，標準 11 個 bucket（0.005 到 10.0）|
| `process_cpu_seconds_total` | 累積 CPU 秒數（counter）|
| `process_resident_memory_bytes` | 記憶體用量（gauge）|
| `service_retry_queue_depth` | retry backlog 深度，故障期間升高 |
| `service_cache_refresh_lag_seconds` | cache 更新 lag（user-service 專用）|
| `go_goroutines` | goroutine 數（隨流量浮動）|
| `up` | scrape target 健康，永遠為 1 |

### Loki logs

JSON 格式，每筆 log 有：

```json
{
  "timestamp": "2026-04-04T10:05:14.123Z",
  "level": "error",
  "service": "payment-service",
  "method": "POST",
  "path": "/api/payments",
  "status": 500,
  "duration_ms": 23.4,
  "trace_id": "a3f2d1...",
  "message": "request failed"
}
```

log label：`job`、`service`、`level`

四種 log 類型：
- **request log**（info/error）：每個 request 一筆，含 trace_id
- **warning log**（warn）：slow query、high memory、retry attempt
- **retry backlog log**（warn）：`queue_depth` 升高時產生，`queue: "payment-retries"`
- **cache refresh log**（warn）：`lag_seconds` 和 `stale_keys`
- **deployment log**（info）：`event: "deployment"`，故障前幾分鐘出現

`trace_id` 欄位和 Tempo 的 trace 是同一個，可以跨 signal 對應。

### Tempo traces

每個 request 一條 trace，call chain：

```
webapp (root span)
  └─ api-gateway
       └─ target-service  ← error 標在這裡（span status = ERROR）
            └─ dependency（70% 機率）
```

關鍵設計：**error 的 span status 只標在真正的根源 span**，upstream 的 webapp 和 api-gateway 只帶 HTTP 500 status code 但 span status 是 OK。這樣 AI 必須真的去追 call chain 才能找到根因，不能只看最外層。

cache refresh 發生時，`/api/users` 的 target span 下還會多一個 `refresh auth cache` child span，模擬 cache 操作阻塞請求。

---

## provision_task_resources.py — Task 資源初始化

### 用途

某些 task 需要 Grafana 上已經存在某個 dashboard（例如「修復這個壞掉的 dashboard」、「讀取這個 dashboard 的 panels」），`provision_task_resources.py` 負責在 stack 啟動後把這些 dashboard 預先建好。

### 觸發時機

`entrypoint.sh` 在 Grafana ready 後呼叫，讀取 `/task/setup.json`（由 Harbor 從 task 的 `environment/setup.json` 掛入）。

### setup.json 格式

task spec 裡的 `setup_dashboards` 欄位，在 `sync_tasks.py` 生成時會輸出成 `setup.json`：

```yaml
# tasks-spec/grafana_api/search-dashboards.yaml
setup_dashboards:
- uid: svc-overview
  title: Service Overview
  panels:
  - id: 1
    title: Request Rate
    type: timeseries
    ...
```

### 執行邏輯

```
讀 /task/setup.json
  └─ 取 setup_dashboards 列表
       └─ 對每個 dashboard payload：
            1. build_dashboard()：補齊必要欄位（id=null、schemaVersion=41 等）
            2. POST /api/dashboards/db（overwrite=true）
            3. wait_dashboard_visible()：輪詢 GET /api/dashboards/uid/{uid}
               確認 dashboard 真的可讀（最多等 30 秒）
```

### 哪些 task 用到

目前 9 個 task 有 `setup_dashboards`，主要是：

- `grafana_api/`：search-dashboards、inspect-dashboard-queries、audit-* 系列（需要有現成的 dashboard 可以查）
- `dashboarding/`：dashboard-repair-cache-review（需要有一個預先建好的壞 dashboard 讓 agent 去修）

沒有 `setup_dashboards` 的 task，`provision_task_resources.py` 讀到空 list 直接印 `No task Grafana resources to provision` 結束。
