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


# A clean schema-alignment artifact, so the topology tests below stay pure and
# do not depend on whether schema_alignment.json happens to be on disk.
_CLEAN_SCHEMA = {"checked": 5, "declared_metrics": 6, "undeclared": [], "note": "ok"}


def _patch(monkeypatch, drift, schema=_CLEAN_SCHEMA):
    monkeypatch.setattr(dq_mod, "get_last_drift", lambda: drift)
    monkeypatch.setattr(dq_mod, "schema_alignment", lambda: schema)


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


# ---- the schema-registry dimension ----------------------------------------


def test_missing_alignment_artifact_is_unproven(monkeypatch):
    """Never checked is not the same as checked and fine."""
    _patch(monkeypatch, _drift(), schema=None)
    v = dq_verdict()
    assert v["proven_good"] is False and "never checked" in v["note"]


def test_unreadable_registry_is_unproven_not_degraded(monkeypatch):
    """An unreadable registry yields an empty declared-metric set, which would
    make every SLI look undeclared. It must land as 'no evidence' instead."""
    _patch(
        monkeypatch,
        _drift(),
        schema={"checked": 0, "declared_metrics": 0, "undeclared": [], "note": "not readable"},
    )
    v = dq_verdict()
    assert v["proven_good"] is False and "unproven" in v["note"]


def test_undeclared_sli_degrades(monkeypatch):
    _patch(
        monkeypatch,
        _drift(),
        schema={
            "checked": 5,
            "declared_metrics": 6,
            "undeclared": ["payment-service: SLI references 'x_total' not declared"],
            "note": "1 undeclared",
        },
    )
    v = dq_verdict()
    assert v["proven_good"] is False and "degraded" in v["note"]
    assert "x_total" in v["note"]  # the message must name the offender


def test_schema_is_checked_before_topology(monkeypatch):
    """A schema violation outranks a missing reconcile: fix the contract first."""
    _patch(
        monkeypatch,
        None,
        schema={"checked": 5, "declared_metrics": 6, "undeclared": ["a: b"], "note": "n"},
    )
    assert "registry does not declare" in dq_verdict()["note"]
