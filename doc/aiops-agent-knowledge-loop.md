# AIOps Agent — 知識管理與自我校正調查迴路

> 前置閱讀：[aiops-agent-design-v3.md](./aiops-agent-design-v3.md)（push 模式 / runbook 執行層）。
>
> 本文回答兩個獨立但相關的問題：
> 1. **Knowledge Management**：agent 從事故中學到的東西去哪裡？runbook / SOP 怎麼隨時間長大而不腐化？
> 2. **Loop Engineering**：agent 調查方向偏了怎麼辦？他怎麼知道自己偏了？信心不足時能不能自己繞回來？

---

## 0. TL;DR

| 問題 | 現況 | 這份文件加的 |
|---|---|---|
| 過去 incident 對當下調查沒幫助 | investigations 存 SQLite 但從不被讀 | **閉環一**：調查開始前注入過去類似案例 |
| 正確解法消失在 log 裡 | label=correct 但不產生任何知識 | **閉環二**：合成 draft runbook → Git PR |
| Runbook 步驟失敗但沒人知道 | verify_failed 只記 audit，沒有反饋 | **閉環三**：runbook_feedback table + 週報 |
| Agent 方向偏了自己不知道 | 沒有任何 mid-investigation 校正 | **假說樹 + 信心閘門 + 自我 loop** |
| Human gate 在哪裡 | 只有 autonomy/governance 一道關卡 | 明確定義哪裡必須人介入、哪裡不必 |

---

## 1. 知識管理三閉環

### 閉環一 — Past Incident Context Injection

**目的**：讓 agent 在開始調查前，知道同樣的問題上週怎麼解。

**觸發點**：`webhook.py` 或 chat 入口，在第一個 turn 組裝 system context 時。

**機制**（零新依賴，純 SQLite）：

```python
def past_incident_context(service: str, alertname: str, limit: int = 5) -> str:
    """從 investigations table 撈出最近 N 筆同 service + alertname 且 correct=True 的記錄，
    組成 markdown block 注入 turn_messages。"""
    rows = store.inv_query_similar(service=service, alertname=alertname, limit=limit)
    if not rows:
        return ""
    lines = ["## Past similar incidents (use as reference, not as answer)"]
    for r in rows:
        lines.append(f"- [{r['ts'][:10]}] {r['summary']} "
                     f"(confidence {r['confidence']:.0%}, correct={r['correct']})")
        if r.get("hypothesis"):
            lines.append(f"  hypothesis: {r['hypothesis']}")
        if r.get("suspected_version"):
            lines.append(f"  culprit version: {r['suspected_version']}")
    lines.append("\nIf the current evidence contradicts these past cases, trust the evidence.")
    return "\n".join(lines)
```

需要在 `store.py` 加一個 `inv_query_similar`：

```sql
SELECT payload FROM investigations
WHERE json_extract(payload, '$.service') = ?
  AND json_extract(payload, '$.alertname') = ?
  AND json_extract(payload, '$.correct') = 1
ORDER BY ts DESC LIMIT ?
```

**注意事項**：
- 只注入 `correct=True` 的記錄，未標記或錯誤的不注入（避免強化錯誤模式）。
- 加一行「如果當下證據與過去不符，相信當下證據」，防止 agent anchoring 在過去。
- 若沒有相似案例，不注入任何東西（不要注入 "no similar cases found"，這是噪音）。

---

### 閉環二 — Investigation → Draft Runbook 合成

**目的**：正確解決了一個沒有 runbook 的 incident，這個知識不應該消失。

**觸發條件**：`label_run(run_id, correct=True)` 被呼叫，且該 `run_id` 對應的 investigation 沒有命中任何 runbook（`match_runbook` 回傳 None）。

**流程**：

```
label_run(correct=True)
    ↓
check: investigation.alert → match_runbook() == None?
    ↓ Yes
synthesize_draft_runbook(investigation)  ← LLM 輔助 or template-based
    ↓
write runbooks/drafts/<alertname>-<service>-<date>.yaml
    ↓
GitHub PR  (or manual: 存檔 + Slack 通知 on-call)
    ↓
Human review → merge to runbooks/ → 下次 match_runbook 可撈到
```

**Draft runbook 的資料來源**（你們已全部存在 investigations table）：

| 欄位 | 用途 |
|---|---|
| `alertname` | trigger.alertname |
| `service` | trigger.labels.service_name |
| `hypothesis` | title / 描述 |
| `suspected_version` | 填入 diagnostics 的 git_version 參數 |
| `decisions[].action` | remediation 步驟的 action |
| `decisions[].reason` | 每個 remediation step 的 desc |
| `confidence` | 放進 runbook metadata 供人參考 |

