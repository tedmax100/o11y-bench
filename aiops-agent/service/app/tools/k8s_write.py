"""Write-only Kubernetes remediation client — step 7b-4.

This is the ONLY module that mutates cluster state. Deliberately separate from
tools/k8s.py (read-only) to enforce the SA split enforced by RBAC:

  read SA  (aiops-agent)       — get/list/watch pods, events, deployments, replicasets
  write SA (aiops-agent-write) — get/list/watch + patch/update deployments;
                                  NO delete. Token mounted at
                                  /var/run/secrets/k8s-write/token (projected volume).

The impl_* functions here are wired into actions.py's ActionSpec.impl. They are
never called directly — only through registry.execute() which enforces the
kill switch + impl-exists gate. Both read the current state via the existing
read client (k8s._load_client) to avoid granting the write SA broader permissions
than the patch it needs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..config import settings
from . import k8s

logger = logging.getLogger("aiops_agent.k8s_write")

_WRITE_TOKEN_PATH = "/var/run/secrets/k8s-write/token"
_CLUSTER_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

_write_api = None
_write_error: str | None = None


def _load_write_api() -> Any:
    """Return AppsV1Api bound to the write SA credentials. Cached.
    In-cluster: uses the projected write SA token. Host-side dev: falls back
    to the local kubeconfig (which has full perms — dev only)."""
    global _write_api, _write_error
    if _write_api is not None:
        return _write_api
    if _write_error is not None:
        raise RuntimeError(_write_error)

    try:
        from kubernetes import client, config
        from kubernetes.config.config_exception import ConfigException

        if Path(_WRITE_TOKEN_PATH).exists():
            cfg = client.Configuration()
            cfg.host = "https://kubernetes.default.svc"
            cfg.ssl_ca_cert = _CLUSTER_CA_PATH
            cfg.api_key["authorization"] = Path(_WRITE_TOKEN_PATH).read_text().strip()
            cfg.api_key_prefix["authorization"] = "Bearer"
            _write_api = client.AppsV1Api(client.ApiClient(configuration=cfg))
        else:
            try:
                config.load_incluster_config()
            except ConfigException:
                config.load_kube_config()
            _write_api = client.AppsV1Api()

        return _write_api
    except Exception as e:
        _write_error = f"k8s write client unavailable ({type(e).__name__}: {e})"
        raise RuntimeError(_write_error) from e


def _ns(args: dict) -> str:
    return args.get("namespace", settings.k8s_namespace)


async def impl_rollout_undo(args: dict) -> dict:
    """Roll a deployment back to its previous ReplicaSet — the write half of
    'kubectl rollout undo'. Reads current state via the read SA, writes via
    the write SA (patch only, no delete)."""
    deployment = args["deployment"]
    ns = _ns(args)

    # --- read phase (read SA) ------------------------------------------------
    _, apps_r = await asyncio.to_thread(k8s._load_client)

    dep = await asyncio.to_thread(apps_r.read_namespaced_deployment, deployment, ns)
    annotations = dep.metadata.annotations or {}
    current_rev = int(annotations.get("deployment.kubernetes.io/revision", "0"))

    selector = ",".join(
        f"{k}={v}" for k, v in dep.spec.selector.match_labels.items()
    )
    rs_list = await asyncio.to_thread(
        apps_r.list_namespaced_replica_set, ns, label_selector=selector
    )

    # Find the RS with revision == current_rev - 1 (the "previous" revision)
    prev_rs = None
    for rs in rs_list.items:
        rs_ann = rs.metadata.annotations or {}
        rev = int(rs_ann.get("deployment.kubernetes.io/revision", "0"))
        if rev == current_rev - 1:
            prev_rs = rs
            break

    if prev_rs is None:
        raise RuntimeError(
            f"no previous ReplicaSet found for {deployment} in {ns} "
            f"(current revision {current_rev}); cannot roll back"
        )

    # --- write phase (write SA) ----------------------------------------------
    apps_w = await asyncio.to_thread(_load_write_api)

    # Overwrite deployment's spec.template with the previous RS's template.
    # Use sanitize_for_serialization to get proper camelCase keys — to_dict()
    # returns snake_case which breaks strategic merge patch (merge key lookup
    # fails for e.g. "container_port" vs the expected "containerPort").
    from kubernetes import client as k8s_client
    sanitize = k8s_client.ApiClient().sanitize_for_serialization
    prev_template = sanitize(prev_rs.spec.template)

    # Strip revision annotation from the template so the controller re-assigns it.
    tmpl_meta = prev_template.get("metadata") or {}
    tmpl_ann = tmpl_meta.get("annotations") or {}
    tmpl_ann.pop("deployment.kubernetes.io/revision", None)
    tmpl_meta["annotations"] = tmpl_ann
    prev_template["metadata"] = tmpl_meta

    patch = {"spec": {"template": prev_template}}
    await asyncio.to_thread(
        apps_w.patch_namespaced_deployment,
        name=deployment, namespace=ns, body=patch,
    )

    prev_images = [c.image for c in prev_rs.spec.template.spec.containers]
    logger.warning(
        "rollout_undo: %s/%s rev %d→%d images=%s",
        ns, deployment, current_rev, current_rev - 1, prev_images,
    )
    return {
        "action": "rollout_undo",
        "deployment": deployment,
        "namespace": ns,
        "rolled_back_to_revision": current_rev - 1,
        "images": prev_images,
    }


async def impl_scale(args: dict) -> dict:
    """Scale a deployment to the requested replica count."""
    deployment = args["deployment"]
    ns = _ns(args)
    replicas = int(args["replicas"])

    # Read current state (read SA)
    _, apps_r = await asyncio.to_thread(k8s._load_client)
    dep = await asyncio.to_thread(apps_r.read_namespaced_deployment, deployment, ns)
    old_replicas = dep.spec.replicas

    # Write (write SA)
    apps_w = await asyncio.to_thread(_load_write_api)
    await asyncio.to_thread(
        apps_w.patch_namespaced_deployment,
        name=deployment, namespace=ns,
        body={"spec": {"replicas": replicas}},
    )

    logger.warning(
        "scale: %s/%s %d→%d", ns, deployment, old_replicas, replicas
    )
    return {
        "action": "scale",
        "deployment": deployment,
        "namespace": ns,
        "previous_replicas": old_replicas,
        "new_replicas": replicas,
    }


# rollout undo's inverse is another rollout undo (go back to the version we left)
rollback_rollout_undo = impl_rollout_undo
