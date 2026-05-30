"""Payment service.

OTel setup is ZERO-CODE — the container runs `opentelemetry-instrument
uvicorn ...` so traces / metrics / OTLP-logs are wired by the distro from
environment variables (OTEL_SERVICE_NAME, OTEL_RESOURCE_ATTRIBUTES,
OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_*_EXPORTER). This module touches no
SDK provider classes.

This module still calls `setup_stdout_json_logging()` for the `kubectl logs`
debug view — that's the only logging concern we own. OTLP logs flow through
the distro's own LoggingHandler in parallel.
"""

import os
import random
import uuid

from fastapi import FastAPI, HTTPException
from opentelemetry import metrics
from pydantic import BaseModel, Field

from o11y_shared import (
    BizEvent,
    FeatureFlags,
    get_logger,
    log_event,
    setup_stdout_json_logging,
)


class ChargeRequest(BaseModel):
    order_id: str
    user_id: str
    amount_cents: int = Field(gt=0)
    currency: str = "USD"


class ChargeResponse(BaseModel):
    payment_id: str
    status: str


setup_stdout_json_logging(level=os.environ.get("LOG_LEVEL", "INFO"))

_payments: dict[str, dict] = {}
_flags = FeatureFlags(file_path=os.environ.get("FEATURE_FLAGS_PATH"))
_log = get_logger("payment_service")
_meter = metrics.get_meter("payment_service")
_charges_counter = _meter.create_counter(
    "payment_charges_total",
    description="Total payment charge attempts",
)
_charge_latency = _meter.create_histogram(
    "payment_charge_duration_seconds",
    description="Charge handler duration",
    unit="s",
)

app = FastAPI(title="payment-service")
_log.info("payment-service started")


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/charge", response_model=ChargeResponse)
async def charge(req: ChargeRequest) -> ChargeResponse:
    import time

    start = time.perf_counter()
    log_event(
        _log,
        BizEvent.PAYMENT_REQUESTED,
        f"charge requested for order {req.order_id}",
        order_id=req.order_id,
        user_id=req.user_id,
        amount_cents=req.amount_cents,
    )

    if random.random() < 0.01:
        log_event(
            _log,
            BizEvent.PAYMENT_GATEWAY_ERROR,
            "upstream gateway timeout",
            order_id=req.order_id,
        )
        _charges_counter.add(1, {"status": "error", "reason": "gateway"})
        _charge_latency.record(time.perf_counter() - start, {"status": "error"})
        raise HTTPException(status_code=502, detail="gateway timeout")

    payment_id = str(uuid.uuid4())
    _payments[payment_id] = {
        "order_id": req.order_id,
        "user_id": req.user_id,
        "amount_cents": req.amount_cents,
        "currency": req.currency,
        "status": "authorized",
    }
    log_event(
        _log,
        BizEvent.PAYMENT_AUTHORIZED,
        f"payment {payment_id} authorized",
        payment_id=payment_id,
        order_id=req.order_id,
    )
    _charges_counter.add(1, {"status": "authorized"})
    _charge_latency.record(time.perf_counter() - start, {"status": "authorized"})

    return ChargeResponse(payment_id=payment_id, status="authorized")
