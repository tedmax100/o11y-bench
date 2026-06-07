"""購物車服務 — 使用 Weaver 生成的常數確保屬性名稱合規"""
import random
import time
from dataclasses import dataclass, field
from typing import List

from opentelemetry import trace, metrics
from opentelemetry.trace import SpanKind

from generated.semconv import CartAttrs, CartMetric, CommonAttrs

tracer = trace.get_tracer("cart-service")
meter  = metrics.get_meter("cart-service")

# 建立 Metric instruments（使用生成的常數）
_add_item_counter = meter.create_counter(
    name=CartMetric.ADD_ITEM_COUNT_NAME,
    unit=CartMetric.ADD_ITEM_COUNT_UNIT,
    description=CartMetric.ADD_ITEM_COUNT_DESC,
)
_checkout_histo = meter.create_histogram(
    name=CartMetric.CHECKOUT_TOTAL_NAME,
    unit=CartMetric.CHECKOUT_TOTAL_UNIT,
    description=CartMetric.CHECKOUT_TOTAL_DESC,
)
_active_sessions = meter.create_up_down_counter(
    name=CartMetric.ACTIVE_SESSIONS_NAME,
    unit=CartMetric.ACTIVE_SESSIONS_UNIT,
    description=CartMetric.ACTIVE_SESSIONS_DESC,
)


@dataclass
class AddItemRequest:
    session_id: str
    item_id:    str
    quantity:   int
    price:      float


@dataclass
class CheckoutRequest:
    session_id:   str
    items:        List[AddItemRequest] = field(default_factory=list)
    total_amount: float = 0.0


def open_session(session_id: str) -> None:
    _active_sessions.add(1, attributes={
        CommonAttrs.DEPLOYMENT_ENVIRONMENT: "development",
    })


def close_session(session_id: str) -> None:
    _active_sessions.add(-1, attributes={
        CommonAttrs.DEPLOYMENT_ENVIRONMENT: "development",
    })


def add_item(req: AddItemRequest) -> bool:
    """加入商品，發出符合 Schema 的 OTel 訊號"""
    with tracer.start_as_current_span(
        "cart.add_item", kind=SpanKind.SERVER
    ) as span:
        # ✓ 使用生成的常數
        span.set_attributes({
            CartAttrs.SESSION_ID:               req.session_id,
            CartAttrs.ITEM_ID:                  req.item_id,
            CartAttrs.ITEM_QUANTITY:             req.quantity,
            CartAttrs.ITEM_PRICE:               req.price,
            CommonAttrs.GIT_TAG:                "v1.0.0",
            CommonAttrs.DEPLOYMENT_ENVIRONMENT: "development",
        })
        time.sleep(0.01)

        _add_item_counter.add(req.quantity, attributes={
            CartAttrs.ITEM_ID:                  req.item_id,
            CommonAttrs.DEPLOYMENT_ENVIRONMENT: "development",
        })
        return True


def checkout(req: CheckoutRequest) -> bool:
    """結帳，發出符合 Schema 的 OTel 訊號"""
    with tracer.start_as_current_span(
        "cart.checkout", kind=SpanKind.SERVER
    ) as span:
        span.set_attributes({
            CartAttrs.SESSION_ID:               req.session_id,
            CartAttrs.TOTAL_AMOUNT:             req.total_amount,
            CartAttrs.ITEM_COUNT:               len(req.items),
            CommonAttrs.GIT_TAG:                "v1.0.0",
            CommonAttrs.DEPLOYMENT_ENVIRONMENT: "development",
        })
        time.sleep(0.03)

        _checkout_histo.record(req.total_amount, attributes={
            CommonAttrs.DEPLOYMENT_ENVIRONMENT: "development",
        })
        return True


def add_item_broken(req: AddItemRequest) -> bool:
    """刻意使用錯誤屬性名稱，演示 Weaver live-check 攔截"""
    with tracer.start_as_current_span(
        "cart.add_item", kind=SpanKind.SERVER
    ) as span:
        # ❌ 錯誤的屬性名稱 — 缺少 "cart." 前綴
        span.set_attributes({
            "session":  req.session_id,  # ❌ 應為 cart.session_id
            "item":     req.item_id,     # ❌ 應為 cart.item_id
            "qty":      req.quantity,    # ❌ 應為 cart.item_quantity
        })
        time.sleep(0.01)
        return True
