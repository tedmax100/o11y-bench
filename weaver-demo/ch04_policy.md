# 第四章：靜態驗證與 Policy 防護

> 本章介紹如何用 OPA Rego 撰寫自訂命名規則、必填屬性防護、破壞性變更偵測，以及在 CI/CD 中強制執行 Policy 的完整配置。

---

## 4.1 為什麼需要自訂 Policy？

Weaver 的內建驗證只能確保 YAML 語法正確、必填欄位存在。但企業實際上需要更多規則：

| 場景 | 標準驗證能做到嗎？ | 需要 Policy |
|------|-------------------|-------------|
| 強制指標前綴為 `acme.` | 否 | 是 |
| `brief` 欄位一律必填且不得為空 | 否 | 是 |
| 所有 Resource 必須包含 `git.tag` | 否 | 是 |
| 禁止直接刪除 `required` 屬性 | 否 | 是 |
| YAML 基本語法正確 | 是 | — |
| 必填欄位（unit、stability）存在 | 是 | — |

### Policy 的執行時機

Weaver 的 Policy 在 `after_resolution` 階段執行——也就是所有 `ref` 展開、Schema 完全解析之後。這意味著 Policy 看到的是**完整的、展開後的**資料結構，而非原始 YAML。

---

## 4.2 Rego 語言基礎

