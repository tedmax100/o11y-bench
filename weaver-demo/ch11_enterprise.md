# 第十一章：企業級整合策略

> 本章介紹在多團隊、多遺留系統的企業環境中，如何組織多個 Registry、發布 Schema 工件、建立治理流程，以及跨語言服務的一致性管理策略。

---

## 11.1 企業環境的核心挑戰

在規模較大的組織中，遙測 Schema 管理面臨的問題遠比單一服務複雜：

| 挑戰 | 說明 |
|------|------|
| 多團隊同時修改 Schema | 需要明確的所有權與審查流程 |
| 遺留系統無法快速遷移 | 需要漸進式整合，允許過渡期的不完整合規 |
| 不同語言的服務（Go、Python、Java、Node.js）| 需要多語言程式碼生成 |
| OTel 官方 semconv 與公司自訂 Schema 共存 | 需要多 Registry 合併策略 |
| Schema 作為跨團隊 API 的穩定性保證 | 需要嚴格的版本管理與廢棄流程 |

---

## 11.2 多 Registry 架構（Comcast 模式）

大型組織通常使用「OTel 官方 + 公司擴充」的雙層架構：

```
opentelemetry-semantic-conventions/   ← OTel 官方標準（不修改）
  └── model/
      ├── http.yaml
      ├── db.yaml
      └── messaging.yaml

my-company-extensions/                ← 公司內部擴充（注入相同目錄結構）
  └── model/
      ├── legacy-app-attributes.yaml  ← 遺留系統的特殊屬性
      ├── internal-metrics.yaml       ← 公司內部專用指標
      └── business-domains/
          ├── payment.yaml
          ├── cart.yaml
          └── inventory.yaml

# Weaver 同時讀取兩個來源（會合併處理）
weaver registry check \
  --registry ./opentelemetry-semantic-conventions/model \
  --registry ./my-company-extensions/model
```

### 為什麼要繼承官方 semconv？

- **工具相容性**：Grafana、Datadog 等工具對官方 semconv 有內建支援（如 HTTP dashboard 模板）
- **跨組織可讀性**：外部工程師一看屬性名稱就能理解用途
- **社群資源**：可重用社群提供的 SLO 模板、Alert Rules

### 公司擴充的命名隔離

```yaml
# my-company-extensions/model/internal-metrics.yaml
groups:
  # ✓ 使用公司專屬前綴，避免與官方 semconv 衝突
  - id: acme.metric.payment.amount
    type: metric
    metric_name: "acme.payment.amount"   # 公司前綴 "acme."
    instrument: histogram
    unit: "{TWD}"
    stability: stable
    brief: "ACME Corp 支付金額分佈"
```

---

## 11.3 遺留系統整合

遺留系統往往有自己的命名慣例，無法立即遷移到 OTel 標準。

### 遺留系統屬性定義範例

```yaml
# my-company-extensions/model/legacy-app-attributes.yaml
groups:
  - id: legacy.mainframe
    type: attribute_group
    brief: "Legacy 大型主機系統的專屬屬性（過渡期使用）"
    attributes:
      - id: legacy.job_id
        type: string
        stability: development    # development 表示過渡期屬性
        brief: "大型主機作業識別碼（過渡期屬性，目標遷移至 trace.id）"
        deprecated: "計畫於 2025 Q3 移除，請改用標準 trace.id"
        examples: ["JOB-20240601-001"]

      - id: legacy.cics_transaction_id
        type: string
        stability: development
        brief: "CICS 交易識別碼"
        examples: ["TXN-CICS-001"]

      - id: legacy.host_name           # 遺留系統用 host_name（底線）
        type: string
        stability: development
        brief: "遺留系統主機名稱（請改用 host.name）"
        deprecated: "使用 OTel 標準的 host.name 替代"
        examples: ["mainframe-01"]
```

### 遺留系統 Policy（允許過渡期的例外）

