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

import asyncio
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import action_requests as ar
from . import audit, blast_radius, breaker, store
from .action_requests import ActionRequest, Status
from .actions import ActionDisabled, registry
from .calibration import label_run
from .config import settings

# Module-level so tests can monkeypatch without importing the agent stack.
from .runbook import load_runbooks, run_diagnostics

logger = logging.getLogger("aiops_agent.execution")


def _read_only_tools() -> dict:
    """The agent's all-read-only TOOLS, keyed by name. Imported lazily so the
    executor doesn't pull the LLM stack unless a precondition check needs it."""
    from .agent import TOOLS

    return {t.name: t for t in TOOLS}


def _eval_verify_check(check: dict, output: Any) -> tuple[bool, str]:
    """Evaluate a verify step's check dict against the tool output.
    Returns (passed, detail_string)."""
    if not check:
        return True, "no check specified"

    if "max_value" in check:
        # Prometheus instant vector: {"resultType": "vector", "result": [{..., "value": float}]}
        #
        # An empty result is NOT zero here, and this gate learned that the
        # expensive way: a drill remediated an incident on a cluster whose demo
        # metrics were never scraped, the verify query matched no series, the old
        # code read that as 0, 0 ≤ the threshold, and the executor recorded
        # "executed and verified" for a fix nothing had observed. The failure
        # mode generalises — a metric renamed, a scrape target down, a label
        # dropped — and every one of them makes this gate pass at exactly the
        # moment it is the only thing standing between a wrong action and the
        # case memory learning that the action works.
        #
        # So: no series ⇒ the query cannot see the symptom ⇒ it cannot say the
        # symptom stopped. Fail closed, and let a runbook opt out with
        # `empty_ok: true` where absence genuinely is the signal.
        val: float | None = None
        if isinstance(output, dict):
            rt = output.get("resultType")
            result = output.get("result", [])
            if rt == "scalar":
                val = float(output.get("value", 0))
            elif rt == "vector":
                if not result:
                    if not check.get("empty_ok"):
                        return False, (
                            "verify query matched no series — absence of data is not "
                            "recovery (set empty_ok: true if it is, for this check)"
                        )
                    val = 0.0
                else:
                    raw = result[0].get("value")
                    try:
                        val = float(raw)
                    except (TypeError, ValueError):
                        pass
        if val is None:
            return False, f"could not extract numeric value from output: {str(output)[:200]}"
        limit = float(check["max_value"])
        ok = val <= limit
        return ok, f"value {val:.6g} {'≤' if ok else '>'} max_value {limit}"

    # Fall back to DiagnosticCheck for contains/nonempty/min_rows
    from .runbook import DiagnosticCheck, _evaluate_check

    known = {k: v for k, v in check.items() if k in DiagnosticCheck.model_fields}
    dc = DiagnosticCheck(**known)
    status, detail = _evaluate_check(dc, output)
    return (status in ("pass", "ran")), detail


_RANGE = re.compile(r"\[(\d+)(ms|s|m|h|d|w)\]")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _settle_seconds(verify: dict) -> tuple[int, str]:
    """How long to wait before asking the verify query whether the symptom cleared.

    The configured settle window is a floor, not the answer. A verify query that
    averages over `[2m]` cannot report a clean result 60 seconds after the fix —
    at that moment two thirds of its own lookback window is still the incident.
    The check then fails on a remediation that actually worked, and because a
    verify failure triggers auto-rollback, **the system undoes its own correct
    fix and files it as a failure.** That is worse than not checking at all: a
    missing check leaves the fix in place, a false-negative check removes it.

    So the window is derived from the query itself: the longest range selector in
    the expression, plus a margin for pods to actually roll. Nobody has to keep
    the runbook's PromQL and the settings file mentally in sync.
    """
    floor = settings.verify_delay_seconds
    if floor <= 0:
        # An explicit opt-out, used by tests and by anyone who wants the check to
        # run immediately. Deriving a window here would override a deliberate
        # setting, and a config knob that ignores 0 is a knob nobody trusts.
        return 0, "settle disabled (verify_delay_seconds=0)"
    expr = str((verify.get("args") or {}).get("expr", ""))
    ranges = [int(n) * _UNIT_SECONDS[u] for n, u in _RANGE.findall(expr)]
    if not ranges:
        return floor, f"{floor}s (configured; no range selector in the verify query)"
    needed = int(max(ranges)) + settings.verify_rollout_margin_seconds
    if needed <= floor:
        return floor, f"{floor}s (configured; covers the query's {int(max(ranges))}s lookback)"
    return needed, (
        f"{needed}s (the verify query looks back {int(max(ranges))}s, so the configured "
        f"{floor}s would have measured the incident it was checking was over)"
    )


