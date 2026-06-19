"""Unit tests for Signal Plane s4: live dependency-health blame propagation.
The Prometheus call is faked; the topology + contracts are the real shipped
artifacts, so this also pins the wiring (order→payment/user neighbours) and the
self-vs-downstream verdict logic."""


import app.signals.health as health
from app.signals.health import (
    NeighborHealth,
    _fmt,
    evaluate_dependency_health,
)


def _fake_scalar(mapping: dict[str, float]):
    """Return an async _instant_scalar that keys off substrings of the promql."""
    async def _scalar(expr: str):
        for needle, val in mapping.items():
            if needle in expr:
                return val
        return 0.0
    return _scalar


# ---- neighbour/self verdict from its SLI -----------------------------------

async def test_error_sli_above_threshold_unhealthy(monkeypatch):
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({"declined": 0.12}))
    h = await health._evaluate("payment-service", "downstream")
    assert h.verdict == "unhealthy"
    assert h.metric == "error"


async def test_error_sli_below_threshold_healthy(monkeypatch):
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({"declined": 0.001}))
    h = await health._evaluate("payment-service", "downstream")
    assert h.verdict == "healthy"


async def test_throughput_only_is_unknown(monkeypatch):
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({"user_lookups_total": 5.0}))
    h = await health._evaluate("user-service", "downstream")
    assert h.metric == "throughput"
    assert h.verdict == "unknown"


async def test_query_none_is_unavailable(monkeypatch):
    async def _none(expr):
        return None
    monkeypatch.setattr(health, "_instant_scalar", _none)
    h = await health._evaluate("payment-service", "downstream")
    assert h.verdict == "unavailable"


async def test_neighbor_without_contract_is_skipped(monkeypatch):
    # api-gateway declares no SLIs → nothing to judge it by.
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({}))
    assert await health._evaluate("api-gateway", "upstream") is None


# ---- blame propagation across the real demo topology -----------------------

async def test_self_breaching_with_no_downstream_is_root_cause(monkeypatch):
    # The payment case: payment is itself breaching its declined-rate SLO and is
    # a leaf — it must be called the root cause, not dismissed as healthy.
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({"declined": 0.41}))
    block = await evaluate_dependency_health(["payment-service"])
    assert "this service payment-service: error 41.0% — UNHEALTHY" in block
    assert "breaches objective declined_rate < 1%" in block
    assert "LIKELY ROOT CAUSE" in block
    assert "Do NOT dismiss this as normal" in block


async def test_unhealthy_downstream_but_self_healthy_is_cautious(monkeypatch):
    # order-service investigated; itself healthy but downstream payment on fire.
    # s4.1 (Q2 fix): must NOT assert order is a symptom — its own SLI is healthy,
    # so tell the agent to confirm impact before blaming the dependency.
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({
        "payment_charges_total": 0.12,   # payment error SLI → unhealthy
        "user_lookups_total": 4.7,       # user throughput
        # orders_total (order's own) → default 0.0 → order healthy
    }))
    block = await evaluate_dependency_health(["order-service"])
    assert "this service order-service: error 0.0% — healthy" in block
    assert "downstream payment-service: error 12.0% — UNHEALTHY" in block
    assert "liveness only" in block  # user-service throughput
    # cautious wording, not an over-claim
    assert "HEALTHY SLIs themselves" in block
    assert "CONFIRM they actually see failures attributed to that dependency" in block
    assert "Fix the unhealthy dependency regardless" in block


async def test_self_and_downstream_both_unhealthy_is_cascade(monkeypatch):
    # order itself erroring AND payment downstream unhealthy → cascade.
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({
        "orders_total": 0.3,             # order's own error SLI → unhealthy
        "payment_charges_total": 0.41,   # payment downstream → unhealthy
        "user_lookups_total": 4.7,
    }))
    block = await evaluate_dependency_health(["order-service"])
    assert "cascading" in block
    assert "order-service" in block and "payment-service" in block


async def test_all_healthy(monkeypatch):
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({}))  # all 0.0
    block = await evaluate_dependency_health(["order-service"])
    assert "Neither the service(s)" in block
    assert "SYMPTOM" not in block and "ROOT CAUSE" not in block


async def test_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(health.settings, "signal_dependency_health_enabled", False)
    assert await evaluate_dependency_health(["order-service"]) is None


async def test_unknown_service_returns_none(monkeypatch):
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({}))
    assert await evaluate_dependency_health(["mystery-service"]) is None


# ---- formatting ------------------------------------------------------------

def test_fmt_self_unhealthy_shows_objective():
    line = _fmt(NeighborHealth(service="payment-service", relation="self",
                               metric="error", value=0.41, unit="ratio",
                               objective="declined_rate < 1%", verdict="unhealthy"))
    assert "this service payment-service: error 41.0% — UNHEALTHY" in line
    assert "breaches objective declined_rate < 1%" in line


def test_fmt_throughput_liveness():
    assert "liveness only" in _fmt(
        NeighborHealth(service="user-service", relation="downstream",
                       metric="throughput", value=4.7, unit="rps", verdict="unknown"))
