# 賢者大叔的觀測結界：讓 agent 推理得動的 30 天

投稿主題：**AI Engineering**（將 AI 模型整合進實際系統與產品的工程實踐，涵蓋模型部署、Agent 架構設計、上下文管理、評估與監控）。

## 定位

這不是 30 篇解說文，是 30 個建構里程碑。每天結束時要有一個可以 `git diff` 看到的真實變更，或至少一個可重現的指令輸出。文章記錄「為什麼這樣設計、卡在哪裡」，不是憑空講概念。

**敘事主軸是倒敘的**：Day1 先給失敗現場（agent 在真實系統只拿 4.5/9 分），然後一層層往回挖——架構層錯在哪、上下文層錯在哪、資料層錯在哪、怎麼證明修好了、怎麼敢讓它上線。治理內容全部保留，但**不再是前提，而是答案**：讀者是先痛過才看到 Weaver。

**驗收標準**：30 天寫完，讀者手上要有一個能裝進自己公司的東西——Grafana 告警燒起來，agent 自動調查，結論回到 Grafana annotation，需要動手時人在 plugin 上點一下核准。不是一個只能跑分的 demo。

## 兩種模式

- **治理篇（Day10-14、19、29）**：獨立、可重現的教學工程，讀者跟著做能從零搭出 Operator + Weaver 環境。k3d/kind 均可。
- **Agent / Signal Plane / 評估 / 交付篇**：直接在 `aiops-agent/` 現有基礎上做深做穩，不重新設計。

## 概念日規則

只有兩天有純概念成分（Day2、Day17），而且都必須在**當天**用現有程式碼落地驗證，不讓理論停在空中。Day3 的 LangGraph 模型講完立刻對照 `agent.py` 拆解；Day17 的 CEL 三職責同一天對照 `context.py`/`topology.py` 逐項打勾或留白。

---

## Phase 0 — 失敗現場（Day1-2）

**Day1 — 一個查得動 Prometheus 的 agent，為什麼在真實系統只拿 4.5/9 分**
產出（**已完成**，`otel-aiops-agent/ironman-2026/day01/`）：k3d 起一套「別人建的」o11y stack ＋ 一支 baseline agent（三個查詢工具、寫死的 schema prompt、4 次 tool call 預算）＋ 九題 RCA 與不接 LLM 的評分器。三個 seed 取平均 **4.5/9**（metrics 0.5、logs 1.0、traces 3.0），逐題分數三次一致。不修任何東西。

角度：全系列的反面教材。實跑之後**失敗清單跟原本設想的不一樣**，照實寫：
- **`deployment_environment="demo"` 這個不存在的 label**，讓四次查詢全部回 `"status":"success"` 的空陣列，agent 結論是「這個 metric 可能沒有資料」——推理沒錯，錯在它把一個公理當公理。
- **Loki 的 `level` 值是小寫 `warn`，prompt 寫的是 `WARN`**，60 筆 log 變成 0 筆；然後 agent 回答「產生了 **814** 筆 warning」。零筆資料，一個具體到個位數的數字。更糟的是另一題它**明明撈到了真的 log**，報出來的數字還是編的。
- **它有時候會自己 discover 救回來**（下 `{__name__=~".*requests.*"}` 探 schema 後改用 `job` 答對），有時候不會。Day7 要處理的因此不是「教會它 discover」，是「讓 discover 變成走不掉的路徑」。
- **TraceQL 沒有失敗**——`resource.service.name`／`status = error` 在這套 stack 上合法，grounding 檢查也全過。trace 三題拿 3/3，因為 trace 的回傳結果自帶結構，不需要事先猜對 label。這個對比是 Day15-17 的動機。
- **評分器自己有三個 bug**（Gemini content block 裡的 base64 thought signature 被當成數字、`5xx` 的 `5` 被當成數字、Loki `count_over_time[6h]` 用 range query 跑導致真值膨脹 160 倍）。前兩個放水、第三個冤枉，共同點是都不產生錯誤訊息。這段留在文章裡，是 Day18 的預演。
- k8s 兩個坑（inotify 上限讓節點靜默不註冊、同一個 404 在 compose 是健康在 k8s 是失敗）收在文末，形狀跟上面完全一樣。

**已決定用倒敘開場。** 現有的 `ironman-2026/day01.md`（未治理的示範服務）需要重寫成這篇——那些「亂長的命名」素材不丟掉，改成 Day1 後半的解釋材料：agent 之所以查錯，是因為它面對的就是這種服務。這樣既保留原本的反面教材，又讓第一天就出現分數。

