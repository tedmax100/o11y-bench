# Weaver Demo — 實作範例

## 目錄結構

```
examples/
├── telemetry/registry/      # Schema YAML 定義
│   ├── common.yaml          # 共用屬性
│   ├── payment-spans.yaml   # 支付 Span
│   ├── payment-metrics.yaml # 支付 Metric
│   ├── cart-spans.yaml      # 購物車 Span
│   └── cart-metrics.yaml    # 購物車 Metric
│
├── policies/                # Rego Policy 規則
│   ├── enforce_naming.rego  # 命名規範
│   └── no_breaking_changes.rego
│
├── go-service/              # Go 實作
│   ├── cmd/main.go          # 入口點
│   ├── internal/
│   │   ├── cart/service.go
│   │   ├── payment/service.go
│   │   └── telemetry/setup.go
│   └── generated/semconv/   # Weaver 生成的常數
│
├── python-service/          # Python 實作
│   ├── main.py              # 入口點
│   ├── cart/service.py
│   ├── payment/service.py
│   ├── telemetry_setup.py
│   └── generated/semconv/  # Weaver 生成的常數
│
├── docker-compose.yml       # 完整本地環境
├── otel-collector-config.yaml
├── prometheus.yml
└── Makefile                 # 所有操作入口
```

## 快速開始

### 步驟 1：安裝依賴

```bash
# Go（需要 Go 1.22+）
cd go-service && go mod download && cd ..

# Python（需要 Python 3.11+，建議在 venv 內執行）
pip install -r python-service/requirements.txt
```

### 步驟 2：Schema 靜態驗證

```bash
# 需要 Docker（Weaver 透過 Docker 執行）
make check

# 預期輸出：✓ Schema 驗證通過
```

### 步驟 3：執行服務（無後端，stdout 模式）

```bash
# 若沒有 OTLP 後端，可以先用 stdout exporter 測試
# 在 go-service/internal/telemetry/setup.go 把 exporter 改為 stdouttrace

# Go 正常模式
cd go-service && go run ./cmd/main.go --otlp localhost:4317 --loops 5

# Python 正常模式
cd python-service && python main.py --loops 5
```

### 步驟 4：Live-check 演示（核心功能）

**終端機 1 — 啟動 Weaver live-check：**
```bash
docker run --rm \
  -v $(pwd)/telemetry:/workspace/telemetry:ro \
  -v $(pwd)/policies:/workspace/policies:ro \
  -v $(pwd)/reports:/reports \
  -p 4317:4317 \
  ghcr.io/open-telemetry/weaver:latest \
  registry live-check \
  --registry /workspace/telemetry/registry \
  --policy /workspace/policies \
  --input-source otlp \
  --format yaml \
  --output /reports/weaver-report.yaml \
  --otlp-grpc-address 0.0.0.0 \
  --otlp-grpc-port 4317
```

**終端機 2 — 執行 Go 服務（正常模式）：**
```bash
cd go-service && go run ./cmd/main.go --otlp localhost:4317 --loops 10
```

**終端機 2 — 執行 Go 服務（破壞模式，Weaver 應攔截）：**
```bash
cd go-service && go run ./cmd/main.go --otlp localhost:4317 --broken --loops 5
```

**查看報告：**
```bash
cat reports/weaver-report.yaml
```

### 步驟 5：完整視覺化堆疊

```bash
# 啟動 Jaeger + Prometheus + Grafana + OTel Collector
make stack-up

# 將訊號發往 OTel Collector（port 4318）
OTLP_ENDPOINT=localhost:4318 make run-go

# 瀏覽器打開：
# Traces → http://localhost:16686
# Metrics → http://localhost:9090
# 儀表板  → http://localhost:3000 (admin/admin)
```

## 使用 Makefile 的完整指令

```bash
make help              # 顯示所有指令
make check             # Schema 靜態驗證
make run-go            # 執行 Go 服務（正常）
make run-python        # 執行 Python 服務（正常）
make run-go-broken     # 執行 Go 服務（破壞模式）
make live-check-go     # live-check + Go 服務 + 報告
make live-check-python # live-check + Python 服務 + 報告
make live-check-broken-go  # 演示攔截
make stack-up          # 啟動 Docker 堆疊
make report            # 顯示 Weaver 報告
make clean             # 清理
```

## 關鍵檔案說明

| 檔案 | 說明 |
|------|------|
| `go-service/generated/semconv/payment.go` | Weaver 生成的 Go 常數（勿手改） |
| `python-service/generated/semconv/__init__.py` | Weaver 生成的 Python 常數（勿手改） |
| `go-service/internal/payment/service.go` | 正確用法 vs `ProcessBroken` 錯誤示範 |
| `python-service/payment/service.py` | 正確用法 vs `process_broken` 錯誤示範 |
| `reports/weaver-report.yaml` | live-check 執行後的合規報告 |
