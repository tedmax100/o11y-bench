# AIOps 30 天：設計 → 實作 → 應用

定位轉變：這不是 30 篇解說文，是 30 個建構里程碑。每天先有一個對應到具體檔案/指令的產出（config、code diff、可跑的 demo），文章是紀錄「為什麼這樣設計、卡在哪裡」，不是憑空講概念。

範圍決策：
- 治理篇（Operator/Weaver）：repo 目前沒有 Operator 落地，這段是獨立、可重現的教學工程，讀者跟著做能從零搭出 Operator+Weaver 治理環境。
- 拓撲圖譜/管線/Agent/復盤：直接在 `aiops-agent/service/app/` 現有基礎上做深做穩，不重新設計。現況：
  - `signals/topology.py`（180行）— 已有 ServiceNode/Edge/Topology 模型，upstream/downstream/impacted_by/journey_of 查詢，`validate_against_live()` 對活資料做存在性檢查
  - `signals/reconcile.py`（184行）— 對帳邏輯，目前對節點；edge 對真實 Tempo call graph 的對帳（topology.py 註解裡提到的 s2）是缺口
  - `signals/contract.py`（161行）、`signals/dq.py`（66行）、`signals/health.py`（342行）、`signals/weaver.py`（74行）、`signals/context.py`（157行）、`signals/compile.py`（182行）
  - `rubric.py`（152行）、`eval/harness.py`（339行）— 評測骨架已在
  - 已知缺口（來自過往排查記憶）：s4 real-mutate remediation 尚未做、跨 schema 泛化尚未驗證、agent 幻覺/TraceQL 語法錯誤尚未系統性修

每天結束時要有一個可以 `git diff` 看到的真實變更，或至少一個可重現的指令輸出。

---

## 第一階段：Telemetry 治理基礎建設（Day 1–14）
獨立教學環境（k3d/minikube 均可），讀者跟著搭一遍。

**Day1 — 起手式：搭一個乾淨的 k3d/kind cluster + 部署一支未治理的服務**
產出：一個會亂長 span/attribute 命名的示範服務（故意不治理），作為後面對照組。
文章角度：治理債會複利，用這個示範服務的「亂長」當開場證據。

**Day2 — 安裝 OTel Operator，拆解 CRD**
產出：cluster 裝好 opentelemetry-operator，`kubectl get otelcol,instrumentation` 有實際輸出。
文章角度：CRD/Instrumentation/Collector 三件套怎麼分工。

**Day3 — 用 annotation 做 auto-instrumentation**
產出：Day1 的服務加一個 annotation 後自動出現 trace，不改一行程式碼。
文章角度：全公司規模下最小成本達到覆蓋率的證據截圖。

**Day4 — Collector 部署模式實測**
產出：同一支服務分別跑過 sidecar / daemonset / gateway 三種 collector 部署，記錄資源用量差異。
文章角度：選型不是憑感覺，是有數字的權衡。

**Day5 — Operator 設定轉成 GitOps**
產出：把 Day2-4 的 CRD/Instrumentation 用 Helm/Kustomize 做成可 PR 審查的檔案，進 repo。
文章角度：治理要可審查、可回溯，不能是誰手動 kubectl apply 出來的。

**Day6 — 主動製造升級/資源限制問題並修**
產出：刻意升版本或調低 resource limit 讓 collector 掉資料，記錄排查過程與修法。
文章角度：把踩坑實錄變成可重現的實驗，而非事後補述。

**Day7 — 接上 Weaver：跑第一次 `weaver registry check`**
產出：拿 `weaver-demo/examples` 現有 registry 對 Day1 服務的 telemetry 跑一次 check，記錄真實違規輸出。
文章角度：Registry 到底在檢查什麼，用真實違規案例說明。

**Day8 — 故意製造命名漂移，用 weaver 抓出來**
產出：改一個 attribute 命名（如 `userId` vs `user.id`），weaver check 應該要 fail。
文章角度：口頭約定 vs 工具強制的差異，用同一個案例正反對照。

