---
title: "2026鐵人賽_Outline v6"
tags: 2026鐵人賽
date: 2026-07-24
---

# 【AIOps with OpenTelemetry】

## 前言

這個系列橫跨 40 天、分兩段（Series 1 三十天、Series 2 十天），走完你會得到三個層次的東西，由淺到深：

**一套可以直接搬回團隊用的治理工程能力。** Series 1 的 Day1-13 走完，你手上會有：能講清楚 Operator CRD 在幹嘛的知識、一份可執行的 CI Gate workflow、一份新服務上線 checklist。這是最實際的產出——別人問「怎麼在我們團隊落地 OTel 治理」，你有真的跑過一遍、有截圖有 diff 的答案，不是轉述文件。

**對「可觀測性資料要怎麼設計才能被 agent 用」這個問題,一個誠實而非行銷式的答案。** 這是這次系列真正的價值所在。Series 1 的 Day14-21 會親手證明「決策級遙測」在這個系統裡目前只做到哪一步——`context.py` 有 enrichment/correlation，但 projection 有沒有收斂成單一物件、有沒有 baseline、有沒有 confidence score，Day20-21 會逐項對照 CEL 的三個職責誠實打勾或留白，而不是照搬書上一個漂亮 JSON 範例就宣稱做到了。這種「概念 vs 實作現況」的落差本身，比任何完美案例都更有教學價值——因為讀者接下來要做的事，正是補上這個落差，這個系列等於幫他們畫好了地圖。這份誠實還有第二層：整個系列刻意分成兩段,而不是把「agent 能給建議」跟「agent 能自主執行、能自我學習」硬塞進同一段——這個分段本身,就是「概念 vs 現況」誠實態度的延伸,承認後者需要治理平面、校準機制都到位,不是一段就能誠實交代完的東西。

**一套可重複驗證、不會退化的 agent 品質保證機制。** Series 1 Day24 把真實踩過的坑寫成 eval fixture，而 Day23 讓這件事變得更迫切：那份注入給 agent 的 catalog 把環境裡唯一那個事故的答案也寫了進去，所以在拿掉洩題之前，任何分數都不算數；Series 2 Day8 和 Day10 再把預算壓力下的 fast-path 幻覺、histogram bucket 假象值兩個坑也收進來。這代表系列結束後留下的不是一篇篇文章,而是一個 `eval/harness.py` 跑得動的回歸測試集。下次改 agent 邏輯,這些 fixture 會告訴你有沒有把舊坑踩回去——這是比「寫文章記錄教訓」更持久的資產。

如果要用一句話收斂:你會學到——治理(Operator/Weaver)、資料設計(Signal Plane/CEL)、agent 決策、evaluation 這四件事不是四個獨立主題,而是一條因果鏈:治理不好 → 資料不可信 → agent 決策依據是假的 → eval 分數低。Series 1 的 Day30 先畫一次「正向循環」的前半段(治理 → 拓撲/CEL → agent 建議),Series 2 的 Day10 把後半段(執行/治理平面 → 校準/SLO → 回饋治理與拓撲)接上去,合起來才是這整個系列真正要教的東西——不是「這裡有一堆很酷的技術」,而是「這些技術為什麼非得按這個順序疊起來不可」。這個因果鏈的直覺,才是 40 天寫完後最值錢、也最難從單篇文章學到的東西。

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

真正跟主軸有關、值得留的，只有「collector 被壓垮 → 資料悄悄變少 → 靠指標對帳才發現」這條排查邏輯——它呼應系列反覆強調的主題（可觀測性系統本身的健康也要被觀測），也是 Day14-21（Signal Plane 資料可信度）的伏筆。於是把這段 OOMKilled 排查案例直接併進 Day5（annotation 注入那天），拿掉三模式效能比較的部分，原本的 Day6 整篇刪除。

連帶影響：Series 1 的 Phase 1 從 18 天變成 17 天（Day1-17），Phase 2/Phase 3 天數不變但整體往前遞補一天（Day18-25、Day26-33），Series 1 總天數 34→33 天，Series 2 維持 10 天不變，總天數 44→**43 天**。

### v5：Day2-17 壓縮成 12 篇（Day2-13），天數 43→39

寫到 Day17 之後回頭看，第一階段（原 Day1-17）有兩個問題。**一是切得太細**：純概念日跟它對應的動手日分開（Operator 概念／裝 Operator、Weaver 概念／第一次 check），讀者要跨兩天才拿到一個完整的東西；GitOps 那天自己都承認「不是這系列因果鏈裡的必經環節」。**二是兩條主軸的比重不對**：前半段真正要講的是**平台工程視角的治理**（誰維護、成本落在誰身上、被擋的人能不能自己走出去），後半段要講的是**讓 agent 能推斷、而且這件事能被測試**——但原本的切法讓後者只散落在兩三天裡，最關鍵的「怎麼在沒有 LLM 的情況下驗證治理資產」完全沒有自己的位置。

v5 的調整：**合併 5 篇、新增 1 篇，Day2-17（16 篇）變成 Day2-13（12 篇）。**

| 新編號 | 內容 | 來自舊編號 |
|---|---|---|
| Day2 | AIOps 要的不是更多資料，是可推斷的資料 | Day2 |
| Day3 | OTel Operator：宣告式環境（CRD 拆解 ＋ GitOps 收尾） | Day3＋Day4＋Day6 |
| Day4 | 注入了不代表送達（sidecar ＋ OOMKilled） | Day5 |
| Day5 | Weaver 上手：schema 是共識、第一次 check、infer | Day7＋Day8＋Day9 |
| Day6 | 命名漂移與 Rego policy | Day10 |
| Day7 | 治理成為門：CI gate ＋ live-check 的兩個時間點 | Day11＋Day12 |
| Day8 | 分層與所有權：哪一層統一、哪一層放手 | Day13 |
| Day9 | breaking change：三層驗證模型與 diff | Day14 |
| Day10 | registry 成為 agent 的工具（MCP） | Day15 |
| Day11 | 機器可讀的意圖 ＋ codegen | Day16 |
| **Day12** | **可測試性：不用 LLM 也能驗證治理資產（新增）** | 抽自舊 Day15/16/17 的方法論 |
| Day13 | 新服務上線 checklist ＋ 階段收尾 | Day17 |

三個合併的理由各不相同。**Day3** 把「概念→CRD 實作→GitOps 入口」收成一條線，因為 GitOps 的價值就是「讓這些 CR 能被 review」，本來就是同一件事的收尾，獨立成天會顯得份量不足。**Day5** 把「為什麼需要 schema→第一次 check→從流量反推」放在一起，因為那個往返實驗（`emit` → `infer`，語意/值域/承諾三種資訊全部丟失）正是「schema 是團隊共識而不是觀察結果」最好的證明，跟概念日分開講會失去力道。**Day7** 把 CI gate 跟 live-check 合併，因為它們是同一條規則守在兩個時間點——「定義對不代表行為對」這個區分，分兩天講反而會被稀釋。

新增的 **Day12** 是這次調整真正的收穫。原本散在三天裡的方法論——不接 LLM 驗證 MCP（`mcp_probe.py`）、從真實 span 抽樣本而不是手打（`run_and_extract.py`，這個做法直接抓出我自己文章裡的一個錯）、故意讓每條規則失敗一次（那些刻意寫壞的 fixture）、先量一個基準——收斂成一支 `regress.sh`（實際寫出來是 29 條斷言、其中 8 條的預期離開碼是 1，測「還會不會擋」而不是「會不會通過」）。它同時是 Series 2 那個 eval harness 的前身，也讓「agent 表現不好」這句話第一次能被歸因。

連帶影響：Phase 1 從 17 天變 13 天（Day1-13），Phase 2 順延成 Day14-21、Phase 3 成 Day22-29，Series 1 總天數 33→**29 天**，Series 2 維持 10 天，總天數 43→**39 天**。

~~**範例 repo 的資料夾沿用原本的日號**（`day03/`…`day17/` 加一個新的 `testability/`），沒有跟著文章重編。~~ **這條後來沒有執行**，見 v6 的對帳。

### v6：Series 1 加一天收尾（Day30），並跟已寫的 Day1-23 對帳，天數 39→40

兩件事。

**一、加 Day30。** 原本 Day29 一天要同時做端到端 demo 跟整個系列的收尾，實際排下去會互相排擠：真實輸出佔掉版面，缺口那段就只剩幾行條列。拆成兩天，**Day29 是動手日**（把整條鏈真的跑一次、貼輸出），**Day30 是回顧日**（能做到什麼、對誰有價值、還缺什麼、交棒 Series 2）。Phase 3 從 Day22-29 變成 Day22-30，Series 1 從 29 天變 **30 天**，總天數 39→**40 天**。

