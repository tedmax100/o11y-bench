"""Unit tests for the eval harness pure functions — no live stack, no agent.

These pin the grading (positive + negative/inconclusive), aggregation,
regression diff, baseline round-trip, and `startsAt: now` resolution.
"""

from pathlib import Path
from types import SimpleNamespace as NS

import app.eval.harness as harness
from app.eval.harness import (
    DEFAULT_FIXTURES,
    Fixture,
    RunResult,
    grade_run,
    load_baseline,
    load_fixtures,
    regression_diff,
    save_baseline,
    summarize,
)


def _findings(*, confidence=0.7, services=None, version=None, summary=""):
    return NS(
        confidence=confidence,
        services=services or [],
        suspected_version=version,
        summary=summary,
    )


def _fixture(**kw):
    base = {"id": "f", "alert": {"labels": {}}}
    base.update(kw)
    return Fixture.model_validate(base)


# ---- resolved_alert ---------------------------------------------------------


def test_resolved_alert_replaces_now_and_does_not_mutate_original():
    fx = _fixture(alert={"labels": {"service_name": "payment-service"}, "startsAt": "now"})
    resolved = fx.resolved_alert()
    assert resolved["startsAt"] != "now"
    assert resolved["startsAt"].endswith("Z") and "T" in resolved["startsAt"]
    # original fixture alert is untouched (deep copy)
    assert fx.alert["startsAt"] == "now"


def test_resolved_alert_keeps_explicit_timestamp():
    fx = _fixture(alert={"startsAt": "2026-04-04T10:05:00Z"})
    assert fx.resolved_alert()["startsAt"] == "2026-04-04T10:05:00Z"


def test_resolved_alert_pins_now_to_scenario_time():
    fx = _fixture(alert={"startsAt": "now"})
    assert fx.resolved_alert("2026-04-04T10:05:00Z")["startsAt"] == "2026-04-04T10:05:00Z"


# ---- grade_run: culprit (positive) mode -------------------------------------


def test_grade_culprit_service_and_version_correct():
    fx = _fixture(truth={"service": "payment-service", "version": "v2.5.0"})
    correct, svc, ver = grade_run(_findings(services=["payment-service"], version="v2.5.0"), fx)
    assert (correct, svc, ver) == (True, True, True)


def test_grade_culprit_version_miss_is_incorrect_but_service_still_hits():
    fx = _fixture(truth={"service": "payment-service", "version": "v2.5.0"})
    correct, svc, ver = grade_run(_findings(services=["payment-service"], version="v2.4.0"), fx)
    assert correct is False
    assert svc is True
    assert ver is False


def test_grade_culprit_service_only_truth_leaves_version_none():
    fx = _fixture(truth={"service": "payment-service"})
    correct, _svc, ver = grade_run(_findings(services=["payment-service"]), fx)
    assert correct is True
    assert ver is None


def test_grade_culprit_service_named_in_summary_counts():
    fx = _fixture(truth={"service": "payment-service"})
    correct, svc, _ = grade_run(
        _findings(services=[], summary="root cause is payment-service v2.5.0"), fx
    )
    assert correct is True and svc is True


# ---- grade_run: inconclusive (negative) mode --------------------------------


def test_grade_inconclusive_low_confidence_is_correct():
    fx = _fixture(expect="inconclusive", max_confidence=0.6, forbid_services=["payment-service"])
    correct, svc, ver = grade_run(_findings(confidence=0.3, services=[]), fx)
    assert (correct, svc, ver) == (True, True, None)


def test_grade_inconclusive_overconfident_is_incorrect():
    fx = _fixture(expect="inconclusive", max_confidence=0.6)
    correct, _, _ = grade_run(_findings(confidence=0.85), fx)
    assert correct is False


def test_grade_inconclusive_blaming_forbidden_service_is_incorrect_even_if_hedged():
    fx = _fixture(expect="inconclusive", max_confidence=0.6, forbid_services=["payment-service"])
    correct, _, _ = grade_run(_findings(confidence=0.4, services=["payment-service"]), fx)
    assert correct is False


# ---- summarize --------------------------------------------------------------


