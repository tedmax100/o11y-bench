---
title: "【Day7】Weaver 基礎知識：為什麼 telemetry 需要 schema"
series: "2026 鐵人賽：AIOps with OpenTelemetry"
tags: [OpenTelemetry, Weaver, 鐵人賽]
---
# Day7：Weaver 基礎知識——為什麼 telemetry 需要 schema

今天是純概念日，不跑任何指令，也不碰 `demo-services` 的程式碼。Day1 已經示範過一次「沒有治理會長成什麼樣子」（`userId` 混 `user.id`、span name 沒語意），Day8 開始要動手用 Weaver 去攔這些問題——但在動手之前，先把「為什麼要有 schema」「Weaver 這個工具內部到底在做什麼」這兩件事講清楚，讓接下來好幾天的動手做，是在對照一張已經畫好的地圖，而不是邊做邊發明新名詞。

## 為什麼 telemetry 需要 schema

先講清楚一個容易混淆的地方：這裡的「schema」不是資料庫 schema（表結構、欄位型別、外鍵），是**團隊對「這個 span/metric/attribute 叫什麼、代表什麼、必不必填」的共識**。

Day1 那個反面教材的問題，本質上不是「程式碼寫錯」——`userId` 這個 attribute name 完全是合法的字串，不會讓任何 SDK 報錯，服務照樣正常運作、trace 照樣送得出去。問題在於：這個共識只存在於寫這段程式碼的人腦中，沒有被寫下來、沒有被檢查。等到第二個服務也要記錄使用者 ID，寫的人不知道團隊已經有 `user.id` 這個慣例（甚至不知道「慣例」這件事存在），於是又造了一個新名字。多個服務、多個名字、同一個語意——這時候想在 Grafana 上做一個跨服務的查詢，才發現要嘛全部改名重新部署，要嘛在查詢語法裡手動兼容五種寫法。

OpenTelemetry 官方為此定義了一套 **semantic convention**（語意慣例）：`http.method`、`db.system`、`service.name` 這類跨語言、跨服務都該長一樣的欄位名稱與型別規範。但光有官方那份是不夠的——每個團隊都有自己的業務語意（`payment.amount`、`order.status` 這種官方慣例不會涵蓋的欄位），這些自訂欄位一樣需要被治理，否則會重演 Day1 的故事，只是換成公司內部版本。

**Registry** 就是把這些 semantic convention（不管是官方的還是團隊自訂的）組織起來、可以被驗證、可以被查詢、可以拿去生成文件與程式碼的容器。而 **Weaver** 是操作這個容器的工具——它不是又一個 collector、不是又一個 SDK，它的角色更接近「telemetry schema 的編譯器與檢查器」。

## Weaver 內部：一條處理管線，不是一個黑盒子

Weaver 的原始碼是一個 cargo workspace（Rust 的 monorepo 概念），底下每個 `crates/weaver_*` 各自負責管線裡的一段。不需要懂 Rust，但看懂這張分工表，之後每天看到的指令輸出（`registry check` 的錯誤訊息長什麼樣、`registry generate` 產出什麼）都能對回是哪一段在做事：

| Crate | 負責什麼 | 對應到哪個指令 |
|---|---|---|
| `weaver_semconv` | 解析 registry YAML，定義「一個 group（span/metric/attribute_group/event）長什麼樣子」的資料模型 | 所有指令的第一步 |
| `weaver_resolver` | 處理 `ref`/`extends`/`imports` 這些繼承/重用關係，把多個 YAML 檔解析成一份「resolved」schema | `registry resolve`（**已標記 deprecated**，官方建議改用 `registry generate`/`registry package`，這裡列出是因為它是理解管線順序的關鍵步驟，不是要你實際下這個指令） |
| `weaver_checker` | 對 resolved schema 跑 Rego policy，輸出違規（Finding） | `registry check` |
| `weaver_forge` | 套 Jinja template，把 resolved schema 生成文件或程式碼 | `registry generate` |
| `weaver_emit` | 把 registry 定義的 signal 實際發送成 OTLP | `registry emit` |
| `weaver_live_check` | 拿真實 OTLP 流量對照 registry，找出 runtime 才會出現的違規 | `registry live-check` |
| `weaver_mcp` | 把 resolved registry 包裝成 MCP server，讓 LLM 能用自然語言查 | `registry mcp` |
| `weaver_search` | 支援上面 MCP/CLI 查詢用的搜尋引擎 | 被其他指令內部呼叫；`registry search` 本身也已標記 deprecated |

管線的順序基本上是固定的：`weaver_semconv` 解析 YAML → `weaver_resolver` 處理繼承關係、產出 resolved schema → 之後才分流到 `weaver_checker`（驗證）、`weaver_forge`（生成）、`weaver_emit`/`weaver_live_check`（跟真實流量對話）、`weaver_mcp`（跟 LLM 對話）。這代表 Day10 看到的「命名漂移」錯誤，是 `weaver_resolver` 先把繼承關係解開之後，才輪到 `weaver_checker` 去報錯——如果 resolve 這一步就失敗（例如 `extends` 指到一個不存在的 group），你會先看到 resolver 的錯誤，而不是 checker 的 Finding，這兩種錯誤訊息長得不一樣，來源也不一樣。

## CLI 速查表

今天不跑，但先列出來，之後幾天會陸續用到：

| 指令 | 做什麼 | 對應天數 |
|---|---|---|
| `weaver registry check` | 驗證 registry 是否符合 policy（Rego），輸出 Finding | Day8、Day10、Day11 |
| `weaver registry resolve` | 解析 `ref`/`extends`，輸出 resolved schema（JSON/YAML）——**已 deprecated**，官方導向改用下面的 `generate`/`package` | 不特別示範 |
| `weaver registry generate` | 套 template，生成文件或程式碼 | Day16 |
| `weaver registry diff` | 比較兩個版本的 registry，分類 added/renamed/updated/obsoleted/removed | Day14 |
| `weaver registry emit` | 把 registry 定義的 signal 實際發成 OTLP，用來驗證 pipeline | 之後視需要 |
| `weaver registry stats` | 統計 registry 內容（多少 group、多少 attribute） | 之後視需要 |
| `weaver registry json-schema` | 匯出 registry 的 JSON Schema，給外部工具做結構驗證 | 之後視需要 |
| `weaver registry infer` | 從真實 OTLP 流量反推、產生 schema 草稿 | Day9 |
| `weaver registry package` | 把 registry 打包成可分發的格式 | 之後視需要 |
| `weaver registry live-check` | 拿真實流量對照 registry，抓 runtime 才出現的違規 | Day12 |
| `weaver registry mcp` | 啟動 MCP server，讓 agent 用自然語言查 registry | Day15 |
| `weaver completion` | 產生 shell 自動補全設定 | 不特別示範 |

## 今天沒做的事

沒有對 Day1 的服務跑任何一次 `weaver registry check`——那是 Day8 要做的事，今天刻意只講完「為什麼需要」跟「內部怎麼分工」，不提前劇透第一次真實輸出長什麼樣子。也沒有展開 Rego policy 或 Jinja template 的細節，那些留到真正用到的那幾天（Day10-11 講 policy、Day16 講 template）才展開，避免今天塞太多還沒有場景可以掛的名詞。

明天：回到 Day1 那個反面教材，第一次真的對它跑 `weaver registry check`，貼真實違規輸出，對照今天這張 crate 分工表，看看到底是哪一段在報錯。
