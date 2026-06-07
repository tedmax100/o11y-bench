# 第三章：撰寫第一個遙測 Schema

> 本章完整介紹 Weaver Schema YAML 的所有欄位與型別，包含 attribute_group、span、metric 的撰寫方式，以及 ref 機制的運作原理，並附常見錯誤對照。

---

## 3.1 專案結構最佳實踐

```
my-service/
├── telemetry/
│   └── registry/
│       ├── common.yaml          # 共用屬性（跨服務 ref 用）
│       ├── payment-spans.yaml   # 訂單支付 Span 定義
│       ├── payment-metrics.yaml # 訂單支付 Metric 定義
│       └── cart-metrics.yaml    # 購物車 Metric 定義
├── templates/
│   ├── go/                      # Go 程式碼生成模板
│   └── python/                  # Python 程式碼生成模板
├── policies/
│   └── enforce_naming.rego      # 自訂命名規則
└── generated_from_template/     # 自動生成的程式碼（勿手動修改）
```

### 為什麼要拆分多個 YAML 檔案？

Weaver 的 `--registry` 參數可以指向一個目錄，它會自動讀取該目錄下所有的 `.yaml` 檔案並合併處理。

拆分的好處：
- **關注點分離**：每個服務領域（payment、cart、auth）有獨立檔案，便於 PR review
- **減少衝突**：不同團隊同時修改不同領域的 Schema，git merge conflict 機率大幅降低
- **可讀性**：一個 YAML 50 行比一個 YAML 500 行更容易閱讀和理解

---

## 3.2 Schema YAML 的完整欄位說明

每個 YAML 檔都是一個 **registry**，由一個或多個 `group` 組成。

### group 的共用必填欄位

```yaml
groups:
  - id: span.payment.process     # 唯一識別碼，命名慣例：<type>.<namespace>.<name>
    type: span                   # 類型（見下方類型說明）
    stability: stable            # 穩定度：stable / development / deprecated
    brief: "簡短說明（必填）"    # 一行描述，模板會把它轉成程式碼註解
```

### `type` 有哪些值？

| type | 用途 | 專屬欄位 |
|------|------|---------|
| `attribute_group` | 定義可被 ref 引用的屬性集合 | — |
| `span` | 定義一個追蹤 Span 的規格 | `span_kind` |
| `metric` | 定義一個指標的規格 | `metric_name`, `instrument`, `unit` |
| `event` | 定義一個 Log Event | — |
| `resource` | 定義資源屬性（SDK Resource） | — |

### `stability` 的意義

| 值 | 意義 | 能否刪除 |
|----|------|---------|
| `stable` | 生產可用，有向後相容保證 | 不能直接刪，要先 deprecated |
| `development` | 實驗性，未來可能改變 | 可刪 |
| `deprecated` | 已廢棄，計畫移除 | 需寫 `deprecated: "說明"` |

### group 的可選欄位

```yaml
groups:
  - id: span.payment.process
    type: span
    stability: stable
    brief: "處理訂單支付流程的 Span"
    note: |                        # 詳細補充說明（選填）
      此 Span 代表完整的支付處理流程，
      從接收請求到收到支付閘道回應為止。
    prefix: "payment"              # 屬性 id 的共同前綴（通常不建議設定，讓 id 保持完整）
    extends: "common.base_span"    # 繼承另一個 group 的屬性（進階用法）
    span_kind: server              # span 專屬
    attributes: []                 # 屬性列表
```

---

## 3.3 attribute 的欄位說明

屬性是 Schema 的最小單元，每個 attribute 有以下欄位：

```yaml
attributes:
  - id: payment.order_id         # 屬性名稱（用 . 分隔命名空間）
    type: string                 # 資料型別（見下方）
    stability: stable
    brief: "唯一的訂單識別碼"
    examples: ["ord-001", "ord-002"]   # 範例值（string/int/double 需要）
    requirement_level: required  # 填寫規則（見下方）
    note: "額外補充說明（選填）"

  # 引用其他 group 已定義的屬性（不重複定義）
  - ref: git.tag                 # ref 只需要屬性 id，不用重新寫欄位
```

### `type` 支援哪些值？

