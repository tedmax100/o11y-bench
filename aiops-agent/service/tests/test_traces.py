"""Unit tests for app/traces.py — OTLP-JSON helpers, trace normalization,
cost model, and truncation. No live HTTP or LLM calls."""

import pytest

from app.traces import (
    _cost,
    _flatten,
    _maybe_json,
    _node_kind,
    _node_label,
    _normalize_trace,
    _otlp_val,
    _truncate,
)

# ---- _otlp_val -------------------------------------------------------------


def test_otlp_val_string():
    assert _otlp_val({"stringValue": "hello"}) == "hello"


def test_otlp_val_int():
    assert _otlp_val({"intValue": "42"}) == 42


def test_otlp_val_int_non_numeric():
    # non-numeric intValue falls back to raw string
    assert _otlp_val({"intValue": "not-a-number"}) == "not-a-number"


def test_otlp_val_bool():
    assert _otlp_val({"boolValue": True}) is True


def test_otlp_val_double():
    assert _otlp_val({"doubleValue": 3.14}) == pytest.approx(3.14)


def test_otlp_val_array():
    v = {"arrayValue": {"values": [{"stringValue": "a"}, {"intValue": "1"}]}}
    assert _otlp_val(v) == ["a", 1]


def test_otlp_val_unknown_returns_first_value():
    assert _otlp_val({"weirdKey": "x"}) == "x"


# ---- _flatten --------------------------------------------------------------


def test_flatten_basic():
    attrs = [
        {"key": "service.name", "value": {"stringValue": "payment"}},
        {"key": "http.status_code", "value": {"intValue": "200"}},
    ]
    result = _flatten(attrs)
    assert result == {"service.name": "payment", "http.status_code": 200}


def test_flatten_empty():
    assert _flatten([]) == {}


def test_flatten_none():
    assert _flatten(None) == {}


# ---- _maybe_json -----------------------------------------------------------


def test_maybe_json_parses_object():
    assert _maybe_json('{"key": "val"}') == {"key": "val"}


def test_maybe_json_parses_array():
    assert _maybe_json("[1, 2, 3]") == [1, 2, 3]


def test_maybe_json_plain_string_unchanged():
    assert _maybe_json("hello") == "hello"


def test_maybe_json_non_string_passthrough():
    assert _maybe_json(42) == 42
    assert _maybe_json(None) is None


def test_maybe_json_invalid_json_unchanged():
    s = "{broken json"
    assert _maybe_json(s) == s


# ---- _truncate -------------------------------------------------------------


def test_truncate_long_string():
    s = "a" * 700
    result = _truncate(s, limit=600)
    assert result.endswith("…")
    assert len(result) == 601


def test_truncate_short_string_unchanged():
    s = "short"
    assert _truncate(s, limit=600) == "short"


def test_truncate_list_caps_at_20():
    lst = list(range(30))
    result = _truncate(lst)
    assert len(result) == 20


def test_truncate_dict_recurses():
    d = {"key": "a" * 700}
    result = _truncate(d, limit=600)
    assert result["key"].endswith("…")


# ---- _cost -----------------------------------------------------------------


def test_cost_known_model():
    # gemini-2.5-flash: 0.30/M input, 2.50/M output
    c = _cost("gemini-2.5-flash", in_tok=1_000_000, out_tok=0, cache_tok=0)
    assert c == pytest.approx(0.30, rel=1e-4)


def test_cost_cache_discount():
    # cached tokens billed at 0.25x input price
    c = _cost("gemini-2.5-flash", in_tok=1_000_000, out_tok=0, cache_tok=1_000_000)
    assert c == pytest.approx(0.30 * 0.25, rel=1e-4)


def test_cost_unknown_model_returns_none():
    assert _cost("unknown-model", in_tok=1000, out_tok=1000, cache_tok=0) is None


def test_cost_zero_tokens():
    c = _cost("gemini-2.5-flash", in_tok=0, out_tok=0, cache_tok=0)
    assert c == pytest.approx(0.0)


# ---- _node_kind ------------------------------------------------------------


def test_node_kind_llm_by_name():
    assert _node_kind("ChatGoogleGenerativeAI.chat", {}) == "llm"


def test_node_kind_llm_by_attr():
    assert _node_kind("generate", {"gen_ai.operation.name": "chat"}) == "llm"


def test_node_kind_tool():
    assert _node_kind("execute_tool_foo", {}) == "tool"


def test_node_kind_tool_by_attr():
    assert _node_kind("anything", {"gen_ai.tool.name": "query_prometheus"}) == "tool"


def test_node_kind_http_by_attr():
    assert _node_kind("call", {"http.method": "GET"}) == "http"


def test_node_kind_http_by_name():
    assert _node_kind("GET /api/v1/query", {}) == "http"


def test_node_kind_business():
    assert _node_kind("payment.charge", {"app.outcome": "success"}) == "business"


# ---- _node_label -----------------------------------------------------------


def test_node_label_uses_run_name():
    assert _node_label("span", {"run_name": "RCA_Agent"}) == "RCA_Agent"


