"""Typing a tool result as evidence — or refusing to.

Day18 walked the CEL checklist and left two boxes empty: correlation and
projection. Everything the agent learns mid-run arrives as a `ToolMessage`
carrying whatever shape that particular store happens to return, and nothing
between the store and the model says what *kind* of thing came back. A
Prometheus vector, an empty Loki stream and "Kubernetes is not reachable" are
all just text in the transcript, and the model is left to decide which of them
counts. Measured on the benchmark, it decides badly in one specific way: it
reads an empty result and reports a number anyway.

`query.py` already tries to head that off with prose — an empty Prometheus
result comes back with a note saying the metric is never emitted, so rewording
won't help. `refutation.py` documents what that class of fix is worth: the
model was told, in three seeds out of three, and restated the thing it had been
told to avoid. Advice in the payload is not a control.

So this module makes the same judgement where the model cannot argue with it.
Every tool result is classified into a `DiagnosticFact` by deterministic rules —
which store it came from, which causal role that store can speak to, and
crucially whether it is *usable as evidence at all*. An empty result, an
unreachable cluster, a truncated dump and a catalog lookup are each unusable,
each for a different stated reason, and the ledger says so in one line per fact.

Two things this deliberately is not:

**It is not a planner.** There are no plan steps here binding a hypothesis to a
step before it runs, so `causal_role` is a *hint* derived from the tool, not a
proof that the observation tests that role. `query_prometheus` can speak to a
mechanism; it does not follow that this particular query did. The hint is worth
having because it makes the shape of the evidence visible ("three observations,
all mechanism, nothing on the trigger"), and it is labelled a hint everywhere it
is rendered so nobody downstream mistakes it for a verdict.

**It is not a scorer.** `usable` is not a quality judgement. It answers one
question — may this be cited as evidence for a root cause — and it answers it
from the payload's own structure, never from the model's summary of it.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("aiops_agent.facts")

# Which store a tool speaks for. `catalog` is deliberately its own domain rather
# than being folded into runtime/log: a discovery call tells us what *could* be
# queried, which is context for the next step and never evidence about this
# incident. Counting it as a source would let "I listed the metric names" look
# like independent corroboration.
_SOURCE_DOMAIN = {
    "query_prometheus": "runtime",
    "query_loki_logs": "log",
    "query_tempo_traces": "trace",
    "k8s_pod_status_tool": "runtime",
    "k8s_events_tool": "runtime",
    "k8s_deployment_status_tool": "change",
    "github_compare": "change",
    "github_get_file": "change",
    "discover_metrics_tool": "catalog",
    "discover_span_names_tool": "catalog",
    "discover_log_fields_tool": "catalog",
}

# The causal role each store can *speak to* — see the module docstring on why
# this is a hint. Roughly: what changed (trigger), how it broke (mechanism),
# what it did to users (impact), and what merely orients the search (context).
_ROLE_HINT = {
    "query_prometheus": "mechanism",
    "query_loki_logs": "impact",
    "query_tempo_traces": "mechanism",
    "k8s_pod_status_tool": "mechanism",
    "k8s_events_tool": "mechanism",
    "k8s_deployment_status_tool": "trigger",
    "github_compare": "trigger",
    "github_get_file": "context",
    "discover_metrics_tool": "context",
    "discover_span_names_tool": "context",
    "discover_log_fields_tool": "context",
}

# Dispositions. `observed` and `truncated` carry real content; the rest each name
# a different reason the observation cannot carry a root cause, because "no
# evidence" and "the cluster was unreachable" call for different next steps.
#
# `truncated` counting as usable is not a soft edge — it was measured. The most
# ordinary Tempo query in this stack (one service, one hour) comes back over the
# 8 KB cap and is served as slim summaries, and those summaries are real traces.
# Typing that as "nothing was measured" would have called a successful query a
# blank, which is the same mistake in the other direction. What it must not do
# is carry a *quantity* — the payload was cut before anyone counted it.
OBSERVED = "observed"
EMPTY = "empty"
UNAVAILABLE = "unavailable"
ERROR = "error"
TRUNCATED = "truncated"
CONTEXT = "context"

_DISPOSITION_NOTE = {
    EMPTY: "no data in this window — MUST NOT be cited as evidence",
    UNAVAILABLE: "store unreachable — absence here proves nothing",
    ERROR: "the query failed — nothing was measured",
    TRUNCATED: "real but capped — cite what is in it, never a total or a rate from it",
    CONTEXT: "catalog/reference lookup — orients the next query, not evidence",
}


@dataclass(frozen=True)
class DiagnosticFact:
    """One tool result, typed. `usable` is the only field a gate should read."""

    fact_id: str
    tool: str
    source_domain: str
    role_hint: str
    disposition: str
    usable: bool
    digest: str

    def line(self) -> str:
        mark = "ok " if self.usable else "XX "
        tail = _DISPOSITION_NOTE.get(self.disposition) or self.digest
        if self.disposition == OBSERVED:
            tail = self.digest
        return f"[{self.fact_id}] {mark}{self.source_domain}/{self.role_hint} {self.tool}: {tail}"


def _coerce(content: Any) -> Any:
    """Recover the tool's return value from a ToolMessage's content.

    ToolNode stringifies whatever the tool returned, so by the time it reaches
    the graph it is a `str` holding a Python repr (single quotes, True/None) —
    not JSON. Try both, and fall back to the raw string, which the text-level
    checks below can still read.
    """
    if not isinstance(content, str):
        return content
    text = content.strip()
    if not text:
        return ""
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(text)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            continue
    return content


# Substrings that mean the tool ran but measured nothing. Matched against the
# raw text as a backstop for results that did not survive `_coerce` — the
# structured checks below are the primary path.
_EMPTY_MARKERS = (
    '"result": []',
    "'result': []",
    '"traces": []',
    "'traces': []",
    "matched no lines",
    "is never emitted",
    "no series",
)
_ERROR_MARKERS = ("error:", "toolexception", "hint:", "you already ran this exact query")


def _classify_payload(tool: str, payload: Any, raw: str) -> tuple[str, str]:
    """Return (disposition, digest) for one tool result."""
    if _SOURCE_DOMAIN.get(tool) == "catalog":
        return CONTEXT, _digest(payload, raw)

    if isinstance(payload, dict):
        if payload.get("unavailable"):
            return UNAVAILABLE, str(payload.get("detail", ""))[:120]
        if payload.get("truncated"):
            return TRUNCATED, str(payload.get("reason", ""))[:120]
        # `note` is set by query.py only on an empty result it could explain.
        if payload.get("note") and _is_empty_payload(payload):
            return EMPTY, str(payload["note"])[:160]
        if _is_empty_payload(payload):
            return EMPTY, ""
        return OBSERVED, _digest(payload, raw)

    low = raw.lower()
    if any(m in low for m in _ERROR_MARKERS):
        return ERROR, raw[:120]
    if any(m.lower() in low for m in _EMPTY_MARKERS):
        return EMPTY, ""
    if not raw.strip():
        return EMPTY, ""
    return OBSERVED, _digest(payload, raw)


def _is_empty_payload(payload: dict) -> bool:
    """Structural emptiness, per store. Each store spells 'nothing' differently,
    and every one of these spellings has been read as a number by a model at
    least once on the benchmark."""
    result = payload.get("result")
    if isinstance(result, list) and not result:
        return True
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("result"), list) and not data["result"]:
        return True
    if "traces" in payload and not payload.get("traces"):
        return True
    for count_key in ("count", "event_count", "metric_count"):
        if payload.get(count_key) == 0:
            return True
    for list_key in ("events", "pods", "span_names", "fields", "metrics", "commits"):
        if list_key in payload and not payload.get(list_key):
            return True
    return False


def _digest(payload: Any, raw: str) -> str:
    """One short line describing what came back. Keys and counts only — never a
    value the model could later quote back as if it had been verified."""
    if isinstance(payload, dict):
        parts = []
        for key in ("count", "event_count", "metric_count", "resultType", "service"):
            if key in payload:
                parts.append(f"{key}={payload[key]}")
        for key in ("result", "traces", "events", "pods", "metrics"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                parts.append(f"{key}[{len(value)}]")
        if parts:
            return ", ".join(parts[:4])
        return ", ".join(list(payload)[:5])
    return raw[:100].replace("\n", " ")


def classify(tool: str, content: Any, index: int) -> DiagnosticFact:
    """Type one tool result. Never raises — an unclassifiable payload is still a
    fact, just an unusable one, because a gate that crashes on a weird payload
    is worse than one that says 'I could not read this'."""
    raw = content if isinstance(content, str) else str(content)
    try:
        payload = _coerce(content)
        disposition, digest = _classify_payload(tool, payload, raw)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("facts: could not classify %s (%s)", tool, e)
        disposition, digest = ERROR, f"unreadable payload ({type(e).__name__})"
    return DiagnosticFact(
        fact_id=f"f{index:02d}",
        tool=tool,
        source_domain=_SOURCE_DOMAIN.get(tool, "unknown"),
        role_hint=_ROLE_HINT.get(tool, "context"),
        disposition=disposition,
        usable=disposition in (OBSERVED, TRUNCATED),
        digest=digest,
    )


def usable_facts(facts: list[DiagnosticFact]) -> list[DiagnosticFact]:
    return [f for f in facts if f.usable]


def independent_domains(facts: list[DiagnosticFact]) -> set[str]:
    """Distinct stores that actually returned something. Two Prometheus queries
    are one source, not two — which is the whole point of counting domains
    rather than counting observations."""
    return {f.source_domain for f in usable_facts(facts)}


def ledger(facts: list[DiagnosticFact]) -> str:
    """The block injected back into the turn. Kept to one line per fact: it is
    re-sent on every loop, and a block long enough to skim past is a block that
    gets skimmed past."""
    if not facts:
        return ""
    lines = [
        "EVIDENCE LEDGER (machine-typed from the tool payloads, not from your summary):",
        *(f.line() for f in facts),
    ]
    usable = usable_facts(facts)
    domains = sorted(independent_domains(facts))
    lines.append(
        f"usable: {len(usable)}/{len(facts)} across {len(domains)} "
        f"independent source(s) {domains or '[]'}. "
        "role is a hint from which store answered, not proof it tested that role."
    )
    if not usable:
        lines.append(
            "Nothing usable yet. Do NOT state a root cause or quote any number: "
            "every result so far was empty, failed, or was reference material. "
            "Run a different query, or say plainly that you found no evidence."
        )
    return "\n".join(lines)


# A conclusion is only checked when it reads like one. These are the shapes an
# answer takes when it commits: a named cause, or a quantity. Deliberately dumb
# and explainable — see refutation.py on why a similarity score is not something
# you can say out loud to whoever is on call.
_CLAIM_MARKERS = (
    "root cause",
    "根因",
    "caused by",
    "導致",
    "因為",
    "due to",
    "the culprit",
    "confirmed",
    "確認",
)


def grounding_check(answer: str, facts: list[DiagnosticFact]) -> tuple[bool, str]:
    """Refuse a committed conclusion drawn from nothing.

    Returns (True, "") when the answer is allowed to stand. The bar is
    deliberately at the floor — *zero* usable observations — because anything
    stricter needs a hypothesis bound to each step, which this layer does not
    have. It catches exactly the benchmark's recorded failure: four queries come
    back empty and the run still reports a precise number.
    """
    if not facts or usable_facts(facts):
        return True, ""
    low = answer.lower()
    has_claim = any(m in low for m in _CLAIM_MARKERS)
    if not (has_claim or _has_quantity(answer)):
        return True, ""
    logger.warning(
        "facts: answer commits to a conclusion with 0 usable observations (%d facts)",
        len(facts),
    )
    reasons = ", ".join(sorted({f"{f.tool}:{f.disposition}" for f in facts}))
    return False, (
        "Every observation this turn was unusable as evidence "
        f"({reasons}), yet your answer states a conclusion or quotes a number. "
        "Rewrite it: say which checks you ran, that each returned nothing usable, "
        "and what the on-call should check next. Do NOT name a root cause and do "
        "NOT quote any figure you did not read off a non-empty tool result."
    )


def _has_quantity(answer: str) -> bool:
    """A digit that is doing work. Bare list numbering ('1.') and version-ish
    tokens are not claims, and flagging them would train whoever reads these
    warnings to ignore them."""
    return bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|ms|s\b|rps|req|次|筆|條|個)", answer))