def _run(correct, *, svc=True, ver=None, conf=0.7, err=None):
    return RunResult("f", 0, correct, svc, ver, conf, [], None, "", error=err)


def test_summarize_rates_and_any_correct():
    runs = [_run(True, ver=True), _run(True, ver=False), _run(False, svc=False, ver=False)]
    s = summarize("f", runs)
    assert s.n == 3
    assert abs(s.correct_rate - 2 / 3) < 1e-9
    assert s.any_correct is True
    assert abs(s.service_rate - 2 / 3) < 1e-9
    assert abs(s.version_rate - 1 / 3) < 1e-9


def test_summarize_version_rate_none_when_no_version_graded():
    s = summarize("f", [_run(True, ver=None), _run(True, ver=None)])
    assert s.version_rate is None
    assert s.correct_rate == 1.0


def test_summarize_counts_errors_and_all_wrong():
    s = summarize("f", [_run(False, err="boom"), _run(False)])
    assert s.errors == 1
    assert s.any_correct is False
    assert s.correct_rate == 0.0


# ---- regression_diff + baseline ---------------------------------------------


def test_regression_diff_flags_new_regression_and_skips_unchanged():
    summaries = [
        summarize("regressed", [_run(False), _run(True)]),  # 0.5
        summarize("stable", [_run(True), _run(True)]),  # 1.0
        summarize("brand-new", [_run(True)]),  # 1.0, no baseline
    ]
    baseline = {"regressed": 1.0, "stable": 1.0}
    diff = dict((fid, (base, cur)) for fid, base, cur in regression_diff(summaries, baseline))
    assert diff["regressed"] == (1.0, 0.5)  # regression surfaced
    assert "stable" not in diff  # unchanged → omitted
    assert diff["brand-new"][0] is None  # new fixture → base None


def test_baseline_round_trip(tmp_path):
    summaries = [summarize("a", [_run(True), _run(False)]), summarize("b", [_run(True)])]
    p = tmp_path / "baseline.json"
    save_baseline(p, summaries)
    loaded = load_baseline(p)
    assert loaded == {"a": 0.5, "b": 1.0}


def test_load_baseline_missing_file_is_empty(tmp_path):
    assert load_baseline(tmp_path / "nope.json") == {}


# ---- shipped fixtures parse + carry both modes ------------------------------


def test_default_fixtures_include_positive_and_negative():
    fixtures = {f.id: f for f in load_fixtures(DEFAULT_FIXTURES)}
    assert "payment-decline-service" in fixtures
    assert fixtures["payment-decline-service"].expect == "culprit"
    neg = fixtures["user-service-no-incident"]
    assert neg.expect == "inconclusive"
    assert "payment-service" in neg.forbid_services


def _latency_fixture(**over):
    base = {
        "id": "payment-latency-false-alarm",
        "alert": {"labels": {"service_name": "payment-service", "alertname": "X"}},
        "expect": "inconclusive",
        "max_confidence": 0.6,
        "forbid_versions": ["v2.5.0"],
    }
    base.update(over)
    return Fixture.model_validate(base)


class _F:
    def __init__(self, confidence, services, suspected_version=None):
        self.confidence = confidence
        self.services = services
        self.suspected_version = suspected_version


def test_forbid_versions_catches_a_culprit_inherited_from_recall():
    """The failure `forbid_services` cannot see: the alert names payment-service
    and so does the answer, but the version came from a past case about a
    different symptom."""
    fx = _latency_fixture()
    correct, _, _ = grade_run(_F(0.5, ["payment-service"], "v2.5.0"), fx)
    assert correct is False


def test_forbid_versions_allows_an_honest_hedge():
    fx = _latency_fixture()
    correct, _, _ = grade_run(_F(0.5, ["payment-service"], None), fx)
    assert correct is True


def test_forbid_versions_is_opt_in():
    fx = _latency_fixture(forbid_versions=[])
    correct, _, _ = grade_run(_F(0.5, ["payment-service"], "v2.5.0"), fx)
    assert correct is True


