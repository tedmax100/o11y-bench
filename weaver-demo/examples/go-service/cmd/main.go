package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"math/rand"
	"time"

	"weaver-demo/internal/cart"
	"weaver-demo/internal/payment"
	"weaver-demo/internal/telemetry"
)

func main() {
	var (
		otlpEndpoint = flag.String("otlp", "localhost:4317", "OTLP gRPC endpoint")
		broken       = flag.Bool("broken", false, "使用錯誤屬性名稱（演示 Weaver 攔截）")
		loops        = flag.Int("loops", 10, "模擬的業務操作次數")
	)
	flag.Parse()

	ctx := context.Background()

	// 初始化 OTel SDK
	shutdown, err := telemetry.Setup(ctx, "weaver-demo-service", *otlpEndpoint)
	if err != nil {
		log.Fatalf("OTel 初始化失敗: %v", err)
	}
	defer shutdown()

	// 建立服務
	paymentSvc, err := payment.NewService()
	if err != nil {
		log.Fatalf("建立 PaymentService 失敗: %v", err)
	}

	cartSvc, err := cart.NewService()
	if err != nil {
		log.Fatalf("建立 CartService 失敗: %v", err)
	}

	mode := "✅ 正常模式（符合 Schema）"
	if *broken {
		mode = "❌ 破壞模式（錯誤屬性名稱，Weaver 應攔截）"
	}
	fmt.Printf("\n=== Weaver Demo Service ===\n")
	fmt.Printf("模式: %s\n", mode)
	fmt.Printf("OTLP endpoint: %s\n", *otlpEndpoint)
	fmt.Printf("操作次數: %d\n\n", *loops)

	providers := []string{"stripe", "paypal", "bank_transfer"}
	items := []struct {
		ID    string
		Price float64
	}{
		{"SKU-001", 299.0},
		{"SKU-002", 1500.0},
		{"SKU-003", 89.0},
	}

	for i := 0; i < *loops; i++ {
		sessionID := fmt.Sprintf("sess-%04d", rand.Intn(9999))
		orderID := fmt.Sprintf("ord-%s-%04d", time.Now().Format("20060102"), i)
		provider := providers[rand.Intn(len(providers))]
		item := items[rand.Intn(len(items))]
		qty := rand.Intn(3) + 1

		cartSvc.OpenSession(ctx)

		// 購物車加入商品
		addReq := cart.AddItemRequest{
			SessionID: sessionID,
			ItemID:    item.ID,
			Quantity:  qty,
			Price:     item.Price,
		}

		if *broken {
			err = cartSvc.AddItemBroken(ctx, addReq)
		} else {
			err = cartSvc.AddItem(ctx, addReq)
		}
		if err != nil {
			log.Printf("[Cart]  AddItem 失敗: %v", err)
		} else {
			fmt.Printf("[%02d] Cart:    session=%s item=%s qty=%d\n", i+1, sessionID, item.ID, qty)
		}

		// 購物車結帳
		checkoutReq := cart.CheckoutRequest{
			SessionID:   sessionID,
			Items:       []cart.AddItemRequest{addReq},
			TotalAmount: item.Price * float64(qty),
		}
		if err = cartSvc.Checkout(ctx, checkoutReq); err != nil {
			log.Printf("[Cart]  Checkout 失敗: %v", err)
		}

		// 支付
		payReq := payment.Request{
			OrderID:  orderID,
			Amount:   item.Price * float64(qty),
			Provider: provider,
			Currency: "TWD",
		}

		if *broken {
			err = paymentSvc.ProcessBroken(ctx, payReq)
		} else {
			err = paymentSvc.Process(ctx, payReq)
		}
		if err != nil {
			fmt.Printf("[%02d] Payment: order=%s provider=%s ❌ %v\n", i+1, orderID, provider, err)
		} else {
			fmt.Printf("[%02d] Payment: order=%s provider=%s ✓\n", i+1, orderID, provider)
		}

		cartSvc.CloseSession(ctx)
		time.Sleep(500 * time.Millisecond)
	}

	fmt.Println("\n所有操作完成，等待 flush...")
	time.Sleep(3 * time.Second)
	fmt.Println("✓ 完成")
}
