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

## 系列節奏

```
第 1 篇  入門概念 + 四步驟 demo
第 2 篇  CI 防護（靜態）
第 3 篇  自動化（codegen + 執行期）
第 4 篇  規模化（治理 + AI）
```

每篇難度遞增，但各篇獨立可讀，都附實際可執行的指令。
