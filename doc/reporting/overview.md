# reporting/ 目錄說明

負責把 job 執行結果轉換成人可讀的 HTML 報告，以及提供跨 job 的資料載入和聚合工具。

---

## 檔案對照

```
reporting/
  report_data.py       # 核心資料載入、trial 狀態分類、格式化工具函式
  summary.py           # TrialRow / TaskSummary 型別定義與聚合計算
  categories.py        # task 類別的顯示名稱對照表
  model_costs.py       # 少數模型的 token 費率（litellm 未支援時的 fallback）
  report_paths.py      # 路徑輔助函式（suite 目錄、最新 job 等）
  run_report.py        # 生成單一 job 的 HTML 報告（run_report.html）
  report.py            # 生成跨模型比較的 leaderboard 報告（comparison.html）
  compare_report.py    # 生成兩個 job 並排比較的 HTML 報告
  report_template.html # run_report.py 使用的 HTML 模板
  compare_template.html# compare_report.py 使用的 HTML 模板
```

---

## 報告種類

| 指令 | 輸出檔案 | 內容 |
|---|---|---|
| `mise run bench:job` 結束後自動生成 | `jobs/<job-name>/run_report.html` | 單一 job：每個 task 的分數、transcript、grading details |
| `mise run bench:suite` 結束後自動生成 | `jobs/<suite-id>/comparison.html` | 全 suite leaderboard：所有模型 × task 的分數矩陣 |
| `mise run report` | `jobs/<suite-id>/comparison.html` | 手動重建 leaderboard 報告 |
| `uv run python -m reporting.compare_report` | 自訂路徑 | 兩個 job 的並排比較 |

---

## report_data.py — 核心資料層

所有報告腳本共用的資料載入與處理函式。

### Trial 載入

**`load_trials(jobs_dir)`** — 遞迴掃描 `jobs_dir` 下所有 `result.json`，回傳 trial dict list。跳過隱藏目錄（`.resume-pruned`）和 job 層級的 `result.json`（深度 < 2）。每個 trial dict 加上 `__result_path` 欄位供後續定位。

### Trial 狀態分類

**`classify_trial_artifact(trial_dir, trial)`** — 判斷 trial 的完整性：

| 狀態 | 條件 |
|---|---|
| `complete` | 正常完成，無中斷跡象 |
| `retryable` | 被 kill 中斷（CancelledError）或 reward file 不存在 |
| `stale` | task 的 `problem.yaml` checksum 與記錄不符 |
| `corrupt` | `result.json` 不存在或格式錯誤 |

`is_invalid_infra_trial()` 進一步識別「agent crash 但不是 step limit」的情況（`NonZeroAgentExitCodeError`），這類 trial 在報告中標為 invalid 而不影響分數統計。

### 分數與 Pass 判斷

- **`reward_counts_as_pass(trial)`** — reward == 1.0 且不是 timeout
- **`grading_counts_as_pass(trial, grading)`** — 所有 rubric subscores ≥ 1.0 且不是 timeout
- **`rubric_passed(grading)`** — 從 `grading_details.json` 判斷是否全過

### 格式化工具

| 函式 | 用途 |
|---|---|
| `pretty_variant(model, effort)` | `"claude-sonnet-4-6"` + `"high"` → `"Sonnet 4.6 (high)"` |
| `format_duration(seconds)` | `93` → `"1m33s"` |
| `format_cost(cost_usd)` | `0.0015` → `"$1.50m"` |
| `format_compact_count(value)` | `59589` → `"59.6k"` |
| `score_color_class(score)` | 回傳 Tailwind CSS 顏色 class（綠/黃/紅） |
| `agent_result_metrics(result)` | 從 result dict 取出 cost、input tokens、cache tokens、output tokens |

---

## summary.py — 聚合計算

### TypedDict 定義

**`TrialRow`** — 單一 trial 的平坦化資料：
```
task_name, score, cost_usd, agent_secs,
n_input_tokens, n_output_tokens, n_cache_tokens,
tool_calls, invalid_infra, counts_as_pass
```

**`TaskSummary`** — 單一 task 在多次 trial 的聚合：
```
scores (list), passed (any trial passed),
consistent (all trials passed), mean_score, best_score, cost_usd
```

**`TrialsSummary`** — 整個 job 的統計：
```
per_task, n_tasks, n_valid_trials, n_passed, n_consistent,
pass_rate, pass_hat_rate, mean_score,
total_cost_usd, total_agent_secs,
total_tokens_in/out/cache, total_tool_calls,
shots_per_task, steps_per_trial
```

### 兩種 pass 指標

- **`pass_rate`（pass@k）** — 有至少一次 trial 通過的 task 比例
- **`pass_hat_rate`（pass^k）** — 所有 trial 都通過（consistent）的 task 比例

---

## report_paths.py — 路徑工具

| 函式 | 用途 |
|---|---|
| `normalize_repo_path(root, path)` | 相對路徑轉絕對，不存在也不 raise |
| `is_suite_dir(path)` | 目錄名稱以 `full-suite-` 開頭 |
| `latest_suite_dir(jobs_root)` | 找最新的 suite 目錄（按最後修改時間）|
| `latest_job_dir(jobs_dir, job_name)` | 找指定或最新的 job 目錄 |
| `run_report_output_path(job_dir)` | `jobs/<job-name>/run_report.html` |
| `suite_report_output_path(jobs_dir)` | `jobs/<suite-id>/comparison.html` |

---

## categories.py

六種 task 類別的顯示名稱：

| 內部名稱 | 顯示名稱 |
|---|---|
| `prometheus_query` | PromQL |
| `loki_query` | LogQL |
| `tempo_query` | TraceQL |
| `dashboarding` | Dashboarding |
| `grafana_api` | Grafana API |
| `investigation` | Investigation |

---

## model_costs.py

litellm 尚未內建的模型費率 fallback 表（目前只有 `gpt-5.4-mini` 和 `gpt-5.4-nano`）。`agent_runner.py` 有一份相同的表，兩者要保持同步（有 TODO 標記）。

費率格式：`(input_per_1M, cached_input_per_1M, output_per_1M)` 美元。

---

## 三種報告腳本

### run_report.py — 單一 job 報告

用 `report_template.html` 生成自包含 HTML（不依賴外部 CDN）。

主要內容：
- 整體分數摘要（mean score、pass rate、cost、token 用量）
- 按 task 類別分組的分數表
- 每個 task 的詳細頁：所有 trial 的 transcript、grading details
- Transcript 內容經 gzip + base64 壓縮後嵌入 HTML，點擊才展開

### report.py — Suite leaderboard

讀取 suite 目錄下所有 job，生成跨模型比較矩陣。

主要內容：
- 所有模型的 pass@k、pass^k、mean score 排名表
- 按 task 類別分組的分數熱力圖
- 各模型的 cost 和 token 用量比較
- 點入每個 model 可看個別 `run_report.html`

### compare_report.py — 兩個 job 並排比較

逐 task 對比兩個 job 的分數，用顏色標出哪個模型在哪個 task 表現更好。

---

## 手動重建報告

```bash
# 重建單一 job report
uv run python -m reporting.run_report --job-dir jobs/<job-name>

# 重建 suite leaderboard
mise run report -- --jobs-dir jobs/<suite-id>

# 兩個 job 並排比較
uv run python -m reporting.compare_report \
  --job-dir jobs/<suite-id>/<job-a> \
  --job-dir jobs/<suite-id>/<job-b>
```
