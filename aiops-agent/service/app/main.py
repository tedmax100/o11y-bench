import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import action_requests, actions, agent, audit, breaker, execution, governance, store
from .agent import lifespan, stream_chat
from .alerts import AlertProvisioningDisabled, AlertSpec, build_alert_rule, provision_alert
from .calibration import CULPRIT, INCONCLUSIVE, default_grading_mode, label_run
from .config import settings
from .investigations import get_investigation, list_investigations
from .traces import analyze_trace, get_trace, list_traces, stream_trace_chat
from .webhook import handle_alert, reinvestigate

app = FastAPI(title="aiops-agent-service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None
    # Set when the user picked a service from the clarify menu; skips resolution.
    service_hint: str | None = None


@app.get("/healthz")
async def healthz():
    # `store` is here so "which database is this pod actually reading" is a curl
    # away. Two same-named stores on different mounts disagreed for weeks and
    # nothing surfaced it; identity you have to shell in to discover is identity
    # nobody checks.
    # Readiness is the *cached* verdict on purpose — /actions/readiness is the
    # live probe. What belongs on a health endpoint is "is the standing signal
    # fresh and green", which is exactly what a stale age tells you.
    from .signals.actuation import actuation_verdict

    return {"ok": True, "store": store.describe(), "actuation": actuation_verdict()}


@app.post("/chat")
async def chat(req: ChatRequest):
    thread_id = req.thread_id or str(uuid.uuid4())

    async def event_gen():
        yield {"event": "thread", "data": json.dumps({"thread_id": thread_id})}
        async for evt in stream_chat(req.message, thread_id, req.service_hint):
            yield {"event": evt["type"], "data": json.dumps(evt)}

    return EventSourceResponse(event_gen())


# ---- Alert webhook (PUSH-mode RCA) ------------------------------------------


@app.post("/webhook/alert")
async def webhook_alert(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
):
    """Grafana Alerting POSTs firing alerts here; each distinct alert kicks off a
    headless RCA (doc v3 §4). fail-closed: disabled unless a secret is configured,
    and the request must present it (header or ?token=)."""
    if not settings.webhook_secret:
        raise HTTPException(status_code=503, detail="alert webhook disabled (no secret configured)")
    token = x_webhook_secret or request.query_params.get("token")
    if token != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="invalid webhook secret")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body must be JSON")

    return await handle_alert(payload)


# ---- Headless investigations (plugin visibility) ----------------------------


@app.get("/investigations")
async def investigations_list(limit: int = 50):
    """Recent alert-driven RCA runs with their conclusion + governance decisions,
    most recent first. Read-only."""
    return {"investigations": list_investigations(limit=limit), "store": store.describe()}


class LabelRequest(BaseModel):
    correct: bool
    error_dimension: str | None = None  # root_cause | scope | action | other
    correction_note: str | None = None
    # Which ruler the verdict is on. Omitted means "decide it from the run" —
    # see calibration.default_grading_mode. A caller may still say it outright.
    grading_mode: str | None = None


# Strong refs for re-investigation and draft-runbook background tasks.
_reinvestigation_tasks: set[asyncio.Task] = set()
_draft_tasks: set[asyncio.Task] = set()