**Day2 —（概念）AIOps 要的不是更多資料，是可推斷的資料 ＋ 30 天地圖**
產出：一份寫進 repo 的範圍宣告文件（做什麼、不做什麼）＋ 五層因果鏈的地圖圖。
角度：正面回答「AIOps 是什麼、不是什麼」——不是裝一個 AI 幫你看 dashboard，也不是自動化腳本包一層聊天介面。核心問題是可觀測性資料通常是為人設計的，machine consumer 拿來推理時缺語意、缺情境、缺信任度。劃清範圍：不做自主學習校正。地圖要畫到交付面為止（Phase 5），讓讀者第一天就知道終點是一個裝得起來的東西。

---

## Phase 1 — Agent 架構設計（Day3-6）

**Day3 — 從 LLM 到 agent：ReAct 迴圈，以及 `agent.py` 的四個 node**
產出：一個 20 行的泛用 LangGraph 最小範例（跑得起來、貼執行軌跡）＋ 基於真實程式碼的 `agent.py` mermaid 圖 ＋ 標出 discover→query→hypothesize→verify 對應到哪些函式。
角度：兩節。**前半概念**——單純一問一答 vs 需要「觀察→決策→行動」反覆循環，為什麼需要一張圖而不是 while 迴圈（狀態要跨輪傳遞、需要條件式分支、需要可中斷/恢復），`StateGraph`/node/`add_edge`/`add_conditional_edges`/checkpointer 五個原語。**後半立刻落地**——`agent`/`tools`/`force_answer`/`rubric_trace` 逐 node 對回這五個原語，那條 `add_conditional_edges` 決定要不要重試的邏輯是全天重點，並標出「哪一步開始讀 Phase 3 產出的決策級 context」這個入口點。
合併理由：泛用範例跟真實程式碼隔一天講，讀者要跨兩天才拿到一個完整的東西；同一天講完，第二節的每個 node 都能直接指回第一節剛定義的原語。

**Day4 — 工具描述就是介面契約：寫壞一個 description，agent 就查錯**
產出：改一版 tool description 前後各跑一次同一個 task，貼 before/after 的工具選擇差異。
角度：MCP `search` 是關鍵字 AND 不是語意搜尋、`browse_namespace` 不標 deprecated 而 `search` 會標且降權（同一份 registry 兩個入口兩種真相）——兩個實測都是「description 就是 agent 唯一看得到的介面」的證據。

**Day5 — 真實 API 的三個怪癖，以及 agent 怎麼在三種訊號之間跳**
產出：`tools/query.py` / `tools/discovery.py` 的防呆 diff ＋ 三個對應測試 ＋ 一次「metric 異常 → 找到代表性 trace → 撈出該 trace 的 log」的完整跳轉。
角度：前半是怪癖——Prom metadata 為空、Loki label 需要時間範圍、Tempo dotted tag，直接連 API 才會踩到、文件不會寫，也是 agent 表現上限的實際位置。順帶收 Tempo 健康探測噪音要用 `trace:duration > 5ms` 在 TraceQL 裡濾掉、不是 Python 後濾。

後半是 **OTel 相對於「三套獨立監控系統」最本質的價值**：`trace_id` 是三種訊號的接縫。exemplar 有沒有真的接上、log 裡有沒有帶 `trace_id`、Tempo 查到的 span 能不能反查回 Loki——這條路徑通不通，直接決定 agent 的調查能不能收斂。**通不了，agent 就只能在每一種訊號裡各猜一次，然後把三個猜測拼成一個聽起來合理的故事**——那正是 Day1 那份 4.5/9 分報告的形狀。

**Day6 — 預算壓力下 agent 會抄捷徑：fast-path 與 trace-id 幻覺**
產出：`eval/harness.py` 限縮 budget/turns 跑一次，重現幻覺；記錄現象但不修（防護留到 Day28）。
角度：這是架構問題不是治理問題——截斷策略決定了 agent 什麼時候開始編。誠實揭露現實限制，用可重現的失敗案例代替空泛警語。

---

## Phase 2 — 上下文的供應鏈：治理即上下文工程（Day7-14）

**Day7 — discover-before-query：把 Day1 的失敗修掉的第一刀**
產出：`eval/fixtures.yaml` 新增 regression case ＋ agent 決策鏈改動 diff，跑 harness 證明分數變化。
角度：全系列第一次「修好一個真實失敗」。硬編碼 schema 假設換成先問系統。可引一句外部佐證：Grafana Cloud 自家 production agent 的 system prompt 分析裡，第一條列出的優點就是 "Mandatory discovery before querying"——同一把刀，別人在 production 上也是這樣下的。

