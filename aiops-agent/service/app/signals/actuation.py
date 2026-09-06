"""Actuation readiness — the third thing the governance gate has to prove.

The gate already proves two things before granting autonomy: *should we*
(confidence + calibration) and *is the map trustworthy* (DQ + environment fit).
It never proved the third: **can we still act at all.**

The cost of that gap was measured. A rollback cleared every policy gate, the
executor claimed the request, ran the dry-run, passed the blast-radius check —
and Kubernetes answered 401. Nothing anywhere reported it, for eight days.

The diagnosis written here originally was that the write ServiceAccount token had
expired. It had not. The token was always valid; `tools/k8s_write.py` keyed the
auth *prefix* under the header name while the generated client reads it under the
scheme name, so the raw JWT went out with no `Bearer ` in front of it and the API
server rejected it before authorization was ever evaluated. An unparseable header
and a dead credential are the same 401 from the outside, which is exactly why the
wrong story survived for months: it was plausible, and nothing was probing.

That makes the case for this module stronger, not weaker. Whatever the cause, a
credential is only observed at the instant it is used, and this one was used once
every few weeks.

The fix is to stop treating permission as a static fact and start treating it as
a signal that expires — the same move this system already made for topology
(reconcile against real traces) and for injected knowledge (resolve against the
live stores). An RBAC grant is a *declaration*, and a declaration nobody
reconciles eventually becomes a lie.

`SelfSubjectAccessReview` is the right probe because it is a real authenticated
call: a dead token fails it with 401 before authorization is even evaluated, so
one request covers both "is this identity still real" and "may it still patch".
It mutates nothing.

The check is also two-sided. It fails when a required verb is denied — and
equally when a *forbidden* verb is allowed, because a write credential that has
quietly gained `delete` is not a healthy credential, it is a broader blast
radius than every policy in this repo was written against.

Verdict shape is `{proven_good, score, note}`, identical to `dq_verdict()` and
`fit_verdict()`, so governance consumes it without learning anything new.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import settings
from ..tools.k8s_write import in_cluster_write_creds, load_write_authz_api

logger = logging.getLogger("aiops_agent.signals.actuation")

# What the registered actions actually need. Both `k8s.rollout_undo` and
# `k8s.scale` are a read of a Deployment plus a patch of it — nothing else. If a
# future action needs more, it belongs here, because a preflight that checks
# less than the executor does is a preflight that passes right before a failure.
REQUIRED: tuple[tuple[str, str, str], ...] = (
    ("apps", "deployments", "get"),
    ("apps", "deployments", "patch"),
)

# Verbs this credential must NOT have. Being able to do these is not an
# improvement; it means RBAC drifted away from what the safety design assumes.
FORBIDDEN: tuple[tuple[str, str, str], ...] = (
    ("apps", "deployments", "delete"),
    ("", "pods", "delete"),
)


@dataclass
class ActuationFit:
    computed_ts: float
    # None = the probe could not be run at all (no client, API unreachable).
    reachable: bool = False
    in_cluster: bool = False
    missing: list[str] = field(default_factory=list)  # required but denied
    excess: list[str] = field(default_factory=list)  # forbidden but allowed
    namespaces: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and not self.missing and not self.excess


_last: ActuationFit | None = None


def _from_row(row: dict) -> ActuationFit:
    """Rebuild a probe from its stored row.

    `computed_ts` is not a column: the table keeps the human-readable `ts`, and
    the verdict only needs it to compute an age in seconds. Parsing it back is
    cheaper than a migration, and a row we cannot parse is treated as no row at
    all rather than as a probe with a bogus age.
    """
    ts = datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    ns = [n for n in (row["namespaces"] or "").split(",") if n]
    return ActuationFit(
        computed_ts=ts.timestamp(),
        reachable=bool(row["reachable"]),
        in_cluster=bool(row["in_cluster"]),
        missing=list(json.loads(row["missing"] or "[]")),
        excess=list(json.loads(row["excess"] or "[]")),
        namespaces=ns,
        error=row["error"] or None,
    )


def get_last_actuation(path=None) -> ActuationFit | None:
    """The last probe, from memory or from the store.

    The fallback matters for the same reason it does on the environment-fit
    gate: without it the answer is per-process rather than per-credential, so a
    restart puts readiness back to "never checked" even though the probe history
    is sitting right there in the table. Every probe was already being persisted
    — only the read side was missing, which is the quietest possible version of
    this bug, because the write side looks completely healthy.

    Unreadable storage reads as no probe: unproven, never ready.
    """
    global _last
    if _last is not None:
        return _last
    try:
        from .. import store

        rows = store.actuation_probe_recent(limit=1, path=path)
    except Exception as e:
        logger.warning("actuation probe not readable from store: %s", e)
        return None
    if not rows:
        return None
    try:
        _last = _from_row(rows[0])
    except Exception as e:  # a malformed row is not a readiness claim
        logger.warning("stored actuation probe unreadable: %s", e)
        return None
    logger.info(
        "actuation readiness loaded from store: ok=%s (%ds old)",
        _last.ok,
        int(time.time() - _last.computed_ts),
    )
    return _last


def _rule(group: str, resource: str, verb: str, ns: str) -> str:
    g = f"{group}/" if group else ""
    return f"{verb} {g}{resource} in {ns}"


def _review(authz, group: str, resource: str, verb: str, ns: str) -> bool:
    from kubernetes import client

    body = client.V1SelfSubjectAccessReview(
        spec=client.V1SelfSubjectAccessReviewSpec(
            resource_attributes=client.V1ResourceAttributes(
                namespace=ns, group=group, resource=resource, verb=verb
            )
        )
    )
    resp = authz.create_self_subject_access_review(body)
    return bool(resp.status.allowed)


def _probe(namespaces: list[str]) -> ActuationFit:
    """Blocking body of the probe — run via asyncio.to_thread."""
    fit = ActuationFit(computed_ts=time.time(), in_cluster=in_cluster_write_creds())
    try:
        authz = load_write_authz_api()
        if authz is None:
            fit.error = "no authorization client on the write credentials"
            return fit
        for ns in namespaces:
            for group, resource, verb in REQUIRED:
                if not _review(authz, group, resource, verb, ns):
                    fit.missing.append(_rule(group, resource, verb, ns))
            for group, resource, verb in FORBIDDEN:
                if _review(authz, group, resource, verb, ns):
                    fit.excess.append(_rule(group, resource, verb, ns))
        # Every review completed, so the identity authenticated — which is the
        # half of this check that the 401 was hiding in.
        fit.reachable = True
    except Exception as e:
        # A 401 lands here. Recording it as an error (not as "denied") keeps the
        # two failure modes distinguishable: a dead token is an ops problem, a
        # denied verb is an RBAC problem, and they get fixed by different people.
        fit.error = f"{type(e).__name__}: {getattr(e, 'reason', None) or e}"
    return fit


async def check_actuation(
    namespaces: list[str] | None = None, *, source: str = "rca", path=None
) -> ActuationFit:
    """Run the preflight, cache it, and persist it. Read-only; never raises.

    Persisting matters as much as probing: a verdict that only exists in memory
    is gone on the next restart, and a readiness signal you can't look backwards
    at can't answer "how long has this been broken" — which is the exact question
    nobody could answer for 46 days."""
    global _last
    ns = namespaces or list(settings.execution_namespace_allowlist)
    fit = await asyncio.to_thread(_probe, ns)
    fit.namespaces = ns
    _last = fit
    try:
        from .. import store

        total = max(1, len(ns) * len(REQUIRED))
        store.actuation_probe_insert(
            ok=fit.ok,
            reachable=fit.reachable,
            in_cluster=fit.in_cluster,
            score=round((total - len(fit.missing)) / total, 4) if fit.reachable else 0.0,
            namespaces=ns,
            missing=fit.missing,
            excess=fit.excess,
            error=fit.error or "",
            source=source,
            path=path,
        )
    except Exception as e:  # a storage failure must not turn a good probe into no probe
        logger.warning("actuation probe not recorded: %s", e)
    if not fit.ok:
        logger.warning(
            "actuation preflight not ready: error=%s missing=%s excess=%s",
            fit.error,
            fit.missing,
            fit.excess,
        )
    return fit


async def can_still_write(namespaces: list[str] | None = None, *, path=None) -> tuple[bool, str]:
    """Fresh (uncached) answer to "does this credential still work right now",
    for the rollback path.

    Rollback runs with the *same* credential the failed execute used, so when the
    failure was the credential itself, rollback cannot possibly succeed — it will
    fail for the same reason and the request lands in `rollback_failed`. That
    status then says "we tried to undo and couldn't", which is a different and
    much less alarming claim than the truth: **we never had the ability to undo.**
    An on-call reading the first one goes looking for a stuck rollout; reading the
    second one they go fix a token.

    Cached deliberately not used: the point is the state *after* the failure."""
    fit = await check_actuation(namespaces, source="rollback", path=path)
    if not fit.reachable:
        return False, f"write credentials no longer authenticate ({fit.error})"
    if fit.missing:
        return False, f"write credentials lost required permission: {fit.missing[0]}"
    return True, "write credentials still valid"


async def refresh_actuation(path=None) -> None:
    """Re-probe when nobody has, or the last probe went stale. Best-effort — a
    failure leaves readiness unproven, which the verdict treats as "do not grant
    autonomy"."""
    if not settings.actuation_check_enabled:
        return
    try:
        last = get_last_actuation(path=path)
        age = time.time() - last.computed_ts if last else None
        if last is None or age is None or age > settings.actuation_max_age_seconds:
            await check_actuation(source="loop", path=path)
    except Exception as e:
        logger.warning("actuation refresh failed: %s", e)


