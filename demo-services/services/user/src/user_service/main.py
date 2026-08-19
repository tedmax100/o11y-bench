"""User service.

Owns user lookup + a synthetic auth-check endpoint used by order-service.
Zero-code OTel via `opentelemetry-instrument` in the container CMD.

Carries the second incident scenario: `user_session_cache_disabled`. With the
session cache off, every auth check falls through to the (simulated) session
store, which is slow and times out on a fraction of calls. The alert fires on
*order-service* — its orders start failing — while the cause is one hop away
here. That distance is the point: the first scenario's cause and symptom live
in the same service, so an agent can be right about it while reasoning badly.
"""

import asyncio
import os
import random
import time

from fastapi import FastAPI, HTTPException
from o11y_shared import (
    BizEvent,
    FeatureFlags,
    get_logger,
    log_event,
    setup_stdout_json_logging,
)
from opentelemetry import metrics

setup_stdout_json_logging(level=os.environ.get("LOG_LEVEL", "INFO"))

_users: dict[str, dict] = {
    f"u-{i}": {"id": f"u-{i}", "name": f"user-{i}", "tier": "standard"} for i in range(1, 21)
}
_flags = FeatureFlags(file_path=os.environ.get("FEATURE_FLAGS_PATH"))
_log = get_logger("user_service")
_meter = metrics.get_meter("user_service")
_lookups = _meter.create_counter(
    "user_lookups_total",
    description="Total user lookup attempts",
)
_auth_checks = _meter.create_counter(
    "user_auth_checks_total",
    description="Total auth checks",
)
_authcheck_latency = _meter.create_histogram(
    "user_authcheck_duration_seconds",
    description="Auth check handler duration",
    unit="s",
    # Recorded in SECONDS. Without this advisory the SDK's default
    # millisecond-scaled boundaries put every sub-second sample in the first
    # bucket and histogram_quantile returns a constant that reads like a real
    # measurement. Same note as payment-service and order-service.
    explicit_bucket_boundaries_advisory=[
        0.001,
        0.0025,
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
    ],
)

# What the session store costs when nothing is cached in front of it, and how
# often it gives up. Both are what the incident actually looks like from the
# outside; neither is written down anywhere the agent can read.
SESSION_STORE_LATENCY_RANGE_S = (0.18, 0.42)
SESSION_STORE_TIMEOUT_RATE = 0.12

app = FastAPI(title="user-service")
_log.info("user-service started")


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/api/users")
async def list_users() -> dict:
    _lookups.add(1, {"op": "list"})
    return {"users": list(_users.values())}


@app.get("/api/users/{user_id}")
async def get_user(user_id: str) -> dict:
    _lookups.add(1, {"op": "get"})
    user = _users.get(user_id)
    if user is None:
        log_event(
            _log,
            BizEvent.USER_AUTH_FAILED,
            f"unknown user {user_id}",
            user_id=user_id,
            reason="not_found",
        )
        raise HTTPException(status_code=404, detail="user not found")
    return user


@app.get("/api/users/{user_id}/authcheck")
async def authcheck(user_id: str) -> dict:
    """Synthetic endpoint that order-service calls before accepting an order.
    Occasionally fails to simulate auth flakiness — gives the agent something
    to find in logs when correlating with order-service errors."""
    start = time.perf_counter()

    def _done(outcome: str) -> None:
        _authcheck_latency.record(time.perf_counter() - start, {"status": outcome})

    if user_id not in _users:
        _auth_checks.add(1, {"status": "cancelled", "reason": "not_found"})
        _done("cancelled")
        log_event(
            _log,
            BizEvent.USER_AUTH_FAILED,
            f"auth failed for {user_id}: unknown user",
            user_id=user_id,
            reason="not_found",
        )
        raise HTTPException(status_code=401, detail="unauthorized")

    # The flag is read per request, not at startup. The first scenario needs a
    # pod restart to take effect, which means the drill's own restart lands in
    # the same minute as the fault and every latency chart has two explanations.
    if _flags.bool("user_session_cache_disabled", False):
        log_event(
            _log,
            BizEvent.CACHE_MISS,
            f"session cache miss for {user_id}, falling through to the session store",
            user_id=user_id,
            cache="user_session",
        )
        await asyncio.sleep(random.uniform(*SESSION_STORE_LATENCY_RANGE_S))
        if random.random() < SESSION_STORE_TIMEOUT_RATE:
            _auth_checks.add(1, {"status": "error", "reason": "session_store_timeout"})
            _done("error")
            log_event(
                _log,
                BizEvent.USER_AUTH_FAILED,
                f"session store timed out for {user_id}",
                user_id=user_id,
                reason="session_store_timeout",
            )
            raise HTTPException(status_code=503, detail="auth temporarily unavailable")

    if random.random() < 0.005:
        _auth_checks.add(1, {"status": "error", "reason": "transient"})
        _done("error")
        log_event(
            _log,
            BizEvent.USER_AUTH_FAILED,
            f"auth check transient failure for {user_id}",
            user_id=user_id,
            reason="transient",
        )
        raise HTTPException(status_code=503, detail="auth temporarily unavailable")

    _auth_checks.add(1, {"status": "authorized"})
    _done("authorized")
    log_event(
        _log,
        BizEvent.USER_LOGGED_IN,
        f"auth check passed for {user_id}",
        user_id=user_id,
    )
    return {"user_id": user_id, "ok": True}
