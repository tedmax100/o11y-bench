# 第七章：動態實時驗證 (Live-check)

> 本章介紹 live-check 如何在執行時攔截 OTLP 訊號並驗證合規性，包含 Go 與 Python 的完整整合測試範例、覆蓋率報告解讀，以及 emit 指令的預開發驗證工作流。

---

## 7.1 靜態 vs 動態驗證的差異

靜態驗證（`weaver registry check`）：
- 確保 YAML 規格書語法正確
- 確保 Rego Policy 規則通過
- 無法確認程式碼真的按照規格發送訊號

動態驗證（`weaver registry live-check`）：
- 攔截程式實際發出的 OTLP 訊號
- 即時比對訊號是否符合 Schema
- 提供覆蓋率報告（哪些 metric/span 被測試到了？）
- 可偵測「Schema 定義了但程式沒發送」的情況

### 為什麼靜態驗證不夠？

```
Schema 說 payment.order_id 是 required
→ 但開發人員用了 order_id（少了前綴）
→ weaver registry check 通過（Schema 本身沒錯）
→ 生產環境的 Grafana dashboard 一片空白
→ 事後才發現屬性名稱錯了
```

live-check 的作用是在這兩層之間橋接：**確保程式的實際行為與 Schema 規格相符**。

---

## 7.2 live-check 的工作原理

```
程式 → OTLP Exporter → [Weaver live-check 接收端] → 報告
                              ↓
                       比對收到的屬性名稱
                       vs Schema 定義的屬性
                              ↓
                       輸出 violation / coverage 報告
```

Weaver live-check 作為一個 **OTLP 接收端（receiver）**啟動，接收程式發出的 Spans 和 Metrics，對每個訊號：
1. 查找對應的 Schema group（以 span 名稱 / metric 名稱匹配）
2. 檢查 required 屬性是否存在
3. 檢查屬性名稱是否在 Schema 中定義
4. 執行 Rego Policy（若有指定）
5. 記錄哪些 Schema group 有收到訊號（覆蓋率統計）

---

## 7.3 啟動 Live-check

```bash
# 完整指令
weaver registry live-check \
  --registry ./telemetry/registry \
  --policy ./policies \
  --input-source otlp \
  --format yaml \
  --output ./reports/weaver-report.yaml \
  --otlp-grpc-address 0.0.0.0 \
  --otlp-grpc-port 4318

# 說明：
# --input-source otlp      目前只支援 otlp
# --format yaml             報告格式：yaml 或 json
# --output                  報告輸出路徑（若不指定則輸出到 stdout）
# --otlp-grpc-address       監聽地址（0.0.0.0 = 所有介面）
# --otlp-grpc-port 4318     OTLP HTTP/Protobuf 埠
#                           注意：4317 是 gRPC 原生埠，4318 是 HTTP 埠
```

啟動後輸出：
```
✓ Weaver live-check server started
✓ Listening for OTLP signals on 0.0.0.0:4318
✓ Registry loaded: 5 groups, 12 attributes, 3 metrics
```

---

## 7.4 Go 整合測試範例

