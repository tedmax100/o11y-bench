"""Headless investigation store — makes the alert-driven RCA runs visible.

The webhook path runs RCA with no human watching; its conclusion + governance
decisions only ever went to logs / a Grafana annotation / the CE jsonl. This
records each run as a structured row so the plugin can list "what did the agent
investigate, what did it conclude, and what did the governance gate decide" —
the UI surface for the headless work (ARE gap-analysis step 6).

Read-only display: recording here never affects an investigation, and correctness
verdicts live in the CE harness (calibration.py), merged in at list time so there
is one source of truth for "was it right".
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

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


def _path() -> Path:
    return Path(settings.investigations_log_path)


def record_investigation(fp: str, alert: dict, result: dict, path: Path | None = None) -> None:
    """Append a row for a finished headless run. Best-effort — never raises."""
    if not settings.investigations_enabled:
        return
    try:
        labels = alert.get("labels") or {}
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
                    action=d.action, autonomy=d.autonomy.value,
                    reason=d.reason, requires_human=d.requires_human,
                )
                for d in decisions
            ],
            answer=(result.get("answer") or "")[:2000],
        )
        p = path or _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(rec.model_dump_json() + "\n")
    except Exception as e:
        logger.warning("record_investigation failed for %s: %s", fp, e)


def _load(path: Path | None = None) -> list[InvestigationRecord]:
    p = path or _path()
    if not p.exists():
        return []
    out: list[InvestigationRecord] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(InvestigationRecord.model_validate_json(line))
        except Exception as e:
            logger.warning("skipping malformed investigation line: %s", e)
    return out


def list_investigations(limit: int = 50, path: Path | None = None) -> list[dict[str, Any]]:
    """Most-recent-first list, with the CE correctness verdict merged in. The
    same fp can appear once per run; we keep the latest per fp."""
    records = _load(path)
    # latest record per fingerprint
    by_fp: dict[str, InvestigationRecord] = {}
    for r in records:
        by_fp[r.fp] = r  # later lines win (file is append order = chronological)
    latest = sorted(by_fp.values(), key=lambda r: r.ts, reverse=True)[:limit]

    # merge correctness from the CE harness (single source of truth)
    try:
        from .calibration import load_records

        verdict = {}
        for cr in load_records():
            if cr.correct is not None:
                verdict[cr.run_id] = cr.correct
    except Exception:
        verdict = {}

    out = []
    for r in latest:
        d = r.model_dump()
        d["correct"] = verdict.get(r.fp)
        out.append(d)
    return out
