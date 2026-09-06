"""Labeling a run that blamed nobody (day36).

Two bugs, one shape: the Todo queue listed chat runs as work waiting on a
person, and there was no calibration row to put the verdict in; and the only
label path in the product hardcoded culprit mode, which is the ruler that scores
a 0.0-confidence refusal as a full 1.0 of calibration gap.
"""

from app import store
from app.calibration import (
    CULPRIT,
    INCONCLUSIVE,
    default_grading_mode,
    label_run,
    load_records,
)
from app.investigations import record_investigation


class _F:
    def __init__(self, conf, version=None, services=()):
        self.confidence = conf
        self.summary = "s"
        self.hypothesis = "h"
        self.suspected_version = version
        self.services = list(services)


def _chat(path, fp, conf, version=None, services=()):
    record_investigation(
        fp,
        {"labels": {"service_name": None}, "annotations": {}},
        {
            "answer": "a",
            "findings": _F(conf, version, services),
            "decisions": [],
            "sufficiency": {},
        },
        path=path,
        source="chat",
    )


def test_label_backfills_a_row_for_a_run_that_never_had_one(tmp_path):
    p = tmp_path / "s.db"
    store.init(p)
    _chat(p, "fb490a75", 0.9)
    assert not load_records(p)  # a chat run writes no calibration row

    assert label_run("fb490a75", correct=True, grading_mode=INCONCLUSIVE, path=p) is True
    rec = load_records(p)
    assert len(rec) == 1
    assert rec[0].correct is True and rec[0].grading_mode == INCONCLUSIVE
    assert rec[0].confidence == 0.9


def test_backfill_needs_something_to_backfill_from(tmp_path):
    p = tmp_path / "s.db"
    store.init(p)
    assert label_run("nothing-here", correct=True, path=p) is False


def test_default_mode_reads_the_run_not_the_button(tmp_path):
    p = tmp_path / "s.db"
    store.init(p)
    _chat(p, "blamed-nobody", 0.0)
    _chat(p, "blamed-payment", 0.95, version="v2.5.0", services=["payment-service"])
    assert default_grading_mode("blamed-nobody", p) == INCONCLUSIVE
    assert default_grading_mode("blamed-payment", p) == CULPRIT


def test_unknown_run_defaults_to_culprit(tmp_path):
    """The stricter pool is the safe default for something we know nothing about."""
    p = tmp_path / "s.db"
    store.init(p)
    assert default_grading_mode("who?", p) == CULPRIT


def test_inconclusive_labels_stay_out_of_the_culprit_count(tmp_path):
    """The gate reads culprit only, so a refusal graded honestly cannot move it."""
    p = tmp_path / "s.db"
    store.init(p)
    _chat(p, "refusal", 0.0)
    label_run("refusal", correct=True, grading_mode=INCONCLUSIVE, path=p)
    assert store.cal_count_by_source(modes=(CULPRIT,), path=p) == 0
    assert store.cal_count_by_source(modes=(INCONCLUSIVE,), path=p) == 1
