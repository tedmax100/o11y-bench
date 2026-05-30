# AIOps Agent v3：從「問答式 RCA」到「alert 驅動的自動調查與 SOP 執行」

> v1/v2 把「人問、agent 答」這條打通了。v3 要回答的是另一個方向的問題：
> **alert 自己發生時，怎麼主動把 agent 叫起來、自動跑完 RCA、甚至安全地執行已知 runbook。**
> 同時補上兩個還沒做的訊號源與能力：k8s event、設計 alert。
>
> 前置閱讀：[aiops-agent-design.md](./aiops-agent-design.md)（pre-MVP 架構）、
> [aiops-agent-design-v2.md](./aiops-agent-design-v2.md)（context 控制 / wrapper cap）。

---

## 0. TL;DR

v1/v2 的成果是一個 **pull 模式**的助手：人在 plugin 裡問，agent 用 grafana-mcp 查、用 `git_version` 串 GitHub diff、把結果用 ```promql``` 圖表回。v3 要加四件事：

1. **k8s event 訊號源** —— 多掛一個 k8s MCP server，補上「是 code change 還是 pod 層面（OOM / 重啟 / rollout 失敗）」這半邊因果。**最低成本，先做。**
2. **設計 alert 的能力** —— 沿用「LLM 產 spec → plugin 渲染 → 人按按鈕才建立」的 human-in-the-loop pattern。
3. **Alert 驅動的自動 RCA（push 模式）** —— 新增 `POST /webhook/alert`，alert fire 時 Grafana 主動 POST 進來，agent headless 跑完一輪 RCA，把結論寫回 annotation / 通知管道。
4. **Runbook / SOP 執行** —— 分三層 graduate：連結 → 診斷自動化（唯讀）→ 自動 remediation（白名單 + approval + circuit breaker）。**最危險，最後做。**

核心論點：**做 push 模式會逼你把 [v2 §3](./aiops-agent-design-v2.md) 的 `planner → executor → finalizer` graph 真正補完——因為沒人盯著的 headless run，才是 budget guard 跟 structured findings 最值錢的場景。**

---

## 1. 整體能力地圖（v3 後的目標形態）

```plaintext
                          ┌──────────────────────────────────────┐
   PULL（人發起）          │              AIOps Agent Service        │
   plugin chat ───POST /chat──▶│                                    │
                          │   ┌────────────────────────────────┐   │
   PUSH（alert 發起）      │   │  LangGraph RCA graph (v2 §3)    │   │
   Grafana Alerting ──POST /webhook/alert──▶│ planner→executor→finalizer │
                          │   └──────────────┬─────────────────┘   │
                          │                  │ MCP / tools          │
                          └──────────────────┼──────────────────────┘
                ┌─────────────┬──────────────┼───────────────┬─────────────┐
                ▼             ▼              ▼               ▼             ▼
           grafana-mcp    k8s-mcp      github (REST)   alert provisioning  action
        Loki/Tempo/Prom   events/pods   compare/file   (create rule)       registry
                                                                         (runbook)
```

兩個入口（chat / webhook）**共用同一個 RCA graph**。差別只在：

| | PULL（chat） | PUSH（alert webhook） |
|---|---|---|
| 觸發者 | 人 | Grafana Alerting |
| 起手 context | 自然語言問題 | alert labels（service / version / severity 已知）|
| 「now」 | wall clock | alert 的 `startsAt` |
| 輸出 | SSE stream 回 plugin | 寫回 annotation / 通知，thread 留存 |
| 有人盯 | 有，可打斷 | **沒有** → 必須有 budget guard |
| 可不可以執行副作用 | 設計 alert 要按按鈕 | runbook 分層，預設只到「診斷」 |

---

## 2. k8s event 訊號源（先做，幾乎是接線）

[v1 §5](./aiops-agent-design.md) 早就點名 `kubernetes-mcp-server`，只是 MVP 沒接。架構**本來就支援**——`service/app/agent.py:292` 用的是 `MultiServerMCPClient`，多掛一個 server 即可：

```python
_mcp_client = MultiServerMCPClient({
    "grafana": {"url": settings.mcp_grafana_url, "transport": "streamable_http"},
    "k8s":     {"url": settings.mcp_k8s_url,     "transport": "streamable_http"},
})
```

要做的事：

1. `config.py` 加 `mcp_k8s_url`。
2. `schema_catalog.md` 加一段 k8s routing：哪類問題查 `events` / `pod status` / `rollout history`；namespace 固定 `demo`；只給 **read-only** 權限的 ServiceAccount（見 §6.3 認證）。
3. system prompt 的 routing 表（`agent.py:118` 那張）加一列：「pod 重啟 / OOM / rollout 卡住 → k8s events」。
4. `wrap.py` **不用動**——k8s 回傳通常小，且不是 LogQL/PromQL/TraceQL，wrapper 的 name 比對自然 pass-through。