| type | Go 對應 | Python 對應 | 說明 |
|------|---------|------------|------|
| `string` | `attribute.Key.String()` | `str` | 字串 |
| `int` | `attribute.Key.Int()` | `int` | 整數 |
| `double` | `attribute.Key.Float64()` | `float` | 浮點數 |
| `boolean` | `attribute.Key.Bool()` | `bool` | 布林值 |
| `string[]` | `attribute.Key.StringSlice()` | `list[str]` | 字串陣列 |
| `int[]` | `attribute.Key.IntSlice()` | `list[int]` | 整數陣列 |
| `double[]` | `attribute.Key.Float64Slice()` | `list[float]` | 浮點數陣列 |
| `boolean[]` | `attribute.Key.BoolSlice()` | `list[bool]` | 布林值陣列 |
| `enum`（用 members 定義） | 常數字串 | 常數字串 | 固定選項 |

### Enum 型別的完整寫法

```yaml
- id: payment.provider
  type:
    members:
      - id: stripe           # 程式碼中的識別名稱（snake_case）
        value: "stripe"      # 實際發送的字串值
        brief: "Stripe 信用卡支付"
        stability: stable
      - id: paypal
        value: "paypal"
        brief: "PayPal 電子錢包"
        stability: stable
      - id: bank_transfer
        value: "bank_transfer"
        brief: "銀行轉帳"
        stability: stable
      - id: crypto           # 實驗性功能，尚未正式支援
        value: "crypto"
        brief: "加密貨幣（實驗性）"
        stability: development
  stability: stable
  brief: "支付服務提供商"
```

### `requirement_level` 有哪些值？

| 值 | 意義 | live-check 行為 |
|----|------|----------------|
| `required` | 必填 | 缺少此欄位報 violation（高嚴重度）|
| `recommended` | 建議填寫 | 缺少有警告（低嚴重度）|
| `opt_in` | 選填，明確選擇才填 | 缺少不警告 |
| `conditionally_required: "條件說明"` | 在特定條件下必填 | 視條件評估 |

### conditionally_required 正確寫法（YAML 語法注意）

```yaml
# ✓ 正確：縮排成 mapping
requirement_level:
  conditionally_required: "僅在支付失敗時填入"

# ❌ 錯誤：內聯寫法會解析失敗
requirement_level: conditionally_required: "..."
```

---

## 3.4 attribute_group：定義可重用的屬性集

`attribute_group` 是「屬性的共用庫」，它本身不代表任何訊號。其他 group 透過 `ref` 引用它定義的屬性，Weaver 在 resolve 時自動展開。

```yaml
# telemetry/registry/common.yaml
groups:
  - id: common.resource
    type: attribute_group
    brief: "跨服務通用的資源屬性"
    attributes:
      - id: git.tag
        type: string
        stability: stable
        brief: "部署的 Git 版本標籤"
        examples: ["v1.0.0", "v2.1.3-rc1"]
        requirement_level: required

      - id: deployment.environment
        type: string
        stability: stable
        brief: "服務部署環境"
        examples: ["production", "staging", "development"]
        requirement_level: required

      - id: service.team
        type: string
        stability: stable
        brief: "負責此服務的團隊名稱"
        examples: ["payments", "cart", "auth"]
        requirement_level: recommended
```

### 為什麼要用 attribute_group？

假設你有 10 個 Span，每個都需要 `git.tag` 和 `deployment.environment`。

❌ 不用 attribute_group：在每個 group 中重複定義，共 10 份重複程式碼。若 `deployment.environment` 的 brief 需要更新，要改 10 個地方。

✓ 用 attribute_group：定義一次，10 個 group 都 `ref` 它。修改只需要改一個地方，Weaver resolve 後自動同步。

---

## 3.5 span：定義追蹤規格

```yaml
# telemetry/registry/payment-spans.yaml
groups:
  - id: span.payment.process   # 慣例：span.<namespace>.<operation>
    type: span
    span_kind: server          # server / client / producer / consumer / internal
    stability: stable
    brief: "處理訂單支付流程的 Span"
    attributes:
      - ref: git.tag                    # 引用 common.resource 中定義的屬性
      - ref: deployment.environment     # 同上

      - id: payment.order_id            # 此 span 專屬的屬性
        type: string
        stability: stable
        brief: "唯一的訂單識別碼"
        examples: ["ord-20240601-001"]
        requirement_level: required

      - id: payment.status
        type: string
        stability: stable
        brief: "支付結果"
        examples: ["success", "failed"]
        requirement_level: required

      - id: error.type
        type: string
        stability: stable
        brief: "失敗時的錯誤類型"
        examples: ["card_declined", "network_error"]
        requirement_level:
          conditionally_required: "僅在 payment.status = failed 時填入"
```