@app.post("/investigations/{fp}/label")
async def investigations_label(fp: str, req: LabelRequest):
    """Record the correctness verdict for an investigation (closes the CE loop
    from the UI). When correct=False, kicks off a re-investigation in the same
    thread with the human correction injected as context. When correct=True and
    no active runbook covers the alert, synthesizes a draft runbook (閉環二)."""
    if req.grading_mode not in (None, CULPRIT, INCONCLUSIVE):
        raise HTTPException(status_code=400, detail=f"unknown grading_mode {req.grading_mode!r}")
    # A human pressing correct/wrong on an investigation that blamed something is
    # judging whether the blame was right — the reading the calibration math
    # assumes. But the same two buttons sit under chat answers that blamed
    # nobody, and "correct" there means "it was right to not blame anyone",
    # which is a different question and a different pool. Deciding it from the
    # run rather than from the button is what keeps a 0.0-confidence refusal out
    # of the culprit curve, where it would score a full 1.0 of calibration gap.
    mode = req.grading_mode or default_grading_mode(fp)
    ok = label_run(
        fp,
        correct=req.correct,
        source="ui",
        error_dimension=req.error_dimension,
        correction_note=req.correction_note,
        grading_mode=mode,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"no calibration record for fingerprint {fp}")

    reinvestigating = False
    draft_queued = False

    if not req.correct:
        inv = get_investigation(fp)
        if inv is not None:
            task = asyncio.create_task(
                reinvestigate(fp, inv.alert, req.error_dimension, req.correction_note)
            )
            _reinvestigation_tasks.add(task)
            task.add_done_callback(_reinvestigation_tasks.discard)
            reinvestigating = True
    else:
        # Closed-loop 二: correct=True → synthesize draft runbook if no active
        # runbook covers this alert. Best-effort background task.
        inv = get_investigation(fp)
        if inv is not None:
            from .draft_runbook import maybe_synthesize_draft

            task = asyncio.create_task(maybe_synthesize_draft(inv))
            _draft_tasks.add(task)
            task.add_done_callback(_draft_tasks.discard)
            draft_queued = True

    return {
        "ok": True,
        "fp": fp,
        "correct": req.correct,
        "reinvestigating": reinvestigating,
        "draft_queued": draft_queued,
    }


# ---- Design-alert capability (propose-only; human button provisions) --------


@app.post("/alerts/preview")
async def alerts_preview(spec: AlertSpec):
    """Dry-run: return the Grafana alert-rule payload this spec would create,
    without writing anything. Lets the plugin card show exactly what the button
    will provision; no Grafana credentials required."""
    return {"payload": build_alert_rule(spec)}


