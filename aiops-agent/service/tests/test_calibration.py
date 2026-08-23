"""Unit tests for the CE harness. The calibration math is pure and
deterministic, so it's pinned exactly; the store and verdict sources get
round-trip / behavioral tests."""

from types import SimpleNamespace as NS

import pytest

import app.calibration as cal
from app.calibration import (
    CalibrationRecord,
    compute_calibration,
    grade_against_truth,
    label_run,
    load_records,
    record_run,
    score_to_correct,
)


def _rec(conf, correct):
    return CalibrationRecord(run_id="r", ts="t", confidence=conf, correct=correct)


# ---- calibration math ------------------------------------------------------


def test_perfect_calibration_is_zero_ece():
    # Two bins, each acc == conf → ECE/MCE = 0.
    recs = (
        [_rec(0.1, False)] * 9
        + [_rec(0.1, True)] * 1  # bin [0.1,0.2): acc 0.1
        + [_rec(0.9, True)] * 9
        + [_rec(0.9, False)] * 1  # bin [0.9,1.0): acc 0.9
    )
    c = compute_calibration(recs, n_bins=10)
    assert c["labeled"] == 20
    assert c["ece"] == 0.0
    assert c["mce"] == 0.0


def test_overconfident_run():
    # All predictions say 0.9 but only half are correct → gap 0.4 in one bin.
    recs = [_rec(0.9, True)] * 5 + [_rec(0.9, False)] * 5
    c = compute_calibration(recs, n_bins=10)
    assert c["ece"] == pytest.approx(0.4)
    assert c["mce"] == pytest.approx(0.4)
    assert c["overconfidence"] == pytest.approx(0.4)  # conf 0.9 - acc 0.5
    # Brier: (0.9-1)^2*5 + (0.9-0)^2*5 = (0.01*5 + 0.81*5)/10 = 0.41
    assert c["brier"] == pytest.approx(0.41)


def test_confidence_1_lands_in_last_bin_not_overflow():
    c = compute_calibration([_rec(1.0, True), _rec(1.0, False)], n_bins=10)
    last = c["bins"][-1]
    assert last["count"] == 2
    assert last["accuracy"] == pytest.approx(0.5)


def test_unlabeled_records_excluded():
    recs = [_rec(0.8, True), _rec(0.8, None), _rec(0.8, None)]
    c = compute_calibration(recs)
    assert c["count"] == 3 and c["labeled"] == 1


def test_empty():
    c = compute_calibration([])
    assert c["labeled"] == 0 and c["ece"] is None


# ---- grading mode ----------------------------------------------------------


def _rec_mode(conf, correct, mode):
    return CalibrationRecord(
        run_id="r", ts="t", confidence=conf, correct=correct, grading_mode=mode
    )


def test_mode_filter_excludes_other_modes_and_unknowns():
    recs = [
        _rec_mode(0.9, True, "culprit"),
        _rec_mode(0.9, True, "inconclusive"),
        _rec_mode(0.9, True, None),
    ]
    assert compute_calibration(recs)["labeled"] == 3  # no filter → everything
    assert compute_calibration(recs, modes=("culprit",))["labeled"] == 1
    # A row that never said which question it answers is not eligible.
    assert compute_calibration(recs, modes=("culprit", "inconclusive"))["labeled"] == 2


def test_mixing_modes_cancels_opposite_errors():
    """The Day39 finding, pinned: one mode's underconfidence hides the other's
    overconfidence when they are averaged into a single number."""
    culprit = [_rec_mode(0.6, True, "culprit")] * 10  # says 0.6, always right
    inconclusive = [_rec_mode(0.4, False, "inconclusive")] * 10  # says 0.4, never right

    mixed = compute_calibration(culprit + inconclusive)
    assert mixed["overconfidence"] == 0.0  # looks perfectly calibrated

    only_culprit = compute_calibration(culprit + inconclusive, modes=("culprit",))
    assert only_culprit["overconfidence"] == -0.4  # underconfident, and visible


def test_hedging_rate_reports_inconclusive_rows_only():
    recs = [
        _rec_mode(0.1, True, "inconclusive"),
        _rec_mode(0.7, False, "inconclusive"),
        _rec_mode(0.9, True, "culprit"),
        _rec_mode(0.5, None, "inconclusive"),  # unlabeled
    ]
    h = cal.hedging_rate(recs)
    assert h["labeled"] == 2 and h["hedged"] == 1 and h["rate"] == 0.5
    assert h["mean_confidence"] == 0.4


def test_hedging_rate_empty():
    assert cal.hedging_rate([])["rate"] is None


# ---- verdict sources -------------------------------------------------------


def test_score_to_correct_threshold(monkeypatch):
    monkeypatch.setattr(cal.settings, "calibration_correct_threshold", 0.7)
    assert score_to_correct(0.7) is True
    assert score_to_correct(0.69) is False
    assert score_to_correct(0.5, threshold=0.4) is True


