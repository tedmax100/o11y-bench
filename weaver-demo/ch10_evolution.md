# 第十章：Schema 演進與版本管理

> 本章介紹安全地演進 Schema 的完整流程，包含廢棄欄位的正確手法、版本差異比較、打包發布，以及 Go 與 Python 的過渡期相容程式碼範例。

---

## 10.1 Schema 版本管理的核心挑戰

Schema 是遙測資料的契約。每次改動都可能影響：
- 生產環境的警報規則（依賴特定屬性名稱）
- Grafana dashboard（依賴 metric_name）
- 下游消費者的查詢（Loki LogQL、Tempo TraceQL、PromQL）

**主要風險類型：**

| 變更類型 | 風險 | 處理方式 |
|---------|------|---------|
| 新增 optional 屬性 | 低 | 直接新增，不需要過渡期 |
| 新增 required 屬性 | 中 | 先新增為 `recommended`，過渡後改為 `required` |
| 重新命名屬性 | 高 | 同時保留舊名（`deprecated`）和新名，過渡期後再移除 |
| 刪除 required 屬性 | 極高 | 先標記 `deprecated`，等所有消費者遷移後才移除 |
| 修改 metric_name | 極高 | 視同「新增新 metric + 廢棄舊 metric」 |

---

## 10.2 安全的欄位廢棄流程

### 階段 1：標記廢棄（Breaking Change Warning）

```yaml
# ✓ 正確：標記廢棄，保留相容性
groups:
  - id: span.payment.process
    attributes:
      - id: payment.order_id
        type: string
        stability: stable
        deprecated: "請改用 payment.transaction_id，此欄位將於 v3.0 移除"
        brief: "唯一的訂單識別碼（已廢棄）"
        examples: ["ord-20240601-001"]
        requirement_level: recommended  # 從 required 降為 recommended

      - id: payment.transaction_id   # 同時加入新欄位
        type: string
        stability: stable
        brief: "唯一的交易識別碼（取代 payment.order_id）"
        examples: ["txn-20240601-001"]
        requirement_level: required
```

### 階段 2：過渡期程式碼（同時發送新舊欄位）

```go
// payment/service.go — Go 過渡期寫法
func (s *PaymentService) ProcessPayment(ctx context.Context, req PaymentRequest) error {
    ctx, span := tracer.Start(ctx, "payment.process")
    defer span.End()

    // 新欄位（優先使用）
    span.SetAttributes(
        semconv.PAYMENT_TRANSACTION_ID.String(req.TransactionID),
    )
    
    // 舊欄位（相容用途，過渡期同時發送）
    // TODO: 待所有 Dashboard/Alert 遷移到 payment.transaction_id 後移除
    // 預定移除日期：2025-Q3
    if req.OrderID != "" {
        span.SetAttributes(
            semconv.PAYMENT_ORDER_ID.String(req.OrderID), // deprecated
        )
    }

    return nil
}
```

```python
# payment/service.py — Python 過渡期寫法
import warnings
from generated_from_template.semconv_attrs import (
    PAYMENT_TRANSACTION_ID,
    PAYMENT_ORDER_ID,  # deprecated，過渡期使用
)

def process_payment(
    order_id: str,
    transaction_id: str,
    *,
    emit_deprecated: bool = True,  # feature flag，過渡期後設為 False
) -> bool:
    with tracer.start_as_current_span("payment.process") as span:
        # 新欄位
        span.set_attribute(PAYMENT_TRANSACTION_ID, transaction_id)
        
        # 舊欄位（過渡期相容）
        if emit_deprecated and order_id:
            warnings.warn(
                "payment.order_id 已廢棄，請改用 payment.transaction_id",
                DeprecationWarning,
                stacklevel=2,
            )
            span.set_attribute(PAYMENT_ORDER_ID, order_id)
    
    return True
```

### 階段 3：移除廢棄欄位（Breaking Change）

當所有消費者（Dashboard、Alert、下游服務）都遷移完成後：

