"""What the fact layer must get right, stated as the payloads that actually
arrive. Every shape below was copied from a real tool return in `app/tools/`,
because the whole value of this layer is that it reads the payload rather than
the model's account of it — a test written against an invented shape would
prove nothing about that.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import app.agent as agent
from app.facts import (
    CONTEXT,
    EMPTY,
    OBSERVED,
    TRUNCATED,
    UNAVAILABLE,
    classify,
    grounding_check,
    independent_domains,
    ledger,
    usable_facts,
)


def _fact(tool: str, content, i: int = 1):
    return classify(tool, content, i)


# --- emptiness, per store ---------------------------------------------------


def test_empty_prometheus_vector_is_not_evidence():
    f = _fact("query_prometheus", {"resultType": "vector", "result": []})
    assert f.disposition == EMPTY
    assert not f.usable


def test_empty_result_with_explanatory_note_keeps_the_note():
    # query.py attaches this when the metric is never emitted at all.
    f = _fact(
        "query_prometheus",
        {
            "resultType": "vector",
            "result": [],
            "note": "`payment_declined_total` is never emitted by this stack",
        },
    )
    assert f.disposition == EMPTY
    assert "never emitted" in f.digest


def test_empty_tempo_search_is_not_evidence():
    f = _fact("query_tempo_traces", {"traces": [], "count": 0})
    assert not f.usable


def test_k8s_events_with_zero_events_is_not_evidence():
    f = _fact("k8s_events_tool", {"service": "payment", "event_count": 0, "events": []})
    assert f.disposition == EMPTY


def test_unreachable_cluster_is_its_own_disposition():
    # Distinct from EMPTY on purpose: absence of k8s events proves nothing when
    # the cluster was never reached, and the next step differs.
    f = _fact("k8s_pod_status_tool", {"unavailable": True, "detail": "k8s API error: timeout"})
    assert f.disposition == UNAVAILABLE
    assert not f.usable


def test_a_truncated_result_still_counts_as_an_observation():
    # Measured against the live stack: one service, one hour of Tempo is over the
    # 8 KB cap, so the most ordinary trace query in this system arrives truncated.
    # Typing that as "nothing was measured" would call a successful query a blank.
    f = _fact(
        "query_tempo_traces",
        {
            "truncated": True,
            "reason": "Tempo result > 8192B; returning slim summaries.",
            "traces": [{"traceID": "31c23409fd04f91bb3bf8f16892b4a0"}],
        },
    )
    assert f.disposition == TRUNCATED
    assert f.usable


def test_a_truncated_result_is_told_not_to_carry_a_total():
    f = _fact("query_loki_logs", {"truncated": True, "reason": "Raw Loki output > cap"})
    assert "never a total" in f.line()


# --- what counts as evidence at all -----------------------------------------


def test_a_real_series_is_evidence():
    f = _fact(
        "query_prometheus",
        {"resultType": "vector", "result": [{"metric": {"service_name": "payment"}, "last": 0.55}]},
    )
    assert f.disposition == OBSERVED
    assert f.usable
    assert f.source_domain == "runtime"


def test_discovery_is_context_never_evidence():
    # A non-empty catalog listing is still not an observation about the incident.
    f = _fact(
        "discover_metrics_tool",
        {"service": "payment", "metric_count": 12, "metrics": [{"name": "http_requests_total"}]},
    )
    assert f.disposition == CONTEXT
    assert not f.usable
    assert f.source_domain == "catalog"


def test_tool_error_text_is_not_evidence():
    f = _fact("query_loki_logs", "Loki error: parse error at line 1\nHINT: LogQL must START with")
    assert not f.usable


def test_python_repr_payload_is_parsed():
    # ToolNode stringifies the dict, so this is the shape that really arrives.
    f = _fact("query_tempo_traces", "{'traces': [], 'count': 0}")
    assert f.disposition == EMPTY


def test_unparseable_payload_never_raises():
    f = _fact("query_prometheus", "<<not a payload at all>>")
    assert f.fact_id == "f01"


# --- counting sources -------------------------------------------------------


def test_two_prometheus_queries_are_one_independent_source():
    facts = [
        _fact("query_prometheus", {"result": [{"last": 1}]}, 1),
        _fact("query_prometheus", {"result": [{"last": 2}]}, 2),
    ]
    assert len(usable_facts(facts)) == 2
    assert independent_domains(facts) == {"runtime"}


def test_empty_results_do_not_count_as_a_source():
    facts = [
        _fact("query_prometheus", {"result": [{"last": 1}]}, 1),
        _fact("query_loki_logs", {"data": {"result": []}}, 2),
    ]
    assert independent_domains(facts) == {"runtime"}


# --- the ledger -------------------------------------------------------------


def test_ledger_states_the_count_and_names_the_unusable():
    facts = [
        _fact("query_prometheus", {"result": []}, 1),
        _fact("query_loki_logs", {"data": {"result": [{"stream": {}, "values": [1]}]}}, 2),
    ]
    text = ledger(facts)
    assert "[f01]" in text and "[f02]" in text
    assert "usable: 1/2" in text
    assert "MUST NOT be cited" in text


def test_ledger_with_nothing_usable_says_so_explicitly():
    facts = [_fact("query_prometheus", {"result": []}, 1)]
    assert "Do NOT state a root cause" in ledger(facts)


def test_no_facts_no_ledger():
    assert ledger([]) == ""


# --- the grounding guard ----------------------------------------------------


_NOTHING = [
    classify("query_prometheus", {"result": []}, 1),
    classify("query_loki_logs", {"data": {"result": []}}, 2),
]


def test_a_number_from_nothing_is_sent_back():
    ok, prompt = grounding_check("payment 的拒絕率是 55%，這是根因。", _NOTHING)
    assert not ok
    assert "query_prometheus:empty" in prompt


def test_a_named_root_cause_from_nothing_is_sent_back():
    ok, _ = grounding_check("The root cause is a bad deploy of order-service.", _NOTHING)
    assert not ok


def test_honestly_reporting_nothing_is_allowed():
    ok, _ = grounding_check(
        "I ran two checks and both came back empty; I have no evidence either way.",
        _NOTHING,
    )
    assert ok


def test_one_usable_observation_clears_the_floor():
    # The bar is deliberately at zero — this layer has no hypothesis binding, so
    # it cannot judge whether one observation is *enough*, only whether there is
    # any. Raising it belongs to the sufficiency gate, not here.
    facts = _NOTHING + [classify("query_prometheus", {"result": [{"last": 0.55}]}, 3)]
    ok, _ = grounding_check("拒絕率 55%，根因是 payment。", facts)
    assert ok


def test_no_facts_at_all_is_not_this_guards_business():
    # A turn that called no tools (a definition question) must not be blocked.
    ok, _ = grounding_check("A deadlock is when two transactions each hold...", [])
    assert ok


def test_list_numbering_is_not_a_quantity_claim():
    ok, _ = grounding_check("1. Check the pool\n2. Check the wait graph", _NOTHING)
    assert ok


# --- wiring: does any of this reach the graph -------------------------------
# The classifier being right is worth nothing if the ledger never leaves the
# node, so these two drive the real graph with a stubbed model.


class _ScriptedLLM:
    def __init__(self, script):
        self.script, self.seen = script, []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, msgs):
        self.seen.append(msgs)
        return self.script.pop(0)


class _EmptyPrometheus:
    async def ainvoke(self, state):
        return {
            "messages": [
                ToolMessage(
                    tool_call_id="c1",
                    name="query_prometheus",
                    content="{'resultType': 'vector', 'result': []}",
                )
            ]
        }


async def _ok(answer):
    return (True, "")


async def _run(monkeypatch, script, thread="t"):
    llm = _ScriptedLLM(script)
    monkeypatch.setattr(agent, "ChatGoogleGenerativeAI", lambda **k: llm)
    monkeypatch.setattr(agent, "ToolNode", lambda *a, **k: _EmptyPrometheus())
    monkeypatch.setattr("app.rubric.verify_trace_ids", _ok)
    monkeypatch.setattr(agent, "_refutation_check", lambda a: (True, ""))
    graph = agent._build_graph()
    out = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="why is payment failing?")],
            "facts": [],
            "tool_calls_used": 0,
            "budget": 5,
            "rubric_feedback": "",
            "rubric_revision_count": 0,
        },
        config={"configurable": {"thread_id": thread}},
    )
    return out, llm


_CALL = AIMessage(
    content="", tool_calls=[{"name": "query_prometheus", "args": {"expr": "up"}, "id": "c1"}]
)


async def test_a_fabricated_number_on_empty_results_is_sent_back(monkeypatch):
    out, llm = await _run(
        monkeypatch,
        [
            _CALL,
            AIMessage(content="拒絕率是 55%，根因是 payment。"),
            AIMessage(content="No evidence."),
        ],
    )
    assert [f.disposition for f in out["facts"]] == ["empty"]
    # The ledger reached the model, and the guard bought a third turn.
    assert any(
        "EVIDENCE LEDGER" in str(getattr(m, "content", "")) for msgs in llm.seen for m in msgs
    )
    assert len(llm.seen) == 3
    assert "No evidence" in agent._flatten_content(out["messages"][-1].content)


async def test_the_ledger_does_not_carry_across_turns(monkeypatch):
    # Same thread_id: `messages` accumulates, `facts` must not — a second turn
    # that measured nothing has to look ungrounded even though the first had a
    # (still empty) observation.
    out, _ = await _run(monkeypatch, [_CALL, AIMessage(content="no evidence")], thread="carry")
    assert len(out["facts"]) == 1


async def test_identical_retry_result_keeps_the_tool_name(monkeypatch):
    # A small model re-sends the exact same (name, args) tool call; tools_node
    # short-circuits it with a directive ToolMessage instead of re-running it.
    # That message must still carry `name=`, or the fact layer files it under
    # "unknown" and it can never be attributed to the tool that produced it.
    # Two distinct AIMessage objects (LangGraph's message reducer dedupes by
    # id, so reusing the same object wouldn't exercise the dup-retry branch).
    same_call = AIMessage(
        content="", tool_calls=[{"name": "query_prometheus", "args": {"expr": "up"}, "id": "c2"}]
    )
    out, _ = await _run(monkeypatch, [_CALL, same_call, AIMessage(content="no evidence")])
    facts = out["facts"]
    assert [f.tool for f in facts] == ["query_prometheus", "query_prometheus"]
    assert facts[-1].disposition == "error"
