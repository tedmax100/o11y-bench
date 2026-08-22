"""The fifth gate: the fixture record, counted apart from the production curve.

Day41+: production had 7 labels and the harness had 94, in a different database
that governance never read. Merging them would have cleared two gates by letting
grader labels on a baked stack vouch for a write to a live cluster — and the
clock probe had already measured what that vouching is worth (one fixture went
100% to 0% between two boots on untouched code). So they are counted separately
against the same bar, and AUTO requires both.
"""

from datetime import UTC, datetime, timedelta

import pytest

import app.governance as gov
from app.actions import ActionSpec
from app.calibration import CalibrationRecord, compute_calibration
from app.governance import Autonomy, decide, regression_verdict
from app.store import cal_insert, cal_label


def _spec(name="k8s.test"):
    return ActionSpec(name=name, description="d", reversible=True, requires_approval=False)


def _curve(*groups):
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


def _good_curve():
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
    monkeypatch.setattr(gov.settings, "governance_min_human_labeled_runs", 0)
    monkeypatch.setattr(gov.settings, "governance_regression_gate_enabled", True)
    monkeypatch.setattr(gov.settings, "governance_fixture_max_age_days", 14)
    yield


def _eval_store(tmp_path, *groups, source="eval-harness", age_days=1):
    """A real eval store written through the same calls the harness uses."""
    p = tmp_path / "eval.db"
    ts = (datetime.now(UTC) - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    i = 0
    for conf, n_ok, n_bad in groups:
        for correct in [True] * n_ok + [False] * n_bad:
            run_id = f"f{i}"
            i += 1
            cal_insert(
                run_id=run_id,
                ts=ts,
                confidence=conf,
                summary="s",
                hypothesis="h",
                suspected_version=None,
                services=["order-service"],
                grading_mode="culprit",
                path=p,
            )
            cal_label(
                run_id=run_id,
                correct=correct,
                score=None,
                source=source,
                grading_mode="culprit",
                path=p,
            )
    return p


# ---- the verdict itself ----------------------------------------------------


def test_a_clean_fixture_record_is_proven_good(tmp_path):
    p = _eval_store(tmp_path, (0.9, 9, 1), (0.6, 6, 4), (0.3, 3, 7))
    v = regression_verdict(path=p)
    assert v["proven_good"], v["note"]


def test_a_regressed_fixture_record_withholds_autonomy(tmp_path):
    """20% right in the band AUTO is granted in — the shape production was
    actually measured to have."""
    p = _eval_store(tmp_path, (0.9, 2, 8), (0.6, 6, 4), (0.3, 3, 7))
    v = regression_verdict(path=p)
    assert not v["proven_good"]
    # The note names the fixture side, so an operator reading a PROPOSE reason
    # can tell which body of evidence withheld the autonomy.
    assert v["note"].startswith("fixtures: ")


def test_a_thin_fixture_record_earns_nothing(tmp_path):
    p = _eval_store(tmp_path, (0.9, 3, 0))
    assert not regression_verdict(path=p)["proven_good"]


def test_a_missing_store_is_no_record_not_a_pass(tmp_path):
    v = regression_verdict(path=tmp_path / "does-not-exist.db")
    assert not v["proven_good"]
    assert "no fixture record" in v["note"]


def test_the_gate_can_be_turned_off_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(gov.settings, "governance_regression_gate_enabled", False)
    assert regression_verdict(path=tmp_path / "nope.db")["proven_good"]


def test_self_produced_labels_cannot_carry_the_fixture_record(tmp_path, monkeypatch):
    """The harness half is the grader's word. A remediation grading its own fix
    is not, and it must not reach the floor from this side either."""
    monkeypatch.setattr(gov.settings, "governance_min_human_labeled_runs", 20)
    p = _eval_store(tmp_path, (0.9, 9, 1), (0.6, 6, 4), (0.3, 3, 7), source="remediation-verified")
    assert not regression_verdict(path=p)["proven_good"]


# ---- the gate inside decide() ---------------------------------------------


def test_auto_requires_the_fixture_record_too(tmp_path):
    """Production calibration clean, fixtures regressed → PROPOSE. This is the
    case the merged accounting would have gotten wrong in the other direction."""
    bad = regression_verdict(path=_eval_store(tmp_path, (0.9, 2, 8), (0.6, 6, 4), (0.3, 3, 7)))
    d = decide(_spec(), 0.9, _good_curve(), None, None, None, None, bad)
    assert d.autonomy is Autonomy.PROPOSE
    assert "fixtures" in d.reason


def test_a_clean_fixture_record_does_not_rescue_bad_production_calibration(tmp_path):
    """The other direction, which is the whole point of counting them apart:
    94 grader labels must not vouch for a write to a live cluster."""
    good = regression_verdict(path=_eval_store(tmp_path, (0.9, 9, 1), (0.6, 6, 4), (0.3, 3, 7)))
    assert good["proven_good"]
    d = decide(
        _spec(), 0.9, _curve((0.9, 1, 4), (0.6, 6, 4), (0.3, 3, 7)), None, None, None, None, good
    )
    assert d.autonomy is Autonomy.PROPOSE
    assert "calibration" in d.reason


def test_both_clean_reaches_auto(tmp_path):
    good = regression_verdict(path=_eval_store(tmp_path, (0.9, 9, 1), (0.6, 6, 4), (0.3, 3, 7)))
    d = decide(_spec(), 0.9, _good_curve(), None, None, None, None, good)
    assert d.autonomy is Autonomy.AUTO
    assert "fixture record" in d.reason


def test_an_unevaluated_fixture_record_is_not_a_veto(tmp_path):
    """`None` means "nobody asked", same as the other four verdicts — callers
    that do not wire it keep their existing behaviour."""
    d = decide(_spec(), 0.9, _good_curve(), None, None, None, None, None)
    assert d.autonomy is Autonomy.AUTO
    assert d.ev_note == "fixture record not evaluated"


# ---- freshness: a regression gate reading old labels is not a regression gate


def test_labels_older_than_the_window_stop_counting(tmp_path):
    """The shipped version pooled seven weeks into one number, and the pool was
    flattering: +0.19 over everything, +0.42 over the last fortnight. Runs that
    predate most of the agent's code were voting it well calibrated."""
    p = _eval_store(tmp_path, (0.9, 9, 1), (0.6, 6, 4), (0.3, 3, 7), age_days=40)
    v = regression_verdict(path=p)
    assert not v["proven_good"]
    assert "older code" in v["note"]
    assert v["newest_age_days"] == 40


def test_a_stale_record_cannot_be_rescued_by_its_own_size(tmp_path):
    """Many old labels are still old labels — volume is not freshness."""
    p = _eval_store(tmp_path, (0.9, 90, 10), age_days=90)
    assert not regression_verdict(path=p)["proven_good"]


def test_only_the_labels_inside_the_window_shape_the_curve(tmp_path, monkeypatch):
    """A clean recent record must not be dragged under by an old bad one, and
    the reverse — which is the direction that actually happened."""
    p = _eval_store(tmp_path, (0.9, 9, 1), (0.6, 6, 4), (0.3, 3, 7), age_days=1)
    v_fresh = regression_verdict(path=p)
    assert v_fresh["proven_good"], v_fresh["note"]
    # Now add a wildly overconfident batch from outside the window.
    _add = _eval_store(tmp_path, (0.9, 0, 30), age_days=60)
    assert _add == p  # same file
    v = regression_verdict(path=p)
    assert v["proven_good"], f"out-of-window labels leaked into the curve: {v['note']}"


def test_the_note_says_how_old_the_evidence_is(tmp_path):
    """Same courtesy the actuation verdict pays ("checked 11s ago") — a gate
    that refuses should say what it read."""
    p = _eval_store(tmp_path, (0.9, 9, 1), (0.6, 6, 4), (0.3, 3, 7), age_days=2)
    assert "newest label 2d ago" in regression_verdict(path=p)["note"]
