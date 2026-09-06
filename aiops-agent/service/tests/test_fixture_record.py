"""The committed record itself: what the autonomy gate is allowed to read.

The gate first shipped reading `app/eval/eval.db`, which is gitignored — it
reached the image only because `COPY app /app/app` picked up whatever was on the
machine that ran the build. The verdict was a function of whose laptop built it,
and on CI there was no record at all.
"""

import json

from app.calibration import CalibrationRecord
from app.eval.record import FIELDS, append, load


def _rec(run_id, *, correct=True, conf=0.9, source="eval-harness"):
    return CalibrationRecord(
        run_id=run_id,
        ts="2026-08-20T00:00:00Z",
        confidence=conf,
        correct=correct,
        source=source,
        grading_mode="culprit",
    )


def test_a_round_trip_keeps_what_the_curve_is_computed_from(tmp_path):
    p = tmp_path / "r.jsonl"
    append([_rec("eval-a-seed0-1")], p)
    (back,) = load(p)
    assert (back.run_id, back.confidence, back.correct) == ("eval-a-seed0-1", 0.9, True)
    assert back.grading_mode == "culprit" and back.source == "eval-harness"


def test_a_rerun_does_not_duplicate_labels_already_in_the_record(tmp_path):
    """The harness writes its whole store every time; only new runs are new
    evidence. Without this a suite re-run would inflate the label count without
    grading anything."""
    p = tmp_path / "r.jsonl"
    rows = [_rec("eval-a-seed0-1"), _rec("eval-a-seed1-1")]
    assert append(rows, p) == 2
    assert append(rows, p) == 0
    assert append([*rows, _rec("eval-b-seed0-2")], p) == 1
    assert len(load(p)) == 3


def test_unlabeled_runs_stay_out(tmp_path):
    """A run nobody graded is not evidence."""
    p = tmp_path / "r.jsonl"
    pending = CalibrationRecord(
        run_id="eval-c-seed0-3", ts="2026-08-20T00:00:00Z", confidence=0.9, correct=None
    )
    assert append([pending], p) == 0
    assert load(p) == []


def test_the_prose_is_left_out_so_the_diff_stays_readable(tmp_path):
    p = tmp_path / "r.jsonl"
    append([_rec("eval-a-seed0-1")], p)
    written = json.loads(p.read_text().splitlines()[0])
    assert set(written) == set(FIELDS)
    assert "summary" not in written and "hypothesis" not in written


def test_one_corrupt_line_does_not_erase_the_record(tmp_path):
    """A record that reads as empty is "no evidence", which withholds autonomy —
    the right direction, but not for the reason a truncated write implies."""
    p = tmp_path / "r.jsonl"
    append([_rec("eval-a-seed0-1"), _rec("eval-a-seed1-1")], p)
    with p.open("a") as fh:
        fh.write("{not json\n")
    assert len(load(p)) == 2


def test_a_missing_record_reads_as_empty_not_an_error(tmp_path):
    assert load(tmp_path / "nope.jsonl") == []


def test_the_shipped_record_is_present_and_readable():
    """The whole point: the file the gate reads is in the repo, not on a laptop."""
    from app.config import settings

    records = load(settings.fixture_record_path)
    assert records, "the committed fixture record is missing or empty"
    assert all(r.correct is not None for r in records)