**二、跟已寫的文章對帳。** Day1-23 寫完之後，這份 outline 有幾處跟成品對不上，已就地更正：

| 這份原本寫的 | 文章實際跑出來的 |
|---|---|
| Day12：21 條斷言、12 條預期 exit 1、不到十秒 | **29 條斷言、8 條預期 exit 1、36 秒** |
| Day12 的腳本在 `testability/` | 在 `ironman-2026/day12/` |
| Day13：`shipping-v0` 9/13 未通過、checklist 有一個洞 | **7/13 通過、有兩個洞** |
| 前言：Day1-17 走完你手上會有… | v5 之後第一階段是 **Day1-13** |
| Day2 結尾給一張 27 天的地圖 | 已拿掉總天數，改成不寫死日號 |
| 範例 repo 資料夾沿用原本日號、外加 `testability/` | **沒有這樣做**。資料夾最後跟著文章重編了：`day01`、`day03`–`day19`、`day23`，沒有 `testability/`，也沒有 `day02`／`day20`–`day22`（那幾天是純概念日，沒有程式碼）。`weaver-demo/ironman-2026/CLAUDE.md` 裡「資料夾日號不跟著文章重編」那一段也是同一筆舊帳，要一起改 |

Day14-23 的條目已經改寫成文章實際的內容（原本是動筆前的計畫），差異最大的是 Day16、Day17、Day19、Day23——那四天真正的內容都是動手之後才浮出來的，計畫裡一個字都沒有。


### v7：補上「人在 Grafana 打字」那一側，Series 1 從 30 天變 33 天

寫完 Day29 的端到端才發現一件事：從 Day23 到 Day29，每一次驗證都是從告警那頭進去的
（`/webhook/alert` / `run_headless`），而這整套東西最後要用的樣子，是一個人在 Grafana
的輸入框打一句話。那條路徑在 30 天裡幾乎沒出現過——plugin 只在「今天沒做的事」被提過
三次，而 `ChatPage`／`InvestigationsPage`／`TraceExplorerPage` 這 2400 行 TypeScript
一天都沒寫過。

更嚴重的是產品本身也缺：`_RCA_PLAYBOOK` 只有告警路徑會注入，所以 chat 沒有假設樹、
沒有信心分數、沒有 investigation 紀錄與回放連結。也就是說這 30 天做的「讓結論可以被
檢查」的機制，有一半只長在沒有人看的那條路上。

於是先補產品、再補文章：新增 **Day30**（使用者這一側的路徑）、**Day31**（答案怎麼變成
面板與提案卡）、**Day32**（Trace Explorer 與成本），原本的回顧日順延成 **Day33**。
Series 1 從 30 天變 **33 天**，總天數 40→**43 天**。

### v8：Series 2 對帳 repo 現況——那幾支檔案早就寫好了，缺的是資料

Day33 收尾時把三件事列成「結構性的、要下一個系列才處理的」：信心分數沒校準、授權
層級沒走過、回饋迴圈沒閉合。照著這三句去讀 repo，發現大綱把它們寫成「Series 2 要蓋
出來的東西」是錯的。

**六支檔案全部已經在 repo 裡**，共 1199 行，362 條測試裡有 56 條直接打它們：
`actions.py`(118)／`action_requests.py`(230, 9 測)／`governance.py`(181, 15 測)／
`breaker.py`(107, 7 測)／`calibration.py`(288, 11 測)／`blast_radius.py`(223, 14 測)。
`calibration_enabled` 預設就是 `True`，`label_run` 接在 `execution.py`、`main.py`
有標註 endpoint、SQLite 有專屬 table 加 migration，`governance._calibration_verdict()`
會算 overconfidence 並用它擋 AUTO。

原本 Day7 寫「過去案例注入／investigation→draft runbook 合成／runbook feedback
這三個閉環設計已寫好、尚未實作」，三句都不對：`_inject_past_incidents` 在 `agent.py`
有三個呼叫點、`draft_runbook.py` 255 行而且 `draft_runbook_enabled` 預設 True、
`runbook_feedback` 表在 `execution.py` 有三個寫入點外加一支健康報告 endpoint。

**而且原本以為的三個空白其實只有兩個。** `store.inv_query_similar` 的 SQL 是
`JOIN calibration c ON c.run_id = i.fp WHERE c.correct = 1`——過去事故庫是空的，
正是因為沒有標註。補標註會同時補掉「校準」跟「事故庫」兩格。

還有一個讓排程樂觀很多的發現：`governance._SELF_LABEL_SOURCES` 只排除
`remediation-verified` 與 `remediation-failed` 這兩個**自我**標註來源，也就是說
o11y-bench 現成的 grader 是合格的非自我標註來源，`governance_min_human_labeled_runs`
要的那 20 筆不需要 20 個人坐著標，跑 eval harness 就能產。

於是 Series 2 的重心整個換掉：**不是蓋出執行/治理平面，是讓已經蓋好的防護網第一次
真的被跑過、被證明會擋。** 天數維持 10 天，但每一天的動詞從「介紹這支檔案」變成
「讓它第一次拿到真實輸入」。排程風險也跟著換了位置——不在工程，在 LLM 變異：Day33
量到同一份程式碼連跑三次總分在 2.5–3.5 之間跳，所以校準曲線那天大概只能誠實標成
「樣本數不足，先看形狀」。

---

## Weaver docs → 系列天數 對照表

| Weaver 文件 | 內容 | 對應天數 |
|---|---|---|
| `docs/architecture.md` | crate 拆解：weaver_semconv/resolver/checker/forge/live_check/mcp，plugin 願景 | **Day5**（純概念） |
| `docs/usage.md` | 完整 CLI 指令表：check/generate/diff/emit/stats/json-schema/infer/package/live-check/mcp/completion | Day5（速查表）、Day5、Day5、Day9 |
| `docs/registry.md` | Registry 是什麼、group 類型(metric/span/event/attribute_group)、`ref`/`extends` 重用機制 | Day8 |
| `docs/define-your-own-telemetry-schema.md` | `manifest.yaml`、`dependencies`、`schema_url`、10 層深度限制、自訂 registry 路徑格式(本地/git/GitHub release) | Day8 |
| `docs/validate.md` | Rego policy、Finding 結構(id/message/level/context/signal_type) | Day6、Day7 |
| 三級嚴重度(information/improvement/violation) | **實測修正**：這是 `live-check` 的 advice 系統，不是 `registry check` 的 policy。check 階段只有 `deny` 會被收集、`level` 恆為 `violation`、`signal_type`/`signal_name` 恆為 `null` | Day6（說明為什麼沒有）、**Day7**（實際展開） |
| `docs/weaver-config.md` | `weaver.yaml` 設定：template_syntax/comment_formats/templates(filter+application_mode)、設定檔載入順序與優先權 | Day11 |
| `docs/codegen.md` | Jinja template + JQ filter 生成文件/型別安全建構子的流程 | Day11 |
| `docs/schema-changes.md` | `diff` 的變更分類（added/renamed/updated/obsoleted/removed） | Day9 |
| `weaver registry mcp`（來自 usage.md + 官方部落格） | 內建 search/get/live_check 三個 MCP tool，讓 LLM 直接用自然語言查 registry | **Day10**（AIOps 軸線第一次具體登場） |
| `weaver registry infer` | 從 OTLP 訊息反推、產生 schema 草稿 | **Day5**（呼應 Day1 的反面教材） |

---

# Series 1（33 天）：OTel 治理 + 讓 AI agent 能推理的決策級事件

## 第一階段：平台工程視角的治理（Day1-13）

