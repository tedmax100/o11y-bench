"""Unit tests for the action registry and governance gate. All pure — no infra,
no execution (the whole point is that nothing runs)."""

import pytest

import app.actions as actions_mod
import app.governance as gov
from app.actions import ActionDisabled, ActionRegistry, ActionSpec
from app.calibration import CalibrationRecord, compute_calibration
from app.governance import Autonomy, decide, propose_remediations


def _spec(reversible=True, requires_approval=False, name="k8s.test"):
    return ActionSpec(
        name=name, description="d", reversible=reversible, requires_approval=requires_approval
    )


def _curve(*groups):
    """(confidence, n_correct, n_wrong)... → a real calibration dict.

    Built by running the actual math over actual records rather than
    hand-shaping a dict, because the gate now reads the reliability bins: a
    hand-written {"labeled": ..., "overconfidence": ...} can express a curve
    that `compute_calibration` could never produce, and a test that passes one
    is testing a curve the system will never see.
    """
    records = []
    for conf, n_ok, n_bad in groups:
        for correct in [True] * n_ok + [False] * n_bad:
            records.append(
                CalibrationRecord(
                    run_id=f"r{len(records)}",
                    ts="2026-01-01T00:00:00Z",
                    confidence=conf,
                    correct=correct,
                )
            )
    return compute_calibration(records)


def _calib():
    """A curve that clears every gate: accuracy tracks confidence in each
    populated bin, and the decision band (≥ 0.8) has 10 runs at 90% accuracy."""
    return _curve((0.9, 9, 1), (0.6, 6, 4), (0.3, 3, 7))


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    monkeypatch.setattr(gov.settings, "governance_conf_high", 0.8)
    monkeypatch.setattr(gov.settings, "governance_conf_low", 0.5)
    monkeypatch.setattr(gov.settings, "governance_max_overconfidence", 0.1)
    monkeypatch.setattr(gov.settings, "governance_min_labeled_runs", 20)
    monkeypatch.setattr(gov.settings, "governance_max_bin_gap", 0.25)
    monkeypatch.setattr(gov.settings, "governance_min_bin_count", 3)
    monkeypatch.setattr(gov.settings, "governance_min_band_accuracy", 0.7)
    yield


# ---- registry --------------------------------------------------------------


def test_registry_register_get_and_dup():
    r = ActionRegistry()
    r.register(_spec(name="a.b"))
    assert r.get("a.b") is not None and r.get("missing") is None
    with pytest.raises(ValueError):
        r.register(_spec(name="a.b"))


async def test_execute_refuses_when_disabled(monkeypatch):
    monkeypatch.setattr(actions_mod.settings, "actions_enabled", False)
    with pytest.raises(ActionDisabled):
        await actions_mod.registry.execute("k8s.rollout_undo", {})


async def test_execute_refuses_when_kill_switch_off():
    # Kill switch off → ActionDisabled regardless of impl presence.
    with pytest.raises(ActionDisabled, match="actions_enabled"):
        await actions_mod.registry.execute(
            "k8s.rollout_undo", {"deployment": "x", "namespace": "demo"}
        )


async def test_execute_unknown_action():
    with pytest.raises(KeyError):
        await actions_mod.registry.execute("nope.nope", {})


def test_seeded_actions_are_safe():
    # every shipped action is reversible and approval-required (7b-4: impl now wired)
    for name in actions_mod.registry.names():
        s = actions_mod.registry.get(name)
        assert s.reversible and s.requires_approval


# ---- gate: hard rules ------------------------------------------------------


def test_irreversible_always_escalates_even_at_max_confidence():
    d = decide(_spec(reversible=False), confidence=1.0, calib=_calib())
    assert d.autonomy is Autonomy.ESCALATE and d.requires_human


def test_approval_required_never_auto():
    d = decide(_spec(requires_approval=True), confidence=0.99, calib=_calib())
    assert d.autonomy is Autonomy.PROPOSE


# ---- gate: confidence bands ------------------------------------------------


def test_low_confidence_escalates():
    assert decide(_spec(), 0.4, _calib()).autonomy is Autonomy.ESCALATE


def test_mid_confidence_proposes():
    assert decide(_spec(), 0.65, _calib()).autonomy is Autonomy.PROPOSE


def test_high_confidence_good_calibration_auto(monkeypatch):
    # bypass the human-label store check: this test verifies the confidence +
    # calibration gate only; the human-label gate is tested in test_learn.py
    monkeypatch.setattr(gov.settings, "governance_min_human_labeled_runs", 0)
    d = decide(_spec(), 0.9, _calib())
    assert d.autonomy is Autonomy.AUTO and not d.requires_human


