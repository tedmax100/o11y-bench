"""Unit tests for app/tools/query.py — time parsing, series summarization,
error hint injection, and the auto queryType routing. No live HTTP calls."""

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

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


@pytest.mark.parametrize(
    "logql",
    [
        'count_over_time({service_name="payment"} [5m])',
        'sum(count_over_time({service_name="x"} [1h]))',
        'rate({service_name="y"} [5m])',
        'sum by (service_name) (count_over_time({app="z"} [10m]))',
        'topk(5, count({job="x"} [1m]))',
    ],
)
def test_is_metric_logql_true(logql):
    assert q._is_metric_logql(logql) is True


@pytest.mark.parametrize(
    "logql",
    [
        '{service_name="payment"} | json',
        '{service_name="payment"} | event="payment.declined"',
        '{service_name="payment"}',
    ],
)
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


def test_selector_found_inside_a_metric_query():
    """`sum(count_over_time({...} [5m]))` is the shape the schema catalog teaches,
    and anchoring at the start of the string excluded all of it — which silently
    disabled every empty-result diagnostic on exactly that shape."""
    sel = q._selector(
        'sum(count_over_time({service_name="payment-service"} | event="payment.declined" [5m]))'
    )
    assert sel == '{service_name="payment-service"}'


def test_selector_found_inside_a_grouped_metric_query():
    sel = q._selector('sum by (git_version) (count_over_time({service_name="x"}[5m]))')
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


def test_tempo_hint_names_the_prom_loki_label_it_used():
    """The agent writes `service_name` because that is the name everywhere else;
    Tempo answers "unexpected IDENTIFIER", which says nothing about that."""
    exc = ToolException("returned 400: parse error at line 1, col 2: unexpected IDENTIFIER")
    hint = str(q._tempo_query_hint('{service_name="payment-service"}', exc))
    assert "`service_name` -> `resource.service.name`" in hint


def test_tempo_hint_calls_out_quoted_status():
    exc = ToolException("returned 500: binary operations must operate on the same type")
    hint = str(q._tempo_query_hint('{resource.service.name="p" && status="error"}', exc))
    assert "no quotes" in hint


def test_tempo_hint_keeps_the_original_error_text():
    exc = ToolException("connection refused")
    hint = str(q._tempo_query_hint("{ status=error }", exc))
    assert hint.startswith("connection refused")


# ---- the Loki series limit ------------------------------------------------


def test_loki_series_limit_hint_writes_the_sum_wrapper():
    """A count with no sum() around it is one series per label combination.

    The generic pipeline hint already contains the word sum(), and it was not
    enough: on the home-field bench the model read the 400, apologised, and
    asked the user how to narrow the question -- three rounds out of three. So
    the limit gets its own branch, with the rewritten query in it.
    """
    exc = ToolException("returned 400: maximum of series (500) reached for a single query")
    logql = 'count_over_time({service_name="payment-service"} | event="payment.declined"[15m])'
    hint = str(q._loki_query_hint(logql, exc))
    assert f"sum({logql})" in hint
    assert "sum by (<label>)" in hint


def test_loki_series_limit_hint_falls_back_when_there_is_nothing_to_wrap():
    exc = ToolException("returned 400: maximum of series (500) reached for a single query")
    hint = str(q._loki_query_hint('{service_name="payment-service"}', exc))
    assert "sum(count_over_time(" in hint


# ---- a Tempo call whose filters came in as keywords ------------------------


@pytest.mark.asyncio
async def test_tempo_kwarg_filters_are_answered_with_the_traceql_they_meant():
    """The model calls this tool the way the Loki one takes filters.

    A required `traceql` field would make pydantic reject the call before any of
    our code sees it, and "traceql: Field required" ended the task every time it
    happened on the away-field bench. So the call is accepted and answered with
    the query it was trying to write.
    """
    with pytest.raises(ToolException) as excinfo:
        await q._query_tempo_traces(service_name="order-service", limit=1)
    msg = str(excinfo.value)
    assert 'traceql="{ resource.service.name="order-service" }"' in msg
    assert "`service_name`" in msg


