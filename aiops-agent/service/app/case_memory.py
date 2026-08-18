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
has to remember that `self` doesn't count. `store.case_confirm` refuses
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
    path=None,
) -> str:
    """Turn one labeled run into what the case now knows. Returns what it did
    ('confirmed' / 'false_positive' / 'ignored') so callers can log it.

    The three-way split matters more than it looks. A `culprit`-graded correct
    run is precedent. An `inconclusive`-graded correct run means "it rightly
    blamed nobody" — that is a fact about the *alert* (this fires without an
    incident behind it), and storing it as a root cause would hand the next run
    a non-incident as a solved case. A wrong run teaches nothing at this layer:
    knowing the answer was wrong is not knowing the answer.
    """
    try:
        if not correct:
            return "ignored"
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
