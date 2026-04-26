# tests/ 目錄說明

`tests/` 包含 o11y-bench 的全套單元測試與整合測試，以 pytest 執行。測試覆蓋 agent 執行、評分系統、任務管理、報表產生等各核心模組。

---

## 檔案對照

```
tests/
  test_adapter.py                   # sync_tasks 的任務生成邏輯
  test_agent_runner.py              # agent loop 的步數限制與 retry 判斷
  test_agent_tool_schema.py         # MCP tool schema 在送給 LLM 前的寬鬆化處理
  test_checks.py                    # grading deterministic checks 全家族
  test_compare_report.py            # compare_report 的跨 job 資料載入
  test_config.py                    # provider_variants 模型清單
  test_full_suite.py                # resume 機制：repair / plan / archive
  test_grading_models.py            # Transcript.to_text() 的截斷與 context 保留
  test_grading_stack_integration.py # grading stack smoke test（需真實 stack）
  test_harbor.py                    # harbor.run() 訊號處理與 cleanup
  test_judge.py                     # judge prompt 建構與 fact 解析
  test_o11y_agent.py                # O11yBenchAgent 與 LangChainO11yBenchAgent
  test_report.py                    # report.aggregate() 聚合邏輯與 write_report
  test_resume.py                    # patch_job_paths_for_resume 路徑修補
  test_run_job.py                   # harbor.build_command / cli._cmd_job / run.execute_job
  test_run_report.py                # run_report.generate_report() 單 job HTML 產生
  test_scenario_clock.py            # scenario_clock 環境變數讀寫
  test_task_spec_ids.py             # task spec YAML 驗證與 id 一致性
```

---

## 測試分類與對應模組

### agents/

**`test_agent_runner.py`**

覆蓋 `agents/agent_runner.py` 的兩個 guard：

- `enforce_step_limit`：等於上限時通過，超過時 raise `RuntimeError`（訊息含上限數字）
- `is_retryable_upstream_error`：HTTP 429（rate limit）、503（gateway）、529（Anthropic overloaded）判 True；非 HTTP 錯誤訊息含 "overloaded_error" 也判 True；純訊息 "503 completed" 判 False

**`test_agent_tool_schema.py`**

覆蓋 `relax_mcp_tool_input_schema_for_llm`：

- object 型別的 property 要加上 `additionalProperties: true`，讓 LLM 不被嚴格 schema 卡住
- 非 object property（如 string）不應被修改
- 函式不能改動原始 schema（immutability）

**`test_o11y_agent.py`**

覆蓋 `agents/o11y_agent.py` 和 `agents/langchain_o11y_agent.py`：

- `select_remote_mcp_url`：跳過 localhost / 127.0.0.1 / o11y-stack，回傳第一個 job-specific URL
- `build_runner_command`：輸出以 `bash -lc` 開頭，且包含 viewer stdout 路徑的 tee 指令
- `O11yBenchAgent.run()`：exit code ≠ 0 時 raise `NonZeroAgentExitCodeError`
- scenario clock 環境變數從 host 傳到容器 exec env
- `LangChainO11yBenchAgent.setup()`：上傳 `langchain_agent_runner.py`、`system_prompt.txt`、`task_prompt.txt`

---

### grading/

**`test_checks.py`**

覆蓋 `grading/checks.py` 和 `grading/env_context.py`，測試各種 check mode：

| 測試 | 驗什麼 |
|---|---|
| `load_verifier_context_defaults_to_localhost` | 無環境變數時四個 URL 預設 localhost |
| `fetch_loki_query_result_uses_range_query_with_nanosecond_eval_time` | Loki range query URL 含正確 ns 時間戳 |
| `fetch_tempo_attribute_values_normalizes_v2_response_objects` | Tempo v2 tag value response 正規化成 string list |
| `run_checks_dispatches_trace_grounding` | `tool_trace_id` grounding check 能找到 trace ID |
| `test_state_datasource_detail_requires_configured_detail` | datasource_detail check 驗 name / type / URL / access |
| `test_state_tempo_trace_service_inventory_uses_attribute_values` | tempo_trace_service_inventory check 驗 service 數量與清單 |
| `test_state_dashboard_state_validates_saved_structure` | dashboard_state check 驗 panels / variables |
| `test_state_dashboard_execute_case_supports_all_binding` | dashboard execute_case 用 `__all__` binding 展開所有 label values |

