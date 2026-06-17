"""Action executor (step 7 後半 §3 / 7b-1) — the coordinator that drives an
approved ActionRequest through the execution pipeline. It is the *only* caller of
`registry.execute()`.

7b-2 adds the two read-only gates (precondition revalidation + blast-radius
policy) before the kill-switched execute. They never mutate, so a happy path
still terminates in REFUSED (impl wired in 7b-4) — but now only after confirming
the runbook's preconditions still hold and the footprint is within policy. A gate
that refuses ends the request in ABORTED.

Pipeline (full shape; ◻ = added in a later phase):
  1. ▣ precondition revalidation (TOCTOU)         — 7b-2
  2. ▣ dry-run + blast-radius gate                — 7b-2
  3. ▣ circuit breaker + idempotency gate         — 7b-3
  4. ▣ registry.execute()  (kill-switched)        — refuses (no impl yet)
  5. ◻ outcome verification                        — 7b-4
  6. ◻ auto-rollback on failure                    — 7b-4
  7. ◻ Learn: outcome → calibration               — 7b-5
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import action_requests as ar
from . import audit, blast_radius, breaker, store
from .actions import ActionDisabled, registry
from .action_requests import ActionRequest, Status
# Module-level so tests can monkeypatch without importing the agent stack.
from .runbook import load_runbooks, run_diagnostics

logger = logging.getLogger("aiops_agent.execution")


def _read_only_tools() -> dict:
    """The agent's all-read-only TOOLS, keyed by name. Imported lazily so the
    executor doesn't pull the LLM stack unless a precondition check needs it."""
    from .agent import TOOLS
    return {t.name: t for t in TOOLS}


async def _revalidate_preconditions(req: ActionRequest, path: Path | None) -> bool:
    """Re-run the source runbook's read-only diagnostics and confirm none of the
    preconditions that were true at decision time has flipped to fail (TOCTOU).
    Aborts only on an explicit `fail` — an error/skip (e.g. a transient probe
    failure) is recorded but doesn't block a human-approved action."""
    if not req.runbook_id:
        audit.record("precondition", "skip", request_id=req.request_id, fp=req.fp,
                     detail={"reason": "no runbook linked"}, path=path)
        return True
    rb = next((b for b in load_runbooks() if b.id == req.runbook_id), None)
    if rb is None or not rb.diagnostics:
        audit.record("precondition", "skip", request_id=req.request_id, fp=req.fp,
                     detail={"reason": f"runbook {req.runbook_id} has no diagnostics"}, path=path)
        return True
    try:
        results = await run_diagnostics(rb, req.params, _read_only_tools())
    except Exception as e:
        audit.record("precondition", "skip", request_id=req.request_id, fp=req.fp,
                     detail={"error": f"{type(e).__name__}: {e}"}, path=path)
        return True
    failed = [r for r in results if r.status == "fail"]
    if failed:
        audit.record("precondition", "abort", request_id=req.request_id, fp=req.fp,
                     detail={"failed": [r.desc for r in failed]}, path=path)
        return False
    audit.record("precondition", "ok", request_id=req.request_id, fp=req.fp,
                 detail={"checked": len(results)}, path=path)
    return True


async def _check_blast_radius(req: ActionRequest, path: Path | None) -> bool:
    """Compute the action's footprint (read-only dry-run), store it for the UI,
    and refuse if it exceeds policy. Fail-closed: a dry-run that can't read the
    cluster, or any error, aborts."""
    spec = registry.get(req.action)
    if spec is None or spec.dry_run is None:
        audit.record("dry_run", "skip", request_id=req.request_id, fp=req.fp,
                     detail={"reason": "no dry-run for action"}, path=path)
        return True
    try:
        br = await spec.dry_run(req.args)
    except Exception as e:
        audit.record("dry_run", "abort", request_id=req.request_id, fp=req.fp,
                     detail={"error": f"{type(e).__name__}: {e}"}, path=path)
        return False
    store.ar_update(req.request_id, blast_radius=br.model_dump(), path=path)
    ok, reason = blast_radius.evaluate_policy(br)
    audit.record("dry_run", "ok" if ok else "abort", request_id=req.request_id, fp=req.fp,
                 detail={"blast_radius": blast_radius.format_blast_radius(br), "reason": reason},
                 path=path)
    return ok


