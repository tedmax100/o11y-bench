# aiops-agent × ARE 精神對照與落差分析

對照基準：[`agent reliability engineerin.md`](./agent%20reliability%20engineerin.md)（Agentic Reliability Engineering, ARE 核心哲學）
受測對象：`aiops-agent/service/app/`（`agent.py` / `webhook.py` / `capability.py` / `config.py` / `tools/`）
撰寫日期：2026-06-15

---

## 一句話結論

aiops-agent 忠實實現了 ARE 的 **訊號平面 + 推論平面 與安全紀律**（尤其是幻覺防禦、有界自主、可解釋性、信心分數），而且因為刻意**全唯讀**，「推論平面的產出永遠只是提案」這條鐵律是**結構性保證**的。

但它停在 DRAL 循環的 **Detect → Reason**，沒有 **Act / Governance（運行時策略）/ Learn**，也沒有 **校準誤差（CE）** 的量測與回饋。它是一個紀律良好的「調查/RCA 代理」，還不是完整的「韌性循環系統」。

覆蓋度速覽：

| 維度 | 狀態 |
|---|---|
| DRAL：Detect | ✅ 有 |
| DRAL：Reason | ✅ 有（且做得好） |
| DRAL：Act | ◾ 產生 propose-only 提案（`actions.py` registry + `governance.py` 閘）；執行 kill-switch 關閉、未接實作 |
| DRAL：Learn | ❌ 無 |
| 平面：Signal | ✅ 有 |
| 平面：Reasoning | ✅ 有（提案鐵律結構性保證） |
| 平面：Governance（運行時） | ✅ 運行時策略閘（`governance.py`：confidence × 校準 → AUTO/PROPOSE/ESCALATE，CE 升高即收緊自主權）；範圍守門另由 intent gate |
| 平面：Execution | ◾ Action Contract 形狀已備（`app/runbook.py` Tier 0/1，唯讀診斷自動執行）；首個 human-gated 副作用已落地（`app/alerts.py` 建 alert，propose→人類按鈕→寫入）；Tier 2 自主 remediation 尚未做 |
| 紀律：可解釋軌跡 | ✅ 有 |
| 紀律：信心分數 | ✅ 有（輸出層面） |
| 紀律：幻覺傳播防禦 | ✅ 有（亮點） |
| 紀律：校準誤差 CE | ◾ 已可量測（`app/calibration.py`，ECE/MCE/Brier）；尚未回饋去調節自主權 |
| 工作流：Incident Response | ◾ 只有「調查」半段 |
| 工作流：Decision-Aware Delivery | ❌ 無 |
| 工作流：Autonomous Chaos | ❌ 無 |
| 工作流：Pre-Incident Intelligence | ❌ 無 |

---

## 二、已滿足的部分（含程式碼證據）

### Detect（偵測）

- Grafana Alerting POST → `webhook.handle_alert`：以 `fingerprint(labels)` 去重、`_in_cooldown` 折疊告警風暴、每個獨立告警起一個背景 headless RCA，HTTP 立即回應避免 webhook timeout。
  - 證據：`webhook.py:32`（fingerprint）、`webhook.py:46`（cooldown）、`webhook.py:105`（handle_alert）

### Reason（推論）

- 強制「先假說再查詢」：system prompt「**State a hypothesis before each query**」。
- headless 路徑帶明確 RCA 方法（`_RCA_PLAYBOOK`）：先用真實 counter/histogram 名稱依 `git_version` × `reason` 拆解 → deploy correlation → 用一條代表性 trace 佐證 → 下結論。
  - 證據：`agent.py:113`（Workflow）、`agent.py:686`（`_RCA_PLAYBOOK`）

### 推論平面只產出「提案」，絕不直接行動（鐵律）

- **天然滿足**：`TOOLS` 全是唯讀的 query/discover/github，沒有任何寫入或變更系統狀態的工具。推論的產出是 prose answer + 結構化 `Findings`，不觸發任何行動。
  - 證據：`agent.py:443`（`TOOLS`）、`agent.py:591`（`Findings`）

### 可解釋推論軌跡（Explainability）

- 強制在最終答案 cite 跑過的 exact query（promql/logql/traceql fenced block，plugin 直接 render 成 panel）、cite 真實 trace id。
- 串流 `tool_start` / `tool_end` 事件給 UI，使用者看得到代理在查什麼。
  - 證據：`agent.py:190`（Panels 規則）、`agent.py:939`（tool 事件串流）

### 信心分數（Confidence）

- `Findings.confidence`（0.0–1.0），且 prompt 明確要求 inconclusive 時給 low confidence、不可捏造證據。
- headless 結論的 confidence 會寫進 Grafana annotation 文字。
  - 證據：`agent.py:596`（confidence 欄位）、`agent.py:608`（`_FINDINGS_PROMPT`）、`webhook.py:63`