@app.post("/alerts/provision")
async def alerts_provision(spec: AlertSpec):
    """Write a proposed alert rule to Grafana. Reached only from a human button
    click in the plugin (the human-in-the-loop gate). fail-closed: 503 when
    provisioning is switched off or Grafana credentials are absent."""
    try:
        result = await provision_alert(spec)
    except AlertProvisioningDisabled as e:
        raise HTTPException(status_code=503, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"grafana rejected the rule: {e.response.text}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"alert provisioning failed: {e}")
    return {"ok": True, **result}


# ---- Action requests (execution-plane lifecycle; 7b-1) ----------------------
# In 7b-1 approval drives the executor, but the kill switch (actions_enabled)
# keeps every execution a refusal — nothing mutates cluster state yet.

# Strong refs to in-flight executor tasks so the loop doesn't GC them mid-run.
_executor_tasks: set[asyncio.Task] = set()

# Which terminal states really touched the cluster. The definition moved into
# the store, because the same set decides both who may be graded and what the
# AE-SLO's denominator is, and two copies would have let those drift apart.
_GRADABLE_STATUSES = store.GRADABLE_STATUSES


def _spawn_executor(request_id: str) -> None:
    task = asyncio.create_task(execution.run(request_id))
    _executor_tasks.add(task)
    task.add_done_callback(_executor_tasks.discard)


class ActorRequest(BaseModel):
    actor: str = "operator"


class RejectRequest(ActorRequest):
    # Optional, and the one field on this endpoint that outlives the request:
    # it becomes a dead end on the incident, so the next investigation of the
    # same thing is told what was already turned down.
    reason: str = ""


@app.get("/actions")
async def actions_list():
    """What this build can do — the registry's contract, not its wiring.

    `executable` is the pair that decides whether a proposal can ever become an
    execution: an action with no impl is propose-only no matter what the gate
    says, and the kill switch is reported separately because "this build knows
    the action" and "this deployment may run it" are different questions that
    look identical from the outside when both are false.
    """
    specs = []
    for name in actions.registry.names():
        spec = actions.registry.get(name)
        specs.append(
            {
                "name": spec.name,
                "description": spec.description,
                "reversible": spec.reversible,
                "requires_approval": spec.requires_approval,
                "category": spec.category,
                "executable": spec.impl is not None,
                "has_dry_run": spec.dry_run is not None,
            }
        )
    return {"actions": specs, "actions_enabled": settings.actions_enabled}


@app.get("/cases/context")
async def cases_context(service: str, alertname: str | None = None):
    """The recall block exactly as the next run would receive it.

    Deliberately returns the rendered text rather than the rows behind it: the
    rows are queryable elsewhere, and what matters here is the thing that reaches
    a prompt. A caller checking whether an incident learned anything should be
    reading the same string the model reads, not a second rendering of it.
    """
    return {
        "service": service,
        "alertname": alertname,
        "context": agent._past_incident_context(service, alertname),
    }


@app.get("/cases")
async def cases_list(
    service: str | None = None,
    status: str | None = None,
    unlabeled: bool | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Browse case memory. Everything is here — including what recall hides.

    Until now the only way to see what this system had learned was to exec into
    the pod and open the SQLite file, which meant in practice that nobody
    looked. `unlabeled=true` is the query a todo view wants: the cases carrying
    no trusted root cause, i.e. the ones a human could still turn into
    precedent.
    """
    return store.case_list(
        service=service, status=status, unlabeled=unlabeled, limit=limit, offset=offset
    )


@app.get("/cases/{case_key}")
async def cases_get(case_key: str):
    """One case with the two things that explain it: the runs it was made of and
    the paths already ruled out.

    `recallable` is answered by asking the recall query itself rather than by
    re-deriving its conditions here — a second copy of "is this fresh and
    trusted enough" would drift from the one that decides what a prompt
    actually sees, and this endpoint exists to tell the truth about that.
    """
    case = store.case_get(case_key)
    if case is None:
        raise HTTPException(status_code=404, detail=f"no such case {case_key}")
    recallable = any(
        c["case_key"] == case_key
        for c in store.case_query_similar(
            service=case.get("service") or "", alertname=case.get("alertname"), limit=50
        )
    )
    return {
        "case": case,
        "recallable": recallable,
        "runs": store.case_runs(case_key),
        "dead_ends": store.case_dead_ends_all(case_key),
    }


@app.get("/actions/requests")
async def actions_requests_list(status: str | None = None, limit: int = 50):
    """List action requests (optionally filtered by status), newest first."""
    return {"requests": action_requests.list_requests(status=status, limit=limit)}


@app.get("/actions/requests/{request_id}")
async def actions_request_get(request_id: str):
    req = action_requests.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"no such request {request_id}")
    return req.model_dump()


@app.post("/actions/requests/{request_id}/approve")
async def actions_request_approve(request_id: str, body: ActorRequest):
    """Human approves a proposed request (the human-in-the-loop gate). On success
    the executor is kicked off in the background; in 7b-1 it terminates in REFUSED
    because actions_enabled is off. 409 if the request can't be approved (missing,
    expired, or already decided)."""
    req = action_requests.approve(request_id, actor=body.actor)
    if req is None:
        raise HTTPException(
            status_code=409, detail="request not approvable (missing, expired, or already decided)"
        )
    _spawn_executor(request_id)
    return req.model_dump()


@app.post("/actions/requests/{request_id}/reject")
async def actions_request_reject(request_id: str, body: RejectRequest):
    req = action_requests.reject(request_id, actor=body.actor, reason=body.reason)
    if req is None:
        raise HTTPException(
            status_code=409, detail="request not rejectable (missing or already decided)"
        )
    return req.model_dump()


class RootCauseRequest(ActorRequest):
    root_cause: str
    # Which run's reasoning is being blessed, when the person is looking at one.
    # Optional because a root cause can also be known from outside the agent
    # entirely — a postmortem, a vendor's status page — and that is still worth
    # recording; it just names no run.
    run_id: str | None = None


@app.post("/cases/{case_key}/root-cause")
async def case_set_root_cause(case_key: str, body: RootCauseRequest):
    """A person says what actually caused this incident.

    The missing half of case memory. Everything else could already write a root
    cause — the grader, the eval harness, the label path on an investigation —
    but a human looking straight at the case could not, so the queue of
    incidents with no cause had no way to be worked down. `source` is fixed to
    `human` here rather than taken from the request: this endpoint is the human,
    and letting a caller name its own source is exactly how self-attestation
    gets in.
    """
    root_cause = body.root_cause.strip()
    if not root_cause:
        raise HTTPException(status_code=400, detail="root_cause must not be empty")
    ok = store.case_confirm(
        case_key,
        root_cause=root_cause,
        source="human",
        run_id=body.run_id or "",
        ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"no such case {case_key}")
    audit.record(
        "case_root_cause",
        "ok",
        fp=case_key,
        actor=body.actor,
        detail={"root_cause": root_cause, "run_id": body.run_id},
    )
    return store.case_get(case_key)


@app.post("/cases/{case_key}/forget")
async def case_forget(case_key: str, body: ActorRequest):
    """Retract what a case claims to know: its root cause stops being recalled
    and its dead ends are retired.

    The counterpart to everything that writes into case memory. Recall ages out
    on a fixed window, which handles the slow drift and nothing else — when an
    environment is rebuilt or a policy changes on a Tuesday, somebody has to be
    able to say so on that Tuesday rather than wait a month for the cutoff.
    """
    result = store.case_forget(case_key)
    if not result["cases"]:
        raise HTTPException(status_code=404, detail="no such case")
    audit.record("case_forget", "ok", fp=case_key, actor=body.actor, detail=result)
    return {"case_key": case_key, **result}


@app.get("/todo")
async def todo(limit: int = 20):
    """Everything currently waiting on a person, in one call.

    The gap this closes is not a modelling gap. Three of the four things this
    system is supposed to do are blocked on work only a human can do — label a
    run, decide on a proposal, say what actually caused an incident — and none
    of it had an entry point: the proposals expired unread (10 of the first 28),
    the calibration labels stalled at 7 of 30, and the cases sat unlabelled.
    Work nobody can see is work nobody does.

    Read-only and best-effort per section: a signal that cannot be computed
    (no cluster, no store) must not take the whole view down, because the view
    is what tells you something is wrong.
    """
    out: dict[str, Any] = {}

    runs = [r for r in list_investigations(limit=200) if r.get("correct") is None]
    out["investigations_to_label"] = {"count": len(runs), "items": runs[:limit]}

    pending = action_requests.list_requests(status="proposed", limit=200)
    expired = action_requests.list_requests(status="expired", limit=200)
    out["requests_to_decide"] = {
        "count": len(pending),
        "items": pending[:limit],
        # Not a queue — a scoreboard. Every one of these is a proposal a person
        # was asked about and never answered, and that is a fact about the
        # process, not about the agent.
        "expired_unattended": len(expired),
    }

    cases = store.case_list(unlabeled=True, limit=limit)
    out["cases_to_label"] = {"count": cases["total"], "items": cases["cases"]}

    # The fourth queue, added once the AE-SLO's denominator was looked at: nine
    # actions had run and three had a verdict, and the six without one appeared
    # on no list at all. "Did the incident actually end" is the one question in
    # this system that only a person can answer, and it was the only piece of
    # human work with no entry point — which is the same reason the other three
    # queues exist.
    ungraded = store.ungraded_actions(limit=200)
    out["actions_to_grade"] = {"count": len(ungraded), "items": ungraded[:limit]}

    try:
        out["autonomy"] = governance.autonomy_status()
    except Exception as e:  # a broken gate reading must not hide the queues
        out["autonomy"] = {"error": f"{type(e).__name__}: {e}"}

    return out


@app.get("/actions/readiness")
async def actions_readiness():
    """Can the agent still act — as opposed to whether policy would let it.

    Deliberately a live probe rather than a cached read: the whole point is that
    a credential's health is only observable at the moment you use it, so an
    endpoint that reports the last stored answer would reproduce the bug."""
    from .signals.actuation import actuation_verdict, check_actuation

    fit = await check_actuation()
    return {
        "verdict": actuation_verdict(),
        "namespaces": fit.namespaces,
        "in_cluster": fit.in_cluster,
        "missing": fit.missing,
        "excess": fit.excess,
        "error": fit.error,
    }


@app.post("/actions/reconcile")
async def actions_reconcile():
    """Run the state-machine reconciliation pass now. It also runs on a timer;
    this exists so a human can see what it would do without waiting for it, and
    so the check can be asserted in CI."""
    return await asyncio.to_thread(action_requests.reconcile)


@app.get("/actions/audit")
async def actions_audit(request_id: str | None = None, fp: str | None = None, limit: int = 200):
    """Append-only audit trail, optionally scoped to a request or fingerprint."""
    return {"audit": audit.history(request_id=request_id, fp=fp, limit=limit)}


@app.get("/actions/breaker")
async def actions_breaker_state():
    """Currently-open circuit breakers (7b-3)."""
    return {"open": breaker.snapshot()}


class BreakerResetRequest(BaseModel):
    scope: str | None = None  # None resets all; else "action|target" or "global"


@app.post("/actions/breaker/reset")
async def actions_breaker_reset(body: BreakerResetRequest):
    """Human re-closes a tripped breaker (a scope, or all). Breakers stay open
    until this is called — automation can't clear its own trip."""
    cleared = breaker.reset(body.scope)
    return {"ok": True, "cleared": cleared, "scope": body.scope or "all"}


class ActionOutcomeRequest(BaseModel):
    """The on-call's verdict on an action that ran."""

    resolved: bool  # did the incident actually end
    actor: str = "unknown"
    side_effect: bool = False  # did it break something else
    note: str = ""


@app.post("/actions/requests/{request_id}/outcome")
async def actions_grade_outcome(request_id: str, body: ActionOutcomeRequest):
    """Grade one executed action: did it actually resolve the incident.

    This is the authoritative AE-SLO numerator, and it is a *person*, on purpose.
    The pipeline already produces its own opinion — the verify step re-runs a
    query the runbook author wrote and checks one number against a threshold —
    but "the decline rate came back under 0.01" and "the incident is over" are
    different claims, and only the second one is what the SLO says it measures.
    Letting the machine grade its own remediation is also how a system starts
    issuing itself the evidence it needs to be trusted with more autonomy, which
    is why governance counts these labels as human and the verify-derived ones as
    self-produced.

    Only actions that really ran can be graded — grading a refusal would put a
    row in the ledger for something that never touched the cluster.
    """
    from . import store as _store

    req = action_requests.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="no such request")
    if req.status not in _GRADABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"request is {req.status}; only an action that actually ran can be graded "
                f"({', '.join(sorted(_GRADABLE_STATUSES))})"
            ),
        )

    # What the machine concluded, so agreement can be computed instead of assumed.
    verify_said: bool | None = None
    for row in audit.history(request_id=request_id):
        if row["phase"] == "verify" and row["verdict"] in ("pass", "fail"):
            verify_said = row["verdict"] == "pass"

    drill = action_requests.is_drill(req.params)
    _store.action_outcome_put(
        request_id=request_id,
        resolved=body.resolved,
        side_effect=body.side_effect,
        actor=body.actor,
        fp=req.fp,
        action=req.action,
        verify_said=verify_said,
        drill=drill,
        note=body.note,
    )
    audit.record(
        "outcome",
        "resolved" if body.resolved else "not_resolved",
        request_id=request_id,
        fp=req.fp,
        actor=body.actor,
        detail={"side_effect": body.side_effect, "verify_said": verify_said, "note": body.note},
    )
    return {"ok": True, "request_id": request_id, "verify_said": verify_said, "drill": drill}


