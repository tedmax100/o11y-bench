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

# Series 1（30 天）：OTel 治理 + 讓 AI agent 能推理的決策級事件

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

## 第三階段：讓 agent 讀懂決策級事件、給分數、給下一步建議（Day22-30）

這階段刻意**不**碰治理平面的信任天花板、不碰校準誤差、不碰「能不能學習」——那是下一個系列的事。這裡只回答：agent 拿到 Phase 2 產出的豐富事件之後，能不能做出一個有信心分數、有下一步建議的判斷。

- **Day22**（已寫，純概念）Agent 基本組成：LangGraph 是什麼——從「一個 LLM 能做什麼」到「一個 agent 能做什麼」的落差：單純一問一答 vs. 需要「觀察→決策→行動」反覆循環（ReAct：reason→act→observe→再 reason），tool calling 是這個迴圈的骨架。講清楚為什麼需要一個「圖」而不是一個 while 迴圈——狀態要在多輪之間傳遞、需要條件式分支（要不要重試、要不要強制作答）、需要可以中斷/恢復。介紹 LangGraph 的核心模型：`StateGraph`（狀態的型別）、node（一個步驟，可能是 LLM 呼叫也可能是純函式）、`add_edge`/`add_conditional_edges`（下一步去哪，固定或依狀態判斷）、checkpointer（讓調查可以中斷後接著跑）。用泛用範例講完後，預告這個 repo 的 `agent.py` 本身就是用這套模型建的——`agent`/`tools`/`force_answer`/`rubric_trace` 四個 node，先給讀者看一眼這張圖但不逐行拆，下一天才真正對照。**寫完之後的結論**：選 LangGraph 而不是自己寫迴圈，換到的東西其實是可讀性，「什麼情況走哪條路」變成看得見的，不用去讀縮排——這在要跟別人解釋「為什麼 agent 那次會那樣做」的時候特別有用。
- **Day23**（已寫）agent 決策鏈梳理——原計畫是「把 `agent.py` 的 `StateGraph` 對回 discover→query→hypothesize→verify，標出讀 Phase 2 context 的入口點」，實際寫出來多了兩個發現。**一，決策鏈的入口在圖的外面**：前面八天做的東西全部是「注入」而不是「工具」，因為它們不需要模型決定要不要用，是模型開始想之前就該在桌上的（7302 個字元）。規定寫在 prompt，執行才在圖上。**二，這場考試是開書的**——那份為了教 agent 認識環境而寫的 catalog，順手把環境裡唯一那個事故的答案也寫了進去，於是這座 demo 上跑出來的每一個漂亮結果都要打折。「我原本是想看 agent 表現如何，結果先看到的是自己的量尺是壞的。」真跑一次的部分表現不錯（假設樹有列、預算省著花、工具報錯會自己修、查不到會說查不到），但有兩次 trace 查詢從一開始就不可能成功，因為沒有任何地方宣告 store 的保留期。未做：**沒有把 catalog 裡洩題的那幾段拿掉再跑一次**（那才是真正能說明它會不會做根因分析的實驗）、trace 保留期該長進契約裡、過去事故庫是空的。
- **Day24**（未寫）先把量尺修好，再重現一次 discover-before-query 失敗案例——這天的第一件事是還 Day23 那筆帳：把 catalog 裡洩題的段落拿掉，讓分數重新有意義。然後才是原本的計畫，把真實踩過的坑（RCA 只拿 2/9 分，因為硬編碼 schema 假設、沒先 discover 就直接查）寫成 regression case 進 `eval/fixtures.yaml`。**動筆前要先解決兩件事**：(1) `fixtures.yaml` 目前只有兩個 case（`payment-decline-service`、`user-service-no-incident`），outline 這裡承諾的那個 fixture 還不存在，要真的寫；(2) Day1 那套 bench（`ironman-2026/day01/bench/` 的九題＋`o11y_bench/` CLI，吃自然語言問題、機械打分）跟 `app/eval/harness.py`（吃 Grafana alert payload、判 culprit/inconclusive）**是兩套獨立的東西**，這天要嘛橋接、要嘛講清楚為什麼是兩套，不能默默換掉——讀者從 Day1 一路看過來會發現對不上。
- **Day25**（未寫）tools/query.py：修一個真實 API 怪癖——Prom metadata 為空、Loki label 需要時間範圍、Tempo dotted tags，這些是直接連接 API 才會踩到的坑，補防呆並寫測試。**素材盤點：`tools/query.py` 539 行＋`test_query.py` 412 行，是這幾天裡最厚的一份**，而且 docstring 開頭就列了「probed against the live stack」的怪癖清單（Loki 的 `start`/`end` 要奈秒、Tempo search 要 unix 秒）。另外有一塊 outline 原本沒列、但可能更有料的東西：byte cap ＋ aggregation fallback ＋ `_summarize_series_result`（把約 60 個高精度浮點數壓成 last/min/max/avg 加最多 8 個取樣點再餵給 LLM）。這天有可能一天寫不完，要先決定切點。
- **Day26**（未寫）從診斷到分數：agent 的假設怎麼被驗證——`rubric.py` 的兩個 LLM-as-judge 守門（trace ID 存在性驗證、k8s 寫入意圖檢查），證明「agent 說它有信心」不代表「這個信心可信」——分數本身也需要被驗證，這是這系列對「給分數」這件事最誠實的一次示範。**素材盤點：`rubric.py` 只有 152 行但兩個 judge 都在**（`verify_trace_ids` 真的去打 Tempo 驗 id 存不存在，失敗回一段要求重查而不是編造的 retry prompt；`check_k8s_write` 用 LLM 判寫入意圖），`test_rubric.py` 241 行，而且 `rubric_trace` 是 `agent.py` 圖上真的一個 node。**這天的因果線最漂亮**：它直接接回 Day1 那個 `grounded` 檢查跟憑空生出來的 814。
- **Day27**（未寫）下一步建議：唯讀乾跑當作建議的具體形狀——把 Day19-26 產出的診斷接到 `blast_radius.py` 的唯讀乾跑（不執行，只估算一個動作會影響幾個 pod、有沒有跨 namespace），示範「下一步建議」長什麼樣子；明確劃線：這系列只做到「估算＋建議」，「是否可以自主執行」留給下一個系列的治理平面。**素材盤點：`blast_radius.py` 218 行＋`test_blast_radius.py` 137 行，份量剛好。**
- **Day28**（未寫）agent 自身可觀測性：決策有沒有被 trace——**先講結論，因為答案是「沒有」**：`audit.record` 在 `execution.py` 裡被呼叫 12 次，寫入類動作全都有紀錄；但 `agent.py` 一次都沒有呼叫。也就是**「要改叢集」可回放，「怎麼推理出這個結論」不可回放**。`investigations.py` 有 `DecisionRow`／`InvestigationRecord`，但那是結果層級的紀錄，不是逐步的工具呼叫。一個做可觀測性的 agent，自己的決策路徑不可回放是雙重諷刺，而照 CLAUDE.md 那條「踩到的坑就是內容」，這天該誠實寫成缺口，不要先偷偷補完再假裝本來就有。要補的話成本也要寫出來（`audit.py` 只有 70 行、`test_audit.py` 41 行，這一層本來就很薄）。
- **Day29**（未寫，動手日）跑一次端到端 demo——治理 checklist → 意圖宣告 → 拓撲/CEL → agent 診斷＋信心分數＋下一步建議，完整跑一次並貼真實輸出。可重現性這一關已經有解：`python -m app.eval --stack` 會啟預先建好的 demo-services-o11y-stack image，把 payment v2.4.1→v2.5.0 那個 incident 決定性地烘進去，並把 fixture 裡的 `now` 全部釘到那座 stack 的 scenario clock，所以每次跑查到的都是同一份資料。**這天最大的風險是依賴鏈最長**，前面任何一環跑不動都會卡在這裡，值得提早排練一次。
- **Day30**（未寫，回顧日）Series 1 收尾：能做到什麼、對誰有價值、還缺什麼——這天是回顧不是報告。三段：
  1. **換到了什麼。** 把 Day13 那張「Day1 的失敗／現在有什麼／還缺什麼」的表擴到整個 Series 1。**這天要交出那個數字**：Day1 開場是 4.5/9，重跑同一組題目（用修好洩題之後的量尺）現在是多少。這筆帳 Day19、Day21、Day23 各記了一次，是全系列唯一能閉合自己迴圈的地方，沒有它讀者會覺得講了三十天治理卻從沒證明它有用。
  2. **價值講人的反應，不是技術清單。** 照 CLAUDE.md 那條小結原則：「新服務上線從讀十幾篇文章變成跑一支腳本」比「達成了資料一致性治理」有份量。
  3. **還缺什麼，這段可以寫滿。** 素材不用現想，前面每一天的「今天沒做的事」加起來就是。分兩類：**能補只是沒補的**（`regress.sh` 沒進 CI、live-check 沒接真服務、17 個 `required` 從沒對帳、Day13 checklist 那兩個洞、`eval/fixtures.yaml` 只有兩個 case、MCP 分層要靠一個已 deprecated 的 flag、`unjudgeable` 只講洞沒補洞、correlation 沒動）；**結構性的、要下一個系列才處理的**（agent 自己的決策路徑不可回放——`audit.record` 在 `execution.py` 裡 12 次、`agent.py` 零次；以及 `governance.py`／`calibration.py`／`breaker.py`／`action_requests.py` 這四支檔案都已經在 repo 裡，但要先有校準跟授權層級才講得清楚）。**「檔案已經存在但刻意不展開」比空口預告 Series 2 有說服力得多。**
  結尾交棒 Series 2：「這個建議準不準、能不能自我校正、能不能授權自主執行」。