### `span_kind` 的完整說明

| span_kind | 使用場景 |
|-----------|---------|
| `server` | 接收 RPC / HTTP 請求的服務端（最常見）|
| `client` | 發出 RPC / HTTP 請求的呼叫方 |
| `producer` | 往訊息佇列（Kafka、RabbitMQ）寫入訊息 |
| `consumer` | 從訊息佇列消費訊息 |
| `internal` | 服務內部的作業，不跨越服務邊界 |

---

## 3.6 metric：定義指標規格

Metric 有三個 Span 沒有的必填欄位：`metric_name`、`instrument`、`unit`。

```yaml
# telemetry/registry/payment-metrics.yaml
groups:
  - id: metric.payment.amount    # 慣例：metric.<namespace>.<name>
    type: metric
    metric_name: payment.amount  # 程式碼中 meter.Float64Histogram() 的名稱
    instrument: histogram        # 見下方 instrument 說明
    unit: "{TWD}"                # UCUM 單位格式
    stability: stable
    brief: "每筆支付的金額分佈"
    attributes:
      - ref: payment.provider
      - ref: deployment.environment
```

### `instrument` 有哪些值？

| instrument | 說明 | 適用場景 | Go API | Python API |
|-----------|------|---------|--------|------------|
| `counter` | 只增不減的計數器 | 請求次數、錯誤次數 | `meter.Int64Counter` | `create_counter` |
| `updowncounter` | 可增可減 | 活躍連線數、隊列長度 | `meter.Int64UpDownCounter` | `create_up_down_counter` |
| `histogram` | 分佈統計 | 延遲（ms）、金額 | `meter.Float64Histogram` | `create_histogram` |
| `gauge` | 即時觀測值 | CPU 使用率、記憶體 | `meter.Float64ObservableGauge` | `create_observable_gauge` |

### `unit` 格式規範（UCUM）

```
"ms"          → 毫秒（milliseconds）
"s"           → 秒（seconds）
"us"          → 微秒（microseconds）
"ns"          → 奈秒（nanoseconds）
"By"          → Bytes
"KiBy"        → Kibibytes (1024 bytes)
"MiBy"        → Mebibytes
"GiBy"        → Gibibytes
"{requests}"  → 自訂單位用 {} 包裹
"{errors}"
"{TWD}"       → 新台幣（自訂貨幣單位）
"1"           → 無單位（比率、百分比）
"%"           → 百分比（少用，建議用 "1"）
```

---

## 3.7 ref 機制：避免重複定義

`ref` 讓你在 span/metric 中引用 `attribute_group` 裡的屬性，不需要重寫欄位，Weaver resolve 時會自動展開：

```yaml
# span 中使用 ref
attributes:
  - ref: git.tag               # 完整繼承 common.resource 的 git.tag 定義
  - ref: deployment.environment

  # 也可以 ref 後覆寫部分欄位（例如改 requirement_level）
  - ref: service.team
    requirement_level: required  # 覆寫：此 span 中 service.team 為必填
```

### resolve 後的實際結構

執行 `weaver registry resolve` 可以看到 ref 展開後的完整 JSON：

```json
{
  "name": "git.tag",
  "type": "string",
  "brief": "部署的 Git 版本標籤",
  "stability": "stable",
  "requirement_level": "required",
  "examples": ["v1.0.0", "v2.1.3-rc1"]
}
```

### ref 的限制

- `ref` 只能引用 `attribute_group` 中定義的屬性（id 必須存在）
- 不能 ref 另一個 span 或 metric 中直接定義的屬性
- ref 後可覆寫的欄位：`requirement_level`、`note`、`brief`（覆寫原本的 brief）

---

## 3.8 完整的多層次 Schema 範例

以下是一個符合最佳實踐的完整 Schema 結構：

```yaml
# telemetry/registry/common.yaml — 共用屬性庫
groups:
  - id: common.resource
    type: attribute_group
    brief: "跨服務通用資源屬性"
    attributes:
      - id: git.tag
        type: string
        stability: stable
        brief: "Git 版本標籤"
        examples: ["v1.0.0"]
        requirement_level: required
      - id: deployment.environment
        type: string
        stability: stable
        brief: "部署環境"
        examples: ["production", "staging"]
        requirement_level: required
```

