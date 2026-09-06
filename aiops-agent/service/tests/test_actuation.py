"""Unit tests for the actuation-readiness preflight.

These pin the verdict logic, not the cluster call: the probe itself is one
SelfSubjectAccessReview and needs a real API server, so `_probe` is faked and
what gets asserted is the thing that actually failed in production — that every
way of *not knowing* whether the credential works produces `proven_good=False`.
"""

import asyncio
import time

import pytest

import app.signals.actuation as act
from app.signals.actuation import ActuationFit, actuation_verdict


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path):
    """Readiness now falls back to the probe history when memory is empty, so
    clearing `_last` is no longer enough to simulate "never checked" — without
    this the tests read whatever the real store happens to hold."""
    monkeypatch.setattr(act.settings, "store_path", str(tmp_path / "probes.db"))


def _fit(**kw):
    base = dict(
        computed_ts=time.time(),
        reachable=True,
        in_cluster=True,
        missing=[],
        excess=[],
        namespaces=["demo"],
        error=None,
    )
    base.update(kw)
    return ActuationFit(**base)


def _set(monkeypatch, fit):
    monkeypatch.setattr(act, "_last", fit)
    monkeypatch.setattr(act.settings, "actuation_check_enabled", True)
    monkeypatch.setattr(act.settings, "actuation_max_age_seconds", 900)


def test_healthy_credentials_are_proven_good(monkeypatch):
    _set(monkeypatch, _fit())
    v = actuation_verdict()
    assert v["proven_good"] and v["score"] == 1.0


def test_never_checked_is_not_proven_good(monkeypatch):
    """The default state must be 'unproven', not 'fine'. This is the state the
    system was in for 46 days."""
    _set(monkeypatch, None)
    v = actuation_verdict()
    assert not v["proven_good"] and "never checked" in v["note"]


def test_dead_token_fails_closed_and_says_so(monkeypatch):
    """A 401 arrives as an exception, so it lands in `error` with reachable=False
    — reported as an authentication failure, not as a denied permission, because
    the two get fixed by different people."""
    _set(monkeypatch, _fit(reachable=False, error="ApiException: Unauthorized"))
    v = actuation_verdict()
    assert not v["proven_good"] and v["score"] == 0.0
    assert "did not authenticate" in v["note"]


def test_stale_check_is_not_proven_good(monkeypatch):
    """A permission checked long enough ago is a permission you are assuming."""
    _set(monkeypatch, _fit(computed_ts=time.time() - 5000))
    v = actuation_verdict()
    assert not v["proven_good"] and "stale" in v["note"]


def test_missing_permission_fails_with_the_rule_named(monkeypatch):
    _set(monkeypatch, _fit(missing=["patch apps/deployments in demo"]))
    v = actuation_verdict()
    assert not v["proven_good"]
    assert "patch apps/deployments in demo" in v["note"]
    assert v["score"] == 0.5  # 1 of 2 required rules survived


def test_excess_permission_is_also_a_failure(monkeypatch):
    """Gaining `delete` is not an improvement — every blast-radius policy in this
    repo was written assuming the write credential cannot do it."""
    _set(monkeypatch, _fit(excess=["delete apps/deployments in demo"]))
    v = actuation_verdict()
    assert not v["proven_good"] and "forbids" in v["note"]


def test_dev_kubeconfig_cannot_prove_readiness(monkeypatch):
    """A developer kubeconfig can do anything, so it can neither prove the
    deployed identity works nor prove it is still limited."""
    _set(monkeypatch, _fit(in_cluster=False))
    v = actuation_verdict()
    assert not v["proven_good"] and "local kubeconfig" in v["note"]


def test_disabled_check_does_not_read_as_healthy(monkeypatch):
    monkeypatch.setattr(act, "_last", _fit())
    monkeypatch.setattr(act.settings, "actuation_check_enabled", False)
    assert not actuation_verdict()["proven_good"]


# ---- the gate this feeds ----------------------------------------------------


def test_governance_withholds_auto_when_actuation_unproven(monkeypatch):
    """The point of the whole module: policy may say yes, but if we cannot show
    the credential still works, autonomy is narrowed before anyone acts."""
    from test_governance import _calib  # a curve that clears every other gate

    import app.governance as gov
    from app.actions import ActionSpec
    from app.governance import Autonomy, decide

    # Isolate the actuation rule: the human-label floor is tested in test_learn.
    monkeypatch.setattr(gov.settings, "governance_min_human_labeled_runs", 0)
    spec = ActionSpec(name="t.a", description="d", reversible=True, requires_approval=False)
    d = decide(
        spec,
        0.9,
        _calib(),
        {"proven_good": True, "note": "dq ok"},
        {"proven_good": False, "note": "readiness unproven"},
        path=None,
    )
    assert d.autonomy is Autonomy.PROPOSE
    assert "actuation readiness not proven-good" in d.reason
    assert d.act_note == "readiness unproven"