**Template-based 合成**（不需要 LLM，先做這個）：

```python
def synthesize_draft_runbook(inv: InvestigationRecord) -> dict:
    return {
        "id": f"draft-{inv.alertname}-{inv.service}",
        "title": f"[DRAFT] {inv.hypothesis[:80]}",
        "_meta": {
            "source_investigation": inv.fp,
            "confidence": inv.confidence,
            "generated_ts": inv.ts,
            "status": "draft — requires human review before activation",
        },
        "trigger": {
            "alertname": inv.alertname,
            "labels": {"service_name": inv.service} if inv.service else {},
        },
        "diagnostics": [],   # 人工填充 — agent 的 tool call history 可參考
        "remediation": [
            {
                "desc": d.reason,
                "action": d.action,
                "reversible": None,          # 人工確認
                "requires_approval": True,   # draft 預設要人批
            }
            for d in inv.decisions if d.action
        ],
    }
```

**為什麼不用 LLM 自動生成完整 runbook？**
因為 runbook 裡的 `diagnostics.check`（deterministic 驗證條件）和 `rollback` contract 需要人確認語意，LLM 生成的這兩部分容易產生看起來合理但實際不對的內容。Template 生成骨架、人補充細節，是更安全的分工。

---

### 閉環三 — Execution Outcome → Runbook Feedback

**目的**：runbook step 失敗或 rollback 發生時，有地方記錄，並定期產出「哪些 SOP 需要重新審查」的報告。

**新增 `runbook_feedback` table**（在 `store.py` schema 加入）：

```sql
CREATE TABLE IF NOT EXISTS runbook_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    runbook_id  TEXT NOT NULL,
    step_desc   TEXT NOT NULL,
    outcome     TEXT NOT NULL,   -- verify_failed / rollback / rollback_failed / ok
    request_id  TEXT NOT NULL DEFAULT '',
    fp          TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '{}'  -- json
);
CREATE INDEX IF NOT EXISTS idx_rb_feedback_runbook ON runbook_feedback(runbook_id);
```

**寫入點**（`execution.py` 的 verify 結果判斷後）：

```python
# verify_failed 或 rollback 時
store.rb_feedback_insert(
    runbook_id=request.runbook_id,
    step_desc=step.desc,
    outcome="verify_failed",   # or "rollback", "rollback_failed"
    request_id=request.request_id,
    fp=request.fp,
    detail={"verify_result": ..., "action": ...},
)
```

**SOP 腐化偵測週報**（可排程或手動跑）：

```python
def runbook_health_report() -> str:
    """找出過去 30 天 verify_failed 率 > 30% 的 runbook，或有過 rollback_failed 的。"""
    ...
```

**三種腐化訊號**：

| 訊號 | 閾值 | 建議動作 |
|---|---|---|
| `verify_failed` 率上升 | > 30% in 30 days | 標記 runbook 為 needs-review |
| `rollback_failed` | 任何一次 | 立即 flag，暫停該 runbook 自動執行 |
| 同 service 的 CE 分數惡化 | ECE 從 < 0.1 → > 0.2 | 提示該 service 的 runbook 可能也過時 |

---

## 2. Runbook 生命週期

### 2.1 Git 作為唯一真實來源

```
runbooks/
├── active/                  ← match_runbook 從這裡載入
│   └── payment-bad-deploy.yaml
├── drafts/                  ← agent 合成的 draft，等人 review
│   └── draft-payment-decline-rate-high-payment-service-2026-01-15.yaml
└── archived/                ← 被取代的舊版本，保留歷史
    └── payment-bad-deploy-v1.yaml
```

好處：
- Runbook 的每一次修改都有 diff 和 commit message，知道為什麼改。
- PR review 就是 SOP review，不需要另建流程。
- CI 可以跑 `runbook.load_runbooks()` 驗 YAML schema，壞掉的 runbook 不會進 main。

### 2.2 Draft → Active 的 promotion 條件

人在 review draft PR 時確認的事：

1. `trigger` 條件夠精確（不會誤 match）
2. `diagnostics` 的 `check` 條件在這個 service 有意義
3. `remediation` 的每個步驟有 `rollback` contract
4. `verify` 條件能真正判斷 remediation 是否成功
5. 決定這個 runbook 是 `autonomy: auto` 還是 `autonomy: propose`