async def run(request_id: str, path: Path | None = None) -> dict:
    """Execute an approved request through the pipeline. Returns a small result
    dict; the authoritative state is the request's status in the store."""
    req = ar.get(request_id, path)
    if req is None:
        return {"status": "not_found"}

    # Atomic claim: approved → executing. If this fails the request wasn't
    # approved (or another worker already claimed it) — never double-execute.
    if not _claim(req.request_id, req.fp, path):
        return {"status": req.status, "outcome": "not in approved state"}

    audit.record("execute", "start", request_id=req.request_id, fp=req.fp,
                 detail={"action": req.action, "args": req.args}, path=path)

    # --- 1. precondition revalidation (7b-2) ---------------------------------
    if not await _revalidate_preconditions(req, path):
        ar_store_transition(req.request_id, Status.EXECUTING, Status.ABORTED,
                            outcome="precondition no longer holds", path=path)
        return {"status": Status.ABORTED.value, "outcome": "precondition no longer holds"}

    # --- 2. dry-run + blast-radius gate (7b-2) -------------------------------
    if not await _check_blast_radius(req, path):
        ar_store_transition(req.request_id, Status.EXECUTING, Status.ABORTED,
                            outcome="blast radius exceeds policy / dry-run unavailable", path=path)
        return {"status": Status.ABORTED.value, "outcome": "blast radius exceeds policy"}

    # --- 3. idempotency + circuit breaker gate (7b-3) ------------------------
    target = ar.target_of(req.args)
    dup = store.ar_find_ran(req.idem_key, req.request_id, path)
    if dup:
        audit.record("idempotency", "abort", request_id=req.request_id, fp=req.fp,
                     detail={"superseded_by": dup, "idem_key": req.idem_key}, path=path)
        ar_store_transition(req.request_id, Status.EXECUTING, Status.ABORTED,
                            outcome=f"idempotent: target already acted on for this incident ({dup})",
                            path=path)
        return {"status": Status.ABORTED.value, "outcome": "idempotent duplicate"}

    allowed, reason = breaker.check(req.action, target, path)
    if not allowed:
        audit.record("breaker", "abort", request_id=req.request_id, fp=req.fp,
                     detail={"reason": reason}, path=path)
        ar_store_transition(req.request_id, Status.EXECUTING, Status.ABORTED,
                            outcome=f"circuit breaker: {reason}", path=path)
        return {"status": Status.ABORTED.value, "outcome": f"circuit breaker: {reason}"}

    # --- 4. execute (kill-switched; refuses until 7b-4 wires an impl) ---------
    try:
        result = await registry.execute(req.action, req.args)
    except (ActionDisabled, KeyError) as e:
        # Expected terminal until 7b-4: the kill switch / missing impl refuses.
        # NOTHING RAN, so this must not feed the breaker (no record_outcome) or the
        # Learn loop — just a clean REFUSED.
        ar_store_transition(req.request_id, Status.EXECUTING, Status.REFUSED,
                            outcome=str(e), path=path)
        audit.record("execute", "refuse", request_id=req.request_id, fp=req.fp,
                     detail={"reason": str(e)}, path=path)
        return {"status": Status.REFUSED.value, "outcome": str(e)}
    except Exception as e:  # the action RAN and errored (7b-4+ territory)
        breaker.record_outcome(req.action, target, fp=req.fp,
                               request_id=req.request_id, success=False, path=path)
        ar_store_transition(req.request_id, Status.EXECUTING, Status.FAILED,
                            outcome=f"{type(e).__name__}: {e}", path=path)
        audit.record("execute", "fail", request_id=req.request_id, fp=req.fp,
                     detail={"error": str(e)}, path=path)
        return {"status": Status.FAILED.value, "outcome": str(e)}

    # --- 5. verify (7b-4) / 6. rollback (7b-4) / 7. Learn (7b-5) -------------
    # Success path is unreachable until 7b-4 (nothing has a wired impl).
    breaker.record_outcome(req.action, target, fp=req.fp,
                           request_id=req.request_id, success=True, path=path)
    ar_store_transition(req.request_id, Status.EXECUTING, Status.SUCCEEDED,
                        outcome="executed", path=path)
    audit.record("execute", "success", request_id=req.request_id, fp=req.fp,
                 detail={"result": str(result)[:500]}, path=path)
    return {"status": Status.SUCCEEDED.value}


# Small wrappers so the store coupling stays in one place and reads cleanly above.
def _claim(request_id: str, fp: str, path: Path | None) -> bool:
    from . import store
    ok = store.ar_transition(request_id, Status.APPROVED.value, Status.EXECUTING.value, path=path)
    if not ok:
        audit.record("execute", "abort", request_id=request_id, fp=fp,
                     detail={"reason": "request not in approved state"}, path=path)
    return ok


def ar_store_transition(request_id: str, expect: Status, to: Status, *,
                        outcome: str | None = None, path: Path | None = None) -> bool:
    from . import store
    return store.ar_transition(request_id, expect.value, to.value, outcome=outcome, path=path)
