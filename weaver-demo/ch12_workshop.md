# 第十二章：綜合實戰 Workshop

> 本章整合前十一章的所有技術，完整實作購物車微服務的端到端可觀測性架構，從 Schema 設計到 CI 防護，包含刻意錯誤演練與 AI 修復工作流。

---

## 12.1 任務描述

為「購物車 (Shopping Cart)」微服務建立完整的可觀測性架構：

1. 定義 Schema（Span + Metric）
2. 撰寫自訂 Policy
3. 生成 Go 和 Python 的型別安全程式碼
4. 實作遙測埋點
5. 撰寫整合測試並驗證 live-check
6. 刻意植入錯誤並驗證 CI 攔截
7. 使用 AI MCP 修復

完成後的專案結構：

```
cart-service/
├── telemetry/
│   └── registry/
│       ├── common.yaml          # 共用屬性
│       ├── cart-spans.yaml      # 購物車 Span 定義
│       └── cart-metrics.yaml    # 購物車 Metric 定義
├── templates/
│   ├── go/
│   │   ├── weaver.yaml
│   │   ├── semconv_attrs.go.j2
│   │   └── semconv_metrics.go.j2
│   └── python/
│       ├── weaver.yaml
│       ├── semconv_attrs.py.j2
│       └── semconv_metrics.py.j2
├── policies/
│   ├── enforce_naming.rego
│   └── no_breaking_changes.rego
├── generated_from_template/
│   ├── semconv_attrs.go
│   ├── semconv_metrics.go
│   ├── semconv_attrs.py
│   └── semconv_metrics.py
├── cart/                        # Go 實作
│   ├── service.go
│   └── service_test.go
├── cart_python/                 # Python 實作
│   ├── service.py
│   └── test_service.py
└── docker-compose.yml
```

---

## 12.2 Step 1：定義 Schema

### common.yaml（共用屬性）

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
```

### cart-spans.yaml

```yaml
# telemetry/registry/cart-spans.yaml
groups:
  - id: span.cart.add_item
    type: span
    span_kind: server
    stability: stable
    brief: "將商品加入購物車的 Span"
    attributes:
      - ref: git.tag
      - ref: deployment.environment
      - id: cart.session_id
        type: string
        stability: stable
        brief: "購物車 Session 識別碼"
        examples: ["sess-abc123", "sess-xyz789"]
        requirement_level: required
      - id: cart.item_id
        type: string
        stability: stable
        brief: "加入購物車的商品 SKU"
        examples: ["SKU-001", "SKU-002"]
        requirement_level: required
      - id: cart.item_quantity
        type: int
        stability: stable
        brief: "加入的商品數量"
        examples: [1, 2, 5]
        requirement_level: required
      - id: cart.item_price
        type: double
        stability: stable
        brief: "商品單價（新台幣）"
        examples: [299.0, 1500.0]
        requirement_level: required
      - id: cart.operation_result
        type: string
        stability: stable
        brief: "加入購物車的操作結果"
        examples: ["success", "failed_out_of_stock", "failed_invalid_quantity"]
        requirement_level: required

  - id: span.cart.remove_item
    type: span
    span_kind: server
    stability: stable
    brief: "從購物車移除商品的 Span"
    attributes:
      - ref: git.tag
      - ref: deployment.environment
      - id: cart.session_id
        type: string
        stability: stable
        brief: "購物車 Session 識別碼"
        examples: ["sess-abc123"]
        requirement_level: required
      - id: cart.item_id
        type: string
        stability: stable
        brief: "移除的商品 SKU"
        examples: ["SKU-001"]
        requirement_level: required
```

### cart-metrics.yaml

```yaml
# telemetry/registry/cart-metrics.yaml
groups:
  - id: metric.cart.add_item.count
    type: metric
    metric_name: cart.add_item.count
    instrument: counter
    unit: "{items}"
    stability: stable
    brief: "加入購物車的次數（依商品分類統計）"
    attributes:
      - ref: cart.item_id
      - ref: deployment.environment

  - id: metric.cart.value
    type: metric
    metric_name: cart.value
    instrument: histogram
    unit: "{TWD}"
    stability: stable
    brief: "每次加入購物車的金額分佈"
    attributes:
      - ref: deployment.environment

  - id: metric.cart.session_count
    type: metric
    metric_name: cart.session.count
    instrument: updowncounter
    unit: "{sessions}"
    stability: stable
    brief: "目前活躍的購物車 Session 數"
    attributes:
      - ref: deployment.environment
