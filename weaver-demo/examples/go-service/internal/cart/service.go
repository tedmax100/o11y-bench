package cart

import (
	"context"
	"fmt"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"

	"weaver-demo/generated/semconv"
)

var tracer = otel.Tracer("cart-service")

type Service struct {
	addItemCounter   otelmetric.Int64Counter
	checkoutHisto    otelmetric.Float64Histogram
	activeSessionsGauge otelmetric.Int64UpDownCounter
}

func NewService() (*Service, error) {
	meter := otel.Meter("cart-service")

	addItemCounter, err := meter.Int64Counter(
		semconv.CartAddItemCountName,
		otelmetric.WithDescription(semconv.CartAddItemCountDesc),
		otelmetric.WithUnit(semconv.CartAddItemCountUnit),
	)
	if err != nil {
		return nil, fmt.Errorf("建立 cart.add_item.count 失敗: %w", err)
	}

	checkoutHisto, err := meter.Float64Histogram(
		semconv.CartCheckoutTotalName,
		otelmetric.WithDescription(semconv.CartCheckoutTotalDesc),
		otelmetric.WithUnit(semconv.CartCheckoutTotalUnit),
	)
	if err != nil {
		return nil, fmt.Errorf("建立 cart.checkout.total 失敗: %w", err)
	}

	activeSessionsGauge, err := meter.Int64UpDownCounter(
		semconv.CartActiveSessionsName,
		otelmetric.WithDescription(semconv.CartActiveSessionsDesc),
		otelmetric.WithUnit(semconv.CartActiveSessionsUnit),
	)
	if err != nil {
		return nil, fmt.Errorf("建立 cart.active_sessions 失敗: %w", err)
	}

	return &Service{
		addItemCounter:      addItemCounter,
		checkoutHisto:       checkoutHisto,
		activeSessionsGauge: activeSessionsGauge,
	}, nil
}

type AddItemRequest struct {
	SessionID string
	ItemID    string
	Quantity  int
	Price     float64
}

type CheckoutRequest struct {
	SessionID   string
	Items       []AddItemRequest
	TotalAmount float64
}

// OpenSession 開啟新的購物車 Session
func (s *Service) OpenSession(ctx context.Context) {
	s.activeSessionsGauge.Add(ctx, 1,
		otelmetric.WithAttributes(semconv.DeploymentEnvironment.String("development")),
	)
}

// CloseSession 關閉購物車 Session
func (s *Service) CloseSession(ctx context.Context) {
	s.activeSessionsGauge.Add(ctx, -1,
		otelmetric.WithAttributes(semconv.DeploymentEnvironment.String("development")),
	)
}

// AddItem 加入商品，發出符合 Schema 的 OTel 訊號
func (s *Service) AddItem(ctx context.Context, req AddItemRequest) error {
	ctx, span := tracer.Start(ctx, "cart.add_item",
		trace.WithSpanKind(trace.SpanKindServer),
	)
	defer span.End()

	// ✓ 使用生成的常數
	span.SetAttributes(
		semconv.CartSessionID.String(req.SessionID),
		semconv.CartItemID.String(req.ItemID),
		semconv.CartItemQuantity.Int(req.Quantity),
		semconv.CartItemPrice.Float64(req.Price),
		semconv.GitTag.String("v1.0.0"),
		semconv.DeploymentEnvironment.String("development"),
	)

	time.Sleep(10 * time.Millisecond)

	s.addItemCounter.Add(ctx, int64(req.Quantity),
		otelmetric.WithAttributes(
			semconv.CartItemID.String(req.ItemID),
			semconv.DeploymentEnvironment.String("development"),
		),
	)
	return nil
}

// Checkout 結帳，發出符合 Schema 的 OTel 訊號
func (s *Service) Checkout(ctx context.Context, req CheckoutRequest) error {
	ctx, span := tracer.Start(ctx, "cart.checkout",
		trace.WithSpanKind(trace.SpanKindServer),
	)
	defer span.End()

	span.SetAttributes(
		semconv.CartSessionID.String(req.SessionID),
		semconv.CartTotalAmount.Float64(req.TotalAmount),
		semconv.CartItemCount.Int(len(req.Items)),
		semconv.GitTag.String("v1.0.0"),
		semconv.DeploymentEnvironment.String("development"),
	)

	time.Sleep(30 * time.Millisecond)

	s.checkoutHisto.Record(ctx, req.TotalAmount,
		otelmetric.WithAttributes(semconv.DeploymentEnvironment.String("development")),
	)
	return nil
}

// AddItemBroken 刻意使用錯誤屬性名稱，演示 live-check 攔截
func (s *Service) AddItemBroken(ctx context.Context, req AddItemRequest) error {
	ctx, span := tracer.Start(ctx, "cart.add_item",
		trace.WithSpanKind(trace.SpanKindServer),
	)
	defer span.End()

	// ❌ 錯誤屬性名稱 — 缺少 "cart." 前綴
	span.SetAttributes(
		attribute.String("session", req.SessionID),   // ❌ 應為 cart.session_id
		attribute.String("item", req.ItemID),         // ❌ 應為 cart.item_id
		attribute.Int("qty", req.Quantity),           // ❌ 應為 cart.item_quantity
	)
	_ = ctx
	return nil
}
