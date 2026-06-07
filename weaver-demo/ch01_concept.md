# 第一章：觀念啟蒙 — 可觀測性即設計

> 本章建立核心理念：遙測訊號是系統的一等公民 API，應在設計階段就納入規格、版本控制、與自動化驗證，而非事後補救。

---

## 1.1 你是否遇過這些痛點？

```
部署新版本後，所有警報瞬間失效 → 有人改了 metric 名稱
團隊 A 用 host-name，團隊 B 用 host_name → 查詢需要一堆 OR 條件
生產環境出問題才發現缺關鍵監控 → 事後諸葛
```

這些問題的根源：**遙測訊號（Metrics、Traces、Logs）被當作二等公民對待。**

### 為什麼會這樣？

遙測資料通常是「補充性的」。開發人員先完成功能邏輯，然後在最後一步「加上 trace」、「加上 metric」，沒有任何人審查命名是否一致、欄位是否完整、單位是否正確。這導致：

- **命名碎片化**：同一概念在不同服務中有不同命名（`user_id` vs `userId` vs `uid`）
- **隱性假設**：SRE 寫的警報規則假設了屬性存在，但沒有任何機制確保它永遠存在
- **文件落差**：RUNBOOK 說「查 `payment.order_id`」，但 v2.0 起該屬性已改名

### 傳統做法的結構性缺陷

| 問題 | 傳統做法 | 帶來的後果 |
|------|---------|-----------|
| 沒有規格書 | 開發人員自由命名 | 命名不一致，跨服務關聯查詢困難 |
| 規格書不自動驗證 | Wiki 或 Confluence 手動維護 | 文件與程式碼脫節，資訊腐朽 |
| 沒有 breaking change 防護 | 靠 code review 人工把關 | 高壓期間容易漏審 |
| 缺少型別安全 | 手打字串埋點 | 拼字錯誤在生產才發現 |

---

## 1.2 核心理念：可觀測性即設計

> **「將遙測訊號視為系統的一等公開 API。」**

就像 `Security by Design`、`Privacy by Design`，我們需要：

- 在 SDLC 中就將可觀測性納入設計
- 自動化驗證遙測資料的正確性
- 用版本控制管理遙測 Schema
- 確保文件與實作永遠同步

### 這個理念的具體意涵

**1. Schema 即合約（Schema as Contract）**

遙測 Schema 就像 REST API 的 OpenAPI Spec，或 Protobuf 定義。它定義：
- 什麼訊號應該存在（Metric、Span、Log）
- 每個訊號有哪些屬性（attribute）
- 屬性的型別、必填性、合法值域

改變 Schema = 改變合約 = 需要版本化和向後相容性評估。

**2. 驗證要自動化（Validation must be automated）**

人工 code review 無法可靠地阻止命名錯誤。需要：
- CI 自動檢查 Schema 語法正確性
- CI 自動比較生成程式碼是否與 Schema 同步
- 整合測試自動驗證執行時發出的訊號是否符合 Schema

**3. 文件要自動生成（Docs must be generated）**

從同一份 Schema 既生成程式碼、又生成文件，確保兩者永遠一致。

---

## 1.3 語義慣例 (Semantic Conventions)

OpenTelemetry 的語義慣例（Semantic Conventions，簡稱 semconv）是一套標準化命名架構，描述「什麼情況下該用什麼名稱」。

### T 型知識框架

| 廣度（T 型橫桿）| 深度（T 型縱桿）|
|---|---|
| 所有 HTTP 伺服器的標準監控方式 | Go Runtime 指標的深度定義 |
| 通用屬性命名規範（`service.name`、`host.name`）| 特定資料庫的查詢追蹤規格 |
| 跨語言、跨框架的一致性 | 特定雲端供應商的資源屬性 |

### 為什麼不自己定義命名？

| 自定義命名 | 遵循 Semantic Conventions |
|----------|--------------------------|
| 供應商鎖定，遷移成本高 | 工具生態系原生支援（Grafana dashboards、Datadog integration）|
| 跨組織協作困難 | 開箱即用的跨服務關聯分析 |
| 需要自行定義文件標準 | 社群共識，降低學習成本 |

### 命名規範原則

官方 semconv 使用 `.` 分隔命名空間：

