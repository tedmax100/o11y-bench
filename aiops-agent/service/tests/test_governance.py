"""Unit tests for the action registry and governance gate. All pure — no infra,
no execution (the whole point is that nothing runs)."""

import pytest

import app.actions as actions_mod
import app.governance as gov
from app.actions import ActionDisabled, ActionRegistry, ActionSpec
from app.governance import Autonomy, decide, propose_remediations


def _spec(reversible=True, requires_approval=False, name="k8s.test"):
    return ActionSpec(name=name, description="d", reversible=reversible,
                      requires_approval=requires_approval)


def _calib(labeled=50, overconfidence=0.02):
    return {"labeled": labeled, "overconfidence": overconfidence}


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    monkeypatch.setattr(gov.settings, "governance_conf_high", 0.8)
    monkeypatch.setattr(gov.settings, "governance_conf_low", 0.5)
    monkeypatch.setattr(gov.settings, "governance_max_overconfidence", 0.1)
    monkeypatch.setattr(gov.settings, "governance_min_labeled_runs", 20)
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
        await actions_mod.registry.execute("k8s.rollout_undo", {"deployment": "x", "namespace": "demo"})


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


def test_high_confidence_good_calibration_auto():
    d = decide(_spec(), 0.9, _calib(labeled=50, overconfidence=0.02))
    assert d.autonomy is Autonomy.AUTO and not d.requires_human


# ---- gate: calibration narrows autonomy (the ARE rule) ---------------------

def test_overconfident_history_downgrades_auto_to_propose():
    d = decide(_spec(), 0.9, _calib(labeled=50, overconfidence=0.25))
    assert d.autonomy is Autonomy.PROPOSE
    assert "calibration not proven-good" in d.reason


def test_unproven_calibration_withholds_auto():
    d = decide(_spec(), 0.9, _calib(labeled=3, overconfidence=0.0))
    assert d.autonomy is Autonomy.PROPOSE
    assert "unproven" in d.calibration_note


def test_no_calibration_data_withholds_auto():
    d = decide(_spec(), 0.95, {"labeled": 0, "overconfidence": None})
    assert d.autonomy is Autonomy.PROPOSE


# ---- propose_remediations + format -----------------------------------------

def test_propose_remediations_maps_registered_only():
    decisions = propose_remediations(
        ["k8s.rollout_undo", "totally.unregistered"], confidence=0.9, calib=_calib())
    assert [d.action for d in decisions] == ["k8s.rollout_undo"]  # unregistered skipped
    assert decisions[0].autonomy is Autonomy.PROPOSE  # approval-required


def test_format_decisions_notes_killswitch(monkeypatch):
    monkeypatch.setattr(gov.settings, "actions_enabled", False)
    d = decide(_spec(), 0.9, _calib())  # AUTO by policy
    text = gov.format_decisions([d])
    assert "kill-switch is OFF" in text and "AUTO" in text
