"""Action-request lifecycle (step 7 後半 §2.1 / 7b-1) — the state machine that
turns a governance Decision into a tracked, human-gated (or autonomous) execution
request.

A governance verdict on its own is ephemeral; to act on it safely we need a
durable object with an auditable status: proposed → approved/rejected/expired →
executing → terminal. Status transitions go through the store's atomic
compare-and-set (`ar_transition`), so two approvals — or an approval racing an
AUTO path — can never both execute the same request.

This module owns *what state a request is in and how it legally moves*; the
executor (execution.py) owns *what happens during executing*; the registry +
kill switch (actions.py) own *whether anything actually mutates*. In 7b-1 the
executor is wired but the kill switch keeps every execution a refusal — so this
state machine is exercised end to end while nothing touches cluster state.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import audit, store
from .config import settings
from .governance import Autonomy, Decision

logger = logging.getLogger("aiops_agent.action_requests")


class Status(str, Enum):
    PROPOSED = "proposed"          # awaiting a human decision (PROPOSE band)
    APPROVED = "approved"          # cleared to execute (human or AUTO)
    REJECTED = "rejected"          # human declined
    EXPIRED = "expired"            # approval TTL passed before action — preconditions stale
    EXECUTING = "executing"        # executor running (transient)
    SUCCEEDED = "succeeded"        # executed + verified (7b-4+)
    FAILED = "failed"              # executed but errored (7b-4+)
    VERIFY_FAILED = "verify_failed"  # executed cleanly but symptom persists (7b-4+)
    ABORTED = "aborted"            # a pre-execution gate refused (preconditions / blast radius)
    REFUSED = "refused"            # kill switch off / no impl — the 7b-1 terminal
    ROLLING_BACK = "rolling_back"  # (7b-4+)
    ROLLED_BACK = "rolled_back"    # (7b-4+)
    ROLLBACK_FAILED = "rollback_failed"  # (7b-4+)


class ActionRequest(BaseModel):
    request_id: str
    fp: str
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    autonomy: str
    status: str
    reversible: bool = False
    rollback: dict[str, Any] | None = None
    blast_radius: dict[str, Any] | None = None
    # Source runbook + incident params, so the executor can re-run the read-only
    # diagnostics and confirm the preconditions still hold before acting (7b-2).
    runbook_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    # action|target|fp — idempotency key so an alert storm can't act on the same
    # target twice for one incident (7b-3).
    idem_key: str = ""
    created_ts: str
    expires_ts: str
    actor: str | None = None
    outcome: str = ""


def target_of(args: dict | None) -> str:
    """Canonical "<namespace>/<deployment>" target string for an action's args —
    the object the action touches. Used for the idempotency key and the breaker
    scope so both name the same thing."""
    args = args or {}
    ns = args.get("namespace") or settings.k8s_namespace
    return f"{ns}/{args.get('deployment', '')}"


def _now() -> datetime:
    return datetime.now(UTC)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def create_from_decision(
    fp: str, decision: Decision, *,
    args: dict | None = None, rollback: dict | None = None,
    runbook_id: str | None = None, params: dict | None = None,
    path: Path | None = None,
) -> ActionRequest | None:
    """Materialize a request from a governance Decision. ESCALATE produces no
    request (no actionable button — it's handed to a human as-is). AUTO becomes
    APPROVED only when the kill switch is on; otherwise (AUTO-but-disabled, or
    PROPOSE) it starts PROPOSED for a human to approve. Best-effort."""
    if decision.autonomy is Autonomy.ESCALATE:
        return None
    try:
        now = _now()
        auto_ok = decision.autonomy is Autonomy.AUTO and settings.actions_enabled
        status = Status.APPROVED if auto_ok else Status.PROPOSED
        req = ActionRequest(
            request_id=uuid.uuid4().hex[:16],
            fp=fp,
            action=decision.action,
            args=args or {},
            autonomy=decision.autonomy.value,
            status=status.value,
            reversible=decision.reversible,
            rollback=rollback,
            runbook_id=runbook_id,
            params=params or {},
            idem_key=f"{decision.action}|{target_of(args)}|{fp}",
            created_ts=_fmt(now),
            expires_ts=_fmt(now + timedelta(seconds=settings.approval_ttl_seconds)),
            actor="system" if auto_ok else None,
        )
        store.ar_insert(req.model_dump(), path)
        audit.record(
            "proposed", "ok", request_id=req.request_id, fp=fp,
            actor=req.actor or "system",
            detail={"action": req.action, "autonomy": req.autonomy,
                    "initial_status": status.value, "reversible": req.reversible},
            path=path,
        )
        return req
    except Exception as e:
        logger.warning("create_from_decision failed for %s: %s", decision.action, e)
        return None


def get(request_id: str, path: Path | None = None) -> ActionRequest | None:
    d = store.ar_get(request_id, path)
    return ActionRequest.model_validate(d) if d else None


def list_requests(*, status: str | None = None, limit: int = 50,
                  path: Path | None = None) -> list[dict[str, Any]]:
    return store.ar_list(status=status, limit=limit, path=path)


def _expire_if_stale(req: ActionRequest, path: Path | None = None) -> bool:
    """If a proposed request is past its TTL, atomically expire it. Returns True
    if it was expired (so callers stop)."""
    if req.status != Status.PROPOSED.value:
        return False
    if _now() <= _parse(req.expires_ts):
        return False
    if store.ar_transition(req.request_id, Status.PROPOSED.value, Status.EXPIRED.value,
                           outcome="approval TTL elapsed before action", path=path):
        audit.record("expired", "ok", request_id=req.request_id, fp=req.fp,
                     detail={"expires_ts": req.expires_ts}, path=path)
    return True


def approve(request_id: str, actor: str, path: Path | None = None) -> ActionRequest | None:
    """Human (or system) approves a proposed request. Returns the approved request,
    or None if it wasn't approvable (missing / expired / already decided — the
    atomic CAS guarantees only one approval wins)."""
    req = get(request_id, path)
    if req is None:
        return None
    if _expire_if_stale(req, path):
        return None
    if not store.ar_transition(request_id, Status.PROPOSED.value, Status.APPROVED.value,
                               actor=actor, path=path):
        audit.record("approved", "abort", request_id=request_id, fp=req.fp, actor=actor,
                     detail={"reason": f"not in proposed state (was {req.status})"}, path=path)
        return None
    audit.record("approved", "ok", request_id=request_id, fp=req.fp, actor=actor, path=path)
    return get(request_id, path)


def reject(request_id: str, actor: str, path: Path | None = None) -> ActionRequest | None:
    req = get(request_id, path)
    if req is None:
        return None
    if not store.ar_transition(request_id, Status.PROPOSED.value, Status.REJECTED.value,
                               actor=actor, path=path):
        return None
    audit.record("rejected", "ok", request_id=request_id, fp=req.fp, actor=actor, path=path)
    return get(request_id, path)
