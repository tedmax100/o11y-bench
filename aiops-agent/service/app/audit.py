"""Audit log (step 7 後半 §5) — the append-only, who/when/what record of every
action-request lifecycle transition.

This is the forensic + accountability core of the execution plane: for any
proposed remediation it captures who approved it, what the preconditions were,
what (if anything) actually ran, the outcome, and whether it was rolled back.
Entries are **insert-only** (the `audit` table is never updated or deleted), so
the trail can't be rewritten after the fact. Recording is best-effort — an audit
write must never break the lifecycle it's describing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import store

logger = logging.getLogger("aiops_agent.audit")


class AuditEntry(BaseModel):
    ts: str
    request_id: str = ""
    fp: str = ""
    phase: str  # proposed/approved/rejected/expired/execute/verify/rollback/...
    verdict: str  # ok / abort / refuse / start / success / fail
    actor: str = "system"
    detail: dict[str, Any] = Field(default_factory=dict)


def record(
    phase: str,
    verdict: str,
    *,
    request_id: str = "",
    fp: str = "",
    actor: str = "system",
    detail: dict | None = None,
    path: Path | None = None,
) -> None:
    """Append one immutable audit entry. Best-effort; never raises."""
    try:
        entry = AuditEntry(
            ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            request_id=request_id,
            fp=fp,
            phase=phase,
            verdict=verdict,
            actor=actor,
            detail=detail or {},
        )
        store.audit_insert(entry.model_dump(), path)
    except Exception as e:
        logger.warning("audit record failed (%s/%s): %s", phase, verdict, e)


def history(
    *,
    request_id: str | None = None,
    fp: str | None = None,
    limit: int = 200,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Chronological audit trail, optionally scoped to a request or fingerprint."""
    return store.audit_list(request_id=request_id, fp=fp, limit=limit, path=path)
