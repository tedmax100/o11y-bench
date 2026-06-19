"""Unit tests for Signal Plane s3: per-service signal contracts and their
injection into the decision-grade context. All pure (shipped yaml + faked
topology); live contract-vs-metric validation is a CLI/k3d smoke."""

import app.signals.context as ctx_mod
from app.signals.context import build_signal_context
from app.signals.contract import (
    SLI,
    SignalContract,
    contract_for,
    get_contracts,
    validate_against_live,
)
from app.signals.topology import Topology


def _topo():
    return Topology.model_validate(
        {
            "version": "1.0.0",
            "nodes": [
                {"name": "payment-service", "tier": 1, "journeys": ["checkout"]},
                {"name": "webapp", "tier": 1, "journeys": ["checkout"]},
            ],
            "journeys": {"checkout": ["webapp", "payment-service"]},
            "edges": [],
        }
    )


# ---- shipped contracts load & match verified metric names ------------------

def test_shipped_contracts_load():
    get_contracts.cache_clear()
    cs = get_contracts()
    assert cs.version == "1.0.0"
    pay = cs.for_service("payment-service")
    assert pay is not None
    kinds = {s.kind for s in pay.slis}
    assert kinds == {"error", "latency", "throughput"}
    # the latency SLI uses the real seconds histogram, queried over _bucket
    lat = next(s for s in pay.slis if s.kind == "latency")
    assert "payment_charge_duration_seconds_bucket" in lat.promql
    assert lat.unit == "s"


def test_contract_for_unknown_is_none():
    assert contract_for("mystery-service") is None


def test_edge_services_have_no_slis_but_caveats():
    gw = contract_for("api-gateway")
    assert gw is not None
    assert gw.slis == []
    assert any("symptom" in ex for ex in gw.exclusions)


# ---- metric base-name extraction + live validation -------------------------

def test_metric_basenames_strip_suffixes():
    c = SignalContract(
        service="x",
        slis=[
            SLI(kind="latency", promql="histogram_quantile(0.95, sum by (le)(rate(foo_duration_seconds_bucket[5m])))"),
            SLI(kind="error", promql="sum(rate(bar_total{status=\"declined\"}[5m]))"),
        ],
    )
    assert c.metric_basenames() == {"foo_duration_seconds", "bar_total"}


def test_validate_against_live_flags_missing():
    pay = contract_for("payment-service")
    # all referenced metrics present → no warnings
    assert validate_against_live(pay, ["payment_charges_total", "payment_charge_duration_seconds"]) == []
    # drop one → drift warning naming the missing metric
    warns = validate_against_live(pay, ["payment_charges_total"])
    assert len(warns) == 1
    assert "payment_charge_duration_seconds" in warns[0]


# ---- injected into the decision-grade context ------------------------------

def test_context_includes_authoritative_sli(monkeypatch):
    monkeypatch.setattr(ctx_mod, "get_topology", _topo)
    monkeypatch.setattr(ctx_mod, "get_last_drift", lambda: None)
    ctx = build_signal_context(["payment-service"])
    assert "SLI (authoritative" in ctx
    assert "payment_charge_duration_seconds_bucket" in ctx
    assert "target: p95 < 0.2s" in ctx
    assert "freshness guarantee: ≤60s" in ctx
    assert "caveat:" in ctx


def test_context_edge_service_shows_caveats_no_sli_header(monkeypatch):
    monkeypatch.setattr(ctx_mod, "get_topology", _topo)
    monkeypatch.setattr(ctx_mod, "get_last_drift", lambda: None)
    ctx = build_signal_context(["webapp"])
    assert "SLI (authoritative" not in ctx   # no SLIs declared
    assert "caveat:" in ctx                   # but exclusions still surface
    assert "originates downstream" in ctx