**Day8 — schema 是團隊共識：三個欄位，以及為什麼觀察不出語意**
產出：從零寫一份 registry，`stats` 當探針（`-r .` 假綠燈的教訓）、第一次 `check` 拿綠燈；接著 `weaver registry emit` 打進 `weaver registry infer`，diff 原始 registry 與反推 registry。
角度：前半——`enum.members` 是 LLM 唯一能事先知道 label 值域的來源、`requirement_level` 是承諾、`template` 是開放維度，三種嚴格度對照（缺 `examples` 完全不吭聲）。後半——往返實驗證明 `brief`／`requirement_level`／`enum.members` 三種資訊全部丟失。**觀察只能給你名字跟型別，語意、承諾、值域必須有人坐下來決定**，這是整個治理篇存在的理由，也是 Day7 那把刀砍不到的地方。兩節合併是因為往返實驗正是「schema 是共識而不是觀察結果」最好的證明，拆開會失去力道。

**Day9 — 官方 semconv 之外：企業自訂事件就是 Signal Plane 的字彙表**
產出：仿 `payment-events.yaml` 為 demo-services 定義一組自訂事件並過 check；同時貼出 `signals/contracts.yaml` 裡實際依賴這些事件名的位置。
角度：這一天是治理篇跟 Signal Plane 之間真正的橋，也是全系列少數「先看後果、再回頭補原因」的日子。

兩個 repo 現況當證據。**一，`signals/weaver.py` 是靠 regex 撈散文欄位在做事**：registry 用慣用 dotted 形式宣告 `app.payment.charges.count`，程式實際 emit 的是 `payment_charges_total`，於是 `note:` 裡寫一句 "Current code metric: \`payment_charges_total\`"，模組再用 `_PROM_NAME_RE` 把它撈出來。契約層跟 schema 層之間的橋是一條 regex——**這就是自訂 semconv 只做到「宣告名字」、沒做到「宣告對應關係」的症狀**。**二，`contracts.yaml` 整份的錯誤偵測都掛在自訂 event 名上**（`http.request_failed`、`error_events`、`| event="..."` 的 Loki 選擇器），這些官方 semconv 都沒有。demo-services 的 log 甚至沒有 `level` 欄位（全是 INFO），要篩事件只能靠 `event=`——**agent 認得的字彙表就是這份自訂 semconv，它沒定義好，`dq.py` 跟 `context.py` 就沒有東西可以 ground**。

平台角度：官方 semconv 是別人替你決定好的共識，自訂那層才是你團隊真正要維護的資產，而維護成本落在誰身上（平台團隊出模板、產品團隊填欄位）決定了它會不會被真的填。伏筆：Day16 的 `dq.py` × `weaver.py` 會回來收這條 regex 橋。

**Day10 — 上下文的供應源頭：注入了不代表送達**
產出：裝 OTel Operator、把手寫 Collector Deployment 換成 CR、annotation 注入的 before/after trace、主動壓到 `OOMKilled`、`kustomization.yaml` 單一入口。
角度：三節。Operator pattern（CRD 宣告期望、controller 持續 reconcile、`Requeue` 那個選擇）→ 逐欄位對照 `-o yaml`，點出 `status.conditions` 是第一個「本來就存在但沒人拿去給 agent 用」的機器可讀訊號 → collector 被壓垮時資料悄悄變少、app 端完全看不到 exporter 錯誤，**agent 讀到的上下文是殘缺的而它不知道**。平台判準第一次明講：一個機制的成本會不會隨團隊數線性成長。GitOps 收尾接 Day19。

**Day11 — 命名漂移：用 Rego 擋下「人看得懂、機器看不懂」的屬性**
產出：三條逐步加難的 Rego 規則（camelCase／正規化後撞名／缺 namespace），實跑 9 個違規、exit 1。
角度：為什麼 code review 擋不住——review 看得到這個 PR 改了什麼，看不到系統目前已經有什麼。`weaver_checker` 放大：resolved schema → Rego `input` → Finding。三個實測修正（三級嚴重度在 check 階段不存在、`type` 只能是 `semconv_attribute`、CI 上要抓 `context.id`）。

**Day12 — 誰維護、誰負責演進：分層 registry 與 breaking change**
產出：base registry ＋ team registry 兩層、四個實測陷阱各一個可重現案例、兩條 `before_resolution` policy；三層驗證模型各跑一次；`registry diff` 對三種變更靜音的重現。

