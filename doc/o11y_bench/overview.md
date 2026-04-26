# o11y_bench/ 目錄說明

benchmark 的主程式套件。負責 CLI 入口、job/suite 的規劃與執行、Harbor 呼叫、resume/repair 邏輯、regrading。

---

## 檔案對照

```
o11y_bench/
  __main__.py        # python -m o11y_bench 的入口，直接呼叫 cli.main()
  cli.py             # argparse subcommands：job / suite / run / finalize / regrade
  config.py          # 常數、JobSpec / SuiteOpts dataclass、suite 矩陣、job name 生成
  run.py             # execute_job / execute_suite / execute_regrade 的核心邏輯
  harbor.py          # harbor CLI 指令建構、subprocess 執行、signal 轉發
  resume.py          # job 目錄的 checksum、staleness 偵測、config patch、trial 封存
  scenario_clock.py  # O11Y_SCENARIO_TIME_ISO 的讀取與設定
  regrade_stack.py   # regrade 時按需啟動臨時 o11y-stack Docker container
```

---

## CLI subcommands

透過 `uv run python -m o11y_bench <subcommand>` 或 `mise run bench:*` 呼叫。

| subcommand | 用途 |
|---|---|
| `job` | 跑或 resume 單一 model × reasoning_effort 的 benchmark |
| `suite` | 按 `STANDARD_SUITE` 矩陣跑所有 provider × model × effort |
| `run` | 直接 passthrough 給 `harbor run`（低階，一般不直接用）|
| `finalize` | 只跑 checksum 印章 + 生成 HTML report，不跑 agent |
| `regrade` | 對已有 transcript 重跑評分，不重跑 agent |

---

## config.py — 常數與設定

**`STANDARD_SUITE`** — 完整 suite 的模型矩陣，tuple 格式：`(provider, model, reasoning_effort)`。目前涵蓋 Anthropic、OpenAI、Google，共數十個組合。

**`JobSpec`** — 一次 job 執行所需的所有參數：

```python
@dataclass(frozen=True)
class JobSpec:
    jobs_dir: Path          # jobs/ 根目錄
    job_name: str           # 唯一識別名稱，e.g. "anthropic-claude-sonnet-4-6-off-k3"
    tasks_dir: Path         # 已生成的 tasks/ 目錄
    model: str              # "provider/model"，e.g. "anthropic/claude-sonnet-4-6"
    reasoning_effort: str   # "off" / "low" / "high"
    n_attempts: int         # 每個 task 跑幾次
    n_concurrent: int       # Harbor 的並行 trial 數
    agent_import_path: str  # 自訂 agent 的 import path
    agent: str | None       # Harbor built-in agent 名稱（與 import_path 二選一）
    task_names: tuple[str, ...]  # 指定只跑哪些 task（空 = 全部）
```

**`make_job_name()`** — 從 provider、model、effort、n_attempts 生成標準化的 job 目錄名稱，特殊字元全部轉成 `-`。例：`google-gemini-2-5-pro-off-k3`。

---

## run.py — 核心執行邏輯

### `execute_job(spec)`

單一 job 的完整生命週期：

```
1. 計算 task checksums
2. 若 job_dir 不存在 → 全新跑（run_harbor → finalize）
3. 若 job_dir 存在 → 計算 resume plan
   a. 找出缺少、中斷、stale 的 trials
   b. 修補 config.json 路徑（jobs_dir / tasks_dir 因移機器而變動）
   c. 把問題 trials 封存到 .resume-pruned/
   d. 重跑 Harbor（只補缺少的 trials）
4. finalize_job_dir → 印 checksum、生 HTML report
```

### `execute_suite(opts)`

用 `ThreadPoolExecutor` 跑，每個 provider 一條 thread，每條 thread 內循序跑該 provider 的所有 model × effort 組合。某個 provider 的 Harbor 失敗時，該 provider 的其餘組合停止跑。

### `execute_regrade(target_dir)`

對指定目錄（job 或 suite）下的所有 trial，重跑評分（不重跑 agent）：

1. 解析 `trajectory.json`
2. 若 task 的 checks/facts 需要存取 live stack → 用 `running_regrade_stack()` 起一個暫時容器
3. 呼叫 `grade(problem, transcript, model)` 重新算分
4. 覆寫 `verifier/reward.txt` 和 `verifier/grading_details.json`

---

## harbor.py — Harbor 子程序

