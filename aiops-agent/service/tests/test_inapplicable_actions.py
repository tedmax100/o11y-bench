"""A right diagnosis can still carry an action that cannot work.

The run these tests come from got the ConfigMap answer right and proposed
`k8s.rollout_undo` anyway, because the action came from the runbook and not
from what the run had just found out. These pin the guard that stops it.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.actions import registry
from app.governance import Autonomy, decide, inapplicable_by_provenance, propose_remediations

CALIB_OK = {"labeled": 0, "overconfidence": None}

TEMPLATE_UNCHANGED = {
    "service": "payment-service",
    "found": True,
    "revisions": [
        {"revision": 71, "mounted_config": ["configMap/payment-flags"], "changed_vs_previous": []}
    ],
    "verdict": "the last rollout changed nothing the process runs (at most a version label or a "
    "restart). If behaviour changed, the cause is outside the template — check the "
    "mounted config: configMap/payment-flags",
}

REAL_DEPLOY = {
    "service": "payment-service",
    "found": True,
    "revisions": [
        {
            "revision": 71,
            "mounted_config": ["configMap/payment-flags"],
            "changed_vs_previous": ["image"],
        }
    ],
    "verdict": "the last rollout changed image — a rollback restores a genuinely different "
    "pod template",
}


def _patch_prov(payload):
    return patch("app.tools.k8s.get_change_provenance", AsyncMock(return_value=payload))


@pytest.mark.asyncio
async def test_rollback_is_ruled_out_when_the_template_did_not_change():
    with _patch_prov(TEMPLATE_UNCHANGED):
        out = await inapplicable_by_provenance("payment-service")
    assert "k8s.rollout_undo" in out
    assert "configMap/payment-flags" in out["k8s.rollout_undo"]


@pytest.mark.asyncio
async def test_a_real_deploy_leaves_the_rollback_alone():
    with _patch_prov(REAL_DEPLOY):
        assert await inapplicable_by_provenance("payment-service") == {}


@pytest.mark.asyncio
async def test_unreachable_cluster_fails_open():
    """Failing closed here would silently strip every proposal whenever k8s is
    unreachable, which is the opposite of what the on-call needs."""
    with _patch_prov({"unavailable": True, "detail": "no kubeconfig"}):
        assert await inapplicable_by_provenance("payment-service") == {}
    with patch("app.tools.k8s.get_change_provenance", AsyncMock(side_effect=RuntimeError("boom"))):
        assert await inapplicable_by_provenance("payment-service") == {}


@pytest.mark.asyncio
async def test_no_service_means_no_opinion():
    assert await inapplicable_by_provenance(None) == {}


def test_decide_escalates_with_the_reason_instead_of_proposing():
    spec = registry.get("k8s.rollout_undo")
    assert spec is not None
    reason = "the cluster says the last rollouts changed nothing the process runs"
    d = decide(spec, 0.95, CALIB_OK, inapplicable=reason)
    assert d.autonomy is Autonomy.ESCALATE
    assert d.reason == reason
    assert d.requires_human is True


def test_high_confidence_does_not_override_it():
    """Confidence is about the diagnosis. Whether the fix can work is not a
    thing the model is more or less sure about."""
    spec = registry.get("k8s.rollout_undo")
    d = decide(spec, 1.0, CALIB_OK, inapplicable="cannot work here")
    assert d.autonomy is Autonomy.ESCALATE


def test_only_the_named_action_is_affected():
    names = ["k8s.rollout_undo", "k8s.configmap_flag_set"]
    decisions = propose_remediations(
        names,
        0.9,
        CALIB_OK,
        inapplicable={"k8s.rollout_undo": "cannot work here"},
    )
    by_action = {d.action: d for d in decisions}
    assert by_action["k8s.rollout_undo"].autonomy is Autonomy.ESCALATE
    assert by_action["k8s.configmap_flag_set"].autonomy is not Autonomy.ESCALATE
