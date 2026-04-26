# tasks-spec/ 目錄說明

Benchmark 的 task 定義來源，是整個專案唯一需要手動維護的 task 資料。`tasks/` 是從這裡生成的 output，不要直接編輯 `tasks/`。

---

## 結構

```
tasks-spec/
  prometheus_query/    # PromQL 查詢與分析（16 個 task）
  loki_query/          # LogQL 查詢與分析（10 個 task）
  tempo_query/         # TraceQL 查詢與 trace 分析（13 個 task）
  grafana_api/         # Grafana REST API 操作（6 個 task）
  dashboarding/        # Dashboard 建立與修改（7 個 task）
  investigation/       # 跨訊號 incident 調查（11 個 task）
```

共 63 個 task。修改後執行 `mise run setup:sync` 重新生成 `tasks/`。

---

## Spec YAML 格式

每個 `.yaml` 檔案的頂層欄位：

```yaml
id: task-id                  # 唯一識別，對應生成後的目錄名稱
category: prometheus_query   # 六種類別之一
tags: [promql, metrics]      # 任意 tag，用於篩選和報告
statement: |                 # 給 agent 的題目文字（user-voiced，不含評分細節）
  ...
checks: []                   # Deterministic checks（可空）
rubric:                      # LLM rubric criteria（可空）
  - criterion: ...
    weight: 30
    fact: ...                # 可選的 ground truth query
```

---

## Rubric Criterion

```yaml
rubric:
- criterion: The final response states 5xx share accurately.
  weight: 65
  fact:
    kind: query
    backend: prometheus
    query: sum(rate(http_requests_total{status=~"5.."}[1h])) / sum(rate(...))
```

- **criterion**：judge 評估的條件，用自然語言寫，`The final response` 指 agent 的最後一則文字回覆
- **weight**：相對權重，所有 criteria 會正規化成加總 100%
- **fact**（選用）：benchmark 自己查詢的 ground truth，結果以 `Source of truth: ...` 插入 judge prompt

---

## Checks（Deterministic）

```yaml
checks:
- name: Response cites a trace ID that appears in Tempo tool results
  weight: 70
  type: grounding
  params:
    mode: tool_trace_id
    prefix_min_chars: 8

- name: Grafana has a Service Overview dashboard with correct panels
  weight: 40
  type: state
  params:
    mode: dashboard_state
    title: Service Overview
    panels:
      - type_any_of: [timeseries, stat]
        datasource_type: prometheus
        match_count: 4
```

兩種 type：
- **grounding**：驗答案裡引用的具體實體（trace ID）是否真的出現在工具結果裡
- **state**：驗 Grafana stack 的實際狀態（dashboard、datasource）

---

## Fact 種類

| kind | backend/resource | 用途 |
|---|---|---|
| `query` | `prometheus` | 跑 PromQL，取 scalar 或 vector |
| `query` | `loki` | 跑 LogQL，取計數或比率 |
| `query` | `tempo` | 搜 trace，取 trace list |
| `resource` | `dashboard` | 從 Grafana 抓 dashboard JSON |
| `resource` | `datasource_inventory` | 從 Grafana 抓所有 datasource |
| `resource` | `datasource_detail` | 從 Grafana 抓特定 datasource |

---

## 六種類別

### prometheus_query（16 個）

測試 agent 能否：
- 寫出正確 PromQL（rate、increase、topk、subquery、offset）
- 解讀 metrics 數值並與題目情境對應
- 用時間窗口（1h、6h）正確限定查詢範圍

代表題目：

| task | 測什麼 |
|---|---|
| `promql-error-rate` | 三個 service 合計 5xx share |
| `promql-burn-rate-assessment` | 比較 payment-service 現在 vs 6h 前的 error rate（`offset`）|
| `promql-subquery-peak-error-rate` | 找 6h 內的 error rate 峰值（subquery）|
| `promql-topk-5xx-share` | 哪個 backend 貢獻最多 5xx（`topk`）|
| `query-cpu-metrics` | 比較各 job 的 CPU 用量並排名 |
| `promql-capacity-analysis` | 記憶體用量趨勢與容量評估 |

---

### loki_query（10 個）

測試 agent 能否：
- 寫 LogQL 的 JSON 解析 pipeline（`| json | __error__=""`)
- 用 `count_over_time`、`unwrap`、`sum by` 做聚合
- 找最慢 endpoint、最高 5xx 路徑、deployment 事件

代表題目：

| task | 測什麼 |
|---|---|
| `logql-top-5xx-endpoint` | 按 path 統計 5xx，找最多那條 |
| `logql-multi-stage-pipeline` | 找最慢 endpoint（`unwrap duration_ms`）|
| `logql-parse-json-logs` | JSON log 解析與欄位過濾 |
| `logql-unwrap-orders-p95-latency` | 計算 /api/orders 的 p95 latency |
| `logql-retry-vs-real-errors` | 區分 retry 造成的 5xx 和真實錯誤 |
| `logql-deployment-rollout-events` | 從 log 找 deployment 事件時間線 |

