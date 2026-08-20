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
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import audit, case_memory, store
from .config import settings
from .governance import Autonomy, Decision

logger = logging.getLogger("aiops_agent.action_requests")


class Status(StrEnum):
    PROPOSED = "proposed"  # awaiting a human decision (PROPOSE band)
    APPROVED = "approved"  # cleared to execute (human or AUTO)
    REJECTED = "rejected"  # human declined
    EXPIRED = "expired"  # approval TTL passed before action — preconditions stale
    EXECUTING = "executing"  # executor running (transient)
    SUCCEEDED = "succeeded"  # executed + verified (7b-4+)
    FAILED = "failed"  # executed but errored (7b-4+)
    VERIFY_FAILED = "verify_failed"  # executed cleanly but symptom persists (7b-4+)
    ABORTED = "aborted"  # a pre-execution gate refused (preconditions / blast radius)
    REFUSED = "refused"  # kill switch off / no impl — the 7b-1 terminal
    ROLLING_BACK = "rolling_back"  # (7b-4+)
    ROLLED_BACK = "rolled_back"  # (7b-4+)
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
    # The investigation run that produced this proposal. Without it the
    # executor's own verification could only label "the latest run of this
    # alert", which is not necessarily the run whose reasoning is being acted on.
    run_id: str | None = None
    # The incident it belongs to, so a decision made on this proposal can be
    # remembered without waiting for the run to finish writing itself down.
    case_key: str | None = None
    created_ts: str
    expires_ts: str
    actor: str | None = None
    outcome: str = ""
    # Why the human decided the way they did. Only ever set by a person.
    decision_note: str | None = None


def target_of(args: dict | None) -> str:
    """Canonical "<namespace>/<deployment>" target string for an action's args —
    the object the action touches. Used for the idempotency key and the breaker
    scope so both name the same thing."""
    args = args or {}
    ns = args.get("namespace") or settings.k8s_namespace
    # Deployment-shaped actions name a deployment; the flag action names a
    # ConfigMap and a flag inside it. Falling through to the empty string gave
    # every ConfigMap action the target "demo/", which is not a typo in a log —
    # it is one breaker scope and one idempotency key shared by every flag on
    # every map in the namespace, so tripping the breaker on one would gag the
    # rest, and two different flips would look like a retry of each other.
    if "deployment" in args:
        return f"{ns}/{args['deployment']}"
    if "configmap" in args:
        flag = args.get("flag")
        cm = args["configmap"]
        return f"{ns}/{cm}#{flag}" if flag else f"{ns}/{cm}"
    return f"{ns}/{args.get('deployment', '')}"


def _now() -> datetime:
    return datetime.now(UTC)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def create_from_decision(
    fp: str,
    decision: Decision,
    *,
    args: dict | None = None,
    rollback: dict | None = None,
    runbook_id: str | None = None,
    params: dict | None = None,
    blast_radius: dict | None = None,
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
            # The footprint is computed when the proposal is made, not when it is
            # approved: a suggestion whose size is only known after you agree to
            # it isn't a suggestion, it's a surprise. The executor still re-runs
            # the dry-run before acting (the cluster moves between the two).
            blast_radius=blast_radius,
            idem_key=f"{decision.action}|{target_of(args)}|{fp}",
            # Pulled from the ambient scope rather than added to this signature:
            # every caller is inside the investigation that produced the
            # decision, and none of them has a reason to know about run ids.
            run_id=(sc.run_id if (sc := case_memory.current_scope()) else None),
            case_key=(sc.case_key if sc else None),
            created_ts=_fmt(now),
            expires_ts=_fmt(now + timedelta(seconds=settings.approval_ttl_seconds)),
            actor="system" if auto_ok else None,
        )
        store.ar_insert(req.model_dump(), path)
        audit.record(
            "proposed",
            "ok",
            request_id=req.request_id,
            fp=fp,
            actor=req.actor or "system",
            detail={
                "action": req.action,
                "autonomy": req.autonomy,
                "initial_status": status.value,
                "reversible": req.reversible,
            },
            path=path,
        )
        return req
    except Exception as e:
        logger.warning("create_from_decision failed for %s: %s", decision.action, e)
        return None


def get(request_id: str, path: Path | None = None) -> ActionRequest | None:
    d = store.ar_get(request_id, path)
    return ActionRequest.model_validate(d) if d else None


def list_requests(
    *, status: str | None = None, limit: int = 50, path: Path | None = None
) -> list[dict[str, Any]]:
    return store.ar_list(status=status, limit=limit, path=path)


def _expire_if_stale(req: ActionRequest, path: Path | None = None) -> bool:
    """If a proposed request is past its TTL, atomically expire it. Returns True
    if it was expired (so callers stop)."""
    if req.status != Status.PROPOSED.value:
        return False
    if _now() <= _parse(req.expires_ts):
        return False
    if store.ar_transition(
        req.request_id,
        Status.PROPOSED.value,
        Status.EXPIRED.value,
        outcome="approval TTL elapsed before action",
        path=path,
    ):
        audit.record(
            "expired",
            "ok",
            request_id=req.request_id,
            fp=req.fp,
            detail={"expires_ts": req.expires_ts},
            path=path,
        )
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
    if not store.ar_transition(
        request_id, Status.PROPOSED.value, Status.APPROVED.value, actor=actor, path=path
    ):
        audit.record(
            "approved",
            "abort",
            request_id=request_id,
            fp=req.fp,
            actor=actor,
            detail={"reason": f"not in proposed state (was {req.status})"},
            path=path,
        )
        return None
    audit.record("approved", "ok", request_id=request_id, fp=req.fp, actor=actor, path=path)
    return get(request_id, path)