### 幻覺傳播防禦（本實作的亮點）

- **live capability snapshot**：查詢前注入該服務「剛剛從 datastore 讀到的」真實 metrics/spans/log fields，並指示「**Trust this over the schema catalog**」——以即時遙測壓過可能 drift 的靜態 catalog。
- **discover-before-query**：catalog 沒列到就呼叫 `discover_*` 取真實名稱，禁止用訓練資料硬猜。
- **identical-retry guard**：同一輪內重複的 (name, args) 查詢被短路，回傳「換 selector 或先 discover」的指令，防止小模型把錯誤查詢重打到耗盡 budget。
- 「**don't invent git_version, read it from a result you already have**」——禁止把未經當下交叉驗證的值當證據。
- intent gate **fail-closed**：分類器出錯就拒絕，不讓未分類訊息溜進工具。
  - 證據：`capability.py:204`（snapshot「Trust this over the catalog」）、`agent.py:162`（anti-patterns）、`agent.py:507`（identical-retry guard）、`agent.py:294`（git_version 規則）、`agent.py:846`（fail-closed）

### 邊界內運作（Bounded Autonomy）

- 硬性 per-turn tool-call budget，由 graph 在 `route_after_agent` 強制，超過就路由到 `force_answer`（不綁工具，逼出文字答案），headless 有自己的 budget——「沒有人在看的代理不能無限迴圈」。
  - 證據：`agent.py:553`（route）、`agent.py:537`（force_answer）、`config.py:22`（`tool_call_budget`）、`config.py:35`（`webhook_tool_call_budget`）

### 人類在迴路之上（Above the loop）

- 代理從不行動；人類透過 panel / Grafana annotation 看到帶信心的結論後自行決策——符合 ARE 把人類角色從「迴路內救火員」提升到「迴路之上設計意圖與風險」。

---

## 三、未滿足的部分（離完整 ARE 最遠之處）

### 1. Act / 執行平面 完全不存在

哲學文件要求：低風險遏制操作（如限流）在政策允許下自主執行、高風險升級人類；過程需符合「行動合約（Action Contracts）」，具備確定性與可逆性。

現況：**沒有任何 action contract、沒有任何可逆操作、沒有執行平面**。代理停在「給結論」，不「採取旨在恢復穩定的行動」。「選擇不行動」目前不是一個深思熟慮的選項，而是唯一選項。

### 2. Governance 平面（運行時策略）缺席

哲學文件要求：在毫秒級內，依當下策略、錯誤預算與信心水準，決定核准提案或升級人類。

現況：目前的 intent gate 只是**範圍守門**（這是不是 o11y 問題），不是依 error budget + 信心門檻決定「自主執行 vs 升級」的治理。沒有 confidence threshold 驅動的核准/升級邏輯（因為根本沒有要核准的行動）。

### 3. Learn 循環沒閉合

哲學文件要求：把每次執行結果回饋模型，校準未來信心，對同類故障越修越快（複利效應）。

現況：`Findings` 只 sink 到 log / Grafana annotation，**沒有任何回饋去校準未來信心**。`MemorySaver` 是 per-thread 會話記憶（讓使用者能續同一個 thread），不是跨事件的學習基礎設施。

### 4. 校準誤差（CE）未被量測、未驅動自主權

哲學文件把 CE 列為「最重要的健康指標」，最危險的失敗模式是「過度自信地做錯（Confidently wrong）」；CE 升高時代理必須主動限縮自主權、交還人類。

現況：代理會**輸出** confidence，但從不拿它跟實際成功率比對，也沒有任何機制用 CE 去調節自主權。

### 5. 四大核心工作流只覆蓋 1 個、且不完整

- **Incident Response**：只有「調查/RCA」半段，沒有「遏制性操作」與「升級」的另一半。
- **Decision-Aware Delivery（CRS）**：無。
- **Autonomous Chaos Engineering**：無。
- **Pre-Incident Intelligence（PRS 預測訊號）**：無——它是被告警觸發的**反應式**，不是 SLO 違規前主動防禦的**預測式**。

---

## 四、補完路線圖：ARE 落差 × v3 路線收斂

**關鍵發現：不需要為 ARE 另開新路線。** ARE 的落差和原本的 v3 設計（[`aiops-agent-design-v3.md`](../aiops-agent-design-v3.md) §7 migration、§8 未決問題）其實是同一條路的兩個視角——v3 的 step 5/7（runbook / action registry）正好就是 ARE 缺的 Act + Execution + Governance；v3 §8 的「Tier 2 confidence 怎麼量化」正好就是 ARE 的 CE 校準問題。

### 4.1 v3 §7 七步現況 × ARE 對應

