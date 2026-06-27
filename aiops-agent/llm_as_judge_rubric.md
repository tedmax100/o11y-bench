# 別再盲信 Prompt Engineering：為 AI Agent 建立低成本的糾錯系統

我們的 AIOps agent 有個很具體的問題：它會捏造 trace ID。

當 on-call 工程師問「payment service 有沒有 error trace？」，agent 有時會信心滿滿地回答「是的，見 trace `a1b2c3d4...`」——但這串 ID 根本不存在 Tempo 裡。工程師點進去，404。壞的不只是使用者體驗，而是這讓整個 RCA 結論失去可信度。

另一個問題是 k8s 操作。我們的 agent 可以提議執行 `rollout_undo` 或 `scale`。這些操作有真實影響：把 replica 縮到 0 可以讓服務整個掛掉。我們有 blast-radius gate、circuit breaker，但這些 gate 都是規則型的，覆蓋不到「action 本身語意上合不合理」的問題。

這兩個問題的共通點是：**規則寫不完，但另一個 LLM 可以判斷**。這就是 LLM-as-Judge 的出發點。

* * *

## RubricMiddleware 在做什麼

先說名詞。「Rubric」在不同脈絡下指的是不同層次的東西：評測框架（如 o11y-bench）裡的 rubric 是**事後打分的標準**，跑完 benchmark 後由 LLM judge 對 agent 的 transcript 評 YES/NO；而這裡 `RubricMiddleware` 的 rubric 是**執行期的品質規格**，agent 每次產出答案就觸發一次審查，不通過就打回去重試。同一個詞，一個離線評估能力，一個即時糾錯——不要混淆。我們最後的實作叫「guard」而非 rubric，也是刻意區分：guard 強調二元的阻擋語意，不是打分。

