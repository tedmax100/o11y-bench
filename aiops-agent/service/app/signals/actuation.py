"""Actuation readiness — the third thing the governance gate has to prove.

The gate already proves two things before granting autonomy: *should we*
(confidence + calibration) and *is the map trustworthy* (DQ + environment fit).
It never proved the third: **can we still act at all.**

The cost of that gap was measured. A rollback cleared every policy gate, the
executor claimed the request, ran the dry-run, passed the blast-radius check —
and Kubernetes answered 401. The write ServiceAccount token had been minted 46
days earlier against a cluster that was rebuilt in between, so its signing key
was long gone. Nothing anywhere reported it. A credential is only observed at
the instant it is used, and this one was used once every few weeks, so its
death was invisible for as long as the interval between attempts.

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
import logging
import time
from dataclasses import dataclass, field

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


def get_last_actuation() -> ActuationFit | None:
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


async def check_actuation(namespaces: list[str] | None = None) -> ActuationFit:
    """Run the preflight and cache it. Read-only; never raises."""
    global _last
    ns = namespaces or list(settings.execution_namespace_allowlist)
    fit = await asyncio.to_thread(_probe, ns)
    fit.namespaces = ns
    _last = fit
    if not fit.ok:
        logger.warning(
            "actuation preflight not ready: error=%s missing=%s excess=%s",
            fit.error,
            fit.missing,
            fit.excess,
        )
    return fit


async def refresh_actuation() -> None:
    """Re-probe when nobody has, or the last probe went stale. Best-effort — a
    failure leaves readiness unproven, which the verdict treats as "do not grant
    autonomy"."""
    if not settings.actuation_check_enabled:
        return
    try:
        last = get_last_actuation()
        age = time.time() - last.computed_ts if last else None
        if last is None or age is None or age > settings.actuation_max_age_seconds:
            await check_actuation()
    except Exception as e:
        logger.warning("actuation refresh failed: %s", e)


def actuation_verdict() -> dict:
    """{proven_good, score, note} — the shape governance already reads.

    `score` is the fraction of required rules that came back allowed, so a
    partial grant reads as a number rather than a boolean."""
    if not settings.actuation_check_enabled:
        return {
            "proven_good": False,
            "score": None,
            "note": "actuation readiness checking is disabled; readiness unproven",
        }
    fit = get_last_actuation()
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
