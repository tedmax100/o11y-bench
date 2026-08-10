"""Unit tests for the actuation-readiness preflight.

These pin the verdict logic, not the cluster call: the probe itself is one
SelfSubjectAccessReview and needs a real API server, so `_probe` is faked and
what gets asserted is the thing that actually failed in production — that every
way of *not knowing* whether the credential works produces `proven_good=False`.
"""

import time

import app.signals.actuation as act
from app.signals.actuation import ActuationFit, actuation_verdict


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
