"""The flag flip that also restarts (day36).

payment-service reads its flags at process start, so patching the ConfigMap and
stopping there is a fix that cannot take effect — the failure this system spent
two days learning to refuse, arriving from the other direction. The restart is
part of the action, not a step somebody remembers afterwards.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import blast_radius
from app.runbook import load_runbooks
from app.tools import k8s_write


def _cm(doc: str):
    cm = MagicMock()
    cm.data = {"flags.json": doc}
    return cm


def _dep(name: str, replicas: int, configmap: str | None):
    d = MagicMock()
    d.metadata.name = name
    d.spec.replicas = replicas
    vol = MagicMock()
    if configmap:
        vol.config_map.name = configmap
    else:
        vol.config_map = None
    d.spec.template.spec.volumes = [vol]
    d.spec.template.spec.containers = []
    return d


@pytest.mark.asyncio
async def test_no_restart_arg_leaves_the_deployment_alone():
    core = MagicMock()
    core.read_namespaced_config_map.return_value = _cm('{"f": true}')
    apps = MagicMock()
    with (
        patch.object(k8s_write.k8s, "_load_client", return_value=(core, MagicMock())),
        patch.object(k8s_write, "_load_write_core_api", return_value=core),
        patch.object(k8s_write, "_load_write_api", return_value=apps),
    ):
        out = await k8s_write.impl_configmap_flag_set(
            {"configmap": "payment-flags", "flag": "f", "value": False}
        )
    assert out["previous_value"] is True and out["new_value"] is False
    assert "restarted" not in out
    apps.patch_namespaced_deployment.assert_not_called()


@pytest.mark.asyncio
async def test_restart_stamps_the_pod_template_with_kubectls_annotation():
    """The same annotation kubectl uses, so the two mechanisms leave one history."""
    core = MagicMock()
    core.read_namespaced_config_map.return_value = _cm('{"f": true}')
    apps = MagicMock()
    with (
        patch.object(k8s_write.k8s, "_load_client", return_value=(core, MagicMock())),
        patch.object(k8s_write, "_load_write_core_api", return_value=core),
        patch.object(k8s_write, "_load_write_api", return_value=apps),
    ):
        out = await k8s_write.impl_configmap_flag_set(
            {
                "configmap": "payment-flags",
                "flag": "f",
                "value": False,
                "restart_deployment": "payment-service",
            }
        )
    assert out["restarted"]["deployment"] == "payment-service"
    body = apps.patch_namespaced_deployment.call_args.kwargs["body"]
    ann = body["spec"]["template"]["metadata"]["annotations"]
    assert "kubectl.kubernetes.io/restartedAt" in ann
    # the flag was still written; a restart alone would put the old value back
    core.patch_namespaced_config_map.assert_called_once()


@pytest.mark.asyncio
async def test_blast_radius_counts_the_pods_the_restart_replaces():
    core = MagicMock()
    core.read_namespaced_config_map.return_value = _cm('{"f": true}')
    deps = MagicMock()
    deps.items = [_dep("payment-service", 2, "payment-flags")]
    # three hops: load the client, read the ConfigMap, list the Deployments
    with patch.object(
        blast_radius.asyncio,
        "to_thread",
        new=AsyncMock(side_effect=[(core, MagicMock()), core.read_namespaced_config_map(), deps]),
    ):
        br = await blast_radius.dry_run_configmap_flag_set(
            {
                "configmap": "payment-flags",
                "flag": "f",
                "value": False,
                "restart_deployment": "payment-service",
            }
        )
    assert any("restarts payment-service (2 pod(s))" in n for n in br.notes)
    assert br.singleton is False


@pytest.mark.asyncio
async def test_restarting_something_that_does_not_read_the_map_is_called_out():
    """A restart of a workload that never mounts the map fixes nothing, and the
    two arguments being one typo apart is exactly how that happens."""
    core = MagicMock()
    core.read_namespaced_config_map.return_value = _cm('{"f": true}')
    deps = MagicMock()
    deps.items = [_dep("payment-service", 1, "payment-flags"), _dep("order-service", 1, None)]
    # three hops: load the client, read the ConfigMap, list the Deployments
    with patch.object(
        blast_radius.asyncio,
        "to_thread",
        new=AsyncMock(side_effect=[(core, MagicMock()), core.read_namespaced_config_map(), deps]),
    ):
        br = await blast_radius.dry_run_configmap_flag_set(
            {
                "configmap": "payment-flags",
                "flag": "f",
                "value": False,
                "restart_deployment": "order-service",
            }
        )
    assert any("does not mount this ConfigMap" in n for n in br.notes)


def test_the_payment_branch_is_executable_and_its_undo_restarts_too():
    rb = next(b for b in load_runbooks("runbooks") if b.id == "payment-bad-deploy")
    step = next(s for s in rb.remediation if s.action == "k8s.configmap_flag_set")
    assert step.args["restart_deployment"] == "payment-service"
    # the rollback must restart as well, or the file and the process disagree
    assert step.rollback["args"]["restart_deployment"] == "payment-service"
    assert step.verify is not None


def test_the_session_cache_flag_carries_no_restart():
    """user-service re-reads per request; restarting it would be blast radius
    bought for nothing."""
    rb = next(b for b in load_runbooks("runbooks") if b.id == "session-cache-timeout")
    step = next(s for s in rb.remediation if s.action == "k8s.configmap_flag_set")
    assert "restart_deployment" not in step.args