async def _verify_outcome(req: ActionRequest, path: Path | None) -> bool:
    """Wait the settle window, run the runbook step's verify spec, return True
    if the symptom cleared. No verify spec → optimistically returns True (skip)."""
    rb = (
        next((b for b in load_runbooks() if b.id == req.runbook_id), None)
        if req.runbook_id
        else None
    )
    step = next((s for s in (rb.remediation if rb else []) if s.action == req.action), None)

    if step is None or not step.verify:
        audit.record(
            "verify",
            "skip",
            request_id=req.request_id,
            fp=req.fp,
            detail={"reason": "no verify spec on remediation step"},
            path=path,
        )
        return True

    verify = step.verify  # {action, args, check}
    settle, why = _settle_seconds(verify)
    audit.record(
        "verify",
        "settle",
        request_id=req.request_id,
        fp=req.fp,
        detail={"settle_seconds": settle, "reason": why},
        path=path,
    )
    await asyncio.sleep(settle)

    action_name = verify.get("action", "")
    v_args = verify.get("args", {})
    check = verify.get("check", {})

    try:
        tools = _read_only_tools()
        tool = tools.get(action_name)
        if tool is None:
            audit.record(
                "verify",
                "skip",
                request_id=req.request_id,
                fp=req.fp,
                detail={"reason": f"verify action {action_name!r} not in read-only tools"},
                path=path,
            )
            return True
        out = await tool.ainvoke(v_args)
    except Exception as e:
        audit.record(
            "verify",
            "error",
            request_id=req.request_id,
            fp=req.fp,
            detail={"error": f"{type(e).__name__}: {e}"},
            path=path,
        )
        return False  # error → conservative fail → trigger rollback

    passed, detail = _eval_verify_check(check, out)
    audit.record(
        "verify",
        "pass" if passed else "fail",
        request_id=req.request_id,
        fp=req.fp,
        detail={"check": check, "detail": detail, "output_preview": str(out)[:300]},
        path=path,
    )
    return passed