def test_grade_against_truth_service_and_version():
    f = NS(services=["payment-service"], suspected_version="v2.5.0", summary="decline spike")
    assert grade_against_truth(f, {"service": "payment-service", "version": "v2.5.0"}) is True
    assert grade_against_truth(f, {"service": "order-service"}) is False
    assert grade_against_truth(f, {"service": "payment-service", "version": "v2.4.1"}) is False


def test_grade_against_truth_service_in_summary_fallback():
    # service only stated in prose, not the structured field
    f = NS(services=[], suspected_version=None, summary="payment-service is failing")
    assert grade_against_truth(f, {"service": "payment-service"}) is True


# ---- store round-trip ------------------------------------------------------


def test_record_and_label_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "aiops.db"
    monkeypatch.setattr(cal.settings, "store_path", str(p))
    monkeypatch.setattr(cal.settings, "calibration_enabled", True)

    findings = NS(
        confidence=0.82,
        summary="s",
        hypothesis="h",
        suspected_version="v2.5.0",
        services=["payment-service"],
    )
    rec = record_run(findings, run_id="fp-abc")
    assert rec is not None
    loaded = load_records(p)
    assert len(loaded) == 1 and loaded[0].correct is None and loaded[0].confidence == 0.82

    assert label_run("fp-abc", correct=True, score=0.9, source="grader", path=p) is True
    relabeled = load_records(p)
    assert relabeled[0].correct is True and relabeled[0].score == 0.9
    assert label_run("missing", correct=True, path=p) is False


def test_label_updates_latest_record_for_run_id(tmp_path, monkeypatch):
    # Two pending records share a run_id; label must hit the newest (atomic UPDATE).
    p = tmp_path / "aiops.db"
    monkeypatch.setattr(cal.settings, "store_path", str(p))
    monkeypatch.setattr(cal.settings, "calibration_enabled", True)
    record_run(NS(confidence=0.4, summary="old"), run_id="fp")
    record_run(NS(confidence=0.9, summary="new"), run_id="fp")
    assert label_run("fp", correct=True, path=p) is True
    recs = load_records(p)
    assert len(recs) == 2
    newest = recs[-1]
    assert newest.confidence == 0.9 and newest.correct is True
    assert recs[0].correct is None  # the older one stays unlabeled


def test_record_run_disabled_is_noop(tmp_path, monkeypatch):
    p = tmp_path / "aiops.db"
    monkeypatch.setattr(cal.settings, "store_path", str(p))
    monkeypatch.setattr(cal.settings, "calibration_enabled", False)
    assert record_run(NS(confidence=0.5), run_id="x") is None
    assert not p.exists()


# ---- rehearsals are not evidence about live incidents ------------------------


def _rec_drill(conf, correct, drill, mode="culprit"):
    return CalibrationRecord(
        run_id=f"r{conf}{correct}{drill}",
        ts="2026-08-23T00:00:00Z",
        confidence=conf,
        correct=correct,
        grading_mode=mode,
        drill=drill,
    )


def test_production_records_drops_drills():
    recs = [_rec_drill(0.9, True, True), _rec_drill(0.9, False, False)]
    assert [r.drill for r in cal.production_records(recs)] == [False]


def test_a_replayed_drill_cannot_lift_the_curve():
    """Six replays of one rehearsal, all right at 0.95, is one piece of evidence
    recorded six times — the exact shape that would have opened the decision
    band on its own."""
    drills = [_rec_drill(0.95, True, True) for _ in range(6)]
    real = [_rec_drill(0.95, False, False)]
    everything = compute_calibration(drills + real, modes=("culprit",))
    production = compute_calibration(cal.production_records(drills + real), modes=("culprit",))
    assert everything["labeled"] == 7
    assert production["labeled"] == 1
    # And the sign of the verdict flips with it.
    assert everything["overconfidence"] < production["overconfidence"]


def test_drill_flag_round_trips_through_the_store(tmp_path, monkeypatch):
    p = tmp_path / "aiops.db"
    monkeypatch.setattr(cal.store.settings, "store_path", str(p))
    for run_id, drill in (("real", False), ("rehearsal", True)):
        cal.store.cal_insert(
            run_id=run_id,
            ts="t",
            confidence=0.9,
            summary="s",
            hypothesis="h",
            suspected_version=None,
            services=[],
            grading_mode="culprit",
            drill=drill,
            path=p,
        )
        cal.store.cal_label(run_id, correct=True, score=1.0, source="human", path=p)
    assert {r.run_id: r.drill for r in load_records(p)} == {
        "real": False,
        "rehearsal": True,
    }
    # The gate's human-label floor is counted over the same rows as the curve.
    assert cal.store.cal_count_by_source(modes=("culprit",), path=p) == 2
    assert cal.store.cal_count_by_source(modes=("culprit",), exclude_drills=True, path=p) == 1