### 2.3 版本化策略

- Runbook 加 `version` 欄位（semver）。
- `action_requests` 的 `runbook_id` 欄位已存在，允許事後 audit 某次執行用的是哪個版本。
- 重大修改（remediation 步驟改變）→ bump minor。新增 diagnostics check → patch。

---

## 3. Human in the Loop — 哪裡必要、哪裡不必要

### 3.1 必要的 human gate（不能省）

| 關卡 | 為什麼必要 |
|---|---|
| Draft runbook → Active | Agent 生成的骨架語意可能錯，rollback contract 需要人確認 |
| `autonomy: propose` 的 action 執行前 | 不可逆操作，blast radius 大 |
| `confidence < threshold` 的 RCA 結論 | 低信心代表 agent 自己也不確定，不該自動執行後續動作 |
| `error_dimension` 標記（wrong label） | 人工校正錯誤維度，是 CE 訓練資料的核心 |
| 新 alert type 第一次出現 | 沒有 past incidents，沒有 runbook，agent 的推論完全是 cold start |

### 3.2 可以自動化、不需要人的判斷

| 行為 | 為什麼可以自動 |
|---|---|
| Past incident context 注入 | 讀取，無副作用 |
| Tier 1 diagnostics（read-only tool 執行）| 唯讀，最壞結果是 skip |
| `autonomy: auto` + `reversible: true` 的操作 | 有 rollback contract，circuit breaker 保護 |
| CE score 計算 | 純數學，無決策 |
| Runbook feedback 週報 | 觀察，不決策 |

### 3.3 設計原則

**Human gate 的成本**：每道 gate 都是延遲。凌晨三點的事故，gate 愈多，MTTR 愈長。

**原則一：gate 要有接收者。** 沒人在看的 approval request 等於沒有 gate。設計 gate 時同時設計通知管道（Telegram / PagerDuty / Slack）和 timeout（超過 N 分鐘未批准 → 降級為 propose only）。

**原則二：gate 的輸入要夠好。** 人批准的品質取決於他看到的資訊。`action_requests` 的 `blast_radius` 和 `runbook_id` 就是為此設計的——讓人在點擊批准前知道「影響範圍是什麼」「依據哪個 SOP」。

**原則三：gate 不是終點。** 批准後的 verify → rollback 自動迴路仍然要跑。Human gate 只保證「開始前」是對的，不保證「執行後」是對的。

---

## 4. Loop Engineering — Agent 自我驗證與方向校正

這是這份文件最核心的問題：**agent 做 RCA 時方向偏了，他怎麼知道？知道後怎麼辦？**

### 4.1 方向偏差的來源

RCA 調查是一個有方向的搜尋過程。偏差通常來自：

1. **錨定（Anchoring）**：runbook match 了，agent 沿著 runbook 的方向走，但這次根本原因不在 runbook 涵蓋的範疇。
2. **確認偏誤（Confirmation bias）**：第一個 tool call 有結果，agent 只繼續找支持該假說的證據，沒有嘗試否定它。
3. **訊號缺失（Signal blindspot）**：agent 只看了 metrics，沒看 k8s events，漏掉了 OOMKilled 這個真正原因。
4. **假 positive 訊號**：某個 metric 剛好在事故窗口異常，但其實是不相關的，agent 卻把它當成根本原因。

### 4.2 假說樹（Hypothesis Tree）

**現況**：agent 的 planner 只產生一個調查方向。

**改法**：planner 明確生成 2–3 個互斥的競爭假說，每個假說獨立蒐集支持/反駁證據，最後比較。

```
Alert: payment-decline-rate-high (payment-service)
    │
    ├── H1: 近期部署引入的 code regression
    │       支持：git_version 在事故前 30min 有變化
    │       反駁：其他 service 同期也有 decline？→ 不像 code 問題
    │       工具：github_compare, query_prometheus(by version)
    │
    ├── H2: 上游依賴（user-service / fraud-service）降級
    │       支持：payment 的 upstream 呼叫 latency 上升
    │       反駁：payment 的 internal error 是不是也高？
    │       工具：query_tempo_traces, query_prometheus(upstream latency)
    │
    └── H3: 基礎設施層問題（OOM / pod 重啟 / 節點壓力）
            支持：k8s events 有 OOMKilled / Evicted
            反駁：pod healthy，available_replicas == desired
            工具：k8s_pod_status, k8s_events
```

在 system prompt 層面，要求 agent 在 planner 輸出時明確列出假說：