```yaml
# telemetry/registry/payment-spans.yaml — 支付 Span 定義
groups:
  - id: span.payment.process
    type: span
    span_kind: server
    stability: stable
    brief: "處理訂單支付流程的 Span"
    attributes:
      - ref: git.tag
      - ref: deployment.environment
      - id: payment.order_id
        type: string
        stability: stable
        brief: "唯一訂單識別碼"
        examples: ["ord-20240601-001"]
        requirement_level: required
      - id: payment.provider
        type:
          members:
            - id: stripe
              value: "stripe"
              brief: "Stripe"
              stability: stable
            - id: paypal
              value: "paypal"
              brief: "PayPal"
              stability: stable
        stability: stable
        brief: "支付服務提供商"
        requirement_level: required
      - id: payment.status
        type: string
        stability: stable
        brief: "支付結果"
        examples: ["success", "failed"]
        requirement_level: required
      - id: error.type
        type: string
        stability: stable
        brief: "失敗時的錯誤類型"
        examples: ["card_declined", "network_error", "timeout"]
        requirement_level:
          conditionally_required: "僅在 payment.status = failed 時填入"
```

```yaml
# telemetry/registry/payment-metrics.yaml — 支付 Metric 定義
groups:
  - id: metric.payment.amount
    type: metric
    metric_name: payment.amount
    instrument: histogram
    unit: "{TWD}"
    stability: stable
    brief: "每筆支付的金額分佈"
    attributes:
      - ref: payment.provider
      - ref: deployment.environment

  - id: metric.payment.errors
    type: metric
    metric_name: payment.errors
    instrument: counter
    unit: "{errors}"
    stability: stable
    brief: "支付失敗次數"
    attributes:
      - id: error.type
        type: string
        stability: stable
        brief: "錯誤類型"
        examples: ["card_declined", "timeout"]
        requirement_level: required
      - ref: deployment.environment
```

---

## 3.9 執行靜態驗證

```bash
# 基本語法驗證
weaver registry check --registry ./telemetry/registry

# 正常輸出：
# ✔ No `after_resolution` policy violation

# 常見錯誤訊息對照：
# "missing required field 'unit'"       → metric 缺少 unit
# "missing required field 'stability'"  → group 缺少 stability
# "mapping values are not allowed"      → conditionally_required 內聯語法錯誤
# "undefined value"                     → 模板中使用了不存在的欄位名稱
# "unknown ref 'git.tag'"               → ref 指向的屬性 id 不存在
```

### 驗證後看 resolve 輸出，確認 ref 展開是否正確

```bash
weaver registry resolve \
  --registry ./telemetry/registry \
  --format json | python3 -m json.tool | head -80
```

---

## 3.10 常見錯誤對照

### 錯誤一：metric 缺少必填欄位

```yaml
# ❌ 錯誤：metric 缺少 unit
groups:
  - id: metric.payment.amount
    type: metric
    metric_name: payment.amount
    instrument: histogram
    # 缺少 unit！
    stability: stable
    brief: "支付金額"

# ✓ 正確
groups:
  - id: metric.payment.amount
    type: metric
    metric_name: payment.amount
    instrument: histogram
    unit: "{TWD}"     # ← 必填
    stability: stable
    brief: "支付金額"
```

### 錯誤二：conditionally_required 內聯語法

```yaml
# ❌ 錯誤：YAML 會解析失敗
- id: error.type
  requirement_level: conditionally_required: "失敗時填入"

# ✓ 正確：使用縮排的 mapping 語法
- id: error.type
  requirement_level:
    conditionally_required: "失敗時填入"
```

### 錯誤三：ref 指向不存在的屬性

```yaml
# ❌ 錯誤：common.resource 中沒有 git.commit_sha
- ref: git.commit_sha

# ✓ 正確：確認屬性 id 存在於某個 attribute_group 中
- ref: git.tag    # 存在於 common.resource group 中
```

### 錯誤四：examples 格式錯誤

```yaml
# ❌ 錯誤：string 類型的 examples 應該是字串陣列
- id: payment.order_id
  type: string
  examples: "ord-001"   # 應該是陣列

# ✓ 正確
- id: payment.order_id
  type: string
  examples: ["ord-001", "ord-002"]
```

---

## 延伸閱讀

- [OTel Semantic Conventions Schema 格式規範](https://opentelemetry.io/docs/specs/otel/schemas/)
- [UCUM 單位規範](https://ucum.org/ucum)
- [OTel Attribute 型別規範](https://opentelemetry.io/docs/specs/otel/common/)