---

# Series 2（10 天）：治理成熟度與學習迴圈——建議之後呢？

這個系列的前提：Series 1 做出的是「唯讀建議」，這裡處理三件事——這個建議能不能被授權自主執行（治理平面）、agent 說的信心準不準（校準）、系統能不能從過去的對錯中變聰明（CLL）。這裡才真正需要 ARE 的四平面架構語言，因為這正是它要解決的問題。

- **Day1** 概念日（獨立一天，純結構、不碰程式碼）：ch06 代理式可靠性架構全貌——這天的目的是先把書的結構性語言講完整，讓 Day2 之後的每一天都是「填一格」而不是「邊做邊定義新名詞」。建議拆成五個段落，剛好對應五張圖：
  1. **四平面總覽圖**——Signal/Reasoning/Execution/Governance 四個方框＋箭頭，標出 ARE §4.2 的「正交性」原則（失效不自動波及）；同時把 Series 1 已經蓋好的部分（Signal=Series1 Phase2 訊號平面、Reasoning=Series1 Day19-26 推理平面的雛形）標成「已完成」，Execution/Governance 標成「這系列要蓋」——這張圖同時是總覽也是進度條。
  2. **平面間的契約介面圖**——不畫平面內部細節，只畫「平面之間傳的是什麼格式」：訊號平面→推理平面傳的是**訊號契約**（§4.3：name/version/owner/支援決策/新鮮度保證/最小觀測視窗/信心門檻/排除條件/schema，對應 Series 1 的 `signals/contract.py`）；推理平面→執行平面傳的是**候選行動**（§4.4：proposal id/觸發訊號/領先假設/排序後的行動選項/意圖對齊/風險/信心/所需授權層級，對應 Series 1 Day19-27 `agent.py`/`investigations.py`/`rubric.py` 的輸出，這裡第一次把書的完整詞彙貼回那幾天做出來的東西）；執行平面的行動本身是**行動契約**（§4.5：意圖/前置條件/執行邏輯/爆炸半徑限制/自動逆轉/成功標準/結果訊號，對應這系列 Day2-3 要拆的 `actions.py`/`blast_radius.py`/`action_requests.py`）。三個契約畫成三個介面框，箭頭方向就是資料流方向。
  3. **授權層級與人在迴圈圖**——治理平面的分級授權（唯讀觀察→提議→可逆執行→有邊界不可逆→人類核准，§4.6）畫成一條光譜；旁邊疊一張人在迴圈的三種模式圖（§4.7：迴圈之上＝定義意圖與政策，週-月節奏；迴圈之中＝特定決策被治理平面路由給人審查；迴圈之上監看＝透過 SLO 與稽核軌跡監督整體運作），誠實註明「這系列的 repo 目前只有『迴圈之上監看』被 eval/harness.py 撐起來一部分，『迴圈之中』的審查介面還是設計層次」。
  4. **參考架構時間軸圖**——書裡 §4.8 用一個結帳服務延遲事件走了一次 t=15s 訊號偵測→t=18s 推理提案→t=22s 治理核准→t=23s 執行實施→t=180s 結果驗證的完整時間軸。這裡不用書的案例，改用這個 repo 自己的一個真實/半真實事件（例如把「2/9 分事件」或某個 payment 相關 bench task 改編成同樣節奏的假設性時間軸），畫一條時間軸，每個時間點標上四平面裡對應哪一支檔案「應該」被觸發——並誠實加註：這是示範性重演，不是 repo 目前真的自動跑得動的時間軸，因為 Day2-3 才要真正把執行/治理平面接上去。
  5. **成熟度定位圖（預告）**——畫一次 L1-L5 的階梯（§4.9），在階梯上標出這個 repo 現在大概卡在哪一級（多半是 L2：訊號標準化、有諮詢式建議，但還沒有被授權的自主寫入），細節留給 Day9 展開，這裡只是先讓讀者知道「後面幾天在往上爬哪一段階梯」。

  這五段講完，Day2 開始就可以直接說「現在來實作契約介面圖裡的第三個框」，不用重新鋪陳。