**`test_grading_models.py`**

覆蓋 `grading/models.py` 的 `Transcript.to_text()`：

- 壓縮後長度在 `max_chars` 以內
- 第一則 user message（題目）必須保留
- tool call 名稱必須出現

**`test_grading_stack_integration.py`**

Smoke test，執行 `scripts/grading_stack_smoke.py`。預設 skip，需設 `O11Y_GRADING_SMOKE=1` 且本機有 stack 才跑。

**`test_judge.py`**

覆蓋 `grading/judge.py` 和 `grading/facts.py`：

- `build_evaluation_prompt`：不含模板佔位字元，含 ground truth 文字
- `build_judge_criteria`：item fact 按 value 由大到小排序，scalar fact 用近似格式（含 ±5% 範圍）
- `resolve_fact`：同 spec 的 query 在同一次 grading 只打一次 API（cache）
- dashboard fact：prompt 含 panel 數量、panel 名稱、variables
- datasource_detail fact：name 和 type 都指定時優先用 name 比對

---

### o11y_bench/

**`test_config.py`**

驗 `config.provider_variants()` 對 anthropic / openai / google 各有預期的低 reasoning 模型集合。

**`test_full_suite.py`**（實際是 resume 測試）

覆蓋 `o11y_bench/resume.py` 的 `repair_job_dir_for_resume`：

| 測試 | 驗什麼 |
|---|---|
| `test_repair_patches_concurrency_and_paths` | 修正 n_concurrent、jobs_dir、trial config 路徑 |
| `test_plan_marks_environment_delete_drift_for_repair` | `environment.delete: true` 被標記為需修正 |
| `test_repair_normalizes_non_dict_environment_config` | trial config 裡 environment 為 null 時補齊 |
| `test_repair_archives_incomplete_and_retryable_trials` | 無 result.json 或 infra failure 的 trial 被封存 |
| `test_repair_archives_nonzero_agent_exit_trials` | agent exit code ≠ 0 的 trial 被封存 |
| `test_repair_archives_stale_trials_when_task_changed` | checksum 過期或 task 已移除的 trial 被封存 |

**`test_harbor.py`**

覆蓋 `o11y_bench/harbor.py`：

- `run(forward_signals=False)`：不安裝 signal handler，但仍呼叫 cleanup
- 非主執行緒呼叫 `run()` 時 raise `RuntimeError`（含 `forward_signals=False` 提示）
- 主執行緒正常安裝 SIGINT / SIGTERM handler
- `run_cleanup()`：呼叫 `bash preflight_script --cleanup-only`

**`test_resume.py`**

覆蓋 `o11y_bench/resume.py` 的 `patch_job_paths_for_resume`：

- 把 trial config 的 relative / stale 路徑修正為絕對路徑
- dry_run 模式只回報不寫入
- 用 `result.json` 裡的完整 task_name 對應被截斷的目錄名稱
- 若 job config datasets 是相對路徑，task path 也保持相對

**`test_run_job.py`**

覆蓋 CLI、harbor command 建構、`run.execute_job`、`run.finalize_job_dir`、`run.regrade_job_dir`：

- `harbor.build_command`：產生合法的 harbor invocation，含 `--yes`、model、job name
- `harbor.build_command` 多 task filter 對應多個 `--include-task-name`
- `execute_job`：已存在 config.json 時使用 `--config` 模式 resume
- `finalize_job_dir`：把所有絕對路徑改成相對路徑（sanitize）；長 task name 截斷問題用 result.json 修正
- `_cmd_job`：dry_run=True 時跳過 preflight；`--agent` 和 `--agent-import-path` 不能並用
- `regrade_job_dir`：重跑 verifier 並更新 reward.txt / grading_details.json / result.json
- `regrade_job_dir` 多個同 task 的 trial 只起動一次 live stack

**`test_scenario_clock.py`**

