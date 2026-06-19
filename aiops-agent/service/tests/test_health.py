"""Unit tests for Signal Plane s4: live dependency-health blame propagation.
The Prometheus call is faked; the topology + contracts are the real shipped
artifacts, so this also pins the wiring (order→payment/user neighbours)."""


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


# ---- neighbour verdict from its SLI ----------------------------------------

async def test_error_sli_above_threshold_unhealthy(monkeypatch):
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({"declined": 0.12}))
    h = await health._evaluate_neighbor("payment-service", "downstream")
    assert h.verdict == "unhealthy"
    assert h.metric == "error"


async def test_error_sli_below_threshold_healthy(monkeypatch):
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({"declined": 0.001}))
    h = await health._evaluate_neighbor("payment-service", "downstream")
    assert h.verdict == "healthy"


async def test_throughput_only_is_unknown(monkeypatch):
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({"user_lookups_total": 5.0}))
    h = await health._evaluate_neighbor("user-service", "downstream")
    assert h.metric == "throughput"
    assert h.verdict == "unknown"


async def test_query_none_is_unavailable(monkeypatch):
    async def _none(expr):
        return None
    monkeypatch.setattr(health, "_instant_scalar", _none)
    h = await health._evaluate_neighbor("payment-service", "downstream")
    assert h.verdict == "unavailable"


async def test_neighbor_without_contract_is_skipped(monkeypatch):
    # api-gateway declares no SLIs → nothing to judge it by.
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({}))
    assert await health._evaluate_neighbor("api-gateway", "upstream") is None


# ---- blame propagation across the real demo topology -----------------------

async def test_unhealthy_downstream_marks_symptom(monkeypatch):
    # order-service investigated; its downstream payment is on fire.
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({
        "declined": 0.12,            # payment error SLI → unhealthy
        "user_lookups_total": 4.7,   # user throughput
    }))
    block = await evaluate_dependency_health(["order-service"])
    assert "downstream payment-service: error 12.0% — UNHEALTHY" in block
    assert "liveness only" in block  # user-service throughput
    assert "SYMPTOM" in block
    assert "investigate the unhealthy dependency first" in block


async def test_healthy_downstream_says_local(monkeypatch):
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({
        "declined": 0.001,           # payment healthy
        "user_lookups_total": 4.7,
    }))
    block = await evaluate_dependency_health(["order-service"])
    assert "healthy" in block
    assert "fault is" in block and "local" in block
    assert "SYMPTOM" not in block


async def test_leaf_service_has_no_downstream(monkeypatch):
    # payment-service is a leaf; only its callers get evaluated.
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({"orders_total": 0.001}))
    block = await evaluate_dependency_health(["payment-service"])
    assert "leaf service" in block
    assert "originates here" in block


async def test_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(health.settings, "signal_dependency_health_enabled", False)
    assert await evaluate_dependency_health(["order-service"]) is None


async def test_unknown_service_returns_none(monkeypatch):
    monkeypatch.setattr(health, "_instant_scalar", _fake_scalar({}))
    assert await evaluate_dependency_health(["mystery-service"]) is None


# ---- formatting ------------------------------------------------------------

def test_fmt_ratio_and_throughput():
    assert "error 12.0% — UNHEALTHY" in _fmt(
        NeighborHealth(service="payment-service", relation="downstream",
                       metric="error", value=0.12, unit="ratio", verdict="unhealthy"))
    assert "liveness only" in _fmt(
        NeighborHealth(service="user-service", relation="downstream",
                       metric="throughput", value=4.7, unit="rps", verdict="unknown"))