async def _auto_rollback(req: ActionRequest, path: Path | None) -> tuple[bool, str]:
    """Execute the rollback contract stored on the ActionRequest.

    Returns `(succeeded, outcome)`. The outcome string is not decoration: the
    three ways this returns False mean different things to whoever reads the
    request afterwards — there was nothing to undo with, we could not undo, or we
    tried to undo and it failed — and collapsing them into one status is how the
    only real execution this system ever ran ended up filed as
    "rollback also failed" when the truth was "the token was dead". Fail-closed:
    no contract or no impl → False."""
    contract = req.rollback
    if not contract:
        audit.record(
            "rollback",
            "skip",
            request_id=req.request_id,
            fp=req.fp,
            detail={"reason": "no rollback contract on request"},
            path=path,
        )
        return False, "no rollback contract"

    # Can we still write at all? Rollback reuses the credential the execute just
    # used, so when *that* is what failed, "rollback failed" understates it: we
    # never had the ability to undo. Record the two cases apart — they are fixed
    # by different people, and only one of them means the cluster is now in a
    # half-applied state nobody chose.
    if settings.actuation_check_enabled:
        from .signals.actuation import can_still_write

        writable, why = await can_still_write([ar.target_of(req.args).split("/")[0]], path=path)
        if not writable:
            audit.record(
                "rollback",
                "unavailable",
                request_id=req.request_id,
                fp=req.fp,
                detail={"reason": why, "action": contract.get("action")},
                path=path,
            )
            return False, f"rollback unavailable: {why}"

    rb_action = contract.get("action")
    rb_args = contract.get("args", {})
    spec = registry.get(rb_action) if rb_action else None
    if spec is None or spec.impl is None:
        audit.record(
            "rollback",
            "abort",
            request_id=req.request_id,
            fp=req.fp,
            detail={"reason": f"rollback action {rb_action!r} has no impl"},
            path=path,
        )
        return False, f"rollback unavailable: action {rb_action!r} has no implementation"

    try:
        result = await spec.impl(rb_args)
        audit.record(
            "rollback",
            "success",
            request_id=req.request_id,
            fp=req.fp,
            detail={"action": rb_action, "args": rb_args, "result": str(result)[:300]},
            path=path,
        )
        return True, "rolled back"
    except Exception as e:
        audit.record(
            "rollback",
            "fail",
            request_id=req.request_id,
            fp=req.fp,
            detail={"action": rb_action, "error": f"{type(e).__name__}: {e}"},
            path=path,
        )
        return False, f"rollback failed: {type(e).__name__}"


def _learn_outcome(req: ActionRequest, *, verified: bool, path: Path | None) -> None:
    """Write the verify outcome back to the CE harness (step 7 / §6.2).

    Two constraints from the design (§6.2):
    1. Only write when `learn_remediation_into_ce=True` (default False) — remediation
       labels feed fix-efficacy by default, not the headline CE stream.
    2. Only a *verify* failure (execute succeeded, symptom persisted) is evidence of
       RCA incorrectness. An execute failure is evidence of an infrastructure problem,
       not a wrong diagnosis — those labels stay out of CE entirely.
    """
    if not settings.learn_remediation_into_ce:
        return
    source = "remediation-verified" if verified else "remediation-failed"
    # The run whose reasoning produced this proposal — not "the latest run of
    # this alert", which after a storm is a different investigation than the one
    # being acted on. Falls back to the fingerprint for proposals created before
    # requests carried a run id.
    ident = req.run_id or req.fp
    ok = label_run(ident, correct=verified, source=source, path=path)
    if ok:
        logger.info("learn: labeled %s correct=%s source=%s", ident, verified, source)
    else:
        logger.warning("learn: no CE record for %s (run may predate this execution)", ident)


def _remember_fix(req: ActionRequest, *, path: Path | None) -> None:
    """The incident now knows what worked on it.

    `cases.resolution` and the `resolved by:` line in the recall block have both
    existed since the case table was written, and nothing ever wrote the column,
    so the line has never appeared in a prompt. This is the edge that was
    missing: an incident could learn what it was, and could learn which paths
    were dead, and could not learn what actually fixed it.
    """
    from . import case_memory

    verdict = case_memory.remember_resolution(
        case_key=req.case_key,
        action=req.action,
        args=req.args,
        runbook_id=req.runbook_id,
        request_id=req.request_id,
        drill=_is_drill(req),
        path=path,
    )
    logger.info("learn: fix for %s -> %s", req.case_key or req.fp, verdict)


