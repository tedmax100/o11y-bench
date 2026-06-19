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
    """Return an async _instant_scalar that keys off substrings of the promql.
    Accepts the `at` time arg (s4.2 baseline) but ignores it → current==baseline."""
    async def _scalar(expr: str, at: str = "now"):
        for needle, val in mapping.items():
            if needle in expr:
                return val
        return 0.0
    return _scalar


def _fake_scalar_timed(mapping_now: dict[str, float], mapping_base: dict[str, float]):
    """Like _fake_scalar but returns different values for `at="now"` vs baseline,
    so s4.2 impact can show a rise."""
    async def _scalar(expr: str, at: str = "now"):
        m = mapping_now if at == "now" else mapping_base
        for needle, val in m.items():
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


async def test_downstream_unhealthy_impact_flat_not_symptom(monkeypatch):
    # s4.2 Q2 fix: order itself healthy, payment downstream unhealthy, but order's
    # failures attributed to payment did NOT rise vs baseline → NOT a symptom.
    # (no "orders_total" key → self error SLI 0 AND attribution 0 now & baseline.)
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({
        "payment_charges_total": 0.12,   # payment error SLI → unhealthy
        "user_lookups_total": 4.7,
    }))
    block = await evaluate_dependency_health(["order-service"])
    assert "this service order-service: error 0.0% — healthy" in block
    assert "downstream payment-service: error 12.0% — UNHEALTHY" in block
    # the impact line + the precise verdict
    assert "failures attributed to it" in block
    assert "flat (no material rise" in block
    assert "NOT materially impacted by this incident" in block
    assert "do not report order-service as a symptom" in block


async def test_downstream_unhealthy_impact_rising_is_symptom(monkeypatch):
    # Same topology, but order's payment-attributed failures ROSE vs baseline →
    # confirmed materially impacted, a genuine symptom.
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar_timed(
        mapping_now={'reason=~"payment': 1.5, "payment_charges_total": 0.12, "user_lookups_total": 4.7},
        mapping_base={'reason=~"payment': 0.1, "payment_charges_total": 0.12, "user_lookups_total": 4.7},
    ))
    block = await evaluate_dependency_health(["order-service"])
    assert "downstream payment-service: error 12.0% — UNHEALTHY" in block
    assert "RISING (materially impacted)" in block
    assert "Confirmed: order-service IS materially impacted by payment-service" in block
    assert "genuine SYMPTOM" in block


async def test_downstream_unhealthy_no_attribution_falls_back(monkeypatch):
    # api-gateway declares no attribution edges, so impact can't be measured →
    # fall back to s4.1 cautious wording (confirm before claiming symptom).
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({
        "payment_charges_total": 0.12,   # payment unhealthy
    }))
    block = await evaluate_dependency_health(["api-gateway"])
    assert "downstream payment-service: error 12.0% — UNHEALTHY" in block
    assert "HEALTHY SLIs themselves" in block
    assert "CONFIRM they actually see failures attributed to that dependency" in block


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
