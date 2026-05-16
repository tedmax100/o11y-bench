# AIOps Agent 設計思路：LangGraph + Grafana Plugin 的 RCA 介面

> 為什麼選這條路、各層的取捨在哪、最容易踩雷的地方是什麼。一份在動手寫 code 之前先把架構想清楚的筆記。

* * *

## 1. 要解決的問題

當 on-call 收到一個 alert，真正花時間的不是「看儀表板」，而是：

- 在 metrics / logs / traces / k8s state 之間來回切換
- 把分散的線索（哪個 pod 重啟、哪段 trace 變慢、哪類 log 開始噴）拼成一個因果鏈
- 用人腦記著「我剛剛查過什麼、還有什麼假設沒驗」

這套流程很適合交給一個 **stateful、會用工具、會推理** 的 agent 來輔助。目標不是取代 SRE，而是把「重複的 query 編寫 + 多訊號交叉比對」這層自動化，讓人專心做判斷。

* * *

## 1.5 Prior Art：跟 Ask O11y 的差別

[Consensys Ask O11y](https://grafana.com/grafana/plugins/consensys-asko11y-app/) 是目前最接近的現成方案，值得拿來對照——它證明了 app plugin + 自然語言查詢這條路是走得通的，但也暴露了我們想解決的限制。

| 維度 | Ask O11y | 本設計 |
|------|----------|--------|
| Plugin 形式 | App plugin (chat UI) | App plugin (chat UI) ✓ |
| LLM 接入 | **必須**透過 [grafana-llm-app](https://grafana.com/grafana/plugins/grafana-llm-app/) | 獨立 agent service，LLM provider 自選 |
| 推理深度 | NL → query 翻譯（單步） | 多步 graph：假設 → 收證據 → 關聯 → 合成 |
| Tool 來源 | 綁 grafana-mcp（45-56 個 tool） | grafana-mcp + k8s + 自家內部 MCP |
| 內部系統整合 | 沒有擴充點 | 直接加 MCP server 就能接（PagerDuty / 內部 deploy / 自家 CMDB...） |
| State / 多輪 | session 記錄 chat history | LangGraph checkpointing，可從任一節點分支重跑 |
| RCA 模式 | alert investigation prompt | 整個 graph 為 RCA 設計，不是 prompt 套殼 |
| Self-host 程度 | 依賴 grafana-llm-app 設定 | 完全 self-host，agent service 是自己的 |

**為什麼不直接用 Ask O11y？**

1. **被 grafana-llm-app 綁住**：LLM provider、prompt、tool 編排都受它約束。要做 prompt caching、要換模型、要加自家的 evaluation harness 都不方便。
2. **單步翻譯 vs 多步推理**：「找出 checkout 服務最近的 error log」這種任務 Ask O11y 表現不錯，但「為什麼下午三點 p99 latency 飆高」這種需要跨訊號推理的，單步 NL→query 力有未逮。
3. **內部系統封閉**：公司自家的 deploy system、CMDB、變更管理、內部 wiki，這些才是 RCA 最關鍵的 context。Ask O11y 沒有開放架構讓你接這些，但 MCP 是現成的擴充點。
4. **不能 fine-tune for domain**：我們可以針對自家 service catalog、log schema、常見 incident pattern 調整 graph 行為；Ask O11y 是通用工具，你只能在 prompt 層做有限調整。

**換句話說，方向是對的，但深度跟整合彈性不夠**——這就是自建的價值所在。如果只是想要 Grafana 裡有個 chat box 翻 PromQL，直接裝 Ask O11y 就好，不用自己做。

* * *

## 2. 整體架構

```plaintext
┌──────────────────────────────────────────────────┐
│  Grafana (UI 層)                                  │
│  ┌────────────────────────────────────────────┐  │
│  │  Custom Plugin (panel or app)               │  │
│  │  - chat UI / streaming                      │  │
│  │  - dashboard context passthrough            │  │
│  └──────────────────┬─────────────────────────┘  │
└─────────────────────┼────────────────────────────┘
                      │  HTTP / SSE
                      ▼
┌──────────────────────────────────────────────────┐
│  LangGraph Agent Service                          │
│  ┌────────────────────────────────────────────┐  │
│  │  Graph: intake → hypothesize → gather       │  │
│  │         → correlate → synthesize            │  │
│  └──────────────────┬─────────────────────────┘  │
│                     │ MCP client                  │
└─────────────────────┼────────────────────────────┘
                      │
       ┌──────────────┼──────────────┬────────────┐
       ▼              ▼              ▼            ▼
   grafana-mcp   kubernetes-mcp   (custom mcp)  ...
       │              │
       ▼              ▼
  Loki / Tempo /    k8s API
  Prometheus
```

三個主要元件：

1. **Grafana plugin**：使用者介面，吃 dashboard 的 time range / variables 當 context
2. **LangGraph agent service**：推理跟編排，stateful graph
3. **MCP tool layer**：跟各 backend 對接的標準化介面

* * *

## 3. UI 層：Panel Plugin vs App Plugin

[Grafana panel plugin tutorial](https://grafana.com/developers/plugin-tools/tutorials/build-a-panel-plugin) 是合理的起點，但要先決定用哪種 plugin 類型。

| 維度 | Panel Plugin | App Plugin |
|------|--------------|------------|
| 定位 | dashboard 裡的一格 | 獨立 nav page |
| UI 自由度 | 受 grid 約束 | 全頁面 |
| 取得當前 panel/dashboard context | 直接拿 | 要自己傳 |
| 對話歷史 / 多輪 session | 不自然 | 自然 |
| 認證 | 走 Grafana session | 走 Grafana session |
| 適合場景 | dashboard 旁邊的小助手 | 獨立 AIOps 介面 |

**判斷準則**：
- 想做「按一下 panel 旁邊的按鈕，agent 分析這張圖」→ panel plugin
- 想做「全頁面、可累積會話、能跨 dashboard 引用」→ app plugin

實務上很多團隊會兩個都做：app plugin 是主介面，panel plugin 是入口按鈕，點下去把 panel context 帶到 app plugin 開新 session。

* * *

## 4. Agent 層：LangGraph Graph 設計

核心 graph 大致這幾個 node：

```plaintext
  ┌─────────────┐
  │  intake     │  ← 接收 alert / 使用者問題 + dashboard context
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ hypothesize │  ← 生成候選假設（CPU 飽和？依賴掛了？最近 deploy？）
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │   gather    │  ← 並行打多個 MCP tool 收證據
  └──────┬──────┘     （可循環，根據結果再產新假設）
         ▼
  ┌─────────────┐
  │  correlate  │  ← 時序對齊、跨訊號 join、跟 k8s event 比對
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │  synthesize │  ← 產生根因敘述 + 建議行動
  └─────────────┘
```

幾個 LangGraph 才有的好處值得用上：

- **Stateful checkpointing**：每個 node 的中間狀態存下來，使用者可以從任一步驟回頭改假設或補 query
- **Conditional edges**：`gather → correlate` 還是 `gather → hypothesize`（證據不足回頭再想）是由 LLM 自己決定
- **Streaming**：node 之間的事件可以 stream 回 UI，讓使用者看到 agent 正在做什麼，而不是盯著 spinner

* * *

## 5. Tool 層：MCP 整合

不用自己造輪子，現成的可以直接接：

- **[grafana-mcp](https://github.com/grafana/mcp-grafana)**：Loki / Tempo / Prometheus / Incident / OnCall / Dashboard 查詢
- **kubernetes-mcp-server**：pods / events / logs / describe / 最近 deploy 歷史
- 其他依需要加：GitHub MCP（看最近 PR）、PagerDuty MCP、自家 deploy system 的 MCP

MCP 的好處是 tool schema 標準化，agent 換 LLM provider 不用改 tool 定義。

* * *

## 6. 最大痛點：Logs / Traces 的資料量

這是整個設計裡最值得花時間的地方。

### 6.1 問題

- Loki 一次 `range query` 拉 1000 行 log 是常態
- 直接塞給 LLM：(a) token 爆量 (b) **lost in the middle**——中段內容 LLM 幾乎讀不到 (c) 容易幻覺出根本不存在的 error pattern
- Traces 更慘，一個 trace 樹展開幾百個 span，每個 span attributes 都是 JSON

### 6.2 解法：Schema-first + 下推到 datasource

**順序很重要**，由上到下：

#### Step 1：把 log/trace schema 預先告訴 agent

在 agent system prompt 或 tool description 裡塞一份 catalog：

```yaml
services:
  checkout:
    log_format: json
    fields: [ts, level, msg, trace_id, user_id, status_code, latency_ms]
    common_errors:
      - "payment gateway timeout"
      - "inventory lock failed"
  order-api:
    log_format: logfmt
    fields: [ts, level, msg, request_id, ...]

labels:
  service: [checkout, order-api, inventory, ...]
  namespace: [prod, staging]
  level: [debug, info, warn, error]

trace_attributes:
  http.status_code, http.route, db.statement, ...
```

**這是最大的單一槓桿**——它讓 LLM「會寫對的 query」，不會用 `{app="checkout"}` 去查一個 label 叫 `service` 的系統。

#### Step 2：強迫 filter 下推到 LogQL / TraceQL / PromQL

LogQL pipeline 本身就能做掉 80% 的「filter / 摘要」需求：

```logql
# 抓 5xx error 並只回傳訊息 + trace_id
{service="checkout"}
  |= "error"
  | json
  | status_code >= 500
  | line_format "{{.msg}} {{.trace_id}}"
```

```logql
# 直接 aggregate，根本不需要拉原始 log
sum by (error_code) (
  count_over_time({service="checkout"} |= "error" | json [5m])
)
```

TraceQL 同樣強：

```traceql
{ resource.service.name="checkout" && status=error && duration > 500ms }
```

能在 datasource 端壓縮的就不要拉回 agent。**Loki 的 index 加速 + token 節省 + 延遲降低，三贏。**

#### Step 3：Python / code interpreter 當 escape hatch

只在 LogQL/TraceQL/PromQL 真的搞不定時才開放。典型場景：

- **跨訊號 join**：logs 抓到的 `trace_id` → Tempo 撈 span → 跟 Prometheus 的 deploy timestamp 對齊
- **Template mining**：用 [drain3](https://github.com/logpai/Drain3) / logmine 把 1000 行 log 壓成 20 個 cluster pattern
- **時序對齊**：error rate 跟 deploy event 畫在同一時間軸
- **統計檢定**：這個 anomaly 是不是顯著偏離 baseline（z-score / EWMA）

### 6.3 要避免的反模式

讓 LLM 看到原始 1000 行 log 後寫：

```python
# 反模式 ❌
for line in raw_logs:
    if "error" in line:
        ...
```

這就是浪費——LogQL 本來就能做。拉到 Python 等於：

1. Network / token 雙重浪費
2. 失去 Loki 的 index 加速
3. 還是會 lost in the middle，因為原始 log 已經先塞進 context 才走到 Python

### 6.4 Tool 分層設計

給 agent 的 tool description 要刻意分層，**用工具命名引導 LLM 走對的路徑**：

```python
tools = [
  # 預設工具：強迫用 LogQL 表達 filter
  query_logs(logql: str, time_range: TimeRange) -> LogResult

  # 統計類：內部跑 drain3，回傳 cluster 不回傳原始行
  summarize_log_patterns(logql: str, time_range: TimeRange) -> Cluster[]

  # 同上但給 traces
  query_traces(traceql: str, time_range: TimeRange) -> TraceResult
  summarize_trace_errors(traceql: str, time_range: TimeRange) -> ErrorPattern[]

  # 指標
  query_metrics(promql: str, time_range: TimeRange) -> MetricResult

  # k8s
  get_pod_status(namespace: str, selector: dict) -> PodInfo[]
  get_recent_events(namespace: str, since: Duration) -> Event[]

  # Escape hatch：只能存取已查回來的 dataframe
  run_analysis(python_code: str, datasets: list[str]) -> AnalysisResult
]
```

關鍵點：`run_analysis` 不能再打外部 API，只能操作已經查回來的資料。這把「LLM 寫 Python 亂查資料」的風險關掉。

* * *

## 7. 其他容易踩雷的點

### 7.1 認證鏈

```plaintext
Grafana session → Plugin → Agent service → MCP → backends
```

關鍵決定：是用 **service account token**（agent 一律高權限）還是 **user identity passthrough**（agent 只能看使用者本來看得到的）？

- 多租戶 / SaaS：必須 passthrough，不然會洩權
- 內部單一團隊使用：service account 簡單，但要 log audit
- 折衷：agent service 拿 user 的 Grafana token 去打 grafana-mcp，但 k8s 用獨立 read-only SA

### 7.2 Tool latency budget

一輪 RCA 可能跑 5-10 個 tool call，每個 PromQL/LogQL range query 可能 1-3 秒。**沒有 streaming UX，使用者會以為當機。**

對策：
- LangGraph 的 event stream 全部往前推到 plugin
- UI 上顯示「目前在執行：querying Loki for service=checkout errors」
- 中間步驟也要顯示假設樹，讓使用者看到 agent 在想什麼

### 7.3 Context 管理

- Tool 回傳結果一律過一層 size cap（譬如 50KB），超過就強迫 agent 改用 `summarize_*` 版本
- LangGraph state 裡可以存「完整結果」，但塞回 LLM context 的是 summary + reference id
- 多輪對話時舊輪的 tool result 要主動清掉，只留 final synthesis

### 7.4 安全性

- `run_analysis` 的 Python 一定要 sandbox（[restrictedpython](https://restrictedpython.readthedocs.io/) / pyodide / 獨立 container）
- LogQL/PromQL/TraceQL 字串不要拼接，直接當參數傳給 client library
- agent 寫出的 query 要先 lint / dry-run（譬如 PromQL 用 `promtool check`）再執行，避免 cardinality 爆炸

* * *

## 8. 開發順序建議

如果要分階段做：

1. **MVP**：app plugin chat UI + LangGraph 單一 ReAct loop + grafana-mcp，先打通端到端
2. **加結構**：把 graph 拆成 hypothesize / gather / correlate，加 schema catalog
3. **加 summarization tool**：drain3 / 統計類 tool，解 context 爆量
4. **加 k8s 跟其他 MCP**：擴充訊號來源
5. **加 evaluation**：把整套接上 [o11y-bench](https://o11ybench.ai/) 之類的 benchmark，量化品質而不是憑感覺調 prompt

* * *

## 9. 一句話總結

技術上完全做得到，**真正的工程難點不在「agent 會不會推理」，而在「怎麼把 observability 資料壓縮到 LLM 吃得下的尺寸、又不丟掉訊號」**。Schema-first + 查詢下推 + tool 分層，是這個問題的標準解法。
