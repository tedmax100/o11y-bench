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
# Same idea for environment fit: these tests are about topology and schema, so
# they run as if someone had already confirmed the catalog belongs here.
_CLEAN_ENV = {"proven_good": True, "score": 1.0, "note": "injected knowledge resolves here (16/16)"}


def _patch(monkeypatch, drift, schema=_CLEAN_SCHEMA, env=_CLEAN_ENV):
    monkeypatch.setattr(dq_mod, "get_last_drift", lambda: drift)
    monkeypatch.setattr(dq_mod, "schema_alignment", lambda: schema)
    monkeypatch.setattr(dq_mod, "fit_verdict", lambda: env)


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


# ---- environment fit (s6) --------------------------------------------------


def test_unmeasured_environment_is_unproven(monkeypatch):
    """Nobody has asked whether the catalog belongs here. That is not evidence
    that it does, so autonomy stays withheld."""
    _patch(
        monkeypatch,
        _drift(),
        env={"proven_good": False, "score": None, "note": "env fit unproven"},
    )
    v = dq_verdict()
    assert v["proven_good"] is False and "unproven" in v["note"]


def test_catalog_from_another_environment_degrades(monkeypatch):
    """The twin-stack case: every store answers, nothing resolves."""
    _patch(
        monkeypatch,
        _drift(),
        env={
            "proven_good": False,
            "score": 0.0,
            "note": "only 0/16 of the injected knowledge resolves against these stores "
            "(metric orders_total (order-service)); the catalog may belong to another environment",
        },
    )
    v = dq_verdict()
    assert v["proven_good"] is False and v["score"] == 0.0
    assert "another environment" in v["note"]


def test_env_fit_is_checked_before_schema_and_topology(monkeypatch):
    """Order matters: a catalog pointed at the wrong stack makes every other
    dimension a measurement of the wrong system, so it is reported first."""
    _patch(
        monkeypatch,
        None,  # no reconcile either
        schema={"checked": 5, "declared_metrics": 6, "undeclared": ["a: b"], "note": "n"},
        env={"proven_good": False, "score": 0.0, "note": "belongs to another environment"},
    )
    assert "another environment" in dq_verdict()["note"]
