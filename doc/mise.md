# mise 是什麼

[mise](https://mise.jdx.dev) 是一個開發環境管理工具，做兩件事：

1. **管理工具版本**：幫你裝指定版本的 Python、uv 等，不用手動 nvm/pyenv
2. **定義 task 捷徑**：把常用指令包成短名稱，類似 Makefile 但更簡潔

設定檔是專案根目錄的 `mise.toml`。

---

## 這個專案用到的工具

```toml
[tools]
python = "3.14"
uv = "latest"
```

第一次在這個目錄執行 `mise install`，mise 就會自動裝好對應版本，之後進這個目錄就自動用這個版本。

---

## Task 捷徑對照表

### 環境設定

| 指令 | 實際執行 | 說明 |
|---|---|---|
| `mise run setup:sync` | `uv run python -m scripts.sync_tasks` | 從 tasks-spec/ 生成 tasks/ |
| `mise run setup:preflight` | `bash scripts/harbor_preflight.sh` | 清理舊容器、預先 build Docker image |
| `mise run setup:smoke` | `uv run python scripts/grading_stack_smoke.py` | 確認 o11y-stack 正常運作 |

### 跑 benchmark

| 指令 | 實際執行 | 說明 |
|---|---|---|
| `mise run bench:job` | `uv run python -m o11y_bench job` | 跑單一模型（自動先跑 setup:sync）|
| `mise run bench:suite` | `uv run python -m o11y_bench suite` | 跑完整 suite（所有模型）|
| `mise run bench:finalize` | `uv run python -m o11y_bench finalize` | 只生成報告，不跑 agent |

### 報告

| 指令 | 實際執行 | 說明 |
|---|---|---|
| `mise run report` | `uv run python -m reporting.report` | 重建跨模型 leaderboard |

### 開發

| 指令 | 實際執行 | 說明 |
|---|---|---|
| `mise run test` | `uv run pytest` | 跑測試 |
| `mise run typecheck` | `uv run mypy` | 型別檢查 |
| `mise run lint` | `uv run ruff check .` | Lint |
| `mise run format` | `uv run ruff format .` | 格式化 |

---

## 怎麼傳參數給 task

用 `--` 隔開，後面的都會傳給實際指令：

```bash
mise run bench:job -- --model anthropic/claude-sonnet-4-6 --task-name query-cpu-metrics
# 實際執行：uv run python -m o11y_bench job --model anthropic/claude-sonnet-4-6 --task-name query-cpu-metrics
```

---

## depends 的意思

部分 task 有 `depends`，代表執行前會自動先跑依賴的 task：

```toml
[tasks."bench:job"]
depends = ["setup:sync"]   # 跑 bench:job 前會自動先跑 setup:sync
```

所以直接 `mise run bench:job` 就夠了，不用手動先跑 `setup:sync`。
