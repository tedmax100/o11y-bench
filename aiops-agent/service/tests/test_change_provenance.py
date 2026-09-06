"""Unit tests for k8s_change_provenance — the tool that exists because the agent
blamed a version four times for faults that never lived in the pod template.

The cases are the two drill shapes plus the one that motivated the tool: a
ConfigMap flip with a restart, where every revision runs the same image.
No cluster needed; the kubernetes client is mocked.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.tools.k8s import get_change_provenance


def _rs(rev, *, image, git_version, env=None, config_maps=("payment-flags",)):
    container = SimpleNamespace(
        image=image,
        env=[SimpleNamespace(name=k, value=v) for k, v in (env or {}).items()],
    )
    volumes = [
        SimpleNamespace(config_map=SimpleNamespace(name=cm), secret=None) for cm in config_maps
    ]
    template = SimpleNamespace(
        metadata=SimpleNamespace(labels={"git_version": git_version}),
        spec=SimpleNamespace(containers=[container], volumes=volumes),
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(
            annotations={"deployment.kubernetes.io/revision": str(rev)},
            creation_timestamp=datetime.now(UTC),
        ),
        spec=SimpleNamespace(template=template),
    )


def _patched(items):
    apps = MagicMock()
    apps.list_namespaced_replica_set.return_value = SimpleNamespace(items=items)
    return patch("app.tools.k8s._load_client", return_value=(MagicMock(), apps))


@pytest.mark.asyncio
async def test_config_flip_says_the_template_did_not_change():
    """The 2026-08-26 incident: same image every revision, only a restart. The
    verdict has to point outside the template or the agent will blame the tag."""
    items = [
        _rs(70, image="payment:dev", git_version="v2.5.0"),
        _rs(71, image="payment:dev", git_version="v2.5.0"),
    ]
    with _patched(items):
        out = await get_change_provenance("payment-service")
    assert out["found"] is True
    assert out["revisions"][-1]["changed_vs_previous"] == []
    assert "outside the template" in out["verdict"]
    assert "configMap/payment-flags" in out["verdict"]


@pytest.mark.asyncio
async def test_real_deploy_is_reported_as_a_real_deploy():
    """Drill scenario a: the image really did change, so a rollback restores a
    genuinely different template and blaming the deploy is the right answer."""
    items = [
        _rs(70, image="payment:v2.4.1", git_version="v2.4.1"),
        _rs(71, image="payment:v2.5.0", git_version="v2.5.0"),
    ]
    with _patched(items):
        out = await get_change_provenance("payment-service")
    assert "image" in out["revisions"][-1]["changed_vs_previous"]
    assert "genuinely different pod template" in out["verdict"]


@pytest.mark.asyncio
async def test_version_label_alone_is_not_a_code_change():
    """The trap in this demo: git_version is a pod-template label, not a build.
    A revision that only bumps it must not read as a deploy."""
    items = [
        _rs(70, image="payment:dev", git_version="v2.4.1"),
        _rs(71, image="payment:dev", git_version="v2.5.0"),
    ]
    with _patched(items):
        out = await get_change_provenance("payment-service")
    changed = out["revisions"][-1]["changed_vs_previous"]
    assert changed == ["git_version(label only)"]
    assert "outside the template" in out["verdict"]


@pytest.mark.asyncio
async def test_env_repointed_at_another_configmap_is_substantive():
    """Drill scenario a ships the bad flag file in the template, by repointing
    FEATURE_FLAGS_PATH and mounting a second ConfigMap."""
    items = [
        _rs(70, image="payment:dev", git_version="v2.5.0", env={"FEATURE_FLAGS_PATH": "/etc/a"}),
        _rs(
            71,
            image="payment:dev",
            git_version="v2.5.0",
            env={"FEATURE_FLAGS_PATH": "/etc/bad"},
            config_maps=("payment-flags", "payment-flags-bad"),
        ),
    ]
    with _patched(items):
        out = await get_change_provenance("payment-service")
    changed = out["revisions"][-1]["changed_vs_previous"]
    assert "env" in changed and "mounted_config" in changed
    assert "genuinely different pod template" in out["verdict"]


@pytest.mark.asyncio
async def test_no_replicasets_is_a_finding_not_a_crash():
    with _patched([]):
        out = await get_change_provenance("billing-service")
    assert out["found"] is False
    assert "billing-service" in out["note"]


@pytest.mark.asyncio
async def test_k8s_unreachable_is_reported_not_raised():
    with patch("app.tools.k8s._load_client", side_effect=RuntimeError("no kubeconfig")):
        out = await get_change_provenance("payment-service")
    assert out["unavailable"] is True


@pytest.mark.asyncio
async def test_the_fact_layer_types_it_as_a_change_source():
    """A new tool missing from the two tables in facts.py is silently demoted to
    unknown/context, which is exactly how it would stop counting as evidence."""
    from app.facts import classify

    items = [_rs(71, image="payment:dev", git_version="v2.5.0")]
    with _patched(items):
        out = await get_change_provenance("payment-service")
    fact = classify("k8s_change_provenance_tool", out, 1)
    assert fact.source_domain == "change"
    assert fact.role_hint == "trigger"
    assert fact.usable is True
