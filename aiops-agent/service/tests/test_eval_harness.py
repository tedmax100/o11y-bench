"""Unit tests for the eval harness pure functions — no live stack, no agent.

These pin the grading (positive + negative/inconclusive), aggregation,
regression diff, baseline round-trip, and `startsAt: now` resolution.
"""

from types import SimpleNamespace as NS

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