- **Day2** 執行平面：`actions.py` 註冊表 + kill switch——把 Series 1 Day27 的「唯讀建議」升級成「一個可以被授權的行動」需要什麼：typed 註冊、reversible 標記、approval 標記、預設關閉的 kill switch。
- **Day3** 執行平面：`action_requests.py` 狀態機——proposed→approved/rejected/expired→executing→terminal，畫一次完整狀態轉移圖，講清楚為什麼要用 atomic compare-and-set 防止兩次 approve 或 approve 撞上 AUTO 路徑。
- **Day4** 治理平面：`governance.py` 把「信任天花板」寫成程式碼——逐條讀 Autonomy 判斷邏輯（irreversible→ESCALATE、confidence<low→ESCALATE、calibration unproven→降級 PROPOSE），這是 L2→L3 轉換需要四項機制同時就緒這句話最字面的實現。
- **Day5** 治理平面：`breaker.py` 熔斷器——runaway（短時間內執行次數暴衝）與 flapping（同一個 target 連續失敗）兩種失效模式，講「熔斷後只能人工重置」這個設計為什麼是必要的，而不是可以自己恢復。
- **Day6** 校準誤差：`calibration.py` 的兩階段設計——`record_run`（跑的當下記信心，還沒有 verdict）→ `label_run`（事後補正確與否）→ `compute_calibration`，跑一次完整流程算出真實 CE 數字。
- **Day7** 封閉學習迴圈（CLL）+ 知識管理三閉環——把 Day6 串成 ARE 的 CLL 五步驟（擷取預測→觀測結果→比較偏差→提議更新→治理路徑套用），並介紹 `doc/aiops-agent-knowledge-loop.md` 現有的三個設計（過去案例注入、investigation→draft runbook 合成、runbook feedback）——標明這是「設計已寫好、尚未實作」，是這系列讀者可以接手的下一步。
- **Day8** 重現一次預算壓力下的幻覺並加防護——tight budget 下的 fast-path/trace-id 幻覺，呼應 Day4-5 的 ESCALATE/熔斷機制：這次失敗是治理平面該攔卻沒攔，還是攔了但誤判。
- **Day9** 概念日：五大旗艦 SLO + 成熟度模型總覽 → Rubric 落地——ARR/DQ-SLO/RL-SLO/AE-SLO/CE 各自定義，直接把整個 repo 對應回 L1-L5 成熟度表，現在卡在哪一級；用 `calibration.py` 的實測結果誠實報告目前累積了幾筆標記樣本、CE 離「proven-good」還差多遠（不是「缺口」，是「現況數字」）。
- **Day10** 總收尾：兩個真實 bug 復盤 + 40 天正向循環圖——2/9 分事件、histogram bucket 假象值各寫成 eval fixture；畫出橫跨 Series 1+2 全部 40 天的正向循環：治理→拓撲/CEL→agent建議→執行/治理平面→校準/SLO→回饋治理與拓撲，作為下一輪迭代的起點。

---

## 兩個系列怎麼安排

- 兩個系列可以獨立完賽：Series 1 單獨結束在「agent 給出有信心分數的下一步建議」，是一個完整的故事，不需要讀者知道 Series 2 存在。
- Series 2 開頭 Day1 那張四平面對照圖，直接把 Series 1 的產出（Phase2 的 Signal Plane、Day19-26 的 Reasoning Plane）標成「已完成」，只有 Execution/Governance 是這個系列的新內容——避免 Series 2 讀者覺得需要重看一次 Series 1 才懂。
