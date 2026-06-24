"""支付服務 — 使用 Weaver 生成的常數確保屬性名稱合規"""

import random
import time
from dataclasses import dataclass

from generated.semconv import CommonAttrs, ErrorAttrs, PaymentAttrs, PaymentMetric
from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, StatusCode

tracer = trace.get_tracer("payment-service")
meter = metrics.get_meter("payment-service")

# 使用生成的常數建立 Metric instruments
_amount_histo = meter.create_histogram(
    name=PaymentMetric.AMOUNT_NAME,
    unit=PaymentMetric.AMOUNT_UNIT,
    description=PaymentMetric.AMOUNT_DESC,
)
_error_counter = meter.create_counter(
    name=PaymentMetric.ERRORS_NAME,
    unit=PaymentMetric.ERRORS_UNIT,
    description=PaymentMetric.ERRORS_DESC,
)
_duration_histo = meter.create_histogram(
    name=PaymentMetric.DURATION_NAME,
    unit=PaymentMetric.DURATION_UNIT,
    description=PaymentMetric.DURATION_DESC,
)


@dataclass
class PaymentRequest:
    order_id: str
    amount: float
    provider: str
    currency: str


def process(req: PaymentRequest) -> bool:
    """處理支付請求，發出符合 Schema 的 OTel 訊號"""
    with tracer.start_as_current_span("payment.process", kind=SpanKind.SERVER) as span:
        start = time.monotonic()

        # ✓ 使用 Weaver 生成的常數 — 不會手打錯誤字串
        span.set_attributes(
            {
                PaymentAttrs.ORDER_ID: req.order_id,
                PaymentAttrs.PROVIDER: req.provider,
                PaymentAttrs.CURRENCY: req.currency,
                CommonAttrs.GIT_TAG: "v1.0.0",
                CommonAttrs.DEPLOYMENT_ENVIRONMENT: "development",
            }
        )

        # 模擬處理耗時
        time.sleep(random.uniform(0.01, 0.1))

        # 模擬 10% 失敗率
        if random.random() < 0.1:
            err_type = "card_declined"
            span.set_attributes(
                {
                    PaymentAttrs.STATUS: "failed",
                    ErrorAttrs.TYPE: err_type,
                }
            )
            span.set_status(StatusCode.ERROR, "payment declined")

            _error_counter.add(1, attributes={PaymentAttrs.PROVIDER: req.provider})
            elapsed_ms = (time.monotonic() - start) * 1000
            _duration_histo.record(elapsed_ms, attributes={PaymentAttrs.PROVIDER: req.provider})
            return False

        span.set_attributes({PaymentAttrs.STATUS: "success"})

        _amount_histo.record(
            req.amount,
            attributes={
                PaymentAttrs.PROVIDER: req.provider,
                CommonAttrs.DEPLOYMENT_ENVIRONMENT: "development",
            },
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        _duration_histo.record(elapsed_ms, attributes={PaymentAttrs.PROVIDER: req.provider})
        return True


def process_broken(req: PaymentRequest) -> bool:
    """刻意使用錯誤屬性名稱，演示 Weaver live-check 攔截"""
    with tracer.start_as_current_span("payment.process", kind=SpanKind.SERVER) as span:
        # ❌ 錯誤的屬性名稱 — 缺少 "payment." 前綴
        span.set_attributes(
            {
                "order_id": req.order_id,  # ❌ 應為 payment.order_id
                "pay_provider": req.provider,  # ❌ 應為 payment.provider
                "currency": req.currency,  # ❌ 應為 payment.currency
            }
        )
        time.sleep(0.05)
        return True
