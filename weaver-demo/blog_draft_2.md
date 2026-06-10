# OpenTelemetry Weaver — 第二篇：讓 Schema 成為你的 Merge Gate

> 上一篇我們建立了 Telemetry Registry，學會靜態驗證、程式碼生成、live-check。  
> 這篇要把這條防線推進到 PR 階段——讓錯誤的 Schema 變更**根本不可能合進 main**。

---

## 為什麼需要 Merge Gate？

假設你的團隊用 Weaver 管理 Schema 已經三個月了。某天有人修了一個 payment span，順手把 `payment.order_id` 改成 `payment.orderId`（camelCase），因為他剛從 Java 專案轉過來。

這個改動通過了 code review（reviewer 沒看 YAML）、也沒有 CI 擋下來。兩週後，你的 Grafana dashboard 開始出現空值，Loki 的查詢也炸了——因為所有下游消費者都在找 `payment.order_id`，但這個 key 已經不存在了。

這就是 **Schema breaking change 沒有被管住**的後果。Schema 的版本控制和 API contract testing 是同一個概念：你不會讓人隨便改 REST API 的 response field 名稱，同樣地，telemetry attribute 的名稱也是合約的一部分。

---

## Schema 完整參考：Registry 結構與欄位語義

在進入 CI 配置之前，先把 Schema 本身說清楚。理解每個欄位的作用，才能知道 policy 在驗證什麼，也才能寫出有意義的 breaking change 規則。