def test_shipped_fixtures_include_a_clean_recall_control():
    """payment-decline-service and payment-latency-false-alarm share a service
    and differ in alertname — that is what makes one arm open book and the other
    a control."""
    fixtures = {f.id: f for f in load_fixtures(DEFAULT_FIXTURES)}
    decline = fixtures["payment-decline-service"]
    control = fixtures["payment-latency-false-alarm"]
    assert decline.alert["labels"]["service_name"] == control.alert["labels"]["service_name"]
    assert decline.alert["labels"]["alertname"] != control.alert["labels"]["alertname"]
    assert control.forbid_versions == ["v2.5.0"]


# ---- a stack with two incidents needs a clock, not just a "now" -------------


def test_relative_start_reaches_an_incident_that_is_not_now():
    """Two incidents both live at data-end are indistinguishable from any one
    alert's point of view, because every window contains both."""
    fx = Fixture(id="x", alert={"labels": {}, "annotations": {}, "startsAt": "now-6h"})
    assert fx.resolved_alert("2026-08-19T12:00:00Z")["startsAt"] == "2026-08-19T06:00:00Z"


def test_relative_start_accepts_minutes():
    fx = Fixture(id="x", alert={"labels": {}, "annotations": {}, "startsAt": "now-90m"})
    assert fx.resolved_alert("2026-08-19T12:00:00Z")["startsAt"] == "2026-08-19T10:30:00Z"


def test_an_absolute_start_is_left_alone():
    fx = Fixture(
        id="x", alert={"labels": {}, "annotations": {}, "startsAt": "2026-01-01T00:00:00Z"}
    )
    assert fx.resolved_alert("2026-08-19T12:00:00Z")["startsAt"] == "2026-01-01T00:00:00Z"


def test_the_shipped_fixtures_point_at_the_incidents_the_stack_bakes():
    """The session-cache window is bounded; a fixture left on `now` would be
    asking about a quiet period and failing as if the agent were wrong."""
    fixtures = load_fixtures(DEFAULT_FIXTURES)
    by_id = {f.id: f for f in fixtures}
    assert by_id["order-service-auth-degradation"].alert["startsAt"] == "now-6h"
    assert by_id["payment-decline-service"].alert["startsAt"] == "now"


# ---- the sampling unit ------------------------------------------------------


def _summary(fixture_id, correct_flags):
    runs = [
        RunResult(fixture_id, i, bool(c), bool(c), None, 0.7, [], None, "")
        for i, c in enumerate(correct_flags)
    ]
    return summarize(fixture_id, runs)


def test_passes_are_kept_not_averaged_away():
    """A mean of 50% built from 100% and 0% is a different situation from one
    built from 50% and 50%, and only the first says the number is unstable."""
    merged = harness.merge_passes([[_summary("f", [1])], [_summary("f", [0])]])
    (s,) = merged
    assert s.correct_rate == 0.5
    assert s.pass_rates == [1.0, 0.0]
    assert s.spread == 1.0

    steady = harness.merge_passes([[_summary("f", [1, 0])], [_summary("f", [1, 0])]])
    assert steady[0].correct_rate == 0.5
    assert steady[0].spread == 0.0


def test_a_single_pass_has_no_spread():
    """One pass cannot say how much it moves, and must not pretend to."""
    (s,) = harness.merge_passes([[_summary("f", [1, 1, 1])]])
    assert s.spread is None


def test_fixture_order_survives_the_merge():
    passes = [
        [_summary("a", [1]), _summary("b", [0])],
        [_summary("b", [1]), _summary("a", [0])],
    ]
    assert [s.fixture_id for s in harness.merge_passes(passes)] == ["a", "b"]


def test_the_report_states_the_spread_as_the_floor():
    merged = harness.merge_passes([[_summary("f", [1])], [_summary("f", [0])]])
    text = harness.format_report(merged, [], store_path=Path("x.db"))
    assert "between passes" in text
    assert "100%" in text
    assert "not a result" in text


def test_a_single_pass_report_says_nothing_about_spread():
    merged = harness.merge_passes([[_summary("f", [1])]])
    assert "between passes" not in harness.format_report(merged, [], store_path=Path("x.db"))