def actuation_verdict(path=None) -> dict:
    """{proven_good, score, note} — the shape governance already reads.

    `score` is the fraction of required rules that came back allowed, so a
    partial grant reads as a number rather than a boolean."""
    if not settings.actuation_check_enabled:
        return {
            "proven_good": False,
            "score": None,
            "note": "actuation readiness checking is disabled; readiness unproven",
        }
    fit = get_last_actuation(path=path)
    if fit is None:
        return {
            "proven_good": False,
            "score": None,
            "note": "write credentials never checked against the cluster; readiness unproven",
        }

    age = int(time.time() - fit.computed_ts)
    total = max(1, len(fit.namespaces) * len(REQUIRED))
    score = round((total - len(fit.missing)) / total, 4) if fit.reachable else 0.0

    if age > settings.actuation_max_age_seconds:
        return {
            "proven_good": False,
            "score": score,
            "note": (
                f"last write-credential check {age}s old "
                f"(> {settings.actuation_max_age_seconds}s); readiness stale"
            ),
        }
    if not fit.reachable:
        return {
            "proven_good": False,
            "score": 0.0,
            "note": (
                f"write credentials did not authenticate against the cluster "
                f"({fit.error}); readiness failed"
            ),
        }
    if not fit.in_cluster:
        # A developer kubeconfig can do anything, so it can neither prove the
        # deployed identity works nor prove it is still limited.
        return {
            "proven_good": False,
            "score": score,
            "note": (
                "write path is using a local kubeconfig, not the projected write "
                "ServiceAccount; readiness says nothing about the deployed identity"
            ),
        }
    if fit.missing:
        return {
            "proven_good": False,
            "score": score,
            "note": (
                f"{len(fit.missing)} required permission(s) denied "
                f"({fit.missing[0]}); readiness failed"
            ),
        }
    if fit.excess:
        return {
            "proven_good": False,
            "score": score,
            "note": (
                f"write credential holds {len(fit.excess)} permission(s) the safety "
                f"design forbids ({fit.excess[0]}); readiness refused"
            ),
        }
    return {
        "proven_good": True,
        "score": score,
        "note": (
            f"write credentials authenticate and hold exactly the required "
            f"permissions in {', '.join(fit.namespaces)} (checked {age}s ago)"
        ),
    }
