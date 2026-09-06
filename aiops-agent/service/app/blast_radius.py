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
import json
import logging

from pydantic import BaseModel, Field

from .config import settings

logger = logging.getLogger("aiops_agent.blast_radius")


class BlastRadius(BaseModel):
    action: str
    target: str  # "<namespace>/<deployment>"
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
    return BlastRadius(
        action=action, namespace=namespace, target=target, available=False, detail=detail
    )


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
        dep = await asyncio.to_thread(
            apps.read_namespaced_deployment, name=deployment, namespace=namespace
        )
        rs_list = await asyncio.to_thread(apps.list_namespaced_replica_set, namespace=namespace)
    except RuntimeError as e:  # k8s not wired
        return _unavailable("k8s.rollout_undo", namespace, target, str(e))
    except Exception as e:
        status = getattr(e, "status", None)
        if status == 404:
            return _unavailable(
                "k8s.rollout_undo",
                namespace,
                target,
                f"no Deployment named '{deployment}' in {namespace}",
            )
        return _unavailable(
            "k8s.rollout_undo", namespace, target, f"k8s API error: {type(e).__name__}: {e}"
        )

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
        action="k8s.rollout_undo",
        target=target,
        namespace=namespace,
        current_revision=current_rev,
        target_revision=target_rev,
        current_replicas=desired,
        target_replicas=desired,
        affected_pods=desired or 0,  # a rollout replaces all pods
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
        return _unavailable(
            "k8s.scale", namespace, target, "scale requires an integer 'replicas' arg"
        )
    try:
        _, apps = await asyncio.to_thread(k8s._load_client)
        dep = await asyncio.to_thread(
            apps.read_namespaced_deployment, name=deployment, namespace=namespace
        )
    except RuntimeError as e:
        return _unavailable("k8s.scale", namespace, target, str(e))
    except Exception as e:
        if getattr(e, "status", None) == 404:
            return _unavailable(
                "k8s.scale", namespace, target, f"no Deployment named '{deployment}' in {namespace}"
            )
        return _unavailable(
            "k8s.scale", namespace, target, f"k8s API error: {type(e).__name__}: {e}"
        )

    current = dep.spec.replicas if dep.spec else None
    delta = abs((target_replicas) - (current or 0))
    notes = []
    if target_replicas == 0:
        notes.append("scales to zero — takes the service fully down")

    return BlastRadius(
        action="k8s.scale",
        target=target,
        namespace=namespace,
        current_replicas=current,
        target_replicas=target_replicas,
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
        return False, (
            f"namespace {br.namespace} not in allowlist {settings.execution_namespace_allowlist}"
        )
    if br.cross_namespace:
        return False, "action crosses namespaces"
    # Scale-to-zero before the singleton rule: both refuse, but only one of them
    # names what the on-call actually proposed. "denied because singleton" sends
    # someone off to try replicas=1, which is refused too, for a different reason.
    if br.action == "k8s.scale" and br.target_replicas == 0:
        return False, "scaling to zero takes the service fully down"
    if settings.deny_singletons and br.singleton:
        return False, "target is a singleton (single replica) — denied by policy"
    if br.affected_pods > settings.max_blast_pods:
        return False, (f"affected pods {br.affected_pods} exceeds max {settings.max_blast_pods}")
    if br.action == "k8s.rollout_undo" and not br.target_revision:
        return False, "no previous revision to roll back to"
    return True, (f"within policy (affected {br.affected_pods} pod(s), ns {br.namespace})")


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


def _mounts_configmap(dep, name: str) -> bool:
    """True when this Deployment's pod template reads the named ConfigMap —
    as a volume, an envFrom source, or a single env var's valueFrom."""
    spec = getattr(getattr(dep.spec, "template", None), "spec", None)
    if spec is None:
        return False
    for vol in getattr(spec, "volumes", None) or []:
        cm = getattr(vol, "config_map", None)
        if cm is not None and getattr(cm, "name", None) == name:
            return True
    containers = list(getattr(spec, "containers", None) or []) + list(
        getattr(spec, "init_containers", None) or []
    )
    for c in containers:
        for src in getattr(c, "env_from", None) or []:
            cm = getattr(src, "config_map_ref", None)
            if cm is not None and getattr(cm, "name", None) == name:
                return True
        for env in getattr(c, "env", None) or []:
            ref = getattr(getattr(env, "value_from", None), "config_map_key_ref", None)
            if ref is not None and getattr(ref, "name", None) == name:
                return True
    return False


async def dry_run_configmap_flag_set(args: dict) -> BlastRadius:
    """Predict a flag flip: which ConfigMap, and every workload that reads it.

    A ConfigMap patch touches no pod directly, which is exactly why its footprint
    has to be computed rather than assumed — the effect lands on whatever mounts
    it, and one shared map can reach services nobody was thinking about. So the
    footprint here is the set of Deployments reading the map, and `affected_pods`
    is their combined replicas even though not a single pod is restarted.

    Reaching more than one workload is not refused here (that is policy's call),
    but it is recorded as a note, because "this flag is not only yours" is the
    thing the on-call needs to see before approving.
    """
    from .tools import k8s

    namespace = args.get("namespace") or settings.k8s_namespace
    name = args.get("configmap") or ""
    key = args.get("key", "flags.json")
    flag = args.get("flag") or ""
    target = f"{namespace}/{name}"

    if not name or not flag:
        return _unavailable(
            "k8s.configmap_flag_set", namespace, target, "configmap and flag are required"
        )

    try:
        core, apps = await asyncio.to_thread(k8s._load_client)
        cm = await asyncio.to_thread(core.read_namespaced_config_map, name, namespace)
        deps = await asyncio.to_thread(apps.list_namespaced_deployment, namespace=namespace)
    except RuntimeError as e:  # k8s not wired
        return _unavailable("k8s.configmap_flag_set", namespace, target, str(e))
    except Exception as e:
        if getattr(e, "status", None) == 404:
            return _unavailable(
                "k8s.configmap_flag_set",
                namespace,
                target,
                f"no ConfigMap named '{name}' in {namespace}",
            )
        return _unavailable(
            "k8s.configmap_flag_set", namespace, target, f"k8s API error: {type(e).__name__}: {e}"
        )

    notes: list[str] = []
    raw = (cm.data or {}).get(key)
    current: object | None = None
    if raw is None:
        notes.append(f"configmap has no key '{key}'")
    else:
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            notes.append(f"key '{key}' is not JSON")
            doc = {}
        if isinstance(doc, dict):
            if flag in doc:
                current = doc[flag]
            else:
                notes.append(f"key '{key}' has no flag '{flag}'")

    readers = [d for d in deps.items if _mounts_configmap(d, name)]
    reader_names = sorted(d.metadata.name for d in readers)
    pods = sum((d.spec.replicas or 0) for d in readers if d.spec is not None)
    if not readers:
        notes.append("no workload in this namespace reads this ConfigMap")
    elif len(readers) > 1:
        notes.append(f"read by {len(readers)} workloads: {', '.join(reader_names)}")

    target_value = bool(args.get("value"))
    if current is not None and bool(current) == target_value:
        notes.append(f"'{flag}' is already {target_value}; the flip would change nothing")

    # A flag flip with a restart is a different-sized action than one without,
    # and the difference is exactly the thing the on-call is approving: pods get
    # replaced. Count them, and say when the deployment being restarted is not
    # one of the workloads that reads the map — that combination is almost
    # always a typo, and it produces a restart that fixes nothing.
    restart = args.get("restart_deployment")
    restarted_pods = 0
    if restart:
        dep = next((d for d in deps.items if d.metadata.name == str(restart)), None)
        if dep is None:
            notes.append(f"restart target '{restart}' is not a Deployment in {namespace}")
        else:
            restarted_pods = dep.spec.replicas or 0 if dep.spec is not None else 0
            notes.append(f"restarts {restart} ({restarted_pods} pod(s)) after the flip")
            if str(restart) not in reader_names:
                notes.append(
                    f"'{restart}' does not mount this ConfigMap — restarting it will not "
                    "make it read the new value"
                )

    return BlastRadius(
        action="k8s.configmap_flag_set",
        target=target,
        namespace=namespace,
        current_revision=None if current is None else f"{flag}={current}",
        target_revision=f"{flag}={target_value}",
        affected_pods=pods,
        # Without a restart no pod is replaced, so "one replica" carries none of
        # the usual single-point-of-failure meaning. With one it carries all of
        # it: a single-replica deployment being rolled is a moment with no
        # healthy pod behind the service.
        singleton=bool(restart) and restarted_pods == 1,
        cross_namespace=False,
        in_protected_namespace=namespace in settings.protected_namespaces,
        notes=notes,
        detail=", ".join(reader_names),
    )
