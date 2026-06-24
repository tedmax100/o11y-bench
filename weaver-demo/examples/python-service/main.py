#!/usr/bin/env python3
"""Weaver Demo — Python 版本"""
import argparse
import random
import time

import telemetry_setup
from cart import service as cart_svc
from cart.service import AddItemRequest, CheckoutRequest
from payment import service as payment_svc
from payment.service import PaymentRequest


def main() -> None:
    parser = argparse.ArgumentParser(description="Weaver Demo Python Service")
    parser.add_argument("--otlp", default="localhost:4317", help="OTLP gRPC endpoint")
    parser.add_argument("--broken", action="store_true", help="使用錯誤屬性名稱（演示 Weaver 攔截）")
    parser.add_argument("--loops", type=int, default=10, help="模擬的業務操作次數")
    args = parser.parse_args()

    # 初始化 OTel SDK
    tp, mp = telemetry_setup.setup("weaver-demo-python", args.otlp)

    mode = "❌ 破壞模式（錯誤屬性名稱，Weaver 應攔截）" if args.broken else "✅ 正常模式（符合 Schema）"
    print("\n=== Weaver Demo Python Service ===")
    print(f"模式: {mode}")
    print(f"OTLP endpoint: {args.otlp}")
    print(f"操作次數: {args.loops}\n")

    providers = ["stripe", "paypal", "bank_transfer"]
    items = [
        ("SKU-001", 299.0),
        ("SKU-002", 1500.0),
        ("SKU-003", 89.0),
    ]

    try:
        for i in range(args.loops):
            session_id = f"sess-{random.randint(1000, 9999)}"
            order_id   = f"ord-{time.strftime('%Y%m%d')}-{i:04d}"
            provider   = random.choice(providers)
            item_id, price = random.choice(items)
            qty        = random.randint(1, 3)

            cart_svc.open_session(session_id)

            # 加入商品
            add_req = AddItemRequest(
                session_id=session_id,
                item_id=item_id,
                quantity=qty,
                price=price,
            )
            if args.broken:
                cart_svc.add_item_broken(add_req)
            else:
                cart_svc.add_item(add_req)
            print(f"[{i+1:02d}] Cart:    session={session_id} item={item_id} qty={qty}")

            # 結帳
            checkout_req = CheckoutRequest(
                session_id=session_id,
                items=[add_req],
                total_amount=price * qty,
            )
            cart_svc.checkout(checkout_req)

            # 支付
            pay_req = PaymentRequest(
                order_id=order_id,
                amount=price * qty,
                provider=provider,
                currency="TWD",
            )
            if args.broken:
                ok = payment_svc.process_broken(pay_req)
            else:
                ok = payment_svc.process(pay_req)

            status = "✓" if ok else "❌ declined"
            print(f"[{i+1:02d}] Payment: order={order_id} provider={provider} {status}")

            cart_svc.close_session(session_id)
            time.sleep(0.5)

    finally:
        print("\n所有操作完成，等待 flush...")
        telemetry_setup.shutdown(tp, mp)
        print("✓ 完成")


if __name__ == "__main__":
    main()
