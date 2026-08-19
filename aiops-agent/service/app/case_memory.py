"""Case memory — the write side.

`store.py` owns the tables and the SQL; this module owns *who may write what,
and when*. Two things live here.

**A scope.** Recording a dead end happens deep in the tool layer, which has no
idea which incident is being investigated — and threading a case key through
every tool signature would be a change to a dozen call sites for a best-effort
side effect. A ContextVar carries it instead, the same way `tools.query` carries
the pinned clock: `asyncio` copies the context into tasks at creation, so graph
nodes spawned inside `agent.ainvoke(...)` inherit it.

**A policy boundary.** Whether a verdict is allowed to become recallable
precedent is decided in exactly one place (`confirm_from_label`), so no caller
has to remember that `self` doesn't count. That covers both directions: a
verdict can confirm a root cause, and it can refute one. The refutations arrive
from the two places a person actually touches this system — labeling a finished
investigation, and declining a proposed action — and they land on the dead-end
shelf rather than the knowledge one. `store.case_confirm` refuses
untrusted sources too — belt and braces, because the cost of getting this wrong
is not a crash, it is a wrong prior injected into every future run of that
incident with a human's name on it.

Everything here is best-effort: a failure to remember must never sink a run.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import store
from .config import settings

logger = logging.getLogger("aiops_agent.case_memory")

# Which label sources may put a *disproof* on a case, and what they count as
# once they are there. Same allowlist shape as the root-cause side and for the
# same reason: an unfamiliar source should be ignored rather than trusted,
# because this text ends up in a prompt. Self-attestation
# (`remediation-verified` / `remediation-failed`) is absent on purpose — the run
# grading its own reasoning is exactly what a dead end must not be built from.
_DISPROOF_BY: dict[str, str] = {
    "ui": "human",
    "manual": "human",
    "eval": "grader",
    "eval-harness": "grader",
}


@dataclass(frozen=True)
class CaseScope:
    """Which incident this run is about, and which run this is."""

    case_key: str
    run_id: str
    alertname: str | None = None
    service: str | None = None


_scope: ContextVar[CaseScope | None] = ContextVar("case_scope", default=None)


def current_scope() -> CaseScope | None:
    return _scope.get()


@contextmanager
def case_scope(
    *, fp: str, alertname: str | None, service: str | None
) -> Iterator[CaseScope | None]:
    """Open a case scope for one investigation.

    Yields None (and records nothing) when the alert carries no service: the
    case key would then be a signature of one field, colliding every unrelated
    alert of the same name into one "incident". A missing scope degrades to the
    old behaviour, which is the right failure.
    """
    if not service or not settings.case_memory_enabled:
        yield None
        return
    sc = CaseScope(
        case_key=store.case_key(alertname, service),
        run_id=store.new_run_id(fp),
        alertname=alertname,
        service=service,
    )
    token = _scope.set(sc)
    try:
        yield sc
    finally:
        _scope.reset(token)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def observe(scope: CaseScope, path=None) -> None:
    """Record that this incident was investigated (again). Not a conclusion —
    `occurrences` counts attention, not knowledge."""
    try:
        store.case_upsert(
            key=scope.case_key,
            ts=_now(),
            alertname=scope.alertname,
            service=scope.service,
            path=path,
        )
    except Exception as e:
        logger.warning("case observe failed for %s: %s", scope.case_key, e)


def remember_dead_end(
    kind: str,
    subject: str,
    *,
    disproved_by: str,
    evidence: str = "",
    ttl_seconds: int | None = None,
    path=None,
) -> bool:
    """Record a path that did not work, against whatever case is in scope.

    No-op outside a scope, which is the common case for chat turns and unit
    tests — hence the boolean return rather than a raise. `ttl_seconds` exists
    for the dead ends that are only true for a while: "Tempo had no trace" is a
    statement about the retention window, not about the trace.
    """
    sc = _scope.get()
    if sc is None:
        return False
    expires = None
    if ttl_seconds:
        expires = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    try:
        store.ruled_out_insert(
            key=sc.case_key,
            run_id=sc.run_id,
            ts=_now(),
            kind=kind,
            subject=subject[:500],
            evidence=evidence[:500],
            disproved_by=disproved_by,
            expires_ts=expires,
            path=path,
        )
        return True
    except Exception as e:
        logger.warning("remember_dead_end failed for %s: %s", sc.case_key, e)
        return False


def confirm_from_label(
    *,
    case_key: str,
    correct: bool,
    source: str,
    grading_mode: str | None,
    root_cause: str,
    run_id: str,
    resolution: dict | None = None,
    correction_note: str | None = None,
    path=None,
) -> str:
    """Turn one labeled run into what the case now knows. Returns what it did
    ('confirmed' / 'false_positive' / 'disproved' / 'ignored') so callers can
    log it.

    The split matters more than it looks. A `culprit`-graded correct run is
    precedent. An `inconclusive`-graded correct run means "it rightly blamed
    nobody" — that is a fact about the *alert* (this fires without an incident
    behind it), and storing it as a root cause would hand the next run a
    non-incident as a solved case.

    A wrong run used to teach nothing here, on the reading that knowing the
    answer was wrong is not knowing the answer. That is true about the *root
    cause* column and false about the case: "a person looked at this and said
    the payment version was not it" is the single most expensive piece of
    evidence this system ever produces, and dropping it meant the next run was
    free to arrive at the same wrong answer with nothing in its way. It goes in
    as a disproof, not as knowledge — the same shelf as a dead-end query, which
    is exactly what a refuted hypothesis is.
    """
    try:
        if not correct:
            return _disprove(
                case_key=case_key,
                source=source,
                grading_mode=grading_mode,
                hypothesis=root_cause,
                run_id=run_id,
                correction_note=correction_note,
                path=path,
            )
        if grading_mode == store.INCONCLUSIVE:
            store.case_set_status(case_key, "false_positive", path=path)
            return "false_positive"
        if grading_mode != store.CULPRIT:
            # Unknown provenance fails closed: this text ends up in a prompt.
            return "ignored"
        ok = store.case_confirm(
            case_key,
            root_cause=root_cause,
            source=source,
            run_id=run_id,
            ts=_now(),
            resolution=resolution,
            path=path,
        )
        return "confirmed" if ok else "ignored"
    except Exception as e:
        logger.warning("confirm_from_label failed for %s: %s", case_key, e)
        return "ignored"


def _disprove(
    *,
    case_key: str,
    source: str,
    grading_mode: str | None,
    hypothesis: str,
    run_id: str,
    correction_note: str | None,
    path=None,
) -> str:
    """A human (or the grader) said this run's conclusion was wrong.

    Only a `culprit` verdict carries something to refute. A wrong
    `inconclusive` run means it declined to blame anyone when it should have,
    and there is no hypothesis on the table to rule out; recording the
    correction note there would inject the *answer* under a heading that says
    "already ruled out", which is worse than recording nothing.

    The note is kept as evidence rather than promoted to a root cause. It is
    free text from a text box — it may be the answer, or a hint, or "see
    thread" — and `cases.root_cause` is a field the next run reads as settled.
    Whoever wants to state a cause has an endpoint that says so.
    """
    disproved_by = _DISPROOF_BY.get(source)
    if disproved_by is None:
        return "ignored"
    if grading_mode != store.CULPRIT:
        return "ignored"
    subject = (hypothesis or "").strip()
    if not subject:
        return "ignored"
    try:
        store.ruled_out_insert(
            key=case_key,
            run_id=run_id,
            ts=_now(),
            kind="hypothesis",
            subject=subject[:500],
            evidence=(correction_note or "").strip()[:500],
            disproved_by=disproved_by,
            path=path,
        )
        return "disproved"
    except Exception as e:
        logger.warning("disprove failed for %s: %s", case_key, e)
        return "ignored"


def rejected_subject(action: str, target: str) -> str:
    """How a declined action is written down, in one place.

    The writer and the lookup have to agree character for character or the next
    run silently re-proposes what was turned down. This project has been bitten
    by a string built in two places before (the trace id regex, in three), and
    the failure mode is identical: everything keeps working, quietly wrong.
    """
    return f"{action} on {target}"


def remember_rejected_action(
    *,
    case_key: str | None,
    run_id: str | None,
    action: str,
    target: str,
    reason: str,
    actor: str,
    path=None,
) -> str:
    """A person declined a proposed action. Remember what, and why.

    Deliberately not a scope call: the decision arrives long after the
    investigation's context has gone, holding nothing but a request id. The
    proposal therefore carries its own case key, pinned when it was made.
    Falling back to resolving it from the run only covers proposals written
    before that column existed — that lookup reads the investigation row, which
    is written when the run *ends*, so it is not something to depend on for a
    decision that can arrive at any time.

    A rejection with no reason given is still recorded. "This was proposed and
    turned down" is weaker evidence than a reason, but it is not nothing, and
    demanding a justification before the system will remember anything is how
    the field ends up empty on every row.
    """
    try:
        key = case_key or (store.case_key_for_run(run_id, path=path) if run_id else None)
        if not key:
            return "ignored"
        store.ruled_out_insert(
            key=key,
            run_id=run_id or "",
            ts=_now(),
            kind="action",
            subject=rejected_subject(action, target)[:500],
            evidence=((reason or "").strip() or f"declined by {actor}, no reason given")[:500],
            disproved_by="human",
            path=path,
        )
        return "rejected"
    except Exception as e:
        logger.warning("remember_rejected_action failed for %s: %s", case_key or run_id, e)
        return "ignored"


def prior_rejection(case_key: str, action: str, target: str, path=None) -> dict | None:
    """Has a person already declined this exact action on this incident?

    Reads through the same freshness rules as recall, so a rejection stops
    binding at the same moment it stops being shown to the model. A rule the
    gate enforces but the prompt no longer mentions is a rule nobody can
    explain.
    """
    try:
        return store.ruled_out_find(
            case_key, kind="action", subject=rejected_subject(action, target), path=path
        )
    except Exception as e:
        logger.warning("prior_rejection lookup failed for %s: %s", case_key, e)
        return None


def remember_resolution(
    *,
    case_key: str | None,
    action: str,
    args: dict | None,
    runbook_id: str | None,
    request_id: str,
    drill: bool,
    path=None,
) -> str:
    """A remediation ran on this incident and the symptom went away.

    This is the one thing the case can learn without a person in the loop, and
    the reason is worth stating: "I ran this command and the symptom stopped" is
    an observation, checked by the executor's own verify window against the same
    metric the alert fired on. It is not the agent grading its own reasoning,
    which is what `_SELF_LABEL_SOURCES` exists to keep out. A root cause is a
    claim about why; a resolution is a record of what was done and what happened
    next.

    It writes only into `resolution`, never into `root_cause`. A fix that worked
    is not proof the diagnosis was right — a restart clears a great many things
    it does not explain — and the recall block keeps them in separate lines for
    that reason.

    Drills are excluded. A rehearsal on a fault someone injected on purpose is
    not evidence about the real incident, and the ledger has been careful to
    mark them since the game day; recalling one as precedent would undo that.
    """
    if drill or not case_key:
        return "ignored"
    try:
        ok = store.case_set_resolution(
            case_key,
            resolution={
                "action": action,
                "args": args or {},
                "runbook_id": runbook_id,
                "request_id": request_id,
                "verified": True,
                "ts": _now(),
            },
            path=path,
        )
        return "recorded" if ok else "ignored"
    except Exception as e:
        logger.warning("remember_resolution failed for %s: %s", case_key, e)
        return "ignored"
