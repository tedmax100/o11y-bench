"""The observer, observed — manual spans and domain metrics for the RCA loop.

Zero-code instrumentation already covers the plumbing: FastAPI server spans,
httpx client spans for every Prometheus/Loki/Tempo round-trip, and GenAI
spans + `gen_ai.client.*` metrics for every Gemini turn. What it cannot know is
anything about *this agent's own decisions* — how long a whole investigation
took, how many times it had to pivot, whether the sufficiency gate let it stop
or the budget cut it off, and what the governance gate then did with the
result. Those are the numbers that answer "is the agent getting better", and
none of them is recoverable from an HTTP span.

Two rules this module follows:

1. **Names come from the registry, not from here.** `aiops.*` is declared in
   `demo-services/weaver/registry/model/genai.yaml` (group `registry.aiops`),
   which is what `weaver registry live-check` grades this process against. This
   file is the emitter; if a name here is not in there, the drift is a bug in
   this file.
2. **Everything degrades to a no-op.** The OTel API hands back a no-op
   tracer/meter when no SDK is configured, so the unit tests and the probe
   scripts that run *without* `opentelemetry-instrument` cost nothing and
   record nothing. Recording is best-effort throughout: telemetry about an
   investigation must never be the thing that fails the investigation.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics, trace

tracer = trace.get_tracer("aiops_agent")
_meter = metrics.get_meter("aiops_agent")

# --- attribute keys (registry: registry.aiops) -------------------------------
ATTR_TOOL_NAME = "aiops.tool.name"
ATTR_TOOL_DISPOSITION = "aiops.tool.disposition"
ATTR_TOOL_CALLS_USED = "aiops.tool_calls.used"
ATTR_TOOL_CALLS_BUDGET = "aiops.tool_calls.budget"
ATTR_INTENT_IN_SCOPE = "aiops.intent.in_scope"
ATTR_TRIGGER = "aiops.investigation.trigger"
ATTR_SUFFICIENT = "aiops.investigation.sufficient"
ATTR_PIVOTS = "aiops.investigation.pivots"
ATTR_GAPS = "aiops.investigation.gaps"
ATTR_CONFIDENCE = "aiops.investigation.confidence"
ATTR_SUBJECT_SERVICE = "aiops.subject.service"
ATTR_ACTION_NAME = "aiops.action.name"
ATTR_AUTONOMY = "aiops.governance.autonomy"

# --- instruments -------------------------------------------------------------
# Created at import: the OTel API's proxy meter resolves these against the real
# SDK whenever `opentelemetry-instrument` installs it, even though that happens
# after this module is imported.
investigation_duration = _meter.create_histogram(
    "aiops.investigation.duration",
    unit="s",
    description="End-to-end duration of one RCA investigation, pivots included.",
)
investigation_confidence = _meter.create_histogram(
    "aiops.investigation.confidence",
    unit="1",
    description="Confidence the run stated in its own conclusion.",
)
investigation_pivots = _meter.create_histogram(
    "aiops.investigation.pivots",
    unit="{pivot}",
    description="How many times the sufficiency gate sent the run back for more evidence.",
)
tool_calls = _meter.create_counter(
    "aiops.tool.calls",
    unit="{call}",
    description="RCA tool calls, split by how the result was typed (facts.py).",
)
chat_turns = _meter.create_counter(
    "aiops.chat.turns",
    unit="{turn}",
    description="Interactive chat turns, split by the intent gate's verdict.",
)
governance_decisions = _meter.create_counter(
    "aiops.governance.decisions",
    unit="{decision}",
    description="Remediation proposals leaving the governance gate, by autonomy level.",
)


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop unset attributes. An absent service is absent, not the string
    'None' — which would otherwise become its own series forever."""
    return {k: v for k, v in attrs.items() if v is not None and v != ""}