覆蓋 `o11y_bench/scenario_clock.py`：

- `resolve_scenario_time`：有 env 時回傳 env，無 env 時回傳合法 ISO 字串
- `bound_scenario_time`：context manager 進入時設定，離開時還原（或清除）

---

### reporting/

**`test_compare_report.py`**

覆蓋 `reporting/compare_report.py` 的 `load_job`：

- 讀 `reasoning_effort` 拼出 model display 名稱（加 `(high)` 後綴）
- infra failure trial（`exception_message` 含 "Docker compose command failed"）不計入 total_tasks

**`test_report.py`**

覆蓋 `reporting/report.py` 和 `reporting/report_data.py`：

| 測試 | 驗什麼 |
|---|---|
| `test_aggregate_keeps_reasoning_variants_separate` | `off` 和 `high` reasoning effort 分列不同 model label |
| `test_aggregate_uses_pass_hat_k_as_primary_metric` | 排序依 pass_hat_rate 由高到低 |
| `test_load_trials_follows_symlinked_job_dirs` | symlink job dir 也能載入 trials |
| `test_latest_suite_dir_prefers_most_recent_suite` | 按 mtime 選最新 suite dir |
| `test_aggregate_excludes_pre_agent_infra_failures` | Docker infra failure 不計入 n_tasks |
| `test_aggregate_excludes_nonzero_agent_exit_trials` | agent exit code ≠ 0 不計入 valid trials |
| `test_aggregate_counts_step_limit_nonzero_exit_as_valid_failure` | step limit 超過的 trial 計為合法失敗 |
| `test_aggregate_counts_retryable_agent_exceptions_as_valid` | AgentTimeoutError trial 計為 valid |
| `test_aggregate_treats_timeout_full_score_as_failure_for_pass_metrics` | timeout 即使 reward=1 也算 pass 失敗 |
| `test_agent_result_metrics_uses_snapshot_fallback_pricing` | cost_usd=0 時用 snapshot 定價補算 |
| `test_write_report_warns_on_mixed_task_checksums` | 不同 model 的同 task checksum 不一致時印警告 |
| `test_aggregate_uses_job_config_n_attempts_for_expected_trials` | expected_trials 用 job config 的 n_attempts 計算 |

**`test_run_report.py`**

覆蓋 `reporting/run_report.py`：

- `rubric_passed`：所有 scored item 全過才算 pass（不以 score ≥ threshold 判斷）
- `generate_report`：從 trial config 讀 reasoning_effort 拼 model label（如 `GPT 5.4 Nano (high)`）

---

### scripts/

**`test_adapter.py`**

覆蓋 `scripts/sync_tasks.py` 的 `generate_task`：

- 由 `problem.yaml` 產生完整的 Harbor task 目錄結構（instruction.md、task.toml、Dockerfile、verifier）
- `task.toml` 含正確的 category、固定 timeout（600s）、MCP URL

**`test_task_spec_ids.py`**

驗 `tasks-spec/` 目錄下所有 YAML 的合法性：

- task id 排序、唯一、與 CLI `--list-ids` 輸出一致
- 每個 YAML 都能通過 `Problem.model_validate()`
- `CheckItem` 拒絕未知 type（錯誤訊息提示 grounding / state）
- `RubricItem` fact 拒絕非法 PromQL（`sum(` 不完整）
- `RubricItem` fact 拒絕未知 kind

---

## 執行方式

```bash
# 全部測試
uv run pytest

# 單一檔案
uv run pytest tests/test_checks.py

# 啟用 smoke test（需本機 stack）
O11Y_GRADING_SMOKE=1 uv run pytest tests/test_grading_stack_integration.py
```

---

## 測試設計原則

- **不碰 network**：所有需要 HTTP 的測試都用 `monkeypatch` 或 `unittest.mock.patch` 替換，確保 CI 不依賴外部服務。
- **tmp_path 隔離**：涉及檔案系統的測試統一用 pytest fixture `tmp_path`，不污染 repo。
- **grading_stack_integration 例外**：唯一真正打 stack 的測試，預設 skip，需明確 opt-in。
