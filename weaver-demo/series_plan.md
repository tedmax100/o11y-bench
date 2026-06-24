# OTel Weaver 系列文章規劃

## 第 1 篇（已完成）：從混亂到一致 — OTel Weaver 入門

核心：為什麼需要 Weaver、四步驟工作流（定義→驗證→生成→監控）

---

## 第 2 篇：讓 Schema 成為你的 Merge Gate

**核心訊息**：Schema 不只是文件，是可以在 PR 階段阻擋問題的防線。

### 大綱
1. Policy 進階 — 除了命名規範，寫「破壞性變更偵測」：屬性刪除或 type 改變時 CI 直接 fail
2. GitHub Actions 完整配置 — `weaver registry check` 接入 PR workflow
3. Drift Detection — Schema 定義與實際打出的 telemetry 不一致時怎麼抓
4. Schema 版本演進 — `deprecated` 欄位的正確手法，不破壞舊服務的前提下改 Schema

**Takeaway**：Schema 版本控制 + CI = 可觀測性的 breaking change protection，跟 API contract testing 是同一個概念。

**對應素材**：ch04、ch08、ch10

---

## 第 3 篇：型別安全的埋點 — 從 Schema 到程式碼到執行期

**核心訊息**：Schema 驗證過了、程式碼也生成了，但第三方套件打出來的 span 怎麼辦？

### 大綱
1. 模板系統運作原理 — `weaver.yaml` + Jinja2，為什麼需要自訂模板而不是直接輸出
2. 多語言 codegen — 同一份 Schema 生成 Go / Python 常數，實際整合到業務邏輯的寫法
3. live-check 作為第二道防線 — Weaver 接在 OTLP pipeline 前，攔截違規 span，解讀報告格式
4. 實際演練 — 故意讓一個 span 少一個 required attribute，看 live-check 怎麼報

**Takeaway**：codegen 消滅拼字錯誤；live-check 抓住你控制不了的第三方；兩者互補。

**對應素材**：ch05、ch06、ch07

---

## 第 4 篇：多團隊治理 — Registry 規模化與 AI 輔助重構

**核心訊息**：當 Schema 規模擴大、團隊變多，治理本身會變成新的問題。

### 大綱
1. 多 Registry 架構 — Platform team 維護 common registry，各產品 team extend，目錄組織與繼承策略
2. Schema 發布工件 — 把 Schema 打包讓其他 repo 引用，版本 pin 的策略
3. Weaver MCP Server — 讓 AI 直接讀你的 Schema，提出符合規範的重構建議（可 demo）
4. 跨語言一致性 — Go 服務和 Python 服務共享同一套 attribute 定義

**Takeaway**：從個人工具到組織基礎設施，Weaver 的成熟路徑。

**對應素材**：ch09、ch11、ch12

---

## 系列節奏（實際定稿）

```
build / merge time（Weaver 本體）
  #1   入門 + 四步驟（define→check→generate→live-check）         [已發布]
  #2   Schema 定義語言深入（type/ref/extends/enum/import/resolve）[已發布]
  #2.5 三個動詞實戰（emit / generate / live-check）               [已發布]
  #3   Merge Gate（breaking-change policy + drift + deprecated）  [已發布]

組織基礎設施（Weaver 規模化）
  #4   Schema 的發布與版本演進（diff / package / publish）        [新增]
  #5   多 Registry 與企業治理（繼承官方 / legacy 豁免 / 所有權）   [新增，伏筆篇]
  #6   用 MCP 讓 AI 讀懂你的 Schema（authoring-time AI）          [新增，AI 首登場]

decision time（橋接 → Signal Plane / AIOps）
  #7   從 build-time 到 decision-time（Signal Plane 思想 + Weaver 接點）[新增，橋接]
  #8   Signal Plane 與 AIOps Agent 實作（s1–s5 落地）            [新增，終點]

版本維護
  #9   0.24.0 新功能實戰（破壞性改名 / .weaver.toml / requirement_level /
       one_of-all_of / semconv_grouped_events / live-check dog-food）  [新增，全程實跑]
```

敘事弧：**Observability by Design（#1–#6）→ by Decision（#7–#8）**。
關鍵伏筆：#5 的「去中心化貢獻 → 編譯產物」模式，在 #7/#8 的 signal.yaml→compile 再現；
#6 的「AI 先查 schema 再答、不捏造」紀律，在 #8 的權威 SLI 契約再現。

每篇難度遞增，但各篇獨立可讀，都附實際可執行的指令。

> ✅ #4 已用 weaver 0.24.0 實跑校正：`weaver registry diff`（含 deprecated.renamed_to → Renamed 歸類）、`generate go/python`、**`registry package --v2`**（取代已 deprecated 的 `resolve`，產出 manifest.yaml + resolved.yaml）均為實測；跨語言常數形態（Go=attribute.Key、Python=str）已對齊真實生成物。
> ✅ #6 已用 weaver 0.24.0 實跑校正：MCP 啟動輸出（走 stdio JSON-RPC、server=rmcp 0.12.0）+ **真實 8 個 tool**（search / browse_namespace / get_attribute / get_metric / get_span / get_event / get_entity / live_check）——ch09 草稿的 list_groups/validate_span 等在 0.24.0 不存在，已全數更正；附真實 `search "payment"` JSON 回應。
> ℹ️ #5 仍承自 ch11 草稿（純架構/policy 概念，無待實跑的工具輸出）。#7/#8 的程式碼片段節錄自現有 signals/ 原始碼，準確。
> 📌 0.24.0 重要變更備忘：`weaver registry resolve` 已 deprecated → 改用 `generate` 或 `package`；`package` 需 `--v2`（同時影響 policy 格式）。
> 📌 0.23.0 → 0.24.0 升級實測（2026-06-21）：兩份 registry（`demo-services/`、`examples/telemetry/`）check 全綠；`generate go/python` 輸出與 0.23.0 byte-identical；MCP `tools/list` 仍是同樣 8 個工具；`metric_requirement_level` 仍是結構性硬錯（連 `--future` 都不用）。**新變化**：新增 `weaver registry infer`（從 OTLP message 反推 schema）；`weaver registry search`（CLI 子命令）改標 deprecated（"not compatible with V2 schema"，與 MCP 的 `search` *工具*無關，後者仍在）。
> ✅ #9 全程實跑（2026-06-21，0.24.0）：(1) `package` 的 `--resolved-schema-uri`→`--resolved-registry-uri` 改名（必填，少了 exit 1）；(2) `.weaver.toml` 的 `[registry] path` + `[package] resolved_registry_uri` 讓 check/package 免帶 flag（印 `Experimental!`）；(3) signal 層級 `requirement_level: recommended/opt_in` 通過、舊名 `metric_requirement_level` 仍硬錯；(4) `entity_associations` 的 `one_of`/`all_of` 通過、打錯 key 報三變體；(5) `semconv_grouped_events` 生出 event 常數（py/go），既有檔仍 byte-identical。新增 demo 素材：`examples/telemetry/registry/payment-events.yaml`、`templates/{go,python}/semconv_events.*.j2`、`examples/.weaver.toml`、`generated_from_template/semconv_events.{py,go}`。