# ---- gate: calibration narrows autonomy (the ARE rule) ---------------------


def test_overconfident_history_downgrades_auto_to_propose():
    # says 0.9, right half the time → mean overconfidence +0.4
    d = decide(_spec(), 0.9, _curve((0.9, 12, 12)))
    assert d.autonomy is Autonomy.PROPOSE
    assert "calibration not proven-good" in d.reason


def test_unproven_calibration_withholds_auto():
    d = decide(_spec(), 0.9, _curve((0.9, 3, 0)))
    assert d.autonomy is Autonomy.PROPOSE
    assert "unproven" in d.calibration_note


def test_no_calibration_data_withholds_auto():
    d = decide(_spec(), 0.95, {"labeled": 0, "overconfidence": None})
    assert d.autonomy is Autonomy.PROPOSE


# ---- gate: what the signed mean cannot see (the Day31 false green light) ----


def test_offsetting_errors_no_longer_pass(monkeypatch):
    """Underconfident on the easy half, overconfident on the hard half. The mean
    cancels to a passing number; the agent is wrong in both directions."""
    monkeypatch.setattr(gov.settings, "governance_min_human_labeled_runs", 0)
    # says 0.3 and is right 60% of the time; says 0.9 and is right 50%.
    calib = _curve((0.3, 12, 8), (0.9, 10, 10))
    assert calib["overconfidence"] <= gov.settings.governance_max_overconfidence  # mean is green
    d = decide(_spec(), 0.9, calib)
    assert d.autonomy is Autonomy.PROPOSE
    # and it says which region it distrusts, not just "not proven-good"
    assert "0.5" in d.calibration_note and "≥ 0.8" in d.calibration_note


def test_worst_bin_outside_the_band_still_narrows_autonomy(monkeypatch):
    """The decision band looks fine but the curve is wild elsewhere — the agent
    does not understand its own confidence, so the band result is luck."""
    monkeypatch.setattr(gov.settings, "governance_min_human_labeled_runs", 0)
    calib = _curve((0.9, 9, 1), (0.1, 6, 4), (0.7, 3, 7))
    assert calib["overconfidence"] <= gov.settings.governance_max_overconfidence
    d = decide(_spec(), 0.9, calib)
    assert d.autonomy is Autonomy.PROPOSE
    assert "worst bin" in d.calibration_note


def test_empty_decision_band_withholds_auto(monkeypatch):
    """35 labeled runs, none of them at a confidence the gate would act on.
    Plenty of evidence, none of it about the question being asked."""
    monkeypatch.setattr(gov.settings, "governance_min_human_labeled_runs", 0)
    d = decide(_spec(), 0.9, _curve((0.6, 15, 10), (0.3, 3, 7)))
    assert d.autonomy is Autonomy.PROPOSE
    assert "no evidence in the band" in d.calibration_note


def test_thin_bins_are_excluded_and_reported(monkeypatch):
    """A one-run bin is 0% or 100% accurate by construction. It must not trip the
    worst-bin gate — and the note must admit it was skipped, so a narrowed
    evidence base is never silently reported as a clean pass."""
    monkeypatch.setattr(gov.settings, "governance_min_human_labeled_runs", 0)
    d = decide(_spec(), 0.9, _curve((0.9, 9, 1), (0.6, 6, 4), (0.3, 3, 7), (0.5, 0, 1)))
    assert d.autonomy is Autonomy.AUTO
    assert "too thin to count" in d.calibration_note


# ---- propose_remediations + format -----------------------------------------


def test_propose_remediations_maps_registered_only():
    decisions = propose_remediations(
        ["k8s.rollout_undo", "totally.unregistered"], confidence=0.9, calib=_calib()
    )
    assert [d.action for d in decisions] == ["k8s.rollout_undo"]  # unregistered skipped
    assert decisions[0].autonomy is Autonomy.PROPOSE  # approval-required


def test_format_decisions_notes_killswitch(monkeypatch):
    monkeypatch.setattr(gov.settings, "actions_enabled", False)
    monkeypatch.setattr(gov.settings, "governance_min_human_labeled_runs", 0)
    d = decide(_spec(), 0.9, _calib())  # AUTO by policy
    text = gov.format_decisions([d])
    assert "kill-switch is OFF" in text and "AUTO" in text