```
Before you start investigating, list 2-3 mutually exclusive hypotheses ranked by prior probability.
For each hypothesis, state:
- what evidence would CONFIRM it
- what evidence would REFUTE it
Then investigate the highest-priority one first, but actively seek refuting evidence.
```

### 4.3 信心評估機制

**信心來自哪裡**（現況：只是 LLM 自己說的一個數字）：

真正有意義的信心應該來自三個維度：

| 維度 | 好的信號 | 差的信號 |
|---|---|---|
| **訊號多樣性** | metrics + logs + traces + k8s 全都指向同一個地方 | 只有 metrics，或只有 logs |
| **反駁證據** | 主動嘗試否定假說，找不到反駁 | 沒有嘗試否定，只有確認性查詢 |
| **假說收斂** | 多個獨立假說最終都指向同一個 root cause | 假說分散，無法排除 |

在 finalizer 的 prompt 中要求 agent 明確評估這三個維度，而不是直接輸出一個黑箱數字：

```
Before outputting confidence, answer:
1. How many independent signal types confirmed the hypothesis? (metrics/logs/traces/k8s)
2. Did you explicitly try to find evidence that CONTRADICTS the hypothesis? What was the result?
3. Are there competing hypotheses you could NOT rule out?

If the answer to (2) is "no", confidence must be ≤ 0.5.
If (3) has surviving alternatives, confidence must be ≤ 0.6.
```

### 4.4 低信心時的自我 Loop

**現況**：agent 輸出低信心結論，然後 governance 拒絕自動執行，結束。人收到一個「不確定」的結果。

**應有的行為**：低信心觸發自我 loop，agent 主動換方向再試一次。

```
finalizer 輸出 confidence < 0.6
    │
    ├── 還有未嘗試的假說？
    │       Yes → 切換到下一個假說，重新執行 executor
    │       No  → 進入 4.5 的 divergence probe
    │
    └── 已嘗試所有假說，信心仍低？
            → 輸出 structured uncertainty（見下方）
            → 觸發 human escalation
```

**在 LangGraph 的實作方式**：

在 graph 的 finalizer node 加一個 conditional edge：

```python
def _should_loop(state: AgentState) -> str:
    findings = state.get("findings")
    if findings is None:
        return "end"
    if findings.confidence < settings.confidence_loop_threshold:   # e.g. 0.6
        untried = state.get("untried_hypotheses", [])
        if untried:
            return "retry_with_next_hypothesis"
        return "escalate"
    return "end"

graph.add_conditional_edges("finalizer", _should_loop, {
    "retry_with_next_hypothesis": "planner",
    "escalate": "human_escalation",
    "end": END,
})
```

**Loop 的安全圍欄**：

- 最多 loop N 次（`settings.max_hypothesis_loops = 3`），防止無限 retry。
- 每次 loop 的 tool call budget 獨立計算（不能讓第二個假說吃掉第一個假說的 budget）。
- Loop 歷史（嘗試過的假說和結果）要保留在 state 中，讓 finalizer 知道「已經試過什麼了」。

### 4.5 死路偵測與 Pivot（Adversarial Probe）

當所有假說都試過但信心仍低，agent 進入「試圖否定所有已知方向」的 adversarial probe：

**Probe 策略**：

1. **範圍擴張**：不只看 payment-service，看整個 call graph（upstream + downstream 全部）。是不是系統性問題而不是單一服務問題？

2. **時間擴張**：事故窗口前後各看兩倍時間。有沒有更早的預兆被忽略？有沒有已經在恢復的跡象？

3. **維度切換**：如果主要查的是 code，現在去查 infra。如果主要查的是 service 層，去查 network 或 DNS。

4. **比較健康基線**：查詢同一個 service 在上週同一時段的 metric，確認「現在的值」是真的異常，還是這個 service 本來就這樣。

在 prompt 層：

```
All hypotheses have been investigated with confidence below threshold.
Now run an adversarial probe:
1. Check all upstream and downstream services — is this a systemic issue?
2. Compare current metrics to the same window last week — is this truly anomalous?
3. Switch signal domain: if you focused on metrics, now check k8s events and logs.
4. Look for evidence that the issue is ALREADY RECOVERING — which changes the urgency.
Report what you find, even if it doesn't point to a clear root cause.
```

### 4.6 Structured Uncertainty — 信心不足時的輸出格式

與其輸出一個不確定的結論，不如輸出**結構化的不確定性**，讓 human escalation 更有效率：

