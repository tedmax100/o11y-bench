"""Unit tests for the Data-Quality SLO verdict (s5). Pure — the reconcile drift
is faked; this pins how drift/staleness map to a governance DQ verdict."""

import time

import app.signals.dq as dq_mod
from app.signals.dq import dq_verdict
from app.signals.reconcile import Edge, TopologyDrift


def _drift(**kw):
    base = dict(
        topology_version="1.0.0",
        traces_sampled=50,
        declared_count=6,
        observed_count=6,
        undeclared_edges=[],
        unobserved_edges=[],
        dq_score=1.0,
        computed_ts=time.time(),
    )
    base.update(kw)
    return TopologyDrift(**base)


def _patch(monkeypatch, drift):
    monkeypatch.setattr(dq_mod, "get_last_drift", lambda: drift)


def test_no_reconcile_is_unproven(monkeypatch):
    _patch(monkeypatch, None)
    v = dq_verdict()
    assert v["proven_good"] is False and "unproven" in v["note"]


def test_zero_traces_is_unproven(monkeypatch):
    _patch(monkeypatch, _drift(traces_sampled=0))
    assert dq_verdict()["proven_good"] is False


def test_fresh_aligned_is_proven_good(monkeypatch):
    _patch(monkeypatch, _drift())
    v = dq_verdict()
    assert v["proven_good"] is True
    assert "aligned to live traffic" in v["note"]


def test_undeclared_edge_degrades(monkeypatch):
    _patch(monkeypatch, _drift(undeclared_edges=[Edge(caller="webapp", callee="payment-service")]))
    v = dq_verdict()
    assert v["proven_good"] is False and "drift" in v["note"]


def test_stale_reconcile_degrades(monkeypatch):
    _patch(monkeypatch, _drift(computed_ts=time.time() - 7200))  # 2h old
    v = dq_verdict()
    assert v["proven_good"] is False and "stale" in v["note"]


def test_low_score_degrades(monkeypatch):
    _patch(monkeypatch, _drift(dq_score=0.5))
    v = dq_verdict()
    assert v["proven_good"] is False and "agreement" in v["note"]
