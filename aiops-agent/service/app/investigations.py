"""Headless investigation store — makes the alert-driven RCA runs visible.

The webhook path runs RCA with no human watching; its conclusion + governance
decisions only ever went to logs / a Grafana annotation / the CE jsonl. This
records each run as a structured row so the plugin can list "what did the agent
investigate, what did it conclude, and what did the governance gate decide" —
the UI surface for the headless work (ARE gap-analysis step 6).

Read-only display: recording here never affects an investigation, and correctness
verdicts live in the CE harness (calibration.py), merged in at list time so there
is one source of truth for "was it right".

Rows live in the durable SQLite store (`app.store`) — same reason as the CE
harness: the ephemeral pod filesystem would lose them on every restart (7b-0).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import case_memory, store
from .audit import current_trace_id
from .config import settings

logger = logging.getLogger("aiops_agent.investigations")


class DecisionRow(BaseModel):
    action: str
    autonomy: str
    reason: str
    requires_human: bool


class InvestigationRecord(BaseModel):
    fp: str = Field(description="alert fingerprint = thread_id of the run")
    ts: str
    alertname: str | None = None
    service: str | None = None
    git_version: str | None = None
    summary: str = ""
    hypothesis: str = ""
    confidence: float = 0.0
    suspected_version: str | None = None
    services: list[str] = Field(default_factory=list)
    decisions: list[DecisionRow] = Field(default_factory=list)
    answer: str = ""
    # filled in at list time from the CE harness; None = not yet judged
    correct: bool | None = None
    # original alert payload — needed to re-run investigation on Wrong label
    alert: dict = Field(default_factory=dict)
    # "alert" (the webhook fired it) or "chat" (a human asked in Grafana). Both
    # go through the same graph; only the kickoff differs, and the plugin wants
    # to show which is which.
    source: str = "alert"
    # The trace of the run that produced this row. The reasoning was always
    # recorded (auto-instrumentation traces every node, tool call and prompt);
    # without this field there was no way to find it from the conclusion.
    trace_id: str | None = None
    # Identity of this invocation. `fp` groups by alert *instance*, so N runs of
    # one alert shared it — which is how a single verdict ended up attached to
    # nine different conclusions. New rows carry their own id.
    run_id: str | None = None
    # Which incident this run belongs to: alertname + service, no version. See
    # store.case_key().
    case_key: str | None = None


def record_investigation(
    fp: str, alert: dict, result: dict, path: Path | None = None, source: str = "alert"
) -> None:
    """Append a row for a finished headless run. Best-effort — never raises."""
    if not settings.investigations_enabled:
        return
    try:
        labels = alert.get("labels") or {}
        scope = case_memory.current_scope()
        findings = result.get("findings")
        decisions = result.get("decisions") or []
        rec = InvestigationRecord(
            fp=fp,
            ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            alertname=labels.get("alertname"),
            service=labels.get("service_name") or labels.get("service"),
            git_version=labels.get("git_version"),
            summary=getattr(findings, "summary", "") or "",
            hypothesis=getattr(findings, "hypothesis", "") or "",
            confidence=float(getattr(findings, "confidence", 0.0) or 0.0),
            suspected_version=getattr(findings, "suspected_version", None),
            services=list(getattr(findings, "services", []) or []),
            decisions=[
                DecisionRow(
                    action=d.action,
                    autonomy=d.autonomy.value,
                    reason=d.reason,
                    requires_human=d.requires_human,
                )
                for d in decisions
            ],
            answer=(result.get("answer") or "")[:2000],
            alert={k: v for k, v in alert.items() if k != "_correction_hint"},
            trace_id=current_trace_id(),
            source=source,
            run_id=scope.run_id if scope else None,
            case_key=scope.case_key if scope else None,
        )
        store.inv_insert(
            rec.fp,
            rec.ts,
            rec.model_dump_json(),
            path,
            run_id=rec.run_id,
            case_key=rec.case_key,
        )
        # Count the attention, not the knowledge: a finished investigation is an
        # occurrence of the case, never a conclusion about it.
        if scope:
            case_memory.observe(scope, path=path)
    except Exception as e:
        logger.warning("record_investigation failed for %s: %s", fp, e)


def _load(path: Path | None = None) -> list[InvestigationRecord]:
    out: list[InvestigationRecord] = []
    for payload in store.inv_load(path):
        try:
            out.append(InvestigationRecord.model_validate_json(payload))
        except Exception as e:
            logger.warning("skipping malformed investigation row: %s", e)
    return out


def get_investigation(fp: str, path: Path | None = None) -> InvestigationRecord | None:
    """Return the most recent investigation record for a fingerprint, or None."""
    records = _load(path)
    matches = [r for r in records if r.fp == fp]
    return matches[-1] if matches else None


def list_investigations(limit: int = 50, path: Path | None = None) -> list[dict[str, Any]]:
    """Most-recent-first list, with the CE correctness verdict merged in. The
    same fp can appear once per run; we keep the latest per fp."""
    records = _load(path)
    # latest record per fingerprint
    by_fp: dict[str, InvestigationRecord] = {}
    for r in records:
        by_fp[r.fp] = r  # later lines win (file is append order = chronological)
    latest = sorted(by_fp.values(), key=lambda r: r.ts, reverse=True)[:limit]

    # Merge correctness from the CE harness (single source of truth). `path` is
    # threaded through deliberately: reading the rows from one store and the
    # verdicts from another is the same seam that made the past-incident JOIN
    # silently empty — the tables were fine, they just lived in different files.
    try:
        from .calibration import load_records

        verdict = {}
        for cr in load_records(path):
            if cr.correct is not None:
                verdict[cr.run_id] = cr.correct
    except Exception:
        verdict = {}

    out = []
    for r in latest:
        d = r.model_dump()
        # By run first, by fingerprint only for rows written before runs had
        # their own id. The fallback is what the old code did for everything,
        # which is how a verdict on one run showed up on eight others.
        d["correct"] = verdict.get(r.run_id) if r.run_id else None
        if d["correct"] is None:
            d["correct"] = verdict.get(r.fp)
        out.append(d)
    return out
