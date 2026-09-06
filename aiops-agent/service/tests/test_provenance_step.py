"""Provenance as a step, and the prose it is supposed to settle.

Two measurements sit behind this file. The tool was called by the model in
1 of 20 runs, so it stopped being a tool the model may reach for and became a
step every alert run takes. And once the `suspected_version` field was settled
from the cluster, 20 of 20 runs had a clean field while 20 of 20 still said
"code regression in v2.5.0" in the sentence — the error moved from the field to
the prose, under a criterion that only read the field.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app import agent


def _rs(rev, *, image, git_version, config_maps=("payment-flags",)):
    container = SimpleNamespace(image=image, env=[])
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


_RESTART_ONLY = [
    _rs(70, image="payment:dev", git_version="v2.5.0"),
    _rs(71, image="payment:dev", git_version="v2.5.0"),
]
_REAL_DEPLOY = [
    _rs(70, image="payment:v2.4.1", git_version="v2.4.1"),
    _rs(71, image="payment:v2.5.0", git_version="v2.5.0"),
]


@pytest.mark.asyncio
async def test_the_run_asks_the_cluster_without_being_asked():
    msgs: list = []
    with _patched(_RESTART_ONLY):
        await agent._inject_change_provenance(msgs, ["payment-service"])
    assert len(msgs) == 1
    content = msgs[0].content
    assert "CHANGE PROVENANCE" in content
    assert "outside the template" in content
    assert "configMap/payment-flags" in content


@pytest.mark.asyncio
async def test_a_real_deploy_is_not_talked_out_of_being_a_deploy():
    """The step must not become a blanket instruction never to blame a version."""
    msgs: list = []
    with _patched(_REAL_DEPLOY):
        await agent._inject_change_provenance(msgs, ["payment-service"])
    assert "genuinely different pod template" in msgs[0].content
    assert agent._provenance_unchanged.get() == {}


@pytest.mark.asyncio
async def test_an_unreachable_cluster_leaves_the_run_alone():
    msgs: list = []
    with patch("app.tools.k8s._load_client", side_effect=RuntimeError("no kubeconfig")):
        await agent._inject_change_provenance(msgs, ["payment-service"])
    assert msgs == []
    assert agent._provenance_unchanged.get() == {}


@pytest.mark.asyncio
async def test_the_answer_may_not_blame_a_version_the_cluster_cleared():
    msgs: list = []
    with _patched(_RESTART_ONLY):
        await agent._inject_change_provenance(msgs, ["payment-service"])
    assert agent._provenance_unchanged.get() == {"payment-service": "v2.5.0"}

    ok, retry = agent._provenance_check(
        "Code regression in payment-service v2.5.0: the new validator rejects odd cents."
    )
    assert ok is False
    assert "v2.5.0" in retry
    assert "mounted config" in retry


@pytest.mark.asyncio
async def test_the_right_answer_passes_untouched():
    msgs: list = []
    with _patched(_RESTART_ONLY):
        await agent._inject_change_provenance(msgs, ["payment-service"])
    ok, retry = agent._provenance_check(
        "Configuration regression in the payment-flags ConfigMap: "
        "payment_use_new_validator was flipped on."
    )
    assert (ok, retry) == (True, "")


def test_no_verdict_means_no_opinion():
    """Fail-open: a run where provenance never answered must not be second-guessed."""
    agent._provenance_unchanged.set({})
    assert agent._provenance_check("Code regression in v2.5.0.") == (True, "")