```
http.request.method          ← 通用 HTTP 屬性
db.system                    ← 資料庫類型
service.name                 ← 資源屬性
payment.order_id             ← 自訂業務屬性（建議保持相同風格）
```

**重要**：自訂屬性應避免使用官方 semconv 的保留前綴（`http.`、`db.`、`net.` 等），以免與未來的官方定義衝突。

---

## 1.4 Weaver 的四大核心工作流

```
┌─────────────────────────────────────────────────────┐
│                   Weaver 工作流                       │
│                                                       │
│  1. Schema (YAML)  →  2. Templates (Jinja2)          │
│         ↓                      ↓                     │
│  4. CI Validate  ←  3. Generate (Code / Docs)        │
│         ↕                                            │
│     AI Refactor (MCP)                                │
└─────────────────────────────────────────────────────┘
```

### 工作流說明

**1. Schema 定義（YAML）**

在 YAML 檔中宣告所有遙測訊號的規格——什麼 Span 要有什麼屬性、什麼 Metric 用什麼 instrument。這是整個流程的「唯一真相來源（Single Source of Truth）」。

**2. 模板系統（Jinja2）**

Jinja2 模板描述「如何將 Schema 轉換成目標語言的程式碼或文件」。模板與 Schema 分離，讓你可以針對不同語言（Go、Python、Java）或不同輸出格式（程式碼、Markdown、Prometheus Rules）撰寫不同模板。

**3. 程式碼與文件生成**

`weaver registry generate` 讀取 Schema + 模板，輸出型別安全的常數檔案。所有開發人員 import 這些生成的常數，而非手打字串。

**4. CI/CD 驗證防護**

- `weaver registry check`：靜態驗證 Schema 語法正確性，以及自訂 Rego Policy
- `weaver registry live-check`：動態驗證執行時發出的 OTLP 訊號是否符合 Schema
- Drift detection：比較生成的程式碼是否與 Schema 同步

**AI 輔助重構（MCP）**

`weaver registry mcp` 啟動 Model Context Protocol 伺服器，讓 AI 助手（如 Claude Code）能直接讀取你的 Schema，提供符合規範的重構建議。

---

## 1.5 Weaver 在整個 SDLC 中的位置

```
需求分析         → 遙測需求應與功能需求一起定義
                   ↓
Schema 設計      → 在 telemetry/registry/*.yaml 中定義訊號規格
                   ↓
程式碼生成       → weaver registry generate → semconv_attrs.go / .py
                   ↓
開發埋點         → import 生成的常數，不手打字串
                   ↓
單元 / 整合測試  → weaver registry live-check 驗證執行時訊號
                   ↓
PR Review        → CI 自動執行 weaver registry check + drift detection
                   ↓
部署             → Schema 版本與 Git tag 綁定，可追溯
```

---

## 1.6 與其他工具的比較

| 工具 | 用途 | 與 Weaver 的關係 |
|------|------|----------------|
| OpenTelemetry SDK | 埋點、收集、傳送遙測訊號 | Weaver 生成的常數供 SDK 使用 |
| Prometheus | 指標儲存與查詢 | Weaver 確保指標名稱與屬性符合規範 |
| Grafana | 視覺化 | Weaver emit 可預先填充測試資料 |
| OPA / Rego | Policy Engine | Weaver 使用 OPA 執行自訂命名規則 |
| protobuf / OpenAPI | API Schema 管理 | 類比：Weaver 是遙測訊號的 protobuf |

---

## 1.7 本書學習路線圖

完成本書後，你將能夠：

1. 設計符合 OTel semconv 風格的遙測 Schema（第三章）
2. 用 Rego Policy 防護命名規則（第四章）
3. 自動生成 Go / Python 型別安全程式碼（第五、六章）
4. 動態驗證執行時遙測訊號的合規性（第七章）
5. 在 CI/CD 流程中阻擋不合規的程式碼（第八章）
6. 使用 AI 助手加速 Schema 遷移（第九章）
7. 安全地管理 Schema 版本演進（第十章）
8. 在企業環境中整合多個 Registry（第十一章）

---

## 延伸閱讀

- [OpenTelemetry Semantic Conventions 規格](https://github.com/open-telemetry/semantic-conventions)
- [Weaver GitHub Repository](https://github.com/open-telemetry/weaver)
- [OpenTelemetry 規格：Observability Primer](https://opentelemetry.io/docs/concepts/observability-primer/)