- **Day1**（已寫）起手式：未治理的示範服務——故意示範命名壞味道（`userId` 混 `user.id`、span name 沒語意），講這種服務在真實團隊裡怎麼長出來的。全系列的反面教材，後面每天都會回頭對照它。
- **Day2**（已寫，純概念）AIOps 要的不是更多資料，是**可推斷**的資料——正面回答「AIOps 是什麼、不是什麼」：不是裝一個 AI 幫你看 dashboard，也不是自動化腳本包一層聊天介面。核心問題是可觀測性資料通常是為人設計的，machine consumer 拿來推理時缺語意、缺情境、缺信任度。對照傳統規則式告警與這系列要走到的「決策級事件＋信心分數＋下一步建議」，並劃清範圍（不做自主執行／自我學習，那是 Series 2）。結尾不給總天數地圖，只交代後面每一天都在把「只給人看的資料」變成「機器不用腦補也能判斷的資料」。
- **Day3**（已寫，合併原 Day3+4+6）**OTel Operator：把「持續維護」從人身上搬到迴圈裡**——先講 Operator pattern（CRD 宣告期望、controller 持續 reconcile、冪等性為什麼是前提，附真代碼裡 `Requeue` 那個選擇），再真的裝一次、把手寫 Collector Deployment 換成 CR（CR 故意叫 `otel` 讓生出來的 Service 正好是 `otel-collector`，五個 app 零改動），逐欄位對照 `-o yaml`（我寫的 vs schema 幫我補的；`status.conditions` 是這系列第一個「本來就存在、但沒人拿去給 agent 用」的機器可讀訊號）。平台工程主軸在「為什麼注入比教會每個團隊寫 OTel 划算」那節第一次明講判準：**一個機制的成本會不會隨團隊數線性成長**，並對照官方〈Don't Wrap OpenTelemetry〉那條岔路。收尾是 GitOps：`kustomization.yaml` 做出單一入口（Day7 的 CI gate 才有東西可以跑），加一份誠實承認「還只是人肉」的 reviewer checklist。
- **Day4**（已寫，原 Day5）**注入了不代表送達**——annotation 做 auto-instrumentation，不改代碼的 before/after trace 對比，誠實講覆蓋不到的地方（自訂 business span 還是要手動加）；兩段延伸：多語言環境（Java/PHP-FPM）的 sidecar 注入案例，以及主動把 collector 壓到 `OOMKilled`——資料悄悄變少、app 端完全看不到 exporter 錯誤，只能靠指標對帳發現。這條線會在 Day14-21 講資料可信度時接上。
- **Day5**（已寫，合併原 Day7+8+9）**Weaver 上手：schema 是團隊共識**——為什麼 telemetry 需要 schema（不是資料庫 schema，是「叫什麼、代表什麼、必不必填」的共識）、registry 的結構（`attribute_group` 當屬性池 + `ref`）、三個決定這份 schema 對 agent 有多少價值的欄位（`enum.members`／`requirement_level`／`template`）、三種嚴格度（缺 `examples` 完全不吭聲）。動手部分：先用 `stats` 當探針（`-r .` 假綠燈的教訓）、第一次 check 拿到綠燈並解釋「這只證明定義自洽」、三個示範踩管線三個位置（resolver 錯誤／checker Finding／**弄壞但沒被抓到**），然後把那條只比對名字前綴的 policy 改成「值域必須有界」，並在乾淨的 registry 上抓到一個真的（`gen_ai.request.model`）。最後 `infer`：它是一個 OTLP 接收器而不是讀檔案，`emit`→`infer` 往返實驗證明 `brief`／`requirement_level`／`enum.members` 三種資訊全部丟失——**觀察只能給你名字跟型別，語意、承諾、值域必須有人坐下來決定**。
- **Day6**（已寫，原 Day10）命名漂移，用 Rego policy 抓出來——先講清楚為什麼靠 code review 擋不住（review 看得到這個 PR 改了什麼，看不到系統目前已經有什麼），再把 `weaver_checker` 放大：resolved schema → Rego `input` → Finding。三條逐步加難的規則（camelCase／正規化後撞名／缺 namespace），實跑 9 個違規、exit 1，把 Day5 欠的 Rego 語法還掉。**三個實測修正**：(1) 三級嚴重度在 check 階段不存在，只有 `deny` 會被收集——那套屬於 live-check 的 advice，移到 Day7；(2) violation 物件的 `type` 只能是 `semconv_attribute`；(3) Rego 物件到 Finding 的欄位是錯位的，CI 上要抓 `context.id`。
- **Day7**（已寫，合併原 Day11+12）**治理成為門：兩個時間點**——先界定「跑得出來」跟「繞不過去」（會自己跑／擋得住／說得清楚，分別落在 CI、branch protection、輸出格式），逐段拆 workflow（釘版本、musl、sha256、假綠燈探針），**三個實測陷阱共通點是都不會讓你看到錯誤訊息**（stderr vs stdout 讓 annotation 完全失效／`file=` 是目錄名且沒有 `line=`／resolver 錯誤只印一個空 `::group::`），並強調 required status check 不在 YAML 裡。後半是 live-check 補上 CI 的盲點：三級嚴重度終於登場並決定離開碼、六種內建 advice 各對應前面某一天的坑、`not_stable` 是技術債的即時提醒、registry coverage 是「規範跟現實的距離」而不是合規率。兩個坑：預設 4317 吃到自己 coding agent 的遙測（含 PII），`--advice-policies` 是**覆蓋不是疊加**。**尚未補**：真的被擋下來的 PR 截圖。
- **Day8**（已寫，原 Day13）分層與所有權——從零寫一份 base registry，再疊一層 team registry。核心命題：**治理的難處不是「要不要統一」，是「哪一層統一、哪一層放手」**。四個實測陷阱：`registry_path` 綁 cwd 而不是 manifest 位置；重複定義不是覆寫而是製造一個沒人引用的孤兒（綠燈！）；依賴不遞移；把所有祖先都列出來又會撞到重複載入。兩條 policy 把前兩個安靜的坑補起來（`before_resolution` 終於有場景了）。
- **Day9**（已寫，原 Day14）breaking change：三層驗證模型——`metric_requirement_level` 規格有但 weaver 兩個版本都 hard error（第一層）；`--future` 讓同一句診斷從 ⚠ 變 ×（第二層，而「CI 要不要加」是平台團隊替所有人做的排程決定）；`comparison_after_resolution` 自己寫規則（第三層，`input` 是新版、`data` 是 baseline）。工具升版也是 breaking change 的來源：Day8 那個「列出所有祖先」的解法在 0.23.0 上直接 panic、exit 134。**`registry diff` 對三種最危險的變更完全靜音**（型別改變、`brief` 改動、enum member 移除）。結尾回答 Day8 的問題：deprecation 是宣告，不是通知。
- **Day10**（已寫，原 Day15）**registry 成為 agent 的工具**——`weaver registry mcp`，實測是八個 tool 不是文件說的三個，分成發現／理解／驗證三種職責，第三組（`live_check`）讓它變成閉環。強調 stdio JSON-RPC 可以不接 LLM 就驗證（`mcp_probe.py`）。四個坑：`search` 是關鍵字 AND 不是語意搜尋（tool description 就是 agent 的介面契約）；`browse_namespace` 不標 deprecated 而 `search` 會標且降權（同一份 registry 兩個入口兩種真相）；`not found` 回 `isError: false` 加一句散文；分層 registry 預設是空的（而修好之後 `provenance.source` 正好回答 Day9 那個「agent 讀到哪一版」）。閉環跑三輪：before → after（**還是紅的，因為把欄位搬到 span event 在 registry 眼裡是新增**）→ 定義出來才綠——**閉環的出口有兩個，一個是改程式碼，一個是改 registry**。
- **Day11**（已寫，原 Day16）機器可讀的意圖 ＋ codegen——三層對照（規則／門檻＋註解／意圖），兩種意圖（穩定狀態編成 alert rule、變更意圖編成部署後的驗證查詢，`unchanged` 那段才是最有價值的）。`compile_intent.py` 拿 registry 驗證意圖再編譯成 PromQL，`why`／`first_check` 直接搬進 alert annotations；兩份故意寫壞的意圖對應 agent 實際犯過的兩種錯，都是 exit 1。後半用 template engine 生出型別安全常數與 `StrEnum`（`PaymentOutcome('DECLINED')` 直接 raise，錯誤從「可被檢查」變成「說不出來」），並發現**生成物的 diff 補上了 Day9 那三個 `diff` 靜音的變更**——所以生成物要 commit 進版控。
- **Day12**（已寫，**新增**）**可測試性：不用 LLM 也能驗證治理資產**——先列出這系列七個「壞掉時症狀是一切看起來很順利」的案例，得出「你要驗證的不是它會不會通過，是它還會不會擋」。四個做法：不接 LLM 驗證 MCP（歸因：「agent 講錯」vs「registry 教錯」）、樣本從真實 span 抽而不是手打（這個做法抓出我自己文章裡的一個錯）、每條規則都要有一個「本來就該紅」的 fixture（把 agent 犯過的錯變成 fixture 是投報率最高的一件事）、先量一個基準。收斂成 `ironman-2026/day12/regress.sh`：29 條斷言、其中 8 條預期 exit 1、跑一次 36 秒、零 LLM 呼叫。這是 Series 2 那個 eval harness 的前身，也是「agent 表現不好」第一次能被歸因。
- **Day13**（已寫，原 Day17）新服務上線 checklist ＋ 階段收尾——先畫第一階段的四層因果鏈（環境→規範→執行→消費，加一條「消費端會反過來檢查規範品質」的回饋線），再給一支會自己跑的 `verify_onboarding.py`：13 項檢查、每一項都真的執行一次工具、失敗訊息包含下一步。兩個服務對照：照抄一半的 `shipping-v0`（7/13 通過，但 `registry check` 是綠的——**六項失敗全部落在「合法但不夠好」的區間**）與補完的 `shipping-v1`（13/13）。誠實記錄我自己的 checklist 有兩個洞（`shippingStatus` 躲過 enum 檢查，因為它剛好也違反命名規則；`biz.user.id` 的衝突躲過檢查，因為沒被任何 span `ref` 到就不進 resolved schema），得出「checklist 只會在壞掉的服務上顯現自己的 bug，所以壞掉的服務是測試資料而不是教材」。平台工程收尾：checklist 是清單不是門（前六項適合擋 PR，後面幾項是上線前的對話），以及每季一次全服務掃描產出**能力覆蓋率**而不是合規率。