Weaver 使用 [Open Policy Agent (OPA)](https://www.openpolicyagent.org/) 的 Rego 語言撰寫 Policy。

### Rego 最小語法速查

```rego
package otel_weaver    # 必須是這個 package 名稱

# input 是傳入的 registry JSON（after_resolution 後的完整結構）
# input.groups 是所有 group 的陣列

# deny 規則：若條件成立，加入一條違規訊息
deny[msg] {
  # 迭代 input.groups 中的每個元素
  group := input.groups[_]       # [_] 表示「任意索引」
  group.type == "metric"
  # 你的條件...
  msg := sprintf("違規訊息 %s", [group.id])
}

# 輔助函式：判斷屬性陣列中是否存在特定屬性
attribute_exists(attributes, attr_id) {
  attr := attributes[_]
  attr.id == attr_id
}

# 或使用 ref 形式（ref 屬性展開後名稱在 attr.name，不在 attr.id）
attribute_name_exists(attributes, attr_name) {
  attr := attributes[_]
  attr.name == attr_name
}
```

### input 的資料結構

```json
{
  "groups": [
    {
      "id": "span.payment.process",
      "type": "span",
      "span_kind": "server",
      "stability": "stable",
      "brief": "處理訂單支付流程的 Span",
      "attributes": [
        {
          "name": "git.tag",          ← ref 展開後，名稱在 .name（不是 .id）
          "type": "string",
          "requirement_level": "required",
          "stability": "stable",
          "brief": "Git 版本標籤"
        },
        {
          "name": "payment.order_id",
          "type": "string",
          "requirement_level": "required"
        }
      ]
    },
    {
      "id": "metric.payment.amount",
      "type": "metric",
      "metric_name": "payment.amount",
      "instrument": "histogram",
      "unit": "{TWD}",
      "stability": "stable"
    }
  ]
}
```

**重要**：`ref` 展開後，屬性的 key 是 `name`（而非 `id`）。直接定義在 group 中的屬性也是 `name`。只有在原始 YAML 中，`attribute_group` 的屬性才用 `id` 定義。

---

## 4.3 撰寫 Rego 策略

### 策略 1：強制包含 `git.tag`

```rego
# policies/enforce_git_tag.rego
package otel_weaver

# 所有 span 和 metric 必須包含 git.tag 屬性
deny[msg] {
  group := input.groups[_]
  group.type in ["span", "metric"]
  not attribute_name_exists(group.attributes, "git.tag")
  msg := sprintf(
    "群組 '%s'（type=%s）缺少必要的 'git.tag' 屬性",
    [group.id, group.type]
  )
}

attribute_name_exists(attributes, attr_name) {
  attr := attributes[_]
  attr.name == attr_name
}
```

### 策略 2：強制指標命名前綴

```rego
# policies/enforce_metric_prefix.rego
package otel_weaver

import future.keywords.if
import future.keywords.in

# 公司規定：所有自訂指標必須以允許的前綴開頭
allowed_prefixes := ["payment.", "cart.", "auth.", "inventory.", "notification."]

deny[msg] if {
  group := input.groups[_]
  group.type == "metric"
  metric_name := group.metric_name
  not starts_with_any(metric_name, allowed_prefixes)
  msg := sprintf(
    "指標 '%s' 不符合命名規範，必須以 %v 其中之一開頭",
    [metric_name, allowed_prefixes]
  )
}

starts_with_any(str, prefixes) if {
  prefix := prefixes[_]
  startswith(str, prefix)
}
```

### 策略 3：所有屬性 `brief` 不得為空

```rego
# policies/enforce_brief_not_empty.rego
package otel_weaver

deny[msg] {
  group := input.groups[_]
  attr := group.attributes[_]
  trim_space(attr.brief) == ""
  msg := sprintf(
    "群組 '%s' 中的屬性 '%s' 的 brief 欄位不可為空",
    [group.id, attr.name]
  )
}

# 也驗證 group 本身的 brief
deny[msg] {
  group := input.groups[_]
  trim_space(group.brief) == ""
  msg := sprintf("群組 '%s' 的 brief 欄位不可為空", [group.id])
}
```

### 策略 4：禁止 development stability 的 span 出現在 stable group 中

```rego
# policies/enforce_stability_consistency.rego
package otel_weaver

# 若 group 是 stable，其所有直接定義的屬性也必須是 stable
deny[msg] {
  group := input.groups[_]
  group.stability == "stable"
  attr := group.attributes[_]
  attr.stability == "development"
  # 只檢查 group 直接定義的屬性（非 ref 展開的）
  attr.name == attr.id  # 近似判斷：直接定義的屬性 name == id
  msg := sprintf(
    "stable 群組 '%s' 中的屬性 '%s' 不得是 development stability",
    [group.id, attr.name]
  )
}
```

---

## 4.4 破壞性變更防護實作

**情境**：SRE 發現有人試圖刪除 `payment.order_id`（已被用在警報規則中）。

```rego
# policies/no_breaking_changes.rego
package otel_weaver

# 這些屬性被生產環境警報依賴，絕不允許從定義的 group 中移除
# key = group.id，value = 必須存在的屬性名稱集合
protected_attributes := {
  "span.payment.process": {"payment.order_id", "payment.provider"},
  "span.cart.add_item":   {"cart.session_id", "cart.item_id"},
  "metric.payment.amount": {"payment.provider"},
}

deny[msg] {
  group := input.groups[_]
  required_attrs := protected_attributes[group.id]
  attr_name := required_attrs[_]
  not attribute_name_exists(group.attributes, attr_name)
  msg := sprintf(
    "受保護屬性 '%s' 不得從群組 '%s' 中移除（被生產警報依賴）",
    [attr_name, group.id]
  )
}

attribute_name_exists(attributes, attr_name) {
  attr := attributes[_]
  attr.name == attr_name
}
```

### 更進一步：防護 required → optional 的降級

```rego
# policies/no_requirement_downgrade.rego
package otel_weaver

# 這些屬性的 requirement_level 不得降級（從 required 改為 optional/recommended）
protected_required_attributes := {
  "span.payment.process": {"payment.order_id", "payment.status"},
}

deny[msg] {
  group := input.groups[_]
  required_attrs := protected_required_attributes[group.id]
  attr_name := required_attrs[_]
  
  # 找到該屬性
  attr := group.attributes[_]
  attr.name == attr_name
  
  # 但它不是 required
  attr.requirement_level != "required"
  
  msg := sprintf(
    "屬性 '%s' 在群組 '%s' 中必須保持 required（不可降級）",
    [attr_name, group.id]
  )
}
```

---

## 4.5 執行含 Policy 的驗證

```bash
# 執行驗證（含 Policy）
weaver registry check \
  --registry ./telemetry/registry \
  --policy ./policies

# 若所有規則通過：
# ✔ No `after_resolution` policy violation

# 若有違規，輸出類似：
# ✗ Policy violations found:
#
#   [enforce_metric_prefix.rego]
#   指標 'internal.cache_hit_rate' 不符合命名規範，必須以
#   ["payment.", "cart.", "auth.", "inventory."] 其中之一開頭
#
#   [no_breaking_changes.rego]
#   受保護屬性 'payment.order_id' 不得從群組 'span.payment.process' 中移除
```

---

## 4.6 Policy 的 CI 整合

```yaml
# .github/workflows/weaver-policy.yml
name: Schema & Policy Check

on:
  pull_request:
    paths:
      - 'telemetry/**'
      - 'policies/**'

jobs:
  policy-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: open-telemetry/weaver-action/setup@v1
        with:
          version: 'latest'

      - name: Validate Schema with Policies
        run: |
          weaver registry check \
            --registry ./telemetry/registry \
            --policy ./policies
        # 若有違規，exit code != 0，CI 自動失敗
```

---

## 4.7 Policy 撰寫的最佳實踐

### 為每個 Policy 寫測試

OPA 支援內建測試框架：

```rego
# policies/enforce_metric_prefix_test.rego
package otel_weaver

import data.otel_weaver.deny

# 測試：符合命名規範的指標不應有違規
test_valid_metric_prefix {
  count(deny) == 0 with input as {
    "groups": [{
      "id": "metric.payment.amount",
      "type": "metric",
      "metric_name": "payment.amount",
      "instrument": "histogram",
      "unit": "{TWD}",
      "stability": "stable",
      "brief": "支付金額"
    }]
  }
}

# 測試：不符合命名規範的指標應有違規
test_invalid_metric_prefix {
  count(deny) == 1 with input as {
    "groups": [{
      "id": "metric.internal.cache",
      "type": "metric",
      "metric_name": "internal.cache_hit_rate",
      "instrument": "gauge",
      "unit": "1",
      "stability": "stable",
      "brief": "快取命中率"
    }]
  }
}
```

執行 OPA 測試：

```bash
# 安裝 OPA
curl -Lo opa https://openpolicyagent.org/downloads/latest/opa_linux_amd64_static
chmod +x opa
sudo mv opa /usr/local/bin/

# 執行所有 Policy 測試
opa test ./policies/ -v

# 預期輸出：
# data.otel_weaver.test_valid_metric_prefix: PASS (1ms)
# data.otel_weaver.test_invalid_metric_prefix: PASS (1ms)
# 2 tests, 0 failures
```

### Policy 組織建議

```
policies/
├── naming/
│   ├── enforce_metric_prefix.rego
│   ├── enforce_attribute_naming.rego
│   └── enforce_id_format.rego
├── completeness/
│   ├── enforce_brief_not_empty.rego
│   ├── enforce_examples_present.rego
│   └── enforce_git_tag.rego
├── compatibility/
│   ├── no_breaking_changes.rego
│   └── no_requirement_downgrade.rego
└── tests/
    ├── naming_test.rego
    ├── completeness_test.rego
    └── compatibility_test.rego
```

---

## 4.8 常見 Policy 錯誤對照

### 錯誤一：用 `.id` 存取 after_resolution 後的屬性名稱

```rego
# ❌ 錯誤：after_resolution 後屬性名稱在 .name，不是 .id
deny[msg] {
  group := input.groups[_]
  attr := group.attributes[_]
  attr.id == "payment.order_id"   # 這個 key 不存在！
  ...
}

# ✓ 正確：使用 .name
deny[msg] {
  group := input.groups[_]
  attr := group.attributes[_]
  attr.name == "payment.order_id"
  ...
}
```

### 錯誤二：package 名稱錯誤

```rego
# ❌ 錯誤：Weaver 只認識 otel_weaver package
package my_policy

deny[msg] { ... }

# ✓ 正確
package otel_weaver

deny[msg] { ... }
```

### 錯誤三：忘記加 `import future.keywords`

```rego
# ❌ 錯誤：新版 Rego 語法需要 import
deny[msg] if {    # "if" 關鍵字需要 import
  ...
}

# ✓ 正確（方式一：import 後使用 if）
import future.keywords.if

deny[msg] if {
  ...
}

# ✓ 正確（方式二：不用 if，用舊式語法）
deny[msg] {
  ...
}
```

---

## 延伸閱讀

- [OPA Rego 語言文件](https://www.openpolicyagent.org/docs/latest/policy-language/)
- [Weaver Policy 官方範例](https://github.com/open-telemetry/weaver/tree/main/policies)
- [OPA Playground（線上測試 Rego）](https://play.openpolicyagent.org/)