**Day9 — weaver check 進 CI Gate**
產出：GitHub Actions（可參考 `.github/workflows` 既有寫法）裡加一個 weaver check job，PR 不過不能 merge。
文章角度：治理落地的第一個可複製機制，附完整 workflow yaml。

**Day10 — weaver live-check 接上 collector**
產出：對 Day1-6 環境跑 live-check，抓到實際飛行中不合規的資料；避開 4317 collision（用專用 port）。
文章角度：靜態 CI vs 動態流量檢查的互補關係，附踩坑記錄。

**Day11 — 自訂 semantic convention**
產出：仿照 `weaver-demo/examples/telemetry/registry/payment-events.yaml` 的做法，為 Day1 服務定義一組企業自訂事件並過 check。
文章角度：官方 semconv 之外，企業內部資料怎麼納管。

**Day12 — Multi-registry 拆分實驗**
產出：把 registry 拆成 base + team-specific 兩層，驗證 weaver 能正確合併/檢查。
文章角度：組織規模下治理不可能只有一份 registry。

**Day13 — 重現一次真實 breaking change**
產出：刻意用 weaver 0.23.0 對含 `metric_requirement_level` 的 schema 跑 check，重現 hard error，記錄繞過/修法。
文章角度：工具鏈本身要版控，不能盲目升級。

**Day14 — 治理環境收尾：產出一份「新服務上線 checklist」**
產出：把 Day1-13 的成果收斂成一份可執行 checklist（含 CI job 範本、registry 範本），存進 repo 文件。
文章角度：治理篇總結，這份 checklist 就是可交付給團隊使用的產出。

---

## 第二階段：AIOps 核心能力管線（Day 15–21）
直接在 `aiops-agent/service/app/signals/` 現有基礎上動刀。

**Day15 — 讀現況：畫出 signals 模組的實際資料流**
產出：一張基於 `topology.py`/`context.py`/`compile.py` 實際 import 關係畫出的架構圖（不是憑印象畫）。
文章角度：AIOps 九大能力總覽，並標出現有程式碼分別落在哪幾格。

**Day16 — 補 s2：edge 對真實 Tempo call graph 做對帳**
產出：擴充 `reconcile.py`，讓它不只對節點存在性（`validate_against_live`），也對 `topology.yaml` 宣告的 edge 跟 Tempo 實際 call graph 做 diff，輸出「宣告但不存在」與「存在但未宣告」的邊。
文章角度：Context and topology mapping 是地基——用這個實作證明「圖不準，後面全部失真」。

**Day17 — Ingest 端補：discovery.py 產生的服務清單餵進 reconcile**
產出：串接 `tools/discovery.py` 的 `list_service_names()` 到 Day16 的 edge reconcile，做成一個可定期跑的腳本。
文章角度：資料進來之前，拓撲已經決定你能不能做有效聚合。

**Day18 — dq.py 擴充：schema 對齊檢查串進 weaver.py**
產出：在 `dq.py` 加一項檢查，用 `signals/weaver.py` 現有的 weaver 整合，驗證進來的 telemetry attribute 是否符合 registry。
文章角度：正式把第一階段的治理成果和 Signal Plane 串起來，enrichment 前提是 schema 對齊。

**Day19 — context.py：把 edge reconcile 的噪音降下來**
產出：修改 `context.py` 注入的 decision-grade context，讓「宣告但無流量」的邊不要污染 agent 的關聯判斷。
文章角度：拓撲關係如何在伺服器端把統計噪音先過濾掉，附 before/after 的 context 輸出對比。

**Day20 — health.py：異常偵測順著圖走**
產出：檢查 `health.py`（342行，現有最大模組）目前的異常判斷邏輯是否用到 topology 的 upstream/downstream，如果沒有，加一個「用拓撲排序異常候選」的步驟。
文章角度：異常偵測為什麼要順著圖走，用改前改後的判斷順序做對比。

