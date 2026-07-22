---
title: "2026鐵人賽_Outline v2"
tags: 2026鐵人賽
date: 2026-07-22
---

# 【AIOps with OpenTelemetry】重新規劃說明

## 為什麼要重規劃

對照 [《代理式可靠性工程》(ARE)](https://tedmax100.github.io/agentic-reliability-engineering-zh-tw/index.html) 全書章節與 `aiops-agent/service/app/` 實際程式碼後，發現三件原本 v1 outline 沒抓到的事：

1. **第三階段嚴重低估了現有實作深度。** `governance.py` / `breaker.py` / `calibration.py` / `blast_radius.py` / `action_requests.py` / `actions.py` 這六支檔案的 docstring **直接引用 ARE 書的章節號**（例如 `governance.py` 開頭就寫「ARE §三 Governance plane / §四.2 calibration」），代表這個 repo 從設計階段就是照著書的四平面架構在蓋。但 v1 的 Day24-29 完全沒提到這六支檔案，只集中在 `agent.py`/`investigations.py`——等於書裡最貼近「治理平面」「執行平面」的具體實作，在系列裡完全沒被講到。
2. **Day31 對 CE 的「缺口」描述已經過時。** `calibration.py`（288 行）已經完整實作了信心分數校準測量（`record_run` → `label_run` → `compute_calibration`），而且 `governance.py` 已經把「校準未證明時拒絕 AUTO」這條 ARE 的核心安全論點寫成了實際的 if/else。真正的缺口不是「沒有 CE 機制」，而是「有機制但累積的已標記樣本數/校準品質現況如何」——這是更誠實、也更有戲的說法。
3. **`doc/aiops-agent-knowledge-loop.md` 這份現有設計文件本身就是 ch07 封閉學習迴圈(CLL) 概念的延伸實作藍圖**（過去案例注入、investigation→draft runbook 合成、runbook feedback 三個閉環 + 假說樹/信心閘門），v1 完全沒有用到這份文件，但它是全 repo 裡跟書中 CLL 概念貼合度最高的既有素材。

調整方向：**把 ARE 的「四平面架構」(ch06) 當成貫穿全系列的顯性骨架**，而不是只在 Day22 用一次 CEL 當概念日。第一、二階段（治理基礎建設、Signal Plane 管線）大致維持 v1 架構（已經很扎實），第三階段（Agent 設計）改為明確地走過推理／執行／治理三平面，並收斂進 ch07 的「情境覺知物件、候選行動集、授權層級、CLL」與五大旗艦 SLO。

---

## 理論 ↔ 程式碼對照表（全系列共用骨架）

| ARE 概念 | 章節 | 對應程式碼 | 現況 |
|---|---|---|---|
| 決策級遙測 / 拓撲圖 / 拓撲代理 | ch03 §2.1-2.4 | `signals/topology.py`(180行), `signals/context.py`(157行) | 拓撲圖已有，edge 對帳(s2)是已知缺口 |
| 五種具名失效模式 | ch03 §2.6 | 見下方對照 | Day1/8/13/17/20 分別對應 |
| 訊號契約 | ch06 §4.3 | `signals/contract.py`(161行), `signals/weaver.py`(74行) | 已有 schema 對齊檢查 |
| 情境豐富層(CEL) | ch10 §8.3 | `signals/compile.py`(182行), `signals/context.py` | enrichment/correlation 有，projection/confidence 待補 |
| 推理平面／候選行動 | ch06 §4.4, ch07 §5.5 | `agent.py`(1460行), `investigations.py`(137行) | 有假設驗證，候選行動排序待查證 |
| 執行平面／行動契約 | ch06 §4.5 | `actions.py`(118行,註冊表+kill switch), `blast_radius.py`(218行,乾跑閘門), `action_requests.py`(224行,狀態機), `execution.py`(614行) | **已完整分層實作**，v1 完全沒提到 |
| 治理平面／授權層級／信任天花板 | ch06 §4.6, §4.9 | `governance.py`(181行,Autonomy AUTO/PROPOSE/ESCALATE), `breaker.py`(107行,runaway/flapping) | **已把「校準未證明拒絕AUTO」寫成程式碼**，v1 完全沒提到 |
| 信心分數／校準誤差(CE)／CLL | ch07 §5.5, §5.8 | `calibration.py`(288行) | **已實作**，v1 誤判為缺口 |
| 五大旗艦 SLO | ch07 | `rubric.py`(152行), `eval/harness.py`(318行) | ARR/RL-SLO 可算，DQ-SLO/AE-SLO/CE 需要標記樣本 |
| 三個知識閉環 + 假說樹/信心閘門 | ch07 CLL 的延伸設計 | `doc/aiops-agent-knowledge-loop.md`（現有設計文件，未實作） | 全新素材，v1 沒用到 |

**五種具名失效模式對照（ch03 §2.6）**：靜默腐化(Silent Decay)→Day13；過時的拓撲(Stale Topology)→Day17；模糊的語意(Ambiguous Semantics)→Day8；訊號洪流(Signal Flood)→Day20；訊號斷崖(Signal Cliff)→**目前系列沒有對應日**，兩個版本都在 Day23 收尾時補一段誠實承認這個缺口。

---

# 版本 A：33 天精簡版

保留 v1 的天數預算，靠合併 Phase 1/2 裡主題相近的兩組 Day，把空間讓給 Phase 3 的四平面深挖。

## 第一階段：治理基礎建設（Day1-14，v1 的 15 天併掉 1 天）

- **Day1** 起手式：未治理的示範服務（對應 ch03 失效模式：模糊的語意的源頭）
- **Day2** 安裝 OTel Operator，拆解 CRD
- **Day3** annotation 做 auto-instrumentation
- **Day4** Collector 部署模式實測
- **Day5** Operator 設定轉 GitOps
- **Day6** 主動製造升級/資源限制問題並修
- **Day7** 接上 Weaver：第一次 `weaver registry check`（銜接 ch06 §4.3 訊號契約）
- **Day8** 命名漂移，用 weaver 抓出來（**對應 ch03 失效模式：模糊的語意**）
- **Day9** weaver check 進 CI Gate
- **Day10** weaver live-check 接上 collector
- **Day11**（合併原 Day11+12）自訂 semantic convention ＋ multi-registry 分層拆分：先從零定義一組企業內部事件過 check，再直接在同一天疊 base+team-specific 兩層 registry 驗證合併規則，一次講完「單團隊夠用」到「多團隊會撞」的完整光譜
- **Day12**（原Day13）重現一次真實 breaking change（**對應 ch03 失效模式：靜默腐化**——weaver 0.23.0 hard error 案例，講三層驗證模型 always-error/future-gated/info）
- **Day13**（原Day14）概念日：機器可讀的「意圖」——這裡明確接上 **ch06 §4.5 行動契約的「意圖對齊(intent alignment)」欄位**，讓讀者知道這個抽象概念不是憑空的，後面 Day29 的 governance.py 就是它的具體實作
- **Day14**（原Day15）治理環境收尾：新服務上線 checklist

## 第二階段：AIOps 核心能力管線（Day15-20，v1 的 8 天併掉 2 天）

- **Day15** 讀現況：畫出 signals 模組實際資料流，對照 ch03 §2.4 拓撲代理概念，標出九宮格哪幾格是空的
- **Day16**（合併原Day17+18）拓撲圖對帳三部曲：擴充 `reconcile.py` 做 edge 對帳（**對應 ch03 失效模式：過時的拓撲**）→ 串 `tools/discovery.py` 的 `list_service_names()` 做成可排程腳本，一天內講完「發現缺口」到「有資料源可定期驗證」的完整迴路
- **Day17**（合併原Day19+20）Signal Plane 品質收斂：`dq.py` 串 `weaver.py` 做 schema 對齊檢查 + `context.py` 降 edge reconcile 噪音（**對應 ch03 失效模式：訊號洪流**），一天內講完「資料對不對」跟「資料吵不吵」兩個層次
- **Day18**（原Day21）health.py：異常偵測順著圖走
- **Day19**（原Day22）概念日：情境豐富層(CEL)——明確引用 **ch10 §8.3**，CEL 三職責（enrichment/correlation/projection）+ 溯源(grounding)
- **Day20**（原Day23）收尾：s1-s4 邊界對照 CEL 三職責，順便誠實補一段「訊號斷崖」這個失效模式目前系列沒有實例

## 第三階段：Agent 設計 —— 走一遍推理/執行/治理三平面（Day21-29，v1 的 6 天擴為 9 天）

- **Day21** 概念日+現況圖：四平面架構對照 repo 實際模組——把上面那張對照表變成一篇文章，講清楚為什麼 governance.py/breaker.py/calibration.py/blast_radius.py/action_requests.py/actions.py 不是隨便命名，而是照書的 §4.2「正交性」原則刻意分檔案
- **Day22** 推理平面：候選行動 vs 單一決策——對照 ch06 §4.4/ch07 §5.5「候選行動集」定義（觸發訊號/假設/選項排序/風險/信心），抓一次真實 investigation 輸出逐欄位對照，誠實標出現在是不是真的「排序多個選項」還是只有一條路徑包裝成看起來像
- **Day23** 重現一次 discover-before-query 失敗案例並修（原Day25內容，寫進 eval fixture）
- **Day24** tools/query.py：修一個真實 API 怪癖（原Day27內容）
- **Day25** Agent 自身可觀測性：決策有沒有被 trace（原Day26內容，檢查 audit.py/execution.py）
- **Day26** 執行平面三件套：`actions.py` 註冊表+kill switch、`blast_radius.py` 乾跑閘門、`action_requests.py` proposed→approved→executing→terminal 狀態機——對照 ch06 §4.5 行動契約六欄位，逐一標出這三支檔案各實作了哪幾格
- **Day27** 治理平面：governance.py 把「信任天花板」寫成程式碼——這是全系列最貼近 **ch06 §4.9「信任天花板」**的一天：L2→L3 轉換需要治理平面/行動契約/自動逆轉/校準信心四項同時就緒，逐條讀 Autonomy 判斷邏輯（irreversible→ESCALATE、confidence<low→ESCALATE、calibration unproven→降級PROPOSE），對照書裡原句展示程式碼字面實現
- **Day28** 校準誤差與封閉學習迴圈：`calibration.py` 的 record_run/label_run 兩階段設計對照 **ch07 §5.8 CLL** 五步驟，跑一次完整流程；順便把 `doc/aiops-agent-knowledge-loop.md` 的三個知識閉環（過去案例注入/draft runbook 合成/runbook feedback）介紹成 CLL 概念的延伸設計藍圖——標明這是「設計已寫好、尚未實作」，給讀者一個清楚的下一步
- **Day29** 重現一次預算壓力下的幻覺並加防護（原Day29內容），呼應 Day27 的 ESCALATE 機制與 breaker.py 的 runaway 防護——這次失敗案例現在有地方可以掛：是治理平面該攔卻沒攔，還是攔了但誤判

## 第四階段：專案復盤與評測（Day30-33，維持 v1 的 4 天）

- **Day30** 概念日：五大旗艦 SLO + 成熟度模型總覽——這天不再是空講表格，而是直接把 Day21-29 走過的程式碼對應回 L1-L5，講清楚這個 repo 現在卡在哪一級、缺哪個機制才能往上
- **Day31** Rubric 落地：對五大 SLO 跑一次完整 eval——**CE 這欄不再寫「缺口」**，而是用 `calibration.py` 的實測結果誠實報告目前累積了幾筆標記樣本、`compute_calibration` 算出來的校準誤差是多少、離 governance.py 判定「proven-good」還差多遠
- **Day32** 復盤兩個真實失敗：2/9 分事件 + histogram bucket 假象值，各寫成 eval fixture
- **Day33** 收尾：跑一次端到端 demo，畫出正向循環（治理checklist→意圖宣告→拓撲reconcile/CEL→四平面決策鏈→五大SLO/rubric評分→回饋治理與拓撲）

---

# 版本 B：36 天完整版

Phase 1/2 完全不動（維持 v1 原本的 15+8=23 天），Phase 3 從 6 天完整擴為 9 天，Phase 4 順延。差異只在 Phase 3；Phase 1(Day1-15)、Phase 2(Day16-23) 直接照抄 v1 原文，這裡不重複列。

## 第三階段：Agent 設計 —— 走一遍推理/執行/治理三平面（Day24-32，9 天）

- **Day24** 概念日+現況圖：四平面架構對照 repo 實際模組（同精簡版 Day21，內容一致，只是編號不同）
- **Day25** 推理平面：候選行動 vs 單一決策（同精簡版 Day22）
- **Day26** 重現一次 discover-before-query 失敗案例並修（= v1 原 Day25）
- **Day27** tools/query.py：修一個真實 API 怪癖（= v1 原 Day27）
- **Day28** Agent 自身可觀測性：決策有沒有被 trace（= v1 原 Day26）
- **Day29** 執行平面三件套：actions.py / blast_radius.py / action_requests.py（同精簡版 Day26）
- **Day30** 治理平面：governance.py 信任天花板即程式碼（同精簡版 Day27）
- **Day31** 校準誤差與封閉學習迴圈：calibration.py + CLL + knowledge-loop 三閉環介紹（同精簡版 Day28，因為天數較寬裕，可以把三個知識閉環的機制各展開一小段，甚至各附一段可執行的原型程式碼，而不是精簡版裡的概略帶過）
- **Day32** 重現一次預算壓力下的幻覺並加防護（= v1 原 Day29）

## 第四階段：專案復盤與評測（Day33-36，內容同精簡版 Day30-33，只是編號整體後移 3 天）

- **Day33** 概念日：五大旗艦 SLO + 成熟度模型總覽
- **Day34** Rubric 落地：對五大 SLO 跑一次完整 eval（CE 用實測數據取代「缺口」敘述）
- **Day35** 復盤兩個真實失敗：2/9 分事件 + histogram bucket 假象值
- **Day36** 收尾：端到端 demo + 正向循環圖

---

## 兩版怎麼選

- **選 33 天版**：如果報名/寫作節奏就是照 v1 原本承諾的 33 天走，不想再多寫；代價是 Day11(自訂semconv+multi-registry)、Day16(拓撲對帳三部曲)、Day17(schema+噪音收斂) 這三天資訊量會比較密，寫的時候要注意別塞太多小節。
- **選 36 天版**：Phase1/2 完全不用改，只是在原本 Day24-29 的位置往後插入 3 天新內容（四平面地圖、執行平面三件套、治理平面），對已經寫完/正在寫 Phase1/2 草稿的人衝擊最小；代價是多寫 3 篇。
- 兩版共用同一份「理論↔程式碼對照表」與五種失效模式對照——這張表本身也可以單獨當作 Day21/24 那篇的骨架直接用。
