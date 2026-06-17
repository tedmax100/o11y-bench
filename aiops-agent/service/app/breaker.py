"""Circuit breaker (step 7 後半 §3 step 3 / 7b-3) — the safety valve against
automation runaway and rollback flapping.

Two failure modes it guards:
  - **Runaway**: too many executions in a short window (a misfiring alert rule, a
    storm) — a global sliding-window rate limit refuses past the ceiling.
  - **Flapping**: an action that keeps failing on the same target
    (rollback → verify fails → rollback → …). After N consecutive failures on a
    (action, target) scope the breaker **trips open and stays open until a human
    resets it** — "tripped means manual reset only", because a breaker that
    re-closes itself can flap right alongside the thing it's meant to stop.

State is durable (store-backed): a breaker that forgets it tripped on restart
isn't a safety mechanism, and the execution plane *causes* restarts. The policy
lives here; the SQL lives in store.py. This module reads/records outcomes but
never executes anything.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from . import store
from .config import settings

logger = logging.getLogger("aiops_agent.breaker")

GLOBAL = "global"


def scope_key(action: str, target: str) -> str:
    return f"{action}|{target}"


def _now() -> datetime:
    return datetime.now(UTC)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def check(action: str, target: str, path=None) -> tuple[bool, str]:
    """(allowed, reason). Refuses if a relevant breaker is open or the global
    window rate limit is hit. fail-closed only on explicit trips/limits — a closed
    breaker allows."""
    if not settings.breaker_enabled:
        return True, "breaker disabled"

    g = store.breaker_get(GLOBAL, path)
    if g and g["open"]:
        return False, f"global breaker open ({g['reason']})"

    sk = scope_key(action, target)
    s = store.breaker_get(sk, path)
    if s and s["open"]:
        return False, f"breaker open for {target} ({s['reason']})"

    since = _fmt(_now() - timedelta(seconds=settings.breaker_window_seconds))
    n = store.exec_window_count(since, path)
    if n >= settings.breaker_max_actions_per_window:
        return False, (f"rate limit: {n} executions in last "
                       f"{settings.breaker_window_seconds}s "
                       f"(max {settings.breaker_max_actions_per_window})")
    return True, "closed"


def record_outcome(action: str, target: str, *, fp: str = "", request_id: str = "",
                   success: bool, path=None) -> None:
    """Record an *actual* execution outcome (call this only after something ran —
    not for refusals/aborts). On enough consecutive failures for the scope, trips
    the breaker open."""
    sk = scope_key(action, target)
    store.exec_record(ts=_fmt(_now()), scope_key=sk, action=action, target=target,
                      fp=fp, request_id=request_id, success=success, path=path)
    if success:
        return  # a success ends the consecutive-failure streak
    fails = store.exec_consecutive_failures(sk, path)
    if fails >= settings.breaker_fail_threshold:
        reason = f"{fails} consecutive failures on {target}"
        store.breaker_set_open(sk, _fmt(_now()), reason, path)
        logger.warning("circuit breaker tripped: %s", reason)


def reset(scope: str | None = None, path=None) -> int:
    """Human re-closes a tripped breaker (a specific scope, or all). Returns the
    number of breakers cleared."""
    cleared = store.breaker_clear(scope, path)
    logger.info("breaker reset (%s): %d cleared", scope or "all", cleared)
    return cleared


def snapshot(path=None) -> list[dict]:
    """All currently-open breakers (for the reset UI / introspection)."""
    return store.breaker_all(path)
