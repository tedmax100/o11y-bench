# AIOps Agent — 啟動 Runbook

從 v2 開始，agent 不再自帶 Grafana/Prom/Loki/Tempo —— data 是 demo-services
k3d cluster。所以要跑起來需要 **四件事**（demo + mcp-grafana + agent + plugin watch）：

```
┌─ demo-services (k3d) ─┐  ┌─ Terminal 1 ──────┐  ┌─ Terminal 2 ─────┐  ┌─ Terminal 3 ──┐
│ Grafana + Prom/Loki/  │  │ mcp-grafana       │  │ Agent service    │  │ Plugin watch  │
│ Tempo + 5 services    │  │ (docker compose)  │  │ FastAPI+LangGraph│  │ webpack -w    │
│ :3001 (graf) :8002    │  │ :8080             │  │ :8000            │  │               │
└───────────────────────┘  └───────────────────┘  └──────────────────┘  └───────────────┘
```

**Plugin UI**：`demo-services/scripts/up.sh` 會把 `aiops-agent/plugin/dist/` docker-cp
進 k3d node 的 `/aiops-plugin/`，Grafana Deployment 透過 hostPath 掛進
`/var/lib/grafana/plugins/tedmax100-aiops-app` 並用 `aiops-plugin-provisioning`
ConfigMap 自動 enable —— 起完 cluster 直接開 `http://localhost:3001` 進 Apps → AIOps → Chat。

> 改 plugin code 時 `npm run dev` 會更新 `dist/`，但 **k3d node 內的 copy 不會自動同步**。
> 改完跑：`docker cp aiops-agent/plugin/dist k3d-demo-services-server-0:/aiops-plugin/tedmax100-aiops-app && kubectl -n demo rollout restart deploy/grafana`。

每個 terminal 都從 repo root (`/home/nathan/Project/o11y-bench`) 開始。

* * *

## 一次性 setup（每台機器只做一次）

```bash
# Plugin 依賴
cd aiops-agent/plugin
npm install

# Service 依賴
cd ../service
uv sync

# Service 環境變數
cp .env.example .env
# 編輯 .env，填好 GOOGLE_API_KEY
```

⚠️ **不要跑 `npm audit fix --force`**——會把 `@grafana/{data,ui,runtime}` 降到 v11，跟 `plugin.json` 的 `grafanaDependency: ">=12.3.0"` 衝突。Scaffold 報的 12 個漏洞警告可以忽略。

* * *

## Step 0 — demo-services k3d cluster

```bash
cd ../demo-services
./scripts/up.sh                 # k3d cluster + 5 services + telemetry stack
./scripts/load.sh &             # 持續打流量產生 telemetry
```

健康檢查：

```bash
curl -s http://localhost:3001/api/health   # Grafana
curl -s http://localhost:8002/health       # webapp (public entry)
```

## Terminal 1 — mcp-grafana (Docker)

只跑 mcp-grafana 一個 container，指到 demo-services 的 Grafana。

```bash
cd aiops-agent
docker compose up --build
```

**啟動完成的訊號**：mcp-grafana 開始 listen 在 `:8080`。
驗證：`curl -s http://localhost:8080/` 回 404 算正常（它服務在 `/mcp`）。

* * *

## Terminal 2 — Agent Service

LangGraph ReAct + Gemini + MCP client，提供 `/chat` SSE endpoint 給 plugin。

```bash
cd aiops-agent/service
uv run uvicorn app.main:app --reload --port 8000
```

**啟動完成的訊號**：

```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Application startup complete.
```

⚠️ Service 跑在 **host 上**（不在 container 裡），因為要 `--reload` 改 code 自動重啟，方便 debug。
它透過 `MCP_GRAFANA_URL=http://localhost:8080/mcp` 連 sidecar 的 mcp-grafana。

### 健康檢查

```bash
curl http://localhost:8000/healthz
# {"ok":true}

# 端到端 smoke test（不經過 plugin）
curl -N -X POST http://localhost:8000/chat \
  -H "content-type: application/json" \
  -d '{"message": "hi"}'
```

