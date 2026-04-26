# scripts/ 目錄說明

三個維運腳本，處理 task 生成、Docker 環境預備、stack 健康檢查。

---

## 檔案對照

```
scripts/
  sync_tasks.py           # 從 tasks-spec/ 生成 tasks/ 目錄（主要維護入口）
  harbor_preflight.sh     # 清理舊 Docker 容器、pre-build 共用 image
  grading_stack_smoke.py  # 對 live stack 做 HTTP smoke test
```

---

## sync_tasks.py — Task 生成

`mise run setup:sync` 觸發的腳本。把 `tasks-spec/` 下的 YAML spec 轉換成 Harbor 可執行的 task 目錄結構，輸出到 `tasks/`。

### 每個 task 目錄的結構

```
tasks/<task-id>/
  instruction.md           # 題目文字（直接從 spec.statement 輸出）
  task.toml                # Harbor task 設定（timeout、env vars、Docker image、MCP server）
  environment/
    Dockerfile             # 從 environment/ 複製
    docker-compose.yaml    # 從 environment/ 複製
    setup.json             # spec 中 setup_* 欄位的資料（e.g. setup_dashboards）
  tests/
    problem.yaml           # 原始 spec 複本（verifier 讀這個）
    verifier.py            # 從 grading/verifier_launcher.py 複製
    test.sh                # 從 grading/test.sh 複製
    grading/               # grading/ 目錄下所有 .py 和 grader_prompt.txt 的複本
```

### task.toml 內容

```toml
[verifier.env]
ANTHROPIC_API_KEY = "${ANTHROPIC_API_KEY}"   # grading judge 用
GRADING_MODEL = "claude-haiku-4-5-20251001"
SKIP_LLM_GRADING = "${SKIP_LLM_GRADING}"    # 設為 1 可跳過 LLM rubric

[environment]
docker_image = "o11y-bench-main:latest"
cpus = 1
memory_mb = 2048
storage_mb = 10240

[[environment.mcp_servers]]
name = "mcp-grafana"
url = "http://o11y-stack:8080/mcp"
```

Harbor 在啟動 verifier 時會把這些 env vars 從 host 環境傳進容器，所以 host 上要先 export 好 `ANTHROPIC_API_KEY`。

### 關鍵行為

- **全量重新生成**：每次執行都把現有的 task 目錄完全刪除重建（`shutil.rmtree` + 重建）
- **孤立 task 清理**：spec 裡消失的 task，對應的 `tasks/` 目錄也會被刪除
- **grading 程式碼同步**：`grading/` 下的 Python 檔和 prompt 會一起複製進每個 task，確保 verifier 容器內用的是最新版本

### 輔助模式

```bash
# 只列出 spec 中所有 task ID，不實際生成
uv run python -m scripts.sync_tasks --list-ids

# 輸出到非預設目錄
uv run python -m scripts.sync_tasks --output-dir /tmp/my-tasks

# 從單一 spec 檔案生成
uv run python -m scripts.sync_tasks --path tasks-spec/prometheus_query/promql-error-rate.yaml
```

### 對任意 spec 路徑的支援（materialize）

當 CLI 傳入 `--path` 指向一個 spec 目錄（不是已生成的 tasks 目錄）時，`materialize_specs_path()` 會把它生成到 `.cache/tasks/<name>-<hash>/`，用 spec 路徑的 SHA256 前 12 碼做隔離。這讓同時測試多個不同 spec 集合成為可能。

---

## harbor_preflight.sh — Docker 預備

`mise run setup:preflight` 觸發，也在每次 `bench:job` 或 `bench:suite` 前自動跑。

### 兩個階段

**1. 清理 stale Harbor compose project**

Harbor 以 Docker compose project 的形式管理容器，異常中斷時容器可能殘留。腳本透過 `com.docker.compose.project.config_files` label 識別 Harbor 的容器：

- 若沒有 Harbor controller 在跑（`harbor run` 行程不存在）→ 所有 Harbor compose project 都視為 stale，全部清除
- 若有 Harbor controller 在跑（suite 進行中）→ 只清除 main container 已不存在的 project（不打斷正在跑的 trial）

清除時用 `docker rm -f`，遇到 "removal already in progress" 最多重試 4 次。

**2. Pre-build 共用 image**

```bash
docker build -t o11y-bench-main:latest -f environment/Dockerfile environment/
docker build -t o11y-bench-o11y-stack:latest -f docker/Dockerfile docker/
```

提前 build 好兩個 image，讓 Harbor 跑 trial 時不需要臨時 build，加快啟動速度。

**`--cleanup-only` 模式**

```bash
bash scripts/harbor_preflight.sh --cleanup-only
```

只做清理，不 build image。`harbor.py` 的 `run_cleanup()` 每次跑完後用這個模式清場。

---

## grading_stack_smoke.py — Stack 健康檢查

`mise run setup:smoke` 觸發，用於確認 o11y-stack 容器正常運作且資料齊全。需要先手動啟動 stack：

```bash
docker run --rm \
  -p 3000:3000 -p 9090:9090 -p 3100:3100 -p 3200:3200 -p 8080:8080 \
  o11y-bench-o11y-stack

uv run python scripts/grading_stack_smoke.py
```

### 檢查項目

| 項目 | 是否必要 | 驗什麼 |
|---|---|---|
| `prometheus_instant` | 選用 | 5xx rate 計算回傳合理比值（0–1），TSDB 無資料時 SKIP |
| `loki_instant` | 必要 | LogQL count_over_time 回傳非負數 |
| `loki_top_path_5xx` | 必要 | 按 path 統計的 5xx topk 有結果 |
| `tempo_search` | 必要 | TraceQL 搜尋 order-service 至少有一條 trace |
| `grafana_health` | 必要 | `/api/health` 回傳 `database: ok` |
| `grafana_dashboard_uid` | 選用 | service-overview dashboard 存在（Harbor 掛載 setup.json 後才有）|

任一必要項目失敗時 exit code 1，SKIP 項目不影響結果。

### 環境變數

預設對 localhost，可透過環境變數覆蓋：
```bash
GRAFANA_URL=http://... PROMETHEUS_URL=http://... LOKI_URL=http://... TEMPO_URL=http://...
uv run python scripts/grading_stack_smoke.py --timeout 30
```
