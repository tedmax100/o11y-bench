# o11y-bench 是什麼

o11y-bench 是一個開放的 benchmark，專門測試 LLM agent 能不能像 SRE 工程師一樣，自主操作真實的 observability 工具，解決真實的監控與事故分析問題。

由 Grafana Labs 開發，基於 [Harbor](https://harborframework.com) 框架運作。

每次跑會起一個真實的 Docker stack（Prometheus + Loki + Tempo + Grafana），裡面有合成的微服務監控資料（user-service、order-service、payment-service 等），agent 透過 MCP tools 自主操作，跑完再評分。資料時間釘死在 `scenario_time.txt`，確保每次跑同一題拿到相同的資料，結果可比較。

---

## 要跑什麼指令

### 前置作業

```bash
export ANTHROPIC_API_KEY=...    # grading 用（Claude 當 judge）
export OPENAI_API_KEY=...       # 如果要跑 GPT 模型
export GOOGLE_API_KEY=...       # 如果要跑 Gemini 模型

mise run setup:sync             # 從 tasks-spec/ 生成 tasks/（第一次或改 spec 後要跑）
```

### 跑單一題目（最快驗證）

```bash
mise run bench:job -- --model google/gemini-2.5-pro --task-name query-cpu-metrics --n-concurrent 1
```

### 跑某個模型的所有 63 題

```bash
mise run bench:job -- --model anthropic/claude-sonnet-4-6 --reasoning-effort off
```

### 跑所有模型全部題目（完整 suite）

```bash
mise run bench:suite
```

### 沒有 Anthropic API key 也能跑

```bash
export SKIP_LLM_GRADING=1
export ANTHROPIC_API_KEY=dummy   # Harbor 前置檢查需要這個變數存在，填假的即可
mise run bench:job -- --model openai/gpt-4o --task-name query-cpu-metrics
```

注意：`investigation` 類的 task 全靠 LLM rubric，這種模式下分數會是 0，但 agent 行為本身還是會跑完。

### 其他常用指令

```bash
mise run test             # 跑 pytest 測試套件
mise run lint             # Ruff lint
mise run typecheck        # mypy
mise run report           # 手動重建 leaderboard HTML
mise run setup:sync       # 從 tasks-spec/ 重新生成 tasks/
mise run setup:preflight  # 預先 build Docker image、清理舊容器

# 只重跑評分，不重跑 agent（例如改了 rubric 之後）
uv run python -m o11y_bench regrade --job-dir jobs/<job-name>

# 重建單一 job report
uv run python -m reporting.run_report --job-dir jobs/<job-name>

# 兩個 job 並排比較
uv run python -m reporting.compare_report \
  --job-dir jobs/<suite-id>/<job-a> \
  --job-dir jobs/<suite-id>/<job-b>
```

---

## 測試什麼（63 道題）

分六種題型，定義在 `tasks-spec/`，生成後放在 `tasks/`（不要手動改 `tasks/`）：

| 題型 | 題數 | 在測什麼 |
|---|---|---|
| **PromQL** | 16 | 能否寫正確的 Prometheus 查詢（rate、topk、subquery、offset）並解讀數字 |
| **LogQL** | 10 | 能否寫 Loki log pipeline（JSON 解析、unwrap、p95 latency 計算）|
| **TraceQL** | 13 | 能否查 Tempo 找 trace、說明 error 傳播路徑、引用真實 trace ID |
| **Grafana API** | 6 | 能否直接操作 Grafana REST API（datasource、dashboard 查詢）|
| **Dashboarding** | 7 | 能否建出在 Grafana 上實際可用的 dashboard（含 variable、annotation）|
| **Investigation** | 11 | 能否跨 metrics + logs + traces 做 incident 根因分析 |

題目刻意用自然語言寫（像 on-call 問你），不給語法提示。例如：

> 「六小時前 checkout 服務開始變慢，找出哪個 service 的 CPU 使用率最高」

---

## 評分機制

每個 task 的分數由兩層組成：

### 第一層：Deterministic checks（程式直接驗，不需要 LLM）

速度快、結果精確，直接執行：

- **grounding**：trace ID 是否真的出現在 Tempo 工具結果裡（防 hallucination）
- **state**：Grafana 上真的有建出那個 dashboard？panels 設定是否正確？datasource type / URL 是否符合規格？

### 第二層：LLM rubric（Claude 當 judge）

需要 `ANTHROPIC_API_KEY`，每題有數條 criterion，各有 weight：

- 有數字的 criterion 會附上 ground truth（benchmark 自己跑 PromQL 拿答案），讓 judge 比對 agent 說的是否正確
- Judge 看完整 transcript（含所有 tool call 紀錄）逐條評 YES/NO
- YES = 1.0，NO = 0.0，加權平均後得出分數

最終分數 = 兩層的加權平均，0.0 到 1.0。

---

## 結果在哪裡

```
jobs/
  <job-name>/
    run_report.html                    # 主要看這個（單一模型報告）
    result.json                        # 所有 trial 的分數摘要
    <task-name>__<trial-id>/
      agent/
        trajectory.json                # 完整對話紀錄（含所有 tool calls）
        command-0/stdout.txt           # tool call 摘要（每步名稱 + token 數）
      verifier/
        reward.txt                     # 這題的分數（0.0–1.0）
        grading_details.json           # 各 criterion 分數 + judge 解釋

  <suite-id>/
    comparison.html                    # 跨模型 leaderboard（suite 跑完後生成）
```

---

## 怎麼解讀數據

### run_report.html（單一模型報告）

看這三個核心指標：

| 指標 | 意思 |
|---|---|
| **pass@k（pass_rate）** | 有 ≥1 次 trial 通過的 task 比例，反映模型「最好狀況」的能力上限 |
| **pass^k（pass_hat_rate）** | 所有 k 次 trial 都通過的 task 比例，反映模型的**穩定性** |
| **mean_score** | 所有 task 的平均分，含部分分（0–1） |

預設每題跑 3 次（k=3），解讀方式：

- **pass@3 高、pass^3 低**：模型有能力，但不穩定，需要多試幾次才成功
- **pass@3 ≈ pass^3**：模型很穩定，基本每次都能做對（或做錯）
- **mean_score 高但 pass_rate 低**：很多題都拿到部分分，但沒有一題完全答對
- **pass_rate 高但 mean_score 只稍高**：模型通過的題都是簡單題，難題全掛

### comparison.html（跨模型 leaderboard）

- 按 pass^k 由高到低排序（穩定性優先）
- 按題型分組可以看出每個模型的弱項（例如某模型 PromQL 很好但 dashboarding 全掛）
- Cost 和 token 用量可以比較各模型的「CP 值」

### grading_details.json（細看某一題）

```json
{
  "score": 0.75,
  "The final response states 5xx share accurately.": 1.0,
  "The final response identifies the service with highest error rate.": 0.0,
  "explanation:The final response identifies...": "Agent said order-service but canonical query shows payment-service"
}
```

`explanation:` 欄位是 judge 說明為什麼給這個分，可以直接看出 agent 哪裡答錯了。

### trajectory.json（細看 agent 行為）

完整的 tool call 紀錄，可以看：

- Agent 用了哪些工具、查了哪些 query
- 工具回傳了什麼資料
- Agent 的推理過程是否基於工具結果，還是憑空推測

---

## 測試套件在驗什麼（`mise run test`）

`tests/` 裡的 pytest **不是跑 benchmark**，而是驗 benchmark 本身的邏輯正不正確，例如：

- 評分邏輯（timeout trial 怎麼算、infra failure 要不要計入分母）
- resume 機制（搬機器後路徑修補、stale trial 封存）
- trace ID grounding check 邏輯
- task spec YAML 是否全部合法

這些測試不需要 Docker、不需要 API key，CI 直接跑。

---

## 開發相關

```bash
# task spec 改了一定要跑，tasks/ 是 generated output 不要手動編輯
mise run setup:sync

# 只列出所有 task ID，不實際生成
uv run python -m scripts.sync_tasks --list-ids
```

task spec 修改注意事項：
1. `statement` 用 user-voiced 語氣，不要洩露評分細節（不要寫「用 PromQL rate function」）
2. 數字精確性用 `fact`，不要把答案寫死在 criterion 文字裡
3. 引用具體實體（trace ID）用 grounding check，不要靠 LLM 判斷
4. dashboard 狀態用 state check，不要靠 LLM 判斷 dashboard 有沒有建起來
5. Prometheus fact query 在 sync 時會被 `promql_parser` 驗證，語法錯誤會馬上報錯
