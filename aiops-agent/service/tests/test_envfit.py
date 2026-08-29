"""Unit tests for environment fit (s6). Pure — the three stores are faked; this
pins what "the catalog belongs to another environment" has to look like before
governance is allowed to act on it."""

import time

import pytest

import app.signals.envfit as envfit_mod
from app.signals.envfit import EnvFit, compute_env_fit, fit_verdict, get_last_fit


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch, tmp_path):
    # The fit is now persisted as well as cached, so isolating the memory alone
    # is no longer enough: a pathless compute would write into the real store and
    # the next "never measured" case would read it back.
    from app.config import settings

    monkeypatch.setattr(settings, "store_path", str(tmp_path / "envfit.db"))
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


# ---- the gate's evidence has to outlive the process -------------------------
# The bug this pins: env fit lived only in a module-level variable, so measuring
# a perfect score from one process left the serving process' gate red — it had
# never asked. A rollout erased the evidence for a gate whose whole argument is
# that evidence must be durable.


def _all_stores_answer_yes(monkeypatch) -> None:
    """Every store knows everything the catalog names — the 1.0 case."""
    from app.signals.contract import get_contracts
    from app.signals.topology import get_topology

    contracts = get_contracts().contracts
    metrics = {b for c in contracts for b in c.metric_basenames()}
    values = {
        c.logs.selector.strip("{}").partition("=")[2].strip().strip('"')
        for c in contracts
        if c.logs and c.logs.selector
    }
    keys = {
        c.logs.selector.strip("{}").partition("=")[0].strip()
        for c in contracts
        if c.logs and c.logs.selector
    }
    monkeypatch.setattr(envfit_mod, "_live_metric_names", _async(metrics))
    monkeypatch.setattr(envfit_mod, "_loki_indexable", _async(keys))
    monkeypatch.setattr(envfit_mod, "_loki_label_values", _async(values))
    monkeypatch.setattr(envfit_mod, "_tempo_service_names", _async(set(get_topology().names())))


@pytest.mark.asyncio
async def test_fit_is_persisted_and_readable_by_another_process(monkeypatch, tmp_path):
    db = tmp_path / "s.db"
    _all_stores_answer_yes(monkeypatch)
    measured = await compute_env_fit(path=db)
    assert measured.score == 1.0

    envfit_mod._last = None  # a fresh process: nothing in memory
    fit = get_last_fit(path=db)
    assert fit is not None, "the measurement did not survive the process"
    assert fit.score == 1.0
    assert fit.by_store == measured.by_store
    assert fit_verdict(path=db)["proven_good"] is True


def test_unmeasured_environment_still_reads_as_unproven(tmp_path):
    envfit_mod._last = None
    assert get_last_fit(path=tmp_path / "empty.db") is None
    assert fit_verdict(path=tmp_path / "empty.db")["proven_good"] is False


def test_storage_failure_leaves_the_fit_unproven_not_fitting(monkeypatch, tmp_path):
    """An unreadable store is "we do not know", which the verdict must treat the
    same as never measured — never as a pass."""
    from app import store

    def _boom(*_a, **_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "env_fit_latest", _boom)
    envfit_mod._last = None
    assert get_last_fit(path=tmp_path / "s.db") is None
    assert fit_verdict(path=tmp_path / "s.db")["proven_good"] is False