```

---

## 12.3 Step 2：撰寫 Policy

```rego
# policies/enforce_naming.rego
package otel_weaver

import future.keywords.if
import future.keywords.in

# 所有購物車 group 的 id 必須以 span.cart 或 metric.cart 開頭
deny[msg] if {
  group := input.groups[_]
  group.type in ["span", "metric"]
  not startswith(group.id, "span.cart")
  not startswith(group.id, "metric.cart")
  not startswith(group.id, "common.")
  msg := sprintf(
    "群組 '%s' 的 id 不符合命名規範（應以 span.cart. 或 metric.cart. 開頭）",
    [group.id]
  )
}
```

---

## 12.4 Step 3：生成程式碼

```bash
# 驗證 Schema
weaver registry check \
  --registry ./telemetry/registry \
  --policy ./policies

# 生成 Go 程式碼
weaver registry generate \
  --registry ./telemetry/registry \
  --templates ./templates \
  go ./generated_from_template

# 生成 Python 程式碼
weaver registry generate \
  --registry ./telemetry/registry \
  --templates ./templates \
  python ./generated_from_template

# 查看生成結果
find ./generated_from_template -type f
```

---

## 12.5 Step 4：Go 實作（使用生成程式碼）

```go
// cart/service.go
package cart

import (
    "context"
    "fmt"

    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/metric"
    "go.opentelemetry.io/otel/trace"
    semconv "cart-service/generated_from_template"
)

var (
    tracer = otel.Tracer("cart-service")
    meter  = otel.Meter("cart-service")
)

type CartService struct {
    addItemCounter  metric.Int64Counter
    cartValueHisto  metric.Float64Histogram
    sessionGauge    metric.Int64UpDownCounter
}

func NewCartService() (*CartService, error) {
    counter, err := meter.Int64Counter(
        semconv.CART_ADD_ITEM_COUNT_NAME,
        metric.WithDescription(semconv.CART_ADD_ITEM_COUNT_DESC),
        metric.WithUnit(semconv.CART_ADD_ITEM_COUNT_UNIT),
    )
    if err != nil {
        return nil, fmt.Errorf("建立 add_item counter 失敗: %w", err)
    }

    histo, err := meter.Float64Histogram(
        semconv.CART_VALUE_NAME,
        metric.WithDescription(semconv.CART_VALUE_DESC),
        metric.WithUnit(semconv.CART_VALUE_UNIT),
    )
    if err != nil {
        return nil, fmt.Errorf("建立 cart value histogram 失敗: %w", err)
    }

    sessionCounter, err := meter.Int64UpDownCounter(
        semconv.CART_SESSION_COUNT_NAME,
        metric.WithDescription(semconv.CART_SESSION_COUNT_DESC),
        metric.WithUnit(semconv.CART_SESSION_COUNT_UNIT),
    )
    if err != nil {
        return nil, fmt.Errorf("建立 session gauge 失敗: %w", err)
    }

    return &CartService{
        addItemCounter: counter,
        cartValueHisto: histo,
        sessionGauge:   sessionCounter,
    }, nil
}

type AddItemRequest struct {
    SessionID string
    ItemID    string
    Quantity  int
    Price     float64
    GitTag    string
    Env       string
}

