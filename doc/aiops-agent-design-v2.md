# AIOps Agent v2：MVP Review 與改良設計

> 在 MVP（`feat/aiops_agent` 分支）跑起來後，針對成本、可擴展性、context 控制做的二輪設計。
> 前置閱讀：[aiops-agent-design.md](./aiops-agent-design.md)（pre-MVP 架構決策）。

---

## 0. TL;DR

MVP 目前用 `create_react_agent` 一個 node 跑到底，靠 prompt discipline 控制 datasource-side aggregation。三個主要問題：

1. **MCP tool output 沒有 wrapper cap** — LLM 一時失手寫成 `{service="payment"}` 沒加聚合，幾 MB log 會原樣灌進 context。
2. **沒有 code interpreter** — log 語意分群、跨 datasource join、自訂統計都做不到。
3. **純 ReAct、無 planner / summarizer** — 長 RCA 線性堆 context，每輪成本疊加。

加上即將定義的 log schema（固定欄位 `git_repo` / `git_version` / `event`，其中 `git_version` 同時出現在 metrics label），這份文件記錄如何用結構化的方式一次解掉這三點，而非引入 code interpreter。

---

## 1. 新前提：固定 log schema

Demo data 與內部規範會強制：

| 欄位 | 出現位置 | 用途 |
|------|----------|------|
| `git_repo` | log field | 從任一行 log 取得對應 GitHub repo，免維護 service→repo 表 |
| `git_version` | log field **與** metrics label | 跨 datasource join 的主鍵 |
| `event` | log field | 業務語意 key（enum-like，低 cardinality） |

這份 schema 是底下所有改良能成立的前提。若 schema 沒落地，所有 fallback aggregation 都還是要靠 LLM 自己想 group by 哪個欄位。

### 1.1 Cardinality 守則（必須先講清楚）

`event` **必須是 enum-like、cardinality < 200**。動態 ID（`user_123_login_failed`）走 log message body，不走 `event` label。

理由：`sum by (event)` 是底下 fallback aggregation 的核心動作；如果 `event` 是高基數，Loki label 會爆，agent 拿到的 fallback 結果也會回幾千列，等同沒做 cap。

---

## 2. Schema 直接消除的需求

| 原本需要 code interpreter 的場景 | Schema 落地後的做法 |
|---|---|
| Log message 語意分群 | `sum by (event) (count_over_time(...))` |
| Trace/metric/log 跨源 join | 全部用 `git_version` 當 join key |
| 找 deploy 對應 repo | 從任一條 log 讀 `git_repo`，不查對照表 |
| 「新版本是不是壞的？」 | `rate(http_errors_total) by (git_version)` |
| 變化點偵測（粗略） | 對 `(git_version, event)` group by 後比 count 差 |

剩下真的需要 code interpreter 的場景（cross-correlation、嚴謹 anomaly score、自訂時序模型）數量少，**延後到實際遇到再加**。

---

## 3. Graph 重構

從單一 ReAct node 拆成三段：

```
START
  │
  ▼
[planner]      ── 輸出 InvestigationPlan（structured output）
  │             context = user question + schema catalog 摘要
  ▼
[executor]     ── ReAct loop on wrapped tools
  │             每個 hypothesis 跑完：
  │              1) 把該輪 ToolMessage 從 messages 砍掉
  │              2) 把結果 append 進 state.findings
  │              3) 檢查 budget，超了直接跳 finalizer
  ▼
[finalizer]    ── 只看 plan + findings，不看 raw tool messages
  │
  ▼
 END
```

### 3.1 State schema

```python
class VersionRef(TypedDict):
    git_repo: str
    from_version: str
    to_version: str

class EventStat(TypedDict):
    event: str
    count: int
    delta_vs_baseline: str   # "+1100%"
    git_version: str | None

class QueryCitation(TypedDict):
    tool: str
    args: dict
    key_result: str          # 一句話濃縮

class Findings(TypedDict, total=False):
    incident_window: str
    affected_services: list[str]
    suspect_versions: list[VersionRef]
    top_events: list[EventStat]
    evidence: list[QueryCitation]

class Budget(TypedDict):
    max_tool_calls: int
    max_tokens: int
    used_tool_calls: int
    used_tokens: int

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    plan: InvestigationPlan
    findings: Findings
    budget: Budget
```

**為什麼 findings 是 structured 不是 free text**：summary 的 schema 已經被 log schema 決定了。`top_events`、`suspect_versions` 都對應到 log/metric 的具體欄位，不需要 LLM 自由發揮。這同時是「summarizer node 的替代品」—— summarizer 不是另一個 LLM call，是 executor 每輪結束時把結果套進 slot 的純 code。

### 3.2 Planner node

用 `llm.with_structured_output` 強制 schema，不讓 LLM 寫長計畫：

```python
class Hypothesis(BaseModel):
    id: str                                          # "H1"
    statement: str                                   # "v2.5.0 在 now-1h 後 error 飆高"
    primary_datasource: Literal["prom", "loki", "tempo"]
    group_by: list[str]                              # ["git_version", "event"]
    expected_signal: str                             # "error rate > baseline x2"

class InvestigationPlan(BaseModel):
    incident_window: str                             # "now-1h..now"
    hypotheses: list[Hypothesis]                     # 2-4 個
```