**這是全系列最長的一天（預估 3500-4000 字），刻意的，不算風險。** 兩節合併的理由是它們是同一個問題的兩半：分層回答「現在誰擁有這份上下文」，breaking change 回答「它變的時候誰負責通知」。分開講，所有權的敘事會斷在最關鍵的地方。

**前半——哪一層統一、哪一層放手。** 治理的難處不是「要不要統一」。四個實測陷阱全部保留：`registry_path` 綁 cwd 而不是 manifest 位置；重複定義不是覆寫而是製造一個沒人引用的孤兒（綠燈！）；依賴不遞移；把所有祖先都列出來又撞重複載入。前兩個是安靜的坑，用兩條 `before_resolution` policy 補起來——`before_resolution` 終於有場景了。

**後半——三層驗證模型，完整講，不壓縮。** `metric_requirement_level` 規格有但 weaver 兩個版本都 hard error（第一層，強制）／`--future` 讓同一句診斷從 ⚠ 變 ×（第二層，「CI 要不要加」是平台團隊替所有人做的排程決定）／`comparison_after_resolution` 自己寫規則（第三層，`input` 是新版、`data` 是 baseline，可按團隊分級）。工具升版本身也是 breaking change 的來源：前半那個「列出所有祖先」的解法在 0.23.0 上直接 panic、exit 134——**這兩節在這裡真的咬在一起，這也是合併的最好理由**。最後：**`registry diff` 對型別改變、`brief` 改動、enum member 移除完全靜音**，對 agent 來說這三種正好是最會改變推理結果卻無聲無息的變更。收尾回答前半的問題：deprecation 是宣告，不是通知。

**Day13 — 讓 registry 成為 agent 的工具：MCP**
產出：`mcp_probe.py` 不接 LLM 直接打 stdio JSON-RPC，八個 tool 全部驗一次；閉環跑三輪。
角度：實測是八個 tool 不是文件說的三個，分成發現／理解／驗證三種職責。閉環三輪：before → after（**還是紅的，因為把欄位搬到 span event 在 registry 眼裡是新增**）→ 定義出來才綠。**閉環的出口有兩個，一個是改程式碼，一個是改 registry**。`provenance.source` 回答 Day12 那個「agent 讀到哪一版」。

**Day14 — 機器可讀的意圖 ＋ codegen：讓錯誤從「可被檢查」變成「說不出來」**
產出：`compile_intent.py` 把意圖編譯成 PromQL；兩份故意寫壞的意圖 exit 1；template engine 生出型別安全常數與 `StrEnum`。
角度：三層對照（規則／門檻＋註解／意圖），兩種意圖（穩定狀態編成 alert rule、變更意圖編成部署後的驗證查詢）。`why`／`first_check` 直接搬進 alert annotations——**這是 agent 拿到告警時唯一的上下文來源**，Day24 那條 PUSH 路徑會真的用到它。`PaymentOutcome('DECLINED')` 直接 raise。收 Day12 的伏筆：生成物的 diff 補上了那三處靜音，所以生成物要 commit 進版控。

---

## Phase 3 — Signal Plane：讓上下文可推斷（Day15-17）

**Day15 — 拓撲：讓異常偵測順著圖走，而不是平鋪掃全部指標**
產出：`health.py` 加一個「用拓撲排序異常候選」的步驟，改前改後的候選順序對比。
角度：先講「過時的拓撲」這個反模式怎麼在真實團隊長出來，再講順著圖走跟平鋪掃描在雜訊量上的差異。附 `signals/` 模組基於真實 import 關係的資料流圖。

**Day16 — 對帳與降噪：宣告的拓撲 vs Tempo 真實 call graph**
產出：`reconcile.py` 擴充 edge 對帳（宣告但不存在／存在但未宣告）＋ 串 `discovery.py` 的 `list_service_names()` 變成可排程 ＋ `context.py` 降噪 before/after ＋ `dq.py` 串 `weaver.py`。
角度：三節。對帳讓「圖準不準」變成可量測 → 降噪處理「圖準了但太吵」 → `dq.py` × `weaver.py` 是全系列第一次讓治理篇跟 Signal Plane 的程式碼互相呼叫，enrichment 的前提是 schema 對齊。這裡收 Day9 那條 regex 橋：**如果自訂 semconv 有正式宣告 dotted name 跟實際 Prom name 的對應，那條 regex 就不需要存在。**

