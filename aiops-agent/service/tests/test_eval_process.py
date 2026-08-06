"""Unit tests for the process checks — no live stack, no LLM.

Every check gets a transcript that must pass AND one that must fail. A grader
only proves something when you have watched it go red on purpose: the checks
here exist to catch a regression, so the test that matters is the one where the
transcript is bad and the check says so.
"""

from types import SimpleNamespace as NS

from app.eval.process import (
    ProcessSpec,
    check_discover_before_retry,
    check_evidence_or_hedge,
    check_grounded,
    check_queried,
    classify_result,
    extract_calls,
    grade_process,
)

TRACE_A = "a" * 32
TRACE_B = "b" * 32


def _ai(*calls):
    """An assistant message carrying tool calls."""
    return NS(
        tool_calls=[
            {"name": name, "args": args, "id": f"c{i}"} for i, (name, args) in enumerate(calls)
        ],
        content="",
    )


def _tool(idx, content):
    return NS(tool_call_id=f"c{idx}", content=content, tool_calls=[])


# ---- result classification --------------------------------------------------


def test_classify_result_ok_empty_error():
    assert classify_result('{"resultType": "vector", "result": [{"value": 3}]}') == "ok"
    assert classify_result('{"resultType": "vector", "result": []}') == "empty"
    assert classify_result("{'traces': [], 'count': 0}") == "empty"
    assert classify_result("") == "empty"
    assert classify_result("Error: parse error at line 1\nHINT: LogQL must START…") == "error"


# ---- transcript flattening --------------------------------------------------


def test_extract_calls_pairs_results_with_calls():
    messages = [
        _ai(("query_loki_logs", {"logql": '{service="x"}'})),
        _tool(0, '{"data": []}'),
        _ai(("discover_log_fields", {"service": "x"})),
        _tool(0, '{"fields": ["event", "trace_id"]}'),
    ]
    calls = extract_calls(messages)
    assert [c.name for c in calls] == ["query_loki_logs", "discover_log_fields"]
    assert [c.kind for c in calls] == ["empty", "ok"]


# ---- queried ----------------------------------------------------------------


def test_queried_counts_only_non_empty_results():
    calls = extract_calls(
        [
            _ai(("query_prometheus", {})),
            _tool(0, '{"result": []}'),
            _ai(("query_prometheus", {})),
            _tool(0, '{"result": [{"value": 1}]}'),
        ]
    )
    assert check_queried(calls, 1)[0] is True
    assert check_queried(calls, 2)[0] is False  # two calls, only one that found anything


# ---- grounded ---------------------------------------------------------------


def test_grounded_passes_when_cited_id_is_in_a_result():
    calls = extract_calls(
        [_ai(("query_tempo_traces", {})), _tool(0, f'{{"traces": [{{"traceID": "{TRACE_A}"}}]}}')]
    )
    assert check_grounded(calls, f"the slow one is {TRACE_A}")[0] is True


def test_grounded_fails_on_an_invented_trace_id():
    calls = extract_calls(
        [_ai(("query_tempo_traces", {})), _tool(0, f'{{"traces": [{{"traceID": "{TRACE_A}"}}]}}')]
    )
    ok, detail = check_grounded(calls, f"see trace {TRACE_B}")
    assert ok is False
    assert "invented" in detail


def test_grounded_is_vacuously_true_without_a_citation():
    assert check_grounded([], "no IDs here")[0] is True


# ---- discover before retry --------------------------------------------------


def test_discover_before_retry_fails_on_a_reworded_query():
    messages = [
        _ai(("query_loki_logs", {"logql": '{service="user-service"} | level="ERROR"'})),
        _tool(0, '{"data": []}'),
        _ai(("query_loki_logs", {"logql": '{service="user-service"} | level=~"ERROR|WARN"'})),
        _tool(0, '{"data": []}'),
    ]
    ok, detail = check_discover_before_retry(extract_calls(messages))
    assert ok is False
    assert "without discovering" in detail


def test_discover_before_retry_passes_when_it_discovers_first():
    messages = [
        _ai(("query_loki_logs", {"logql": '{service="user-service"} | level="ERROR"'})),
        _tool(0, '{"data": []}'),
        _ai(("discover_log_fields", {"service": "user-service"})),
        _tool(0, '{"fields": ["event"]}'),
        _ai(("query_loki_logs", {"logql": '{service_name="user-service"} | event="x"'})),
        _tool(0, '{"data": [{"line": "…"}]}'),
    ]
    assert check_discover_before_retry(extract_calls(messages))[0] is True


def test_discover_before_retry_allows_acting_on_an_error_hint():
    """An error carries a HINT saying what to fix, so a *changed* retry is the
    right move — only re-sending the identical call is blind."""
    messages = [
        _ai(("query_tempo_traces", {"traceql": "{status=error}", "start": "now-1h"})),
        _tool(0, "Error: invalid time\nHINT: Tempo search takes unix seconds"),
        _ai(("query_tempo_traces", {"traceql": "{status=error}", "start": "1786000000"})),
        _tool(0, '{"traces": [{"traceID": "x"}], "count": 1}'),
    ]
    assert check_discover_before_retry(extract_calls(messages))[0] is True


def test_discover_before_retry_fails_on_an_identical_re_send():
    args = {"traceql": "{status=error}"}
    messages = [
        _ai(("query_tempo_traces", args)),
        _tool(0, "Error: parse error\nHINT: predicates go inside the braces"),
        _ai(("query_tempo_traces", dict(args))),
        _tool(0, "Error: parse error\nHINT: predicates go inside the braces"),
    ]
    ok, detail = check_discover_before_retry(extract_calls(messages))
    assert ok is False
    assert "unchanged" in detail


def test_discover_before_retry_ignores_a_run_that_stopped_querying():
    messages = [_ai(("query_prometheus", {})), _tool(0, '{"result": []}')]
    assert check_discover_before_retry(extract_calls(messages))[0] is True


# ---- evidence or hedge ------------------------------------------------------


def test_evidence_or_hedge_fails_on_confidence_built_on_nothing():
    calls = extract_calls([_ai(("query_prometheus", {})), _tool(0, '{"result": []}')])
    assert check_evidence_or_hedge(calls, 0.9, 0.4)[0] is False
    assert check_evidence_or_hedge(calls, 0.3, 0.4)[0] is True


def test_evidence_or_hedge_passes_whenever_something_was_found():
    calls = extract_calls([_ai(("query_prometheus", {})), _tool(0, '{"result": [{"value": 1}]}')])
    assert check_evidence_or_hedge(calls, 0.95, 0.4)[0] is True


# ---- the spec ---------------------------------------------------------------


def test_grade_process_runs_only_the_checks_that_are_set():
    messages = [_ai(("query_prometheus", {})), _tool(0, '{"result": [{"value": 1}]}')]
    assert grade_process(ProcessSpec(), messages, "", 0.9) == []
    names = [
        c.name for c in grade_process(ProcessSpec(queried_min=1, grounded=True), messages, "", 0.9)
    ]
    assert names == ["queried", "grounded"]


def test_grade_process_reports_the_failing_check():
    messages = [
        _ai(("query_loki_logs", {})),
        _tool(0, '{"data": []}'),
        _ai(("query_loki_logs", {})),
        _tool(0, '{"data": []}'),
    ]
    results = grade_process(
        ProcessSpec(queried_min=1, discover_before_retry=True, evidence_or_hedge_ceiling=0.4),
        messages,
        "order-service is fine, ~0.2% errors",
        0.85,
    )
    failed = {c.name for c in results if not c.passed}
    assert failed == {"queried", "discover_before_retry", "evidence_or_hedge"}