**Day21 — 收尾：把 s1-s4 的邊界寫成一份現況文件**
產出：更新（或新建）`signals/` 目錄下的現況說明，明確標出哪些是 s1-s4 已完成、哪些是本階段新補的。
文章角度：管線篇總結，指出學習迴路的終點是回頭改善拓撲圖本身，銜接下一階段。

---

## 第三階段：Agent 設計（Day 22–27）

**Day22 — agent.py 決策鏈梳理**
產出：畫出 `agent.py`/`investigations.py` 目前 discover→query→hypothesize→verify 各步驟對應到哪些函式，標出斷點。
文章角度：Agent 架構總覽，用真實程式碼骨架而非教科書圖。

**Day23 — 重現一次 discover-before-query 失敗案例並修**
產出：用過往排查記憶裡的真實失敗（硬編碼 demo-services schema 假設）寫一個 regression case 進 `eval/fixtures.yaml`，跑 `eval/harness.py` 證明目前會/不會失敗。
文章角度：不是空談原則，是用一個可重跑的 eval case 佐證。

**Day24 — Agent 自身可觀測性：確認 agent 決策有沒有被 trace**
產出：檢查 `audit.py`/`execution.py` 是否已把每個工具呼叫寫進可回放的紀錄，缺的話補上。
文章角度：觀測 agent 和 agent 做觀測同等重要。

**Day25 — tools/query.py：修一個真實 API 怪癖**
產出：從過往排查記憶挑一個具體怪癖（Prom metadata 為空 / Loki label 需要時間範圍 / Tempo tag 命名）在 `tools/query.py` 或 `tools/discovery.py` 裡補防呆，並寫一個測試。
文章角度：工具好不好用直接決定 agent 表現上限，附 diff。

**Day26 — Signal Plane 真正接進 agent 的決策路徑**
產出：確認 `context.py` 產出的拓撲 context 有沒有被 `agent.py` 實際使用在 prompt/工具選擇上，沒有就補上這條連接。
文章角度：本階段核心銜接——系統圖譜不是抽象口號，是 agent 架構裡真的在跑的一層，附 before/after 的 agent 輸出對比。

**Day27 — 重現一次預算壓力下的幻覺並加防護**
產出：用 `eval/harness.py` 限縮 budget/turns 跑一次，重現 fast-path 或 trace-id 幻覺，然後加一個檢查（如 `breaker.py`）擋掉。
文章角度：誠實揭露現實限制，用可重現的失敗案例代替空泛警語。

---

## 第四階段：專案復盤與評測（Day 28–30）

**Day28 — Rubric 落地：跑一次完整 eval，看真實分數**
產出：`rubric.py` + `eval/harness.py` 對目前 agent 跑一次完整評分，記錄各項得分細節（不是「大概怎樣」，是實際數字）。
文章角度：為什麼要自建 benchmark，用這次實測分數當證據。

**Day29 — 復盤兩個真實失敗：2/9 分事件 + histogram bucket 假象值**
產出：把過往排查記憶裡的兩個具體 bug（demo-services schema 誤用、`*_duration_seconds` 用錯 bucket 導致 quantile 恆定為假象值）各寫一個 eval fixture，證明現在是否已修復。
文章角度：evaluation 發現真問題，用兩個可驗證案例收尾，不流於空泛。

**Day30 — 收尾：跑一次端到端 demo，畫出正向循環**
產出：完整跑一次「治理 checklist（Day14）→ 拓撲 reconcile（Day16-21）→ agent 決策（Day22-27）→ rubric 評分（Day28-29）」全鏈路，錄一次 demo 或截圖每一步輸出。
文章角度：把 30 天摺回一張圖，收尾整個系列，並指出評測結果如何反過來改善治理與拓撲（下一輪迭代的起點）。
