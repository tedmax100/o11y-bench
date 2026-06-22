import asyncio
import json
import uuid

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import action_requests, audit, breaker, execution
from .agent import lifespan, stream_chat
from .alerts import AlertProvisioningDisabled, AlertSpec, build_alert_rule, provision_alert
from .calibration import label_run
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
    return {"ok": True}


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
    return {"investigations": list_investigations(limit=limit)}


class LabelRequest(BaseModel):
    correct: bool
    error_dimension: str | None = None   # root_cause | scope | action | other
    correction_note: str | None = None


# Strong refs for re-investigation background tasks.
_reinvestigation_tasks: set[asyncio.Task] = set()


@app.post("/investigations/{fp}/label")
async def investigations_label(fp: str, req: LabelRequest):
    """Record the correctness verdict for an investigation (closes the CE loop
    from the UI). When correct=False, kicks off a re-investigation in the same
    thread with the human correction injected as context."""
    ok = label_run(
        fp, correct=req.correct, source="ui",
        error_dimension=req.error_dimension,
        correction_note=req.correction_note,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"no calibration record for fingerprint {fp}")

    reinvestigating = False
    if not req.correct:
        inv = get_investigation(fp)
        if inv is not None:
            task = asyncio.create_task(
                reinvestigate(fp, inv.alert, req.error_dimension, req.correction_note)
            )
            _reinvestigation_tasks.add(task)
            task.add_done_callback(_reinvestigation_tasks.discard)
            reinvestigating = True

    return {"ok": True, "fp": fp, "correct": req.correct, "reinvestigating": reinvestigating}


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


def _spawn_executor(request_id: str) -> None:
    task = asyncio.create_task(execution.run(request_id))
    _executor_tasks.add(task)
    task.add_done_callback(_executor_tasks.discard)


class ActorRequest(BaseModel):
    actor: str = "operator"


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
        raise HTTPException(status_code=409,
                            detail="request not approvable (missing, expired, or already decided)")
    _spawn_executor(request_id)
    return req.model_dump()


@app.post("/actions/requests/{request_id}/reject")
async def actions_request_reject(request_id: str, body: ActorRequest):
    req = action_requests.reject(request_id, actor=body.actor)
    if req is None:
        raise HTTPException(status_code=409,
                            detail="request not rejectable (missing or already decided)")
    return req.model_dump()


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
    remediation_labels = [r for r in recs if r.source in ("remediation-verified", "remediation-failed")]
    verified_count = sum(1 for r in remediation_labels if r.correct is True)
    failed_count = sum(1 for r in remediation_labels if r.correct is False)

    return {
        "per_action": by_action,
        "remediation_ce_labels": {
            "verified": verified_count,
            "failed": failed_count,
            "total": len(remediation_labels),
            "note": (
                "learn_remediation_into_ce=True" if settings.learn_remediation_into_ce
                else "learn_remediation_into_ce=False (labels not written to CE headline stream)"
            ),
        },
    }


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