## 第二階段：AIOps 核心能力管線（Day14-21）

- **Day14**（已寫）讀現況：畫出 signals 模組實際資料流——`importgraph.py` 從 AST 挖出真實 import 關係（用 AST 而不是 grep，因為 `__main__` 底下與函式內的 import 也是真的邊），八個模組排成定義層（`topology.py`/`contract.py`）／推導層（`reconcile.py`）／消費層（`context.py`/`health.py`/`dq.py`）＋兩支 CLI（`compile.py`/`weaver.py`）。三段真實輸出：`compile_signals()` 五份 fragment 編出 5 節點 6 邊、`build_signal_context()` 注入 agent 的那段話（`service_name (NOT service)` 直接補 Day2 的「缺語意」）、`dq_verdict()` 回 `proven_good: False`。**核心發現：`weaver.py` 沒有任何東西呼叫**——手動跑是綠燈（6 個 metric 全對得上），但 CI 只跑 registry 自己那道 gate，第一階段的成果目前沒有真的流進第二階段。**九宮格在這一天才第一次真的畫出來**（先前只在舊版被引用、從未定義）：軸是 Day2 的三缺（缺語意／缺情境／缺信任度）× 今天才浮出來的三階段（宣告／對帳／消費），結論是**中間「對帳」那一欄三格全是「寫好了但沒在跑」**，而那正是唯一會說「你手上這份資料已經不準」的一欄。
- **Day15**（已寫）補 edge 對帳：拓撲圖對真實 Tempo call graph 做驗證。`reconcile.py` 其實早就寫完（含兩份清單與 CLI），所以這天做的是「把那個沒人跑過的東西真的跑一次」。三段真實輸出：沒流量時 0 traces → 六條邊全報 unobserved、`dq_score=None`（假紅燈，而 `{ trace:duration > 5ms }` 探針過濾器是必要的，實測那段時間 214 筆 trace 全是 ≤1ms 的 `GET /health`）；有流量後 50 traces → declared=6 observed=5 dq=1.0，只剩 `api-gateway → payment-service`。**核心發現：那條邊是活的，是取樣沒抽到**——撈單一 trace 套 `edges_from_trace()` 直接證明它存在，再掃取樣數得到 max_traces=50/100 報死、300 報正常，而**預設值就是 50**。結論：`observed` 是下界不是事實、`unobserved` 是「我沒看到」不是「不存在」，而 stale／low-traffic／no-traffic 三種原因在報告上長得一模一樣。平台工程：宣告歸產品團隊（各自的 `signal.yaml`），對帳必然歸平台團隊（要跨服務看 Tempo），所以報告得補「上次觀察到是什麼時候」與「取樣涵蓋率」才讓對方修得動。附帶一個自己踩的坑：本機 3200 被另一座 k3d 叢集佔用，`port-forward` 靜默失敗但 `curl` 照常有回應，於是對著錯的 Tempo 做了一小時根因分析——`tempo_probe.py` 第一行印 buildinfo 就是為了這件事。
- **Day16**（已寫）discovery 清單餵進 reconcile——`tools/discovery.py` 的 `list_service_names()` 跟 `topology.py validate` 這條路其實也早就接好了，跑起來是 `topology v1.0.0 aligns with 5 live services`。**核心發現：那是假綠燈**，因為 `list_service_names()` 只讀 Loki。同一時間問三個 store：Loki 5 個、Prometheus 6 個、Tempo 6 個，多出來的是 `aiops-agent` 自己（有 trace 有 metric，但沒接 `o11y_shared/logging.py`，log 只進 stdout，往回查七天 Loki 都沒看過它）。所以「一個服務越不合規，越不容易被這個檢查抓到」，跟 Day13 checklist 第 8 項那個諷刺同型。新寫 `topology_watch.py`：三個 store 取聯集、單獨標出「只有部分 store 看得到」的服務（那通常代表遙測有一塊沒接上）、離開碼 0/1/2，**2 = 問不到所以什麼都不能斷定**，直接補上 Day15 抱怨的「三種原因一種呈現」。排程化之後 `--lookback` 的性質改變：它從「我想看多久以前」變成「多久沒訊號就算死」，對只有月底跑的服務會天天誤報，該下放到各服務的 `signal.yaml`（未做）。平台工程：兩個漂移方向該找的人不同——declared-but-dead 找服務團隊（只有他們分得出下線 vs 遙測斷了），live-but-undeclared 找平台團隊（是上線流程漏洞）。而第一個被抓到的未宣告服務，是寫這個檢查的人自己漏掉的那一個。
- **Day17**（已寫）dq.py 串 weaver.py：schema 對齊檢查串進 Signal Plane，全系列第一次讓兩個階段的程式碼互相呼叫。**卡最久的是 fail-open 變 fail-closed**：`weaver.py` 讀不到 registry 時回空集合（它自己那層很合理），拿去餵 `validate_against_weaver()` 就變成六筆假違規（實測輸出有貼）。這是連續第三次遇到同一個形狀（對帳分不出「圖錯了/沒流量」、服務清單分不出「沒這個服務/Loki 看不到」、現在是「沒宣告/讀不到 registry」），收斂成一條規則：**任何回傳集合的檢查函式，都要能回答「這個空集合是結論，還是我根本沒查成功」**。解法是抄 `topology.yaml`／`contracts.yaml` 既有的模式——不在 runtime 讀 registry（映像檔裡沒有），改成編譯期產物 `schema_alignment.json`，用 `checked: 0` 區分「一份都沒檢查」與「檢查了沒問題」。`dq_verdict()` 讀那份產物並把 schema 排在拓撲之前（契約錯了跑再多次對帳也不會變對），於是**全系列第一次拿到 `proven_good: True`**。CI 的 Weaver job 多一步：重生產物 + `git diff --exit-code`，而這招成立的前提是產物必須決定性（原本放的 `computed_ts` 會讓它永遠紅，拿掉了）。新增四條測試，全套 322 通過。平台工程：訊息要指名服務與 metric 名字，不然每次紅燈都變成給平台團隊的工單。未做：只比對名字不比對單位／值域／`requirement_level`；沒有反向檢查「registry 宣告了但沒人用」；撈名字仍靠正規表示式讀 `note` 的慣例而不是 `annotations`。
- **Day18**（已寫）context.py：把 edge reconcile 的噪音降下來。切入點是一段真的注入輸出裡的自我矛盾：**標題寫 `agreement 100%`，往下兩行有兩個 ⚠，而那兩個講的是同一條邊**。拆成三個成因：(1) `_annotate()` 對 upstream/downstream 都套用，同一事實講兩次；(2) 「沒看到」不等於「有機會看到卻沒看到」，原本沒有任何證據就下判斷；(3) `dq_score` 只算 observed→declared，unobserved 不進分母，所以滿分與 ⚠ 並存。三個修法：`reconcile.py` 順手記 `caller_samples`（同一批 trace 免費撈得到，實測 `api-gateway: 30` 等），`context.py` 只在呼叫方被跑過 ≥`_MIN_CALLER_EVIDENCE`(=5) 時才給 ⚠ 並寫出次數、不夠時退成不帶符號的 `not exercised in this sample`；標記只留在**呼叫方**那側（呼應 `signal.yaml` 由呼叫方宣告自己打出去的邊）；DQ 那行補一句交代分數不涵蓋什麼。before/after 有真實輸出對比，⚠ 從 2 個變 1 個且帶證據。四條新測試，全套 325 通過。主軸句：「我原本以為要降噪就得少講一點，結果實際做出來是多講了一個數字，然後噪音就不見了」＋「一個沒有講清楚自己邊界的正確數字，跟一個錯的數字造成的後果差不多」。平台工程：願意把不確定的訊號降級，是平台團隊替下游（沒有上下文的模型＋半夜被吵醒的人）承擔一部分判斷責任。未做：門檻沒跟取樣總數連動、沒有歷史（這次沒走到 vs 連三天沒走到）、`undeclared_edges` 那側仍會重複、沒量對 agent 實際輸出的影響。
- **Day19**（已寫）health.py：異常偵測順著圖走——同一場真的事故（payment 拒絕率漲），平鋪掃描給你 221 條 series 排序過的清單，順著圖走只挑得出兩條能判生死。贏的地方不是比較準（兩邊都看到了），是**它知道自己在看什麼，所以說得出「這兩個服務之間的關係是什麼」**。但後半段才是重點：順著圖走的前提是圖上每個節點都能被判生死，而這個前提在真實環境裡不成立——這座只有五個服務、還是自己設計的 demo，就有兩個節點走不到（api-gateway 的錯誤定義在 Loki 的 `event=http.request_failed`，而 `health.py` 只會跑 PromQL；user-service 的 throughput 掉到零一律回 `unknown`）。原本的處理方式是不講，而**不講在一份看起來像結論的報告裡會被讀成「沒問題」**，於是新增 `unjudgeable` 把走不到的地方講出來。Day18 那句話換個場景又成立一次：一個沒有講清楚自己走不到哪裡的分析，跟一個亂猜的分析後果差不多。未做：`unjudgeable` 只是把洞講出來沒補洞（SLI 還不能是一句 LogQL）、`rising` 那一支這次沒在真環境跑出來（只有單元測試蓋著）。
- **Day20**（已寫）概念日：情境豐富層（CEL）——切入點是一段沒有人打的流量：40 rps 的假付款。它沒有影響任何使用者，但它示範了**一份聚合遙測 JSON，在它自己的格式裡沒有任何位置可以承認自己不可靠**。訊號跟情境的差別攤開來就是資料形狀的差別：一個裸值加時間戳，對上一個帶著基準線、目標值、拓撲位置、可信度、而且回頭走得回原始查詢的物件。前者只撐得住「發生了什麼」，後者才撐得起「該不該做什麼」。CEL 三職責（enrichment/correlation/projection）＋溯源(grounding)在這天定義，兩種 JSON 並排對照。
- **Day21**（已寫）收尾：逐項對照 CEL 三職責——**四項裡只做到一項半**，而且兩個空格的性質不一樣：projection 是刻意不做的，correlation 是還沒做的，混在一起講會讓讀者以為都是取捨。這一階段真正的產出不是那 1545 行程式碼，是三條對帳路徑——宣告很便宜（誰都能寫一份 YAML 說自己的服務長什麼樣），貴的是持續證明那份宣告還準。當天就抓到一個現行的 silent decay：一個過期的 `git_version` 宣告，沒有任何地方在檢查它，**沒有對帳的宣告會在沒有人發現的情況下慢慢變成一份謊話**。「訊號斷崖」那個反模式誠實承認這系列沒有實例。未做：`git_version` 對帳沒補、correlation 沒動（要先讓三段文字變成三個物件）、`trust` 那段還是沒有。**而「沒有量這一階段對 agent 實際表現的影響」這筆帳，這天記的是第三次。**