func (s *CartService) AddItem(ctx context.Context, req AddItemRequest) error {
    ctx, span := tracer.Start(ctx, "cart.add_item",
        trace.WithSpanKind(trace.SpanKindServer),
    )
    defer span.End()

    // ✓ 使用生成的常數，不會拼字錯誤
    span.SetAttributes(
        semconv.CART_SESSION_ID.String(req.SessionID),
        semconv.CART_ITEM_ID.String(req.ItemID),
        semconv.CART_ITEM_QUANTITY.Int(req.Quantity),
        semconv.CART_ITEM_PRICE.Float64(req.Price),
        semconv.GIT_TAG.String(req.GitTag),
        semconv.DEPLOYMENT_ENVIRONMENT.String(req.Env),
    )

    if req.Quantity <= 0 {
        span.SetAttributes(
            semconv.CART_OPERATION_RESULT.String("failed_invalid_quantity"),
        )
        return fmt.Errorf("數量無效: %d", req.Quantity)
    }

    span.SetAttributes(
        semconv.CART_OPERATION_RESULT.String("success"),
    )

    // 記錄 Metrics
    s.addItemCounter.Add(ctx, int64(req.Quantity),
        metric.WithAttributes(
            semconv.CART_ITEM_ID.String(req.ItemID),
            semconv.DEPLOYMENT_ENVIRONMENT.String(req.Env),
        ),
    )

    totalValue := req.Price * float64(req.Quantity)
    s.cartValueHisto.Record(ctx, totalValue,
        metric.WithAttributes(
            semconv.DEPLOYMENT_ENVIRONMENT.String(req.Env),
        ),
    )

    return nil
}
```

---

## 12.6 Step 5：Python 實作（使用生成程式碼）

```python
# cart_python/service.py
from __future__ import annotations

from opentelemetry import trace, metrics
from generated_from_template.semconv_attrs import (
    CART_SESSION_ID,
    CART_ITEM_ID,
    CART_ITEM_QUANTITY,
    CART_ITEM_PRICE,
    CART_OPERATION_RESULT,
    GIT_TAG,
    DEPLOYMENT_ENVIRONMENT,
)
from generated_from_template.semconv_metrics import (
    CART_ADD_ITEM_COUNT_NAME,
    CART_ADD_ITEM_COUNT_UNIT,
    CART_ADD_ITEM_COUNT_DESC,
    CART_VALUE_NAME,
    CART_VALUE_UNIT,
    CART_VALUE_DESC,
    CART_SESSION_COUNT_NAME,
    CART_SESSION_COUNT_UNIT,
    CART_SESSION_COUNT_DESC,
)

tracer = trace.get_tracer("cart-service")
meter = metrics.get_meter("cart-service")

# 使用生成的常數建立 metric instruments
_add_item_counter = meter.create_counter(
    name=CART_ADD_ITEM_COUNT_NAME,
    unit=CART_ADD_ITEM_COUNT_UNIT,
    description=CART_ADD_ITEM_COUNT_DESC,
)

_cart_value_histogram = meter.create_histogram(
    name=CART_VALUE_NAME,
    unit=CART_VALUE_UNIT,
    description=CART_VALUE_DESC,
)

_session_gauge = meter.create_up_down_counter(
    name=CART_SESSION_COUNT_NAME,
    unit=CART_SESSION_COUNT_UNIT,
    description=CART_SESSION_COUNT_DESC,
)


def add_item(
    session_id: str,
    item_id: str,
    quantity: int,
    price: float,
    git_tag: str = "unknown",
    env: str = "production",
) -> bool:
    with tracer.start_as_current_span("cart.add_item") as span:
        # ✓ 使用生成的常數，不手打字串
        span.set_attributes({
            CART_SESSION_ID: session_id,
            CART_ITEM_ID: item_id,
            CART_ITEM_QUANTITY: quantity,
            CART_ITEM_PRICE: price,
            GIT_TAG: git_tag,
            DEPLOYMENT_ENVIRONMENT: env,
        })

        if quantity <= 0:
            span.set_attribute(CART_OPERATION_RESULT, "failed_invalid_quantity")
            return False

        span.set_attribute(CART_OPERATION_RESULT, "success")

        # 記錄指標
        _add_item_counter.add(
            quantity,
            attributes={
                CART_ITEM_ID: item_id,
                DEPLOYMENT_ENVIRONMENT: env,
            },
        )

        total_value = price * quantity
        _cart_value_histogram.record(
            total_value,
            attributes={DEPLOYMENT_ENVIRONMENT: env},
        )

        return True
```

---

## 12.7 Step 6：刻意植入錯誤，測試 CI 攔截

```go
// cart/service_sabotaged.go — 刻意錯誤版本
package cart

import (
    "context"
    "go.opentelemetry.io/otel/attribute"
)