**價值**：現在的因果鏈是 `metric → trace → log → github diff`。補 k8s 後，`git_version` boundary 旁邊若剛好有 `Killing` / `BackOff` / `FailedScheduling` event，agent 就能分辨「**code change** vs **基礎設施層**（OOMKilled、rollout 中途失敗、資源不足）」——這是 deploy correlation 缺的另一半。

### 2.1 踩雷點

- k8s MCP 的 tool 數量可能不少，全塞進 tool list 會稀釋 LLM 的選擇。只暴露 read 類（`get_events` / `list_pods` / `describe` / `rollout_history`），write 類（`delete_pod` / `scale` / `rollout_undo`）**不要**從這裡進——那些走 §5 的 action registry，有圍欄。
- k8s 沒有 `git_version` 這種天然 join key。對齊靠**時間 + pod label**（`app.kubernetes.io/name` 對 `service_name`）。catalog 要把這個對照寫清楚，否則 LLM 不知道 `payment-service` 對應哪個 deployment。

---

## 3. 設計 alert 的能力（沿用 chart 的 human-in-the-loop pattern）

現有一個好用的 pattern：LLM 在回答裡吐 ```promql``` block，plugin 自動畫成 panel（`agent.py:168-207`）。**設計 alert 就是同一招延伸一個 block 類型**：

