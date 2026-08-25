"""The stopping rule, and the loop it drives.

The unit tests below pin the four checks; the last two drive `run_headless`
itself with a stubbed model, because the point of this change is not that the
checks compute correctly — it is that they, and not a number the model writes
about itself, decide whether the investigation goes another round.
"""

import pytest
from langchain_core.messages import AIMessage

import app.agent as agent
from app.config import settings
from app.facts import classify
from app.sufficiency import evaluate_sufficiency, pivot_instruction


def _prom(i=1, empty=False):
    return classify("query_prometheus", {"result": [] if empty else [{"last": 1.3}]}, i)


def _loki(i=2, empty=False):
    return classify("query_loki_logs", {"data": {"result": [] if empty else [{"values": [1]}]}}, i)


def _deploy(i=3):
    return classify("k8s_deployment_status_tool", {"service": "payment", "replicas": 2}, i)


def _catalog(i=4):
    return classify("discover_metrics_tool", {"service": "payment", "metric_count": 6}, i)


_CITED = ["payment declined rate 0.55"]


# --- the four checks --------------------------------------------------------


def test_two_stores_and_two_roles_is_enough():
    v = evaluate_sufficiency([_prom(), _loki()], _CITED)
    assert v.sufficient
    assert v.gaps == []


def test_a_deploy_check_supplies_the_trigger_role():
    # runtime+change, mechanism+trigger: a different pair from the usual
    # metrics+logs, and it clears the same bar.
    v = evaluate_sufficiency([_prom(), _deploy()], _CITED)
    assert v.sufficient


def test_one_store_is_not_corroboration():
    # Two Prometheus queries agreeing with each other is one source, not two.
    v = evaluate_sufficiency([_prom(1), _prom(2)], _CITED)
    assert not v.sufficient
    assert [c.name for c in v.gaps] == ["independent_sources", "causal_roles"]


def test_empty_results_do_not_move_the_gate():
    v = evaluate_sufficiency([_prom(1, empty=True), _loki(2, empty=True)], _CITED)
    assert not v.sufficient
    assert next(c.name for c in v.gaps) == "observed"


def test_catalog_lookups_are_not_a_source():
    # Otherwise "I listed the metric names" would count as a second store.
    v = evaluate_sufficiency([_prom(), _catalog()], _CITED)
    assert not v.sufficient
    assert any(c.name == "independent_sources" for c in v.gaps)


def test_a_conclusion_that_cites_nothing_is_not_sufficient():
    v = evaluate_sufficiency([_prom(), _loki()], [])
    assert not v.sufficient
    assert [c.name for c in v.gaps] == ["conclusion_cites_evidence"]


def test_blank_citations_do_not_count():
    v = evaluate_sufficiency([_prom(), _loki()], ["", "   "])
    assert not v.sufficient


def test_thresholds_are_settings_not_constants():
    facts = [_prom(), _loki()]
    assert not evaluate_sufficiency(facts, _CITED, min_sources=3).sufficient
    assert not evaluate_sufficiency(facts, _CITED, min_roles=3).sufficient


def test_every_check_reports_its_measurement():
    # A gate that says only "no" cannot be argued with at 3am.
    v = evaluate_sufficiency([_prom(1), _prom(2)], _CITED)
    detail = next(c.detail for c in v.checks if c.name == "independent_sources")
    assert "1 independent source(s) ['runtime']" in detail
    assert "needs 2" in detail


def test_verdict_survives_as_data():
    d = evaluate_sufficiency([_prom(), _loki()], _CITED).as_dict()
    assert d["sufficient"] is True
    assert {c["name"] for c in d["checks"]} == {
        "observed",
        "independent_sources",
        "causal_roles",
        "conclusion_cites_evidence",
    }


# --- what the agent is told next -------------------------------------------


def test_the_pivot_names_the_store_that_was_never_queried():
    facts = [_prom(1), _prom(2)]
    text = pivot_instruction(evaluate_sufficiency(facts, _CITED), facts)
    assert "logs" in text and "traces" in text
    assert "metrics or k8s state" not in text  # it already has that one


def test_the_pivot_asks_for_the_missing_causal_role():
    facts = [_prom(), _loki()]  # mechanism + impact, no trigger
    v = evaluate_sufficiency(facts, [])
    text = pivot_instruction(v, facts)
    assert "what changed" in text


