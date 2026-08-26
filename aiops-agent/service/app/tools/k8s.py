"""Read-only Kubernetes signal tools — v3 §2 (the infra half of the causal chain).

The metric→trace→log→github_diff chain answers "was it a *code* change". These
tools answer the other half: "or was it the *platform*" — OOMKilled, CrashLoop,
image pull failures, a rollout that never went Available, eviction / scheduling
pressure. An incident sitting on a `git_version` boundary could be a bad deploy
*or* a pod that never came up healthy; without k8s the agent can't tell them apart.

Scope discipline (v3 §2.1 / §6): **read only**. We expose get-events /
pod-status / deployment-status and nothing that mutates cluster state. Any write
action (rollout undo, scale, delete) is the action-registry's job (v3 §5.3), not
this module — that boundary is enforced here by simply never importing a write API.

Service → k8s object mapping: the demo workloads label pods/deployments with
`app=<service_name>` (NOT `app.kubernetes.io/name`), and also carry `git_version`
as a pod label — so we can read the running version straight off the pod. Both
the namespace and the label key are config (`k8s_namespace` / `k8s_label_key`) so
this isn't pinned to the demo's conventions.

Auth: `load_incluster_config()` in the pod (read-only ServiceAccount, v3 §6.3),
falling back to the local kubeconfig for host-side dev against k3d. The
kubernetes client is synchronous, so every call is wrapped in `asyncio.to_thread`
to stay off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..config import settings

logger = logging.getLogger("aiops_agent.k8s")

# Event reasons that actually carry incident signal. A stable cluster emits a lot
# of routine Normal events (Scheduled, Pulled, Created, Started) — surfacing those
# is noise. These are the ones that explain a *failure*.
_INTERESTING_EVENT_REASONS = frozenset(
    {
        "OOMKilling",
        "OOMKilled",
        "Killing",
        "BackOff",
        "CrashLoopBackOff",
        "Failed",
        "FailedScheduling",
        "FailedMount",
        "FailedCreate",
        "Unhealthy",
        "ProbeWarning",
        "ProgressDeadlineExceeded",
        "Evicted",
        "Preempting",
        "NodeNotReady",
        "FailedKillPod",
        "ErrImagePull",
        "ImagePullBackOff",
        "InspectFailed",
    }
)

# Set once the kubernetes client + config load successfully. None means we
# haven't tried yet; an Exception cached here means config load failed and we
# should report "unavailable" rather than retry-spam the API every call.
_core_api = None
_apps_api = None
_load_error: str | None = None


def _load_client() -> tuple[Any, Any]:
    """Lazily init the k8s clients. Tries in-cluster first (the pod's read-only
    SA), then the local kubeconfig (host-side dev against k3d). Cached."""
    global _core_api, _apps_api, _load_error
    if _core_api is not None and _apps_api is not None:
        return _core_api, _apps_api
    if _load_error is not None:
        raise RuntimeError(_load_error)

    from kubernetes import client, config  # imported lazily so the dep is only
    from kubernetes.config.config_exception import ConfigException  # needed when k8s is wired

    try:
        try:
            config.load_incluster_config()
        except ConfigException:
            config.load_kube_config()
    except Exception as e:  # no in-cluster SA AND no kubeconfig → k8s not wired
        _load_error = f"kubernetes config not available ({type(e).__name__}: {e})"
        raise RuntimeError(_load_error) from e

    _core_api = client.CoreV1Api()
    _apps_api = client.AppsV1Api()
    return _core_api, _apps_api


def _unavailable(detail: str) -> dict[str, Any]:
    """Uniform 'k8s not reachable' result. Returned (not raised) so the agent
    reports it as a finding and does NOT burn a retry re-issuing the same call."""
    return {
        "unavailable": True,
        "detail": detail,
        "note": "Kubernetes is not reachable from the agent; skip k8s checks for "
        "this turn and rely on metrics/logs/traces.",
    }


def _age(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    delta = datetime.now(UTC) - ts
    secs = int(delta.total_seconds())
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _selector() -> str:
    return f"{settings.k8s_label_key}={{svc}}"


# ---- pod status ------------------------------------------------------------


def _summarize_pod(pod) -> dict[str, Any]:
    st = pod.status
    statuses = st.container_statuses or []
    restarts = sum(cs.restart_count for cs in statuses)
    ready = sum(1 for cs in statuses if cs.ready)

    # The waiting reason (CrashLoopBackOff / ImagePullBackOff) and the LAST
    # terminated reason (OOMKilled / Error) are where the platform cause lives.
    waiting, last_terminated = [], []
    for cs in statuses:
        state = cs.state
        if state and state.waiting and state.waiting.reason:
            waiting.append(state.waiting.reason)
        last = getattr(cs, "last_state", None)
        if last and last.terminated and last.terminated.reason:
            last_terminated.append(
                f"{last.terminated.reason}"
                + (
                    f"(exit {last.terminated.exit_code})"
                    if last.terminated.exit_code is not None
                    else ""
                )
            )

    labels = pod.metadata.labels or {}
    return {
        "pod": pod.metadata.name,
        "phase": st.phase,
        "ready": f"{ready}/{len(statuses)}" if statuses else "0/0",
        "restarts": restarts,
        "waiting_reasons": sorted(set(waiting)),
        "last_terminated": sorted(set(last_terminated)),
        "git_version": labels.get("git_version"),
        "node": pod.spec.node_name if pod.spec else None,
        "age": _age(pod.status.start_time),
    }


async def get_pod_status(service: str) -> dict[str, Any]:
    """Pod phase / readiness / restart counts / crash reasons for a service."""
    try:
        core, _ = await asyncio.to_thread(_load_client)
        resp = await asyncio.to_thread(
            core.list_namespaced_pod,
            namespace=settings.k8s_namespace,
            label_selector=_selector().format(svc=service),
        )
    except RuntimeError as e:
        return _unavailable(str(e))
    except Exception as e:
        logger.warning("get_pod_status(%s) failed: %s", service, e)
        return _unavailable(f"k8s API error: {type(e).__name__}: {e}")

    pods = [_summarize_pod(p) for p in resp.items]
    return {
        "service": service,
        "namespace": settings.k8s_namespace,
        "pod_count": len(pods),
        "pods": pods,
    }


# ---- events ----------------------------------------------------------------


async def get_k8s_events(service: str, limit: int = 20) -> dict[str, Any]:
    """Recent *interesting* k8s events for a service's objects (pods / rs /
    deployment). Routine Normal events are filtered out — only the reasons that
    explain a failure (OOM, BackOff, FailedScheduling, Unhealthy, …) are kept."""
    try:
        core, _ = await asyncio.to_thread(_load_client)
        resp = await asyncio.to_thread(core.list_namespaced_event, namespace=settings.k8s_namespace)
    except RuntimeError as e:
        return _unavailable(str(e))
    except Exception as e:
        logger.warning("get_k8s_events(%s) failed: %s", service, e)
        return _unavailable(f"k8s API error: {type(e).__name__}: {e}")

    # Objects for a service are named `<service>`, `<service>-<rs-hash>`,
    # `<service>-<rs-hash>-<pod-hash>` — all share the `<service>` prefix.
    prefix = f"{service}-"
    events = []
    for ev in resp.items:
        obj = ev.involved_object
        name = obj.name if obj else ""
        if not (name == service or name.startswith(prefix)):
            continue
        if ev.type == "Normal" and ev.reason not in _INTERESTING_EVENT_REASONS:
            continue
        ts = (
            ev.last_timestamp
            or ev.event_time
            or (ev.metadata.creation_timestamp if ev.metadata else None)
        )
        events.append(
            {
                "type": ev.type,
                "reason": ev.reason,
                "object": f"{obj.kind}/{name}" if obj else name,
                "message": (ev.message or "").strip(),
                "count": ev.count,
                "age": _age(ts),
                "_ts": ts,
            }
        )

    # Most recent first; drop the sort key before returning.
    events.sort(key=lambda e: e["_ts"] or datetime.min.replace(tzinfo=UTC), reverse=True)
    for e in events:
        e.pop("_ts", None)
    return {
        "service": service,
        "namespace": settings.k8s_namespace,
        "event_count": len(events),
        "events": events[:limit],
    }


# ---- deployment / rollout status -------------------------------------------


async def get_deployment_status(service: str) -> dict[str, Any]:
    """Deployment replica health + rollout conditions + current revision. A
    rollout stuck (ProgressDeadlineExceeded) or replicas not Available points at
    a deploy that never became healthy — distinct from a code regression."""
    try:
        _, apps = await asyncio.to_thread(_load_client)
        dep = await asyncio.to_thread(
            apps.read_namespaced_deployment,
            name=service,
            namespace=settings.k8s_namespace,
        )
    except RuntimeError as e:
        return _unavailable(str(e))
    except Exception as e:
        # 404 → no deployment named <service>; report it as a finding, not a crash.
        logger.warning("get_deployment_status(%s) failed: %s", service, e)
        status = getattr(e, "status", None)
        if status == 404:
            return {
                "service": service,
                "namespace": settings.k8s_namespace,
                "found": False,
                "note": f"no Deployment named '{service}' in this namespace",
            }
        return _unavailable(f"k8s API error: {type(e).__name__}: {e}")

    st = dep.status
    conditions = [
        {
            "type": c.type,
            "status": c.status,
            "reason": c.reason,
            "message": (c.message or "").strip(),
        }
        for c in (st.conditions or [])
    ]
    annotations = dep.metadata.annotations or {}
    return {
        "service": service,
        "namespace": settings.k8s_namespace,
        "found": True,
        "git_version": (dep.spec.template.metadata.labels or {}).get("git_version")
        if dep.spec and dep.spec.template and dep.spec.template.metadata
        else None,
        "revision": annotations.get("deployment.kubernetes.io/revision"),
        "desired_replicas": dep.spec.replicas if dep.spec else None,
        "ready_replicas": st.ready_replicas or 0,
        "available_replicas": st.available_replicas or 0,
        "updated_replicas": st.updated_replicas or 0,
        "unavailable_replicas": st.unavailable_replicas or 0,
        "conditions": conditions,
    }


# ---- agent-facing tools ----------------------------------------------------


class ServiceArg(BaseModel):
    service: str = Field(description="Exact service_name, e.g. payment-service.")


k8s_pod_status_tool = StructuredTool(
    name="k8s_pod_status",
    description="Read a service's pod health from Kubernetes: phase, readiness, "
    "restart counts, and crash reasons (CrashLoopBackOff, OOMKilled, "
    "ImagePullBackOff). Use to tell a platform-level failure apart from "
    "a code regression when an incident sits on a deploy boundary.",
    args_schema=ServiceArg,
    coroutine=get_pod_status,
)

k8s_events_tool = StructuredTool(
    name="k8s_events",
    description="Recent failure-related Kubernetes events for a service (OOMKilling, "
    "BackOff, FailedScheduling, Unhealthy, ProgressDeadlineExceeded). Use "
    "to find infra-level causes: pod restarts, scheduling pressure, probe "
    "failures, evictions.",
    args_schema=ServiceArg,
    coroutine=get_k8s_events,
)

k8s_deployment_status_tool = StructuredTool(
    name="k8s_deployment_status",
    description="Deployment rollout health for a service: desired vs available "
    "replicas, rollout conditions (e.g. ProgressDeadlineExceeded), current "
    "revision and git_version. Use to check whether a deploy actually "
    "became healthy.",
    args_schema=ServiceArg,
    coroutine=get_deployment_status,
)


def _template_fingerprint(rs) -> dict[str, Any]:
    """The parts of a pod template that change what the process actually runs.

    Deliberately not a full diff: `kubectl rollout restart` writes a timestamp
    annotation, which makes every field-by-field comparison report a change and
    tells the reader nothing. What matters for attribution is narrower — the
    image, the env, and which ConfigMaps/Secrets are mounted.
    """
    spec = rs.spec.template.spec if rs.spec and rs.spec.template else None
    containers = getattr(spec, "containers", None) or []
    c = containers[0] if containers else None
    env = {}
    for e in getattr(c, "env", None) or []:
        if e.value is not None:
            env[e.name] = e.value
    sources = []
    for v in getattr(spec, "volumes", None) or []:
        if getattr(v, "config_map", None):
            sources.append(f"configMap/{v.config_map.name}")
        if getattr(v, "secret", None):
            sources.append(f"secret/{v.secret.secret_name}")
    labels = (rs.spec.template.metadata.labels or {}) if rs.spec and rs.spec.template else {}
    return {
        "image": getattr(c, "image", None),
        "env": env,
        "mounted_config": sorted(sources),
        "git_version": labels.get("git_version"),
    }


async def get_change_provenance(service: str, limit: int = 4) -> dict[str, Any]:
    """Did the last rollout change what runs, or only restart it?

    This exists because of a mistake the agent made four times in a row: an
    alert carrying a `git_version` label was reported as "a code regression in
    that version" whether the fault shipped in the pod template or in a
    ConfigMap the unchanged template mounts. Those two look identical from
    metrics, and the fix for one does nothing for the other — `rollout undo`
    restores a template that was never the problem.

    So the answer is not "which version is running" but "what actually changed
    between the last revisions, and what config is mounted from outside them".
    """
    try:
        _, apps = await asyncio.to_thread(_load_client)
        rs_list = await asyncio.to_thread(
            apps.list_namespaced_replica_set,
            namespace=settings.k8s_namespace,
            label_selector=f"app={service}",
        )
    except RuntimeError as e:
        return _unavailable(str(e))
    except Exception as e:
        logger.warning("get_change_provenance(%s) failed: %s", service, e)
        return _unavailable(f"k8s API error: {type(e).__name__}: {e}")

    def _rev(rs) -> int:
        try:
            return int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", 0))
        except (TypeError, ValueError):
            return 0

    revisions = sorted(rs_list.items, key=_rev)[-max(2, limit) :]
    if not revisions:
        return {
            "service": service,
            "found": False,
            "note": f"no ReplicaSets labelled app={service} in this namespace",
        }

    rows, prev = [], None
    for rs in revisions:
        fp = _template_fingerprint(rs)
        changed: list[str] = []
        if prev is not None:
            if fp["image"] != prev["image"]:
                changed.append("image")
            if fp["env"] != prev["env"]:
                changed.append("env")
            if fp["mounted_config"] != prev["mounted_config"]:
                changed.append("mounted_config")
            if fp["git_version"] != prev["git_version"]:
                changed.append("git_version(label only)")
        rows.append(
            {
                "revision": _rev(rs),
                "git_version": fp["git_version"],
                "image": fp["image"],
                "mounted_config": fp["mounted_config"],
                "changed_vs_previous": changed if prev is not None else None,
                "created": _age(rs.metadata.creation_timestamp),
            }
        )
        prev = fp

    latest = rows[-1]
    substantive = [c for c in (latest["changed_vs_previous"] or []) if "label only" not in c]
    if latest["changed_vs_previous"] is None:
        verdict = "only one revision known; nothing to compare"
    elif substantive:
        verdict = (
            f"the last rollout changed {', '.join(substantive)} — a rollback restores a "
            "genuinely different pod template"
        )
    else:
        verdict = (
            "the last rollout changed nothing the process runs (at most a version label or a "
            "restart). If behaviour changed, the cause is outside the template — check the "
            f"mounted config: {', '.join(latest['mounted_config']) or 'none'}"
        )

    return {
        "service": service,
        "namespace": settings.k8s_namespace,
        "found": True,
        "revisions": rows,
        "verdict": verdict,
    }


class ProvenanceArg(BaseModel):
    service: str = Field(description="Exact service_name, e.g. payment-service.")
    limit: int = Field(default=4, description="How many recent revisions to compare (min 2).")


k8s_change_provenance_tool = StructuredTool(
    name="k8s_change_provenance",
    description="What the last rollouts actually changed for a service: image, env and "
    "mounted ConfigMaps/Secrets per revision. Use BEFORE blaming a deploy: a "
    "version label changing is not a code change, and a fault that lives in a "
    "mounted ConfigMap survives any rollback.",
    args_schema=ProvenanceArg,
    coroutine=get_change_provenance,
)