## 第三階段：讓 agent 讀懂決策級事件、給分數、給下一步建議（Day22-33）

這階段刻意**不**碰治理平面的信任天花板、不碰校準誤差、不碰「能不能學習」——那是下一個系列的事。這裡只回答：agent 拿到 Phase 2 產出的豐富事件之後，能不能做出一個有信心分數、有下一步建議的判斷。

- **Day22**（已寫，純概念）Agent 基本組成：LangGraph 是什麼——從「一個 LLM 能做什麼」到「一個 agent 能做什麼」的落差：單純一問一答 vs. 需要「觀察→決策→行動」反覆循環（ReAct：reason→act→observe→再 reason），tool calling 是這個迴圈的骨架。講清楚為什麼需要一個「圖」而不是一個 while 迴圈——狀態要在多輪之間傳遞、需要條件式分支（要不要重試、要不要強制作答）、需要可以中斷/恢復。介紹 LangGraph 的核心模型：`StateGraph`（狀態的型別）、node（一個步驟，可能是 LLM 呼叫也可能是純函式）、`add_edge`/`add_conditional_edges`（下一步去哪，固定或依狀態判斷）、checkpointer（讓調查可以中斷後接著跑）。用泛用範例講完後，預告這個 repo 的 `agent.py` 本身就是用這套模型建的——`agent`/`tools`/`force_answer`/`rubric_trace` 四個 node，先給讀者看一眼這張圖但不逐行拆，下一天才真正對照。**寫完之後的結論**：選 LangGraph 而不是自己寫迴圈，換到的東西其實是可讀性，「什麼情況走哪條路」變成看得見的，不用去讀縮排——這在要跟別人解釋「為什麼 agent 那次會那樣做」的時候特別有用。
- **Day23**（已寫）agent 決策鏈梳理——原計畫是「把 `agent.py` 的 `StateGraph` 對回 discover→query→hypothesize→verify，標出讀 Phase 2 context 的入口點」，實際寫出來多了兩個發現。**一，決策鏈的入口在圖的外面**：前面八天做的東西全部是「注入」而不是「工具」，因為它們不需要模型決定要不要用，是模型開始想之前就該在桌上的（7302 個字元）。規定寫在 prompt，執行才在圖上。**二，這場考試是開書的**——那份為了教 agent 認識環境而寫的 catalog，順手把環境裡唯一那個事故的答案也寫了進去，於是這座 demo 上跑出來的每一個漂亮結果都要打折。「我原本是想看 agent 表現如何，結果先看到的是自己的量尺是壞的。」真跑一次的部分表現不錯（假設樹有列、預算省著花、工具報錯會自己修、查不到會說查不到），但有兩次 trace 查詢從一開始就不可能成功，因為沒有任何地方宣告 store 的保留期。未做：**沒有把 catalog 裡洩題的那幾段拿掉再跑一次**（那才是真正能說明它會不會做根因分析的實驗）、trace 保留期該長進契約裡、過去事故庫是空的。
- **Day24**（已寫）先把量尺修好，再讓 fixture 去讀逐字稿——第一件事是還 Day23 那筆帳，而 `leakcheck.py`（零 token，把系統 prompt ＋ 每一則注入都掃一次答案關鍵字，exit 1）掃出**洩題有兩處**：catalog 那處昨天就知道了，沒想到的是 `## Signal context` 也在洩——那句 `e.g. the new_validator flag shipping in a release` 長在 payment-service 自己的 `signal.yaml` 裡，是第二階段最乾淨的那一層。共通點是**會洩題的兩塊都是人手寫的**，現場查出來的能力快照與依賴健康從來沒洩過。清乾淨之後 A/B 跑真的 RCA（`ab_run.py`，A 邊吃清理前的原樣快照）：**分數沒掉**，兩邊都指到 payment-service ＋ v2.5.0、信心都 0.70，差別在 B 是從 `sum by (git_version, reason)` 的結果讀到版本的。後半是原本的計畫：`run_headless` 多回傳 `messages`，新增 `app/eval/process.py` 四條機械檢查（`queried`／`grounded`／`discover_before_retry`／`evidence_or_hedge`），fixture 多一個 `process:` 區塊、過程沒過就不算對，並新增 `order-service-discover-before-query`。**我的第一版檢查判錯了**：把 Tempo 語法錯誤也算成盲目重試，但錯誤帶 HINT、空結果才需要 discover，兩者性質不同。實跑 3 fixture × 2 seed = 50%，新 fixture 0/2，兩次都是「查回空的就換一句查詢」——Day1 那個坑今天還在。兩套 bench 的帳用一張對照表講清楚為什麼不合併，橋接的是判準（`queried`／`grounded` 從 Day1 grader 原封不動搬過來）。未做：`baseline.json` 沒更新、只有兩顆種子、假設樹/反證沒有機械化、`leakcheck.py` 還沒進 CI。
- **Day25**（已寫）空結果不是答案——切點決定成「三個 store 的臉色」＋「工具那一層怎麼翻譯」，byte cap／`_summarize_series_result` 只用一組實測數字帶過（1h step=15s：13 series × 241 點，82,779B → 5,154B）。`probe_apis.py` 把 docstring 那份怪癖清單變成可執行的：**Prometheus `/api/v1/metadata` 與 `/api/v1/targets` 都是空的**（OTel remote-write，沒人 scrape），要問「有哪些指標」得走 `/api/v1/series?match[]=`；**Loki `{service=...}` 回 200 + 零筆**（合法但永遠不匹配，Day1 那隻 agent 就死在這）；**Tempo 三種寫法全部大聲**（ns 時間 400、`service_name` 400 unexpected IDENTIFIER、`status="error"` 500）。順手更正自己寫的 docstring：Loki 3.2.0 已經兩種時間單位都吃，「不給奈秒會靜默回空」不再是現在的行為。改動有三：空結果補 `note`/`hint`（Prom 對 `__name__`、Loki 對可索引標籤，都 fail-open）、Tempo 錯誤提示改成指名要改哪個字（`service_name` → `resource.service.name`、`status=error` 不要引號），以及**丟掉 Loki 回應裡的 `stats`**（一則空回應 2,892B → 39B）。順帶修掉一個真 bug：舊守衛只在訊息含 `400`/`parse` 時給提示，而 `status="error"` 回的是 500，最常見的寫法錯誤一直沒拿到提示。**誠實記錄**：昨天 0/2 的 fixture 今天 2/2，但抓逐字稿發現那次一個空結果都沒遇到，新提示一次都沒觸發——分數變好比較可能是 order-service 這次有活流量，因果證不了，因為 fixture 的時鐘是 `now`。未做：三個 byte cap 常數沒驗證、summarize 會不會抹掉尖峰沒測、Tempo 空結果還是裸的（常常其實是保留期到了）。
- **Day26**（已寫）守門的人自己在崗位上嗎——`rubric.py` 兩個 judge 都拿去撞。**trace ID 守門有一批輸入從來沒被看過**：OTel trace ID 是 32 個 hex，但 Tempo search 會把前導零拿掉，實測一小時、五個服務、去重之後短於 32 字的佔 14–32%（三次抽樣分別 31%／32%／14%，比例會跳是因為 Tempo search 回傳集合不穩，所以引用百分比要一起講抽樣方式）。`{32}` 的樣式對這些 ID 是「沒被檢查」而不是「檢查過放行」，兩者輸出都是 pass。改成 `{24,32}` ＋ 查詢前補回前導零。**而且我 Day24 寫的 `grounded` 檢查複製了同一個 bug**，Day1 那支不接 LLM 的 grader 反而寫對（`{16,32}`），所以這個 bug 是後來引進的；`process.py` 改成 import `rubric` 那一份，全專案只留一個定義。第二個發現：`_tempo_trace_exists` 網路失敗一律回 True，Tempo 掛掉時守門全面放行且只留 debug log；配上 Tempo 1h 保留期，同一個守門在網路壞時太寬鬆、在資料過期時太嚴格。**k8s judge 四條 BLOCK 規則有兩條不可能生效**（`rollout_undo` 對不對得上 RCA、scale 超過 10 倍），因為 executor 只傳了一個 runbook id 當 context；實測同一個動作 thin/rich context 判決相反（ALLOW → BLOCK，理由是照著規則念的）。新增 `_rubric_context()` 把事故參數＋blast radius＋rollback 組進去。另記一個安靜的洞：BLOCK 之後的 abort 包在 `settings.actions_enabled` 裡，關著的時候連 audit 都不會寫。未做：沒有測試防止第三處又自己定義 regex、404 的兩種原因仍分不開、judge 判決沒進評測、那條 abort 路徑沒測試。
- **Day27**（已寫）下一步建議要連「多大」一起講——`blast_radius.py` 本身沒問題（八個提案跑一輪，ALLOW/REFUSE 都對，並用 generation/resourceVersion 前後比對證明六次乾跑真的沒動任何東西），問題全在它被放的位置。**兩個真正的發現**：(1) `footprint at proposal time: None`——範圍是在人按下 Approve 之後、進執行管線才算的，等於唯一需要這個數字的人拿不到；改成提案當下就跑一次乾跑，把 footprint ＋ policy 判決存進 ActionRequest，執行前那次保留（職責不同：提案那次給人看、執行那次防 TOCTOU）。(2) **`PaymentDeclineRateHigh` 比不到 runbook `payment-decline-rate-high`，整條「診斷→建議」被一次字串比對安靜關掉**（0 decisions、0 action requests；Day23 看到 `runbook: None` 時我以為只是告警名字亂取）；加上正規化 fallback 比對，但比中要留 warning，label 對不上也要留 warning。第三個小坑：scale 到 0 的拒絕理由被 singleton 規則搶走，會把人推去試 `replicas=1`（一樣被拒、理由才是真的 singleton），改成歸零有自己的理由。未做：plugin 卡片還沒渲染範圍、只有 undo/scale 兩種動作有乾跑（沒乾跑的動作直接跳過這道門）、量尺只有 affected pods（沒讀拓撲的 tier/journey）、正規化可能把兩個真的不同的告警比在一起。
- **Day28**（已寫）**大綱裡「決策不可回放」的預設結論是錯的**——`agent.py` 沒有呼叫 `audit.record` 是真的，但記錄推理過程的根本不是那一層。第一次跑探測 Tempo 是空的，差點就照原本結論收工；停下來才發現**這系列前面每一支探測腳本都是在 host 上直接呼叫 `run_headless()`，沒經過 `opentelemetry-instrument`，所以一個 span 都沒有**。改從叢集裡被 instrument 的服務走 `/webhook/alert` 進去，一次調查產生 46 個 span（fastapi 4／httpx 12／langchain 30），`opentelemetry-instrumentation-langchain` 把每個 LangGraph node、每次工具呼叫（含 `gen_ai.tool.call.arguments`／`result`）、每次模型呼叫（含 `gen_ai.system_instructions`／`input.messages`／token 用量）都變成 span——Day22 那張圖等於有了逐格錄影。**真正缺的只是一個欄位**：investigation row 跟 audit 都沒有 trace id，要回放得先去 log 撈。加了 `current_trace_id()`（拿不到回 None，不拋例外），investigation 多一個 `trace_id`、audit 寫進 `detail`。順帶在正式路徑上看到 Day26 的守門真的擋下一次幻覺 trace ID。成本首次可見：一次調查 26,123 tokens。誠實記的代價：那條 trace 帶著完整 system prompt 跟每則訊息，可回放等於可外洩（呼應 Day7 的 PII）。未做：plugin 沒畫出連結、Tempo 1h 保留期讓昨天的推理過程今天就沒了、audit 與 trace 的職責分界只寫在腦子裡、沒量 instrumentation 開銷。
- **Day29**（已寫，動手日）整條鏈跑一次，六段：治理 checklist（13/13）→ 意圖編成 alert rule → Signal Plane 編譯＋洩題掃描 →告警進去出診斷（信心 0.7、帶 `trace_id`）→ 提案帶 footprint（2 pods、rev 25→24、policy_ok）→ 固定資料上打分。**第一次跑是 3 ok / 3 failed，三個紅燈沒有一個是主功能壞掉**：(1) 我把 JSON 解析寫成 bash 裡的 `python3 -c`，跳脫疊三層 → 拆成 `report.py`；(2) **洩題掃描誤判**——它掃到 runbook 唯讀診斷「查出來」的 `v2.5.0`，也就是說這個檢查只有在事故沒發生時才會綠，而它的用途正是在有事故的環境上驗量尺；改成只掃「人寫的」區塊，量出來的標 `read`；(3) `capability snapshot failed` 其實是「這個服務在這份資料裡沒有 inventory」，`capability_for_services()` 照設計回 None 而 `SystemMessage(content=None)` 才炸——訊息描述技術現象而不是實際情況，跟 Day25/Day27 同一種病。**兩個環境不能同時存在也不等價**：stack image 自己佔 9090/3100/3200 且沒有 k8s API，同樣三個 fixture 在活叢集是 2/2、1/2、2/2，在固定資料是 1/1、0/1、0/1——fixture 是跟著它被寫出來的環境長的。誠實記：叢集裡的 image 是舊的，這條鏈證明的是程式碼不是部署。未做：`-n 1` 只有一顆種子、固定資料上兩個 fixture 仍紅、`e2e.sh` 沒進 CI。
- **Day30**（已寫）**使用者這一側的路徑**——前面每一天的驗證都是從告警那頭進去的（`/webhook/alert`／`run_headless`），而產品要用的樣子是人在 Grafana 打字。並排列出兩條路徑的清單才看到：chat 有意圖閘門／服務解析／clarify／面板，但**沒有 RCA playbook、沒有 findings（信心分數）、沒有過去事故、沒有 investigation 紀錄與 `trace_id`**——原因只是 `_RCA_PLAYBOOK` 這個常數只有 `_alert_to_prompt()` 一個呼叫點。補上三件事：investigate 模式注入同一份 playbook（放 system message，不是黏在問句後面）、回合結束抽 findings 並發 `findings` 事件＋存一列（`source=chat`）、過去事故查詢的 alertname 改成可選。前端也接了三處：對話下方的信心橫幅、investigation 列表的 `chat` 標記與「看它怎麼想的」連結（Trace Explorer 吃 `?trace=`）、提案列的影響範圍。兩個坑都跟「話對誰講」有關：英文指令黏在中文問句後面會讓模型換語言；假設樹在告警路徑沒人看、在 chat 會整棵印出來，要明說「內部想，不要印」。誠實記：信心分數還是模型自講的（同一題跑出過 1.0，而它自己寫『沒找反證』）。
- **Day31**（已寫）**答案怎麼變成面板與那顆按鈕**——契約有兩端，今天兩端都斷過。(1) `/alerts/provision` 第一次真的按下去拿到 `invalid alert rule: folder does not exist`，而那個 folder 是 `AlertSpec` 的預設值選的、使用者從沒看過，改成送規則前先確認 folder、沒有就建（409 也算成功）；真的寫進 Grafana（uid `bfudlf17fvw8wb`，帶 `X-Disable-Provenance` 才能在 UI 編輯）。(2) 問「幫我設一個告警」拿到一份**完全正確的 Prometheus YAML 規則**，plugin 只認 ```alert``` JSON，所以卡片不出現、使用者得自己複製貼上；在 prompt 明寫禁止項之後 JSON 對了但 fence 變成 ```json，卡片還是不出現 → 接收端改成寬容：```json 只要驗得成 AlertSpec 就當提案。**只靠 prompt 的契約是機率性的，發送端要求＋接收端寬容兩件事都要做。** 另記：這個解析在 plugin(TS) 與服務端(Python) 有兩份，跨語言收斂不了，只能讓其中一份有測試並互相標註。還講了面板的設計：面板不是把 agent 查到的資料畫出來，是把它用的查詢再跑一次（這也是 Day25 敢把 72KB 壓成 5KB 的前提）。
- **Day32**（已寫）**Trace Explorer：看它怎麼想的，以及那一次多少錢**——`/traces/{id}` 把 OTLP-JSON 轉成節點樹，一次 chat 調查攤開來是 55 spans／5 次模型呼叫／1 次工具呼叫／18,070 tokens／$0.001964。四個以前只能猜的數字：一次「調查」其實是五次模型呼叫（意圖閘門、兩次推理、findings 抽取、後續問題）；錢有 53% 花在第一次推理的**輸入**（10,039 tokens，也就是前二十天做的 context）；**慢的是想不是查**（tools 39ms vs 每次思考 1.5 秒）；Day30 加的 findings 抽取是看得見的 15%。改動：價格表加 `PRICES_AS_OF`、rollup 附 `cost_basis`——沒有人對帳的價格表就是這系列講過很多次的『宣告會慢慢變成謊話』，沒辦法讓它變準，至少讓它不假裝準（呼應 Day20 溯源）。未做：成本沒進指標、span 全取樣（配 Tempo 1h 保留期＝昨天的推理今天就沒了）、Trace Explorer 的 AI 分析沒被任何 rubric 檢查。
- **Day33**（已寫，回顧日）**那個數字交出來了，而且是倒退的**：同一小時、同一座 Day1 stack、同一支 grader（`bench/grade.py` 直接 import 沒改），Day1 那隻 5.5/9（當時記錄 4.5/9）、今天這隻連跑三次是 3.5／2.5／3.5、拿掉治理資產 2.5/9——同一份程式碼跑三次總分就在 2.5-3.5 之間跳，logs 那欄 1.0→0.0，所以數字只能當訊號。**倒退的原因是治理資產屬於另一座環境**：它去查 `http_server_requests_total`、用 `service_name`、用 `span.http.request.method`，在 demo-services 全對、在 Day1 那座 stack 全錯。今天這隻犯的錯跟 Day1 那隻是同一種（帶著寫死的環境知識自信地查不存在的東西），差別只在誰的寫死知識剛好對。所以結論不是「治理沒用」而是**治理是環境的函數**：對的環境是資產、錯的環境是負債、完全沒有更差。三段照計畫走：換到什麼（四項都不是「更聰明」而是「更容易被檢查」）；價值講人的反應（新服務上線從讀十幾篇文件變成跑一支腳本；值班的人不需要相信它、可以查它）；還缺什麼分兩類（能補沒補的八項 vs 結構性三項：信心分數沒校準、授權層級沒走過、回饋迴圈沒閉合，四支檔案都在 repo 裡但刻意不展開）。`day33/rerun_bench.py` 支援 `--which today|baseline` 與 `--no-governance`。
  1. **換到了什麼。** 把 Day13 那張「Day1 的失敗／現在有什麼／還缺什麼」的表擴到整個 Series 1。**這天要交出那個數字**：Day1 開場是 4.5/9，重跑同一組題目（用修好洩題之後的量尺）現在是多少。這筆帳 Day19、Day21、Day23 各記了一次，是全系列唯一能閉合自己迴圈的地方，沒有它讀者會覺得講了三十天治理卻從沒證明它有用。
  2. **價值講人的反應，不是技術清單。** 照 CLAUDE.md 那條小結原則：「新服務上線從讀十幾篇文章變成跑一支腳本」比「達成了資料一致性治理」有份量。
  3. **還缺什麼，這段可以寫滿。** 素材不用現想，前面每一天的「今天沒做的事」加起來就是。分兩類：**能補只是沒補的**（`regress.sh` 沒進 CI、live-check 沒接真服務、17 個 `required` 從沒對帳、Day13 checklist 那兩個洞、`eval/fixtures.yaml` 只有兩個 case、MCP 分層要靠一個已 deprecated 的 flag、`unjudgeable` 只講洞沒補洞、correlation 沒動）；**結構性的、要下一個系列才處理的**（agent 自己的決策路徑不可回放——`audit.record` 在 `execution.py` 裡 12 次、`agent.py` 零次；以及 `governance.py`／`calibration.py`／`breaker.py`／`action_requests.py` 這四支檔案都已經在 repo 裡，但要先有校準跟授權層級才講得清楚）。**「檔案已經存在但刻意不展開」比空口預告 Series 2 有說服力得多。**
  結尾交棒 Series 2：「這個建議準不準、能不能自我校正、能不能授權自主執行」。