```go
// payment/service_test.go
package payment_test

import (
    "context"
    "testing"
    "time"

    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
    "go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
    sdkmetric "go.opentelemetry.io/otel/sdk/metric"
    sdktrace "go.opentelemetry.io/otel/sdk/trace"
    "google.golang.org/grpc"
    "google.golang.org/grpc/credentials/insecure"
)

// setupOTLP 將遙測訊號發往本地 Weaver live-check 實例
func setupOTLP(t *testing.T) func() {
    t.Helper()
    ctx := context.Background()

    // 設定 Trace exporter
    traceExporter, err := otlptracegrpc.New(ctx,
        otlptracegrpc.WithEndpoint("localhost:4318"),
        otlptracegrpc.WithDialOption(grpc.WithTransportCredentials(insecure.NewCredentials())),
    )
    if err != nil {
        t.Fatalf("建立 OTLP trace exporter 失敗: %v", err)
    }

    tp := sdktrace.NewTracerProvider(
        sdktrace.WithBatcher(traceExporter),
    )
    otel.SetTracerProvider(tp)

    // 設定 Metric exporter
    metricExporter, err := otlpmetricgrpc.New(ctx,
        otlpmetricgrpc.WithEndpoint("localhost:4318"),
        otlpmetricgrpc.WithDialOption(grpc.WithTransportCredentials(insecure.NewCredentials())),
    )
    if err != nil {
        t.Fatalf("建立 OTLP metric exporter 失敗: %v", err)
    }

    mp := sdkmetric.NewMeterProvider(
        sdkmetric.WithReader(
            sdkmetric.NewPeriodicReader(metricExporter,
                sdkmetric.WithInterval(100*time.Millisecond), // 測試用：縮短 push 間隔
            ),
        ),
    )
    otel.SetMeterProvider(mp)

    return func() {
        // 強制 flush：確保所有 spans 都發送到 Weaver
        if err := tp.ForceFlush(ctx); err != nil {
            t.Errorf("flush TracerProvider 失敗: %v", err)
        }
        if err := tp.Shutdown(ctx); err != nil {
            t.Errorf("關閉 TracerProvider 失敗: %v", err)
        }
        if err := mp.Shutdown(ctx); err != nil {
            t.Errorf("關閉 MeterProvider 失敗: %v", err)
        }
    }
}

func TestProcessPayment_EmitsCorrectSpan(t *testing.T) {
    // 1. 將遙測訊號導向 Weaver live-check
    shutdown := setupOTLP(t)
    defer shutdown()

    // 2. 建立 service（包含 metric instruments）
    svc, err := NewPaymentService()
    if err != nil {
        t.Fatalf("建立 PaymentService 失敗: %v", err)
    }

    // 3. 執行業務邏輯（應發出合規的遙測訊號）
    err = svc.ProcessPayment(context.Background(), PaymentRequest{
        OrderID:  "ord-20240601-001",
        Amount:   1500.0,
        Provider: "stripe",
        GitTag:   "v1.0.0",
        Env:      "testing",
    })
    if err != nil {
        t.Fatalf("支付失敗: %v", err)
    }

    // 4. 測試結束後 defer shutdown() 會 flush 所有訊號到 Weaver
    // Weaver live-check 在所有訊號接收完畢後輸出報告
    // 若 Exit Code != 0（有 violation），CI 會在 Weaver 步驟失敗
}

func TestProcessPayment_ErrorCase_EmitsErrorType(t *testing.T) {
    shutdown := setupOTLP(t)
    defer shutdown()

    svc, err := NewPaymentService()
    if err != nil {
        t.Fatalf("建立 PaymentService 失敗: %v", err)
    }

    // 負數金額應觸發錯誤，並設定 error.type（conditionally_required）
    err = svc.ProcessPayment(context.Background(), PaymentRequest{
        OrderID:  "ord-20240601-002",
        Amount:   -100.0,
        Provider: "stripe",
        GitTag:   "v1.0.0",
        Env:      "testing",
    })

    // 我們預期業務邏輯返回錯誤
    if err == nil {
        t.Fatal("應該返回錯誤，但沒有")
    }

    // Weaver 會驗證 error.type 屬性是否出現在發出的 Span 中
}
```

---

## 7.5 Python 整合測試範例

```python
# tests/test_payment_telemetry.py
from __future__ import annotations

import pytest
from opentelemetry import trace, metrics
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader


WEAVER_ENDPOINT = "http://localhost:4318"


@pytest.fixture(scope="session", autouse=True)
def setup_otlp_to_weaver():
    """將所有 OTLP 訊號導向本地 Weaver live-check 實例"""
    # Trace provider
    trace_exporter = OTLPSpanExporter(
        endpoint=WEAVER_ENDPOINT,
        insecure=True,
    )
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Metric provider
    metric_exporter = OTLPMetricExporter(
        endpoint=WEAVER_ENDPOINT,
        insecure=True,
    )
    meter_provider = MeterProvider(
        metric_readers=[
            PeriodicExportingMetricReader(
                exporter=metric_exporter,
                export_interval_millis=100,  # 測試用：縮短 push 間隔
            )
        ]
    )
    metrics.set_meter_provider(meter_provider)

    yield

    # 測試結束後強制 flush，確保所有訊號都被 Weaver 接收
    tracer_provider.force_flush()
    tracer_provider.shutdown()
    meter_provider.shutdown()


def test_process_payment_emits_correct_span():
    """驗證支付流程是否發出符合 Schema 的遙測訊號"""
    from payment.service import process_payment

    result = process_payment(
        order_id="ord-20240601-001",
        amount=1500.0,
        provider="stripe",
        git_tag="v1.0.0",
        env="testing",
    )

    assert result is True
    # 若發出的 Span 屬性不合規，Weaver live-check 會在報告中標記
    # CI 跑完後 weaver 回傳 exit code 1，整個流程失敗


def test_payment_error_emits_error_attributes():
    """驗證支付失敗時是否正確記錄 error.type 屬性"""
    from payment.service import process_payment

    result = process_payment(
        order_id="ord-20240601-002",
        amount=-100.0,  # 負數應觸發錯誤
        provider="stripe",
        git_tag="v1.0.0",
        env="testing",
    )

    assert result is False
    # Weaver 會驗證 error.type 屬性是否出現在發出的 Span 中


def test_payment_provider_values():
    """驗證 payment.provider 只使用 Schema 定義的 enum 值"""
    from payment.service import process_payment

    for provider in ["stripe", "paypal", "bank_transfer"]:
        result = process_payment(
            order_id=f"ord-test-{provider}",
            amount=100.0,
            provider=provider,
        )
        assert result is True
```

---

## 7.6 live-check 報告解讀

### 成功報告（無違規）

```yaml
# weaver-report.yaml
summary:
  total_spans: 5
  compliant_spans: 5
  violation_count: 0
  
coverage:
  total_schema_groups: 3
  covered_groups: 3
  coverage_percentage: 100%

details:
  - group: span.payment.process
    received_count: 3
    violations: []
    
  - group: metric.payment.amount
    received_count: 3
    violations: []
```