Planner 的 context 只放 user question + schema catalog 摘要（不是完整 `schema_catalog.md`）。完整 catalog 留給 executor。

### 3.3 Executor 內的 prune 規則

每個 hypothesis 跑完一輪後：

1. 把該輪的所有 `ToolMessage` 從 `state.messages` 移除
2. 留一句 `AIMessage`：`"H1 -> confirmed, query=..."`
3. raw 資料寫進 `findings.evidence`，finalizer 要的時候自己讀

效果：messages 線性增長被砍斷；每輪 LLM call 的 input token 維持在常數量級。

---

## 4. Tool wrapper：schema-aware truncation

新檔：`service/app/tools/wrap.py`。

核心想法：**truncation 不要 head N 截斷，要按 schema 重打一個 aggregation query**。

```python
LOKI_CAP_BYTES = 8 * 1024
TEMPO_CAP_BYTES = 8 * 1024

async def cap_loki_output(query: str, result: dict, time_range: tuple) -> dict:
    size = _approx_size(result)
    if size <= LOKI_CAP_BYTES:
        return result

    fallback_query = f"""
      topk(20,
        sum by (service, level, event, git_version) (
          count_over_time(({_extract_selector(query)})[{_window(time_range)}])
        )
      )
    """
    agg = await _loki_client.query(fallback_query, time_range)
    return {
        "truncated": True,
        "original_query": query,
        "reason": f"raw output {size}B > cap {LOKI_CAP_BYTES}B",
        "fallback_aggregation": agg,
        "hint": (
            "Raw output too large. Auto-aggregated by "
            "(service, level, event, git_version). "
            "Pick a specific event/version and re-query with that filter."
        ),
    }
```

Tempo 同模式：超量 fallback 成 `count by (resource.service.name, span:status, status_code)`。
Prometheus 通常已聚合過，cap 很少觸發；若觸發代表 query 寫錯（沒加 `sum by`），直接回 error 訊息要 LLM 改。

### 4.1 接進 LangGraph

```python
async def _build_agent():
    mcp_tools = await _mcp_client.get_tools()
    wrapped = [
        wrap_with_cap(t) if t.name in {"query_loki_logs", "query_tempo_traces"}
        else t
        for t in mcp_tools
    ]
    tools = wrapped + [github_compare, github_get_file]
    tool_node = ToolNode(tools, handle_tool_errors=True)
    ...
```

### 4.2 為什麼這個比「head N 截斷」好

Head N 截斷的問題：LLM 拿到的是「隨機的前 N 行」，無法從中推斷分佈。Schema-aware fallback 給的是「按結構化欄位聚合的 top-K」，LLM 直接看出哪個 `event` 在哪個 `git_version` 爆量，可以下一步精準下鑽。

---

## 5. Budget guardrail

LangGraph 的 `recursion_limit` 只擋無限 loop，token 用量要自己 track。

```python
class Budget(TypedDict):
    max_tool_calls: int       # 預設 15
    max_tokens: int           # 預設 30000
    used_tool_calls: int
    used_tokens: int
```

Executor 每次 LLM call 前檢查；超 budget 強制 transition 到 finalizer，並在 messages 留一句 `"Budget exhausted; synthesizing partial answer from findings."`。

---

## 6. Code interpreter：延後決策

延後而非永久不做。觸發加入的條件：

- 出現「PromQL/LogQL 無法表達」的具體分析需求（不是猜測，是 incident 後復盤發現的）
- 例如：「兩個 metric 的時序相關係數」、「在無 `git_version` label 的舊資料上做變化點偵測」

加入時用 sandboxed runtime（e2b / pyodide），不要直接 `exec`。輸入限定為「前一個 tool 的結構化 output」，不給網路權限。

---

## 7. Migration 順序

按投入產出比：

| # | 動作 | 影響範圍 | 為什麼這個順序 |
|---|------|----------|----------------|
| 1 | Demo data 灌入 `git_repo` / `git_version` / `event` | data layer | 沒有 schema 一切白談 |
| 2 | Wrapper cap（schema-aware fallback） | `service/app/tools/wrap.py` 新檔 | 最大破口、改動最小、立刻省錢 |
| 3 | `Findings` structured state + 每輪 prune ToolMessage | `service/app/agent.py` 大改 | context 線性增長立刻解掉 |
| 4 | Planner node + `with_structured_output` | `service/app/agent.py` 加 node | 多步調查才看得出效果 |
| 5 | Budget guard | state 加欄位 + executor 加檢查 | 防呆 |
| 6 | Postgres checkpointer 取代 `MemorySaver` | 部署層 | production 才需要 |
| 7 | （延後）Code interpreter | 新 tool | 等真的遇到 schema 解不掉的分析 |

---

## 8. 未決問題

- **`event` 的 enum 由誰維護？** 是 service owner 在 code 裡寫死，還是有一份 central registry？這影響 schema catalog 怎麼產。
- **歷史資料怎麼辦？** Schema 落地前的 log 沒有 `git_version`，那段時間的 RCA 走另一條路（degraded mode）還是直接放棄？
- **Planner 失誤怎麼回收？** 如果 planner 給的 hypothesis 全錯，executor 跑完只會得到「全部沒驗證到」的 findings。要不要在 executor 中加一條 escape：findings 都空時允許 fallback 回原本 ReAct loop？