func (s *CartService) AddItemBroken(ctx context.Context, req AddItemRequest) error {
    ctx, span := tracer.Start(ctx, "cart.add_item")
    defer span.End()

    // ❌ 不使用生成的常數，手動輸入錯誤的屬性名稱
    span.SetAttributes(
        attribute.String("session", req.SessionID),    // ❌ 應為 cart.session_id
        attribute.String("item", req.ItemID),           // ❌ 應為 cart.item_id
        attribute.Int("qty", req.Quantity),             // ❌ 應為 cart.item_quantity
        // ❌ 完全漏掉 cart.item_price、git.tag、deployment.environment、cart.operation_result
    )

    return nil
}
```

執行測試並讓 Weaver 攔截：

```bash
# 啟動 Weaver live-check
weaver registry live-check \
  --registry ./telemetry/registry \
  --format yaml \
  --output ./weaver-sabotage-report.yaml \
  --otlp-grpc-port 4318 &

sleep 3

# 執行測試（測試中呼叫 AddItemBroken）
go test ./cart/... -run TestAddItem_Sabotaged -tags=integration -v

kill %1
wait %1 || true
```

CI 輸出：
```
✗ Weaver live-check: 發現 7 項違規
  - 屬性 'session' 未在 Schema 中定義（建議：cart.session_id）
  - 屬性 'item' 未在 Schema 中定義（建議：cart.item_id）
  - 屬性 'qty' 未在 Schema 中定義（建議：cart.item_quantity）
  - required 屬性 'cart.item_price' 未出現
  - required 屬性 'git.tag' 未出現
  - required 屬性 'deployment.environment' 未出現
  - required 屬性 'cart.operation_result' 未出現
Exit code: 1 — CI 流程中斷 ✓
```

---

## 12.8 Step 7：使用 AI MCP 修復

### 啟動 MCP 伺服器

```bash
weaver registry mcp --registry ./telemetry/registry
```

### 設定 Claude Code MCP

```json
// .claude/settings.json
{
  "mcpServers": {
    "weaver": {
      "command": "weaver",
      "args": ["registry", "mcp", "--registry", "./telemetry/registry"]
    }
  }
}
```

### Prompt

```
我的 CI 被 Weaver 攔截。報告顯示 cart/service_sabotaged.go 中的
AddItemBroken 函式有以下違規：
1. 屬性 'session' 應為 'cart.session_id'
2. 屬性 'item' 應為 'cart.item_id'
3. 屬性 'qty' 應為 'cart.item_quantity'
4. 缺少 required 屬性：cart.item_price、git.tag、deployment.environment、cart.operation_result

請根據 Weaver Schema 修復 AddItemBroken 函式，使用
generated_from_template/semconv_attrs.go 中的常數。
```

AI 根據 MCP 後會建議修復後的程式碼，確保使用正確的常數。

---

## 12.9 完整的 Docker Compose 本地測試環境

```yaml
# docker-compose.yml
version: '3.8'

services:
  # Weaver live-check（接收 OTLP 並驗證合規性）
  weaver:
    image: otel/weaver:latest
    command: >
      registry live-check
      --registry /workspace/telemetry/registry
      --policy /workspace/policies
      --input-source otlp
      --format yaml
      --output /reports/weaver-report.yaml
      --otlp-grpc-address 0.0.0.0
      --otlp-grpc-port 4318
    volumes:
      - ./telemetry:/workspace/telemetry:ro
      - ./policies:/workspace/policies:ro
      - ./reports:/reports
    ports:
      - "4318:4318"

  # Go 微服務
  cart-service-go:
    build:
      context: .
      dockerfile: Dockerfile.go
    environment:
      OTEL_EXPORTER_OTLP_ENDPOINT: http://weaver:4318
      OTEL_SERVICE_NAME: cart-service
      DEPLOYMENT_ENVIRONMENT: development
    depends_on:
      - weaver

  # Python 微服務
  cart-service-python:
    build:
      context: .
      dockerfile: Dockerfile.python
    environment:
      OTEL_EXPORTER_OTLP_ENDPOINT: http://weaver:4318
      OTEL_SERVICE_NAME: cart-service-python
      DEPLOYMENT_ENVIRONMENT: development
    depends_on:
      - weaver

  # Grafana 視覺化（用於驗證 emit 指令的模擬訊號）
  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    environment:
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Admin

  # Prometheus（用於接收 metric 訊號）
  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