@pytest.mark.asyncio
async def test_tempo_kwarg_filters_are_joined_not_guessed_at():
    with pytest.raises(ToolException) as excinfo:
        await q._query_tempo_traces(service="payment-service", status="error")
    assert 'resource.service.name="payment-service" && status=error' in str(excinfo.value)


@pytest.mark.asyncio
async def test_tempo_missing_traceql_with_no_filters_falls_back_to_the_syntax_hint():
    with pytest.raises(ToolException) as excinfo:
        await q._query_tempo_traces(traceql="  ")
    assert "TraceQL predicates go inside braces" in str(excinfo.value)


@pytest.mark.asyncio
async def test_tempo_args_schema_accepts_the_mis_shaped_call(monkeypatch):
    """The rewrite is only reachable if the args schema lets the call through."""
    args = q.TempoArgs(service_name="order-service")
    assert args.traceql is None
    assert args.model_dump()["service_name"] == "order-service"


# ---- empty-result notes ----------------------------------------------------


@pytest.mark.asyncio
async def test_prom_empty_names_the_metric_that_does_not_exist(monkeypatch):
    async def mock_get_json(base, path, params):
        if path == "/api/v1/label/__name__/values":
            return {"data": ["payment_charges_total"]}
        return {"resultType": "matrix", "result": []}

    monkeypatch.setattr(q, "_get_json", mock_get_json)
    result = await q._query_prometheus("sum(rate(payment_declines_total[5m]))")
    assert "payment_declines_total" in result["note"]
    assert "discover_metrics" in result["hint"]


@pytest.mark.asyncio
async def test_prom_empty_with_real_metric_points_at_the_matchers(monkeypatch):
    async def mock_get_json(base, path, params):
        if path == "/api/v1/label/__name__/values":
            return {"data": ["payment_charges_total"]}
        return {"resultType": "matrix", "result": []}

    monkeypatch.setattr(q, "_get_json", mock_get_json)
    result = await q._query_prometheus('sum(rate(payment_charges_total{status="nope"}[5m]))')
    assert "label matchers" in result["note"]
    assert "hint" not in result


@pytest.mark.asyncio
async def test_loki_empty_names_the_unindexed_selector_key(monkeypatch):
    async def mock_get_json(base, path, params):
        if path == "/loki/api/v1/labels":
            return {"data": ["service_name", "git_version"]}
        return {"resultType": "streams", "result": []}

    monkeypatch.setattr(q, "_get_json", mock_get_json)
    result = await q._query_loki_logs('{service="payment-service"}')
    assert "Not an indexable stream label: service" in result["note"]
    assert "discover_log_fields" in result["hint"]


@pytest.mark.asyncio
async def test_loki_empty_note_fails_open(monkeypatch):
    """A hint is never worth an exception: if the label call fails, the empty
    result still comes back as an empty result."""

    async def mock_get_json(base, path, params):
        if path == "/loki/api/v1/labels":
            raise ToolException("labels endpoint down")
        return {"resultType": "streams", "result": []}

    monkeypatch.setattr(q, "_get_json", mock_get_json)
    result = await q._query_loki_logs('{service="payment-service"}')
    assert result["result"] == []
    assert "note" not in result