**Day17 —（概念＋同日落地）決策級遙測長什麼樣：CEL 四職責逐項打勾**
產出：更新 `signals/` 現況文件，逐一對照 enrichment/correlation/projection/grounding，標出 `context.py`/`topology.py` 各自落在哪一項、哪一項還缺。
角度：前半概念——CEL 三職責＋溯源，對照「傳統聚合遙測 JSON」vs「決策級遙測 JSON」的具體資料形狀差異。後半誠實打勾：projection 有沒有收斂成單一物件、有沒有 baseline、有沒有 confidence score。**概念 vs 實作現況的落差本身比任何完美案例都更有教學價值**，因為讀者接下來要做的正是補上這個落差。順帶承認「訊號斷崖」這個反模式目前沒有實例。

---

## Phase 4 — 評估與監控（Day18-22）

**Day18 — 不接 LLM 也能驗證：21 條斷言，其中 12 條預期 exit 1**
產出：`testability/regress.sh`，跑完不到十秒、零 LLM 呼叫。
角度：先列出這系列七個「壞掉時症狀是一切看起來很順利」的案例，得出**你要驗證的不是它會不會通過，是它還會不會擋**。四個做法：不接 LLM 驗證 MCP（歸因「agent 講錯」vs「registry 教錯」）、樣本從真實 span 抽而不是手打（這個做法抓出我自己文章裡的一個錯）、每條規則都要有一個「本來就該紅」的 fixture、先量一個基準。

**Day19 — 治理成為門：CI gate ＋ live-check 的兩個時間點**
產出：完整 workflow YAML ＋ 被擋下來的 PR 截圖 ＋ live-check 對真實流量跑一次。
角度：先界定「跑得出來」跟「繞不過去」（會自己跑／擋得住／說得清楚，分別落在 CI、branch protection、輸出格式）。**三個實測陷阱的共通點是都不會讓你看到錯誤訊息**（stderr vs stdout 讓 annotation 完全失效／`file=` 是目錄名且沒有 `line=`／resolver 錯誤只印一個空 `::group::`）——這是治理能不能規模化的分水嶺。後半 live-check：三級嚴重度終於登場、六種內建 advice 各對應前面某一天的坑、registry coverage 是「規範跟現實的距離」而不是合規率。兩個坑：預設 4317 吃到自己 coding agent 的遙測（含 PII）、`--advice-policies` 是覆蓋不是疊加。
放這裡的理由：它跟 Day18 的 `regress.sh`、Day20 的 `eval/harness.py` 是**同一件事的三個時間點**——commit 時擋、執行時擋、事後打分。

**Day20 — 把踩過的坑寫成 fixture：`eval/harness.py` 與回歸測試集**
產出：Day1、Day6、Day7 的失敗各寫成 fixture，跑一次完整 eval，貼分數表對照 Day1；**每筆 eval run 綁定當下的 system prompt hash 與模型 ID**（`calibration.py` 的 `record_run` 加欄位）。
角度：把 agent 犯過的錯變成 fixture 是投報率最高的一件事。系列結束後留下的不是一篇篇文章，是一個跑得動的回歸測試集——下次改 agent 邏輯，這些 fixture 會告訴你有沒有把舊坑踩回去。順帶收 histogram bucket 假象值（`*_duration_seconds` 記秒進預設 ms buckets → quantile 恆定 ~4.75）。

但回歸測試集有一個前提條件容易被忽略：**分數變好，是因為改對了邏輯，還是因為有人順手動了 prompt？** 沒有版本綁定，這個測試集能告訴你「退步了」，不能告訴你「為什麼」——歸因鏈在這裡是斷的。所以 fixture 之外要補的是身分：prompt 內容 hash、模型 ID，兩個欄位就夠。這個做法是從 Grafana Cloud AI Observability 學來的（他們把 system prompt 當內容定址的 artifact 版控，畫面上一個 agent 累積 15 個版本），差別是他們拿來看 dashboard，這裡拿來**讓 Day22 的校準數字可以按版本切開**——否則 CE 是一堆不同 agent 的分數混在一起算的。

**Day21 — LLM-as-judge：agent 說它有信心，不代表這個信心可信**
產出：`rubric.py` 兩個守門（trace ID 存在性驗證、k8s 寫入意圖檢查）各跑一次通過與不通過的案例。
角度：**分數本身也需要被驗證**。這是整個系列對「給分數」這件事最誠實的一次示範——判官也會錯，所以判官要有可被檢查的判準，而不是再問一次 LLM「你覺得對嗎」。

**Day22 — 校準誤差：`record_run` → `label_run` → `compute_calibration`**
產出：跑一次完整兩階段流程，算出真實 CE 數字，誠實報告目前累積幾筆標記樣本。
角度：兩階段設計為什麼必要——跑的當下記信心（還沒有 verdict）、事後補正確與否。CE 緩慢上升是認知漂移的早期訊號。這裡不是「缺口」，是「現況數字」。留一個問題給 Day27：**誰來 label？** 答案是 `/investigations/{fp}/label`，那天會把這條人工迴圈接上。

