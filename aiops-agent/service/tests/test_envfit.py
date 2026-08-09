"""Unit tests for environment fit (s6). Pure — the three stores are faked; this
pins what "the catalog belongs to another environment" has to look like before
governance is allowed to act on it."""

import time

import pytest

import app.signals.envfit as envfit_mod
from app.signals.envfit import EnvFit, compute_env_fit, fit_verdict, get_last_fit


@pytest.fixture(autouse=True)
def _clear_cache():
    envfit_mod._last = None
    yield
    envfit_mod._last = None


def _fit(**kw) -> EnvFit:
    base = dict(
        checked=16,
        resolved=16,
        by_store={"metrics": (6, 6), "logs": (5, 5), "traces": (5, 5)},
        unresolved=[],
        computed_ts=time.time(),
        complete=True,
    )
    base.update(kw)
    return EnvFit(**base)


def test_never_measured_is_unproven():
    assert get_last_fit() is None
    v = fit_verdict()
    assert v["proven_good"] is False and "never checked" in v["note"]


def test_full_fit_is_proven_good():
    envfit_mod._last = _fit()
    v = fit_verdict()
    assert v["proven_good"] is True and v["score"] == 1.0


def test_zero_fit_names_the_first_thing_that_did_not_resolve():
    envfit_mod._last = _fit(
        resolved=0,
        by_store={"metrics": (0, 6), "logs": (0, 5), "traces": (0, 5)},
        unresolved=["metric orders_total (order-service)"],
    )
    v = fit_verdict()
    assert v["proven_good"] is False and v["score"] == 0.0
    assert "orders_total" in v["note"] and "another environment" in v["note"]


def test_a_store_that_did_not_answer_is_unproven_not_zero():
    """Silence is not evidence of a mismatch. It must not read as fit 0.0, and
    it must not read as fit either."""
    envfit_mod._last = _fit(
        resolved=11, checked=11, by_store={"metrics": (6, 6), "logs": (5, 5)}, complete=False
    )
    v = fit_verdict()
    assert v["proven_good"] is False and "did not answer" in v["note"]


def test_stale_measurement_is_not_proven_good():
    envfit_mod._last = _fit(computed_ts=time.time() - 99999)
    v = fit_verdict()
    assert v["proven_good"] is False and "stale" in v["note"]


@pytest.mark.asyncio
async def test_compute_counts_all_three_stores(monkeypatch):
    """The twin case end to end: every store answers, nothing resolves."""
    monkeypatch.setattr(envfit_mod, "_live_metric_names", _async({"acme_orders_count_total"}))
    monkeypatch.setattr(envfit_mod, "_loki_indexable", _async({"service_name"}))
    monkeypatch.setattr(envfit_mod, "_loki_label_values", _async({"unknown_service"}))
    monkeypatch.setattr(envfit_mod, "_tempo_service_names", _async(set()))

    fit = await compute_env_fit()
    assert fit.checked > 0 and fit.resolved == 0 and fit.complete is True
    assert fit.score == 0.0
    assert fit_verdict()["proven_good"] is False


@pytest.mark.asyncio
async def test_an_indexable_key_with_the_wrong_values_does_not_count(monkeypatch):
    """Loki keeps `service_name` and fills it with `unknown_service` when the
    resource attribute is missing, so checking the key alone reads as success."""
    monkeypatch.setattr(envfit_mod, "_live_metric_names", _async(set()))
    monkeypatch.setattr(envfit_mod, "_loki_indexable", _async({"service_name"}))
    monkeypatch.setattr(envfit_mod, "_loki_label_values", _async({"unknown_service"}))
    monkeypatch.setattr(envfit_mod, "_tempo_service_names", _async(set()))

    fit = await compute_env_fit()
    hit, total = fit.by_store["logs"]
    assert total > 0 and hit == 0
    assert any("matches nothing" in u for u in fit.unresolved)


def _async(value):
    async def _f(*_a, **_kw):
        return value

    return _f
