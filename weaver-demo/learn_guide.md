# OpenTelemetry Weaver 實戰學習指南
## 從可觀測性設計到 CI/CD 自動化防護

> **目標讀者：** 開發人員、SRE、DevOps 工程師
> **預計學習時間：** 2 天（12 小時）
> **語言範例：** Go 1.22+、Python 3.11+

---

## 目錄索引

本指南已拆分為 12 個獨立的章節檔案，每章可單獨閱讀。

| 章節 | 檔案 | 摘要 |
|------|------|------|
| 第一章：觀念啟蒙 | [ch01_concept.md](ch01_concept.md) | 為何遙測訊號應視為一等 API；Weaver 四大工作流；與 SDLC 的整合位置 |
| 第二章：環境建置與核心工具 | [ch02_setup.md](ch02_setup.md) | 三種安裝方式；所有核心指令的完整用法；專案目錄最佳實踐 |
| 第三章：撰寫第一個遙測 Schema | [ch03_schema.md](ch03_schema.md) | group 所有欄位說明；attribute type/requirement_level 完整列表；ref 機制 |
| 第四章：靜態驗證與 Policy 防護 | [ch04_policy.md](ch04_policy.md) | Rego 語言基礎；命名規則 Policy；破壞性變更防護；Policy 測試 |
| 第五章：Weaver 模板系統 | [ch05_templates.md](ch05_templates.md) | weaver.yaml 完整欄位；jq filter 語法；Jinja2 內建 filter；Go/Python/Markdown 模板 |
| 第六章：型別安全程式碼生成 | [ch06_codegen.md](ch06_codegen.md) | 生成程式碼的完整結構；Go 與 Python 業務邏輯整合範例 |
| 第七章：動態實時驗證 (Live-check) | [ch07_livecheck.md](ch07_livecheck.md) | live-check 工作原理；Go/Python 整合測試；報告解讀；emit 預開發驗證 |
| 第八章：CI/CD 整合實戰 | [ch08_cicd.md](ch08_cicd.md) | 四層防護設計；完整 GitHub Actions 配置；破壞性測試演練；GitLab CI 等效配置 |
| 第九章：AI 輔助重構 (MCP Server) | [ch09_mcp.md](ch09_mcp.md) | MCP 原理；Claude Code 設定；Prompt 工作流範例；MCP Tool 列表 |
| 第十章：Schema 演進與版本管理 | [ch10_evolution.md](ch10_evolution.md) | 廢棄流程三階段；版本 diff；打包發布；多版本共存策略 |
| 第十一章：企業級整合策略 | [ch11_enterprise.md](ch11_enterprise.md) | 多 Registry 架構；遺留系統整合；治理模型；跨語言一致性 |
| 第十二章：綜合實戰 Workshop | [ch12_workshop.md](ch12_workshop.md) | 購物車微服務端到端實作；刻意錯誤演練；AI MCP 修復；Docker Compose 環境 |

---

## 快速導覽：依任務找章節

### 我想設計一個新的遙測 Schema
→ [第一章](ch01_concept.md)（理念）→ [第三章](ch03_schema.md)（語法）→ [第四章](ch04_policy.md)（Policy）

### 我想生成型別安全的程式碼
→ [第五章](ch05_templates.md)（模板系統）→ [第六章](ch06_codegen.md)（程式碼整合）

### 我想驗證執行時的遙測訊號是否合規
→ [第七章](ch07_livecheck.md)（live-check）

### 我想把 Weaver 加入 CI/CD 流程
→ [第八章](ch08_cicd.md)（CI/CD）

### 我想用 AI 輔助修復遙測問題
→ [第九章](ch09_mcp.md)（MCP Server）

### 我想安全地修改已上線的 Schema
→ [第十章](ch10_evolution.md)（版本管理）

### 我想在企業環境中管理多個團隊的 Schema
→ [第十一章](ch11_enterprise.md)（企業整合）

### 我想從頭跑一次完整的端到端範例
→ [第十二章](ch12_workshop.md)（Workshop）

---

## 學習路線建議

**初學者（第一次接觸 Weaver）**：
1 → 2 → 3 → 5 → 6 → 12

**已有基礎，想加強 CI 防護**：
4 → 7 → 8

**SRE 關注生產穩定性**：
10 → 11 → 4（Policy 中的破壞性變更防護）

**AI 工具用戶**：
9（依賴第三章的 Schema 知識）

---

> 本指南根據 `weaver-demo/` 教材內容整理，加入 Go 1.22 與 Python 3.11 實作範例。
> 所有生成的程式碼應放在 `generated_from_template/` 目錄並納入版本控制。
