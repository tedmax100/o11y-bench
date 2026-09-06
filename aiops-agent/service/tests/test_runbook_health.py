"""A runbook's own execution record, and the two places it now reaches.

`runbook_feedback` had been collecting outcomes since the executor was written
and nothing read it but a report endpoint, so a procedure that had failed
verification four times running was still injected as authoritative guidance and
still eligible for autonomous execution. These pin both halves: what the model
is told, and what the gate allows."""

import app.agent as agent
import app.governance as governance
import app.store as store
from app.actions import ActionSpec


def _cfg(monkeypatch, tmp_path):
    p = tmp_path / "aiops.db"
    monkeypatch.setattr(store.settings, "store_path", str(p))
    return p


def _feedback(p, runbook_id, outcomes):
    for o in outcomes:
        store.rb_feedback_insert(runbook_id=runbook_id, outcome=o, path=p)


# ---- one definition of decay ------------------------------------------------


def test_a_runbook_nobody_ran_has_no_record_not_a_clean_record(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    assert store.rb_health("never-run", path=p)["status"] == store.RB_NO_RECORD


def test_one_failure_out_of_one_is_not_a_hundred_percent(monkeypatch, tmp_path):
    """The reporting rule the game day produced: below the minimum, a rate is a
    rumour. It used to print 100% off a single run."""
    p = _cfg(monkeypatch, tmp_path)
    _feedback(p, "payment-bad-deploy", ["verify_failed"])
    h = store.rb_health("payment-bad-deploy", path=p)
    assert h["status"] == store.RB_NO_RECORD
    assert "too few to rate" in h["note"]
    assert store.rb_feedback_health_report(path=p) == []


def test_sustained_verify_failure_is_decay(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    _feedback(p, "payment-bad-deploy", ["ok", "verify_failed", "verify_failed", "ok"])
    h = store.rb_health("payment-bad-deploy", path=p)
    assert h["status"] == store.RB_NEEDS_REVIEW
    assert h["verify_failed_rate"] == 0.5


def test_a_failed_rollback_counts_at_any_sample_size(monkeypatch, tmp_path):
    """Not a rate. The escape hatch was tried once and did not work."""
    p = _cfg(monkeypatch, tmp_path)
    _feedback(p, "payment-bad-deploy", ["rollback_failed"])
    assert store.rb_health("payment-bad-deploy", path=p)["status"] == store.RB_SUSPENDED


def test_the_report_and_the_gate_read_the_same_verdict(monkeypatch, tmp_path):
    """The whole reason `_rb_verdict` exists: a page saying a procedure is fine
    while the governance plane treats it as suspended is worse than either."""
    p = _cfg(monkeypatch, tmp_path)
    _feedback(p, "payment-bad-deploy", ["ok", "verify_failed", "verify_failed", "ok"])
    (row,) = store.rb_feedback_health_report(path=p)
    assert (
        row["status"] == governance.runbook_health_verdict("payment-bad-deploy", path=p)["status"]
    )


# ---- what the gate does with it --------------------------------------------

_GOOD_CALIB = {"labeled": 100, "overconfidence": 0.0}


def _spec(**kw):
    return ActionSpec(
        name="k8s.rollout_undo",
        description="undo",
        reversible=kw.get("reversible", True),
        requires_approval=kw.get("requires_approval", False),
    )


def test_a_suspended_runbook_escalates_regardless_of_confidence(monkeypatch, tmp_path):
    """A reversible action is allowed *because* it can be undone. This one's
    record says the undo did not work, so the premise is gone."""
    _cfg(monkeypatch, tmp_path)
    monkeypatch.setattr(governance.settings, "governance_min_human_labeled_runs", 0)
    d = governance.decide(
        _spec(),
        0.95,
        _GOOD_CALIB,
        rb={"proven_good": False, "status": store.RB_SUSPENDED, "note": "its undo did not work"},
    )
    assert d.autonomy is governance.Autonomy.ESCALATE
    assert "its undo did not work" in d.reason


def test_a_decayed_runbook_cannot_reach_auto(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path)
    monkeypatch.setattr(governance.settings, "governance_min_human_labeled_runs", 0)
    rb = {"proven_good": False, "status": store.RB_NEEDS_REVIEW, "note": "verify_failed 50% (2/4)"}
    d = governance.decide(_spec(), 0.95, _GOOD_CALIB, rb=rb)
    assert d.autonomy is governance.Autonomy.PROPOSE
    assert d.rb_note == "verify_failed 50% (2/4)"


def test_an_unevaluated_runbook_changes_nothing(monkeypatch, tmp_path):
    """Passing no verdict has to behave exactly as before this gate existed."""
    _cfg(monkeypatch, tmp_path)
    monkeypatch.setattr(governance.settings, "governance_min_human_labeled_runs", 0)
    before = governance.decide(_spec(), 0.95, _GOOD_CALIB)
    assert before.rb_note == "runbook health not evaluated"


def test_a_runbook_with_no_record_has_not_earned_autonomy(monkeypatch, tmp_path):
    """Harsh on a freshly written procedure, and the same position taken
    everywhere else here: autonomy is earned against a record."""
    p = _cfg(monkeypatch, tmp_path)
    v = governance.runbook_health_verdict("brand-new", path=p)
    assert v["proven_good"] is False


# ---- what the model is told -------------------------------------------------


def test_track_record_is_silent_for_an_unrun_runbook(monkeypatch, tmp_path):
    """'0 executions' reads as a warning about the runbook when it is a
    statement about us."""
    _cfg(monkeypatch, tmp_path)
    assert agent._runbook_track_record("never-run") == ""


def test_track_record_warns_when_the_procedure_keeps_failing(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    _feedback(p, "payment-bad-deploy", ["ok", "verify_failed", "verify_failed", "verify_failed"])
    text = agent._runbook_track_record("payment-bad-deploy")
    assert "the symptom survived the fix" in text
    assert "hypothesis, not as the answer" in text


def test_track_record_states_a_clean_run_plainly(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    _feedback(p, "payment-bad-deploy", ["ok", "ok", "ok"])
    text = agent._runbook_track_record("payment-bad-deploy")
    assert "3/3 verified clean" in text
    assert "hypothesis" not in text