```json
{
  "confidence": 0.42,
  "summary": "payment decline rate elevated, root cause unclear",
  "hypothesis_status": [
    {
      "hypothesis": "code regression in payment-service v2.5.0",
      "supporting_evidence": ["decline rate correlated with deploy timestamp"],
      "refuting_evidence": ["order-service also shows minor decline, suggesting systemic"],
      "confidence": 0.45
    },
    {
      "hypothesis": "upstream user-service degradation",
      "supporting_evidence": ["user-service p99 latency +200ms"],
      "refuting_evidence": ["payment internal errors also high, not just upstream timeouts"],
      "confidence": 0.35
    }
  ],
  "missing_signals": ["k8s events for user-service", "fraud-service health"],
  "recommended_human_action": "Check k8s events for user-service and fraud-service. Neither hypothesis is conclusively ruled out."
}
```

這個格式直接告訴 on-call：
- 哪個假說比較可能（但都不確定）
- 還缺哪些資訊
- 下一步要做什麼

比「我不確定，請人工介入」要有用得多。

---

## 5. 整體流程圖

```
Alert 進來
    │
    ├── [閉環一] past_incident_context(service, alertname) → 注入 system prompt
    │
    ▼
Planner
    │
    ├── match_runbook() → 有 → 注入 runbook guidance + 執行 Tier 1 diagnostics
    │                  → 無 → cold start，依照 hypothesis tree 開始
    │
    ├── 生成 2-3 個競爭假說（H1, H2, H3）
    │
    ▼
Executor（per hypothesis）
    │
    ├── 每個假說：主動蒐集支持 + 反駁證據
    │
    ▼
Finalizer
    │
    ├── 評估信心（多樣性 + 反駁嘗試 + 假說收斂）
    │
    ├── confidence >= 0.7 → 輸出 Findings → governance → 執行 / propose
    │
    ├── 0.5 ≤ confidence < 0.7 → 還有未試的假說？
    │       Yes → loop 回 Planner（最多 3 次）
    │       No  → Adversarial Probe
    │
    └── confidence < 0.5 → Structured Uncertainty → Human Escalation
            │
            └── 人回覆正確方向 → 注入 correction_hint → 重新調查
                    │
                    └── [閉環二] correct=True + no runbook → Draft Runbook 合成
                                correct=False → error_dimension → CE 訓練資料

執行後
    │
    ├── verify pass → [閉環三] runbook_feedback(outcome=ok)
    └── verify fail / rollback → [閉環三] runbook_feedback(outcome=verify_failed)
```

---

## 6. 實作優先順序

| 優先 | 項目 | 難度 | 收益 |
|---|---|---|---|
| **P0** | 閉環一：past incident context injection | 低（SQL + prompt） | 立即，零依賴 |
| **P0** | Hypothesis tree prompt（2-3 假說） | 低（prompt engineering） | 立即降低錨定偏誤 |
| **P1** | 信心評估 prompt 改造（多樣性 + 反駁） | 低（prompt） | 讓 confidence 數字有意義 |
| **P1** | LangGraph conditional loop（低信心 → retry） | 中（graph 邊修改） | 自動換方向，不需人介入 |
| **P2** | Structured Uncertainty 輸出格式 | 中（Findings schema 擴展） | 人工 escalation 更有效率 |
| **P2** | `runbook_feedback` table + 週報 | 低（一張 table + query） | 防止 SOP 腐化 |
| **P3** | Draft runbook 合成 + Git PR | 中（synthesize + GitHub API） | 最大知識沉澱價值 |

---

## 7. 未決問題

1. **Loop budget 怎麼分配？** 每次 loop 是否有獨立的 token/tool-call budget，還是共用一個總 budget？共用的話第二個假說可能根本沒錢查。

2. **Hypothesis tree 的深度由誰決定？** 現在設計是 LLM 在 planner 時自己生成假說，但這個列表本身可能就有偏差（anchoring 在 LLM 的訓練偏見）。需要考慮是否從 runbook triggers 反推候選假說，作為 prior。

3. **Adversarial probe 的停損點？** Probe 本身也可能跑偏。目前設計是固定策略（範圍擴張 + 時間擴張 + 維度切換），但某些場景下這些都沒用。

4. **Calibration 對 hypothesis-level confidence 有意義嗎？** 現在的 ECE/Brier 是對最終 Findings confidence 算的。如果加了 hypothesis tree，每個假說也有 confidence，需要考慮是對最終 confidence 校準，還是對每個假說分別校準。
