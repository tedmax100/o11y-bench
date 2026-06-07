.PHONY: help setup check generate run-go run-python run-go-broken run-python-broken \
        live-check-go live-check-python stack-up stack-down report clean

OTLP_ENDPOINT ?= localhost:4317
WEAVER        := docker run --rm -v $(PWD):/workspace -w /workspace \
                 ghcr.io/open-telemetry/weaver:latest
REGISTRY      := /workspace/telemetry/registry
POLICIES      := /workspace/policies

# ── 顏色 ──────────────────────────────────────────────────────
GREEN  := \033[0;32m
YELLOW := \033[1;33m
RED    := \033[0;31m
RESET  := \033[0m

help: ## 顯示所有可用指令
	@echo ""
	@echo "$(GREEN)=== Weaver Demo Makefile ===$(RESET)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(YELLOW)%-28s$(RESET) %s\n", $$1, $$2}'
	@echo ""

# ── 環境準備 ───────────────────────────────────────────────────

setup: ## 安裝 Go 和 Python 依賴
	@echo "$(GREEN)>>> 安裝 Go 依賴$(RESET)"
	cd go-service && go mod download
	@echo "$(GREEN)>>> 安裝 Python 依賴（需要 .venv 已啟動，或用 pip install）$(RESET)"
	pip install -r python-service/requirements.txt
	@echo "$(GREEN)>>> 確認 Weaver CLI 可用$(RESET)"
	$(WEAVER) --version || echo "$(YELLOW)提示: Weaver 透過 Docker 執行$(RESET)"
	mkdir -p reports
	@echo "$(GREEN)✓ 環境準備完成$(RESET)"

# ── Schema 驗證 ────────────────────────────────────────────────

check: ## 執行 Weaver Schema + Policy 靜態驗證
	@echo "$(GREEN)>>> 執行靜態驗證$(RESET)"
	$(WEAVER) registry check \
		--registry $(REGISTRY) \
		--policy $(POLICIES)
	@echo "$(GREEN)✓ Schema 驗證通過$(RESET)"

check-broken: ## 驗證一個刻意錯誤的 Schema（演示報錯）
	@echo "$(YELLOW)>>> 驗證錯誤 Schema（預期會報錯）$(RESET)"
	@echo 'groups:\n  - id: bad.metric\n    type: metric\n    metric_name: bad_no_prefix\n    instrument: counter\n    unit: "{items}"\n    brief: "missing prefix"' > /tmp/bad-schema.yaml
	$(WEAVER) registry check \
		--registry /tmp/bad-schema.yaml \
		--policy $(POLICIES) || true
	@rm /tmp/bad-schema.yaml

diff: ## 顯示當前 Schema 的解析結果（依賴展開）
	$(WEAVER) registry resolve \
		--registry $(REGISTRY)

# ── 程式碼生成 ──────────────────────────────────────────────────

generate-go: ## 從 Schema 生成 Go 常數（需要 templates/go 目錄存在）
	@echo "$(GREEN)>>> 生成 Go 程式碼$(RESET)"
	@echo "$(YELLOW)注意: 已預先生成於 go-service/generated/semconv/$(RESET)"
	@echo "實際使用時執行: weaver registry generate --registry ... --templates ./templates/go"

generate-python: ## 從 Schema 生成 Python 常數
	@echo "$(YELLOW)注意: 已預先生成於 python-service/generated/semconv/$(RESET)"

generate: generate-go generate-python ## 生成所有語言的程式碼

# ── 執行服務（正常模式）─────────────────────────────────────────

run-go: ## 執行 Go 服務（正常模式，符合 Schema）
	@echo "$(GREEN)>>> 執行 Go 服務（符合 Schema 模式）$(RESET)"
	@echo "OTLP endpoint: $(OTLP_ENDPOINT)"
	cd go-service && go run ./cmd/main.go \
		--otlp $(OTLP_ENDPOINT) \
		--loops 15

run-python: ## 執行 Python 服務（正常模式，符合 Schema）
	@echo "$(GREEN)>>> 執行 Python 服務（符合 Schema 模式）$(RESET)"
	@echo "OTLP endpoint: $(OTLP_ENDPOINT)"
	cd python-service && python main.py \
		--otlp $(OTLP_ENDPOINT) \
		--loops 15

# ── 執行服務（破壞模式）─────────────────────────────────────────

run-go-broken: ## 執行 Go 服務（破壞模式，故意使用錯誤屬性）
	@echo "$(RED)>>> 執行 Go 服務（破壞模式 — Weaver 應攔截）$(RESET)"
	cd go-service && go run ./cmd/main.go \
		--otlp $(OTLP_ENDPOINT) \
		--broken \
		--loops 5

run-python-broken: ## 執行 Python 服務（破壞模式）
	@echo "$(RED)>>> 執行 Python 服務（破壞模式 — Weaver 應攔截）$(RESET)"
	cd python-service && python main.py \
		--otlp $(OTLP_ENDPOINT) \
		--broken \
		--loops 5

# ── Live-check 演示 ────────────────────────────────────────────

live-check-setup: ## 在背景啟動 Weaver live-check（監聽 :4317）
	@echo "$(GREEN)>>> 啟動 Weaver live-check (port 4317)$(RESET)"
	@mkdir -p reports
	$(WEAVER) registry live-check \
		--registry $(REGISTRY) \
		--policy $(POLICIES) \
		--input-source otlp \
		--format yaml \
		--output /workspace/reports/weaver-report.yaml \
		--otlp-grpc-address 0.0.0.0 \
		--otlp-grpc-port 4317 &
	@sleep 2
	@echo "$(GREEN)✓ Weaver live-check 已啟動$(RESET)"

live-check-go: ## 完整演示：啟動 live-check + 執行 Go 服務 + 顯示報告
	@echo "$(GREEN)=== Live-check Go 完整演示 ===$(RESET)"
	@mkdir -p reports
	@echo "$(YELLOW)步驟 1: 啟動 Weaver live-check$(RESET)"
	$(WEAVER) registry live-check \
		--registry $(REGISTRY) \
		--policy $(POLICIES) \
		--input-source otlp \
		--format yaml \
		--output /workspace/reports/weaver-report-go.yaml \
		--otlp-grpc-address 0.0.0.0 \
		--otlp-grpc-port 4317 & \
	sleep 2 && \
	echo "$(YELLOW)步驟 2: 執行 Go 服務$(RESET)" && \
	cd go-service && go run ./cmd/main.go \
		--otlp localhost:4317 \
		--loops 10
	@sleep 2
	@$(MAKE) report

live-check-python: ## 完整演示：啟動 live-check + 執行 Python 服務 + 顯示報告
	@echo "$(GREEN)=== Live-check Python 完整演示 ===$(RESET)"
	@mkdir -p reports
	$(WEAVER) registry live-check \
		--registry $(REGISTRY) \
		--policy $(POLICIES) \
		--input-source otlp \
		--format yaml \
		--output /workspace/reports/weaver-report-python.yaml \
		--otlp-grpc-address 0.0.0.0 \
		--otlp-grpc-port 4317 & \
	sleep 2 && \
	cd python-service && python main.py \
		--otlp localhost:4317 \
		--loops 10
	@sleep 2
	@$(MAKE) report

live-check-broken-go: ## 演示攔截：執行 Go 破壞模式並顯示 Weaver 報告
	@echo "$(RED)=== Live-check 攔截演示（Go 破壞模式）===$(RESET)"
	@mkdir -p reports
	$(WEAVER) registry live-check \
		--registry $(REGISTRY) \
		--policy $(POLICIES) \
		--input-source otlp \
		--format yaml \
		--output /workspace/reports/weaver-report-broken.yaml \
		--otlp-grpc-address 0.0.0.0 \
		--otlp-grpc-port 4317 & \
	sleep 2 && \
	cd go-service && go run ./cmd/main.go \
		--otlp localhost:4317 \
		--broken \
		--loops 5 || true
	@sleep 2
	@echo ""
	@echo "$(RED)=== Weaver 報告（應顯示 violation）===$(RESET)"
	@$(MAKE) report

# ── 報告 ───────────────────────────────────────────────────────

report: ## 顯示最新的 Weaver 合規報告
	@echo "$(GREEN)=== Weaver 合規報告 ===$(RESET)"
	@for f in reports/weaver-report*.yaml; do \
		if [ -f "$$f" ]; then \
			echo "$(YELLOW)--- $$f ---$(RESET)"; \
			cat "$$f"; \
			echo ""; \
		fi; \
	done
	@if [ -z "$$(ls reports/weaver-report*.yaml 2>/dev/null)" ]; then \
		echo "$(YELLOW)尚無報告，請先執行 live-check-go 或 live-check-python$(RESET)"; \
	fi

# ── Docker 完整堆疊 ────────────────────────────────────────────

stack-up: ## 啟動完整 Docker 堆疊（Jaeger + Prometheus + Grafana）
	@echo "$(GREEN)>>> 啟動 Docker 堆疊$(RESET)"
	docker compose up -d jaeger prometheus grafana otel-collector
	@echo ""
	@echo "$(GREEN)✓ 服務已啟動：$(RESET)"
	@echo "  Jaeger UI:    http://localhost:16686"
	@echo "  Prometheus:   http://localhost:9090"
	@echo "  Grafana:      http://localhost:3000  (admin/admin)"
	@echo ""
	@echo "接著執行: OTLP_ENDPOINT=localhost:4318 make run-go"

stack-down: ## 停止 Docker 堆疊
	docker compose down

stack-with-weaver: ## 啟動完整堆疊（含 Weaver live-check）
	@echo "$(GREEN)>>> 啟動含 Weaver 的完整堆疊$(RESET)"
	docker compose up -d
	@echo "$(GREEN)✓ 所有服務已啟動$(RESET)"
	@echo "  Weaver OTLP: localhost:4317"
	@echo "  Jaeger UI:   http://localhost:16686"
	@echo "  Grafana:     http://localhost:3000"

# ── 清理 ───────────────────────────────────────────────────────

clean: ## 清理報告和建置產出物
	rm -rf reports/*.yaml
	cd go-service && go clean ./...
	find python-service -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	@echo "$(GREEN)✓ 清理完成$(RESET)"