```yaml
# 從 YAML 中移除 deprecated 屬性
groups:
  - id: span.payment.process
    attributes:
      # payment.order_id 已移除（v3.0）
      - id: payment.transaction_id
        type: string
        stability: stable
        brief: "唯一的交易識別碼"
        requirement_level: required
```

此時重新執行 `weaver registry generate`，生成的程式碼中 `PAYMENT_ORDER_ID` 常數會消失，所有還在使用舊常數的程式碼會**編譯失敗**，強迫開發人員完成遷移。

---

## 10.3 比較 Schema 版本差異

```bash
# 比較當前版本與上一個 release tag 的差異
weaver registry diff \
  --registry-old https://github.com/myorg/schemas/tree/v1.0.0 \
  --registry-new ./telemetry/registry

# 比較兩個本地版本
weaver registry diff \
  --registry-old ./backup/registry-v1 \
  --registry-new ./telemetry/registry

# 輸出範例：
# + 新增: metric.payment.refund (stable, counter)
# + 新增: span.payment.process.payment.transaction_id (required, stable)
# ~ 修改: span.payment.process.payment.order_id
#    deprecated: "請改用 payment.transaction_id，此欄位將於 v3.0 移除"
#    requirement_level: required → recommended
# - 移除: 無（正確！不允許直接刪除必填欄位）
```

### 在 CI 中用 diff 產生 PR 摘要

```yaml
# .github/workflows/schema-diff.yml
- name: Schema Diff Summary
  run: |
    # 比較 PR branch 與 main branch 的 Schema 差異
    git stash
    git checkout main
    weaver registry resolve \
      --registry ./telemetry/registry \
      --format json > /tmp/schema-old.json
    git stash pop
    
    weaver registry diff \
      --registry-old /tmp/schema-old.json \
      --registry-new ./telemetry/registry \
      > /tmp/schema-diff.txt
    
    echo "## Schema 變更摘要" >> $GITHUB_STEP_SUMMARY
    cat /tmp/schema-diff.txt >> $GITHUB_STEP_SUMMARY
```

---

## 10.4 解析並打包 Schema

### 打包為單一工件

```bash
# 將 Schema 與所有依賴打包為單一 YAML 工件（用於發布）
weaver registry resolve \
  --registry ./telemetry/registry \
  --format yaml \
  --output ./dist/schema-v2.0.0.yaml

# 其他團隊可以直接使用這個打包後的 Schema
weaver registry check \
  --registry ./dist/schema-v2.0.0.yaml

# 生成程式碼（使用打包後的 Schema）
weaver registry generate \
  --registry ./dist/schema-v2.0.0.yaml \
  --templates ./templates \
  go ./generated
```

### 版本標記策略

```bash
# 與 Git tag 對齊
VERSION=$(git describe --tags --abbrev=0)
weaver registry resolve \
  --registry ./telemetry/registry \
  --format yaml \
  --output "./dist/schema-${VERSION}.yaml"

# 發布到企業 artifact registry
aws s3 cp \
  "./dist/schema-${VERSION}.yaml" \
  "s3://company-schemas/otel/${VERSION}/schema.yaml"

# 更新 latest 指向
aws s3 cp \
  "s3://company-schemas/otel/${VERSION}/schema.yaml" \
  "s3://company-schemas/otel/latest/schema.yaml"
```

---

## 10.5 Schema 版本演進的 Git 策略

### 版本號語義

建議遵循 Schema 版本語義：

```
vMAJOR.MINOR.PATCH

MAJOR：有 Breaking Change（刪除了 stable 屬性、重命名了 metric_name）
MINOR：新增了 stable group 或 attribute（向後相容）
PATCH：修改 brief、note 等非行為性欄位（向後相容）
```

### CHANGELOG 自動化