```rego
# policies/legacy_exemptions.rego
package otel_weaver

import future.keywords.in

# 遺留系統的 group 允許使用 deprecated 屬性，不需要強制遷移完成
legacy_groups := {
  "legacy.mainframe",
  "legacy.cobol_batch",
  "legacy.tibco_ems",
}

# 只對非遺留 group 強制 git.tag 要求
deny[msg] {
  group := input.groups[_]
  not group.id in legacy_groups
  group.type in ["span", "metric"]
  not attribute_name_exists(group.attributes, "git.tag")
  msg := sprintf(
    "群組 '%s' 缺少必要的 'git.tag' 屬性",
    [group.id]
  )
}

attribute_name_exists(attributes, attr_name) {
  attr := attributes[_]
  attr.name == attr_name
}
```

---

## 11.4 Schema 所有權與治理模型

### 責任分配矩陣（RACI）

```
                        | Platform Team | Domain Team | SRE | Security |
────────────────────────┼───────────────┼─────────────┼─────┼──────────┤
新增 attribute_group    |      A        |      R      |  C  |    I     |
新增 metric/span group  |      C        |      R      |  R  |    I     |
廢棄 stable 屬性        |      A        |      R      |  R  |    I     |
更新 Policy 規則        |      A        |      C      |  R  |    R     |
發布 Schema 版本        |      A        |      C      |  C  |    C     |

R=Responsible（執行）A=Accountable（負責）C=Consulted（諮詢）I=Informed（告知）
```

### CODEOWNERS 設定

```
# .github/CODEOWNERS

# Schema 核心架構：需要 Platform Team 審查
telemetry/registry/common.yaml          @platform-team
policies/                               @platform-team

# 各 domain Schema：由各 domain 負責，SRE 一起審查
telemetry/registry/payment*.yaml        @payments-team @sre-team
telemetry/registry/cart*.yaml           @cart-team @sre-team
telemetry/registry/inventory*.yaml      @inventory-team @sre-team

# 生成的程式碼：只要重新 generate 就會自動更新，不需要人工審查
generated_from_template/               @github-actions[bot]
```

---

## 11.5 企業 Registry 發布流程

```bash
#!/bin/bash
# scripts/publish-schema.sh

set -euo pipefail

VERSION=$(git describe --tags --abbrev=0)
REGISTRY_PATH="./telemetry/registry"
DIST_DIR="./dist"
OUTPUT_PATH="${DIST_DIR}/schema-${VERSION}.yaml"

echo "打包 Schema 版本 ${VERSION}..."

# 建立 dist 目錄
mkdir -p "${DIST_DIR}"

# 驗證並打包
weaver registry check \
  --registry "${REGISTRY_PATH}" \
  --policy ./policies

weaver registry resolve \
  --registry "${REGISTRY_PATH}" \
  --format yaml \
  --output "${OUTPUT_PATH}"

echo "Schema 打包成功：${OUTPUT_PATH}"

# 為所有支援語言生成程式碼
for LANG in go python java; do
  if [ -d "./templates/${LANG}" ]; then
    weaver registry generate \
      --registry "${REGISTRY_PATH}" \
      --templates ./templates \
      "${LANG}" "./generated/${LANG}"
    echo "生成 ${LANG} 程式碼完成"
  fi
done

# 上傳到公司 artifact registry
if [ "${CI:-false}" == "true" ]; then
  # GitHub Releases
  gh release upload "${VERSION}" "${OUTPUT_PATH}"
  
  # 或 AWS S3
  # aws s3 cp "${OUTPUT_PATH}" "s3://company-schemas/otel/${VERSION}/"
  # aws s3 cp "${OUTPUT_PATH}" "s3://company-schemas/otel/latest/schema.yaml"
  
  echo "Schema 已發布到 artifact registry"
fi
```

---

## 11.6 跨語言一致性

企業環境中通常有多種語言的服務。建立多語言模板確保所有語言使用相同的常數值：

### 在 Go module 中發布生成的 Schema

```
internal/semconv/                  # Go module: github.com/acme/semconv
  ├── go.mod
  ├── semconv_attrs.go              # 由 Weaver 生成
  └── semconv_metrics.go            # 由 Weaver 生成
```

