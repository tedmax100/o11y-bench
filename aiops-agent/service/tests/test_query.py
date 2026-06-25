"""Unit tests for app/tools/query.py — time parsing, series summarization,
error hint injection, and the auto queryType routing. No live HTTP calls."""

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.tools import ToolException

import app.tools.query as q


# ---- _parse_dt -------------------------------------------------------------

def test_parse_dt_now():
    before = datetime.now(UTC)
    result = q._parse_dt("now")
    after = datetime.now(UTC)
    assert before <= result <= after


def test_parse_dt_relative_minutes():
    result = q._parse_dt("now-30m")
    expected = datetime.now(UTC) - timedelta(minutes=30)
    assert abs((result - expected).total_seconds()) < 2


def test_parse_dt_relative_hours():
    result = q._parse_dt("now-2h")
    expected = datetime.now(UTC) - timedelta(hours=2)
    assert abs((result - expected).total_seconds()) < 2


def test_parse_dt_relative_days():
    result = q._parse_dt("now-1d")
    expected = datetime.now(UTC) - timedelta(days=1)
    assert abs((result - expected).total_seconds()) < 2


def test_parse_dt_relative_seconds():
    result = q._parse_dt("now-45s")
    expected = datetime.now(UTC) - timedelta(seconds=45)
    assert abs((result - expected).total_seconds()) < 2


def test_parse_dt_rfc3339():
    result = q._parse_dt("2026-01-15T12:00:00Z")
    assert result == datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def test_parse_dt_rfc3339_with_offset():
    result = q._parse_dt("2026-01-15T12:00:00+00:00")
    assert result == datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def test_parse_dt_invalid_raises():
    with pytest.raises(ToolException, match="Unrecognized time"):
        q._parse_dt("yesterday")


def test_parse_dt_now_whitespace():
    result = q._parse_dt("  now  ")
    assert abs((result - datetime.now(UTC)).total_seconds()) < 2


# ---- now_override ----------------------------------------------------------

def test_now_override_pins_clock():
    fixed = datetime(2025, 6, 1, 10, 0, 0, tzinfo=UTC)
    with q.now_override(fixed):
        assert q.current_now() == fixed
        parsed = q._parse_dt("now-1h")
        assert parsed == fixed - timedelta(hours=1)

    # after context exits, real clock is back
    assert q.current_now() != fixed


def test_now_override_none_restores_real_clock():
    fixed = datetime(2025, 6, 1, 10, 0, 0, tzinfo=UTC)
    with q.now_override(fixed):
        with q.now_override(None):
            assert abs((q.current_now() - datetime.now(UTC)).total_seconds()) < 2


# ---- _is_metric_logql ------------------------------------------------------

@pytest.mark.parametrize("logql", [
    'count_over_time({service_name="payment"} [5m])',
    'sum(count_over_time({service_name="x"} [1h]))',
    'rate({service_name="y"} [5m])',
    'sum by (service_name) (count_over_time({app="z"} [10m]))',
    'topk(5, count({job="x"} [1m]))',
])
def test_is_metric_logql_true(logql):
    assert q._is_metric_logql(logql) is True


@pytest.mark.parametrize("logql", [
    '{service_name="payment"} | json',
    '{service_name="payment"} | event="payment.declined"',
    '{service_name="payment"}',
])
def test_is_metric_logql_false(logql):
    assert q._is_metric_logql(logql) is False


# ---- _round_sig ------------------------------------------------------------

def test_round_sig_basic():
    assert q._round_sig(123456.789, 4) == 123500.0


def test_round_sig_zero():
    assert q._round_sig(0) == 0.0


def test_round_sig_string_passthrough():
    assert q._round_sig("x") == "x"


def test_round_sig_nan():
    result = q._round_sig(float("nan"))
    assert math.isnan(result)


# ---- _summarize_series_result ----------------------------------------------

def _make_matrix(values: list[float]) -> dict:
    pairs = [[1000 + i * 60, str(v)] for i, v in enumerate(values)]
    return {
        "resultType": "matrix",
        "result": [{"metric": {"job": "test"}, "values": pairs}],
    }


def test_summarize_matrix_basic():
    data = _make_matrix([1.0, 2.0, 3.0, 4.0, 5.0])
    out = q._summarize_series_result(data)
    assert out["resultType"] == "matrix_summary"
    s = out["result"][0]
    assert s["last"] == 5.0
    assert s["min"] == 1.0
    assert s["max"] == 5.0
    assert s["points"] == 5


def test_summarize_matrix_no_sample_when_constant():
    data = _make_matrix([42.0] * 20)
    out = q._summarize_series_result(data)
    s = out["result"][0]
    assert "sample" not in s


def test_summarize_matrix_includes_sample_for_spike():
    values = [1.0] * 15 + [100.0] + [1.0] * 4  # big spike
    data = _make_matrix(values)
    out = q._summarize_series_result(data)
    s = out["result"][0]
    assert "sample" in s


def test_summarize_vector():
    data = {
        "resultType": "vector",
        "result": [{"metric": {"job": "x"}, "value": [1000, "3.14"]}],
    }
    out = q._summarize_series_result(data)
    assert out["resultType"] == "vector"
    assert out["result"][0]["value"] == 3.14


def test_summarize_scalar():
    data = {"resultType": "scalar", "result": [1000, "99.9"]}
    out = q._summarize_series_result(data)
    assert out["resultType"] == "scalar"
    assert out["value"] == 99.9


def test_summarize_non_dict_passthrough():
    assert q._summarize_series_result("raw string") == "raw string"
    assert q._summarize_series_result(None) is None


def test_summarize_empty_values():
    data = {
        "resultType": "matrix",
        "result": [{"metric": {}, "values": []}],
    }
    out = q._summarize_series_result(data)
    assert out["result"][0]["points"] == 0