| v3 step | 狀態 | 對應 ARE 維度 |
|---|---|---|
| 1. k8s read-only signal | ✅ 完成（`tools/k8s.py`：pod/events/deployment 唯讀 + read-only SA RBAC）| 強化 Signal / Detect（infra vs code 的另一半因果）|
| 2. budget guard + Findings | ✅ 大致完成（StateGraph + `force_answer` + `Findings`；多假設 planner 未做）| 有界自主、信心輸出 |
| 3. `/webhook/alert` + dedup + 來源驗證 | ✅ 完成（`webhook.py`）| Detect |
| 4. findings sink + thread=fingerprint | ✅ 完成 | — |
| 5. Tier 0/1 runbook（連結 + 唯讀診斷）| ✅ 完成（`app/runbook.py` + `runbooks/`）| **Act 的合約骨架（仍唯讀）** |
| 6. plugin 呈現 + 設計 alert | ✅ 完成（`/investigations` API + plugin Investigations 頁：結論/信心/治理決策 + UI 標記對錯回寫 CE；設計 alert：agent 產 ```alert``` block → plugin 卡片 + 「Create alert」按鈕 → `POST /alerts/provision` 寫入 Grafana provisioning API）| **Governance（有副作用就 gate）熱身** |
| 7. action registry + Tier 2 remediation | ◾ 前半完成（`actions.py` registry + `governance.py` 策略閘，propose-only）；執行 / circuit-breaker / audit / dry-run 未做 | **Act / Execution / Governance / CE 門檻** |

### 4.2 建議的下一步（此順序同時是 v3 的 ROI 序，也是 ARE 的安全序）

**1. v3 step 1：k8s read-only 訊號源 ✅ 已完成**
純 Signal/Detect 強化，補上「OOMKilled / CrashLoopBackOff / rollout 卡住」這半邊因果。實作為 **native 工具**（`tools/k8s.py`，配合現行架構已棄用 MCP、改直連 native API 的作法），非 MCP server：`k8s_pod_status` / `k8s_events` / `k8s_deployment_status`，皆唯讀；service→object 以 `app=<service_name>` label 解析（demo 實際慣例，非 v3 文件假設的 `app.kubernetes.io/name`）；in-cluster 用 read-only ServiceAccount（`demo-services/k8s/15-aiops-agent.yaml` 加了 SA+Role+RoleBinding），host-side 用 kubeconfig，兩者皆不可用時優雅降級為 `unavailable`。catalog 與 RCA playbook 都加了「infra vs code」的判讀。測試：mock 失敗路徑 7 個單元測試 + k3d 實機 smoke。
> 對齊：DRAL Detect、訊號平面。

**2. ARE 補強：CE 量測 harness ✅ 已完成**
把每次 headless run 的 `Findings.confidence` 落地，對錯**離線標記**後算 ECE / MCE / Brier / reliability bins。實作 `app/calibration.py`：兩階段（線上 `record_run` 記 pending、離線 `label_run` 補 verdict），correctness 來源**可插拔**且與 grader 解耦——`score_to_correct`（吃 o11y-bench 分數過門檻）或 `grade_against_truth`（demo 的 service/version ground-truth 比對）。webhook 路徑已 best-effort 接上（fingerprint 當 run_id）。CLI：`python -m app.calibration report | label <run_id> --correct/--wrong`。測試：10 個單元測試 pin 死校準數學 + store round-trip。它是 step 7「Tier 2 confidence 門檻」的**唯一前置**，且零行為改變。
> 對齊：紀律—校準誤差（CE）；同時是 Learn 與 Governance 的共同前置。

**3. v3 step 5：Tier 0/1 runbook ✅ 已完成**
把 ARE 從「純推論」推向執行平面而**不跨進副作用**。實作 `app/runbook.py` + `runbooks/`（pyyaml，零外部新依賴）：
- **Runbook = Action Contract**：pydantic 模型 `trigger` / `diagnostics`（唯讀）/ `remediation`（標 `reversible` / `requires_approval`）。
- **Tier 0（連結）**：`match_runbook`（annotations 的 `runbook_id` 優先，否則 trigger 比對）→ `render_runbook` 把 incident 參數填進步驟，注入 headless RCA；remediation 步驟只渲染、標示「需人工核准、不自動執行」。
- **Tier 1（唯讀診斷）**：`run_diagnostics` 在 agent loop 之前、**不計入 agent budget** 自動跑診斷，結果（pass/fail/skipped/error + `expect`）注入讓 agent 在「已確認的前提」上推論。
- **唯讀 by construction**：runner 只 dispatch 呼叫方傳入的 read-only `tool_map`（agent 的全唯讀 TOOLS）；action 不在 map 內（例如 remediation 的 `k8s.rollout_undo`）→ 結構性 skip，永不執行；參數未填滿的步驟也 skip，不發半成品查詢。
- 已接上 `run_headless`（best-effort）。測試：13 個單元測試（match/substitute/render/check 評估/runner 的 pass/skip-非唯讀/skip-未解析/error）+ k3d 實機整合（k8s 診斷 PASS、Prom 步驟優雅 ERROR）。
> 對齊：Act 合約骨架（唯讀）、執行平面的契約模型。

