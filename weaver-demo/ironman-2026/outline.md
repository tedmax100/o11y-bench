---
title: "2026鐵人賽_Outline v4"
tags: 2026鐵人賽
date: 2026-07-24
---

# 【AIOps with OpenTelemetry】

## 前言

這個系列橫跨 43 天、分兩段（Series 1 三十三天、Series 2 十天），走完你會得到三個層次的東西，由淺到深：

**一套可以直接搬回團隊用的治理工程能力。** Series 1 的 Day1-17 走完，你手上會有：能講清楚 Operator CRD 在幹嘛的知識、一份可執行的 CI Gate workflow、一份新服務上線 checklist。這是最實際的產出——別人問「怎麼在我們團隊落地 OTel 治理」，你有真的跑過一遍、有截圖有 diff 的答案，不是轉述文件。

**對「可觀測性資料要怎麼設計才能被 agent 用」這個問題,一個誠實而非行銷式的答案。** 這是這次系列真正的價值所在。Series 1 的 Day18-25 會親手證明「決策級遙測」在這個系統裡目前只做到哪一步——`context.py` 有 enrichment/correlation，但 projection 有沒有收斂成單一物件、有沒有 baseline、有沒有 confidence score，Day24-25 會逐項對照 CEL 的三個職責誠實打勾或留白，而不是照搬書上一個漂亮 JSON 範例就宣稱做到了。這種「概念 vs 實作現況」的落差本身，比任何完美案例都更有教學價值——因為讀者接下來要做的事，正是補上這個落差，這個系列等於幫他們畫好了地圖。這份誠實還有第二層：整個系列刻意分成兩段,而不是把「agent 能給建議」跟「agent 能自主執行、能自我學習」硬塞進同一段——這個分段本身,就是「概念 vs 現況」誠實態度的延伸,承認後者需要治理平面、校準機制都到位,不是一段就能誠實交代完的東西。

**一套可重複驗證、不會退化的 agent 品質保證機制。** Series 1 Day28 把真實踩過的坑（discover-before-query 失敗、RCA 只拿 2/9 分)寫成 eval fixture；Series 2 Day8 和 Day10 再把預算壓力下的 fast-path 幻覺、histogram bucket 假象值兩個坑也收進來。這代表系列結束後留下的不是一篇篇文章,而是一個 `eval/harness.py` 跑得動的回歸測試集。下次改 agent 邏輯,這些 fixture 會告訴你有沒有把舊坑踩回去——這是比「寫文章記錄教訓」更持久的資產。

如果要用一句話收斂:你會學到——治理(Operator/Weaver)、資料設計(Signal Plane/CEL)、agent 決策、evaluation 這四件事不是四個獨立主題,而是一條因果鏈:治理不好 → 資料不可信 → agent 決策依據是假的 → eval 分數低。Series 1 的 Day33 先畫一次「正向循環」的前半段(治理 → 拓撲/CEL → agent 建議),Series 2 的 Day10 把後半段(執行/治理平面 → 校準/SLO → 回饋治理與拓撲)接上去,合起來才是這整個系列真正要教的東西——不是「這裡有一堆很酷的技術」,而是「這些技術為什麼非得按這個順序疊起來不可」。這個因果鏈的直覺,才是 43 天寫完後最值錢、也最難從單篇文章學到的東西。

---

## 改版說明（給自己看的修訂記錄）

### v3：這版改了什麼、為什麼

v2 把 ARE（《代理式可靠性工程》）的四平面架構當成貫穿全系列的顯性骨架，理論密度偏重、而且把系列的終點放在「agent 能不能自主執行、能不能學習」——這需要 governance.py 的信任天花板、calibration.py 的校準誤差、CLL 封閉學習迴圈全部到位，30 天講不完，硬塞會變成每天都在鋪陳一個 30 天後才收斂的概念，讀者中途很難有「這篇單獨看也有收穫」的感覺。

v3 的調整：