@pytest.mark.asyncio
async def test_loki_strips_the_stats_block(monkeypatch):
    monkeypatch.setattr(
        q,
        "_get_json",
        AsyncMock(
            return_value={
                "resultType": "streams",
                "result": [{"stream": {"service_name": "x"}, "values": [["1", "line"]]}],
                "stats": {"summary": {"totalBytesProcessed": 0}},
            }
        ),
    )
    result = await q._query_loki_logs('{service_name="x"}')
    assert "stats" not in result


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

    result = await q._query_prometheus("rate(http_requests_total[5m])", start="now-1h", end="now")
    assert result["resultType"] == "matrix_summary"
    mock.assert_awaited_once()
    _, path, _params = mock.call_args[0]
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
    _, path, _params = mock.call_args[0]
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
async def test_query_loki_forced_instant_on_raw_selector_is_refused_with_a_rewrite(monkeypatch):
    """Loki cannot run a raw stream selector as an instant query, and demoting it
    to a range query silently caps the line count — so the tool refuses and hands
    back the metric-shaped rewrite that answers the question it was really asked."""
    mock = AsyncMock()
    monkeypatch.setattr(q, "_get_json", mock)

    with pytest.raises(ToolException) as exc:
        await q._query_loki_logs(
            '{service_name="payment-service"} | event="payment.declined"', queryType="instant"
        )
    assert "count_over_time" in str(exc.value)
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_loki_forced_instant_kept_for_metric_logql(monkeypatch):
    fake_resp = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1000, "5"]}]},
    }
    mock = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(q, "_get_json", mock)

    await q._query_loki_logs('sum(count_over_time({service_name="x"} [5m]))', queryType="instant")
    _, path, _ = mock.call_args[0]
    assert path == "/loki/api/v1/query"


@pytest.mark.asyncio
async def test_query_loki_parse_error_gets_hint(monkeypatch):
    monkeypatch.setattr(
        q,
        "_get_json",
        AsyncMock(side_effect=ToolException("returned 400: parse error at position 0")),
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


# ---- metric-name extraction: labels are not metrics -------------------------


def test_metric_names_ignores_grouping_labels():
    """A live drill wrote five dead ends named `reason` — a label, not a metric —
    into the recall block, which then told the next run not to query the labels
    the answer is written in."""
    from app.tools.query import _metric_names

    assert _metric_names('sum by (reason) (rate(orders_total{status="cancelled"}[5m]))') == {
        "orders_total"
    }
    assert _metric_names("sum without (le, reason) (rate(x_total[5m]))") == {"x_total"}


def test_metric_names_ignores_label_matchers():
    from app.tools.query import _metric_names

    expr = 'sum(rate(user_auth_checks_total{status="error", reason="session_store_timeout"}[2m]))'
    assert _metric_names(expr) == {"user_auth_checks_total"}


def test_metric_names_keeps_both_sides_of_a_join():
    from app.tools.query import _metric_names

    expr = "sum(rate(a_total[5m])) / on(job) group_left(pod) sum(rate(b_total[5m]))"
    assert _metric_names(expr) == {"a_total", "b_total"}


# ---- truncation fallback ----------------------------------------------------


def test_fallback_keeps_the_filter_that_was_asked_about():
    """The bug: `|= "declined"` came back as counts of every event on the service,
    top bucket `payment.authorized`, under a key labelled `original_query`."""
    logql = '{service_name="payment-service"} |= "declined"'
    sel = q._selector(logql)
    pipe = q._loki_pipeline(logql, sel)
    assert pipe == '|= "declined"'
    fb = q._loki_fallback(sel, pipe)
    assert '|= "declined"' in fb
    assert "detected_level" in fb
    assert " level," not in fb  # the field these services never emitted


def test_fallback_keeps_a_label_filter_too():
    logql = '{service_name="payment-service"} | event="payment.declined"'
    sel = q._selector(logql)
    assert q._loki_pipeline(logql, sel) == '| event="payment.declined"'


def test_a_stage_that_cannot_be_counted_is_left_out():
    """`line_format` rewrites the line rather than selecting lines; counting it
    is meaningless, so the caller falls back to the selector and says so."""
    logql = '{service_name="x"} | json | line_format "{{.msg}}"'
    sel = q._selector(logql)
    assert q._loki_pipeline(logql, sel) == ""


def test_a_bare_selector_has_no_pipeline():
    logql = '{service_name="x"}'
    assert q._loki_pipeline(logql, q._selector(logql)) == ""


def test_a_metric_query_contributes_no_pipeline_tail():
    """Its range and closing parens are not a filter; taking them would be
    nonsense — and its result was small enough never to reach here anyway."""
    logql = 'sum(count_over_time({service_name="x"} | event="e" [5m]))'
    assert q._loki_pipeline(logql, q._selector(logql)) == '| event="e"'
