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
_write_authz_api = None
_write_error: str | None = None
_write_token_fp: tuple[int, int] | None = None


def in_cluster_write_creds() -> bool:
    """True when the projected write-SA token is mounted — i.e. we are holding
    the restricted credential and not a developer's full-permission kubeconfig.
    The distinction matters to any check that asks "what am I allowed to do":
    on a dev kubeconfig the answer is "everything", which proves nothing about
    what the deployed agent can do."""
    return Path(_WRITE_TOKEN_PATH).exists()


def _token_fingerprint() -> tuple[int, int] | None:
    try:
        st = Path(_WRITE_TOKEN_PATH).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _invalidate_on_rotation() -> None:
    """Drop the cached clients when the projected token has been rewritten.

    kubelet refreshes bound ServiceAccount tokens *in place*, and a cached
    ApiClient keeps presenting the bearer string it read at startup. That is how
    a credential stays dead for weeks while every client object in the process
    still looks perfectly healthy: nothing re-reads the file, and the only code
    path that would notice is the one that runs once a quarter."""
    global _write_api, _write_authz_api, _write_error, _write_token_fp
    fp = _token_fingerprint()
    if fp != _write_token_fp:
        if _write_api is not None or _write_error is not None:
            logger.info("write token changed on disk; rebuilding client")
        _write_api = _write_authz_api = None
        _write_error = None
        _write_token_fp = fp


def _build_write_clients() -> tuple[Any, Any]:
    """(AppsV1Api, AuthorizationV1Api) on the same credentials. The authz client
    is built from the identical Configuration on purpose — a readiness check that
    asks a different client is answering about a different identity."""
    from kubernetes import client, config
    from kubernetes.config.config_exception import ConfigException

    if in_cluster_write_creds():
        cfg = client.Configuration()
        cfg.host = "https://kubernetes.default.svc"
        cfg.ssl_ca_cert = _CLUSTER_CA_PATH
        # Both entries are keyed by the *scheme name* the generated client looks
        # up, not by the header name. `get_api_key_with_prefix("BearerToken",
        # alias="authorization")` finds the key under either name but only ever
        # reads the prefix under "BearerToken" — so keying the prefix on
        # "authorization" (which reads perfectly sensibly) silently sends the raw
        # JWT with no `Bearer ` in front of it, the API server can't parse the
        # header, and every call comes back 401 Unauthorized.
        #
        # That 401 is the one this system spent months believing was an expired
        # token: the credential was always valid, the header was always malformed,
        # and the two are indistinguishable from the outside. Keep both keys.
        token = Path(_WRITE_TOKEN_PATH).read_text().strip()
        cfg.api_key["BearerToken"] = token
        cfg.api_key["authorization"] = token
        cfg.api_key_prefix["BearerToken"] = "Bearer"
        cfg.api_key_prefix["authorization"] = "Bearer"
        api_client = client.ApiClient(configuration=cfg)
    else:
        try:
            config.load_incluster_config()
        except ConfigException:
            config.load_kube_config()
        api_client = client.ApiClient()
    return client.AppsV1Api(api_client), client.AuthorizationV1Api(api_client)


def _load_write_api() -> Any:
    """Return AppsV1Api bound to the write SA credentials. Cached, but the cache
    is dropped when the token file changes (see `_invalidate_on_rotation`).
    In-cluster: uses the projected write SA token. Host-side dev: falls back
    to the local kubeconfig (which has full perms — dev only)."""
    global _write_api, _write_authz_api, _write_error
    _invalidate_on_rotation()
    if _write_api is not None:
        return _write_api
    if _write_error is not None:
        raise RuntimeError(_write_error)

    try:
        _write_api, _write_authz_api = _build_write_clients()
        return _write_api
    except Exception as e:
        _write_error = f"k8s write client unavailable ({type(e).__name__}: {e})"
        raise RuntimeError(_write_error) from e


def load_write_authz_api() -> Any:
    """AuthorizationV1Api on the write credentials — for SelfSubjectAccessReview
    preflights. Same cache and same rotation handling as the write client."""
    _load_write_api()
    return _write_authz_api


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

    selector = ",".join(f"{k}={v}" for k, v in dep.spec.selector.match_labels.items())
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
        name=deployment,
        namespace=ns,
        body=patch,
    )

    prev_images = [c.image for c in prev_rs.spec.template.spec.containers]
    logger.warning(
        "rollout_undo: %s/%s rev %d→%d images=%s",
        ns,
        deployment,
        current_rev,
        current_rev - 1,
        prev_images,
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
        name=deployment,
        namespace=ns,
        body={"spec": {"replicas": replicas}},
    )

    logger.warning("scale: %s/%s %d→%d", ns, deployment, old_replicas, replicas)
    return {
        "action": "scale",
        "deployment": deployment,
        "namespace": ns,
        "previous_replicas": old_replicas,
        "new_replicas": replicas,
    }


# rollout undo's inverse is another rollout undo (go back to the version we left)
rollback_rollout_undo = impl_rollout_undo
