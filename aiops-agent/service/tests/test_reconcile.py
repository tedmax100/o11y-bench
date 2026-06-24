"""Unit tests for Signal Plane s2: reconciling the declared topology against the
live Tempo call graph. The edge-extraction and diff math are pure (faked OTLP);
the network observe loop is covered by a k3d live smoke, not here."""

import app.signals.context as ctx_mod
from app.signals.context import build_signal_context
from app.signals.reconcile import (
    TopologyDrift,
    diff_edges,
    edges_from_trace,
)
from app.signals.topology import Edge, Topology


def _span(sid, parent, service):
    return {
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
        "scopeSpans": [{"spans": [{"spanId": sid, "parentSpanId": parent, "name": "op"}]}],
    }


def _trace(*batches):
    return {"batches": list(batches)}


def _topo():
    return Topology.model_validate(
        {
            "version": "1.0.0",
            "nodes": [
                {"name": "webapp", "tier": 1, "journeys": ["checkout"]},
                {"name": "api-gateway", "tier": 1, "journeys": ["checkout"]},
                {"name": "payment-service", "tier": 1, "journeys": ["checkout"]},
            ],
            "journeys": {"checkout": ["webapp", "api-gateway", "payment-service"]},
            "edges": [
                {"caller": "webapp", "callee": "api-gateway"},
                {"caller": "api-gateway", "callee": "payment-service"},
            ],
        }
    )


# ---- edge extraction from OTLP traces --------------------------------------


def test_edges_from_trace_call_boundary():
    # api-gateway span is the parent of payment-service span → one edge.
    raw = _trace(
        _span("a1", None, "api-gateway"),
        _span("b1", "a1", "payment-service"),
    )
    assert edges_from_trace(raw) == {("api-gateway", "payment-service")}


def test_edges_from_trace_multi_hop():
    raw = _trace(
        _span("w1", None, "webapp"),
        _span("g1", "w1", "api-gateway"),
        _span("p1", "g1", "payment-service"),
    )
    assert edges_from_trace(raw) == {
        ("webapp", "api-gateway"),
        ("api-gateway", "payment-service"),
    }


def test_edges_from_trace_same_service_no_edge():
    # An internal child span in the same service is not a call boundary.
    raw = _trace(
        _span("a1", None, "api-gateway"),
        _span("a2", "a1", "api-gateway"),
    )
    assert edges_from_trace(raw) == set()


def test_edges_from_trace_root_and_orphans():
    # Root (no parent) and a span whose parent is outside the trace → no edge.
    raw = _trace(
        _span("a1", None, "api-gateway"),
        _span("x1", "missing", "payment-service"),
    )
    assert edges_from_trace(raw) == set()


# ---- diff math -------------------------------------------------------------


def test_diff_perfect_alignment():
    t = _topo()
    observed = {("webapp", "api-gateway"), ("api-gateway", "payment-service")}
    drift = diff_edges(t, observed, traces_sampled=10)
    assert drift.undeclared_edges == []
    assert drift.unobserved_edges == []
    assert drift.dq_score == 1.0


def test_diff_undeclared_edge_is_drift():
    t = _topo()
    # webapp now calls payment-service directly — observed but never declared.
    observed = {
        ("webapp", "api-gateway"),
        ("api-gateway", "payment-service"),
        ("webapp", "payment-service"),
    }
    drift = diff_edges(t, observed, traces_sampled=10)
    assert [(e.caller, e.callee) for e in drift.undeclared_edges] == [("webapp", "payment-service")]
    assert drift.unobserved_edges == []
    # 2 of 3 observed edges are declared.
    assert drift.dq_score == round(2 / 3, 3)


def test_diff_unobserved_declared_edge():
    t = _topo()
    observed = {("webapp", "api-gateway")}  # payment edge had no traffic
    drift = diff_edges(t, observed, traces_sampled=10)
    assert [(e.caller, e.callee) for e in drift.unobserved_edges] == [
        ("api-gateway", "payment-service")
    ]
    assert drift.dq_score == 1.0  # everything observed was declared


def test_diff_no_traffic_dq_none():
    t = _topo()
    drift = diff_edges(t, set(), traces_sampled=0)
    assert drift.dq_score is None
    assert len(drift.unobserved_edges) == 2


# ---- drift surfaced in the injected context --------------------------------


def _drift(**kw):
    base = dict(topology_version="1.0.0", traces_sampled=10, dq_score=1.0)
    base.update(kw)
    return TopologyDrift(**base)


def test_context_marks_unobserved_declared_edge(monkeypatch):
    monkeypatch.setattr(ctx_mod, "get_topology", _topo)
    monkeypatch.setattr(
        ctx_mod,
        "get_last_drift",
        lambda: _drift(unobserved_edges=[Edge(caller="api-gateway", callee="payment-service")]),
    )
    ctx = build_signal_context(["api-gateway"])
    assert "payment-service (⚠ declared, not seen in recent traces)" in ctx


def test_context_surfaces_undeclared_edge_and_dq(monkeypatch):
    monkeypatch.setattr(ctx_mod, "get_topology", _topo)
    monkeypatch.setattr(
        ctx_mod,
        "get_last_drift",
        lambda: _drift(
            dq_score=0.667,
            undeclared_edges=[Edge(caller="webapp", callee="payment-service")],
        ),
    )
    ctx = build_signal_context(["payment-service"])
    assert "observed dependencies NOT in the declared topology: webapp → payment-service" in ctx
    assert "agreement 67%" in ctx
    assert "out of date vs live traffic" in ctx


def test_context_no_drift_no_annotation(monkeypatch):
    monkeypatch.setattr(ctx_mod, "get_topology", _topo)
    monkeypatch.setattr(ctx_mod, "get_last_drift", lambda: None)
    ctx = build_signal_context(["payment-service"])
    assert "⚠" not in ctx
    assert "data-quality" not in ctx