@app.get("/actions/ae-slo")
async def actions_ae_slo():
    """Action Effectiveness SLO, drills reported separately from incidents, no
    percentage below the reporting floor, and every ratio carrying the share of
    executed actions it was actually computed from."""
    from . import store as _store

    slo = _store.ae_slo()
    slo["proposals"] = _store.proposal_disposition()
    return slo


@app.get("/actions/outcomes/pending")
async def actions_outcomes_pending(limit: int = 50):
    """The grading backlog: actions that ran and that nobody has judged.

    Without this list the missing verdicts had no address — the SLO divided by
    the graded rows, so an execution nobody looked at was indistinguishable from
    an execution that never happened. Same role `cases_to_label` plays for the
    calibration pool.
    """
    from . import store as _store

    rows = _store.ungraded_actions(limit=limit)
    return {"count": len(rows), "pending": rows}


@app.get("/actions/fix-efficacy")
async def actions_fix_efficacy():
    """Per-action fix-efficacy summary (7b-5 Learn). Reads the executions ledger
    to compute success rate per action, and the CE calibration store for the
    remediation-verified/-failed self-label count. Separate from the headline CE
    (which requires human/grader labels) — this is the 'did the fix work' view."""
    from . import store as _store
    from .calibration import load_records

    # Per-action success rate from the executions ledger
    with _store._connect() as conn:
        rows = conn.execute(
            "SELECT action, COUNT(*) as total, SUM(success) as successes "
            "FROM executions GROUP BY action ORDER BY action"
        ).fetchall()
    by_action = [
        {
            "action": r["action"],
            "total": r["total"],
            "successes": int(r["successes"] or 0),
            "success_rate": round(int(r["successes"] or 0) / r["total"], 3) if r["total"] else None,
        }
        for r in rows
    ]

    # Self-label counts from CE calibration store
    recs = load_records()
    remediation_labels = [
        r for r in recs if r.source in ("remediation-verified", "remediation-failed")
    ]
    verified_count = sum(1 for r in remediation_labels if r.correct is True)
    failed_count = sum(1 for r in remediation_labels if r.correct is False)

    return {
        "per_action": by_action,
        "ae_slo": _store.ae_slo(),
        "remediation_ce_labels": {
            "verified": verified_count,
            "failed": failed_count,
            "total": len(remediation_labels),
            "note": (
                "learn_remediation_into_ce=True"
                if settings.learn_remediation_into_ce
                else "learn_remediation_into_ce=False (labels not written to CE headline stream)"
            ),
        },
    }


