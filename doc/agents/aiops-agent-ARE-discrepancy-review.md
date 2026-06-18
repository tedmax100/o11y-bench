# aiops-agent × ARE 原典出入報告

對照基準：`Agentic 可靠性工程 #1`～`#7`（ARE 原典 ch1–ch10）＋ [`agent reliability engineerin.md`](./agent%20reliability%20engineerin.md)（核心哲學濃縮）
受測對象：`aiops-agent/service/app/`（實際程式碼，非設計稿）
銜接文件：[`aiops-agent-ARE-gap-analysis.md`](./aiops-agent-ARE-gap-analysis.md)（DRAL/平面覆蓋度）、[`aiops-agent-step7-execution-plane-design.md`](./aiops-agent-step7-execution-plane-design.md)
撰寫日期：2026-06-19

> 這份文件跟既有的 gap-analysis 角度不同：gap-analysis 從「DRAL 四拍 + 四平面我們做了哪些」盤覆蓋度；本文從**讀完 ARE 全套原典 ch1–ch10 後**，回頭確認「我們要做的 AIOps agent 跟 ARE 北極星之間真正的出入在哪」，並指出 gap-analysis 漏估的一處。

---

## 0. 一句話結論

> 我們蓋了 ARE 四平面裡最顯眼的三個（Reasoning / Governance / Execution 骨架），卻跳過了 ARE 認定是**一切前提**的第一個 —— **Signal Plane / decision-grade telemetry**，而那偏偏是本專案（o11y-bench）的本業。ARE 的成熟度模型早就預言了結果：在非 decision-grade 的底層上強行加自主，問題會「更早、更明顯地暴露」—— 對應到我們 RCA 在 o11y-bench 只拿 ~2/9、confident-wrong 的症狀。

真正的出入**不在** gap-analysis 已列的「Act/Learn 還沒做」（那是已知的、規劃中的），而在更底層、更該先補的 Signal 層。

---

## 0.5 速覽：問題 → 設計 → 缺口（專案全景）

### 我們要解決的問題

**核心痛點（v1 原點）**：on-call 收到 alert 後，真正花時間的不是看儀表板，而是在 metrics / logs / traces / k8s 之間來回切換、把分散線索拼成因果鏈、用人腦記著「查過什麼、還有什麼假設沒驗」。
**目標**：不取代 SRE，把「重複的 query 編寫 + 多訊號交叉比對」自動化，讓人專心做判斷。

**後來擴張（v3 / ARE）**：alert 自己發生時能否主動叫起 agent、自動跑完 RCA（push）；agent 能否在嚴格圍欄下安全執行已知 runbook；怎麼知道 agent 判斷可不可信（校準），可信才給更多自主權。
一句話：**把 RCA 從「人問 agent 答」進化到「alert 驅動、自動調查、信心校準、人類在迴路之上的可靠性循環」。**

### 對應的設計

