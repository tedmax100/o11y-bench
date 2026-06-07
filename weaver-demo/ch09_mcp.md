# 第九章：AI 輔助重構 (MCP Server)

> 本章介紹如何啟動 Weaver MCP Server，讓 AI 助手能直接讀取你的 Schema 並提供符合規範的重構建議，包含 Claude Code 的設定方式與完整的 Prompt 工作流範例。

---

## 9.1 MCP 的作用與設計理念

### 什麼是 MCP（Model Context Protocol）？

MCP 是 Anthropic 提出的開放協定，讓 AI 助手（如 Claude）能夠透過標準化介面存取外部資料來源和工具。

### 為什麼 Weaver 需要 MCP？

當 CI 被 live-check 攔截後，開發人員需要：
1. 看懂 Weaver 報告（哪個屬性名稱錯了）
2. 查詢正確的屬性名稱（翻 YAML 或查文件）
3. 修改程式碼中所有用到該屬性的地方
4. 確認修改後符合 Schema

如果有 10 個屬性需要修正，這個流程很繁瑣。

**MCP 的解法**：讓 AI 直接讀取你的 Schema，然後你只需要描述問題，AI 就能根據 Schema 直接生成符合規範的修改。

```
開發人員 → 告訴 AI「CI 報告說 order_id 不合規」
AI → 讀取 MCP (Schema) → 找到正確名稱 payment.order_id
AI → 讀取程式碼 → 找到所有用到 order_id 的地方
AI → 生成修改建議（使用生成的常數 semconv.PAYMENT_ORDER_ID）
```

---

## 9.2 啟動 Weaver MCP 伺服器

```bash
# 基本啟動
weaver registry mcp \
  --registry ./telemetry/registry

# 預期輸出：
# ✓ MCP Server started
# ✓ Registry loaded: 15 groups, 42 attributes, 8 metrics
# ✓ Listening on stdio (MCP stdio transport)
```

**重要**：Weaver MCP 使用 **stdio transport**（標準輸入/輸出），不是 HTTP。這是 MCP 協定的標準方式，AI 工具透過啟動子程序與 MCP 伺服器通訊。

---

## 9.3 在 Claude Code 中設定 MCP

### 方式一：專案級設定（推薦）

```json
// .claude/settings.json
{
  "mcpServers": {
    "weaver": {
      "command": "weaver",
      "args": [
        "registry",
        "mcp",
        "--registry",
        "./telemetry/registry"
      ]
    }
  }
}
```

設定後，在 Claude Code 工作階段中，AI 會自動取得 Schema 上下文。

### 方式二：使用 Docker 版本

```json
// .claude/settings.json
{
  "mcpServers": {
    "weaver": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "-v", "/path/to/your/project:/workspace",
        "-w", "/workspace",
        "otel/weaver:latest",
        "registry",
        "mcp",
        "--registry",
        "./telemetry/registry"
      ]
    }
  }
}
```

### 方式三：全域設定（適合 mono-repo）

```json
// ~/.claude/settings.json（全域設定）
{
  "mcpServers": {
    "weaver-myproject": {
      "command": "weaver",
      "args": [
        "registry",
        "mcp",
        "--registry",
        "/absolute/path/to/telemetry/registry"
      ]
    }
  }
}
```

### 驗證 MCP 已載入

在 Claude Code 中輸入：
```
請列出目前 Weaver Schema 中所有定義的 metric 名稱。
```

若 MCP 正確連接，AI 應能回答出你 Schema 中的實際 metric 名稱。

---

## 9.4 AI 重構 Prompt 工作流

### 場景一：CI 攔截後的修復

CI 報告：
```yaml
violations:
  - span: "payment.process"
    attribute: "order_id"
    error: "屬性 'order_id' 不在 Schema 定義中"
  - span: "payment.process"
    missing_required: "payment.status"
```

Prompt：
```
我的 CI 被 Weaver live-check 攔截，報告顯示：
1. 屬性 'order_id' 不在 Schema 定義中
2. required 屬性 'payment.status' 未出現

請幫我修復 payment/service.go，讓它符合 Weaver Schema 中定義的屬性規範。
請使用 generated_from_template/semconv_attrs.go 中的生成常數，而不是手動輸入字串。
```

AI 根據 MCP 讀取 Schema 後會建議：

```go
// 修復前（違規）
span.SetAttributes(
    attribute.String("order_id", req.OrderID),
    attribute.String("provider", req.Provider),
    // 缺少 payment.status
)

// 修復後（AI 根據 Schema 自動建議）
span.SetAttributes(
    semconv.PAYMENT_ORDER_ID.String(req.OrderID),     // ← order_id → payment.order_id
    semconv.PAYMENT_PROVIDER.String(req.Provider),    // ← provider → payment.provider
    semconv.PAYMENT_STATUS.String("success"),          // ← 補上 required 屬性
    semconv.GIT_TAG.String(req.GitTag),               // ← 補上 required 屬性
    semconv.DEPLOYMENT_ENVIRONMENT.String(req.Env),  // ← 補上 required 屬性
)
```

### 場景二：新功能的遙測設計

