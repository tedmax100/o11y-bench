// demo_const：用三種方式實作同樣的 payment span，說明 const 的意義
package main

import (
	"context"
	"fmt"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/stdout/stdouttrace"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	semconv "weaver-demo/generated/semconv"
)

func main() {
	// 輸出到 stdout 方便觀察
	exp, _ := stdouttrace.New(stdouttrace.WithPrettyPrint())
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	otel.SetTracerProvider(tp)

	fmt.Println("=== 方式 A：手打字串（危險）===")
	withRawStrings(context.Background())

	fmt.Println("\n=== 方式 B：用生成的 const（安全）===")
	withGeneratedConst(context.Background())

	fmt.Println("\n=== 方式 C：改一個名稱，手打 vs const 的差異立刻顯現 ===")
	fmt.Println("假設 Schema 把 payment.order_id 改名為 payment.transaction_id")
	fmt.Println("方式 A：全專案 grep 'payment.order_id' 然後手動改，容易漏")
	fmt.Println("方式 B：只需重跑 weaver generate，然後編譯器幫你找出所有用到 PAYMENT_ORDER_ID 的地方")

	tp.Shutdown(context.Background())
}

// ─────────────────────────────────────────────
// 方式 A：直接手打字串
// ─────────────────────────────────────────────
func withRawStrings(ctx context.Context) {
	tracer := otel.Tracer("demo")
	_, span := tracer.Start(ctx, "payment.process")
	defer span.End()

	// 問題 1：看不出 "order_id" 是不是正確的屬性名稱
	// 問題 2：打錯了（漏掉 payment. 前綴）Weaver live-check 才會發現
	// 問題 3：Schema 改名時，這裡不會有任何編譯錯誤提示
	span.SetAttributes(
		attribute.String("order_id", "ord-001"),   // ❌ 應為 payment.order_id
		attribute.String("provider", "stripe"),    // ❌ 應為 payment.provider
		attribute.String("currency", "TWD"),       // ❌ 應為 payment.currency
	)
}

// ─────────────────────────────────────────────
// 方式 B：用 Weaver 生成的 const
// ─────────────────────────────────────────────
func withGeneratedConst(ctx context.Context) {
	tracer := otel.Tracer("demo")
	_, span := tracer.Start(ctx, "payment.process")
	defer span.End()

	// ✓ 優點 1：IDE 打 semconv.PAY 就能自動補齊，不用查文件
	// ✓ 優點 2：名稱永遠和 Schema YAML 同步（由 Weaver 生成）
	// ✓ 優點 3：Schema 改名 → 重跑 weaver generate → 編譯器報錯 → 不會漏改
	span.SetAttributes(
		semconv.PaymentOrderID.String("ord-001"),
		semconv.PaymentProvider.String("stripe"),
		semconv.PaymentCurrency.String("TWD"),
		semconv.GitTag.String("v1.0.0"),
		semconv.DeploymentEnvironment.String("development"),
	)
}