1. **主軸換回 OTel 本身，AIOps 是疊加的一層，不是终點。** Phase 1 直接吃 [weaver 官方 docs 目錄](https://github.com/open-telemetry/weaver/tree/main/docs) 裡原本沒用到的素材（`architecture.md` 的 crate 拆解、`usage.md` 的完整指令表、`registry.md`/`define-your-own-telemetry-schema.md` 的 manifest 規格、`validate.md` 的 Finding 結構、`weaver-config.md` 的 template engine、`specs/multi-registry`），內容量明顯變厚，而且都是 OTel/Weaver 本身的知識，不需要讀者先接受一套 ARE 的架構語言才看得懂。
2. **AIOps 的目標明確縮小到「能不能組出讓 agent 推理的豐富事件、給診斷、給信心分數、給下一步建議」，不做「能不能自主執行、能不能自我學習校正」。** 這代表 Phase 3（agent 篇）**刻意不深入** `governance.py` 的信任天花板、`calibration.py` 的校準誤差、CLL——這些留到下一個系列。Phase 3 只做到：Signal Plane 出的決策級事件 → agent 讀進來產生假設 → `rubric.py` 這類機制幫這個假設打分/驗證 → `blast_radius.py` 的唯讀乾跑當「下一步建議」的具體示範（估算範圍，但不執行）。
3. **原本 v2 Phase 3 後半那些「治理平面/校準/CLL/五大SLO」的內容沒有丟掉，搬去一個獨立的、接續的 10 天系列**，明確定位成「上一個系列做出了『建議』，這個系列處理『這個建議準不準、能不能治理、能不能自我校正』」。兩個系列可以獨立閱讀，也可以接續著看。

### v3.1：追加四個「純概念、不碰程式碼」的日子

盤點下來，有四塊內容原本混在「概念 + 動手做」同一天裡，值得拆成獨立的純概念日——先把名詞、模型講完整，動手做的那天才不用邊做邊定義新東西：

1. **AIOps 基礎概念**（新增 Day2）——這系列從沒有一天正面回答過「AIOps 到底是什麼、不是什麼」，都是直接跳進 Operator/Weaver。補一天在 Day1 反面教材之後，先把「這系列要往哪走」的地圖畫出來。
2. **OTel Operator 基礎概念**（新增 Day3，原 Day2 的動手做部分變成 Day4）——原 Day2 把 Operator pattern 的概念（CRD/controller/reconciliation loop）跟 `kubectl get -o yaml` 的動手拆解混在一起，拆開後兩天各自更聚焦。
3. **Weaver 基礎知識**（新增，原動手做部分順延）——原本把 `architecture.md` 的 crate 拆解（純概念）跟第一次 `weaver registry check`（動手做）混在一起，同樣拆開。
4. **Agent 基本組成（LangGraph）**（新增，Phase 3 原本直接從「agent.py 決策鏈梳理」開始的那段整段順延一天）——Phase 3 原本直接從「agent.py 決策鏈梳理」開始，預設讀者已經懂 ReAct/tool-calling/LangGraph 的 StateGraph/node/edge 模型。這個 repo 的 `agent.py` 本身就是用 LangGraph 的 `StateGraph` 建的（`agent`/`tools`/`force_answer`/`rubric_trace` 四個 node，一個 `add_conditional_edges` 決定要不要重試），值得先用泛用範例把 LangGraph 的模型講清楚，再回頭對照這個 repo 的圖。

四天都新增，Series 1 從 30 天變成 34 天，Series 2 維持 10 天不變，總天數 40→44 天。

### v4：把「Collector 部署模式實測」併回 Day5，天數 44→43

實際動筆到 Day6（原規劃：sidecar/daemonset/gateway 三種部署模式的 CPU/記憶體/延遲實測，再主動調低其中一種模式的 resource limit 到它開始丟資料）時發現這天跟系列主軸接不上——這系列的骨幹是治理（命名/schema/CI gate/意圖宣告）跟資料可信度，三種部署模式的效能比較其實是換了一個關注點（SRE 容量規劃），前後文銜接不上，讀者會覺得「怎麼突然在講效能」。

真正跟主軸有關、值得留的，只有「collector 被壓垮 → 資料悄悄變少 → 靠指標對帳才發現」這條排查邏輯——它呼應系列反覆強調的主題（可觀測性系統本身的健康也要被觀測），也是 Day18-25（Signal Plane 資料可信度）的伏筆。於是把這段 OOMKilled 排查案例直接併進 Day5（annotation 注入那天），拿掉三模式效能比較的部分，原本的 Day6 整篇刪除。

連帶影響：Series 1 的 Phase 1 從 18 天變成 17 天（Day1-17），Phase 2/Phase 3 天數不變但整體往前遞補一天（Day18-25、Day26-33），Series 1 總天數 34→33 天，Series 2 維持 10 天不變，總天數 44→**43 天**。

---

## Weaver docs → 系列天數 對照表

| Weaver 文件 | 內容 | 對應天數 |
|---|---|---|
| `docs/architecture.md` | crate 拆解：weaver_semconv/resolver/checker/forge/live_check/mcp，plugin 願景 | **Day7**（純概念） |
| `docs/usage.md` | 完整 CLI 指令表：check/generate/diff/emit/stats/json-schema/infer/package/live-check/mcp/completion | Day7（速查表）、Day8、Day9、Day14 |
| `docs/registry.md` | Registry 是什麼、group 類型(metric/span/event/attribute_group)、`ref`/`extends` 重用機制 | Day13 |
| `docs/define-your-own-telemetry-schema.md` | `manifest.yaml`、`dependencies`、`schema_url`、10 層深度限制、自訂 registry 路徑格式(本地/git/GitHub release) | Day13 |
| `docs/validate.md` | Rego policy、Finding 結構(id/message/level/context/signal_type) | Day10、Day11 |
| 三級嚴重度(information/improvement/violation) | **實測修正**：這是 `live-check` 的 advice 系統，不是 `registry check` 的 policy。check 階段只有 `deny` 會被收集、`level` 恆為 `violation`、`signal_type`/`signal_name` 恆為 `null` | Day10（說明為什麼沒有）、**Day12**（實際展開） |
| `docs/weaver-config.md` | `weaver.yaml` 設定：template_syntax/comment_formats/templates(filter+application_mode)、設定檔載入順序與優先權 | Day16 |
| `docs/codegen.md` | Jinja template + JQ filter 生成文件/型別安全建構子的流程 | Day16 |
| `docs/schema-changes.md` | `diff` 的變更分類（added/renamed/updated/obsoleted/removed） | Day14 |
| `weaver registry mcp`（來自 usage.md + 官方部落格） | 內建 search/get/live_check 三個 MCP tool，讓 LLM 直接用自然語言查 registry | **Day15**（AIOps 軸線第一次具體登場） |
| `weaver registry infer` | 從 OTLP 訊息反推、產生 schema 草稿 | **Day9**（呼應 Day1 的反面教材） |

---

# Series 1（33 天）：OTel 治理 + 讓 AI agent 能推理的決策級事件

## 第一階段：治理基礎建設（Day1-17）

- **Day1** 起手式：未治理的示範服務——故意示範命名壞味道（`userId` 混 `user.id`、span name 沒語意），講這種服務在真實團隊裡怎麼長出來的。全系列的反面教材，後面每天都會回頭對照它。
- **Day2**（純概念）AIOps 基礎概念——這系列第一次正面回答「AIOps 是什麼、不是什麼」：不是「裝一個 AI 幫你看 dashboard」，也不是自動化腳本外面包一層聊天介面。核心問題是可觀測性資料通常是為人設計的（給人看的圖表/告警閾值），machine consumer（agent/LLM）拿來推理時往往缺語意、缺情境、缺信任度。對照傳統規則式告警（靜態閾值/固定規則）跟這系列要走到的「決策級事件 + 信心分數 + 下一步建議」；同時先劃線：這系列的 AIOps 範圍刻意只做到「讓 agent 讀懂、給判斷、給建議」，不做「自主執行」「自我學習」（那是 Series 2 的事）。這天結束時給一張「接下來 31 天的地圖」。
- **Day3**（純概念）OTel Operator 基礎概念——Kubernetes Operator pattern 是什麼：CRD 宣告期望狀態、controller 持續 reconcile 讓實際狀態逼近它，這跟一般人熟悉的「執行一次性 kubectl apply」有什麼不同。講清楚為什麼 OTel 需要一個 Operator 而不是手動貼一堆 YAML（collector 設定、sidecar injection、SDK 版本管理都是「持續調和」的需求，不是一次性部署）。畫一張圖：`OpenTelemetryCollector` CR（部署一個 collector 實例）跟 `Instrumentation` CR（定義 auto-instrumentation 注入規則，給 admission webhook 拿來改 Pod spec）兩者的分工，這天完全不碰真實 cluster，只建立詞彙跟心智模型。
- **Day4** 安裝 OTel Operator，拆解 CRD 實作——回到 Day3 的那張圖，實際 `kubectl get otelcol,instrumentation -o yaml` 逐欄位對照，指出哪些欄位是「部署行為」、哪些是「注入行為」。
- **Day5** annotation 做 auto-instrumentation——不改代碼就能覆蓋，前後 trace 對比，誠實講覆蓋不到的地方（自訂 business span 還是要手動加）；額外收錄兩段延伸：一段是公司多語言環境（Java/PHP-FPM）的 sidecar 注入案例，證明 annotation 驅動注入不是 Python 專屬技巧；另一段是主動把 collector 壓到 `OOMKilled`，示範「annotation 注入了不代表資料穩定送達」——collector 本身也可能是那個沒被觀測到的東西，這條線會在 Day18-25 講資料可信度時再接上。
- **Day6** Operator 設定轉 GitOps——CRD 從 `kubectl apply` 改成可 PR review 的 Helm/Kustomize 檔案，講 review 這類 YAML 該看什麼（會不會讓某服務突然沒有 trace，而不是語法對不對）。
- **Day7**（純概念）Weaver 基礎知識——先回答「為什麼 telemetry 需要 schema」：不是資料庫 schema，是「這個 span/metric/attribute 叫什麼、代表什麼、必不必填」的團隊共識；semantic convention 就是這種共識，registry 是把一堆相關 conventions 組織起來、可被驗證/查詢/生成文件與程式碼的容器。用 `architecture.md` 拆 Weaver 的 crate 畫一張定位圖：`weaver_semconv`（資料模型）、`weaver_resolver`（解析 extends/ref 繼承關係）、`weaver_checker`（跑 Rego policy 驗證）、`weaver_forge`（套 Jinja template 生文件/程式碼）、`weaver_live_check`/`weaver_mcp`（兩個「跟外部系統對話」的介面），再用 `usage.md` 列一張完整 CLI 指令速查表（`check/generate/diff/emit/stats/json-schema/infer/package/live-check/mcp`）——這天不跑任何指令，是下一天的地圖。
https://opentelemetry.io/blog/2025/otel-weaver/
- **Day8** Weaver 上手：第一次 `weaver registry check`——回到 Day7 的速查表，對 Day1 的服務跑第一次 check，貼真實違規輸出，逐條對照輸出格式跟 Day7 講的 crate 分工（是 `weaver_resolver` 先解析、還是 `weaver_checker` 在報錯）。
- **Day9** `weaver registry infer`：從 Day1 那支亂長服務的 OTLP 流量反推一份 schema 草稿——治理不是只能從一張白紙手寫 schema，也可以先用 `infer` 生成起點，再人工修正欄位命名/型別/required 與否；順便講清楚「自動生成的草稿」跟「團隊審過的規範」之間還差什麼審查。
- **Day10**（已寫）命名漂移，用 Rego policy 抓出來——先講清楚命名漂移為什麼靠 code review 擋不住（review 看得到這個 PR 改了什麼，看不到系統目前已經有什麼），再把 `weaver_checker` 這一格放大：resolved schema → Rego `input` → Finding。三條逐步加難的規則（camelCase／正規化後撞名 `userId <-> user_id`／缺 namespace），實跑 9 個違規、exit 1，順便把 Day8 欠的 Rego 語法（`[_]` 迭代、集合收集、`a < b` 去對稱重複）還掉。**三個實測修正**：(1) 三級嚴重度在 check 階段不存在，只有 `deny` 會被收集，改用規則名稱分級一個 Finding 都不產生——那套屬於 live-check 的 advice，移到 Day12；(2) violation 物件的 `type` 只能是 `semconv_attribute`，寫別的值整份 policy 檔被拒絕且錯誤訊息誤導；(3) Rego 物件到 Finding 的欄位是錯位的（你寫的 `type` 變成 Finding 的 `id`，整個物件變成 `context`），CI 上要抓 `context.id`。
- **Day11** weaver check 進 CI Gate——完整可貼上用的 GitHub Actions workflow，搭配 `--diagnostic-format gh_workflow_command` 讓違規直接變成 PR 上的 annotation，附一個真的被擋下來的 PR 截圖。
- **Day12** weaver live-check 接上 collector——補 CI 的盲點（靜態檢查看不到 runtime 才出現的違規），附 port collision 踩坑記錄（預設 4317 意外吃到自己 coding agent 的 OTLP 遙測，裡面有 PII——別用預設 port）。**這天要接住 Day10 移過來的三級嚴重度**：`--advice-policies` 的 advice 系統才是 `information`/`improvement`/`violation` 真正生效的地方，也是 Finding 的 `signal_type`/`signal_name` 會被填上的地方（check 階段恆為 `null`，因為靜態定義沒有「哪一筆遙測」這個概念）。
- **Day13**（合併自訂 semconv ＋ multi-registry）從零定義一組 `payment-events.yaml` 過 check，再疊一層 team-specific registry 在 base 之上——用 `registry.md` 的 group 類型/`ref`/`extends` 講屬性怎麼設計不重複定義，用 `define-your-own-telemetry-schema.md` 的 `manifest.yaml`/`dependencies`/`schema_url`/10 層深度限制講多團隊分層會撞在哪、Weaver 怎麼解。
- **Day14** 重現一次真實 breaking change——weaver 0.23.0 對合法欄位 hard error 的真實踩坑，講清楚三層驗證模型（always-error/future-gated/info）與 `weaver registry diff` 的變更分類（added/renamed/updated/obsoleted/removed），升級前該怎麼測。
- **Day15** `weaver registry mcp`：讓 AI agent 直接用自然語言查 registry——這是全系列第一次把 Weaver 跟 AI agent 具體接在一起。內建 `search`/`get`/`live_check` 三個 MCP tool，示範用 coding agent 問「這個 service 該用哪個 attribute 記付款金額」、以及讓 agent 對一段沒過 CI 的程式碼自動改到符合規範。這天是全系列 AIOps 軸線正式登場的起點：治理資產（registry）本身變成一個 agent 可以呼叫的工具，而不是一份人看的文件。
- **Day16** 概念日：機器可讀的「意圖」——三個對照範例（日常營運意圖 vs CPU 門檻規則、變更意圖 YAML、穩定狀態意圖 YAML），核心論點是意圖要能被 pipeline/agent 直接消費；順便用 `weaver-config.md`／`codegen.md` 補一段「schema 不只給人看、給 CI 擋，也能透過 template engine 生成型別安全常數」，讓「意圖機器可讀」這件事有具體的程式碼收口。
- **Day17** 治理環境收尾：新服務上線 checklist——可執行 checklist（CI job 範本、registry 範本），新增「服務是否宣告意圖」「registry 是否有對應 MCP 可查」兩欄。

## 第二階段：AIOps 核心能力管線（Day18-25）

- **Day18** 讀現況：畫出 signals 模組實際資料流——基於真實 import 關係的架構圖，對照 AIOps 九宮格，標出 `topology.py`/`context.py`/`compile.py` 各自落在哪幾格。
- **Day19** 補 edge 對帳：拓撲圖對真實 Tempo call graph 做驗證——這是「過時的拓撲」這個可觀測性反模式的具體示範：擴充 `reconcile.py`，輸出「宣告但不存在」與「存在但未宣告」的邊。
- **Day20** discovery 清單餵進 reconcile——串 `tools/discovery.py` 的 `list_service_names()`，做成可排程腳本，讓拓撲對帳從「手動跑一次」變成有資料源可以定期跑。
- **Day21** dq.py 串 weaver.py：schema 對齊檢查串進 Signal Plane——把第一階段的治理成果真正接進管線，而不是兩條平行線；這是全系列第一次讓兩個階段的程式碼互相呼叫。
- **Day22** context.py：把 edge reconcile 的噪音降下來——「訊號洪流」反模式的具體示範，處理「圖準了但太吵」的問題，before/after 的 context 輸出對比。
- **Day23** health.py：異常偵測順著圖走——改前改後的異常候選判斷順序對比，講「順著圖走」跟「平鋪掃全部指標」在雜訊量上的差異。
- **Day24** 概念日：情境豐富層（CEL）——CEL 三職責（enrichment/correlation/projection）+ 溯源(grounding)，對照「傳統聚合遙測 JSON」vs「決策級遙測 JSON」的具體資料形狀差異。
- **Day25** 收尾：s1-s4 邊界對照 CEL 三職責——逐一對照 enrichment/correlation/projection/grounding，標出哪一項還缺，順便誠實補一段「訊號斷崖」這個反模式目前系列沒有實例。

## 第三階段：讓 agent 讀懂決策級事件、給分數、給下一步建議（Day26-33）

這階段刻意**不**碰治理平面的信任天花板、不碰校準誤差、不碰「能不能學習」——那是下一個系列的事。這裡只回答：agent 拿到 Phase 2 產出的豐富事件之後，能不能做出一個有信心分數、有下一步建議的判斷。

- **Day26**（純概念）Agent 基本組成：LangGraph 是什麼——從「一個 LLM 能做什麼」到「一個 agent 能做什麼」的落差：單純一問一答 vs. 需要「觀察→決策→行動」反覆循環（ReAct：reason→act→observe→再 reason），tool calling 是這個迴圈的骨架。講清楚為什麼需要一個「圖」而不是一個 while 迴圈——狀態要在多輪之間傳遞、需要條件式分支（要不要重試、要不要強制作答）、需要可以中斷/恢復。介紹 LangGraph 的核心模型：`StateGraph`（狀態的型別）、node（一個步驟，可能是 LLM 呼叫也可能是純函式）、`add_edge`/`add_conditional_edges`（下一步去哪，固定或依狀態判斷）、checkpointer（讓調查可以中斷後接著跑）。用泛用範例講完後，預告這個 repo 的 `agent.py` 本身就是用這套模型建的——`agent`/`tools`/`force_answer`/`rubric_trace` 四個 node，先給讀者看一眼這張圖但不逐行拆，下一天才真正對照。
- **Day27** agent 決策鏈梳理——回到 Day26 那張泛用的 LangGraph 圖，把 `agent.py` 真實的 `StateGraph`（`agent`/`tools`/`force_answer`/`rubric_trace` 四個 node、`add_conditional_edges` 決定要不要重試）對回 discover→query→hypothesize→verify 這個決策鏈，標出「哪一步開始讀 Phase 2 產出的決策級 context」這個入口點。
- **Day28** 重現一次 discover-before-query 失敗案例並修——真實踩過的坑（RCA 只拿 2/9 分，因為硬編碼 schema 假設、沒先 discover 就直接查），寫成 regression case 進 `eval/fixtures.yaml`。
- **Day29** tools/query.py：修一個真實 API 怪癖——Prom metadata 為空、Loki label 需要時間範圍、Tempo dotted tags，這些是直接連接 API 才會踩到的坑，補防呆並寫測試。
- **Day30** 從診斷到分數：agent 的假設怎麼被驗證——`rubric.py` 的兩個 LLM-as-judge 守門（trace ID 存在性驗證、k8s 寫入意圖檢查），證明「agent 說它有信心」不代表「這個信心可信」——分數本身也需要被驗證，這是這系列對「給分數」這件事最誠實的一次示範。
- **Day31** 下一步建議：唯讀乾跑當作建議的具體形狀——把 Day27-30 產出的診斷接到 `blast_radius.py` 的唯讀乾跑（不執行，只估算一個動作會影響幾個 pod、有沒有跨 namespace），示範「下一步建議」長什麼樣子；明確劃線：這系列只做到「估算＋建議」，「是否可以自主執行」留給下一個系列的治理平面。
- **Day32** agent 自身可觀測性：決策有沒有被 trace——檢查 `audit.py`/`execution.py` 是否把每個工具呼叫寫進可回放的紀錄，一個做可觀測性的 agent，自己的決策路徑不可回放會是雙重諷刺。
- **Day33** Series 1 收尾：跑一次端到端 demo——治理 checklist → 意圖宣告 → 拓撲/CEL → agent 診斷＋信心分數＋下一步建議，完整跑一次；結尾預告 Series 2 要處理「這個建議準不準、能不能自我校正、能不能授權自主執行」。

---

# Series 2（10 天）：治理成熟度與學習迴圈——建議之後呢？

這個系列的前提：Series 1 做出的是「唯讀建議」，這裡處理三件事——這個建議能不能被授權自主執行（治理平面）、agent 說的信心準不準（校準）、系統能不能從過去的對錯中變聰明（CLL）。這裡才真正需要 ARE 的四平面架構語言，因為這正是它要解決的問題。

- **Day1** 概念日（獨立一天，純結構、不碰程式碼）：ch06 代理式可靠性架構全貌——這天的目的是先把書的結構性語言講完整，讓 Day2 之後的每一天都是「填一格」而不是「邊做邊定義新名詞」。建議拆成五個段落，剛好對應五張圖：
  1. **四平面總覽圖**——Signal/Reasoning/Execution/Governance 四個方框＋箭頭，標出 ARE §4.2 的「正交性」原則（失效不自動波及）；同時把 Series 1 已經蓋好的部分（Signal=Series1 Phase2 訊號平面、Reasoning=Series1 Day27-30 推理平面的雛形）標成「已完成」，Execution/Governance 標成「這系列要蓋」——這張圖同時是總覽也是進度條。
  2. **平面間的契約介面圖**——不畫平面內部細節，只畫「平面之間傳的是什麼格式」：訊號平面→推理平面傳的是**訊號契約**（§4.3：name/version/owner/支援決策/新鮮度保證/最小觀測視窗/信心門檻/排除條件/schema，對應 Series 1 的 `signals/contract.py`）；推理平面→執行平面傳的是**候選行動**（§4.4：proposal id/觸發訊號/領先假設/排序後的行動選項/意圖對齊/風險/信心/所需授權層級，對應 Series 1 Day27-31 `agent.py`/`investigations.py`/`rubric.py` 的輸出，這裡第一次把書的完整詞彙貼回那幾天做出來的東西）；執行平面的行動本身是**行動契約**（§4.5：意圖/前置條件/執行邏輯/爆炸半徑限制/自動逆轉/成功標準/結果訊號，對應這系列 Day2-3 要拆的 `actions.py`/`blast_radius.py`/`action_requests.py`）。三個契約畫成三個介面框，箭頭方向就是資料流方向。
  3. **授權層級與人在迴圈圖**——治理平面的分級授權（唯讀觀察→提議→可逆執行→有邊界不可逆→人類核准，§4.6）畫成一條光譜；旁邊疊一張人在迴圈的三種模式圖（§4.7：迴圈之上＝定義意圖與政策，週-月節奏；迴圈之中＝特定決策被治理平面路由給人審查；迴圈之上監看＝透過 SLO 與稽核軌跡監督整體運作），誠實註明「這系列的 repo 目前只有『迴圈之上監看』被 eval/harness.py 撐起來一部分，『迴圈之中』的審查介面還是設計層次」。
  4. **參考架構時間軸圖**——書裡 §4.8 用一個結帳服務延遲事件走了一次 t=15s 訊號偵測→t=18s 推理提案→t=22s 治理核准→t=23s 執行實施→t=180s 結果驗證的完整時間軸。這裡不用書的案例，改用這個 repo 自己的一個真實/半真實事件（例如把「2/9 分事件」或某個 payment 相關 bench task 改編成同樣節奏的假設性時間軸），畫一條時間軸，每個時間點標上四平面裡對應哪一支檔案「應該」被觸發——並誠實加註：這是示範性重演，不是 repo 目前真的自動跑得動的時間軸，因為 Day2-4 才要真正把執行/治理平面接上去。
  5. **成熟度定位圖（預告）**——畫一次 L1-L5 的階梯（§4.9），在階梯上標出這個 repo 現在大概卡在哪一級（多半是 L2：訊號標準化、有諮詢式建議，但還沒有被授權的自主寫入），細節留給 Day9 展開，這裡只是先讓讀者知道「後面幾天在往上爬哪一段階梯」。

  這五段講完，Day2 開始就可以直接說「現在來實作契約介面圖裡的第三個框」，不用重新鋪陳。
- **Day2** 執行平面：`actions.py` 註冊表 + kill switch——把 Series 1 Day31 的「唯讀建議」升級成「一個可以被授權的行動」需要什麼：typed 註冊、reversible 標記、approval 標記、預設關閉的 kill switch。
- **Day3** 執行平面：`action_requests.py` 狀態機——proposed→approved/rejected/expired→executing→terminal，畫一次完整狀態轉移圖，講清楚為什麼要用 atomic compare-and-set 防止兩次 approve 或 approve 撞上 AUTO 路徑。
- **Day4** 治理平面：`governance.py` 把「信任天花板」寫成程式碼——逐條讀 Autonomy 判斷邏輯（irreversible→ESCALATE、confidence<low→ESCALATE、calibration unproven→降級 PROPOSE），這是 L2→L3 轉換需要四項機制同時就緒這句話最字面的實現。
- **Day5** 治理平面：`breaker.py` 熔斷器——runaway（短時間內執行次數暴衝）與 flapping（同一個 target 連續失敗）兩種失效模式，講「熔斷後只能人工重置」這個設計為什麼是必要的，而不是可以自己恢復。
- **Day6** 校準誤差：`calibration.py` 的兩階段設計——`record_run`（跑的當下記信心，還沒有 verdict）→ `label_run`（事後補正確與否）→ `compute_calibration`，跑一次完整流程算出真實 CE 數字。
- **Day7** 封閉學習迴圈（CLL）+ 知識管理三閉環——把 Day6 串成 ARE 的 CLL 五步驟（擷取預測→觀測結果→比較偏差→提議更新→治理路徑套用），並介紹 `doc/aiops-agent-knowledge-loop.md` 現有的三個設計（過去案例注入、investigation→draft runbook 合成、runbook feedback）——標明這是「設計已寫好、尚未實作」，是這系列讀者可以接手的下一步。
- **Day8** 重現一次預算壓力下的幻覺並加防護——tight budget 下的 fast-path/trace-id 幻覺，呼應 Day4-5 的 ESCALATE/熔斷機制：這次失敗是治理平面該攔卻沒攔，還是攔了但誤判。
- **Day9** 概念日：五大旗艦 SLO + 成熟度模型總覽 → Rubric 落地——ARR/DQ-SLO/RL-SLO/AE-SLO/CE 各自定義，直接把整個 repo 對應回 L1-L5 成熟度表，現在卡在哪一級；用 `calibration.py` 的實測結果誠實報告目前累積了幾筆標記樣本、CE 離「proven-good」還差多遠（不是「缺口」，是「現況數字」）。
- **Day10** 總收尾：兩個真實 bug 復盤 + 43 天正向循環圖——2/9 分事件、histogram bucket 假象值各寫成 eval fixture；畫出橫跨 Series 1+2 全部 43 天的正向循環：治理→拓撲/CEL→agent建議→執行/治理平面→校準/SLO→回饋治理與拓撲，作為下一輪迭代的起點。

---

## 兩個系列怎麼安排

- 兩個系列可以獨立完賽：Series 1 單獨結束在「agent 給出有信心分數的下一步建議」，是一個完整的故事，不需要讀者知道 Series 2 存在。
- Series 2 開頭 Day1 那張四平面對照圖，直接把 Series 1 的產出（Phase2 的 Signal Plane、Day27-30 的 Reasoning Plane）標成「已完成」，只有 Execution/Governance 是這個系列的新內容——避免 Series 2 讀者覺得需要重看一次 Series 1 才懂。
