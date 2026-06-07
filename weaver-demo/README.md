這份涵蓋 OpenTelemetry Weaver 核心概念、實作與進階整合的素材非常豐富！為您規劃了一份為期 **2 天、總計約 12 小時的「OpenTelemetry Weaver：設計即具備可觀測性 (Observability by Design) 實戰培訓」**教材大綱。

這份教材專為開發人員、SRE 與 DevOps 工程師設計，將從核心理念一路帶領學員實作到 CI/CD 的自動化防護與 AI 輔助重構。

---

# 📚 OpenTelemetry Weaver 實戰培訓教材 (2 天 / 12 小時)

## 🗓️ 第一天：奠定基礎與 Schema 定義 (共 6 小時)
**目標：** 了解可觀測性痛點、掌握 Weaver 的核心理念，並學會撰寫與驗證遙測 Schema，最後自動生成文件與程式碼。

### 🕒 第 1 小時：觀念啟蒙 — 可觀測性即設計 (Observability by Design)
*   **痛點分析：** 探討部署後警報失效、指標命名不一致導致查詢困難、缺少關鍵監控數據等常見問題。
*   **核心理念：** 將遙測訊號（指標、追蹤、日誌）視為與程式碼同等重要的一等「公開 API」。
*   **Weaver 簡介：** 認識 OpenTelemetry Weaver 工具集及其如何解決遙測可靠性與一致性的問題。
*   **了解語義慣例 (Semantic Conventions)：** 探討 OpenTelemetry 的公開標準庫與 T 型訊號定義。

### 🕒 第 2 小時：Weaver 環境建置與核心工具概覽
*   **環境安裝：** 介紹預編譯二進位檔、Docker、原始碼編譯與 CI/CD (GitHub Actions) 等安裝選項。
*   **Weaver 四大核心工作流：** 建立 Schema、準備模板、自動生成產出物、在 CI 中驗證。
*   **指令總覽：** 快速認識 `check`、`resolve`、`diff`、`generate`、`live-check`、`emit` 與 `mcp` 等指令的用途。

### 🕒 第 3 小時：建立團隊的第一個遙測 Schema (實作)
*   **YAML 檔案結構解析：** 學習如何使用 YAML 定義 Spans（追蹤）、Metrics（指標）、Logs/Events（日誌）及其屬性。
*   **目錄與檔案最佳實踐：** 將不同訊號類型拆分至獨立檔案（例如 `telemetry-schema/` 目錄），並將跨服務通用的屬性（Common attributes）獨立以供參照。
*   **實作練習：** 定義一個電子商務的「訂單支付 (Order Payment)」微服務 Schema，包含計數器指標 (Count) 與測量單位。

### 🕒 第 4 小時：靜態驗證與自訂策略 (Policy) 防護
*   **基本語法驗證：** 使用 `weaver registry check` 指令驗證 YAML 格式與語義。
*   **Rego 策略語言簡介：** 學習使用 Open Policy Agent (OPA) 的 Rego 語言撰寫自訂策略。
*   **實作練習：** 撰寫策略以強制規定特定的指標前綴，並將原本為選填的欄位改為必填。進階練習：強制所有服務的資源屬性必須包含 `git.tag` 版本資訊。

### 🕒 第 5 小時：利用 Jinja2 模板自動生成文件
*   **模板驅動開發：** 認識 Weaver 支援的 Jinja2 模板引擎。
*   **現成模板應用：** 介紹社群提供的 Java、Go 語言或 Markdown 現成模板。
*   **文件生成實作：** 使用 `weaver registry generate` 搭配 Markdown 模板，一鍵生成系統遙測規格的說明文件，並學習使用 `update-markdown` 更新特定區塊。

### 🕒 第 6 小時：型別安全 (Type-safe) 的程式碼生成
*   **為什麼需要型別安全？** 避免工程師手動輸入錯誤名稱（如拼字錯誤）導致的遙測資料不合規。
*   **程式碼生成實作：** 利用 Weaver 生成 Go 語言的遙測介面 (API) 與常數。
*   **IDE 整合：** 體驗開發時 IDE 根據生成程式碼提供的自動補齊與提示功能，實現流暢的開發體驗。