def _remember_failed_fix(req: ActionRequest, *, path: Path | None) -> None:
    """It ran cleanly and the symptom stayed. That is a fact about this action on
    this incident, and the next run should not spend an approval discovering it
    again — so it goes on the same shelf as a declined action rather than into
    `resolution`, which is reserved for what worked.

    `disproved_by` is the tool result, not a person: nobody judged anything here,
    the verify window did.
    """
    if _is_drill(req) or not req.case_key:
        return
    from . import case_memory

    try:
        store.ruled_out_insert(
            key=req.case_key,
            run_id=req.run_id or "",
            ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            kind="action",
            subject=case_memory.rejected_subject(req.action, ar.target_of(req.args)),
            evidence="ran cleanly, symptom persisted through the verify window",
            disproved_by="tool_result",
            path=path,
        )
    except Exception as e:
        logger.warning("remember_failed_fix failed for %s: %s", req.case_key, e)


async def _revalidate_preconditions(req: ActionRequest, path: Path | None) -> bool:
    """Re-run the source runbook's read-only diagnostics and confirm none of the
    preconditions that were true at decision time has flipped to fail (TOCTOU).
    Aborts only on an explicit `fail` — an error/skip (e.g. a transient probe
    failure) is recorded but doesn't block a human-approved action."""
    if not req.runbook_id:
        audit.record(
            "precondition",
            "skip",
            request_id=req.request_id,
            fp=req.fp,
            detail={"reason": "no runbook linked"},
            path=path,
        )
        return True
    rb = next((b for b in load_runbooks() if b.id == req.runbook_id), None)
    if rb is None or not rb.diagnostics:
        audit.record(
            "precondition",
            "skip",
            request_id=req.request_id,
            fp=req.fp,
            detail={"reason": f"runbook {req.runbook_id} has no diagnostics"},
            path=path,
        )
        return True
    try:
        results = await run_diagnostics(rb, req.params, _read_only_tools())
    except Exception as e:
        audit.record(
            "precondition",
            "skip",
            request_id=req.request_id,
            fp=req.fp,
            detail={"error": f"{type(e).__name__}: {e}"},
            path=path,
        )
        return True
    failed = [r for r in results if r.status == "fail"]
    if failed:
        audit.record(
            "precondition",
            "abort",
            request_id=req.request_id,
            fp=req.fp,
            detail={"failed": [r.desc for r in failed]},
            path=path,
        )
        return False
    audit.record(
        "precondition",
        "ok",
        request_id=req.request_id,
        fp=req.fp,
        detail={"checked": len(results)},
        path=path,
    )
    return True


async def _check_blast_radius(req: ActionRequest, path: Path | None) -> bool:
    """Compute the action's footprint (read-only dry-run), store it for the UI,
    and refuse if it exceeds policy. Fail-closed: a dry-run that can't read the
    cluster, or any error, aborts."""
    spec = registry.get(req.action)
    if spec is None or spec.dry_run is None:
        audit.record(
            "dry_run",
            "skip",
            request_id=req.request_id,
            fp=req.fp,
            detail={"reason": "no dry-run for action"},
            path=path,
        )
        return True
    try:
        br = await spec.dry_run(req.args)
    except Exception as e:
        audit.record(
            "dry_run",
            "abort",
            request_id=req.request_id,
            fp=req.fp,
            detail={"error": f"{type(e).__name__}: {e}"},
            path=path,
        )
        return False
    store.ar_update(req.request_id, blast_radius=br.model_dump(), path=path)
    ok, reason = blast_radius.evaluate_policy(br)
    audit.record(
        "dry_run",
        "ok" if ok else "abort",
        request_id=req.request_id,
        fp=req.fp,
        detail={"blast_radius": blast_radius.format_blast_radius(br), "reason": reason},
        path=path,
    )
    return ok


def _is_drill(req: ActionRequest) -> bool:
    """Whether this execution is a rehearsal, taken from the alert's own labels.

    Marking it at write time rather than deciding later is deliberate: whoever
    reads the ledger next year will not be able to reconstruct which rows were
    drills from the timestamps, and an un-marked drill is indistinguishable from
    a production incident in every ratio computed on top of it."""
    return ar.is_drill(req.params)


