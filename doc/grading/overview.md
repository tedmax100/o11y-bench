# grading/ 目錄說明

評分系統負責把 agent 的完整對話紀錄（transcript）轉換成 0–1 的分數。設計上刻意保持小巧：deterministic checks 處理「能程式驗證」的事情，LLM rubric 處理「需要語意理解」的事情。

---

## 檔案對照

```
grading/
  verifier.py            # Harbor verifier 入口，整個評分流程的 main()
  verifier_launcher.py   # uv inline script 包裝，讓 Harbor 能用 uv run 啟動 verifier
  models.py              # 所有資料型別定義（task spec + transcript 結構）
  checks.py              # deterministic check 執行邏輯
  facts.py               # canonical fact 取得、快取、渲染
  judge.py               # LLM judge：prompt 建構、API 呼叫、YES/NO 解析
  scoring.py             # 權重正規化、加權分數計算
  transcript_parser.py   # 解析 trajectory.json → Transcript 物件
  env_context.py         # 對 Grafana / Prometheus / Loki / Tempo 發 HTTP 請求
  dashboard_state.py     # dashboard 狀態 check 邏輯
  dashboard_snapshot.py  # dashboard JSON 載入與正規化
  dashboard_queries.py   # dashboard saved query 語意驗證
  helpers.py             # transcript 與 trace ID 輔助函式
  grader_prompt.txt      # LLM judge 的 prompt 模板
```

---

## 評分流程

```
problem.yaml
    ↓ models.py 解析
Problem（checks + rubric + facts）

trajectory.json
    ↓ transcript_parser.py 解析
Transcript（messages: user / assistant / tool）

                ↓
    ┌───────────────────────────────┐
    │  1. Deterministic checks      │  checks.py
    │     (grounding / state)       │
    └───────────────────────────────┘
                ↓
    ┌───────────────────────────────┐
    │  2. Resolve facts             │  facts.py
    │     (query ground truth from  │
    │      Prometheus / Loki / Tempo│
    │      / Grafana)               │
    └───────────────────────────────┘
                ↓
    ┌───────────────────────────────┐
    │  3. LLM rubric（judge call）  │  judge.py
    │     每條 criterion → YES/NO   │
    └───────────────────────────────┘
                ↓
    ┌───────────────────────────────┐
    │  4. 加權合算                  │  scoring.py
    └───────────────────────────────┘
                ↓
    score (0.0–1.0)  +  grading_details.json
```

---

## 兩種評分維度

### Deterministic checks

程式直接驗，不需要 LLM，速度快、結果精確。在 task spec 裡的 `checks:` 欄位定義。

支援兩大家族：

| 家族 | 用途 |
|---|---|
| `grounding` | 驗 agent 答案裡引用的具體實體是否真的來自工具結果（例如 trace ID 不能憑空捏造）|
| `state` | 驗 Grafana stack 的實際狀態（dashboard 是否存在、panels 設定是否正確、datasource 是否齊全）|

五種具體 check mode：

| mode | 驗什麼 |
|---|---|
| `tool_trace_id` | 答案引用的 trace ID 必須出現在 Tempo tool result 裡 |
| `dashboard_state` | Grafana 上存在指定 dashboard，且 panels / variables / annotations 符合規格 |
| `datasource_inventory` | Grafana 有指定的 datasource types 或 names |
| `datasource_detail` | 指定 datasource 有正確的 type / URL / access mode |
| `tempo_trace_service_inventory` | Tempo 裡有指定 service 的 trace 資料 |

### LLM rubric

由另一個 Claude 當 judge，閱讀完整 transcript 後逐條評 YES/NO。在 task spec 的 `rubric:` 欄位定義。

每條 criterion 可以附一個 `fact`，讓 judge 有 ground truth 可以比對：

```yaml
rubric:
- criterion: The final response states 5xx share accurately.
  weight: 65
  fact:
    kind: query
    backend: prometheus
    query: sum(rate(http_requests_total{status=~"5.."}[1h])) / sum(rate(http_requests_total[1h]))
```

benchmark 自己跑那個 query 拿到答案，把結果以 `Source of truth: ...` 格式插入 judge prompt，讓 judge 比對 agent 的答案是否符合。

---

## models.py — 資料型別

定義所有的 pydantic 模型，分兩大類：