LangChain 的 [RubricMiddleware](https://docs.langchain.com/oss/python/deepagents/rubric) 把執行期糾錯這個 pattern 包成 middleware，接在 `create_deep_agent()` 上：

```python
from langchain_deepagents import RubricMiddleware, create_deep_agent

middleware = RubricMiddleware(
    model="openai:gpt-4o-mini",   # grader 用的模型，可以比主 agent 便宜
    tools=[run_test_suite],        # grader 可以呼叫的工具（選用）
    max_iterations=3,              # 最多重跑幾次（上限 20）
    on_evaluation=log_verdict,     # 每次審查後的 callback（選用）
)
agent = create_deep_agent(..., middleware=[middleware])

# rubric 在呼叫時傳入，不用寫死在 agent 裡
result = await agent.ainvoke(
    {"messages": [{"role": "user", "content": "幫我寫一個 find_duplicates 函數"}]},
    rubric="""
    - 函數必須通過所有 unit test
    - 時間複雜度必須是 O(n)
    - 不能用任何第三方套件
    """,
    config={"configurable": {"thread_id": "thread-001"}},
)
```

rubric 是換行分隔的 checklist。沒有傳 rubric，middleware 就不會啟動。`thread_id` + checkpointer 讓 rubric 可以跨多次 invocation 持續，agent 可以在中斷後繼續跑。

每次 agent 產出答案，grader 就審一次，回傳五種 verdict 之一：

```plaintext
satisfied              → 全部通過，終止
needs_revision         → 注入 per-criterion feedback，agent 重跑
max_iterations_reached → 達到 max_iterations，強制終止
failed                 → rubric 本身無法評估（寫法有問題）
grader_error           → grader sub-agent 拋了 exception
```

`needs_revision` 是最重要的路徑：grader 不只說「不通過」，它會針對每個 criterion 給出具體的 gap（哪裡不對、缺什麼），然後把這份 feedback 注入回 agent 的 message history，讓 agent 下一輪有明確方向修正，而不是盲目重試。

`on_evaluation` callback 每次審查後都會觸發，拿到的是一個 `RubricEvaluation` dict：

```python
def log_verdict(evaluation: RubricEvaluation):
    print(f"iteration={evaluation['iteration']}")
    print(f"result={evaluation['result']}")       # verdict 字串
    print(f"explanation={evaluation['explanation']}")
    for c in evaluation['criteria']:
        # 每個 criterion 的個別判斷
        print(f"  [{c['passed']}] {c['criterion']}: {c['gap']}")
```

用 `stream_events(..., version="v3")` 的話，可以從 event stream 裡抓到 `rubric_evaluation_start` 和 `rubric_evaluation_end` 兩個自訂 event，適合需要即時顯示審查進度的場景。

**grader 帶工具**是這個設計最關鍵的地方。文件裡的例子是讓 grader 拿到一個 `run_test_suite` 工具，實際執行測試套件，而不是靠 LLM 自己猜程式碼對不對：

```python
@tool
def run_test_suite(code: str) -> str:
    """執行測試，回傳 pass/fail 結果"""
    # 實際跑 pytest 或 unittest
    ...

middleware = RubricMiddleware(
    model="openai:gpt-4o-mini",
    tools=[run_test_suite],  # grader 會主動呼叫這個來驗證
)
```

這把 grader 從「LLM 憑感覺打分」變成「帶工具的 sub-agent 驗證」。差別很大：前者受 LLM 幻覺影響，後者的結果是確定性的。

![](https://cdn.hashnode.com/uploads/covers/6420f5cbbdbe7d697133d12a/e7a40416-6010-4936-82d1-c2b349a447d7.png align="center")

這個設計的吸引力在於把「品質標準」從 prompt 裡抽出來，變成一個獨立可測試的元件。rubric 可以是字串，可以有工具，可以隨場景換，也可以在 runtime 動態決定要審什麼。

* * *

## 我們沒用 Middleware，但用了同樣的思路

我們的 agent 是 LangGraph + Gemini，不是 LangChain deep agent，沒辦法直接插 `RubricMiddleware`。但核心思路一樣：**在 agent 產出之後，用獨立的 LLM 或程式驗證，不通過就打回去重試**。

我們把這個邏輯包在 `rubric.py` 裡，實作了兩個 guard。

### Guard 1：Trace ID 驗證

思路很直接：用 regex 把答案裡所有 32-hex 的字串找出來，逐一去 Tempo HTTP API 確認存不存在。

```python
_TRACE_ID_RE = re.compile(r"\b([0-9a-f]{32})\b", re.IGNORECASE)

async def _tempo_trace_exists(trace_id: str) -> bool:
    url = f"{settings.tempo_url}/api/traces/{trace_id}"
    async with httpx.AsyncClient(timeout=3.0) as client:
        resp = await client.get(url)
    if resp.status_code == 404:
        return False
    return bool(resp.json().get("batches", []))

async def verify_trace_ids(answer: str) -> tuple[bool, str]:
    ids = {m.group(1).lower() for m in _TRACE_ID_RE.finditer(answer)}
    missing = [tid for tid in ids if not await _tempo_trace_exists(tid)]
    if not missing:
        return True, ""
    return False, (
        f"The trace IDs {missing} you cited do not exist in Tempo. "
        "Call `query_tempo_traces` again to find real traces."
    )
```

這個 guard 本身不用 LLM，直接查資料庫，3 秒 timeout，網路出錯就 pass-through（不能讓 infra 問題阻擋正常回答）。

「為什麼不直接在 prompt 裡叫 agent 不要捏造？」我們試過了。Prompt 有效果，但失敗率不是零。當工具回傳空結果，agent 有時會「補全」一個看起來合理的 trace ID。這種行為靠 prompt 壓不死，要靠事後驗證。

### Guard 2：K8s Write 預飛審查

這個 guard 才真正用到 LLM-as-judge。我們需要判斷「這個 action 在當前 context 下合不合理」，這是語意層面的問題，規則寫不完。

```python
class _K8sRubricVerdict(BaseModel):
    safe_to_proceed: bool
    reason: str

_K8S_RUBRIC_SYSTEM = """你是 Kubernetes 操作的安全審查員。

BLOCK（safe_to_proceed=false）的條件：
- deployment name 有萬用字元或異常通用（"all"、"*"）
- replicas = 0（服務整個掛掉）
- rollout_undo 但 context 顯示問題不是 bad deploy（例如 DB 過載、infra 故障）
- scale 倍數超過當前的 10 倍

其他情況 ALLOW。blast-radius 和 circuit breaker 已在前面跑過，只擋語意上明顯有問題的操作。"""

async def check_k8s_write(action: str, args: dict, context: str = "") -> tuple[bool, str]:
    try:
        llm = _k8s_rubric_llm()  # structured output → _K8sRubricVerdict
        verdict = await llm.ainvoke([
            SystemMessage(content=_K8S_RUBRIC_SYSTEM),
            HumanMessage(content=f"Action: {action}\nArgs: {args}\nContext: {context}")
        ])
        return verdict.safe_to_proceed, verdict.reason
    except Exception as e:
        return True, f"rubric check skipped ({e})"
```

Structured output（Pydantic model）確保 grader 一定回傳 `bool` 而非讓我們自己 parse 文字。例外情況一律放行——grader 出問題不能成為阻擋正當操作的理由。

* * *

## 接線到 LangGraph

兩個 guard 的性質不同，接的位置也不一樣。一開始我們用最快的方式驗證邏輯：在 `ainvoke()` 回傳後加一個 Python `try` block，不通過就再呼叫一次 `ainvoke()`。這能動，但它繞過了 LangGraph 的狀態機——rubric 不是圖的一部分，LangGraph Studio 看不到這個重試迴圈，`recursion_limit` 也管不到它。

重構後，trace ID guard 變成圖裡的第一等公民節點。

### LangGraph 原生做法：rubric 作為節點與條件邊

在動手之前，先把 LangGraph 的三個核心概念說清楚，因為 rubric 的設計決策都是從這裡來的。

![]( align="center")

**State** 是整張圖唯一的共享記憶體。每個節點讀入當前的 State、執行邏輯後回傳一個 dict，LangGraph 把這個 dict merge 回 State，再傳給下一個節點。節點之間沒有直接傳參，所有溝通都透過 State——這讓每個節點變成 pure function，只依賴輸入 State、只影響輸出 State，容易獨立測試。

**Node** 是圖裡的執行單元，負責「做事」。它讀 State、寫 State，但**不決定下一步去哪**。這個職責分離是刻意的設計：讓節點保持單一職責，路由邏輯集中在邊上管理，不散落在各個節點裡。

**Conditional Edge** 是路由邏輯的唯一所在。它是一個函數，讀入當前 State snapshot，回傳下一個節點的名稱。LangGraph 在每個節點執行完後呼叫這個函數，決定執行流走哪條路。`recursion_limit` 和 LangGraph Studio 的追蹤也都在這個層面生效。

![](https://cdn.hashnode.com/uploads/covers/6420f5cbbdbe7d697133d12a/c4d92e05-826c-4fd8-bcd9-4f5135677a9f.png align="center")

* * *

把 rubric 放進圖裡，就是把這三個概念各自對應到一段程式碼。

**第一步：在 State 上開兩個欄位。** rubric 節點和 agent 節點之間沒有直接傳參，唯一的溝通管道是 State。`rubric_feedback` 存放 correction prompt（空字串代表通過），`rubric_revision_count` 記錄已重試幾次，防止無限迴圈：

```python
class RcaState(TypedDict):
    messages: Annotated[list, add_messages]
    tool_calls_used: int
    budget: int
    rubric_feedback: str        # correction prompt；"" 代表通過
    rubric_revision_count: int  # 本輪已重試幾次，防止無限迴圈
```

**第二步：把驗證邏輯封裝成 Node。** `rubric_trace_node` 讀取 State 裡最後一則訊息（agent 的答案），呼叫 `verify_trace_ids` 檢查 trace ID，然後把結果寫回 State。注意它只寫 State，不做任何路由判斷——下一步去哪，是條件邊的事：

```python
async def rubric_trace_node(state: RcaState):
    msgs = state["messages"]
    answer = _flatten_content(getattr(msgs[-1], "content", None)) if msgs else ""
    try:
        ok, retry_prompt = await verify_trace_ids(answer)
    except Exception as e:
        logger.warning("rubric_trace_node: check failed (%s) — passing through", e)
        ok, retry_prompt = True, ""
    revision = state.get("rubric_revision_count", 0)
    return {
        "rubric_feedback": retry_prompt,
        "rubric_revision_count": revision + (0 if ok else 1),
    }
```

**第三步：路由邏輯放在 Conditional Edge 裡。** `route_after_rubric` 讀 State、回傳下一個節點名稱。`rubric_feedback` 非空代表發現幻覺，只要還沒超過重試上限就回到 `agent`；否則走向 `END`：

```python
_MAX_RUBRIC_REVISIONS = 1

def route_after_rubric(state: RcaState) -> str:
    if state.get("rubric_feedback") and state.get("rubric_revision_count", 0) <= _MAX_RUBRIC_REVISIONS:
        return "agent"
    return END
```

**回到 agent 時，State 上的 feedback 就是它的新任務。** `agent_node` 啟動時先檢查 `rubric_feedback`，有內容就把它轉成 `HumanMessage` 注入 prompt，讓 agent 帶著明確的糾錯指示重跑。執行完後把 `rubric_feedback` 清空，避免下一輪重複注入：

```python
async def agent_node(state: RcaState):
    feedback = state.get("rubric_feedback", "")
    extra = [HumanMessage(content=feedback)] if feedback else []
    sys = build_system_prompt() if state["tool_calls_used"] == 0 else CONTINUE_PROMPT
    msgs = [SystemMessage(content=sys)] + state["messages"] + extra
    return {
        "messages": [await llm_with_tools.ainvoke(msgs)],
        "rubric_feedback": "",
    }
```

注入的 feedback 長這樣——具體說明哪些 ID 不存在、下一步該怎麼做，不是模糊地叫 agent「重試」：

```plaintext
The trace IDs ['deadbeef...'] you cited do not exist in Tempo.
You MUST NOT invent trace IDs. Call `query_tempo_traces` again to find real traces,
then cite the `traceID` value verbatim from the tool result.
```

**最後，把節點和邊接進圖裡。** `agent` 產出答案後不再直接走向 `END`，而是先過 `rubric_trace`；`force_answer`（預算耗盡時的強制回答）也一樣。`rubric_trace` 之後才由 `route_after_rubric` 決定終止或重試：

```python
graph = StateGraph(RcaState)
graph.add_node("agent", agent_node)
graph.add_node("tools", tools_node)
graph.add_node("force_answer", force_answer_node)
graph.add_node("rubric_trace", rubric_trace_node)

graph.add_edge(START, "agent")
graph.add_conditional_edges(
    "agent", route_after_agent,
    {"tools": "tools", "force_answer": "force_answer", "rubric_trace": "rubric_trace"},
)
graph.add_edge("tools", "agent")
graph.add_edge("force_answer", "rubric_trace")
graph.add_conditional_edges(
    "rubric_trace", route_after_rubric,
    {"agent": "agent", END: END}
)
```

完整的執行流程長這樣：

![](https://cdn.hashnode.com/uploads/covers/6420f5cbbdbe7d697133d12a/75be136b-b623-4eb6-83fc-a778484432e8.png align="center")

這樣做的好處是 rubric 的重試迴圈完全在 LangGraph 的狀態機裡面：LangGraph Studio 可以追蹤每一次 revision、`recursion_limit` 統一管控所有迴圈深度、State 的欄位明確記錄了「已重試幾次」而不是散落在外部 Python 變數裡。

這個 pattern 在 LangGraph 官方文件裡有直接對應的範例。[Self-RAG tutorial](https://langchain-ai.github.io/langgraphjs/tutorials/rag/langgraph_self_rag/) 示範了完全一樣的結構：grader 節點評估 RAG 生成的答案，條件邊決定要走到 END 還是回到 retrieval 節點重查；[Reflection tutorial](https://langchain-ai.github.io/langgraphjs/tutorials/reflection/reflection/) 則是更通用的版本，LLM 觀察自己的輸出並評分，不夠好就走 conditional edge 重試。我們的 `rubric_trace_node → route_after_rubric → agent` 迴圈是同一套骨架，只是 grader 換成了確定性的 Tempo HTTP 查詢。

### 為什麼 k8s write guard 不進圖

`check_k8s_write` 是 **tool 執行前的 pre-flight gate**，它在 `execution.py` 的 gate 序列裡、在 ToolNode 呼叫之前觸發。這個時間點不在圖的節點邊界上，而是在 tool 函數的執行中間。

把它做成圖節點反而會破壞現有的 gate 序列設計——blast-radius gate、circuit breaker、rubric 這三道 gate 是有順序的，且它們作用的對象是「一個具體的 ActionRequest」，不是 agent 的完整答案。

所以 k8s write guard 正確的位置就是 `execution.py` 的 gate 3b，不在圖裡。

**K8s write guard** 在 `execution.py` 的 gate 序列裡：

![](https://cdn.hashnode.com/uploads/covers/6420f5cbbdbe7d697133d12a/1073ccc6-26f9-4b8d-b1a3-cd059100cfd1.png align="center")

```python
# execution.py —— gate 3b
rubric_ok, rubric_reason = await check_k8s_write(req.action, req.args, context)
if not rubric_ok and settings.actions_enabled:  # kill switch 關著時不擋
    ar_store_transition(req.request_id, Status.EXECUTING, Status.ABORTED,
                        outcome=f"rubric blocked: {rubric_reason}")
    return {"status": "aborted", "outcome": f"rubric blocked: {rubric_reason}"}
```

`settings.actions_enabled` 是全局 kill switch。當它是 `False`（預設），整個 execute 路徑都不會真正跑，所以 gate 3b 也沒必要阻擋——這讓既有測試（期待走到 `refused`）不受影響。

* * *

## 測試策略

16 個 unit test，分三層：

**底層：HTTP 行為**

用 `respx` mock Tempo API，測試四種情境：200 有 batches、200 空 batches、404、網路錯誤（均應回傳 `True` 避免誤擋）。

**中層：guard 邏輯**

Mock `_tempo_trace_exists`，不跑真實 HTTP。測試重點：

*   重複 ID 只查一次（dedup）
    
*   混合情境（一真一假）整體判為 fail
    
*   LLM exception → pass-through
    

**上層：gate 條件**

不啟動完整 execution pipeline，直接驗布林邏輯：

```python
assert not (not rubric_ok and ex.settings.actions_enabled)
# rubric 說 False，actions_enabled=False → gate 不觸發
```

這讓測試不依賴 k8s cluster 或真實 LLM 呼叫，跑起來快。

* * *

## Tradeoff 與我們沒做的事

**Token 成本**

每次答案生成後多一次 LLM 呼叫（k8s guard），或一次 HTTP 查詢（trace guard）。我們的 headless path 本來就有 confidence loop（低信心時重跑），rubric retry 是在這之外再疊一層。最壞情況：一次 alert 觸發 → 3 個 confidence loop × 1 rubric retry = 6 次 agent run。

這是可接受的，因為 k8s 操作本來就該謹慎，而 trace ID 查詢只是 HTTP GET，成本可忽略。

**Grader 本身的幻覺**

K8s write guard 用 LLM 來 judge，但 LLM 本身也可能判錯。我們的設計是「寧可漏判，不要誤擋」：exception 一律放行，block 條件設得相對明確（不模糊）。真正的安全線是 blast-radius policy 和 circuit breaker（純規則型），rubric 是額外的語意層，不是唯一防線。

**RubricMiddleware vs LangGraph 原生節點**

如果你用的是 LangChain deep agent，`RubricMiddleware` 封裝得很完整，rubric 的 retry loop 和 verdict 狀態都由框架管。但我們的架構是 LangGraph——在這裡，把 rubric 做成圖的節點才是慣用法，可以直接利用 State 傳遞 feedback、用 Conditional Edge 控制重試，`recursion_limit` 也統一管控所有迴圈深度。

起初我們用最快的方式驗證邏輯（`ainvoke()` 後面加 Python try-block），確認有效後再重構成圖內節點。這個順序是合理的：rubric 邏輯本身和「怎麼接進圖裡」是兩個獨立的問題，先把邏輯驗正確，再考慮架構整合。

* * *

程式碼在 [`rubric.py`](https://github.com/tedmax100/o11y-bench/blob/feat/aiops_agent/aiops-agent/service/app/rubric.py)。