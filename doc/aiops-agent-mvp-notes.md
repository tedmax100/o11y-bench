# AIOps Agent MVP 開發筆記

> 邊做邊記。每個段落是一個實際遇到的決定 / 卡點 / 解法。
> 設計層的思路在 [aiops-agent-design.md](./aiops-agent-design.md)。

* * *

## 目標範圍（MVP）

只驗證一件事：**使用者在 Grafana 裡打字 → agent 透過 grafana-mcp 查資料 → 結果（含中間步驟）stream 回 UI**。

刻意不做：

- 多 node graph（用 prebuilt ReAct）
- Schema catalog
- `summarize_*` tool
- k8s MCP
- 完整認證鏈

## 技術選型

| 元件 | 選擇 | 理由 |
|------|------|------|
| LLM | gemini-3.1-flash-lite | 成本低、tool use 支援、速度快適合 MVP 多輪測試 |
| Agent 框架 | LangGraph (prebuilt ReAct) | stateful、stream 友善、之後好擴成多 node graph |
| Backend | FastAPI + SSE | LangGraph stream 直接接 SSE，前端好接 |
| Plugin 形式 | App plugin | 全頁面 chat，session 多輪友善 |
| Plugin scaffold | `@grafana/create-plugin` | 官方 toolchain、unsigned dev mode 內建 |
| 套件管理 (Python) | uv | 跟 repo 一致 |
| Plugin ID | `tedmax100-aiops-app` | `<org>-<name>-app` 命名約束 |
| 接資料源 | grafana-mcp (sidecar 已內建) | 不重造輪子 |

* * *

## 開發紀錄

### v0.0.1 — 端到端 scaffold 完成

**完成的事**：

1. `aiops-agent/service/` Python 專案：
   - `pyproject.toml` 含 fastapi / langgraph / langchain-google-genai / langchain-mcp-adapters
   - `app/agent.py`：prebuilt ReAct agent + MemorySaver checkpoint + MCP client 接 grafana-mcp (streamable-http)
   - `app/main.py`：FastAPI + SSE endpoint `POST /chat`，事件型別 `token` / `tool_start` / `tool_end` / `done` / `thread`
2. `aiops-agent/plugin/` Grafana app plugin：
   - 用 `@grafana/create-plugin@7.6.0` 起的 scaffold，pluginType=app
   - 刪掉樣板 PageOne~Four，只留 `ChatPage`
   - ChatPage 自己用 `fetch().body.getReader()` parse SSE（不能用 EventSource 因為 POST）
3. `aiops-agent/docker-compose.yaml`：
   - 直接 build 既有 `docker/Dockerfile`（不重複裝 mcp-grafana / Loki / Tempo / Prometheus）
   - 把 `plugin/dist/` 掛到 `/var/lib/grafana/plugins/tedmax100-aiops-app`
   - 設 `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS`

### 過程踩到的坑

**1. `@grafana/create-plugin` 沒有完全 non-interactive mode**

`--pluginType --pluginName --orgName` 可以走 flag，但「Add a backend?」這題沒對應 flag，要 pipe stdin 用 `printf "n\n..." |` 餵 default。後續若要再 scaffold，最快是這樣：

```bash
printf "n\nn\nn\nn\n" | npx -y @grafana/create-plugin@latest \
  --pluginType app --pluginName aiops --orgName tedmax100
```

**2. Plugin 預設樣板包含 4 個 demo pages**

要動三處：`plugin.json` 的 `includes`、`constants.ts` 的 `ROUTES` enum、`App.tsx` 的 `<Route>`。然後 e2e test (`tests/appNavigation.spec.ts`) 也會 reference 舊 page，要一併改，不然 CI 會壞。

**3. SSE 在 plugin 端只能用 fetch + stream reader**

`EventSource` 不支援 POST，要用 `fetch().body.getReader()` 自己 parse `event:` / `data:` block，遇到空行作為訊息邊界。實作在 `ChatPage.tsx` 的 send handler。

**4. Plugin 的 `CLAUDE.md` 守則要遵守**

`@grafana/create-plugin` 生出來的 repo 自帶 `CLAUDE.md` + `.config/AGENTS/instructions.md`：
- 不可改 `.config/`
- 不可改 plugin id 跟 type
- `plugin.json` 改了要重啟 Grafana 才生效

**5. mcp-grafana 路徑**

sidecar 的 mcp-grafana 跑在 `:8080`，但 streamable-http 的 endpoint 在 `/mcp` 不是 `/`。要用 `http://localhost:8080/mcp` 而不是 `http://localhost:8080/`。

### 還沒驗的東西

- [x] ~~plugin webpack build~~ ✓ `npm run build` 過，dist 104KB
- [x] ~~plugin typecheck~~ ✓ `npm run typecheck` 過
- [ ] `langchain-google-genai` 實際支不支援 `gemini-3.1-flash-lite` model id（要 `GOOGLE_API_KEY` 設好後第一次 `astream_events` 才知道）
- [ ] `langchain-mcp-adapters` 接 streamable-http transport 的實際相容性（套件還滿新的）
- [ ] e2e：plugin 在 Grafana UI 真的顯示、按下 Send 真的能 stream

### 額外踩到的坑

**6. `npm audit fix --force` 會想 downgrade Grafana SDK 到 v11**

`@grafana/create-plugin@7.6.0` scaffold 出來 `package.json` 鎖 `@grafana/{data,ui,runtime}@12.4.2`，配 `grafanaDependency: ">=12.3.0"`。但 `npm audit` 看到 transitive deps 的漏洞會建議 `--force` upgrade，**反而把這幾個 Grafana package 「升」成 v11**（因為 audit fix 不認 SemVer major 方向，只看「最新沒漏洞版本」）。執行下去會：