| 問題 | 設計 | 落點 |
|---|---|---|
| 多訊號交叉比對 | LangGraph agent + 唯讀工具（Prom/Loki/Tempo/k8s native 直連） | `agent.py` / `tools/` |
| LLM 寫錯 query / 幻覺 | schema catalog + live capability snapshot + discover-before-query + identical-retry guard | `capability.py` / `agent.py` |
| 沒人盯的 headless run 失控 | per-turn tool budget + `force_answer` 強制收斂 | `agent.py` / `config.py` |
| alert 驅動自動 RCA | `POST /webhook/alert` + fingerprint 去重 + cooldown | `webhook.py` |
| 結論可信、可追溯 | `Findings`（結論+confidence）+ 強制 cite exact query / trace id | `agent.py` |
| 校準誤差量測 | CE harness（ECE/MCE/Brier） | `calibration.py` |
| runbook 表示與唯讀診斷 | Tier 0/1 runbook（連結 + 唯讀 diagnostics 自動跑） | `runbook.py` / `runbooks/` |
| 設計 alert（有副作用） | ` ```alert ` block → plugin 卡片 → 人類按鈕才寫入 | `alerts.py` + plugin |
| 「有副作用就 gate」 | action registry（雙閘）+ governance（AUTO/PROPOSE/ESCALATE，讀 CE 收緊自主） | `actions.py` / `governance.py` |
| 執行平面骨架（仍 propose-only） | 狀態機 + audit + dry-run/blast-radius + circuit breaker + SQLite 持久化 | `execution.py` `action_requests.py` `audit.py` `blast_radius.py` `breaker.py` `store.py` |

**貫穿鐵律**：全唯讀推論核心 + 可關閉、帶回滾合約的行動外掛 —— 推論永遠只產出提案，結構性保證。

### 缺少的（按嚴重度）

| | 缺口 | 為什麼重要 |
|---|---|---|
| 🔴 | **Signal Plane / decision-grade telemetry** | ARE 地基、本專案本業，卻沒蓋。agent 吃原始給人看的遙測，靠 catalog「猜」；無 signal 語意、無 first-class topology、無 criticality。→ RCA 在 o11y-bench 只 ~2/9 的根因（不是 PromQL bug，是跳過 Signal 層） |
| 🔴 | **Learn 閉環** | DRAL 第四拍。Findings 只 sink 到 log/annotation，CE 只能離線手標，無跨事件學習。沒它就只是 automation |
| 🟠 | **五大旗艦 SLO 只有 1/5** | 只有 CE（半套）；缺 ARR / DQ-SLO / RL-SLO / AE-SLO。沒有「自主健不健康」的儀表 |
| 🟠 | **四大工作流只覆蓋 1 個半** | 只有 Incident Response 的 Detect+Reason；缺 Delivery(CRS)、Chaos(ACE)、Pre-Incident(PRS) |
| 🟡 | **Intent 抽象其實沒有** | intent gate 只是 scope 守門（是不是 o11y 問題），非 ARE 的「要維持什麼結果+約束+trade-off」 |
| 🟡 | **多假設推論偏線性** | RCA playbook 像線性 ReAct，非同時 surface 多假設+confidence 排序 |
| ⏳ | **執行平面後半（7b-4~6）** | 真 mutate / verify / auto-rollback / 真 AUTO；刻意壓最後，要 Signal+CE 穩了才碰 |

**收斂順序**：Signal Plane → Learn 閉環 + 補 SLO → step7 後半 → CRS。

---

## 1. 對齊良好的部分（先肯定，這些是資產，別動）

| ARE 要求（出處） | 我們的實作（證據） | 評 |
|---|---|---|
| 推論平面只產出提案，絕不直接行動（ch4.4 鐵律） | `TOOLS` 全唯讀，結構性保證（`agent.py` TOOLS / Findings） | ✅ 教科書級 |
| 幻覺傳播防禦（ch7.7：cross-signal corroboration / freshness / negative-signal / confidence-decay / forced evidence citation） | live capability snapshot「Trust this over the catalog」（`capability.py`）、discover-before-query（`agent.py` anti-patterns）、identical-retry guard、「don't invent git_version」 | ✅ **與 ARE 最強的對齊**；ch7.7 五道防線命中四道 |
| 有界自主（ch3.5 / ch4.4） | per-turn tool budget + `force_answer`（`agent.py` / `config.py`） | ✅ |
| 可解釋推論軌跡（ch4.4 candidate action 即 audit） | 強制 cite exact query（promql/logql/traceql fenced）+ 真實 trace id；串流 tool 事件 | ✅ |
| Action Contract（ch4.5：scope/precond/reversal/verify/success/outcome） | `runbook.py` + `actions.py` + step7 的 dry-run/blast-radius/breaker/audit/rollback（`execution.py` 等） | ✅ 形狀齊（仍 propose-only） |
| 信心分數（ch4.4 / ch5.2） | `Findings.confidence`，inconclusive 給 low confidence | ✅ |
| CE 是最重要的健康指標（ch3.6 / ch5.9） | `calibration.py`（ECE/MCE/Brier/reliability bins） | ◾ 數學對，但未閉環 |

---

## 2. 真正的出入（按嚴重度，最重要的放第一個）

### 2.1 🔴 訊號平面 = decision-grade telemetry：ARE 的地基，我們幾乎沒有

**ARE 怎麼說**（ch2「Foundations of Agentic Observability」、ch3、ch4.3）：

- agent **不是先在決策失敗，是先在感知失敗**；observability 是「foundational dependency」，不是 enabling capability。
- 「getting the order wrong is the single most common reason early agentic initiatives stall.」
- Signal Plane 的門檻是 **decision-grade telemetry**：
  - **signal contract**（versioned、宣告 freshness/confidence 保證、宣告支援哪些決策、exclusion conditions）
  - **語意標註**（p50/p99/p999、journey、criticality tier、upstream dependency health 直接長在訊號裡 —— ch2.2 的 before/after 範例）
  - **topology graph 當 first-class artefact**（ch2.4 / ch4.3：活的、持續對齊遙測的依賴圖，不是 wiki page）
  - **ownership / criticality** 當 schema 而非社群知識

**我們的現況**：agent 直接吃 demo-services 的**原始、給人看的** Prom/Loki/Tempo。沒有 signal contract、沒有語意 enrichment、沒有 first-class 依賴圖、沒有 criticality tier。我們用 `schema_catalog.md` ＋ `capability.py` 的 live snapshot 在**查詢當下**補這層。

**為什麼這是最大出入**：ARE ch1.3 明白點名 —— 低成熟度環境裡「humans have been quietly subsidising the system」，一放 agent 上去 subsidy 就消失，confident-wrong 浮現。我們的 catalog + snapshot **正是那層人類補貼的程式化版本**：它在「人在旁邊看」時很好用，但它是在**繞過** Signal Plane，不是**實作** Signal Plane。

> 直接證據鏈：RCA 在 o11y-bench ~2/9，PromQL/aggregation/hallucination bug 仍在 —— 這正是 ARE 成熟度模型（ch1.3 + ch4.9）預測的症狀。**「修 PromQL bug」是表象，「我們跳過了 Signal Plane」是根因。**

### 2.2 🔴 Learn 閉環沒接（DRAL 缺第四拍）

ch3.2 / ch4 反覆強調：沒有 Learn，系統就只是 automation 不是 agentic；「the cost of not learning compounds」。

現況：`Findings` 只 sink 到 log / Grafana annotation；CE 只能離線手標（`calibration.py` 的 `label_run`）。step7 的 7b-5 規劃了 verify-outcome → CE 回寫，但未實作。`MemorySaver` 是 per-thread 會話記憶，不是跨事件學習。

### 2.3 🟠 五大旗艦 SLO 只有 1/5（ARE 要求 day one 五個都在）

ch3.6：ARR / DQ-SLO / RL-SLO / AE-SLO / CE 是「the five flagships」，且要**一起讀**才有意義（高 ARR + 低 DQ = 不安全自主；高 DQ + 低 ARR = 護欄過嚴；CE 緩升 = drift）。

現況：只有 CE（半套）。**ARR / DQ-SLO / RL-SLO / AE-SLO 完全沒有量測**。等於沒有「自主是否健康」的儀表板。

### 2.4 🟠 四大工作流只覆蓋 1 個、且只有半個

讀完 ch5–ch8 後逐一確認：

| ARE 工作流 | 出處 | 我們 | 缺什麼 |
|---|---|---|---|
| Incident Response | ch5 | ◾ 半段 | 有 Detect+Reason；缺遏制操作 + 升級的另一半 |
| Decision-Aware Delivery（CRS） | ch6 | ❌ | **完全沒有**。CRS 是書裡「最乾淨的公式」（四分量加權），與我們 deploy-correlation 強項天然契合，風險又比 Tier 2 remediation 低 |
| Autonomous Chaos（ACE + VaC） | ch7 | ❌ | **完全沒有** |
| Pre-Incident Intelligence（PRS + CEL） | ch8 | ❌ | **完全沒有**。我們是被 alert 觸發的反應式，不是 SLO 違規前的預測式 |

### 2.5 🟡 「Intent」這個抽象我們其實沒有（容易誤會成有）

ch1.4：ARE 的 intent = 機器可讀的「要維持什麼結果 + 約束 + 容許的 trade-off」（`p99<200ms`、`prefer cost-efficient`、`不可超 2 replicas`…），是 first-class 輸入，與 telemetry/policy 並列。

現況：我們的 **intent gate 是 scope 守門**（這是不是 o11y 問題，`agent.py` fail-closed 分類器）。**名字一樣，東西不一樣** —— 我們沒有任何 intent 宣告餵給 agent 當優化目標或 governance 依據。

### 2.6 🟡 多假設推論仍偏線性

ch5.5 / ch9：要的是同時 surface 多個 candidate hypotheses（含 confidence + evidence chain，連不推薦的也列），由 ranking 選。我們 headless `_RCA_PLAYBOOK` 比較像線性 ReAct（gap-analysis 亦承認「多假設 planner 未做」）。

---

## 3. 刻意偏離、而且合理的地方（不用焦慮，無需改）

1. **單 agent vs 多 agent 生態**：ARE 假設一整個 cast（Observability/Topology/Release/Business Impact/Learning/Capacity/Chaos Agent）＋ SRE Orchestrator（ch8.9 / ch14）。我們把這些**角色塌縮進一個 LangGraph agent**。對 demo/benchmark 完全 OK —— ARE 自己也說那些是「roles, not products」。我們量級下不需要 Orchestrator。
2. **全唯讀核心**：我們其實**比 ARE 更保守**（ARE 在 L3+ 允許自主遏制）。這個結構性唯讀保證是 ARE 會讚賞的資產，代表我們刻意坐在 ARE 描述的自主度**之下**，是選擇不是缺陷。
3. **Governance 是模組、不是毫秒級 runtime 平面**：我們量級下 OK。
4. **MCP → native 直連**：實作選擇，與 ARE 正交。

---

## 4. 對既有 gap-analysis 的修正建議

`aiops-agent-ARE-gap-analysis.md` 速覽表把「平面：Signal ✅ 有」打勾。**依 ARE 自己的定義（ch2/ch4.3），這個勾太寬鬆。**

- 能查 Prom/Loki/Tempo ≠ Signal Plane。
- ARE 的 Signal Plane 門檻是 decision-grade telemetry（contract + 語意 + topology + ownership）。

建議：把「平面：Signal」從 **✅** 降為 **◾**（「能取得原始遙測，但非 decision-grade；無 signal contract / 語意 enrichment / first-class topology」），並把「decision-grade telemetry + topology graph」列為**第一順位缺口**。

連帶影響 gap-analysis §4.2 的路線：原路線（step 1→5→6→7）火力集中在 Act/Execution，但 ARE 成熟度模型（ch4.9）會說 —— **地基（Signal）沒到 decision-grade 之前，往上蓋 Execution 的邊際 ROI 是負的**。

---

## 5. 建議收斂順序（= ROI 序 = 安全序 = ARE 成熟度序）

1. **補 Signal Plane（最優先）**：給 demo-services 遙測加 decision-grade 包裝 —— signal 語意（criticality/journey/dependency health 等）＋ 一張真的 topology/依賴圖。直接把 RCA 分數帶起來，因為 agent 不必再用 catalog 猜。
2. **接 Learn 閉環 + 補齊五大 SLO 量測**（至少 DQ-SLO / AE-SLO；CE 已有）。
3. 才談 step7 後半（7b-4 真 mutate）。
4. （擴影響力再做）挑一個新工作流落地 —— **CRS（Delivery, ch6）最划算**：與 deploy-correlation 強項天然對齊，風險低於 Tier 2 remediation。

---

## 6. 一句話

> 沿 step 1→5→6→7 把 Execution 補完，是在補 ARE 的 Act/Governance/Execution；但 ARE 的第一塊磚是 **Signal**。先把 decision-grade telemetry + topology 這層地基蓋起來，RCA 的 confident-wrong 才會從根本收斂，後面的 Act 才站得穩。
