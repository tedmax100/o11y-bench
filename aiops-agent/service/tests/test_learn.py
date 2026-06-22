"""Unit tests for the 7b-5 Learn 閉環: verify outcome → CE label + governance
human-label gate. Three concerns:
  1. _learn_outcome writes labels only when learn_remediation_into_ce=True.
  2. verify-failure labels as correct=False; verify-pass labels as correct=True.
  3. governance._calibration_verdict withholds AUTO when human_labeled < min,
     even if total labeled >= min (self-produced labels cannot unlock AUTO).
"""

import app.execution as ex
import app.governance as gov
import app.store as store
from app.calibration import label_run, load_records
from app.governance import Autonomy, decide


# ---- helpers ----------------------------------------------------------------

def _plant(run_id: str, *, source: str, correct: bool = True, path=None) -> None:
    """Insert + label a CE record in one call."""
    store.cal_insert(run_id=run_id, ts="2026-01-01T00:00:00Z",
                     confidence=0.9, summary="s", hypothesis="",
                     suspected_version=None, services=[], path=path)
    store.cal_label(run_id, correct, score=None, source=source, path=path)


def _req(fp: str):
    from app.action_requests import ActionRequest
    return ActionRequest(
        request_id="r-" + fp, fp=fp, action="k8s.rollout_undo", args={},
        autonomy="propose", status="executing", reversible=True,
        idem_key="k", created_ts="2026-01-01T00:00:00Z",
        expires_ts="2026-01-01T01:00:00Z",
    )


def _calib(labeled=50, overconfidence=0.0):
    return {"labeled": labeled, "overconfidence": overconfidence,
            "ece": overconfidence, "brier": 0.1, "bins": []}


# ---- _learn_outcome ---------------------------------------------------------

def test_learn_disabled_writes_nothing(tmp_path, monkeypatch):
    p = tmp_path / "l.db"
    monkeypatch.setattr(ex.settings, "learn_remediation_into_ce", False)
    store.cal_insert(run_id="fp1", ts="2026-01-01T00:00:00Z",
                     confidence=0.9, summary="s", hypothesis="",
                     suspected_version=None, services=[], path=p)

    ex._learn_outcome(_req("fp1"), verified=True, path=p)

    recs = load_records(path=p)
    assert all(r.source != "remediation-verified" for r in recs)


def test_learn_enabled_verified_labels_correct_true(tmp_path, monkeypatch):
    p = tmp_path / "l.db"
    monkeypatch.setattr(ex.settings, "learn_remediation_into_ce", True)
    store.cal_insert(run_id="fp2", ts="2026-01-01T00:00:00Z",
                     confidence=0.9, summary="s", hypothesis="",
                     suspected_version=None, services=[], path=p)

    ex._learn_outcome(_req("fp2"), verified=True, path=p)

    recs = load_records(path=p)
    labeled = [r for r in recs if r.source == "remediation-verified"]
    assert len(labeled) == 1 and labeled[0].correct is True


def test_learn_enabled_verify_failure_labels_correct_false(tmp_path, monkeypatch):
    p = tmp_path / "l.db"
    monkeypatch.setattr(ex.settings, "learn_remediation_into_ce", True)
    store.cal_insert(run_id="fp3", ts="2026-01-01T00:00:00Z",
                     confidence=0.9, summary="s", hypothesis="",
                     suspected_version=None, services=[], path=p)

    ex._learn_outcome(_req("fp3"), verified=False, path=p)

    recs = load_records(path=p)
    labeled = [r for r in recs if r.source == "remediation-failed"]
    assert len(labeled) == 1 and labeled[0].correct is False


def test_learn_missing_fp_is_noop(tmp_path, monkeypatch):
    """No CE record for the fp → label_run is a no-op; no crash."""
    p = tmp_path / "l.db"
    monkeypatch.setattr(ex.settings, "learn_remediation_into_ce", True)
    ex._learn_outcome(_req("ghost-fp"), verified=True, path=p)
    assert load_records(path=p) == []


# ---- store.cal_count_by_source ----------------------------------------------

def test_cal_count_by_source_excludes_self_labels(tmp_path):
    p = tmp_path / "c.db"
    for i in range(5):
        _plant(f"u{i}", source="ui", path=p)
    for i in range(3):
        _plant(f"rv{i}", source="remediation-verified", path=p)

    total = store.cal_count_by_source(path=p)
    human_only = store.cal_count_by_source(
        exclude_sources=("remediation-verified", "remediation-failed"), path=p
    )
    assert total == 8
    assert human_only == 5


def test_cal_count_by_source_unlabeled_not_counted(tmp_path):
    p = tmp_path / "c.db"
    # insert without labeling
    store.cal_insert(run_id="unlabeled", ts="2026-01-01T00:00:00Z",
                     confidence=0.9, summary="s", hypothesis="",
                     suspected_version=None, services=[], path=p)
    _plant("labeled", source="ui", path=p)
    assert store.cal_count_by_source(path=p) == 1


# ---- governance human-label gate --------------------------------------------

def test_human_label_gate_withholds_auto_when_self_labels_only(tmp_path, monkeypatch):
    """50 self-labels → AUTO still withheld (human-label gate not cleared)."""
    p = tmp_path / "g.db"
    monkeypatch.setattr(gov.settings, "governance_min_labeled_runs", 20)
    monkeypatch.setattr(gov.settings, "governance_min_human_labeled_runs", 20)

    for i in range(50):
        _plant(f"r{i}", source="remediation-verified", path=p)

    from app.actions import registry
    spec = registry.get("k8s.rollout_undo")
    d = decide(spec, 0.9, _calib(labeled=50, overconfidence=0.0), path=p)
    assert d.autonomy is Autonomy.PROPOSE
    assert "self-produced labels cannot unlock AUTO" in d.calibration_note


def test_human_label_gate_allows_auto_with_enough_human_labels(tmp_path, monkeypatch):
    """20 human labels + 10 self-labels → AUTO (overconfidence ok, no approval gate)."""
    p = tmp_path / "g2.db"
    monkeypatch.setattr(gov.settings, "governance_min_labeled_runs", 20)
    monkeypatch.setattr(gov.settings, "governance_min_human_labeled_runs", 20)

    for i in range(20):
        _plant(f"h{i}", source="ui", path=p)
    for i in range(10):
        _plant(f"s{i}", source="remediation-verified", path=p)

    # k8s.rollout_undo requires_approval=True → PROPOSE anyway; use a non-approval spec
    from app.actions import ActionSpec
    spec = ActionSpec(name="test.action", description="t",
                      reversible=True, requires_approval=False)
    d = decide(spec, 0.9, _calib(labeled=30, overconfidence=0.0), path=p)
    assert d.autonomy is Autonomy.AUTO


def test_human_label_gate_skipped_when_min_is_zero(monkeypatch):
    """governance_min_human_labeled_runs=0 → no store query, gate bypassed."""
    monkeypatch.setattr(gov.settings, "governance_min_human_labeled_runs", 0)
    monkeypatch.setattr(gov.settings, "governance_min_labeled_runs", 0)

    from app.actions import ActionSpec
    spec = ActionSpec(name="test.action", description="t",
                      reversible=True, requires_approval=False)
    d = decide(spec, 0.9, _calib(labeled=50, overconfidence=0.0))
    assert d.autonomy is Autonomy.AUTO