Prompt：
```
我要在 cart-service 中加入「移除商品」功能（RemoveItem）。
請根據現有的 Weaver Schema 風格，幫我：
1. 設計一個新的 span.cart.remove_item Schema group
2. 設計相應的 metric.cart.remove_item.count
3. 在 cart/service.go 中實作遙測埋點
```

AI 會參考現有 Schema 的風格和慣例來生成一致的新定義。

### 場景三：Schema 遷移

Prompt：
```
我需要把所有 span 中的 deployment.environment 屬性從 string 改成 enum，
只允許 "production"、"staging"、"development" 三個值。

請幫我：
1. 更新 telemetry/registry/common.yaml 中的 deployment.environment 定義
2. 更新所有使用 DEPLOYMENT_ENVIRONMENT 的程式碼，改用 enum 常數
```

---

## 9.5 MCP Server 提供的 Tools

Weaver MCP Server 向 AI 工具暴露以下能力（Tool 列表）：

| Tool 名稱 | 用途 |
|---------|------|
| `list_groups` | 列出所有 Schema groups |
| `get_group` | 取得特定 group 的完整定義 |
| `list_attributes` | 列出所有定義的屬性 |
| `get_attribute` | 取得特定屬性的完整定義 |
| `search` | 搜尋屬性名稱或 group id |
| `list_metrics` | 列出所有 metric 定義 |
| `validate_span` | 驗證一組屬性是否符合某個 span 的 Schema |

這些 Tool 讓 AI 能夠：
- 查詢正確的屬性名稱（而非猜測或從記憶中提取）
- 驗證它生成的程式碼是否符合 Schema
- 在多個 Schema 中搜尋相關定義

---

## 9.6 最佳實踐：讓 AI 輔助更有效

### 提示一：明確指定 Schema 來源

```
# ❌ 模糊
請幫我修復遙測埋點

# ✓ 明確
請根據 Weaver MCP Schema 中的 span.payment.process 定義，
修復 payment/service.go 的遙測埋點
```

### 提示二：提供 CI 報告的完整內容

```
這是 Weaver live-check 的報告（從 CI artifact 下載）：

[貼上完整的 weaver-report.yaml 內容]

請根據這份報告修復所有違規項目。
```

### 提示三：指定使用生成的常數

```
修復時請一律使用 generated_from_template/ 目錄下生成的常數，
不要手動寫字串。例如用 semconv.PAYMENT_ORDER_ID 而非
attribute.String("payment.order_id", ...)
```

### 提示四：批次修復多個檔案

```
這個專案有 3 個服務（payment、cart、inventory）都有遙測埋點問題。
請先讀取每個服務的程式碼，然後一次修復所有違規。
請確保每個服務都使用了生成的 semconv 常數，
並且所有 required 屬性都有設定。
```

---

## 9.7 與其他 AI 工具整合

### GitHub Copilot（VS Code 擴充）

GitHub Copilot 也支援 MCP，可在 VS Code 的 `settings.json` 中設定：

```json
// .vscode/settings.json
{
  "github.copilot.advanced": {
    "mcp": {
      "servers": {
        "weaver": {
          "command": "weaver",
          "args": ["registry", "mcp", "--registry", "./telemetry/registry"]
        }
      }
    }
  }
}
```

### 自訂 AI Agent 整合

若你有自行開發的 AI Agent，可以透過 MCP 客戶端程式庫連接 Weaver：

```python
# 使用 Python MCP 客戶端
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def query_schema():
    server_params = StdioServerParameters(
        command="weaver",
        args=["registry", "mcp", "--registry", "./telemetry/registry"],
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            # 列出所有 groups
            result = await session.call_tool("list_groups", {})
            print(result)
            
            # 查詢特定屬性
            attr = await session.call_tool("get_attribute", {"name": "payment.order_id"})
            print(attr)
```

---

## 9.8 常見問題

### 問題：AI 建議的屬性名稱與 Schema 不符

這通常表示 MCP 沒有正確連接。確認步驟：

```bash
# 手動測試 MCP 是否正常啟動
weaver registry mcp --registry ./telemetry/registry
# 應該顯示 "Registry loaded: X groups, Y attributes, Z metrics"

# 在 Claude Code 中確認 MCP 已載入
# 輸入：/mcp 查看已連接的 MCP servers
```

### 問題：AI 生成了 Schema 中不存在的屬性

提示 AI 先查詢 Schema，再生成程式碼：

```
在生成任何程式碼之前，請先用 Weaver MCP 的 list_attributes 工具
確認 Schema 中有哪些屬性，然後只使用 Schema 中存在的屬性。
```

### 問題：MCP 連線超時

```bash
# 增加超時設定
{
  "mcpServers": {
    "weaver": {
      "command": "weaver",
      "args": ["registry", "mcp", "--registry", "./telemetry/registry"],
      "timeout": 30000    // 毫秒
    }
  }
}
```

---

## 延伸閱讀

- [Model Context Protocol 官方規格](https://modelcontextprotocol.io/)
- [Claude Code MCP 文件](https://docs.anthropic.com/en/docs/claude-code/mcp)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Weaver MCP 功能說明](https://github.com/open-telemetry/weaver/blob/main/docs/mcp.md)