### 失敗報告（有違規）

```yaml
# weaver-report.yaml
summary:
  total_spans: 3
  compliant_spans: 1
  violation_count: 4

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

coverage:
  total_schema_groups: 3
  covered_groups: 2
  coverage_percentage: 66.7%
  uncovered:
    - "metric.payment.errors"  ← Schema 定義了但測試沒覆蓋到
```

---

## 7.7 使用 `emit` 預先測試儀表板

在應用程式還沒開發完之前，先根據 Schema 發送模擬訊號，讓 SRE 可以先建立 Grafana 儀表板。

```bash
# 發送所有訊號到本地 Prometheus/Grafana 環境
weaver registry emit \
  --registry ./telemetry/registry \
  --otlp-endpoint http://localhost:4317

# 指定特定的群組發送
weaver registry emit \
  --registry ./telemetry/registry \
  --group metric.payment.amount \
  --otlp-endpoint http://localhost:4317

# 持續發送（壓測/視覺化測試用）
weaver registry emit \
  --registry ./telemetry/registry \
  --count 1000 \
  --otlp-endpoint http://localhost:4317
```

### 使用 Docker Compose 建立本地 emit 測試環境

```yaml
# docker-compose.emit-test.yml
version: '3.8'

services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    ports:
      - "4317:4317"
      - "4318:4318"
    volumes:
      - ./otel-collector-config.yaml:/etc/otel-collector-config.yaml:ro
    command: ["--config=/etc/otel-collector-config.yaml"]

  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9090:9090"

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    environment:
      GF_AUTH_ANONYMOUS_ENABLED: "true"
```

```bash
# 啟動環境
docker compose -f docker-compose.emit-test.yml up -d

# 等環境就緒後發送模擬訊號
weaver registry emit \
  --registry ./telemetry/registry \
  --count 50 \
  --otlp-endpoint http://localhost:4317

# 開啟 Grafana: http://localhost:3000
# 開始設計 Dashboard，此時應用程式還不需要完成
```

---

## 7.8 在 CI 中整合 live-check

```yaml
# .github/workflows/live-check.yml
name: Live-check Integration Test

jobs:
  live-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: open-telemetry/weaver-action/setup@v1

      - name: Start Weaver live-check（背景執行）
        run: |
          mkdir -p ./reports
          weaver registry live-check \
            --registry ./telemetry/registry \
            --policy ./policies \
            --input-source otlp \
            --format yaml \
            --output ./reports/weaver-report.yaml \
            --otlp-grpc-address 0.0.0.0 \
            --otlp-grpc-port 4318 &
          WEAVER_PID=$!
          echo "WEAVER_PID=$WEAVER_PID" >> $GITHUB_ENV
          # 等待 Weaver 啟動
          sleep 3

      - name: Run integration tests（OTLP 會送到 Weaver）
        env:
          OTEL_EXPORTER_OTLP_ENDPOINT: "http://localhost:4318"
        run: go test ./... -tags=integration -v

      - name: Stop Weaver and check exit code
        run: |
          kill -SIGTERM $WEAVER_PID
          wait $WEAVER_PID
          WEAVER_EXIT=$?
          if [ $WEAVER_EXIT -ne 0 ]; then
            echo "❌ Weaver live-check 發現違規！"
            cat ./reports/weaver-report.yaml
            exit 1
          fi
          echo "✓ 所有遙測訊號符合 Schema 規範"

      - name: Upload report（無論成功失敗都上傳）
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: weaver-compliance-report
          path: ./reports/weaver-report.yaml
```

---

## 7.9 常見問題與除錯

### 問題：連線被拒絕（Connection refused）

```bash
# 確認 Weaver 已啟動且在正確的埠監聽
netstat -tlnp | grep 4318

# 確認你的 OTLP exporter 端點設定正確
# HTTP/Protobuf 用 4318，gRPC 用 4317
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318"
```

### 問題：Weaver 沒有收到訊號

```bash
# Go：確認 ForceFlush 在測試結束前被呼叫
# 加上 sleep 確認（除錯用）
time.Sleep(500 * time.Millisecond)
tp.ForceFlush(ctx)
```

```python
# Python：確認 force_flush 在 provider shutdown 前被呼叫
tracer_provider.force_flush(timeout_millis=5000)
tracer_provider.shutdown()
```

### 問題：報告顯示 group 未覆蓋但 test 有執行

確認 span 名稱與 Schema 中的 group id 一致：

```go
// Schema 定義：id: span.payment.process
// 程式碼必須用完全相同的名稱
tracer.Start(ctx, "payment.process")  // ← 注意：不含 "span." 前綴
```

---

## 延伸閱讀

- [OpenTelemetry Go SDK Exporters](https://pkg.go.dev/go.opentelemetry.io/otel/exporters/otlp)
- [OpenTelemetry Python SDK](https://opentelemetry-python.readthedocs.io/en/latest/sdk/sdk.html)
- [OTLP 規格](https://opentelemetry.io/docs/specs/otlp/)