@dataclass
class Investigation:
    """The handle `investigation_span` yields. Holds what the run learned about
    itself so the duration histogram can be split by outcome at the end rather
    than guessed at the start."""

    span: trace.Span
    trigger: str
    service: str | None
    sufficient: bool | None = None
    failed: bool = False

    def record(
        self,
        *,
        sufficient: bool,
        pivots: int,
        confidence: float | None,
        gaps: list[str],
    ) -> None:
        """Stamp the verdict onto the span and its metrics.

        `gaps` goes on the span only. It is the most useful field for a human
        reading one bad run and the worst possible metric dimension — the unmet
        checks vary per run, so a gate that names what is missing would mint a
        new time series for every phrasing.
        """
        self.sufficient = sufficient
        try:
            self.span.set_attributes(
                _clean(
                    {
                        ATTR_SUFFICIENT: sufficient,
                        ATTR_PIVOTS: pivots,
                        ATTR_CONFIDENCE: confidence,
                    }
                )
            )
            if gaps:
                self.span.set_attribute(ATTR_GAPS, gaps)
            dims = _clean(
                {
                    ATTR_TRIGGER: self.trigger,
                    ATTR_SUBJECT_SERVICE: self.service,
                    ATTR_SUFFICIENT: sufficient,
                }
            )
            investigation_pivots.record(pivots, dims)
            if confidence is not None:
                investigation_confidence.record(confidence, dims)
        except Exception:  # pragma: no cover - telemetry never breaks a run
            pass


@contextmanager
def investigation_span(trigger: str, service: str | None) -> Iterator[Investigation]:
    """Wrap one investigation.

    The span is the parent that every model call, tool call and HTTP request in
    the run hangs off — which is what makes the `trace_id` stored beside a
    conclusion (`audit.current_trace_id`) worth keeping in the first place.

    Yields the handle the caller stamps its verdict onto; timing and the span's
    lifetime belong to this contextmanager, so a run that raises still closes
    the span with its error recorded and still reports a duration.
    """
    with tracer.start_as_current_span(
        "aiops.investigation",
        attributes=_clean({ATTR_TRIGGER: trigger, ATTR_SUBJECT_SERVICE: service}),
    ) as span:
        handle = Investigation(span=span, trigger=trigger, service=service)
        started = time.perf_counter()
        try:
            yield handle
        except Exception:
            handle.failed = True
            raise
        finally:
            try:
                investigation_duration.record(
                    time.perf_counter() - started,
                    _clean(
                        {
                            ATTR_TRIGGER: trigger,
                            ATTR_SUBJECT_SERVICE: service,
                            ATTR_SUFFICIENT: handle.sufficient,
                            "error.type": "exception" if handle.failed else None,
                        }
                    ),
                )
            except Exception:  # pragma: no cover - telemetry never breaks a run
                pass


def record_tool_result(tool: str, disposition: str) -> None:
    """One typed tool result. `disposition` is `facts.classify`'s verdict
    (observed / empty / error / …), which is the split that matters: a tool
    that answers and a tool that returns nothing are both HTTP 200, and the
    difference between them is most of this agent's failure modes."""
    try:
        tool_calls.add(1, {ATTR_TOOL_NAME: tool or "unknown", ATTR_TOOL_DISPOSITION: disposition})
    except Exception:  # pragma: no cover
        pass


def record_chat_turn(in_scope: bool) -> None:
    """One interactive turn past the intent gate.

    Counted rather than spanned: the chat path is an async generator, and a
    span held open across a `yield` stays attached to whatever the consumer
    does next — a context leak that would reparent unrelated work under this
    turn. The headless path, which is a plain coroutine, gets the real span.
    """
    try:
        chat_turns.add(1, {ATTR_INTENT_IN_SCOPE: in_scope})
    except Exception:  # pragma: no cover
        pass


def record_budget(used: int, budget: int) -> None:
    """Stamp the per-turn tool-call budget onto whatever span is current.

    A run that stopped because it ran out of budget and a run that stopped
    because it was satisfied produce the same shaped answer; only these two
    numbers tell them apart after the fact.
    """
    try:
        span = trace.get_current_span()
        span.set_attribute(ATTR_TOOL_CALLS_USED, used)
        span.set_attribute(ATTR_TOOL_CALLS_BUDGET, budget)
    except Exception:  # pragma: no cover
        pass


def record_decisions(decisions: list, service: str | None) -> None:
    """The governance gate's output. `autonomy` is the whole point: the series
    that says AUTO never fires is the one that shows the gate is doing its job
    (or that nobody has fed it evidence yet)."""
    try:
        for d in decisions:
            governance_decisions.add(
                1,
                _clean(
                    {
                        ATTR_ACTION_NAME: getattr(d, "action", None),
                        ATTR_AUTONOMY: str(getattr(d, "autonomy", "")),
                        ATTR_SUBJECT_SERVICE: service,
                    }
                ),
            )
    except Exception:  # pragma: no cover
        pass