---

# Series 2（10 天）：治理成熟度與學習迴圈——建議之後呢？

這個系列的前提：Series 1 做出的是「唯讀建議」，這裡處理三件事——這個建議能不能被授權自主執行（治理平面）、agent 說的信心準不準（校準）、系統能不能從過去的對錯中變聰明（CLL）。這裡才真正需要 ARE 的四平面架構語言，因為這正是它要解決的問題。

**但要先講清楚這個系列不是什麼**（見 v8）。那六支檔案都已經在 repo 裡而且有測試，所以這十天不是一個「把執行平面蓋出來」的施工日誌。它們共同的狀態是**寫好了、測試過了、但從來沒有拿到過真實輸入**：`actions_enabled` 是 `False`、`calibration` 表裡沒有足夠的標註、過去事故庫因此是空的。所以這十天的動詞是「跑跑看」跟「證明它會擋」，不是「做出來」。這跟 Series 1 那條「該紅的還會不會紅」是同一個標準，只是對象從 registry 換成了防護網。

整個系列的樞紐在 Day5：**沒有標註，後面五天全部做不了。** 排這張表的時候要把它當成關鍵路徑，而不是其中一天。

- **Day1** 概念日（獨立一天，純結構、不碰程式碼）：ch06 代理式可靠性架構全貌——這天的目的是先把書的結構性語言講完整，讓 Day2 之後的每一天都是「填一格」而不是「邊做邊定義新名詞」。建議拆成五個段落，剛好對應五張圖：
  1. **四平面總覽圖**——Signal/Reasoning/Execution/Governance 四個方框＋箭頭，標出 ARE §4.2 的「正交性」原則（失效不自動波及）；同時把 Series 1 已經蓋好的部分（Signal=Series1 Phase2 訊號平面、Reasoning=Series1 Day19-26 推理平面的雛形）標成「已完成」。Execution/Governance **不要標成「這系列要蓋」**——那兩格的程式碼也已經在 repo 裡了（見 v8），要標的是第三種狀態：「蓋好了，但沒有人按過開關」。這張圖同時是總覽也是進度條，而這個系列要推進的是那個第三種顏色。
  2. **平面間的契約介面圖**——不畫平面內部細節，只畫「平面之間傳的是什麼格式」：訊號平面→推理平面傳的是**訊號契約**（§4.3：name/version/owner/支援決策/新鮮度保證/最小觀測視窗/信心門檻/排除條件/schema，對應 Series 1 的 `signals/contract.py`）；推理平面→執行平面傳的是**候選行動**（§4.4：proposal id/觸發訊號/領先假設/排序後的行動選項/意圖對齊/風險/信心/所需授權層級，對應 Series 1 Day19-27 `agent.py`/`investigations.py`/`rubric.py` 的輸出，這裡第一次把書的完整詞彙貼回那幾天做出來的東西）；執行平面的行動本身是**行動契約**（§4.5：意圖/前置條件/執行邏輯/爆炸半徑限制/自動逆轉/成功標準/結果訊號，對應這系列 Day2-3 要拆的 `actions.py`/`blast_radius.py`/`action_requests.py`）。三個契約畫成三個介面框，箭頭方向就是資料流方向。
  3. **授權層級與人在迴圈圖**——治理平面的分級授權（唯讀觀察→提議→可逆執行→有邊界不可逆→人類核准，§4.6）畫成一條光譜；旁邊疊一張人在迴圈的三種模式圖（§4.7：迴圈之上＝定義意圖與政策，週-月節奏；迴圈之中＝特定決策被治理平面路由給人審查；迴圈之上監看＝透過 SLO 與稽核軌跡監督整體運作），誠實註明「這系列的 repo 目前只有『迴圈之上監看』被 eval/harness.py 撐起來一部分，『迴圈之中』的審查介面還是設計層次」。
  4. **參考架構時間軸圖**——書裡 §4.8 用一個結帳服務延遲事件走了一次 t=15s 訊號偵測→t=18s 推理提案→t=22s 治理核准→t=23s 執行實施→t=180s 結果驗證的完整時間軸。這裡不用書的案例，改用這個 repo 自己的一個真實/半真實事件（例如把「2/9 分事件」或某個 payment 相關 bench task 改編成同樣節奏的假設性時間軸），畫一條時間軸，每個時間點標上四平面裡對應哪一支檔案「應該」被觸發——並誠實加註：這是示範性重演，不是 repo 目前真的自動跑得動的時間軸，因為 Day2-3 才要真正把執行/治理平面接上去。
  5. **成熟度定位圖（預告）**——畫一次 L1-L5 的階梯（§4.9），在階梯上標出這個 repo 現在大概卡在哪一級（多半是 L2：訊號標準化、有諮詢式建議，但還沒有被授權的自主寫入），細節留給 Day9 展開，這裡只是先讓讀者知道「後面幾天在往上爬哪一段階梯」。

  這五段講完，Day2 開始就可以直接說「現在來實作契約介面圖裡的第三個框」，不用重新鋪陳。