---

### tempo_query（13 個）

測試 agent 能否：
- 用 TraceQL 搜尋 trace（`{ resource.service.name = "..." }`）
- 找 failing trace 並說明 error 在 call chain 哪裡發生
- 引用真實的 trace ID（有 grounding check 防 hallucination）
- 分析 parent-child span 關係和 latency

代表題目：

| task | 測什麼 |
|---|---|
| `traceql-error-chain-orders` | 找 POST /api/orders 的 failing trace，說明 error 傳播路徑 |
| `traceql-structural-query` | order-service checkout 流程的 downstream call chain |
| `traceql-tail-latency-bottleneck` | 找 p99 最慢的 service 和 span |
| `traceql-metrics-error-rate-by-service` | 用 TraceQL metrics 計算各 service error rate |
| `trace-error-analysis` | 找 error span 並分析根因 |

---

### grafana_api（6 個）

測試 agent 能否直接操作 Grafana REST API（不只是用 Grafana UI）：

| task | 測什麼 |
|---|---|
| `list-datasources` | 列出所有 datasource 的名稱和 type |
| `get-datasource-details` | 取得特定 datasource 的 URL 和 access mode |
| `search-dashboards` | 搜尋 dashboard list |
| `inspect-dashboard-queries` | 讀取 dashboard panels 裡的查詢內容 |
| `audit-service-overview-datasources` | 確認 dashboard 使用了哪些 datasource |
| `audit-service-overview-variable` | 確認 dashboard variable 的設定 |

這類 task 大多同時有 deterministic check（直接驗 Grafana 狀態）和 LLM rubric（驗回答是否忠實呈現資料）。

---

### dashboarding（7 個）

測試 agent 能否建立或修改 Grafana dashboard，且結果在 Grafana 上真實可用。評分以 deterministic state check 為主（直接查 Grafana API 驗證）。

| task | 測什麼 |
|---|---|
| `dashboard-create-service-overview` | 建立含 timeseries、stat、logs panel、variable、annotation 的完整 dashboard |
| `dashboard-add-cache-lag-panels` | 在現有 dashboard 加入 cache lag 相關 panels |
| `dashboard-add-deployment-annotation` | 加入 deployment 事件 annotation |
| `dashboard-update-add-service-variable` | 加入 service dropdown variable 並讓所有 panel 跟隨 |
| `dashboard-repair-cache-review` | 修復有問題的 dashboard |

`dashboard-create-service-overview` 是最複雜的 task，要求一次建立 5 種 panel type + 1 個 multi-value variable + 1 個 Loki annotation，並驗證 variable binding 在選不同 service 時真的生效（execute_cases）。

---

### investigation（11 個）

測試 agent 能否像 SRE 一樣，跨 metrics + logs + traces 做 incident 分析。題目用情境式自然語言描述（像 on-call 問你），不給 PromQL/LogQL 提示。

| task | 測什麼 |
|---|---|
| `incident-triage` | 跨 Prometheus + Loki 找出哪些 service 受影響、何時開始、是否相關 |
| `payments-path-root-cause` | 用 logs + traces 找出 /api/payments 是不是 root cause |
| `dependency-outage-false-lead` | api-gateway 的 5xx 是自己的問題還是 downstream 傳上來的？|
| `service-degradation-rca` | 找出哪個 service 最先出問題 |
| `slow-path-hotspot-correlation` | 把 slow path 的 logs 和 metrics 對應起來 |
| `cache-incident-blast-radius` | cache 問題影響了哪些 service |
| `retry-backlog-incident` | retry backlog 累積的原因和影響範圍 |

Investigation task 通常沒有 deterministic check（`checks: []`），全靠 LLM rubric，且 criteria 包含：
- 數字準確性（有 fact 提供 ground truth）
- 推理過程是否基於工具結果（transcript evidence）
- 結論是否區分主因和次要影響

---

## 新增 Task 的注意事項

1. **statement 用 user-voiced 語氣**，不要在題目裡洩露評分細節（不要寫「用 PromQL rate function」）
2. **數字精確性用 fact**，而不是把答案寫死在 criterion 文字裡
3. **cite 具體實體（trace ID）用 grounding check**，不要靠 LLM 判斷
4. **dashboard 狀態用 state check**，不要靠 LLM 判斷 dashboard 有沒有建起來
5. 修改完執行 `mise run setup:sync` 重新生成 `tasks/`
6. Prometheus fact query 在 sync 時會被 `promql_parser` 驗證，語法錯誤會馬上報錯