def reconcile(path: Path | None = None) -> dict[str, Any]:
    """Let time move the state machine, instead of only people.

    Every transition in this module used to require somebody to knock: a
    proposal expired only when a human tried to approve it, and a request that
    reached `executing` had no way back if the process running it died. Neither
    is harmless once the kill switch is on. The first means the on-call sees a
    seven-hour-old suggestion presented as current, and they are the one person
    without the context to know better. The second means a change is recorded as
    in-flight forever, which is the one status that makes the idempotency key
    refuse every retry.

    Two rules, and the second one is deliberately conservative:

      - `proposed` past its TTL → `expired`. Safe: nothing has run.
      - `executing` past `executing_timeout_seconds` → `failed`. **No rollback
        is attempted.** We do not know whether the write landed before the
        executor vanished, and a background job that guesses in that situation
        can turn a maybe-nothing-happened into a definitely-something-happened.
        Reconciliation may only make the record honest; deciding what to do
        about a half-known change stays with a human.

    Everything it does is written to the audit log under actor `reconciler`, so
    a status that changed with nobody watching is still attributable — otherwise
    this becomes the second invisible actor in a system built to not have one.
    """
    now = _now()
    changed: dict[str, Any] = {"expired": [], "abandoned": [], "checked": 0}

    for row in store.ar_list(status=Status.PROPOSED.value, limit=1000, path=path):
        changed["checked"] += 1
        try:
            if now <= _parse(row["expires_ts"]):
                continue
        except Exception:
            continue  # unparseable timestamp: leave it alone rather than guess
        if store.ar_transition(
            row["request_id"],
            Status.PROPOSED.value,
            Status.EXPIRED.value,
            outcome="approval TTL elapsed before action (reconciler)",
            path=path,
        ):
            changed["expired"].append(row["request_id"])
            audit.record(
                "expired",
                "ok",
                request_id=row["request_id"],
                fp=row.get("fp", ""),
                actor="reconciler",
                detail={"expires_ts": row["expires_ts"]},
                path=path,
            )

    cutoff = now - timedelta(seconds=settings.executing_timeout_seconds)
    for row in store.ar_list(status=Status.EXECUTING.value, limit=1000, path=path):
        changed["checked"] += 1
        try:
            # created_ts is the honest lower bound we have for "how long has this
            # been running" — a claim timestamp would be better, and its absence
            # is why the timeout has to be generous.
            if _parse(row["created_ts"]) > cutoff:
                continue
        except Exception:
            continue
        if store.ar_transition(
            row["request_id"],
            Status.EXECUTING.value,
            Status.FAILED.value,
            outcome=(
                "executor never reported back (process restart?); "
                "whether the change landed is unknown — no rollback attempted"
            ),
            path=path,
        ):
            changed["abandoned"].append(row["request_id"])
            audit.record(
                "abandoned",
                "abort",
                request_id=row["request_id"],
                fp=row.get("fp", ""),
                actor="reconciler",
                detail={"created_ts": row["created_ts"], "rollback_attempted": False},
                path=path,
            )

    if changed["expired"] or changed["abandoned"]:
        logger.warning(
            "reconciler: expired=%d abandoned=%d",
            len(changed["expired"]),
            len(changed["abandoned"]),
        )
    return changed


def reject(
    request_id: str, actor: str, reason: str = "", path: Path | None = None
) -> ActionRequest | None:
    """A human declines a proposed action, and says why.

    `reason` is optional at the API and durable once given: it is the only
    channel through which "we don't do that here" reaches the next
    investigation. Without it a rejection was a fact about one request; with it
    it becomes a fact about the incident.
    """
    req = get(request_id, path)
    if req is None:
        return None
    # Same TTL check as approve(): without it, two equally stale proposals end up
    # telling different stories — one becomes `expired` with a reason, the other
    # `rejected` with a person's name on it, and the audit log now says a human
    # declined something that had already lapsed.
    if _expire_if_stale(req, path):
        return None
    if not store.ar_transition(
        request_id,
        Status.PROPOSED.value,
        Status.REJECTED.value,
        actor=actor,
        decision_note=reason or None,
        path=path,
    ):
        return None
    audit.record(
        "rejected",
        "ok",
        request_id=request_id,
        fp=req.fp,
        actor=actor,
        detail={"reason": reason} if reason else None,
        path=path,
    )
    # Best effort, and after the transition: the decision is the durable part,
    # remembering it is not allowed to fail it.
    verdict = case_memory.remember_rejected_action(
        case_key=req.case_key,
        run_id=req.run_id,
        action=req.action,
        target=target_of(req.args),
        reason=reason,
        actor=actor,
        path=path,
    )
    logger.info("rejection of %s remembered: %s", request_id, verdict)
    return get(request_id, path)
