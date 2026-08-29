"""Grade *how* an answer was reached, not just whether it named the right service.

A verdict-only grade can't tell an investigation from a lucky guess: an agent
that queries a label that doesn't exist, gets nothing back, and still writes a
confident conclusion scores the same as one that discovered the real labels
first. That was the original baseline agent's failure mode — hardcoded schema
assumptions, no discovery step, numbers produced out of empty results — and the
only way a regression suite catches it coming back is to read the transcript.

The checks here are the process half of the same suite the answer checks live
in, and deliberately mirror the ones the day-one bench ran against natural
language answers (`queried`, `grounded`) so the two graders judge the same
things where they overlap.

Nothing here calls an LLM: given the message list from `run_headless`, every
check is a mechanical read of tool calls and tool results.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from ..rubric import _TRACE_ID_RE as TRACE_ID_RE  # one definition of "a trace ID"

QUERY_TOOLS = {"query_prometheus", "query_loki_logs", "query_tempo_traces"}
DISCOVER_PREFIX = "discover_"

ResultKind = Literal["ok", "empty", "error"]


@dataclass
class ToolCall:
    """One tool call paired with the result it got back."""

    name: str
    args: dict[str, Any]
    result: str = ""
    kind: ResultKind = "ok"


def classify_result(content: str) -> ResultKind:
    """ok / empty / error, from the tool result text the model actually read."""
    text = (content or "").strip()
    if not text:
        return "empty"
    low = text.lower()
    if low.startswith("error") or "toolexception" in low or "\nhint:" in low:
        return "error"
    # Tool results arrive as the str() of a dict; parse when we can, fall back to
    # matching the empty containers textually.
    payload: Any = None
    try:
        payload = json.loads(text)
    except Exception:
        pass
    if isinstance(payload, dict):
        for key in ("result", "traces", "data", "metrics", "fields", "spanNames"):
            if key in payload and payload[key] in ([], {}, None):
                return "empty"
        if payload.get("count") == 0:
            return "empty"
        return "ok"
    if re.search(r"['\"](result|traces|data)['\"]:\s*(\[\]|\{\})", text):
        return "empty"
    if re.search(r"['\"]count['\"]:\s*0\b", text):
        return "empty"
    return "ok"


def extract_calls(messages: list) -> list[ToolCall]:
    """Flatten a LangGraph transcript into tool calls paired with their results."""
    pending: dict[str, ToolCall] = {}
    calls: list[ToolCall] = []
    for msg in messages:
        for tc in getattr(msg, "tool_calls", None) or []:
            call = ToolCall(name=tc.get("name", "?"), args=dict(tc.get("args") or {}))
            calls.append(call)
            call_id = tc.get("id")
            if call_id:
                pending[call_id] = call
        call_id = getattr(msg, "tool_call_id", None)
        if call_id and call_id in pending:
            call = pending.pop(call_id)
            content = getattr(msg, "content", "")
            call.result = content if isinstance(content, str) else str(content)
            call.kind = classify_result(call.result)
    return calls


def tool_names(messages: list) -> list[str]:
    return [c.name for c in extract_calls(messages)]


# ---- the checks -------------------------------------------------------------


def check_queried(calls: list[ToolCall], minimum: int) -> tuple[bool, str]:
    """It has to actually look. An answer with no successful tool call is a
    guess no matter how well it reads."""
    ok_calls = [c for c in calls if c.kind == "ok"]
    return len(ok_calls) >= minimum, f"{len(ok_calls)} successful of {len(calls)} call(s)"


def check_grounded(calls: list[ToolCall], answer: str) -> tuple[bool, str]:
    """Every trace ID in the answer must appear verbatim in a tool result."""
    cited = set(TRACE_ID_RE.findall(answer or ""))
    if not cited:
        return True, "no trace ID cited"
    seen = " ".join(c.result for c in calls)
    invented = sorted(t for t in cited if t not in seen)
    if invented:
        return False, f"{len(invented)} invented ID(s): {invented[0][:12]}…"
    return True, f"{len(cited)} cited ID(s) all in tool results"


def check_discover_before_retry(calls: list[ToolCall]) -> tuple[bool, str]:
    """After a query comes back EMPTY, the next thing must be a `discover_*`
    call — not the same query with the wording changed.

    This is the discover-before-query rule stated so a grader can check it.
    Rephrasing a query whose labels don't exist is the loop the baseline agent
    burned its whole budget in, and it is specific to *empty*: an empty result
    means the names you assumed may not exist, and nothing but discovery can
    tell you which ones do.

    An ERROR is different — the query tools answer errors with a HINT that says
    what to fix (the time format, the missing stream selector), so acting on the
    hint is the right move and only an identical re-send counts as blind.
    """
    for i, call in enumerate(calls):
        if call.name not in QUERY_TOOLS or call.kind == "ok":
            continue
        nxt = next(
            (
                c
                for c in calls[i + 1 :]
                if c.name.startswith(DISCOVER_PREFIX) or c.name in QUERY_TOOLS
            ),
            None,
        )
        if nxt is None or nxt.name.startswith(DISCOVER_PREFIX):
            continue  # stopped querying, or discovered first — both fine
        if call.kind == "empty":
            # Both are failures of the same rule, but they are not the same
            # mistake and the counts move for different reasons: re-asking the
            # store that just said nothing is the budget-burning loop this rule
            # was written for, while pivoting to another store at least changes
            # the question. Measured 2026-08-29, closed-book: 14 of 17 were the
            # first kind. Saying which keeps that split readable without
            # softening the rule — you still have to discover first.
            same = "re-queried" if nxt.name == call.name else "pivoted to"
            return False, (f"{call.name} came back empty, {same} {nxt.name} without discovering")
        if nxt.name == call.name and nxt.args == call.args:
            return False, f"{call.name} errored and was re-sent unchanged"
    return True, "no blind retry after an empty result"


def check_evidence_or_hedge(
    calls: list[ToolCall], confidence: float, ceiling: float
) -> tuple[bool, str]:
    """Confidence must be backed by at least one non-empty result. Nothing found
    and high confidence is the fabrication case — the one that reads best and is
    worth the least."""
    if any(c.kind == "ok" for c in calls):
        return True, "has evidence"
    ok = confidence <= ceiling
    return ok, f"no non-empty result, confidence {confidence:.2f} (ceiling {ceiling:.2f})"


def check_used_tools(calls: list[ToolCall], wanted: list[str]) -> tuple[bool, str]:
    """A named tool has to actually get called on this fixture.

    Every other check here reads what the agent did with the tools it chose.
    This one exists because "the model sometimes reaches for that tool and
    sometimes doesn't" was a claim resting on two observed runs, which is not a
    rate — it is two anecdotes. A per-fixture check turns it into a number the
    suite reports on every pass, so `--repeat N` measures the frequency instead
    of leaving it to memory.

    Calling the tool counts, whatever it returned: this is about reach, not
    about whether the cluster answered.
    """
    used = {c.name for c in calls}
    missing = [t for t in wanted if t not in used]
    if missing:
        return False, f"never called: {', '.join(missing)}"
    return True, f"called {', '.join(wanted)}"


@dataclass
class ProcessSpec:
    """Per-fixture process expectations. All optional; unset checks don't run."""

    queried_min: int | None = None
    grounded: bool = False
    discover_before_retry: bool = False
    evidence_or_hedge_ceiling: float | None = None
    used_tools: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


def grade_process(
    spec: ProcessSpec, messages: list, answer: str, confidence: float
) -> list[CheckResult]:
    calls = extract_calls(messages)
    out: list[CheckResult] = []
    if spec.queried_min is not None:
        passed, detail = check_queried(calls, spec.queried_min)
        out.append(CheckResult("queried", passed, detail))
    if spec.grounded:
        passed, detail = check_grounded(calls, answer)
        out.append(CheckResult("grounded", passed, detail))
    if spec.discover_before_retry:
        passed, detail = check_discover_before_retry(calls)
        out.append(CheckResult("discover_before_retry", passed, detail))
    if spec.evidence_or_hedge_ceiling is not None:
        passed, detail = check_evidence_or_hedge(calls, confidence, spec.evidence_or_hedge_ceiling)
        out.append(CheckResult("evidence_or_hedge", passed, detail))
    if spec.used_tools:
        passed, detail = check_used_tools(calls, spec.used_tools)
        out.append(CheckResult("used_tools", passed, detail))
    return out