---

## 🗓️ 第二天：CI/CD 整合、動態驗證與進階管理 (共 6 小時)
**目標：** 將 Weaver 整合進開發與部署流程，實施動態阻擋機制，學會與 AI 代理協作重構，並探討跨團隊的擴展策略。

### 🕒 第 1 小時：動態實時驗證 (Live-checking) 原理
*   **靜態 vs 動態驗證：** 了解靜態檢查的極限，以及為何需要實時檢查實際產生的 OTLP 訊號。
*   **彈性的輸入與輸出設定：** 認識 Live-check 支援的多種輸入來源（檔案、標準輸入、OTLP）與輸出格式（YAML、JSON）。
*   **模擬遙測發送：** 使用 `weaver registry emit` 根據 Schema 自動產生並發送範例遙測訊號，進行初步測試。

### 🕒 第 2 小時：將 Weaver 整合入 CI/CD 流程
*   **GitHub Actions 整合：** 使用 `setup-weaver` 建立 CI 工作流，自動驗證每次 PR 的 Schema 語法與生成的程式碼是否同步。
*   **在測試階段執行 Live-check (實作)：** 將 OTel 訊號發送至 `weaver registry live-check` 的監聽埠 (`0.0.0.0:4318`)。
*   **打造把關機制：** 演練當程式碼發出的訊號不符 Schema 規範時，Live-check 如何以 Exit Code 1 終止測試，並產出合規與覆蓋率報告，防止錯誤部署。

### 🕒 第 3 小時：AI 輔助重構 (MCP Server 整合)
*   **重構的挑戰：** 當 CI 流程因遙測不合規被阻擋時，工程師面臨的重構負擔。
*   **啟動 MCP 伺服器：** 使用 `weaver registry mcp` 指令將 Weaver 註冊表暴露給 AI 工具。
*   **與 AI Coding Agent 協作：** 搭配支援 MCP 的 AI 助手（如 GitHub Copilot、Claude Code 等），讓 AI 讀懂遙測規範並自動輔助重構未通過 CI 驗證的程式碼。

### 🕒 第 4 小時：Schema 的演進、版本比較與解析
*   **版本演進與破壞性變更防護：** 探討如何使用 Weaver 的政策防止隨意刪除警報所依賴的必填屬性。
*   **註冊表依賴與解析：** 使用 `weaver registry resolve` 將註冊表與依賴項解析為單一工件，並用 `weaver registry diff` 比較不同版本的差異。
*   **廢棄欄位 (Deprecated) 管理：** 學習在註冊表中標示廢棄欄位，引導使用者過渡到穩定的新欄位，避免直接破壞依賴的應用程式。

### 🕒 第 5 小時：企業級應用 — 整合遺留系統與共享生態
*   **共存策略：** 參考業界（如 Comcast）案例，學習如何將自有的遺留應用程式 (Legacy applications) 屬性注入到標準 Semantic Conventions 目錄中，提供單一的 Schema 來源。
*   **發布與共享註冊表：** 探討企業如何發布自己的客製化語義慣例供其他團隊或系統（如資料庫引擎）調用。
*   **未來展望：** 探討查詢時動態適應指標變化的機制，以及與資料目錄 (Data catalogs) 原生整合的可能性。

### 🕒 第 6 小時：綜合戰鬥營 (Workshop) & 結業 Q&A
*   **端到端實戰挑戰：**
    1. 定義一個全新的微服務 Schema。
    2. 撰寫 Jinja2 模板並生成程式碼結構。
    3. 實作遙測發送邏輯。
    4. 刻意修改程式碼植入錯誤的屬性名稱，並觀察 Live-check 在 CI 階段的攔截。
    5. 使用 MCP 伺服器結合 AI 完成程式碼修正。
*   **總結回顧與 Q&A 交流。**

---
這套課程設計由淺入深，讓學員在兩天的時間內，從了解 Weaver「設計即具備可觀測性」的核心哲學，進階到能夠利用 CI/CD 與 AI 工具建立嚴密的自動化防護網。如果有哪個章節您希望能進一步深入展開（例如想看更多 YAML 或 Rego 的具體教學投影片內容），隨時可以告訴我！