def test_node_label_uses_traceloop_entity():
    assert _node_label("span", {"traceloop.entity.name": "AgentNode"}) == "AgentNode"


def test_node_label_uses_langgraph_node():
    assert _node_label("span", {"langgraph_node": "call_model"}) == "call_model"


def test_node_label_falls_back_to_name():
    assert _node_label("my_span", {}) == "my_span"


# ---- _normalize_trace ------------------------------------------------------


def _make_span(
    span_id: str,
    parent_id: str | None,
    name: str,
    start_ns: int,
    end_ns: int,
    attrs: list | None = None,
    error: bool = False,
    service: str = "payment",
) -> dict:
    status = {"code": "STATUS_CODE_ERROR"} if error else {}
    return {
        "spanId": span_id,
        "parentSpanId": parent_id,
        "name": name,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attrs or [],
        "status": status,
    }


def _make_raw_trace(spans: list[dict], service: str = "payment") -> dict:
    return {
        "batches": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                },
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }


def test_normalize_trace_single_span():
    span = _make_span("aaa", None, "GET /charge", 1_000_000_000, 2_000_000_000)
    raw = _make_raw_trace([span])
    tree = _normalize_trace(raw)
    assert tree["rollup"]["span_count"] == 1
    assert len(tree["roots"]) == 1
    assert tree["roots"][0]["name"] == "GET /charge"
    assert tree["roots"][0]["duration_ms"] == pytest.approx(1000.0)


def test_normalize_trace_parent_child_linking():
    parent = _make_span("p", None, "root", 1_000_000_000, 5_000_000_000)
    child = _make_span("c", "p", "child", 2_000_000_000, 3_000_000_000)
    raw = _make_raw_trace([parent, child])
    tree = _normalize_trace(raw)
    assert len(tree["roots"]) == 1
    assert len(tree["roots"][0]["children"]) == 1
    assert tree["roots"][0]["children"][0]["name"] == "child"


def test_normalize_trace_error_flag():
    span = _make_span("e1", None, "bad", 0, 1_000_000_000, error=True)
    raw = _make_raw_trace([span])
    tree = _normalize_trace(raw)
    assert tree["roots"][0]["error"] is True
    assert tree["rollup"]["error_count"] == 1


def test_normalize_trace_llm_rollup():
    llm_attrs = [
        {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "100"}},
        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "50"}},
        {"key": "gen_ai.request.model", "value": {"stringValue": "gemini-2.5-flash"}},
    ]
    span = _make_span("l1", None, "ChatGoogleGenerativeAI.chat", 0, 1_000_000_000, attrs=llm_attrs)
    raw = _make_raw_trace([span])
    tree = _normalize_trace(raw)
    rollup = tree["rollup"]
    assert rollup["llm_calls"] == 1
    assert rollup["input_tokens"] == 100
    assert rollup["output_tokens"] == 50
    assert rollup["total_tokens"] == 150
    assert rollup["cost"] is not None and rollup["cost"] > 0


def test_normalize_trace_children_sorted_by_start():
    parent = _make_span("p", None, "root", 0, 5_000_000_000)
    c1 = _make_span("c1", "p", "first", 2_000_000_000, 3_000_000_000)
    c2 = _make_span("c2", "p", "second", 1_000_000_000, 2_000_000_000)
    raw = _make_raw_trace([parent, c1, c2])
    tree = _normalize_trace(raw)
    children = tree["roots"][0]["children"]
    assert children[0]["name"] == "second"
    assert children[1]["name"] == "first"


def test_normalize_trace_empty():
    tree = _normalize_trace({"batches": []})
    assert tree["roots"] == []
    assert tree["rollup"]["span_count"] == 0


def test_normalize_trace_compact_truncates_payload():
    llm_attrs = [
        {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
        {"key": "gen_ai.input.messages", "value": {"stringValue": "x" * 1000}},
    ]
    span = _make_span("l1", None, "ChatGoogleGenerativeAI.chat", 0, 1_000_000_000, attrs=llm_attrs)
    raw = _make_raw_trace([span])
    tree = _normalize_trace(raw, compact=True)
    node = tree["roots"][0]
    msgs = node["payload"].get("input_messages", "")
    assert isinstance(msgs, str) and msgs.endswith("…")


def test_normalize_trace_tool_kind():
    tool_attrs = [
        {"key": "gen_ai.tool.name", "value": {"stringValue": "query_prometheus"}},
        {"key": "gen_ai.tool.call.arguments", "value": {"stringValue": '{"expr":"up"}'}},
    ]
    span = _make_span("t1", None, "execute_tool_query_prometheus", 0, 500_000_000, attrs=tool_attrs)
    raw = _make_raw_trace([span])
    tree = _normalize_trace(raw)
    node = tree["roots"][0]
    assert node["kind"] == "tool"
    assert tree["rollup"]["tool_calls"] == 1
    assert node["payload"]["tool_name"] == "query_prometheus"
    assert node["payload"]["arguments"] == {"expr": "up"}