- **Day2**（動手日）先讀現況，不要蓋新東西——沿用 Series 1 Day14 那支 `importgraph.py` 的做法，把執行/治理這六支檔案的真實呼叫關係畫出來，因為大綱寫的跟 repo 裡的差很多（見 v8）。逐條讀 `actions.py` 的註冊表設計：typed 註冊、`reversible` 標記、`requires_approval` 標記，以及那個預設 `False` 的 master kill switch。這一天要立的態度是：**這系列面對的不是白紙，是一組沒有人按過的開關**，而沒被按過的開關跟不存在的開關，在事故當下的差別是零。順便把 Day33 那三句「結構性空白」更正掉——講錯自己的東西要當天講清楚，這也是 Series 1 一路的做法。
- **Day3**（動手日）`action_requests.py` 狀態機——proposed→approved/rejected/expired→executing→terminal 畫一次完整轉移圖，講清楚為什麼要用 atomic compare-and-set 防止兩次 approve、或 approve 撞上 AUTO 路徑。這支有 9 條測試，所以這天的重點不是它會不會動，是**去撞測試沒有涵蓋的那幾條邊**：過期之後才按 approve、同一個 request 併發兩次 approve、executing 中途 pod 被砍掉之後那一列停在哪。
- **Day4**（動手日）`governance.py` 的授權判斷逐條讀——irreversible→ESCALATE、`confidence < low`→ESCALATE、high 但 `requires_approval`→PROPOSE、calibration unproven→降級 PROPOSE。重點放在 `_calibration_verdict()` 那兩道門：`governance_min_labeled_runs = 20` 是總標註數，`governance_min_human_labeled_runs = 20` 另外要求其中的非自我標註數，而 `_SELF_LABEL_SOURCES` 只排除 `remediation-verified`／`remediation-failed`。**「自己說自己修好了」不能解鎖自主權**，這是 ARE §6.2 constraint 1 最字面的一行程式碼。這天結束時要能講出一句話：現在按下去會發生什麼——答案是 PROPOSE，因為標註數是 0。
- **Day5**（動手日，**整個系列的關鍵路徑**）把 grader 接成標註來源，產出第一批非自我標註——`label_run` 目前有三個入口（`main.py` 的 endpoint、`calibration.py` 的 CLI、`execution.py` 的自我驗證），但沒有一條是批次的。這天做的是把 o11y-bench 的 grader 接成第四個入口，跑到 `cal_count_by_source(exclude_sources=_SELF_LABEL_SOURCES)` ≥ 20。**誠實記的地方在這裡**：20 是設定檔的地板，不是統計上夠的數字，而且要分佈在不同信心區間才講得出「它說 0.7 的時候實際對幾成」。Day33 已經量到同一份程式碼連跑三次分數在 2.5–3.5 之間跳，所以這天的產出應該標成「樣本數」而不是「結論」。
- **Day6**（動手日）第一張校準曲線——`compute_calibration` 第一次有東西可算，把 overconfidence 這個數字交出來，並對照 `governance_max_overconfidence = 0.1` 看它過不過。這天最該寫的是**兩種紅燈的差別**：「calibration unproven（標註不夠）」跟「overconfident（標註夠但它太有自信）」在 `_calibration_verdict` 裡回的是兩句不同的話，而它們對值班的人意義完全不同——前者是還沒開始量，後者是量完了不該信。順帶回應 Day30 那個「agent 自己寫『沒找反證』卻給 1.0」的實測。
- **Day7**（動手日）過去事故庫第一次非空——因為 `inv_query_similar` 是 `JOIN calibration WHERE correct = 1`，Day5 的標註一進去它就自己活了，這天不用寫新程式碼。要做的是 A/B：同一組 fixture，注入過去事故 vs 不注入，分數有沒有差。**這個實驗很可能是負面結果**，要先想好負面結果怎麼寫——Day25 已經有過一次「分數變好但因果證不了」，那次的處理方式（抓逐字稿去看新機制到底有沒有被觸發）就是這天的模板。同時把 `draft_runbook.py` 與 `runbook_feedback` 這兩個已經在跑的閉環接回 ARE 的 CLL 五步驟講一次。
- **Day8**（動手日）把 `actions_enabled` 打開——這是這個系列真正的那顆按鈕。在真實叢集上跑一次完整鏈路：提案帶 blast radius 乾跑 → 人按 approve → executor 執行 → settle window → verify → 失敗自動 rollback。`test_execution.py` 那 13 條測試已經 monkeypatch 過這條路，所以這天證明的不是邏輯，是**它在真的會壞的東西上會不會壞**。Day26 留下的那個洞要在這天補掉：BLOCK 之後的 abort 包在 `actions_enabled` 裡，關著的時候連 audit 都不會寫，而現在它要開了。
- **Day9**（動手日）反向驗證：讓防護網紅一次——照 Series 1 那條「該紅的還會不會紅」的標準，這天刻意把每一道門弄壞給它看：把 overconfidence 灌超過 0.1 確認 AUTO 自動降級成 PROPOSE、把標註來源全換成自我標註確認 AUTO 不解鎖、連續失敗打到 `breaker.py` 的 runaway 與 flapping 兩種模式並確認熔斷後**只能人工重置**。這天的產出是一份跟 `day12/regress.sh` 同構的斷言清單，每一條寫死預期離開碼。**一個從來沒有紅過的防護網，跟一個不存在的防護網，證據等級是一樣的。**
- **Day10**（回顧日）成熟度定位 + 跨系列的正向循環——把 Day1 那張 L1-L5 階梯拿回來，這次用 Day6 的實測數字定位，不是用猜的。五大旗艦 SLO（ARR/DQ-SLO/RL-SLO/AE-SLO/CE）各自定義並標出哪幾個現在真的量得出來、哪幾個還是設計。最後畫橫跨 Series 1+2 全部 43 天的正向循環：治理→拓撲/CEL→agent 建議→執行/治理平面→校準/SLO→回饋治理與拓撲。收尾要回答 Series 1 Day33 那個沒答完的問題：**治理是環境的函數，那自主權是什麼的函數**——照這十天做下來，答案是標註數量與校準誤差，而那兩個東西都得靠時間累積，買不到也求不來。

---

## 兩個系列怎麼安排

- 兩個系列可以獨立完賽：Series 1 單獨結束在「agent 給出有信心分數的下一步建議」，是一個完整的故事，不需要讀者知道 Series 2 存在。
- Series 2 開頭 Day1 那張四平面對照圖，直接把 Series 1 的產出（Phase2 的 Signal Plane、Day19-26 的 Reasoning Plane）標成「已完成」，避免 Series 2 讀者覺得需要重看一次 Series 1 才懂。Execution/Governance 那兩格標成「已蓋好、沒開過」，那才是這個系列真正的主題。
- Series 2 的關鍵路徑是 Day5（產出第一批非自我標註），Day6-9 全部卡在它後面。如果那天的標註數跑不出來，能照常寫的只有 Day2-4 這三天的「讀現況」，後面六天會全部變成紙上談兵——所以那天要排最多的緩衝，而不是當成中間的一天。