def test_a_sufficient_run_gets_no_pivot():
    assert pivot_instruction(evaluate_sufficiency([_prom(), _loki()], _CITED), []) == ""


def test_the_pivot_does_not_mention_confidence():
    # The old one opened with "your confidence was 40%, which is below 60%",
    # which is a request to restate the same guess more boldly.
    facts = [_prom()]
    text = pivot_instruction(evaluate_sufficiency(facts, []), facts).lower()
    assert "confidence" not in text


# --- the loop ---------------------------------------------------------------


class _Findings:
    def __init__(self, confidence, evidence):
        self.confidence = confidence
        self.evidence = list(evidence)
        self.hypothesis = "payment validator rejects odd amounts"
        self.summary = "s"
        self.services = ["payment-service"]
        self.suspected_version = None


class _FakeGraph:
    """Returns a scripted (facts, answer) per invocation and records the pivot
    instruction it was handed."""

    def __init__(self, turns):
        self.turns, self.pivots = list(turns), []

    async def ainvoke(self, state, config=None):
        last = (state.get("messages") or [{}])[-1]
        content = last.get("content") if isinstance(last, dict) else getattr(last, "content", "")
        self.pivots.append(content or "")
        facts = self.turns.pop(0) if self.turns else []
        return {"messages": [AIMessage(content="answer")], "facts": facts}


@pytest.fixture
def headless(monkeypatch):
    """run_headless with everything but the loop stubbed out."""

    def _setup(turns, findings):
        graph = _FakeGraph(turns)
        seq = list(findings)

        async def _agent():
            return graph

        async def _extract(messages):
            return seq.pop(0) if len(seq) > 1 else seq[0]

        monkeypatch.setattr(agent, "_build_agent", _agent)
        monkeypatch.setattr(agent, "extract_findings", _extract)
        monkeypatch.setattr(settings, "runbook_enabled", False)
        monkeypatch.setattr(settings, "action_requests_enabled", False)
        return graph

    return _setup


_ALERT = {"labels": {"alertname": "PaymentDeclines"}, "annotations": {}, "startsAt": None}


async def test_a_high_confidence_run_on_one_store_still_loops(headless, monkeypatch):
    # The whole point: 0.95 stated confidence does not buy a stop when every
    # observation came from the same place.
    graph = headless(
        turns=[[_prom(1)], [_prom(2), _loki(3)]],
        findings=[_Findings(0.95, _CITED), _Findings(0.95, _CITED)],
    )
    out = await agent.run_headless(_ALERT, thread_id="t-loop")
    assert len(graph.pivots) == 2
    assert "independent_sources" in graph.pivots[1]
    assert out["sufficiency"]["sufficient"] is True


async def test_a_low_confidence_run_with_real_evidence_stops(headless, monkeypatch):
    # And the mirror image: 0.3 stated confidence does not force another round
    # when two stores and two roles already agree.
    graph = headless(
        turns=[[_prom(1), _loki(2)]],
        findings=[_Findings(0.3, _CITED)],
    )
    out = await agent.run_headless(_ALERT, thread_id="t-stop")
    assert len(graph.pivots) == 1
    assert out["sufficiency"]["sufficient"] is True
    assert out["findings"].confidence == 0.3  # still reported, just not in charge


async def test_the_loop_is_bounded_and_says_what_was_missing(headless, monkeypatch):
    monkeypatch.setattr(settings, "max_hypothesis_loops", 2)
    graph = headless(
        turns=[[_prom(1)], [_prom(2)], [_prom(3)]],
        findings=[_Findings(0.2, [])],
    )
    out = await agent.run_headless(_ALERT, thread_id="t-bounded")
    assert len(graph.pivots) == 3  # first run + 2 pivots
    gaps = {c["name"] for c in out["sufficiency"]["checks"] if not c["passed"]}
    assert gaps == {"independent_sources", "causal_roles", "conclusion_cites_evidence"}


async def test_evidence_accumulates_across_pivots(headless, monkeypatch):
    # Turn 2 only queried logs; on its own that is one source. The investigation
    # as a whole has two, and that is what the gate is asked about.
    graph = headless(
        turns=[[_prom(1)], [_loki(2)]],
        findings=[_Findings(0.5, _CITED), _Findings(0.5, _CITED)],
    )
    out = await agent.run_headless(_ALERT, thread_id="t-accum")
    assert len(graph.pivots) == 2
    assert out["sufficiency"]["sufficient"] is True