---

## Phase 5 — 部署進真實系統（Day23-30）

**這八天的驗收標準：讀者看完能把這套東西裝進自己公司。** 前四天把 agent 從「eval harness 呼叫的一個函式」變成「一個掛在 Grafana 告警後面、有授權邊界、有人能核准的服務」，後四天處理可回放、可上線、可衡量。

**既有程式碼的模式：導讀 ＋ 補一個真實缺口。** `governance.py`(181)、`breaker.py`(107)、`actions.py`(118)、`action_requests.py`(224)、`blast_radius.py`(218)、`audit.py`(70)、`execution.py`(614)、`calibration.py`(288)、`webhook.py`、`alerts.py`、`investigations.py` 全部已實作，`service/tests/` 下每一支都有對應測試。所以缺口不會是「沒寫」，是覆蓋邊界。**每一天動筆前要先實跑一次確認缺口真的存在**，不能憑猜——寫出來會變成假的建構里程碑。如果某一天實跑後發現沒有缺口，那天就改走「壓力測試日」：設計一組會觸發該機制的情境並真跑，產出是可重現的指令輸出而非 diff。

目前已知的候選缺口（待逐一驗證）：`audit.py` 只有 70 行、`execution.py` 有 614 行，執行路徑有多少真的被寫進紀錄（Day28，最有把握）；`governance.py` 的 calibration unproven 門檻是寫死還是可設定（Day25）；狀態機的 expired 路徑有沒有真的被排程觸發（Day26）；`blast_radius.py` 有沒有吃 `topology.py` 的圖還是自己另外查（Day25）。

**Day23 —（新增）把 agent 裝起來：部署、設定、憑證、權限**
產出：`docker-compose.yaml` / k8s manifest 真的跑起來一次；設定與憑證的來源與 fail-closed 行為；k8s 寫入需要的最小 RBAC；模型選擇與成本上限設在哪。
角度：主題描述的第一項就是「模型部署」，這天兌現它。四個問題按導入順序回答：**它跑在哪**（一個 FastAPI 服務，`/healthz` 之外還有十幾個 route，哪些該對外）、**它需要什麼**（LLM endpoint、Prometheus/Loki/Tempo 位址、Grafana 憑證）、**憑證壞掉會怎樣**（fail-closed：`alerts.py` 的設計是沒憑證就不提供寫入，不是靜默失敗）、**它能碰什麼**（k8s 寫入的 RBAC 最小權限，這是資安會問的第一個問題）。誠實寫出成本：一個沒有上限的 agent 在真實團隊裡活不過第一張帳單。

**Day24 —（新增）PUSH 入口：告警燒起來，agent 自己去查**
產出：Grafana alert rule → `/webhook/alert` → fingerprint 去重與 cooldown → 背景 headless RCA → Finding 推到 sink（log 一定、Grafana annotation 如果有設定）。真的燒一次告警，端到端錄下來。
角度：**這是整個系列離「導入公司」最近的一天，也是 agent 第一次不是被 eval harness 呼叫的。** 三個工程問題都是導入時第一天就會撞到的：

1. **webhook 不能等 LLM。** HTTP response 必須立刻回，調查在背景跑得比請求久——不這樣 Grafana 的 webhook 會 timeout 重送，然後你會有一堆重複調查。
2. **同一個告警會燒很多次。** fingerprint ＋ cooldown 視窗，否則 flapping 的告警會讓 agent 自己變成事故。這跟 Day26 的熔斷器是同一種思路的不同位置。
3. **結論要回到人看得到的地方。** Grafana annotation 讓調查結果直接貼在出事的那段時間軸上——**這是 agent 的輸出第一次進入既有的維運工作流，而不是躺在 log 裡。**

Day14 的 `why`／`first_check` 在這裡真的被用到：alert annotation 裡的那兩個欄位，就是 agent 拿到告警當下唯一的上下文。**意圖宣告在這天從「一個好想法」變成「調查品質的實際輸入」。**

**Day25 — 從估算到授權：`blast_radius.py` 與信任天花板**
產出：把 Day15-22 產出的診斷接進 `blast_radius.py`，輸出一個真實的影響估算（幾個 pod、有沒有跨 namespace）；`governance.py` 的判斷邏輯逐條讀，三種輸入各跑一次看它怎麼降級。
角度：兩件事本來就是一件——**影響範圍的估算，就是授權層級的輸入**。先講「估算 vs 執行」這條線為什麼非劃不可，再把授權光譜攤開（唯讀觀察→提議→可逆執行→有邊界不可逆→人類核准）。三條降級規則：irreversible→ESCALATE、confidence<low→ESCALATE、**calibration unproven→降級 PROPOSE**——最後這條直接接回 Day22：**沒有校準數字，agent 就不該被授權**，這是整個系列把「評估」跟「治理」綁在一起的那個扣環。