# ---- Runbook health (knowledge-loop §1 閉環三) ------------------------------


@app.get("/runbooks/health")
async def runbooks_health(days: int = 30):
    """SOP decay report: runbooks with verify_failed rate > 30% or any
    rollback_failed in the past `days` days. Used for periodic SOP review."""
    from . import store as _store

    return {"days": days, "decayed_runbooks": _store.rb_feedback_health_report(days=days)}


# ---- Trace Explorer ---------------------------------------------------------


@app.get("/traces")
async def traces_list(
    service: str | None = None,
    q: str | None = None,
    start: str = "now-1h",
    end: str = "now",
    limit: int = 30,
):
    try:
        return await list_traces(service=service, q=q, start=start, end=end, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"trace search failed: {e}")


@app.get("/traces/{trace_id}")
async def traces_get(trace_id: str):
    try:
        return await get_trace(trace_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"trace fetch failed: {e}")


@app.get("/traces/{trace_id}/analysis")
async def traces_analysis(trace_id: str):
    try:
        return {"analysis": await analyze_trace(trace_id)}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"trace analysis failed: {e}")


class TraceChatRequest(BaseModel):
    message: str
    history: list[dict] | None = None


@app.post("/traces/{trace_id}/chat")
async def traces_chat(trace_id: str, req: TraceChatRequest):
    async def event_gen():
        async for evt in stream_trace_chat(trace_id, req.message, req.history):
            yield {"event": evt["type"], "data": json.dumps(evt)}

    return EventSourceResponse(event_gen())