```

### 啟動並執行完整測試

```bash
# 啟動環境
docker compose up -d

# 等待服務就緒
sleep 5

# 執行整合測試
docker compose exec cart-service-go go test ./... -tags=integration
docker compose exec cart-service-python pytest tests/ -m integration

# 查看 Weaver 報告
cat ./reports/weaver-report.yaml

# 用 emit 模擬訊號，讓 Grafana 有資料
weaver registry emit \
  --registry ./telemetry/registry \
  --count 50 \
  --otlp-endpoint http://localhost:4318

# 開啟 Grafana 查看效果
open http://localhost:3000

# 清理環境
docker compose down
```

---

## 12.10 Workshop 完成標準

完成本章 Workshop 後，你應能確認以下所有檢查點：

```bash
# Checklist 驗證腳本
echo "=== Workshop 完成確認 ==="

# 1. Schema 驗證通過
weaver registry check \
  --registry ./telemetry/registry \
  --policy ./policies && \
  echo "✓ Layer 1: Schema & Policy 驗證通過" || \
  echo "✗ Layer 1: Schema & Policy 驗證失敗"

# 2. 生成程式碼存在
[ -f "./generated_from_template/semconv_attrs.go" ] && \
[ -f "./generated_from_template/semconv_metrics.go" ] && \
[ -f "./generated_from_template/semconv_attrs.py" ] && \
[ -f "./generated_from_template/semconv_metrics.py" ] && \
  echo "✓ Layer 2: 生成程式碼存在" || \
  echo "✗ Layer 2: 生成程式碼缺失"

# 3. 生成程式碼與 Schema 同步
weaver registry generate \
  --registry ./telemetry/registry \
  --templates ./templates \
  go /tmp/check_gen > /dev/null 2>&1 && \
diff -r ./generated_from_template/ /tmp/check_gen/ > /dev/null 2>&1 && \
  echo "✓ Layer 2: 生成程式碼與 Schema 同步" || \
  echo "✗ Layer 2: 生成程式碼需要重新 generate"

# 4. 正確版本的業務程式碼編譯通過
go build ./cart/... && \
  echo "✓ Layer 3: Go 程式碼編譯通過" || \
  echo "✗ Layer 3: Go 程式碼編譯失敗"

echo "=== 確認完成 ==="
```

---

## 附錄 A：常見錯誤排除

| 錯誤訊息 | 原因 | 解決方式 |
|---------|------|---------|
| `missing required field 'unit'` | Metric 缺少 unit | 在 YAML 中加入 `unit` 欄位 |
| `attribute 'xxx' not in schema` | 程式碼使用了未定義的屬性 | 使用生成的常數或修改 Schema |
| `backwards compatibility error` | 試圖刪除必填屬性 | 改用 `deprecated` 標記 |
| `policy violation` | 違反 Rego 自訂規則 | 檢查 policies/ 目錄中的規則 |
| `generated code is out of date` | Schema 更新後沒有重新生成 | 執行 `weaver registry generate` |
| `undefined value` | Jinja2 模板中存取了不存在的欄位 | 用 `{{ ctx \| tojson }}` dump 確認 |

## 附錄 B：Weaver 與 OTel SDK 版本相容性

```
Weaver >= 0.13 →  OTel Spec 1.28+
Weaver >= 0.9  →  OTel Spec 1.26+
Weaver >= 0.7  →  OTel Spec 1.24+
```

## 附錄 C：學習資源

- [Weaver GitHub Repository](https://github.com/open-telemetry/weaver)
- [OTel Semantic Conventions](https://github.com/open-telemetry/semantic-conventions)
- [Weaver 模板範例庫](https://github.com/open-telemetry/weaver-templates)
- CNCF Slack: `#otel-weaver` 頻道
- [Open Policy Agent (OPA) Rego 語言](https://www.openpolicyagent.org/docs/latest/policy-language/)
- [OpenTelemetry Go SDK](https://pkg.go.dev/go.opentelemetry.io/otel)
- [OpenTelemetry Python SDK](https://opentelemetry-python.readthedocs.io/)