**Day26 — 執行護欄四件套：註冊表、狀態機、kill switch、熔斷器**
產出：`actions.py` typed 註冊表 ＋ `action_requests.py` 狀態轉移圖 ＋ 實測兩次 approve 撞在一起 ＋ `breaker.py` 兩種失效模式各觸發一次。
角度：proposed→approved/rejected/expired→executing→terminal，為什麼要 atomic compare-and-set（兩次 approve、或 approve 撞上 AUTO 路徑）。kill switch 預設關閉。熔斷器兩種失效模式：runaway（短時間內執行次數暴衝）與 flapping（同一個 target 連續失敗），熔斷後只能人工重置為什麼是必要而不是缺陷。回頭看 Day6 那個幻覺：**這次失敗是治理平面該攔卻沒攔，還是攔了但誤判？**
四件事併一天是因為它們是同一組護欄的四個層次（宣告什麼可以做／流程怎麼走／總開關／自動停損），拆開會讓讀者以為是四個獨立機制。

**Day27 —（新增）人到底在哪裡點那一下**
產出：Grafana plugin 那張帶按鈕的卡（agent 提議 alert rule → 人點 → `/alerts/provision` 寫進 Grafana）＋ `/actions/requests/{id}/approve|reject` 走一次完整核准 ＋ `/investigations/{fp}/label` 補上 Day22 缺的標記來源。
角度：前面兩天講了授權光譜跟狀態機，但**人在哪裡、按什麼、按錯怎麼救，一直沒有具體形狀**。這天給它形狀。

三個介面對應三種人在迴圈的模式：**迴圈之上**（平台團隊定義意圖與政策，週-月節奏）／**迴圈之中**（特定決策被路由給人審查——就是 plugin 那顆按鈕與 approve/reject）／**迴圈之上監看**（透過 SLO 與稽核軌跡監督，Day30 的事）。

