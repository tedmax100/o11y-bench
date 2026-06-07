# 第八章：CI/CD 整合實戰

> 本章提供完整的 GitHub Actions 工作流配置，涵蓋 Schema 驗證、Drift Detection、Live-check 整合測試、文件同步四個 CI 任務，並附破壞性測試的完整演練流程。

---

## 8.1 CI/CD 防護層的設計理念

一個完整的 Weaver CI 流程應包含四個防護層：

```
PR 觸發
  │
  ├─ [Layer 1] Schema & Policy Check
  │    weaver registry check + Rego Policy
  │    → 確保 YAML 語法正確、命名規則合規
  │
  ├─ [Layer 2] Drift Detection
  │    re-generate → git diff
  │    → 確保生成的程式碼與 Schema 同步
  │
  ├─ [Layer 3] Live-check Integration
  │    weaver live-check + 整合測試
  │    → 確保程式實際發出的訊號符合 Schema
  │
  └─ [Layer 4] Documentation Sync
       weaver generate docs → git diff
       → 確保文件與 Schema 同步
```

這四層互補：
- Layer 1 在靜態分析層攔截設計錯誤
- Layer 2 攔截「Schema 更新但忘記重新 generate」
- Layer 3 攔截「程式碼沒用生成的常數，手打字串並拼錯」
- Layer 4 確保文件不落後於 Schema

---

## 8.2 完整的 GitHub Actions 工作流

```yaml
# .github/workflows/otel-weaver.yml
name: OTel Weaver CI

on:
  pull_request:
    paths:
      - 'telemetry/**'
      - 'policies/**'
      - 'templates/**'
      - 'generated_from_template/**'
      - '**/*.go'
      - '**/*.py'

jobs:
  # ─── Layer 1：Schema & Policy 靜態驗證 ────────────────────────────────────────
  validate-schema:
    name: Validate Schema & Policies
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Setup Weaver
        uses: open-telemetry/weaver-action/setup@v1
        with:
          version: 'latest'

      - name: Check Registry with Policies
        run: |
          weaver registry check \
            --registry ./telemetry/registry \
            --policy ./policies

      - name: Show Schema Summary
        run: |
          weaver registry resolve \
            --registry ./telemetry/registry \
            --format json | jq '{
              total_groups: (.groups | length),
              metrics: [.groups[] | select(.type=="metric") | .metric_name],
              spans: [.groups[] | select(.type=="span") | .id]
            }'

  # ─── Layer 2：Drift Detection ──────────────────────────────────────────────────
  check-generated-code:
    name: Check Generated Code is Up-to-Date
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: open-telemetry/weaver-action/setup@v1

      - name: Re-generate Go code
        run: |
          weaver registry generate \
            --registry ./telemetry/registry \
            --templates ./templates \
            go ./generated_from_template

      - name: Re-generate Python code
        run: |
          weaver registry generate \
            --registry ./telemetry/registry \
            --templates ./templates \
            python ./generated_from_template

      - name: Check for drift
        run: |
          if ! git diff --exit-code ./generated_from_template/; then
            echo "❌ 生成的程式碼與 Schema 不同步！"
            echo ""
            echo "請執行以下指令並 commit 更新後的程式碼："
            echo "  make generate"
            echo ""
            echo "差異如下："
            git diff ./generated_from_template/
            exit 1
          fi
          echo "✓ 生成的程式碼已是最新版本"

  # ─── Layer 3：Live-check 整合測試 ──────────────────────────────────────────────
  live-check-go:
    name: Live-check (Go)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: open-telemetry/weaver-action/setup@v1
      - uses: actions/setup-go@v5
        with:
          go-version: '1.22'

      - name: Start Weaver live-check
        run: |
          mkdir -p ./reports
          weaver registry live-check \
            --registry ./telemetry/registry \
            --policy ./policies \
            --input-source otlp \
            --format yaml \
            --output ./reports/weaver-go-report.yaml \
            --otlp-grpc-address 0.0.0.0 \
            --otlp-grpc-port 4318 &
          echo "WEAVER_PID=$!" >> $GITHUB_ENV
          sleep 3
          echo "✓ Weaver live-check started (PID: $WEAVER_PID)"

      - name: Run Go integration tests
        env:
          OTEL_EXPORTER_OTLP_ENDPOINT: "http://localhost:4318"
          OTEL_SERVICE_NAME: "payment-service"
        run: |
          go test ./... -tags=integration -v -timeout=120s

      - name: Stop Weaver and check result
        run: |
          kill -SIGTERM $WEAVER_PID 2>/dev/null || true
          wait $WEAVER_PID 2>/dev/null || true
          WEAVER_EXIT=$?
          if [ $WEAVER_EXIT -ne 0 ]; then
            echo "❌ Weaver live-check 發現違規！"
            echo "報告內容："
            cat ./reports/weaver-go-report.yaml
            exit 1
          fi
          echo "✓ 所有 Go 遙測訊號符合 Schema 規範"

      - name: Upload compliance report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: weaver-go-compliance-report
          path: ./reports/weaver-go-report.yaml

  live-check-python:
    name: Live-check (Python)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: open-telemetry/weaver-action/setup@v1
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install Python dependencies
        run: pip install -r requirements-test.txt

      - name: Start Weaver live-check
        run: |
          mkdir -p ./reports
          weaver registry live-check \
            --registry ./telemetry/registry \
            --policy ./policies \
            --input-source otlp \
            --format yaml \
            --output ./reports/weaver-python-report.yaml \
            --otlp-grpc-address 0.0.0.0 \
            --otlp-grpc-port 4319 &
          echo "WEAVER_PID=$!" >> $GITHUB_ENV
          sleep 3

      - name: Run Python integration tests
        env:
          OTEL_EXPORTER_OTLP_ENDPOINT: "http://localhost:4319"
          OTEL_SERVICE_NAME: "payment-service-python"
        run: |
          pytest tests/ -v -m integration --timeout=120

      - name: Stop Weaver and check result
        run: |
          kill -SIGTERM $WEAVER_PID 2>/dev/null || true
          wait $WEAVER_PID 2>/dev/null || true
          WEAVER_EXIT=$?
          if [ $WEAVER_EXIT -ne 0 ]; then
            echo "❌ Weaver live-check 發現違規！"
            cat ./reports/weaver-python-report.yaml
            exit 1
          fi
          echo "✓ 所有 Python 遙測訊號符合 Schema 規範"

      - name: Upload compliance report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: weaver-python-compliance-report
          path: ./reports/weaver-python-report.yaml

  # ─── Layer 4：文件同步 ─────────────────────────────────────────────────────────
  check-docs:
    name: Check Documentation is Up-to-Date
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: open-telemetry/weaver-action/setup@v1

      - name: Re-generate docs
        run: |
          weaver registry generate \
            --registry ./telemetry/registry \
            --templates ./templates \
            docs ./docs/telemetry

      - name: Check for drift
        run: |
          if ! git diff --exit-code ./docs/telemetry/; then
            echo "❌ 文件與 Schema 不同步！"
            echo "請執行 'make generate-docs' 並 commit 更新後的文件。"
            exit 1
          fi
          echo "✓ 文件已是最新版本"
```