**`build_command(spec)`** — 從 `JobSpec` 組出完整的 `uv run harbor run ...` 指令陣列。

**`build_command_from_args(harbor_args)`** — 給 `run` subcommand 用，把 raw CLI args 包上 repo 預設值（config、tasks 路徑、agent）。

**`run(command, forward_signals)`** — 用 `subprocess.Popen` 啟動 Harbor，可選是否把 SIGINT/SIGTERM 轉發給子程序（suite 的 worker thread 必須設 `forward_signals=False`，因為 signal handler 只能在 main thread 設定）。跑完後呼叫 `run_cleanup()` 清理 Docker。

**`run_preflight()`** — 執行 `scripts/harbor_preflight.sh`：預先 build Docker image、清理舊的 Harbor compose project。

---

## resume.py — Resume 與 Repair

### 核心概念

每次跑 `bench:job` 時，先計算 `ResumeRepairPlan`（dry-run），再執行修補：

**Trial 狀態分類**（`plan_job_dir_for_resume()`）

| 狀態 | 含義 | 處理 |
|---|---|---|
| `complete` | 正常完成 | 保留不動 |
| `retryable` | Harbor 標記可重試 | 封存，重跑 |
| `interrupted` | 被 kill 中斷（有 signal 記錄）| 封存，重跑 |
| `incomplete` | `result.json` 不存在 | 封存，重跑 |
| `stale` | task 的 `problem.yaml` checksum 變了 | 封存，重跑 |
| `corrupt` | `result.json` 解析失敗 | 封存，重跑 |

封存路徑：`jobs/<job-name>/.resume-pruned/<timestamp>/<reason>/`，原始資料保留不刪。

**Config patch**（機器搬移或路徑變動時自動修復）

- `config.json` 的 `jobs_dir`、`tasks_path`
- 所有 trial `config.json` 的 `trials_dir`、`task.path`
- `n_concurrent_trials`（可在 resume 時調整並行度）
- `environment.delete`（對齊 `job.yaml` 設定）

**Semantic mismatch 檢查**

model_name、reasoning_effort、n_attempts、agent 等欄位不符時拒絕 resume，避免把不同 model 的結果混在同一個 job 目錄裡。

---

## scenario_clock.py — 情境時間

`O11Y_SCENARIO_TIME_ISO` 環境變數控制整個 benchmark 的「現在」：

- Agent 把這個時間當作 `now`，推算 query 的時間範圍
- Verifier 用這個時間解析 facts 的 canonical query 結果
- regrade 時重播相同的時間，確保結果可比較

```python
resolve_scenario_time()    # 讀 env var，或用真實 now()
bound_scenario_time(iso)   # context manager，暫時覆蓋時間（regrade 用）
```

---

## regrade_stack.py — Regrade 用的臨時 Stack

部分 check（`dashboard_state`、`datasource_inventory`、`datasource_detail`、facts 中有 query）需要存取 live Grafana stack 才能驗證。Regrade 時沒有 Harbor 幫你起 stack，所以這個模組負責：

1. 判斷 `problem_requires_live_stack(problem)` → 是否需要起容器
2. 用 `docker run -d --rm` 起 `o11y-bench-o11y-stack:latest`，隨機 port 映射到 host
3. 等待 MCP server 就緒（最多 150s）
4. 把 `GRAFANA_URL`、`PROMETHEUS_URL`、`LOKI_URL`、`TEMPO_URL`、`MCP_URL` 設進環境變數
5. regrade 完成後 `docker rm -f` 清除容器

---

## Job 目錄結構

```
jobs/
  <job-name>/                         # 一個 job = 一個 model × effort 組合
    config.json                       # Harbor 的 job config（自動生成）
    job.log                           # Harbor log
    result.json                       # 所有 trials 的分數摘要
    run_report.html                   # HTML 報告
    <task-name>__<trial-id>/          # 一個 trial = 一次 task 執行
      config.json                     # trial config
      result.json                     # trial 結果
      agent/
        instruction.txt               # 題目文字
        trajectory.json               # 完整對話紀錄
        command-0/stdout.txt          # tool call 摘要
      verifier/
        reward.txt                    # 分數（0.0–1.0）
        grading_details.json          # 各 criterion 分數 + 解釋
    .resume-pruned/                   # 封存的舊 trials（resume 時產生）
```

Suite 的結構是多個 job 目錄並列在同一個 `<suite-id>/` 下，加上一個 `comparison.html`。