```bash
#!/bin/bash
# scripts/generate-schema-changelog.sh

PREV_VERSION=$1  # e.g., v1.0.0
CURR_VERSION=$2  # e.g., v2.0.0

echo "# Schema Changelog: ${PREV_VERSION} → ${CURR_VERSION}"
echo ""
echo "Generated on $(date)"
echo ""

weaver registry diff \
  --registry-old "https://github.com/myorg/schemas/tree/${PREV_VERSION}" \
  --registry-new ./telemetry/registry
```

---

## 10.6 多版本共存場景（遷移期）

有些場景需要同時維護多個 Schema 版本：

```
telemetry/
└── registry/
    ├── v1/                    # v1.x 版本（維護中）
    │   ├── common.yaml
    │   └── payment-spans.yaml
    └── v2/                    # v2.x 版本（新版）
        ├── common.yaml
        └── payment-spans.yaml
```

```bash
# 分別驗證兩個版本
weaver registry check --registry ./telemetry/registry/v1
weaver registry check --registry ./telemetry/registry/v2

# 比較兩個版本的差異
weaver registry diff \
  --registry-old ./telemetry/registry/v1 \
  --registry-new ./telemetry/registry/v2
```

---

## 10.7 Policy 保護版本穩定性

在 Policy 中強制遵守廢棄流程：

```rego
# policies/enforce_deprecation_process.rego
package otel_weaver

# 若 stability == stable 的 group，其 required 屬性不得直接消失
# （只能先標記 deprecated，不能直接刪除）
# 這個 Policy 在 diff 工作流中執行，比較新舊兩個 registry

# 受保護的屬性：在 v1.0.0 中是 required 的屬性
protected_v1_required := {
  "span.payment.process": {
    "payment.order_id",
    "payment.status",
    "git.tag",
    "deployment.environment",
  },
  "metric.payment.amount": {
    "payment.provider",
    "deployment.environment",
  },
}

deny[msg] {
  group := input.groups[_]
  required_attrs := protected_v1_required[group.id]
  attr_name := required_attrs[_]
  
  # 屬性存在但沒有 deprecated 標記 → 直接刪除！
  not attribute_exists(group.attributes, attr_name)
  msg := sprintf(
    "穩定版屬性 '%s' 不得直接從群組 '%s' 中移除，請先標記為 deprecated",
    [attr_name, group.id]
  )
}

attribute_exists(attributes, attr_name) {
  attr := attributes[_]
  attr.name == attr_name
}
```

---

## 10.8 完整的版本演進操作範例

假設要將 `payment.order_id` 重新命名為 `payment.transaction_id`：

```bash
# Step 1：更新 Schema（標記 deprecated + 新增新欄位）
# 編輯 telemetry/registry/payment-spans.yaml

# Step 2：驗證變更
weaver registry check \
  --registry ./telemetry/registry \
  --policy ./policies

# Step 3：重新生成程式碼
weaver registry generate \
  --registry ./telemetry/registry \
  --templates ./templates \
  go ./generated_from_template

# Step 4：查看 diff（生成的程式碼有什麼變化）
git diff ./generated_from_template/

# 預期：
# +const PAYMENT_TRANSACTION_ID = attribute.Key("payment.transaction_id")
# 保留 PAYMENT_ORDER_ID（deprecated 但還在）

# Step 5：更新業務程式碼（使用新常數，同時保留舊常數的過渡期發送）
# 手動編輯 payment/service.go

# Step 6：執行完整 CI 驗證
make ci-full

# Step 7：建立 PR 並附上版本升級說明
git commit -m "feat(schema): add payment.transaction_id, deprecate payment.order_id

BREAKING CHANGE (planned v3.0): payment.order_id will be removed in v3.0
Migration: use payment.transaction_id instead
Transition period: both attributes are emitted during v2.x
"
```

---

## 延伸閱讀

- [OTel Semantic Conventions 版本管理策略](https://github.com/open-telemetry/semantic-conventions/blob/main/docs/general/versioning-and-deprecation.md)
- [Semantic Versioning 規範](https://semver.org/)
- [OTel Schema URL 規範](https://opentelemetry.io/docs/specs/otel/schemas/)