---

## 8.3 破壞性測試演練

以下範例展示 CI 如何攔截不合規的程式碼：

### Go 破壞性版本

```go
// payment/service_broken.go — 刻意寫錯，用於演示 CI 攔截
package payment

func ProcessPaymentBroken(ctx context.Context, req PaymentRequest) error {
    ctx, span := tracer.Start(ctx, "payment.process")
    defer span.End()

    // ❌ 錯誤：漏掉 "payment." 前綴，live-check 會攔截
    span.SetAttributes(
        attribute.String("order_id", req.OrderID),   // 應該是 payment.order_id
        attribute.String("provider", req.Provider),  // 應該是 payment.provider
        // ❌ 完全漏掉 payment.status（required 屬性！）
    )

    return nil
}
```

### Python 破壞性版本

```python
# payment/service_broken.py
def process_payment_broken(order_id: str, provider: str) -> bool:
    with tracer.start_as_current_span("payment.process") as span:
        # ❌ 手打字串，漏掉前綴
        span.set_attributes({
            "order_id": order_id,   # 應該是 payment.order_id
            "provider": provider,   # 應該是 payment.provider
            # 完全漏掉 payment.status！
        })
    return True
```

### live-check 攔截輸出

```yaml
# weaver-report.yaml
summary:
  total_spans: 2
  compliant_spans: 0
  violation_count: 6

violations:
  - span: "payment.process"
    attribute: "order_id"
    error: "屬性 'order_id' 不在 Schema 定義中"
    suggestion: "可能是 'payment.order_id' 的誤用"
    severity: HIGH

  - span: "payment.process"
    attribute: "provider"
    error: "屬性 'provider' 不在 Schema 定義中"
    suggestion: "可能是 'payment.provider' 的誤用"
    severity: HIGH

  - span: "payment.process"
    missing_required: "payment.order_id"
    error: "required 屬性 'payment.order_id' 未出現在 span 中"
    severity: HIGH

  - span: "payment.process"
    missing_required: "payment.status"
    error: "required 屬性 'payment.status' 未出現在 span 中"
    severity: HIGH

  - span: "payment.process"
    missing_required: "git.tag"
    error: "required 屬性 'git.tag' 未出現在 span 中"
    severity: HIGH

  - span: "payment.process"
    missing_required: "deployment.environment"
    error: "required 屬性 'deployment.environment' 未出現在 span 中"
    severity: HIGH

coverage:
  total_schema_groups: 3
  covered_groups: 1
  coverage_percentage: 33.3%
  uncovered:
    - "metric.payment.amount"
    - "metric.payment.errors"
```

CI 輸出：
```
❌ Weaver live-check 發現違規！
Error: Process completed with exit code 1.
```