def _rubric_context(req: ActionRequest) -> str:
    """The incident the action belongs to, in one paragraph.

    Half the judge's own rulebook is about intent — "block rollout_undo when the
    RCA says this is not a bad deploy", "block a scale that is more than 10x the
    current replica count". Neither is answerable from the action's arguments,
    so passing only the runbook id (which is what this used to do) leaves the
    judge grading the half of its job it can see.
    """
    bits: list[str] = []
    if req.runbook_id:
        bits.append(f"Runbook: {req.runbook_id}.")
    interesting = ("service_name", "alertname", "severity", "summary", "description")
    incident = {k: v for k, v in (req.params or {}).items() if k in interesting}
    if incident:
        bits.append("Incident: " + "; ".join(f"{k}={v}" for k, v in incident.items()) + ".")
    if req.blast_radius:
        try:
            from .blast_radius import BlastRadius, format_blast_radius

            bits.append("Blast radius: " + format_blast_radius(BlastRadius(**req.blast_radius)))
        except Exception:  # a malformed snapshot must not cost us the whole context
            bits.append(f"Blast radius: {req.blast_radius}")
    if req.rollback:
        bits.append(f"Rollback available: {req.rollback}.")
    return " ".join(bits) or "(none provided)"


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

    audit.record(
        "execute",
        "start",
        request_id=req.request_id,
        fp=req.fp,
        detail={"action": req.action, "args": req.args},
        path=path,
    )

    # --- 1. precondition revalidation (7b-2) ---------------------------------
    if not await _revalidate_preconditions(req, path):
        ar_store_transition(
            req.request_id,
            Status.EXECUTING,
            Status.ABORTED,
            outcome="precondition no longer holds",
            path=path,
        )
        return {"status": Status.ABORTED.value, "outcome": "precondition no longer holds"}

    # --- 2. dry-run + blast-radius gate (7b-2) -------------------------------
    if not await _check_blast_radius(req, path):
        ar_store_transition(
            req.request_id,
            Status.EXECUTING,
            Status.ABORTED,
            outcome="blast radius exceeds policy / dry-run unavailable",
            path=path,
        )
        return {"status": Status.ABORTED.value, "outcome": "blast radius exceeds policy"}

    # --- 3. idempotency + circuit breaker gate (7b-3) ------------------------
    target = ar.target_of(req.args)
    dup = store.ar_find_ran(req.idem_key, req.request_id, path)
    if dup:
        audit.record(
            "idempotency",
            "abort",
            request_id=req.request_id,
            fp=req.fp,
            detail={"superseded_by": dup, "idem_key": req.idem_key},
            path=path,
        )
        ar_store_transition(
            req.request_id,
            Status.EXECUTING,
            Status.ABORTED,
            outcome=f"idempotent: target already acted on for this incident ({dup})",
            path=path,
        )
        return {"status": Status.ABORTED.value, "outcome": "idempotent duplicate"}

    allowed, reason = breaker.check(req.action, target, path)
    if not allowed:
        audit.record(
            "breaker",
            "abort",
            request_id=req.request_id,
            fp=req.fp,
            detail={"reason": reason},
            path=path,
        )
        ar_store_transition(
            req.request_id,
            Status.EXECUTING,
            Status.ABORTED,
            outcome=f"circuit breaker: {reason}",
            path=path,
        )
        return {"status": Status.ABORTED.value, "outcome": f"circuit breaker: {reason}"}

    # --- 3a. actuation readiness: can this credential still act ---------------
    # Runs before the (expensive, LLM-backed) rubric gate because it is the
    # cheapest possible way to discover the thing that actually stopped the only
    # real execution this system ever attempted: a write path that answered 401
    # every time (a malformed auth header, not the expired token everyone assumed
    # — see signals/actuation.py). Failing here costs one API call; failing at the
    # write costs a half-applied change nobody planned for.
    if settings.actions_enabled and settings.actuation_check_enabled:
        from .signals.actuation import actuation_verdict, check_actuation

        await check_actuation([ar.target_of(req.args).split("/")[0]])
        act = actuation_verdict()
        if not act["proven_good"]:
            audit.record(
                "actuation",
                "abort",
                request_id=req.request_id,
                fp=req.fp,
                detail={"note": act["note"], "score": act["score"]},
                path=path,
            )
            ar_store_transition(
                req.request_id,
                Status.EXECUTING,
                Status.ABORTED,
                outcome=f"actuation readiness: {act['note']}",
                path=path,
            )
            return {"status": Status.ABORTED.value, "outcome": f"actuation: {act['note']}"}

    # --- 3b. rubric gate: LLM safety check for k8s mutations ------------------
    # Only run when actions are live — no point blocking a kill-switched execute.
    try:
        from .rubric import check_k8s_write

        rubric_ok, rubric_reason = await check_k8s_write(req.action, req.args, _rubric_context(req))
        if not rubric_ok and settings.actions_enabled:
            audit.record(
                "rubric",
                "abort",
                request_id=req.request_id,
                fp=req.fp,
                detail={"action": req.action, "reason": rubric_reason},
                path=path,
            )
            ar_store_transition(
                req.request_id,
                Status.EXECUTING,
                Status.ABORTED,
                outcome=f"rubric blocked: {rubric_reason}",
                path=path,
            )
            return {"status": Status.ABORTED.value, "outcome": f"rubric blocked: {rubric_reason}"}
    except Exception as _rubric_exc:
        logger.warning("k8s write rubric check failed (best-effort): %s", _rubric_exc)

    # --- 4. execute (kill-switched; refuses until 7b-4 wires an impl) ---------
    try:
        result = await registry.execute(req.action, req.args)
    except (ActionDisabled, KeyError) as e:
        # Expected terminal until 7b-4: the kill switch / missing impl refuses.
        # NOTHING RAN, so this must not feed the breaker (no record_outcome) or the
        # Learn loop — just a clean REFUSED.
        ar_store_transition(
            req.request_id, Status.EXECUTING, Status.REFUSED, outcome=str(e), path=path
        )
        audit.record(
            "execute",
            "refuse",
            request_id=req.request_id,
            fp=req.fp,
            detail={"reason": str(e)},
            path=path,
        )
        return {"status": Status.REFUSED.value, "outcome": str(e)}
    except Exception as e:  # the action RAN and errored
        breaker.record_outcome(
            req.action,
            target,
            fp=req.fp,
            request_id=req.request_id,
            success=False,
            drill=_is_drill(req),
            path=path,
        )
        ar_store_transition(
            req.request_id,
            Status.EXECUTING,
            Status.FAILED,
            outcome=f"{type(e).__name__}: {e}",
            path=path,
        )
        audit.record(
            "execute",
            "fail",
            request_id=req.request_id,
            fp=req.fp,
            detail={"error": str(e)},
            path=path,
        )
        # execute errored → try rollback, but CE is not touched (§6.2 constraint 2)
        ar_store_transition(
            req.request_id,
            Status.FAILED,
            Status.ROLLING_BACK,
            outcome="auto-rollback after execute failure",
            path=path,
        )
        rb_ok, rb_outcome = await _auto_rollback(req, path)
        final = Status.ROLLED_BACK if rb_ok else Status.ROLLBACK_FAILED
        ar_store_transition(
            req.request_id,
            Status.ROLLING_BACK,
            final,
            outcome=rb_outcome,
            path=path,
        )
        return {"status": final.value, "outcome": str(e)}

    audit.record(
        "execute",
        "success",
        request_id=req.request_id,
        fp=req.fp,
        detail={"result": str(result)[:500]},
        path=path,
    )

    # --- 5. verify (closed-loop settle + symptom check) ----------------------
    verified = await _verify_outcome(req, path)
    if verified:
        breaker.record_outcome(
            req.action,
            target,
            fp=req.fp,
            request_id=req.request_id,
            success=True,
            drill=_is_drill(req),
            path=path,
        )
        ar_store_transition(
            req.request_id,
            Status.EXECUTING,
            Status.SUCCEEDED,
            outcome="executed and verified",
            path=path,
        )
        # Closed-loop 三: record ok execution for SOP decay detection.
        _rb_feedback("ok", req, path)
        # --- 7. Learn: verified → correct label (§6.2 constraint 1+3) --------
        _learn_outcome(req, verified=True, path=path)
        _remember_fix(req, path=path)
        return {"status": Status.SUCCEEDED.value}

    # --- 6. verify failed → auto-rollback ------------------------------------
    # §6.2 constraint 2: only verify failure (not execute failure) is RCA-wrongness
    # evidence; label AFTER rollback (whether it succeeded or not — the RCA was wrong
    # regardless of whether rollback worked).
    breaker.record_outcome(
        req.action,
        target,
        fp=req.fp,
        request_id=req.request_id,
        success=False,
        drill=_is_drill(req),
        path=path,
    )
    ar_store_transition(
        req.request_id,
        Status.EXECUTING,
        Status.VERIFY_FAILED,
        outcome="executed but symptom persists after verify window",
        path=path,
    )
    # Closed-loop 三: record verify failure before rollback.
    _rb_feedback("verify_failed", req, path)
    _remember_failed_fix(req, path=path)
    ar_store_transition(
        req.request_id,
        Status.VERIFY_FAILED,
        Status.ROLLING_BACK,
        outcome="auto-rollback triggered by verify failure",
        path=path,
    )
    rb_ok, rb_outcome = await _auto_rollback(req, path)
    final = Status.ROLLED_BACK if rb_ok else Status.ROLLBACK_FAILED
    ar_store_transition(
        req.request_id,
        Status.ROLLING_BACK,
        final,
        outcome=f"{rb_outcome} (after verify failure)",
        path=path,
    )
    # Closed-loop 三: record rollback outcome.
    _rb_feedback("rollback" if rb_ok else "rollback_failed", req, path)
    # --- 7. Learn: verify failed → incorrect label ---------------------------
    _learn_outcome(req, verified=False, path=path)
    rollback_outcome = rb_outcome
    return {"status": final.value, "outcome": f"verify failed; {rollback_outcome}"}