**4. v3 step 6：設計 alert 能力 ✅ 已完成**
第一個「有副作用 + human-in-the-loop」能力，Governance 模式的熱身。沿用 ```promql``` block→panel 的 pattern 延伸 ```alert``` block→卡片+按鈕。實作 `app/alerts.py`（pydantic `AlertSpec` 合約 + 純轉換 `build_alert_rule`：spec→Grafana 三段式 managed alert rule payload〔instant query A → reduce B → threshold C〕+ I/O `provision_alert` + `parse_alert_blocks`，鏡像 plugin 的 splitQueryBlocks）。endpoints：`POST /alerts/preview`（dry-run，不寫入）/ `POST /alerts/provision`（fail-closed：缺 grafana 憑證或 `alert_provisioning_enabled=False`→503）。agent prompt 加 ```alert``` block 規範（**propose-only**，從不自動建立）；plugin `AlertProposalCard.tsx` 渲染卡片，**唯有人類按按鈕**才 POST 去 provisioning——human-in-the-loop 是結構性的。鐵律對齊（§4.3）：唯讀推論核心不變，新增能力可關閉（`alert_provisioning_enabled`）；不像 `actions_enabled`（自主變更，預設 off），建 alert 可逆 + 人類確認，故預設 on 但仍 fail-closed。測試：25 個單元測試（spec 驗證 / payload 形狀 / parse〔含 malformed skip〕/ fail-closed 閘 / 寫入 mock）。
> 對齊：Governance—「有副作用就 gate」、Execution 契約模型（首次跨進唯讀以外，但仍 human-gated）。

**5. v3 step 7：action registry + Tier 2（前半已完成；執行半段待做）**
✅ **前半（safe，propose-only）已實作**：
- `actions.py` — typed action registry，把「LLM 直接 kubectl」的路徑從架構上關掉：agent 只能命名*已登記*的動作（帶 `reversible` / `requires_approval`），且執行受**雙重關卡**——master kill switch `actions_enabled`（預設 False）+ 動作須有實作（本層一律不接）。seeded 動作（`k8s.rollout_undo` / `k8s.scale`）皆 reversible + 須核准 + 無實作。
- `governance.py` — 運行時策略閘 `decide()`：依 run confidence × 量測到的校準（讀 CE harness）決定 `AUTO / PROPOSE / ESCALATE`。**不可逆永不自主**、**須核准至多 PROPOSE**、信心過低 ESCALATE；高信心可逆動作須**校準 proven-good**（足夠標記數 + overconfidence 在容忍內）才 AUTO，否則降級——這正是 ARE「CE 升高 → 收緊自主權」。沒有校準證據預設不授權自主（在不確定時優雅降級交人）。
- 已接 `run_headless`（matched runbook 的 remediation → 經閘 → `result["decisions"]`，webhook 記 log）。propose-only：**完全不動系統狀態**。測試：15 個單元測試（registry 雙重關卡 + 閘的硬規則/信心帶/校準收緊）。

⏳ **後半（待做、最高風險）**：實際執行 + dry-run/blast-radius + circuit breaker + audit log + Learn 閉環（人類核准/否決 → 回寫 CE → 校準未來信心）。需先累積真實標記資料、圍欄齊全才開 `actions_enabled`。
> 對齊：Act、執行平面、Governance（運行時策略）、Learn 閉環、CE 驅動自主權。

**（拉長路線）Pre-Incident Intelligence**
從現有 metrics 衍生簡單的預測性可靠度訊號（PRS），在 SLO 違規前發出無害的防禦性建議（先只是建議，不自動擴容）。v3 路線未涵蓋，屬 ARE 第四工作流的延伸。

### 4.3 設計鐵律

保持「**全唯讀核心 + 可選的、受合約約束的行動外掛**」這個邊界。aiops-agent 目前最大的 ARE 資產，正是它**結構性地**保證了「推論只產出提案」——任何 Act 能力都應以獨立、可關閉、帶回滾合約的方式加上去（v3 §5.3 的 action registry 正是此意），不要污染唯讀的推論核心。

### 4.4 一句話

> 沿著 v3 §7 把 **step 1 → 5 → 6 → 7** 做完，就是在補 ARE 的 **Detect 強化 → Act 合約骨架 → Governance → 完整 Execution**；中間插一個 **CE 量測**（重用 o11y-bench grading）把 §8 的 confidence 門檻問題解掉，**Learn 閉環**自然成形。