---

## 8.4 PR 上的 Review Comment 整合

可以用 GitHub Actions 把 Weaver 報告轉成 PR review comment：

```yaml
  - name: Post Weaver report as PR comment
    if: failure()
    uses: actions/github-script@v7
    with:
      script: |
        const fs = require('fs');
        const yaml = require('js-yaml');
        
        const report = yaml.load(fs.readFileSync('./reports/weaver-go-report.yaml', 'utf8'));
        
        let comment = '## ❌ Weaver Live-check 發現違規\n\n';
        comment += `**總計 ${report.summary.violation_count} 項違規**\n\n`;
        comment += '| Span | 屬性 | 問題 |\n';
        comment += '|------|------|------|\n';
        
        for (const v of report.violations) {
          comment += `| \`${v.span}\` | \`${v.attribute || v.missing_required}\` | ${v.error} |\n`;
        }
        
        comment += '\n請修復上述問題後重新 push。';
        
        github.rest.issues.createComment({
          issue_number: context.issue.number,
          owner: context.repo.owner,
          repo: context.repo.repo,
          body: comment
        });
```

---

## 8.5 GitLab CI 等效配置

```yaml
# .gitlab-ci.yml
stages:
  - validate
  - test

validate-schema:
  stage: validate
  image: otel/weaver:latest
  script:
    - weaver registry check
        --registry ./telemetry/registry
        --policy ./policies
  only:
    changes:
      - telemetry/**/*
      - policies/**/*

live-check:
  stage: test
  image: ubuntu:22.04
  before_script:
    - apt-get update && apt-get install -y curl golang-go
    - curl -Lo /usr/local/bin/weaver
        https://github.com/open-telemetry/weaver/releases/latest/download/weaver-linux-amd64
    - chmod +x /usr/local/bin/weaver
  script:
    - |
      weaver registry live-check \
        --registry ./telemetry/registry \
        --format yaml \
        --output ./weaver-report.yaml \
        --otlp-grpc-port 4318 &
      sleep 3
      go test ./... -tags=integration
      kill %1
      wait %1 || exit 1
  artifacts:
    when: always
    paths:
      - weaver-report.yaml
```

---

## 8.6 Makefile 快速指令整合

```makefile
# Makefile

.PHONY: ci-full validate generate live-check-local

# 完整 CI 流程（本地執行）
ci-full: validate generate-check live-check-local

# Layer 1：Schema & Policy 驗證
validate:
	weaver registry check \
		--registry ./telemetry/registry \
		--policy ./policies

# Layer 2：Drift Detection（本地）
generate-check:
	weaver registry generate \
		--registry ./telemetry/registry \
		--templates ./templates \
		go ./generated_from_template
	weaver registry generate \
		--registry ./telemetry/registry \
		--templates ./templates \
		python ./generated_from_template
	git diff --exit-code ./generated_from_template/ || \
		(echo "Schema 與生成程式碼不同步，請執行 make generate" && exit 1)

# Layer 3：Live-check（需要先啟動 Weaver，再跑測試）
live-check-local:
	@echo "啟動 Weaver live-check..."
	weaver registry live-check \
		--registry ./telemetry/registry \
		--format yaml \
		--output /tmp/weaver-report.yaml \
		--otlp-grpc-port 4318 &
	@sleep 3
	OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
		go test ./... -tags=integration -v
	@kill %1 && wait %1 || true
	@cat /tmp/weaver-report.yaml

# 重新生成所有程式碼
generate:
	weaver registry generate \
		--registry ./telemetry/registry \
		--templates ./templates \
		go ./generated_from_template
	weaver registry generate \
		--registry ./telemetry/registry \
		--templates ./templates \
		python ./generated_from_template
```

---

## 8.7 常見 CI 問題排除

### 問題：Weaver 還沒啟動，測試就開始執行

```bash
# ❌ 問題：sleep 3 不夠可靠
weaver registry live-check ... &
sleep 3
go test ...

# ✓ 改善：輪詢直到 Weaver 就緒
weaver registry live-check ... &
WEAVER_PID=$!
for i in $(seq 1 30); do
  if nc -z localhost 4318 2>/dev/null; then
    echo "Weaver is ready"
    break
  fi
  echo "Waiting for Weaver... ($i/30)"
  sleep 1
done
go test ...
```

### 問題：測試結束後 Weaver 沒有收到所有訊號

```bash
# 測試結束後加入 sleep，給 OTLP exporter 足夠時間 flush
go test ./... -tags=integration
sleep 5    # 等待 BatchSpanProcessor flush 完畢
kill -SIGTERM $WEAVER_PID
wait $WEAVER_PID
```

---

## 延伸閱讀

- [GitHub Actions 文件](https://docs.github.com/en/actions)
- [Weaver GitHub Action](https://github.com/open-telemetry/weaver-action)
- [OTel Collector CI 整合範例](https://github.com/open-telemetry/opentelemetry-collector)