這一節的內容來自官方的 [`semconv-syntax.md`](https://github.com/open-telemetry/weaver/blob/main/schemas/semconv-syntax.md) 與 [`define-your-own-telemetry-schema.md`](https://github.com/open-telemetry/weaver/blob/main/docs/define-your-own-telemetry-schema.md)，加上我們的電商 demo 範例。

---

### 第一步：建立 manifest.yaml

Registry 資料夾裡放一個 `manifest.yaml` 是最佳實踐。沒有它 Weaver 仍然可以執行本地驗證（只會印 info 提示），但一旦你需要引用 OTel 官方 semconv 或跨 registry 依賴，就必須有它。

```yaml
# telemetry/registry/manifest.yaml
name: ecommerce-demo
description: 電商平台示範用 Telemetry Registry（購物車與支付服務）
schema_url: https://my-company.com/schemas/1.0.0
```

如果你要直接引用 OTel 官方屬性（例如 `ref: host.name`、`ref: http.request.method`），加上 `dependencies`：

```yaml
name: ecommerce-demo
description: 電商平台示範用 Telemetry Registry
schema_url: https://my-company.com/schemas/1.0.0
dependencies:
  - name: otel
    registry_path: https://github.com/open-telemetry/semantic-conventions@v1.40.0[model]
```

`@v1.40.0` 鎖定版本，確保 CI 行為穩定、不會因為官方 semconv 更新而悄悄壞掉。

---

### Registry 的檔案結構

```
telemetry/registry/
├── manifest.yaml          ← 必要，Weaver 的入口點
├── common.yaml            ← 跨服務共用的 attribute_group
├── cart-spans.yaml        ← 購物車 span 定義
├── cart-metrics.yaml      ← 購物車 metric 定義
├── payment-spans.yaml     ← 支付 span 定義
└── payment-metrics.yaml   ← 支付 metric 定義
```

每個 YAML 檔案的根節點都是 `groups`，裡面放一個或多個 semantic convention 定義。

---

### `type`：群組的五種類型

```
span           → 定義一個 trace span
metric         → 定義一個 metric 訊號
attribute_group → 定義可被引用的屬性池（不直接對應任何訊號）
resource       → 定義 resource 屬性（service.name 這類）
event          → 定義 log event（有結構化的 body）
```

`type` 省略時預設是 `span`（但 Weaver 會輸出 warning，建議明確標示）。

---

### `stability`：五個層級

官方定義了五個穩定性層級，不只是 `stable` 和 `deprecated`：

| 值 | 語義 |
|---|---|
| `stable` | 生產就緒，不會有 breaking change |
| `release_candidate` | 即將 stable，只接受細微調整 |
| `beta` | 功能完整，API 可能微調 |
| `alpha` | 早期，可能有大幅改動 |
| `development` | 實驗性，隨時可能消失 |

`deprecated` 不是 `stability` 的一個值，而是獨立的欄位（見後面「版本演進」章節）。

---

### 共用屬性：`attribute_group`

```yaml
# telemetry/registry/common.yaml
groups:
  - id: common.resource
    type: attribute_group
    brief: "跨服務通用的資源屬性"
    attributes:
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

      - id: git.tag
        type: string
        stability: stable
        brief: "部署的 Git 版本標籤"
        examples: ["v1.0.0", "v2.1.3-rc1"]
        requirement_level: required
```

`attribute_group` 沒有任何 telemetry 訊號語義，純粹是「屬性定義的倉庫」。定義一次，之後所有 span 和 metric 用 `ref: deployment.environment` 引用，改一個地方全部生效。

**`attribute_group` 的 `stability` 欄位是選填的**——這是它和其他 group type 的差異之一。

---

### Span 定義：完整欄位解析

```yaml
# telemetry/registry/payment-spans.yaml
groups:
  - id: span.payment.process
    type: span
    span_kind: server          # client | server | producer | consumer | internal
    stability: stable
    brief: "處理訂單支付流程的 Span"
    note: "涵蓋從收到請求到第三方支付 API 回應的完整週期"
    attributes:
      - ref: git.tag
      - ref: deployment.environment

      - id: payment.order_id
        type: string
        stability: stable
        brief: "唯一的訂單識別碼"
        examples: ["ord-20240601-001", "ord-20240601-002"]
        requirement_level: required

      - id: payment.provider
        type: string
        stability: stable
        brief: "支付服務提供商"
        examples: ["stripe", "paypal", "bank_transfer"]
        requirement_level: required

      - id: payment.status
        type: string
        stability: stable
        brief: "支付結果狀態"
        examples: ["success", "failed", "pending"]
        requirement_level: required

      - id: payment.currency
        type: string
        stability: stable
        brief: "貨幣代碼（ISO 4217）"
        examples: ["USD", "EUR", "TWD"]
        requirement_level: required

      - id: error.type
        type: string
        stability: stable
        brief: "失敗時的錯誤類型"
        examples: ["insufficient_funds", "card_declined", "network_error"]
        requirement_level:
          conditionally_required: "僅在 payment.status 為 failed 時填入"
        sampling_relevant: false   # 這個屬性不需要在 span 開始時就設定
```

**`span_kind` 的語義**：
- `server`：接收 RPC/HTTP 請求的端點
- `client`：發出 RPC/HTTP 請求的呼叫方
- `producer`：把訊息放入 queue（非同步）
- `consumer`：從 queue 取出訊息處理（非同步）
- `internal`：服務內部呼叫，沒有跨越網路邊界

**`requirement_level` 的四個值**：
- `required`：必填，缺少就算 violation
- `recommended`：建議填，省略不算錯（**預設值**）
- `conditionally_required: "<條件描述>"`：條件式必填，條件文字是人類可讀的說明，policy 可以讀這個值來做進一步驗證
- `opt_in`：主動選擇才填，適合高 cardinality 或敏感資料

**`sampling_relevant`**：設為 `true` 表示這個屬性需要在 span **開始時**就設定（因為 sampler 要用到它來決定是否採樣）。預設 `false`。

---

### Attribute 的型別系統

Weaver 支援的完整型別：

```yaml
# 基本型別
type: string
type: int
type: double
type: boolean

# 陣列型別
type: string[]
type: int[]
type: double[]
type: boolean[]

# 模板型別（動態 key 的字典）
type: template[string[]]

# 列舉型別（inline 定義）
type:
  members:
    - id: success
      value: "success"
      brief: "支付成功"
      stability: stable
    - id: failed
      value: "failed"
      brief: "支付失敗"
      stability: stable
    - id: pending
      value: "pending"
      brief: "等待確認"
      stability: stable
```

**Template type** 是一個特殊用法，代表「有共同前綴的動態屬性字典」。例如 OTel 官方的 `http.request.header` 就是 `template[string[]]`，它在實際使用時會展開成 `http.request.header.content_type`、`http.request.header.authorization` 等。

**Enum type** 的好處：Weaver policy 可以驗證實際打出的值是否在允許的 members 裡，在靜態驗證階段就攔截拼錯的 status 字串。

---

### Metric 定義：`instrument` 選錯會讓 query 寫不對

```yaml
# telemetry/registry/payment-metrics.yaml
groups:
  - id: metric.payment.amount
    type: metric
    metric_name: payment.amount    # Prometheus 裡看到的指標名稱
    instrument: histogram
    unit: "{TWD}"                  # 遵循 UCUM 規範，貨幣用 {} 包住
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
    brief: "支付失敗次數計數器"
    attributes:
      - ref: payment.provider

  - id: metric.payment.duration
    type: metric
    metric_name: payment.duration
    instrument: histogram
    unit: "ms"
    stability: stable
    brief: "支付處理耗時（毫秒）"
    metric_requirement_level: required   # 這個 metric 是必要的，不是可選的
    attributes:
      - ref: payment.provider
      - ref: deployment.environment
```

**`instrument` 選擇指南**：

| instrument | 特性 | 適用場景 |
|---|---|---|
| `counter` | 只增不減，cumulative | 請求數、錯誤數、bytes 發送量 |
| `histogram` | 分佈，自動產生 `_bucket`/`_sum`/`_count` | 延遲、大小、金額 |
| `gauge` | 當前瞬時值，可增可減 | CPU 使用率、記憶體、溫度 |
| `updowncounter` | 可增可減，cumulative | in-flight 請求數、queue 深度、連線數 |

`counter` 和 `updowncounter` 的差別：前者永遠增加，後者可以減少。如果用錯了，Prometheus 的 `rate()` 和 `increase()` 計算就會出錯。

**`unit` 的格式**：遵循 [UCUM](https://ucum.org/ucum) 規範。常見的：
- 時間：`s`（秒）、`ms`（毫秒）
- 大小：`By`（bytes）
- 比率：`1`（無單位）
- 自訂計數：用 `{}` 包住，如 `{requests}`、`{errors}`

**`metric_requirement_level`**：metric 本身是否必要。`required` 表示這個 metric 的存在是強制的（不只是屬性，而是整個 metric）。預設 `recommended`。

---

### `ref` 的引用機制與覆寫規則

`ref` 引用一個已存在的屬性 `id`，繼承它的 `type`、`stability`、`brief`、`examples`。當你需要修改繼承來的值時，可以直接覆寫：

```yaml
attributes:
  - ref: deployment.environment
    # 繼承 type: string, brief, examples
    # 但把這個 span 裡的 requirement_level 從 recommended 改成 required
    requirement_level: required

  - ref: http.response.status_code
    # 繼承官方定義，但補充這個服務情境下的額外 note
    note: "payment service 的 4xx 表示用戶端錯誤，5xx 表示支付閘道問題"
```

使用 `ref` 時，`id`、`type`、`stability`、`deprecated` **不可重複宣告**，只能覆寫 `brief`、`note`、`examples`、`requirement_level`。

---

### `imports`：從其他 Registry 引入群組

除了 `ref` 引用單一屬性，你還可以在檔案層級用 `imports` 區塊批次引入整個群組：

```yaml
groups:
  - id: span.cart.checkout
    type: span
    span_kind: server
    stability: stable
    brief: "購物車結帳 Span"
    attributes:
      - ref: host.name       # 從 otel 依賴引入的屬性
      - ref: cart.session_id

imports:
  metrics:
    - db.*          # 引入 otel semconv 裡所有 db.* metric
  events:
    - session.start # 引入特定 event 群組
  spans:
    - http.*        # 引入所有 http.* span 定義
```

`imports` 讓你不需要把 OTel 官方的 span 定義複製到自己的 registry，直接引用就好。wildcard `db.*` 會把整個 `db` namespace 的 metric 都帶進來。

---

### `weaver registry resolve`：看 Schema 展開後的真實樣子

所有 `ref:` 和 `imports:` 展開後的完整狀態，就是 Weaver policy 引擎實際看到的資料：

```bash
weaver registry resolve --registry ./telemetry/registry
```

輸出是完全展開的 JSON，每個 span 和 metric 的屬性列表都不含任何 `ref:`。如果你的 Rego policy 行為不符預期，先跑一次 `resolve` 看展開結果——90% 的 debug 時間都在這裡找到答案。

---

## Part 1：Policy 進階 — 偵測破壞性變更

Weaver 的 policy 在前一篇只用來做命名規範檢查。更有力的用法是：**在 Schema 被刪除或 type 被改變時，直接讓 CI fail**。

關鍵在於 Weaver 的 `--baseline-registry` 旗標。你可以在 CI 中把目前 main branch 的 registry 當作 baseline，與 PR 的 registry 比對：

```bash
weaver registry check \
  --registry ./telemetry/registry \                    # PR 的 registry
  --baseline-registry ./telemetry/registry-baseline \  # main branch 的 registry
  --policy ./policies
```

寫比對 policy 之前，要先知道 Weaver 的兩個約定：

1. **package 必須是 `comparison_after_resolution`**——只有這個階段拿得到 baseline，一般的 `after_resolution` policy 看不到它。
2. **baseline 從 `data.groups` 進來，PR 的 registry 才是 `input.groups`**。

搭配這支 breaking change policy：

**`policies/breaking_change.rego`**

```rego
package comparison_after_resolution

import rego.v1

# Weaver 的約定：
#   input.groups → 當前 registry（--registry）
#   data.groups  → baseline registry（--baseline-registry）
baseline_groups := data.groups

# 規則：屬性不可被刪除（只能標 deprecated）
deny contains violation if {
    baseline_group := baseline_groups[_]
    baseline_attr := baseline_group.attributes[_]

    # 在新版 registry 找不到同名屬性
    not attribute_exists_in_new(baseline_attr.name)

    violation := {
        "id": "breaking_attr_removed",
        "level": "violation",
        "message": sprintf(
            "屬性 '%s' 已從 registry 中移除。破壞性變更！請改用 deprecated 標記。",
            [baseline_attr.name]
        ),
        "context": {
            "group": baseline_group.id,
            "attr": baseline_attr.name,
        },
    }
}

# 規則：屬性的 type 不可改變
deny contains violation if {
    baseline_group := baseline_groups[_]
    baseline_attr := baseline_group.attributes[_]

    new_group := input.groups[_]
    new_attr := new_group.attributes[_]
    new_attr.name == baseline_attr.name

    # type 不同
    new_attr.type != baseline_attr.type

    violation := {
        "id": "breaking_type_change",
        "level": "violation",
        "message": sprintf(
            "屬性 '%s' 的 type 從 '%s' 改為 '%s'。破壞性變更！",
            [baseline_attr.name, baseline_attr.type, new_attr.type]
        ),
        "context": {
            "attr": baseline_attr.name,
            "old_type": baseline_attr.type,
            "new_type": new_attr.type,
        },
    }
}

attribute_exists_in_new(attr_name) if {
    group := input.groups[_]
    attr := group.attributes[_]
    attr.name == attr_name
}
```

實際觸發的樣子：

```
$ weaver registry check --registry ./telemetry/registry \
    --baseline-registry ./telemetry/registry-baseline \
    --policy ./policies

✗ Policy violations detected (exit code 1):

[breaking_attr_removed]
  屬性 'payment.order_id' 已從 registry 中移除。
  破壞性變更！請改用 deprecated 標記。
  → group: span.payment.process, attr: payment.order_id

CI 中斷。
```

---

## Part 2：GitHub Actions 完整配置

把上面的邏輯包進 GitHub Actions，讓每個 PR 都自動執行。

**`.github/workflows/weaver.yml`**

```yaml
name: Weaver Schema Guard

on:
  pull_request:
    paths:
      - 'telemetry/**'
      - 'policies/**'

jobs:
  schema-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # 需要取 main branch 的 registry 當 baseline

      # Weaver 沒有官方 GitHub Action，從 release 下載 binary 即可
      - name: Setup Weaver
        run: |
          curl -sSL https://github.com/open-telemetry/weaver/releases/latest/download/weaver-x86_64-unknown-linux-gnu.tar.xz \
            | tar -xJ --strip-components=1 -C /usr/local/bin

      # 用 worktree 把 main branch 展開成獨立目錄當 baseline，
      # 不用動到當前 checkout 的 PR 版本
      - name: Export baseline registry
        run: git worktree add /tmp/baseline origin/main

      # 基本 schema 格式 + 命名 policy 驗證
      - name: Validate schema + naming policy
        run: |
          weaver registry check \
            --registry ./telemetry/registry \
            --policy ./policies/enforce_naming.rego

      # Breaking change 偵測（與 main branch baseline 比對）
      - name: Detect breaking changes
        run: |
          weaver registry check \
            --registry ./telemetry/registry \
            --baseline-registry /tmp/baseline/telemetry/registry \
            --policy ./policies/breaking_change.rego

      # 確認生成的程式碼有被更新
      - name: Check generated code is up to date
        run: |
          weaver registry generate \
            --registry ./telemetry/registry \
            --templates ./templates \
            go ./generated_check

          diff -r ./generated_check ./go-service/generated/semconv/ || {
            echo "❌ Schema 已更新但生成的程式碼未同步。請執行 make generate-go 後再 commit。"
            exit 1
          }
```

這三個 check 缺一不可：
- **schema + naming**：格式正確、命名合規
- **breaking change**：不能悄悄刪屬性或改 type
- **generated code sync**：Schema 和程式碼不能脫節

---

## Part 3：Drift Detection — 抓住 Schema 與實際 Telemetry 的落差

靜態 policy 管住了 Schema 的**定義**，但還有另一個問題：**服務打出來的 telemetry 跟 Schema 一致嗎？**

這就是 Drift Detection 的場景。工程師可能：
- 忘記更新生成的 SDK，手寫字串拼錯了
- 用的是還沒更新的舊版函式庫
- 某個第三方 middleware 自作主張加了不在 Schema 裡的 attribute

Weaver 的 `live-check` 在 CI 整合測試階段可以充當 OTLP endpoint，把所有實際打出的 telemetry 與 registry 比對，產出 drift report：

```bash
# ci.sh 中的整合測試段落

# 1. 背景啟動 Weaver live-check
#    --inactivity-timeout：10 秒沒有新資料就自動停止並寫出報告
#    （不要用 kill——SIGTERM 不保證報告被完整寫出）
weaver registry live-check \
  --registry ./telemetry/registry \
  --policy ./policies \
  --input-source otlp \
  --format yaml \
  --output ./reports \
  --inactivity-timeout 10 \
  --otlp-grpc-address 0.0.0.0 \
  --otlp-grpc-port 4317 &

WEAVER_PID=$!
sleep 2

# 2. 執行整合測試（服務指向 Weaver 而非真實 Collector）
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
  go test ./integration/... -timeout 120s

# 3. 等 live-check 因 inactivity timeout 自動結束。
#    Weaver 在報告含有任何 violation 時會以非零 exit code 結束，
#    所以直接用 wait 的回傳值判斷就好，不用自己 grep。
if ! wait $WEAVER_PID; then
  echo "❌ Drift detected! 實際 telemetry 與 Schema 不符。"
  cat ./reports/live_check.yaml
  exit 1
fi

echo "✅ No drift detected."
```

報告長這樣（節錄）：

```yaml
# reports/live_check.yaml
- attribute:
    name: payment.orderId          # 服務實際打出的（Schema 裡只有 payment.order_id）
  live_check_result:
    all_advice:
      - advice_type: missing_attribute
        advice_level: violation
        value: payment.orderId
        message: Does not exist in the registry

statistics:
  advice_level_counts:
    violation: 47
  highest_advice_level: violation
```

`advice_level_counts` 裡的 `violation: 47` 讓你知道這不是偶發，是每一個 payment span 都在打錯誤的 key。

---

## Part 4：Schema 版本演進 — `deprecated` 的正確手法

假設你要把 `payment.order_id` 改名為 `payment.transaction_id`（更精確的命名）。如果直接改，前面的 breaking change policy 就會在 CI 擋下來，這是對的行為。正確的做法是**三步走**：

### Step 1：新舊並存，舊的標 `deprecated`

前面 `stability` 那節提過：`deprecated` 不是 stability 的值，是獨立欄位。而且它是結構化的，`reason` 有三種——`renamed`（改名，要附 `renamed_to`）、`obsoleted`（廢棄，無替代品）、`uncategorized`（其他）。改名情境正好用 `renamed`：

```yaml
# telemetry/registry/payment-spans.yaml
groups:
  - id: span.payment.process
    type: span
    attributes:
      - id: payment.order_id
        type: string
        stability: stable              # stability 維持不變
        deprecated:                    # ← 用結構化欄位標記棄用
          reason: renamed
          renamed_to: payment.transaction_id
          note: "將於 v3.0 移除"
        brief: "訂單識別碼（已棄用）"
        examples: ["ORD-20240601-001"]
        requirement_level: recommended # 從 required 降為 recommended

      - id: payment.transaction_id     # ← 新屬性同時上線
        type: string
        stability: stable
        brief: "交易識別碼"
        examples: ["TXN-20240601-001"]
        requirement_level: required
```

這個 PR 可以通過 breaking change policy，因為舊屬性還在（只是被標 deprecated）。`renamed_to` 不只是文件——程式碼生成的模板和下游工具都讀得到它，知道該往哪裡遷移。

你可以再加一條 policy，讓 Weaver 在 PR 提醒所有使用舊屬性的地方：

```rego
# policies/deprecation_warning.rego
package after_resolution

import rego.v1

warn contains warning if {
    group := input.groups[_]
    attr := group.attributes[_]
    attr.deprecated          # 有 deprecated 欄位就提醒
    warning := {
        "id": "deprecated_attr_in_use",
        "level": "warning",
        "message": sprintf(
            "屬性 '%s' 已標記為 deprecated（%s）：%s",
            [attr.name, attr.deprecated.reason, attr.deprecated.note]
        ),
    }
}
```

CI 會 pass（warning 不 fail），但 PR 的 check 輸出會清楚列出所有 deprecated 屬性，reviewer 有明確訊息。

### Step 2：更新所有服務，改用新屬性

各服務陸續把 `payment.order_id` 替換成 `payment.transaction_id`。因為生成的 SDK 裡兩個常數都有，不會有 compile error，可以逐步替換。

### Step 3：等待足夠的過渡期後，移除舊屬性

確認所有服務都已遷移（drift report 不再出現 `payment.order_id`），才發一個刪除屬性的 PR。此時 CI breaking change policy 會攔下來提醒——直接在 `breaking_change.rego` 裡加一個豁免清單：

```rego
# 例外：已完成遷移的 deprecated 屬性允許移除
allowed_removals := {"payment.order_id"}

deny contains violation if {
    # ... 原本的 breaking_attr_removed 規則
    not baseline_attr.name in allowed_removals  # ← 豁免已知的移除
    # ...
}
```

把豁免清單更新到 PR 裡一起送審，reviewer 就有完整的上下文：「這個屬性在上個 sprint 的 drift report 確認清零了，現在才刪。」

---

## 把四條防線串起來

```
PR 建立
  │
  ├─ [CI] weaver registry check (naming policy)
  │    └─ 命名不合規 → fail，打回修改
  │
  ├─ [CI] weaver registry check (breaking change policy)
  │    └─ 屬性被刪或 type 改了 → fail，要求用 deprecated
  │
  ├─ [CI] diff generated code
  │    └─ Schema 改了但 SDK 沒重新生成 → fail
  │
  └─ [整合測試] weaver live-check (drift detection)
       └─ 服務實際打出的 telemetry 不符 Schema → fail，顯示 drift report

全部通過 → Merge
```

這條流水線的關鍵洞察：**Schema 的合約保護跟 API 合約測試是同一件事**。你不會讓人隨便把 `user_id` 改成 `userId` 然後直接上 prod；telemetry attribute 也應該受到同樣的嚴肅對待，因為它是 Grafana dashboard、Loki 查詢、告警規則的資料來源，悄悄地改名會讓所有這些下游靜默失效。

Weaver 把這條防線從「希望大家有紀律」變成「機器強制執行」。這才是 Schema 的真正價值。

---

## 快速上手

```bash
# clone demo repo
git clone https://github.com/your-org/weaver-demo
cd weaver-demo/examples

# 執行 schema 格式 + 命名 policy 靜態驗證
make check

# 驗證含違規 Schema 的 registry（預期 fail，演示 policy 攔截）
make check-bad

# 看 drift detection 實際攔截
make live-check-broken-go
```

下一篇：**Schema 驅動的文件生成**——讓 Weaver 直接從 registry 輸出人類可讀的 telemetry spec，不再手寫 Confluence 頁面。