正常會看到一連串 `event: token` / `data: {"type": "token", "text": "..."}`。如果只看到 `thread` + `done` 中間空白，看 Terminal 2 有沒有 traceback，最常見是 `GOOGLE_API_KEY` 沒設好或 `GEMINI_MODEL` 名字不存在。

### 進階：開 debug log

想看 LangGraph 內部每筆 event：

```bash
DEBUG_EVENTS=1 uv run uvicorn app.main:app --reload --port 8000
```

每筆 event 會 log 一行 `event=on_xxx name=... data_keys=[...]`。Production 不要開。

* * *

## Terminal 3 — Plugin Watch Build

webpack watch mode，改 `plugin/src/**` 自動 rebuild 到 `plugin/dist/`。

```bash
cd aiops-agent/plugin
npm run dev
```

**啟動完成的訊號**：

```
webpack 5.106.2 compiled successfully in 477 ms
```

之後就停在那邊不動，等你存檔自動 recompile。

### 改了之後在 Grafana 看到新版的步驟

| 改了什麼 | 怎麼套用 |
|---------|----------|
| `plugin/src/**.tsx` 純前端 code | **Hard refresh** 瀏覽器 (Ctrl+Shift+R)。Grafana 不用重啟 |
| `plugin/src/plugin.json` | **重啟 Grafana 容器**（不是 restart，是 down + up） |
| `plugin/provisioning/**` | 同上 |
| `plugin/.config/**` | 不要改，是 toolchain managed |

* * *

## 一鍵跑起來（給熟悉之後的人）

打開三個 terminal，分別貼下面：

```bash
# Terminal 1
cd aiops-agent && docker compose up --build

# Terminal 2  
cd aiops-agent/service && uv run uvicorn app.main:app --reload --port 8000

# Terminal 3
cd aiops-agent/plugin && npm run dev
```

等 Terminal 1 出現 `Environment Ready`、Terminal 2 出現 `Application startup complete`、Terminal 3 出現 `compiled successfully`，三個都綠燈後：

打開 <http://localhost:3000> → **左側欄 Apps → AIOps → Chat** → 丟訊息。

* * *

## 關閉 / 重啟整套

```bash
# 停 Terminal 2/3（Ctrl+C）

# 停 sidecar
cd aiops-agent
docker compose down

# 想清乾淨重來（刪 Grafana DB / Prometheus TSDB）
docker compose down -v
```

* * *

## Troubleshooting 速查

| 症狀 | 最可能原因 | 修法 |
|------|-----------|------|
| Grafana 看不到 AIOps app | Plugin 沒 enable 或 provisioning 沒掛進去 | 確認 `docker compose.yaml` 有掛 `./plugin/provisioning/plugins`；或 UI 進 `/plugins/tedmax100-aiops-app` 手動 Enable |
| UI 進 Chat 頁但 assistant 氣泡永遠空白 | SSE parser bug（CRLF）或 chunk content 是 list | 看 `doc/aiops-agent-mvp-notes.md` 第 11、12 條 |
| Console 顯示「agent service returned 0」 | CORS 沒過或 service 沒起 | 看 Terminal 2 有沒有起來；確認 `service/app/main.py` 的 `cors_allow_origins` 包含你 Grafana 的 origin |
| Service log 看到 `MCP connection refused` | Sidecar 還沒 ready 或 `MCP_GRAFANA_URL` 設錯 | 等 Terminal 1 出現 `Environment Ready`；確認 `.env` 是 `http://localhost:8080/mcp`（**結尾要有 `/mcp`**） |
| `uv sync` warning `VIRTUAL_ENV does not match` | repo 根有另一個 `.venv` | 無害，可忽略；想乾淨就 `unset VIRTUAL_ENV` 再跑 |
| Plugin 改 code 後不生效 | 瀏覽器 chunk cache | DevTools → Application → Clear site data，或開無痕 |
| Plugin 改 `plugin.json` 後不生效 | Grafana manifest cache | `docker compose down && docker compose up --build`，不是 restart |

* * *

## 相關文件

- 設計思路：[../doc/aiops-agent-design.md](../doc/aiops-agent-design.md)
- 開發過程踩到的坑：[../doc/aiops-agent-mvp-notes.md](../doc/aiops-agent-mvp-notes.md)