**Task spec 模型**（從 `problem.yaml` 讀入）
- `Problem`：整個 task 定義（id, category, statement, checks, rubric）
- `CheckItem`：一條 deterministic check（name, weight, params）
- `RubricItem`：一條 rubric criterion（criterion text, weight, 可選的 fact）
- `CheckParams`：五種 check 的參數型別（用 discriminated union 做 dispatch）
- `FactSpec`：fact 的兩種類型（`query` 或 `resource`）

**Transcript 模型**（從 `trajectory.json` 讀入）
- `Transcript`：整個對話，包含有序的 `Message` list
- `Message`：一則訊息（role: system/user/assistant/tool，content, tool_calls, tool_results）
- `ToolCall`：agent 呼叫的工具（id, name, arguments）
- `ToolResult`：工具回傳的結果（tool_call_id, content）

`Transcript.to_text()` 會把對話壓縮成文字給 judge 讀，如果太長會自動按預設策略縮短（先縮 thinking/tool result，最後才截頭截尾）。

---

## facts.py — Ground truth 取得

`resolve_fact()` 根據 fact spec 去實際的 backend 查詢，回傳 `FactResult`（summary 文字 + debug dict）。結果會在同一次 grading 內 cache，避免重複查詢。

| fact.kind | fact.resource / backend | 怎麼取 |
|---|---|---|
| `query` | `prometheus` | 打 `/api/v1/query`，解析 scalar 或 vector |
| `query` | `loki` | 打 `/loki/api/v1/query` |
| `query` | `tempo` | 打 `/api/search`，取 trace list |
| `resource` | `dashboard` | 從 Grafana REST API 抓 dashboard JSON |
| `resource` | `datasource_inventory` | 從 Grafana 抓所有 datasource |
| `resource` | `datasource_detail` | 從 Grafana 抓特定 datasource |

結果渲染成自然語言後插入 judge prompt 作為 `Source of truth`。Criterion 裡出現 roughly / about / approximately 等字樣時，scalar fact 會改用寬鬆格式（給出 ±5% 範圍）。

---

## judge.py — LLM judge

用 Anthropic Claude 評 rubric，需要 `ANTHROPIC_API_KEY`（或 `ANTHROPIC_AUTH_TOKEN`）。

**Prompt 結構**

```
<transcript>
  [System]: ...
  [User]: 題目
  [Assistant Tool Call]: query_prometheus(...)
  [Tool Result]: ...
  [Assistant]: 最終回答
</transcript>

Based on the transcript above, evaluate whether each criterion is satisfied.

<criteria>
  <criterion id="0">The final response states 5xx share accurately.
  Source of truth: The canonical query returned 0.034.</criterion>
  ...
</criteria>
```

**解析回應**

Judge 回傳：
```xml
<evaluation id="0">
<answer>YES</answer>
<explanation>Response states 3.4% which matches the canonical value.</explanation>
</evaluation>
```

`parse_evaluation_response()` 把每個 YES/NO 解析成 1.0 / 0.0。

**Context budget**

Judge prompt 有三段 transcript 長度嘗試（180K / 120K / 80K chars），遇到 `prompt is too long` 錯誤時自動縮短重試。

---

## transcript_parser.py — Transcript 解析

`parse_transcript()` 自動偵測格式：

| 格式 | 觸發條件 | 說明 |
|---|---|---|
| Claude Code JSONL | 找到 `claude-code.txt` 或 `stream.jsonl` | Claude Code 的串流格式 |
| ATIF trajectory.json | 找到 `*.json` 含 `steps` 欄位 | o11y-bench 預設格式 |
| ATIF JSONL | 找到 `*.jsonl` | 每行一個 step |

---

## scoring.py — 分數計算

兩個純函式：

- `normalize_weights(raw_weights)` — 把各 criterion 的 weight 正規化成加總為 1.0
- `calculate_score(subscores, weights)` — 加權平均，結果 clamp 到 [0.0, 1.0]

Check 和 rubric 各自的 weight 都一起丟進來正規化，所以 check 和 rubric 的相對比重由各自的 weight 值決定。

---

## env_context.py — Stack HTTP 存取

`VerifierContext` 持有四個 backend 的 URL（從環境變數讀取，預設指向 localhost）。

提供統一的 HTTP helper，`facts.py` 和 `checks.py` 都透過這層存取 stack，不直接打 URL。

---

## SKIP_LLM_GRADING

設 `SKIP_LLM_GRADING=1` 環境變數可跳過 LLM rubric，只跑 deterministic checks。在沒有 `ANTHROPIC_API_KEY` 的情況下使用。有 rubric-only task 的分數會是 0，但不會 crash。