# ---- _selector -------------------------------------------------------------

def test_selector_extracts_braces():
    assert q._selector('{service_name="payment"}') == '{service_name="payment"}'


def test_selector_extracts_first_braces_with_pipeline():
    sel = q._selector('{service_name="x"} | event="foo"')
    assert sel == '{service_name="x"}'


def test_selector_none_when_no_braces():
    assert q._selector("not a logql expression") is None


# ---- _loki_query_hint ------------------------------------------------------

def test_loki_hint_missing_selector():
    exc = ToolException("returned 400: parse error")
    result = q._loki_query_hint('event="foo"', exc)
    assert "stream selector" in str(result)
    assert "service_name" in str(result)


def test_loki_hint_with_selector_bad_pipeline():
    exc = ToolException("returned 400: unexpected token")
    result = q._loki_query_hint('{service_name="x"} | level="ERROR"', exc)
    assert "count_over_time" in str(result) or "pipeline" in str(result)


def test_loki_hint_non_parse_error_unchanged():
    exc = ToolException("network timeout")
    result = q._loki_query_hint('{service_name="x"}', exc)
    assert str(result) == str(exc)


# ---- _tempo_query_hint -----------------------------------------------------

def test_tempo_hint_injects_traceql_syntax():
    exc = ToolException("returned 400: parse error")
    result = q._tempo_query_hint('resource.service.name="payment"', exc)
    assert "braces" in str(result) or "predicates" in str(result)


def test_tempo_hint_non_parse_passthrough():
    exc = ToolException("connection refused")
    result = q._tempo_query_hint("{ status=error }", exc)
    assert str(result) == str(exc)


# ---- async query functions (httpx mocked) ----------------------------------

@pytest.mark.asyncio
async def test_query_prometheus_range(monkeypatch):
    fake_resp = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"job": "payment"},
                    "values": [[1000 + i * 60, "0.1"] for i in range(5)],
                }
            ],
        },
    }
    mock = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(q, "_get_json", mock)

    result = await q._query_prometheus(
        'rate(http_requests_total[5m])', start="now-1h", end="now"
    )
    assert result["resultType"] == "matrix_summary"
    mock.assert_awaited_once()
    _, path, params = mock.call_args[0]
    assert path == "/api/v1/query_range"


@pytest.mark.asyncio
async def test_query_prometheus_instant(monkeypatch):
    fake_resp = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1000, "42"]}]},
    }
    mock = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(q, "_get_json", mock)

    result = await q._query_prometheus("up", queryType="instant")
    _, path, _ = mock.call_args[0]
    assert path == "/api/v1/query"
    assert result["resultType"] == "vector"


@pytest.mark.asyncio
async def test_query_prometheus_error_body(monkeypatch):
    monkeypatch.setattr(
        q, "_get_json", AsyncMock(return_value={"status": "error", "error": "bad expr"})
    )
    with pytest.raises(ToolException, match="Prometheus error"):
        await q._query_prometheus("bad{")


@pytest.mark.asyncio
async def test_query_loki_metric_uses_instant(monkeypatch):
    fake_resp = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1000, "5"]}]},
    }
    mock = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(q, "_get_json", mock)

    await q._query_loki_logs('count_over_time({service_name="x"} [5m])')
    _, path, params = mock.call_args[0]
    # metric logql → instant endpoint
    assert path == "/loki/api/v1/query"


@pytest.mark.asyncio
async def test_query_loki_raw_uses_range(monkeypatch):
    fake_resp = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{"stream": {}, "values": [["1000", "line1"]]}],
        },
    }
    mock = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(q, "_get_json", mock)

    await q._query_loki_logs('{service_name="payment"} | json')
    _, path, _ = mock.call_args[0]
    assert path == "/loki/api/v1/query_range"


@pytest.mark.asyncio
async def test_query_loki_parse_error_gets_hint(monkeypatch):
    monkeypatch.setattr(
        q,
        "_get_json",
        AsyncMock(side_effect=ToolException('returned 400: parse error at position 0')),
    )
    with pytest.raises(ToolException) as exc_info:
        await q._query_loki_logs('event="foo"')
    assert "stream selector" in str(exc_info.value)


@pytest.mark.asyncio
async def test_query_tempo_adds_select_version(monkeypatch):
    fake_resp = {"traces": [{"traceID": "abc", "rootServiceName": "payment", "durationMs": 500}]}
    mock = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(q, "_get_json", mock)

    result = await q._query_tempo_traces('{ resource.service.name="payment" }')
    _, _, params = mock.call_args[0]
    assert "select(" in params["q"]
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_query_tempo_traceql_hint_on_400(monkeypatch):
    monkeypatch.setattr(
        q,
        "_get_json",
        AsyncMock(side_effect=ToolException("returned 400: parse error")),
    )
    with pytest.raises(ToolException) as exc_info:
        await q._query_tempo_traces('resource.service.name="payment"')
    assert "braces" in str(exc_info.value) or "predicates" in str(exc_info.value)


@pytest.mark.asyncio
async def test_query_loki_byte_cap_triggers_fallback(monkeypatch):
    """When the primary Loki result is over the cap, auto-aggregate falls back."""
    big_stream = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service_name": "x"},
                    "values": [[str(i), "x" * 200] for i in range(200)],
                }
            ],
        },
    }
    small_agg = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"service_name": "x"}, "values": [[1000, "5"]]}],
        },
    }
    call_count = 0

    async def mock_get_json(base, path, params):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return big_stream["data"]
        return small_agg["data"]

    monkeypatch.setattr(q, "_get_json", mock_get_json)

    result = await q._query_loki_logs('{service_name="x"} | json')
    assert result.get("truncated") is True
    assert "fallback" in result or "original_query" in result
