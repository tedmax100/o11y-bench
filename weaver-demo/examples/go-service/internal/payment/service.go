package payment

import (
	"context"
	"fmt"
	"math/rand"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	otelmetric "go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"

	"weaver-demo/generated/semconv"
)

var tracer = otel.Tracer("payment-service")

type Service struct {
	amountHisto   otelmetric.Float64Histogram
	errCounter    otelmetric.Int64Counter
	durationHisto otelmetric.Float64Histogram
}

func NewService() (*Service, error) {
	meter := otel.Meter("payment-service")

	amountHisto, err := meter.Float64Histogram(
		semconv.PaymentAmountName,
		otelmetric.WithDescription(semconv.PaymentAmountDesc),
		otelmetric.WithUnit(semconv.PaymentAmountUnit),
	)
	if err != nil {
		return nil, fmt.Errorf("建立 payment.amount histogram 失敗: %w", err)
	}

	errCounter, err := meter.Int64Counter(
		semconv.PaymentErrorsName,
		otelmetric.WithDescription(semconv.PaymentErrorsDesc),
		otelmetric.WithUnit(semconv.PaymentErrorsUnit),
	)
	if err != nil {
		return nil, fmt.Errorf("建立 payment.errors counter 失敗: %w", err)
	}

	durationHisto, err := meter.Float64Histogram(
		semconv.PaymentDurationName,
		otelmetric.WithDescription(semconv.PaymentDurationDesc),
		otelmetric.WithUnit(semconv.PaymentDurationUnit),
	)
	if err != nil {
		return nil, fmt.Errorf("建立 payment.duration histogram 失敗: %w", err)
	}

	return &Service{
		amountHisto:   amountHisto,
		errCounter:    errCounter,
		durationHisto: durationHisto,
	}, nil
}

type Request struct {
	OrderID  string
	Amount   float64
	Provider string
	Currency string
}

// Process 處理支付請求，發出符合 Schema 的 OTel 訊號
func (s *Service) Process(ctx context.Context, req Request) error {
	ctx, span := tracer.Start(ctx, "payment.process",
		trace.WithSpanKind(trace.SpanKindServer),
	)
	defer span.End()

	start := time.Now()

	// ✓ 使用 Weaver 生成的常數 — IDE 自動補齊，不會拼字錯誤
	span.SetAttributes(
		semconv.PaymentOrderID.String(req.OrderID),
		semconv.PaymentProvider.String(req.Provider),
		semconv.PaymentCurrency.String(req.Currency),
		semconv.GitTag.String("v1.0.0"),
		semconv.DeploymentEnvironment.String("development"),
	)

	// 模擬支付處理耗時
	time.Sleep(time.Duration(rand.Intn(100)) * time.Millisecond)

	// 模擬 10% 失敗率
	if rand.Float64() < 0.1 {
		errType := "card_declined"
		span.SetAttributes(
			semconv.PaymentStatus.String("failed"),
			semconv.ErrorType.String(errType),
		)
		span.SetStatus(codes.Error, "payment declined")

		s.errCounter.Add(ctx, 1,
			otelmetric.WithAttributes(semconv.PaymentProvider.String(req.Provider)),
		)
		elapsed := float64(time.Since(start).Milliseconds())
		s.durationHisto.Record(ctx, elapsed,
			otelmetric.WithAttributes(semconv.PaymentProvider.String(req.Provider)),
		)
		return fmt.Errorf("payment declined: %s", errType)
	}

	span.SetAttributes(semconv.PaymentStatus.String("success"))

	s.amountHisto.Record(ctx, req.Amount,
		otelmetric.WithAttributes(
			semconv.PaymentProvider.String(req.Provider),
			semconv.DeploymentEnvironment.String("development"),
		),
	)
	elapsed := float64(time.Since(start).Milliseconds())
	s.durationHisto.Record(ctx, elapsed,
		otelmetric.WithAttributes(semconv.PaymentProvider.String(req.Provider)),
	)
	return nil
}

// ProcessBroken 刻意使用錯誤的屬性名稱，演示 Weaver live-check 的攔截能力
func (s *Service) ProcessBroken(ctx context.Context, req Request) error {
	ctx, span := tracer.Start(ctx, "payment.process",
		trace.WithSpanKind(trace.SpanKindServer),
	)
	defer span.End()

	// ❌ 錯誤的屬性名稱 — 缺少 "payment." 前綴
	// Weaver live-check 會回報這些 violation
	span.SetAttributes(
		attribute.String("order_id", req.OrderID),       // ❌ 應為 payment.order_id
		attribute.String("pay_provider", req.Provider),  // ❌ 應為 payment.provider
		attribute.String("currency", req.Currency),      // ❌ 應為 payment.currency
	)

	time.Sleep(50 * time.Millisecond)
	return nil
}