def _rb_feedback(outcome: str, req: ActionRequest, path: Path | None) -> None:
    """Best-effort: write one runbook execution outcome for SOP decay detection."""
    if not req.runbook_id:
        return
    try:
        # Find the matching remediation step description from the runbook.
        rb = next((b for b in load_runbooks() if b.id == req.runbook_id), None)
        step_desc = ""
        if rb:
            for s in rb.remediation:
                if s.action == req.action:
                    step_desc = s.desc
                    break
        store.rb_feedback_insert(
            runbook_id=req.runbook_id,
            outcome=outcome,
            step_desc=step_desc,
            request_id=req.request_id,
            fp=req.fp,
            path=path,
        )
    except Exception as e:
        logger.warning("rb_feedback write failed request_id=%s: %s", req.request_id, e)


# Small wrappers so the store coupling stays in one place and reads cleanly above.
def _claim(request_id: str, fp: str, path: Path | None) -> bool:
    from . import store

    ok = store.ar_transition(request_id, Status.APPROVED.value, Status.EXECUTING.value, path=path)
    if not ok:
        audit.record(
            "execute",
            "abort",
            request_id=request_id,
            fp=fp,
            detail={"reason": "request not in approved state"},
            path=path,
        )
    return ok


def ar_store_transition(
    request_id: str,
    expect: Status,
    to: Status,
    *,
    outcome: str | None = None,
    path: Path | None = None,
) -> bool:
    from . import store

    return store.ar_transition(request_id, expect.value, to.value, outcome=outcome, path=path)