### 在 Python package 中發布

```
acme-semconv/                      # PyPI package: acme-semconv
  ├── pyproject.toml
  └── acme_semconv/
      ├── __init__.py
      ├── semconv_attrs.py          # 由 Weaver 生成
      └── semconv_metrics.py        # 由 Weaver 生成
```

### 自動發布流程

```yaml
# .github/workflows/publish-semconv.yml
name: Publish Semconv Packages

on:
  push:
    tags:
      - 'v*'

jobs:
  publish-go:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: open-telemetry/weaver-action/setup@v1

      - name: Generate Go code
        run: |
          weaver registry generate \
            --registry ./telemetry/registry \
            --templates ./templates \
            go ./internal/semconv

      - name: Publish Go module
        run: |
          cd ./internal/semconv
          GOMODVERSION=$(git describe --tags --abbrev=0)
          # Go modules 透過 git tag 自動發布，不需要額外步驟

  publish-python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: open-telemetry/weaver-action/setup@v1

      - name: Generate Python code
        run: |
          weaver registry generate \
            --registry ./telemetry/registry \
            --templates ./templates \
            python ./acme-semconv/acme_semconv

      - name: Build and publish Python package
        run: |
          cd ./acme-semconv
          pip install build twine
          python -m build
          python -m twine upload --repository-url ${{ secrets.PYPI_URL }} dist/*
        env:
          TWINE_PASSWORD: ${{ secrets.PYPI_TOKEN }}
```

---

## 11.7 Schema 健康度指標

建立 Schema 健康度指標，讓管理者了解合規狀況：

```bash
#!/bin/bash
# scripts/schema-health-report.sh
# 輸出 Schema 健康度摘要

echo "# Schema 健康度報告"
echo "生成時間: $(date)"
echo ""

# 統計各類 group 數量
GROUPS_JSON=$(weaver registry resolve \
  --registry ./telemetry/registry \
  --format json)

echo "## Group 統計"
echo "$GROUPS_JSON" | jq -r '
  .groups | group_by(.type) | map({
    type: .[0].type,
    count: length
  }) | .[] | "- \(.type): \(.count)"
'

echo ""
echo "## Stability 分佈"
echo "$GROUPS_JSON" | jq -r '
  .groups | group_by(.stability) | map({
    stability: .[0].stability,
    count: length
  }) | .[] | "- \(.stability): \(.count)"
'

echo ""
echo "## Deprecated 屬性列表"
echo "$GROUPS_JSON" | jq -r '
  .groups[].attributes[] |
  select(.deprecated != null) |
  "- \(.name): \(.deprecated)"
'
```

---

## 11.8 大型團隊的 Schema Review 流程

### PR 模板

```markdown
<!-- .github/PULL_REQUEST_TEMPLATE/schema_change.md -->
## Schema 變更說明

### 變更類型
- [ ] 新增 Group（向後相容）
- [ ] 新增屬性（向後相容）
- [ ] 廢棄屬性（需要遷移期）
- [ ] 移除廢棄屬性（Breaking Change）
- [ ] 修改 Policy
- [ ] 其他（說明）：

### 影響分析
- 影響的服務：
- 影響的 Dashboard/Alert：
- 過渡期計畫（若有 Breaking Change）：
  - 廢棄日期：
  - 計畫移除日期：
  - 遷移說明：

### Checklist
- [ ] `weaver registry check` 通過
- [ ] 已重新 generate 程式碼並 commit
- [ ] 已更新 CHANGELOG
- [ ] SRE 已確認 Dashboard/Alert 不受影響（或已安排遷移）
```

---

## 延伸閱讀

- [OpenTelemetry 語義慣例貢獻指南](https://github.com/open-telemetry/semantic-conventions/blob/main/docs/contribution.md)
- [Comcast OTel 實踐案例](https://www.cncf.io/blog/2023/)
- [OTel Governance 模型](https://opentelemetry.io/community/governance/)
- [Multi-Registry Schema 管理最佳實踐](https://github.com/open-telemetry/weaver/blob/main/docs/registry.md)
