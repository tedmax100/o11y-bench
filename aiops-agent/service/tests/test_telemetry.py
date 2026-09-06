"""The agent's own telemetry, checked the way we check anyone else's: by
reading what actually arrived at a collector, not by asserting the emit call
was made.

An in-memory SDK stands in for the OTLP exporter. The instruments in
`app.telemetry` are created at import time against the API's proxy meter, so
installing a real provider here is also the test that late SDK installation
(what `opentelemetry-instrument` does in production) reaches them at all.
"""

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import telemetry

_reader = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[_reader]))

_spans = InMemorySpanExporter()
_tracer_provider = TracerProvider()
_tracer_provider.add_span_processor(SimpleSpanProcessor(_spans))
trace.set_tracer_provider(_tracer_provider)


def _points(name: str) -> list:
    """Every data point recorded under one instrument name, across all
    resource/scope groupings the reader hands back."""
    out = []
    data = _reader.get_metrics_data()
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


def test_investigation_span_carries_the_verdict():
    _spans.clear()
    with telemetry.investigation_span("alert", "payment") as inv:
        inv.record(sufficient=False, pivots=2, confidence=0.4, gaps=["independent_domains"])

    (span,) = _spans.get_finished_spans()
    assert span.name == "aiops.investigation"
    assert span.attributes["aiops.investigation.trigger"] == "alert"
    assert span.attributes["aiops.subject.service"] == "payment"
    assert span.attributes["aiops.investigation.sufficient"] is False
    assert span.attributes["aiops.investigation.pivots"] == 2
    assert span.attributes["aiops.investigation.gaps"] == ("independent_domains",)


def test_investigation_span_survives_a_failing_run():
    """A run that raises still closes its span and still reports a duration —
    an investigation that crashed is exactly the one you want timed."""
    _spans.clear()
    try:
        with telemetry.investigation_span("alert", "orders"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    (span,) = _spans.get_finished_spans()
    assert span.status.is_ok is False
    point = next(
        p
        for p in _points("aiops.investigation.duration")
        if p.attributes.get("aiops.subject.service") == "orders"
    )
    assert point.attributes["error.type"] == "exception"


def test_unset_attributes_never_become_a_series():
    """No service on the alert means no `aiops.subject.service` — not the
    string 'None', which would be a permanent extra series."""
    with telemetry.investigation_span("chat", None) as inv:
        inv.record(sufficient=True, pivots=0, confidence=0.9, gaps=[])

    point = next(
        p
        for p in _points("aiops.investigation.confidence")
        if p.attributes["aiops.investigation.trigger"] == "chat"
    )
    assert "aiops.subject.service" not in point.attributes
    assert point.sum == 0.9


def test_tool_calls_split_by_disposition():
    telemetry.record_tool_result("query_prometheus", "observed")
    telemetry.record_tool_result("query_loki_logs", "empty")
    telemetry.record_tool_result("query_loki_logs", "empty")

    by_key = {
        (p.attributes["aiops.tool.name"], p.attributes["aiops.tool.disposition"]): p.value
        for p in _points("aiops.tool.calls")
    }
    assert by_key[("query_prometheus", "observed")] == 1
    assert by_key[("query_loki_logs", "empty")] == 2


def test_governance_decisions_carry_their_autonomy_level():
    from app.governance import Autonomy, Decision

    decision = Decision(
        action="restart_deployment",
        autonomy=Autonomy.PROPOSE,
        requires_human=True,
        confidence=0.7,
        reason="mid band",
        calibration_note="",
        reversible=True,
        requires_approval=False,
    )
    telemetry.record_decisions([decision], "payment")

    (point,) = [
        p
        for p in _points("aiops.governance.decisions")
        if p.attributes["aiops.action.name"] == "restart_deployment"
    ]
    assert point.attributes["aiops.governance.autonomy"] == "propose"
    assert point.attributes["aiops.subject.service"] == "payment"