`alerts.py` 的設計值得整段拆：**唯讀的推理核心永遠不寫入任何東西**，agent 只是產出一個 ` ```alert ` 圍欄區塊，plugin 把它渲染成卡片，**只有人點下去才會 POST 回去寫進 Grafana**。人在迴圈是結構性的，不是靠 prompt 叫模型「請先問使用者」。這跟 `actions_enabled`（自主變更，預設關閉）的差別也要講清楚：建立告警是人工確認且可逆的，所以用 fail-closed 憑證＋一個操作者可切換的開關來守，而不是整個鎖死。

**標記那一段接回 Day22**：CE 需要有人事後說「這次判斷對不對」，`/investigations/{fp}/label` 就是那個入口。導入公司時這是最容易被忽略、卻決定校準數字有沒有意義的一件事——**沒有人願意點那個按鈕，你的 CE 就永遠是空的。**

**Day28 — agent 自己的決策路徑能不能回放**
產出：盤點 `execution.py`(614) 的執行分支有多少真的被 `audit.py`(70) 寫進紀錄，補上缺的；把決策軌跡改成 OTel span 送進 agent 自己在查的那個 Tempo；重跑 Day1 那組 4.5/9 分的題目，這次看 trace 不看分數。
角度：**一個做可觀測性的 agent，自己的決策路徑不可回放會是雙重諷刺。** 三節：

1. **缺口盤點。** `audit.py` 只有 70 行而 `execution.py` 有 614 行，這個比例本身就是可疑的訊號。逐條走執行分支，標出哪些路徑（失敗、逾時、熔斷、ESCALATE）根本沒留下紀錄——**壞掉的時候最需要紀錄的那幾條路徑，往往正是沒寫的那幾條。**
2. **自製紀錄 vs OTel span：一個真的要做的決定。** `audit.py` 是一份自製格式的可回放紀錄，而 agent 自己整天在查的是 Tempo。這兩套東西沒有理由不合一——**改成 OTel span 之後，agent 可以用自己的工具調查自己**，也代表它的遙測要走跟前 14 天一樣的治理管線。`gen_ai.*` 是實作手段（它沒有定義 prompt 版本這種欄位，得自己補一組——這正好回收 Day9 那個「官方 semconv 之外，你自己要維護的那層」，只是這次治理對象是自己）。誠實寫出代價：多一組要維護的 attribute、多一份要跑 check 的 registry。
3. **重跑 Day1。** 不是看分數，是看 trace：哪一步燒掉預算、Day6 那個 fast-path 幻覺在 span 上長什麼樣。**Day1 的失敗第一次變成可以看的東西，而不是一個分數**——27 天前你只知道它錯了，27 天後你能指著 span 說它在哪一步開始編。順帶量一次 prompt token 佔總預算的比例，回答 Day6 留下的問題：不是模型不聽話，是 prompt 先把預算吃掉了。

隱私：agent 的 span 會帶使用者輸入。Day19 那個坑（live-check 跑預設 4317，把 coding agent 自己的 OTLP 遙測含 PII 吃進去）本質上就是這種遙測——那時是踩雷記錄，這天要當設計問題處理：哪些欄位進 registry、哪些不收。

**Day29 — 新服務上線 checklist：能力覆蓋率而不是合規率**
產出：`verify_onboarding.py`，13 項檢查、每一項都真的執行一次工具、失敗訊息包含下一步；兩個服務對照跑。
角度：在這個系列裡它不是「上線 checklist」，是**一個新服務要具備什麼條件，agent 才推理得動它**。`shipping-v0` 拿 9/13 但 `registry check` 是綠的——九項失敗全部落在「合法但不夠好」的區間。誠實記錄我自己的 checklist 有一個洞（`shippingStatus` 躲過 enum 檢查，因為它剛好也違反命名規則），得出**checklist 只會在壞掉的服務上顯現自己的 bug，所以壞掉的服務是測試資料而不是教材**。收尾：checklist 是清單不是門，前六項適合擋 PR，後面幾項是上線前的對話。

**Day30 — 端到端跑一次 ＋ 五大 SLO 成熟度定位 ＋ 正向循環圖**
產出：完整鏈路跑一次——**告警燒起來 → webhook → 自動調查 → Grafana annotation → 人在 plugin 上核准 → 執行 → 標記結果**，錄 demo 或逐步截圖；一張 30 天的正向循環圖。
角度：三節。端到端（這次是真的從告警開始，不是從 harness 開始）→ ARR/DQ-SLO/RL-SLO/AE-SLO/CE 各自定義與制衡關係（高 ARR 低 DQ = 不安全自主性；低 RL 低 AE = 決策倉促），**外加成本一項**（Day28 的 `gen_ai.usage.*` 讓每次調查的花費變成可查詢的指標），對照 L1-L5 誠實報告卡在哪一級 → 正向循環圖：治理→拓撲/CEL→agent 建議→執行/治理平面→校準/SLO→回饋治理與拓撲。結尾一段 CLL 封閉學習迴圈與知識管理三閉環當「讀者可以接手的下一步」，明確標成設計已寫好、尚未實作。
最後收在那句話上：**這些技術不是一堆很酷的東西，是為什麼非得按這個順序疊起來不可。**

---

## 已寫好的文章怎麼複用

現有 `ironman-2026/day01.md`–`day13.md` 大約 9 篇可以直接複用主體，主要工作是重寫開頭把「治理問題」的框架換成「agent 上下文問題」的框架：

| 現有 | 新編號 |
|---|---|
| day03 ＋ day04（Operator、注入） | Day10（合併） |
| day05（Weaver 上手） | Day8 ＋ Day9 |
| day06（命名漂移 Rego） | Day11 |
| day07（CI gate ＋ live-check） | Day19 |
| day08（分層與所有權） | Day12 前半 |
| day09（breaking change） | Day12 後半 |
| day10（MCP） | Day13（四個坑中的兩個移到 Day4） |
| day11（意圖 ＋ codegen） | Day14 |
| day12（可測試性） | Day18 |
| day13（上線 checklist） | Day29 |

## 沒收進來的

- GitOps 獨立成天（併進 Day10 收尾一節）
- Collector 三種部署模式效能實測（換了關注點，接不上主軸）
- CLL 封閉學習迴圈與知識管理三閉環的完整實作（Day30 當開放式結尾提一段）
- `/traces/{id}/analysis|chat` 那條以 trace 為中心的追問路徑（Day5 提到訊號跳轉時帶一句，不獨立成天）

## 字數風險

**Day12 是刻意的長天**（3500-4000 字，四個陷阱與三層驗證模型都完整保留），不算風險。

真正的風險是 Day10、Day14、Day24、Day30。寫不完時 **Day10 的 GitOps 那節最好切**，可以獨立成一篇非參賽補充文，正文留連結，不影響因果鏈。其他幾天的內容互相咬著，切了會斷。
