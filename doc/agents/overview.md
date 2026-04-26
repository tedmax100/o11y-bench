# agents/ 目錄說明

`agents/` 包含 o11y-bench 的預設 agent 實作。Agent 是跑在 Docker 容器內的程式，負責接收題目、透過 MCP tools 操作 Grafana stack、產出回答。

---

## 檔案對照

```
agents/
  o11y_agent.py             # Harbor agent 入口，由 Harbor 框架呼叫
  agent_runner.py           # 真正的 agent loop，跑在容器內部
  system_prompt.txt         # agent 的 system prompt
  task_prompt.txt           # 包裝題目的 prompt 模板
  langchain_o11y_agent.py   # LangChain 版 agent 入口（範例）
  langchain_agent_runner.py # LangChain 版 agent loop
```

---

## 執行流程

```
Harbor 框架
  └─ 呼叫 O11yBenchAgent.run()          [o11y_agent.py]
       └─ 把 agent_runner.py 上傳到容器
       └─ 用 uv run 啟動它
            └─ agent_runner.py 跑 agent loop [在容器內]
                 ├─ 連接 MCP server (mcp-grafana)
                 ├─ 取得 36 個工具定義
                 ├─ 進入 while True 循環
                 │    ├─ 呼叫 LLM（litellm 支援各 provider）
                 │    ├─ 若有 tool_calls → 執行並把結果回饋給 LLM
                 │    └─ 若無 tool_calls → 印出最終答案，結束
                 └─ 寫出 trajectory.json
```

---

## o11y_agent.py — Harbor 入口

`O11yBenchAgent` 繼承 Harbor 的 `BaseAgent`，負責：

- 把 `agent_runner.py`、`system_prompt.txt`、`task_prompt.txt` 上傳進容器
- 把 host 的 API keys 轉傳給容器環境變數（`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`GEMINI_API_KEY` 等）
- 啟動 runner，等它結束
- 把 `trajectory.json` 從容器下載回 host
- 若 runner exit code ≠ 0 → 拋出 `NonZeroAgentExitCodeError`

model 名稱轉換：`google/gemini-2.5-pro` → `gemini/gemini-2.5-pro`（litellm 的前綴格式）。

---

## agent_runner.py — 核心 loop

跑在容器裡的 Python 腳本，用 `uv run` 啟動（inline script，dependencies 寫在頂部 header 裡）。

### 主要邏輯

```python
while True:
    resp = await litellm.acompletion(messages, tools=mcp_tools)
    if no tool_calls:
        write trajectory, print "done", break
    for tc in tool_calls:
        out = await mcp_session.call_tool(tc.name, tc.args)
        messages.append(tool result)
    flush_trajectory()   # 每步都寫出，partial work 不會丟失
```

最多跑 50 步（`MAX_AGENT_STEPS`），超過會 raise RuntimeError。

### Prompt caching

| Provider | 實作方式 |
|---|---|
| Anthropic | litellm 自動注入 `cache_control` breakpoint 在 system message |
| OpenAI | 1024+ token prompt 自動 cache |
| Gemini | Google server-side 自動處理 |

### Retry 機制

遇到 429 / 503 / 529 或 rate limit 相關錯誤時自動 retry，delay 從 response header `Retry-After` 讀取，否則指數退避（最多 60s），最多重試 5 次。

### Trajectory 格式（ATIF-v1.6）

每次跑完會產出 `trajectory.json`，結構：

```json
{
  "schema_version": "ATIF-v1.6",
  "session_id": "...",
  "agent": { "name": "o11y-bench", "model_name": "...", "tool_definitions": [...] },
  "steps": [
    { "step_id": 1, "source": "system", "message": "..." },
    { "step_id": 2, "source": "user",   "message": "題目內容" },
    { "step_id": 3, "source": "agent",  "tool_calls": [...], "observation": {...}, "metrics": {...} },
    { "step_id": 4, "source": "agent",  "message": "最終回答" }
  ],
  "final_metrics": {
    "total_prompt_tokens": 59589,
    "total_completion_tokens": 1643,
    "total_cost_usd": 0.064,
    "total_tool_calls": 4,
    "elapsed_seconds": 23.3
  }
}
```

---

## Prompts

### system_prompt.txt

```
You are a helpful assistant working with a Grafana monitoring stack.
Use the tools you are given to interact with the environment.
...
Act autonomously. Do not ask the user for clarification.
Base claims on observed tool results.
```

關鍵指令：
- 把 `<context>` 裡的 `Current time` 當作 scenario 的「現在」，不要用真實的 `now()`
- 建 Grafana dashboard 時一步給出完整 model
- 答案必須基於工具回傳的資料，不能靠記憶推測

### task_prompt.txt

```
<context>
Current time: {current_time}
</context>

{statement}
```

`current_time` 來自 `O11Y_SCENARIO_TIME_ISO` 環境變數（scenario 固定時間），`statement` 是 task 的題目文字。

---

## LangChain 版（範例用途）

`LangChainO11yBenchAgent` + `langchain_agent_runner.py` 是一個 **示範性的自訂 agent**，展示如何用不同 framework 接進 benchmark。

和預設版本的差異：

| | 預設（litellm） | LangChain 版 |
|---|---|---|
| LLM 呼叫 | litellm.acompletion | langchain init_chat_model |
| MCP 整合 | mcp SDK 直接呼叫 | langchain-mcp-adapters |
| Retry 邏輯 | 自己實作 | 依賴 LangChain |
| Cost tracking | 有 | 無（寫死 0.0）|

跑法：

```bash
mise run bench:job -- --model openai/gpt-4o --task-name query-cpu-metrics \
  --agent-import-path agents.langchain_o11y_agent:LangChainO11yBenchAgent
```

---

## 換自己的 Agent

Harbor 支援兩種方式：

**1. 內建 agent（Harbor built-in）**
```bash
mise run bench:job -- --model openai/gpt-4o --agent opencode
```

**2. 自訂 class（import path）**
```bash
mise run bench:job -- --model openai/gpt-4o \
  --agent-import-path agents.langchain_o11y_agent:LangChainO11yBenchAgent
```

自訂 agent 需繼承 `harbor.agents.base.BaseAgent`，實作 `setup()` 和 `run()` 兩個方法，並在 `run()` 結束前把 `trajectory.json` 寫到 `/logs/agent/`。
