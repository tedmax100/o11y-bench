"""Unit tests for the Signal Plane s1: the first-class topology artifact + the
decision-grade context it builds. All pure (no live infra) — loading, the query
API, live-set validation, and the injected context string."""

import app.signals.context as ctx_mod
from app.signals.context import build_signal_context
from app.signals.topology import (
    Topology,
    get_topology,
    tier_label,
    validate_against_live,
)


def _topo():
    return Topology.model_validate(
        {
            "version": "1.0.0",
            "journeys": {"checkout": ["webapp", "api-gateway", "order-service", "payment-service"]},
            "nodes": [
                {"name": "webapp", "tier": 1, "journeys": ["checkout"]},
                {"name": "api-gateway", "tier": 1, "journeys": ["checkout"]},
                {"name": "order-service", "tier": 1, "journeys": ["checkout"]},
                {"name": "payment-service", "tier": 1, "journeys": ["checkout"]},
                {"name": "user-service", "tier": 2, "journeys": []},
            ],
            "edges": [
                {"caller": "webapp", "callee": "api-gateway"},
                {"caller": "api-gateway", "callee": "user-service"},
                {"caller": "api-gateway", "callee": "order-service"},
                {"caller": "api-gateway", "callee": "payment-service"},
                {"caller": "order-service", "callee": "user-service"},
                {"caller": "order-service", "callee": "payment-service"},
            ],
        }
    )


# ---- shipped artifact loads & matches the catalog facts --------------------

def test_shipped_topology_loads():
    get_topology.cache_clear()
    t = get_topology()
    assert t.version == "1.0.0"
    assert set(t.names()) == {
        "webapp", "api-gateway", "order-service", "payment-service", "user-service",
    }
    # payment-service is a leaf called by api-gateway and order-service.
    assert t.downstream("payment-service") == []
    assert t.upstream("payment-service") == ["api-gateway", "order-service"]


def test_unknown_node_queries_are_empty_not_error():
    t = _topo()
    assert t.node("nope") is None
    assert t.upstream("nope") == []
    assert t.downstream("nope") == []
    assert t.tier_of("nope") is None
    assert t.journey_of("nope") == []


# ---- query API -------------------------------------------------------------

def test_upstream_downstream():
    t = _topo()
    assert t.upstream("user-service") == ["api-gateway", "order-service"]
    assert t.downstream("order-service") == ["payment-service", "user-service"]
    assert t.downstream("webapp") == ["api-gateway"]
    assert t.upstream("webapp") == []  # entry point


def test_impacted_by_is_transitive_upstream():
    t = _topo()
    # If payment-service fails, its callers and their callers all degrade.
    assert t.impacted_by("payment-service") == ["api-gateway", "order-service", "webapp"]
    # webapp is the edge; nothing depends on it.
    assert t.impacted_by("webapp") == []


def test_tier_and_journey():
    t = _topo()
    assert t.tier_of("payment-service") == 1
    assert t.tier_of("user-service") == 2
    assert t.journey_of("payment-service") == ["checkout"]
    assert t.journey_of("user-service") == []
    assert t.journey_position("checkout", "payment-service") == (4, 4)
    assert t.journey_position("checkout", "webapp") == (1, 4)
    assert t.journey_position("checkout", "user-service") is None


def test_tier_label():
    assert tier_label(1) == "revenue/edge-critical"
    assert tier_label(2) == "important"
    assert tier_label(99) == "unclassified"


# ---- live-set validation (s1 alignment surface) ---------------------------

def test_validate_against_live():
    t = _topo()
    # perfect alignment → no warnings
    assert validate_against_live(t, t.names()) == []
    # a declared service gone from telemetry + an undeclared live one
    warns = validate_against_live(t, ["webapp", "api-gateway", "order-service",
                                      "payment-service", "new-service"])
    assert any("user-service" in w and "not present" in w for w in warns)
    assert any("new-service" in w and "not declared" in w for w in warns)


# ---- injected decision-grade context ---------------------------------------

def test_build_signal_context_for_known_service(monkeypatch):
    monkeypatch.setattr(ctx_mod, "get_topology", _topo)
    ctx = build_signal_context(["payment-service"])
    assert ctx is not None
    assert "topology v1.0.0" in ctx
    assert "tier-1 (revenue/edge-critical)" in ctx
    assert "checkout (4/4)" in ctx
    # callers surfaced as upstream; leaf has no downstream
    assert "api-gateway, order-service" in ctx
    assert "leaf" in ctx


def test_build_signal_context_unknown_service_is_none(monkeypatch):
    monkeypatch.setattr(ctx_mod, "get_topology", _topo)
    assert build_signal_context(["mystery-service"]) is None


def test_build_signal_context_disabled(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "signal_plane_enabled", False)
    assert build_signal_context(["payment-service"]) is None


def test_build_signal_context_caps_at_three(monkeypatch):
    monkeypatch.setattr(ctx_mod, "get_topology", _topo)
    ctx = build_signal_context([
        "webapp", "api-gateway", "order-service", "payment-service",
    ])
    assert ctx is not None
    # 4th service must not appear
    assert "### payment-service" not in ctx
    assert "### webapp" in ctx