1. agent 根據對話產出 alert rule spec，包在 ```` ```alert ```` block：

   ```yaml
   # ```alert
   title: payment decline rate > 5% (新版本)
   datasource: prometheus
   condition: |
     sum by (git_version) (rate(payment_charges_total{status="declined"}[5m]))
     /
     sum by (git_version) (rate(payment_charges_total[5m]))
     > 0.05
   for: 5m
   labels: { severity: warning, service_name: payment-service }
   annotations:
     summary: "payment-service decline 率異常，疑似版本問題"
     runbook_id: payment-bad-deploy        # ← 串到 §5 的 runbook
   ```

2. plugin 偵測到 ```alert``` block → 渲染成「**預覽卡 + [建立此 alert] 按鈕**」。
3. **按下去才**呼叫 Grafana provisioning API 真正寫入。

### 3.1 為什麼一定要 human-in-the-loop

alert 建立是有副作用的動作（會產生通知、可能 page 人）。跟 code 裡已有的 **intent gate / fail-closed**（`agent.py:222-280`）同一個哲學——**有副作用就要 gate**。LLM 只負責「產出正確的 spec」，按鈕只負責「人確認」，兩件事分開。

### 3.2 註記：跟 Grafana Sift 的關係

Grafana 自己有 **Sift**（自動化 investigation / check）。它跟我們的自動 RCA 重疊，但：

- Sift 的 check 是預設規則庫，**沒有 `git_version → code diff`** 這條（我們的差異化強項）。
- mcp-grafana 的 alerting tool 目前偏 **read**（list/get rules、contact points）；create alert rule 一般走 Grafana 的 **provisioning HTTP API**，不是 MCP tool。spec 由 LLM 產、由 plugin/service 呼叫 provisioning API 落地。

---

## 4. Alert 驅動的自動 RCA（push 模式核心）

### 4.1 怎麼「主動打到 agent」

Grafana Alerting 內建 **webhook contact point**——alert fire 時主動 POST 一包 JSON。所以：

1. agent service 加 `POST /webhook/alert`（`main.py` 現在只有 `/chat`）。
2. Grafana 建一個 webhook contact point 指向它。
3. notification policy 把要自動調查的 alert 路由過去。

webhook payload 帶的 `labels`（`service_name`、`git_version`、`severity`）**就是 RCA 的起手 context**——agent 本來就懂這個 schema（catalog 裡有），等於 alert 直接把「查哪個服務、哪個版本、哪個時間」都喂好了。

```
POST /webhook/alert
{
  "alerts": [{
    "labels": { "alertname": "...", "service_name": "payment-service",
                "git_version": "v2.5.0", "severity": "warning" },
    "annotations": { "summary": "...", "runbook_id": "payment-bad-deploy" },
    "startsAt": "2026-05-30T14:05:00Z",
    "values": { "B": 0.18 }
  }]
}
```

### 4.2 dedup / cooldown（必須有，否則 alert storm 會炸）

alert 會 flapping、會一次噴一批。**不能每包都 spawn 一個調查。**

- fingerprint = `hash(alertname + 關鍵 labels)`。
- 同一 fingerprint 在 cooldown 窗（例如 10 min）內只跑一次；後續的併進同一個調查 thread。
- 這個 fingerprint 同時當 **`thread_id`**（見 §4.4）。

### 4.3 為什麼這裡才是 v2 §3 graph 該落地的地方

互動 chat 用單一 `create_react_agent`（現況 `agent.py:320`）還行——有人盯、隨時能打斷。但**自動觸發的 headless run 沒人看**，所以 [v2 §3](./aiops-agent-design-v2.md) 設計的東西在這個場景才真正划算：

| v2 §3 元件 | 在 chat 模式 | 在 alert 模式 |
|---|---|---|
| **budget guard** | nice-to-have | **必須**——沒人看的 run，token 要硬上限 |
| **structured `Findings`** | 錦上添花 | **必須**——下游 runbook 要機器可讀的結論才能判斷 |
| **planner → executor → finalizer** | 線性 ReAct 也行 | 多假設並行、可回收，省 token 又快 |

**所以 v3 的隱含前置就是把 v2 §3 補完。** 不是因為 chat 需要，而是因為 push 模式沒它不安全。

### 4.4 輸出去哪（findings sink）+ thread 接續

headless 跑完不像 chat 有 SSE 對象，結論要主動推：

- **Grafana annotation**：打在相關 dashboard 的時間軸上（最貼近現場）。
- **通知管道**：Slack / Telegram（專案已有 telegram plugin）/ Grafana Incident。
- **thread 留存**：用 §4.2 的 fingerprint 當 `thread_id` 存進 checkpointer。

最後這點是體驗關鍵：on-call 收到通知、打開 plugin，會看到「**這條 alert 已經自動調查完**」，而且能在**同一個 thread** 接著追問（「那 user-service 有沒有受影響？」）。這直接接上現有的 checkpointer/thread 模型（`agent.py:324` 的 `MemorySaver`，production 換 Postgres——v2 §7 step 6 已列）。

### 4.5 踩雷點

- **「now」要用 `startsAt`，不是 wall clock**。現在 system prompt（`agent.py:54-73`）用真實時鐘；webhook 觸發時要把 scenario time 覆寫成 alert 的 `startsAt`，否則查錯時間窗。
- webhook endpoint 要**驗證來源**（共享 secret / mTLS），不然任何人都能偽造 alert 觸發調查（DoS + 誤導）。fail-closed：驗不過直接丟。
- 自動 run 也要過 §intent gate 的精神——只是這裡 gate 的是「這個 alert 值不值得花一輪 LLM」（低 severity / 已知 noisy alert 可以直接 skip）。

---

## 5. Runbook / SOP 執行（分三層 graduate，別一步到位）

這是整條鏈**風險最高**的一段。原則：**有副作用就 gate，能逆的才談自動**（延續 intent gate / `run_analysis` sandbox 的一貫立場）。

### 5.1 Runbook 表示法（structured，非自由文字）

```yaml
id: payment-bad-deploy
trigger:                       # 什麼 alert 匹配這篇
  alertname: payment-decline-rate-high
  labels: { service_name: payment-service }
diagnostics:                   # Tier 1：唯讀，自動跑來「確認」前提
  - desc: 確認 decline 集中在新版本
    query: |
      sum by (git_version, reason) (rate(payment_charges_total{status="declined"}[5m]))
    expect: 單一 git_version 佔比 > 80%
  - desc: 比對部署 diff
    action: github_compare
    args: { repo: tedmax100/o11y-bench, base: "{prev_version}", head: "{git_version}" }
remediation:                   # Tier 2：有副作用，預設只「提議」不「執行」
  - desc: 回滾到前一版
    action: k8s.rollout_undo
    args: { deployment: payment-service, namespace: demo }
    reversible: true
    requires_approval: true