- 跟 `grafanaDependency: ">=12.3.0"` 衝突
- `cssstyle` rename 的 ENOTEMPTY 失敗，node_modules 半套
- 修法：`rm -rf node_modules && npm install`（不要再跑 audit fix）

**結論：scaffold 提示有 12 個漏洞 (4 low / 4 moderate / 4 high) 可以忽略**，多半在 dev-only 路徑。要修也是個別 update，不要 audit fix --force。

**7. `e: any` 在 `@grafana/ui` Input 的 handler 不會被推斷**

`Input` 元件的 `onChange` / `onKeyDown` 的 callback 參數要顯式標 `React.ChangeEvent<HTMLInputElement>` / `React.KeyboardEvent<HTMLInputElement>`，不然 strict mode 噴 TS7006。

**8. Scaffold 的 AppConfig 樣板要全面換**

`create-plugin` 預設給的 AppConfig 假設你有「外部 API」要配 (api key + api url)。對 LangGraph 這種架構沒意義——我們只需要 agent service URL。要動的地方：

- `src/components/AppConfig/AppConfig.tsx`：移除 SecretInput / apiKey，只留 `agentServiceUrl` Input
- `src/components/testIds.ts`：同步改 testid key（test 會 reference）
- `src/components/AppConfig/AppConfig.test.tsx`：testid 跟 fieldset name 都會壞，要一起改
- `src/module.tsx`：`new AppPlugin<{}>()` 的泛型要換成 `AppPluginSettings`
- `src/components/App/App.tsx`：從 `props.meta.jsonData?.agentServiceUrl` 讀，傳到 ChatPage
- `provisioning/plugins/apps.yaml`：`jsonData.apiUrl` → `jsonData.agentServiceUrl`，移掉 `secureJsonData`

**9. Plugin 不會自動 enable**

第一次跑 plugin 起來，Grafana 側欄不會出現 Apps → AIOps → Chat。要先進 `/plugins/tedmax100-aiops-app` 按 Enable，或在 `provisioning/plugins/apps.yaml` 設 `disabled: false` 再把 provisioning 目錄掛進 Grafana 容器 (`/etc/grafana/provisioning/plugins`)。我們的 `docker-compose.yaml` 兩個 mount 都做了。

**10. Unsigned plugin 在 Configuration 頁會顯示警告但不影響功能**

第一次掛進去看到大黃條「Invalid plugin signature」——這是預期，因為 plugin 沒簽名。`GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS` 讓它能 load，但 UI 還是會顯示 unsigned 標籤。Production 部署才需要 `npx @grafana/sign-plugin`。

**12. sse-starlette 用 `\r\n\r\n` 當 event 分隔，不是 `\n\n`** ⚠️

最難 debug 的一條。`sse-starlette` 的 `EventSourceResponse` 預設 emit 的格式是 HTTP 規範的 CRLF：

```
event: token\r\ndata: {...}\r\n\r\n
```

我前端 SSE parser 寫 `buffer.split('\n\n')` 永遠找不到 boundary，blocks 一直空，event 永遠不會被處理——但 **fetch 是成功的，response 200，Network 的 EventStream 也顯示正常**（DevTools 自己會處理 CRLF）。

`curl -N` 看不出問題是因為 curl 把 `\r\n` 也當換行顯示。瀏覽器 reader 拿 raw bytes 就要自己處理。

**症狀**：
- Console log 只看到 `read chunk done:false valueLen:150`
- 之後沒有任何 `SSE event` log
- UI assistant 氣泡永遠空白
- Server side 一切正常，curl 也一切正常

**修法**：decode 之後先正規化 `\r\n` → `\n`：

```ts
buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
```

**教訓**：寫自己的 SSE parser 一定要同時支援 `\n\n` 跟 `\r\n\r\n` 兩種 boundary，或用 `EventSource` API（但它不支援 POST，所以才要自己寫）。Browser fetch + Reader 是 raw bytes，跟 curl 那種「smart 換行」不一樣。

**11. Gemini chunk.content 是 list of content blocks，不是 string** ⚠️

這個踩很大。`langchain-google-genai` 在 streaming 模式下，每個 `AIMessageChunk.content` 不是純字串而是 multipart：

```python
[{"type": "text", "text": "Hello!", "index": 0}]
```

跟 Anthropic 的 multipart 格式類似。如果 server 端直接 `getattr(chunk, "content", "")` 把它原樣往 SSE wire 推，前端 React 拿到 array of objects 完全不知道怎麼 render，結果就是**「氣泡出現但內容空白」**——而且沒有 error，最難 debug。

**解法**：server 端寫一個 `_flatten_content()` helper，遇到 list 就把所有 `type=text` 的 block 串起來變純字串。`final` event 也要套同樣處理（最終 message 也是 multipart）。

**教訓**：跨 provider 開發 LangChain agent，**永遠不要假設 content 是 string**。OpenAI 多半是 string，Anthropic 跟 Google 都會給 list。這條 wire 上的型別不一致是 LangChain abstraction leaky 的地方之一。

### 下一步

1. 把 `npm install` + `npm run build` 跑過確認 typecheck 沒爆
2. 設 `GOOGLE_API_KEY` 起 service，curl 戳 `/chat` 確認 MCP tools 真的拉得到
3. `docker compose up` 起 sidecar，在 Grafana UI 啟用 plugin，按 Send 看端到端
4. 看 token / latency 真實數字，決定下個迭代要先解 schema catalog 還是 summarize tool
