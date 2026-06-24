"""Dry-run + blast-radius (step 7 後半 §3 step 2 / 7b-2).

Before any action runs we compute — read-only — exactly what it would touch and
how far the effect reaches, then refuse anything outside policy. This is the
"算清範圍" gate: it never mutates (no write API imported), it only reads the
current deployment / ReplicaSet state to predict the action's footprint.

`BlastRadius` is the computed footprint; `evaluate_policy` is the verdict. Both
are fail-closed: if the dry-run can't read the cluster (`available=False`) the
policy refuses, because acting blind is exactly what this gate exists to prevent.

The dry-runs reuse the read-only client from tools/k8s.py — the same SA, no new
permissions. Wiring a *mutating* impl is a later, separately-reviewed change
(7b-4); this module stays entirely on the read side.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, Field

from .config import settings

logger = logging.getLogger("aiops_agent.blast_radius")


class BlastRadius(BaseModel):
    action: str
    target: str                       # "<namespace>/<deployment>"
    namespace: str
    current_revision: str | None = None
    target_revision: str | None = None
    current_replicas: int | None = None
    target_replicas: int | None = None
    affected_pods: int = 0
    singleton: bool = False
    cross_namespace: bool = False
    in_protected_namespace: bool = False
    notes: list[str] = Field(default_factory=list)
    # False ⇒ the dry-run could not read the cluster; policy must fail-closed.
    available: bool = True
    detail: str = ""


def _unavailable(action: str, namespace: str, target: str, detail: str) -> BlastRadius:
    return BlastRadius(action=action, namespace=namespace, target=target,
                       available=False, detail=detail)


def _revision(annotations: dict | None) -> str | None:
    return (annotations or {}).get("deployment.kubernetes.io/revision")


# ---- dry-runs (read-only) --------------------------------------------------

async def dry_run_rollout_undo(args: dict) -> BlastRadius:
    """Predict a `kubectl rollout undo`: which deployment, current → previous
    revision, and how many pods get replaced. No previous revision ⇒ nothing to
    undo (the policy will refuse)."""
    from .tools import k8s

    namespace = args.get("namespace") or settings.k8s_namespace
    deployment = args.get("deployment") or ""
    target = f"{namespace}/{deployment}"
    try:
        _, apps = await asyncio.to_thread(k8s._load_client)
        dep = await asyncio.to_thread(apps.read_namespaced_deployment,
                                      name=deployment, namespace=namespace)
        rs_list = await asyncio.to_thread(apps.list_namespaced_replica_set,
                                          namespace=namespace)
    except RuntimeError as e:        # k8s not wired
        return _unavailable("k8s.rollout_undo", namespace, target, str(e))
    except Exception as e:
        status = getattr(e, "status", None)
        if status == 404:
            return _unavailable("k8s.rollout_undo", namespace, target,
                                f"no Deployment named '{deployment}' in {namespace}")
        return _unavailable("k8s.rollout_undo", namespace, target,
                            f"k8s API error: {type(e).__name__}: {e}")

    desired = dep.spec.replicas if dep.spec else None
    current_rev = _revision(dep.metadata.annotations)

    # ReplicaSets owned by this deployment, by revision; the undo target is the
    # newest revision strictly below the current one.
    owned: list[tuple[int, str]] = []
    for rs in rs_list.items:
        owners = rs.metadata.owner_references or []
        if not any(o.kind == "Deployment" and o.name == deployment for o in owners):
            continue
        rev = _revision(rs.metadata.annotations)
        if rev is not None:
            try:
                owned.append((int(rev), rev))
            except ValueError:
                continue
    owned.sort(reverse=True)
    cur_int = int(current_rev) if current_rev and current_rev.isdigit() else None
    target_rev = next((rv for n, rv in owned if cur_int is None or n < cur_int), None)

    notes = []
    if target_rev is None:
        notes.append("no previous revision to roll back to")

    return BlastRadius(
        action="k8s.rollout_undo", target=target, namespace=namespace,
        current_revision=current_rev, target_revision=target_rev,
        current_replicas=desired, target_replicas=desired,
        affected_pods=desired or 0,                 # a rollout replaces all pods
        singleton=(desired is not None and desired <= 1),
        cross_namespace=False,
        in_protected_namespace=namespace in settings.protected_namespaces,
        notes=notes,
    )


async def dry_run_scale(args: dict) -> BlastRadius:
    """Predict a scale: current → target replicas; affected pods = |delta|."""
    from .tools import k8s

    namespace = args.get("namespace") or settings.k8s_namespace
    deployment = args.get("deployment") or ""
    target = f"{namespace}/{deployment}"
    try:
        target_replicas = int(args["replicas"])
    except (KeyError, TypeError, ValueError):
        return _unavailable("k8s.scale", namespace, target,
                            "scale requires an integer 'replicas' arg")
    try:
        _, apps = await asyncio.to_thread(k8s._load_client)
        dep = await asyncio.to_thread(apps.read_namespaced_deployment,
                                      name=deployment, namespace=namespace)
    except RuntimeError as e:
        return _unavailable("k8s.scale", namespace, target, str(e))
    except Exception as e:
        if getattr(e, "status", None) == 404:
            return _unavailable("k8s.scale", namespace, target,
                                f"no Deployment named '{deployment}' in {namespace}")
        return _unavailable("k8s.scale", namespace, target,
                            f"k8s API error: {type(e).__name__}: {e}")

    current = dep.spec.replicas if dep.spec else None
    delta = abs((target_replicas) - (current or 0))
    notes = []
    if target_replicas == 0:
        notes.append("scales to zero — takes the service fully down")

    return BlastRadius(
        action="k8s.scale", target=target, namespace=namespace,
        current_replicas=current, target_replicas=target_replicas,
        affected_pods=delta,
        singleton=(target_replicas <= 1),
        cross_namespace=False,
        in_protected_namespace=namespace in settings.protected_namespaces,
        notes=notes,
    )


# ---- policy (fail-closed) --------------------------------------------------

def evaluate_policy(br: BlastRadius) -> tuple[bool, str]:
    """(ok, reason). Refuses on: unreadable dry-run, protected/off-allowlist
    namespace, cross-namespace effect, singleton (when denied), too many affected
    pods, or a rollout_undo with no previous revision."""
    if not br.available:
        return False, f"dry-run unavailable ({br.detail}); fail-closed"
    if br.in_protected_namespace:
        return False, f"namespace {br.namespace} is protected"
    if br.namespace not in settings.execution_namespace_allowlist:
        return False, (f"namespace {br.namespace} not in allowlist "
                       f"{settings.execution_namespace_allowlist}")
    if br.cross_namespace:
        return False, "action crosses namespaces"
    if settings.deny_singletons and br.singleton:
        return False, "target is a singleton (single replica) — denied by policy"
    if br.affected_pods > settings.max_blast_pods:
        return False, (f"affected pods {br.affected_pods} exceeds max "
                       f"{settings.max_blast_pods}")
    if br.action == "k8s.rollout_undo" and not br.target_revision:
        return False, "no previous revision to roll back to"
    return True, (f"within policy (affected {br.affected_pods} pod(s), "
                  f"ns {br.namespace})")


def format_blast_radius(br: BlastRadius) -> str:
    if not br.available:
        return f"dry-run unavailable: {br.detail}"
    bits = [f"target {br.target}"]
    if br.current_revision or br.target_revision:
        bits.append(f"revision {br.current_revision}→{br.target_revision}")
    if br.current_replicas is not None and br.target_replicas is not None:
        bits.append(f"replicas {br.current_replicas}→{br.target_replicas}")
    bits.append(f"affected {br.affected_pods} pod(s)")
    if br.singleton:
        bits.append("singleton")
    if br.notes:
        bits.append("; ".join(br.notes))
    return ", ".join(bits)