```

`diagnostics` 段是唯讀 query（agent 自動跑來確認 runbook 前提）；`remediation` 段才是動作。

### 5.2 三層 graduate

**Tier 0 — 連結（先做這個）**
alert annotation 帶 `runbook_id`（見 §3 的 alert spec）。agent 自動 RCA 完，撈對應 runbook，把**已填好 incident 參數的步驟**貼進輸出。純資訊、零副作用。

**Tier 1 — 診斷自動化（安全）**
agent 自動執行 `diagnostics` 段——全是唯讀 query，等於「自動驗證 runbook 的前置判斷」，把 `expect` 的命中與否寫進 findings。跑到 `remediation` 就**停**，等人。

**Tier 2 — 自動 remediation（最後才碰，嚴格圍欄）**
只開放給通過全部圍欄的動作：

- **白名單 + 參數化 + 可逆**：`rollout_undo` / scale up / flip flag back 可以；不可逆的（刪資料、drop）**永遠** `requires_approval`，agent 不自動做。
- **dry-run + blast-radius 檢查**：執行前先模擬，確認影響範圍。
- **approval gate**：confidence 不足或動作非可逆 → 退回給人按准（可走 telegram push 給 on-call）。
- **circuit breaker**：同一服務剛被動過手就不再自動動（防 agent 跟自己/HPA 打架、防 flapping 觸發連續操作）。
- **audit log**：誰（agent）、在什麼證據（findings）下、做了什麼動作、結果如何——全程可回溯。

### 5.3 action registry

write 類動作**不從 k8s MCP 直接暴露**（§2.1 已說 k8s MCP 只給 read）。另設一個 **action registry**：每個動作是一個 typed、白名單、帶 `reversible` / `requires_approval` 標記的函式，套上 §5.2 的圍欄。這把「LLM 直接 `kubectl delete`」的路徑從架構上關掉——agent 只能呼叫 registry 裡登記過的動作。

---

## 6. 認證與安全（push + write 把風險面放大了）

### 6.1 認證鏈（比 v1 §7.1 多了兩條入口）

```
[PULL]  Grafana session → plugin → agent → MCP → backends
[PUSH]  Grafana Alerting → (shared secret) → /webhook/alert → agent → MCP → backends
[ACT]   agent → action registry → (read-only SA for k8s read) / (privileged SA, gated) for write
```

關鍵決定：

- **webhook 入口**：共享 secret / mTLS，fail-closed。
- **k8s read**：獨立 read-only ServiceAccount。
- **k8s / 其他 write（Tier 2）**：獨立的 privileged SA，但**只能透過 action registry**用、且每次過圍欄、留 audit。read 與 write 用**不同 SA**，降低誤刪/越權。

### 6.2 為什麼 write 權限要跟 read 拆開

自動化最怕的不是「查錯」，是「動錯」。read SA 漏了頂多看到不該看的；write SA 漏了會改壞線上。**最小權限 + 動作白名單 + 不可逆需批准**，三道一起上。

---

## 7. Migration 順序（按投入產出比 + 風險）

| # | 動作 | 影響範圍 | 為什麼這個順序 |
|---|------|----------|----------------|
| 1 | 接 k8s MCP（read-only）+ catalog 一段 | `agent.py` / `config.py` / `schema_catalog.md` | 最低成本，立刻補強因果鏈 |
| 2 | **落地 v2 §3 graph**（planner/executor/finalizer + budget + Findings）| `agent.py` 大改 | push 模式的前置；headless 沒它不安全 |
| 3 | `POST /webhook/alert` + dedup/cooldown + 來源驗證 | `main.py` 新 endpoint | 打通 alert → 自動 RCA |
| 4 | findings sink（annotation / 通知）+ thread = fingerprint | service 層 | 自動調查要看得到、能接續 |
| 5 | Tier 0/1 runbook（連結 + 診斷自動化，唯讀）| runbook YAML + executor | 拿到自動化價值、零副作用 |
| 6 | 設計 alert 能力（```alert``` block + plugin 按鈕）| plugin + provisioning API | 獨立功能，與上面解耦，可隨時插入 |
| 7 | action registry + Tier 2 自動 remediation（圍欄）| 新模組 + 獨立 SA | 風險最高，前面都穩了再碰 |

step 6 跟主線解耦，急著要可以提前；step 7 一定壓在最後。

---

## 8. 未決問題

- **runbook 庫誰維護、放哪？** 跟 alert rule 一起 provision（IaC），還是另開一個 registry？這影響 `runbook_id` 怎麼解析。
- **runbook 匹配要做到多模糊？** 先做 annotation 明確帶 `runbook_id`（精確）。要不要進一步「用 incident signature 語意匹配 runbook 庫」（模糊）？模糊匹配的誤配風險在 Tier 2 會放大。
- **自動 RCA 的成本上限怎麼定？** alert storm 時就算有 dedup，仍可能同時多個不同 fingerprint。要不要全域併發上限 / 每日預算？
- **Tier 2 的「confidence」怎麼量化？** 自動 remediation 的 approval gate 需要一個 confidence 門檻——是 findings 命中 `expect` 的比例，還是另跑一個 LLM judge？這直接決定「自動 vs 要人批」的分界。
- **demo 場景**：要不要在 demo-services 種一個「alert fire → 自動 rollback v2.5.0→v2.4.1」的完整 Tier 2 demo？這是最有說服力的 end-to-end，但也是最需要圍欄驗證的。
```