# ---- standing probe + rollback capability (Act closure, Stage 0) ------------


def test_probe_is_persisted_so_readiness_has_a_history(monkeypatch, tmp_path):
    """A verdict that only lives in memory can't answer "how long has this been
    broken" — the question nobody could answer for 46 days."""
    import asyncio

    from app import store

    db = tmp_path / "s.db"
    monkeypatch.setattr(act, "_probe", lambda ns: _fit(reachable=False, error="401 Unauthorized"))
    asyncio.run(act.check_actuation(["demo"], source="loop", path=db))

    rows = store.actuation_probe_recent(path=db)
    assert len(rows) == 1
    assert rows[0]["reachable"] == 0
    assert rows[0]["source"] == "loop"
    assert "401" in rows[0]["error"]


def test_storage_failure_does_not_swallow_the_probe(monkeypatch, tmp_path):
    """Recording is best-effort by contract: losing the row must not lose the
    answer the caller is about to act on."""
    import asyncio

    from app import store

    def _boom(**kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "actuation_probe_insert", _boom)
    monkeypatch.setattr(act, "_probe", lambda ns: _fit())
    fit = asyncio.run(act.check_actuation(["demo"], path=tmp_path / "s.db"))
    assert fit.ok


def test_dead_credential_makes_rollback_unavailable_not_failed(monkeypatch, tmp_path):
    """The distinction the only real execution got wrong: we didn't fail to undo,
    we never had the ability to."""
    import asyncio

    monkeypatch.setattr(act, "_probe", lambda ns: _fit(reachable=False, error="401 Unauthorized"))
    ok, why = asyncio.run(act.can_still_write(["demo"], path=tmp_path / "s.db"))
    assert not ok
    assert "no longer authenticate" in why


def test_live_credential_allows_rollback(monkeypatch, tmp_path):
    import asyncio

    monkeypatch.setattr(act, "_probe", lambda ns: _fit())
    ok, why = asyncio.run(act.can_still_write(["demo"], path=tmp_path / "s.db"))
    assert ok and "still valid" in why


# ---- readiness has to outlive the process -----------------------------------
# Every probe was already being persisted; only the read side was missing. That
# is the quietest version of this bug — the write path looks perfectly healthy,
# and the gate still says "never checked" after every restart.


def test_a_probe_survives_the_process_that_took_it(monkeypatch, tmp_path):
    db = tmp_path / "s.db"
    monkeypatch.setattr(act, "_probe", lambda ns: _fit(namespaces=ns))
    monkeypatch.setattr(act.settings, "actuation_check_enabled", True)
    monkeypatch.setattr(act.settings, "actuation_max_age_seconds", 900)
    asyncio.run(act.check_actuation(["demo"], path=db))

    monkeypatch.setattr(act, "_last", None)  # a fresh process
    fit = act.get_last_actuation(path=db)
    assert fit is not None, "the probe did not survive the process"
    assert fit.reachable and fit.namespaces == ["demo"]
    assert actuation_verdict(path=db)["proven_good"] is True


def test_a_stored_denial_comes_back_as_a_denial(monkeypatch, tmp_path):
    """Not just the happy path: a credential that was missing a verb must still
    be missing it after a restart, or the gate reopens on its own."""
    db = tmp_path / "s.db"
    monkeypatch.setattr(
        act, "_probe", lambda ns: _fit(namespaces=ns, missing=["patch apps/deployments in demo"])
    )
    monkeypatch.setattr(act.settings, "actuation_check_enabled", True)
    monkeypatch.setattr(act.settings, "actuation_max_age_seconds", 900)
    asyncio.run(act.check_actuation(["demo"], path=db))

    monkeypatch.setattr(act, "_last", None)
    v = actuation_verdict(path=db)
    assert v["proven_good"] is False and "denied" in v["note"]


def test_an_empty_store_is_still_never_checked(monkeypatch, tmp_path):
    _set(monkeypatch, None)
    assert act.get_last_actuation(path=tmp_path / "empty.db") is None
    v = actuation_verdict(path=tmp_path / "empty.db")
    assert v["proven_good"] is False and "never checked" in v["note"]


def test_unreadable_storage_is_unproven_not_ready(monkeypatch, tmp_path):
    from app import store

    def _boom(*_a, **_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "actuation_probe_recent", _boom)
    _set(monkeypatch, None)
    assert act.get_last_actuation(path=tmp_path / "s.db") is None
    assert actuation_verdict(path=tmp_path / "s.db")["proven_good"] is False


def test_a_malformed_row_is_not_a_readiness_claim(monkeypatch, tmp_path):
    """A row we cannot parse must read as no probe, not as a probe with a bogus
    age — the second one would look fresh and green."""
    from app import store

    monkeypatch.setattr(
        store, "actuation_probe_recent", lambda *a, **k: [{"ts": "not-a-timestamp"}]
    )
    _set(monkeypatch, None)
    assert act.get_last_actuation(path=tmp_path / "s.db") is None
