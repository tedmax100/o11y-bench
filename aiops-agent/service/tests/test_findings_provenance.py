"""The structured half of the ConfigMap fix.

Giving the agent the provenance tool fixed the *prose*: a fresh ConfigMap
incident now says "configuration regression in the payment-flags ConfigMap", and
the gate strikes `rollout_undo`. The same investigation still carried
`suspected_version: v2.5.0`, because the alert label says so and the extractor
reads the transcript rather than the cluster. Prose and field disagreeing is
worse than either being wrong alone — it only shows up if someone reads both,
and everything downstream of the row (webhook tags, case memory, grading) reads
the field.
"""

import asyncio

import pytest

import app.agent as agent_mod
from app.agent import Findings, _reconcile_version_with_provenance

_TEMPLATE_UNCHANGED = {
    "service": "payment-service",
    "found": True,
    "revisions": [{"revision": 71, "mounted_config": ["configMap/payment-flags"]}],
    "verdict": (
        "the last rollout changed nothing the process runs (at most a version label or a "
        "restart). If behaviour changed, the cause is outside the template — check the "
        "mounted config: configMap/payment-flags"
    ),
}

_TEMPLATE_CHANGED = {
    "service": "payment-service",
    "found": True,
    "revisions": [{"revision": 71, "mounted_config": ["configMap/payment-flags"]}],
    "verdict": (
        "the last rollout changed image — a rollback restores a genuinely different pod template"
    ),
}


def _findings(**kw) -> Findings:
    base = dict(
        summary="configuration regression in the payment-flags ConfigMap",
        hypothesis="new_validator was enabled in the ConfigMap",
        confidence=0.9,
        evidence=["logs: validator_rejected"],
        services=["payment-service"],
        suspected_version="v2.5.0",
    )
    base.update(kw)
    return Findings(**base)


def _prov(result):
    async def _f(service, *_a, **_kw):
        return dict(result, service=service)

    return _f


def _patch(monkeypatch, result):
    import app.tools.k8s as k8s

    monkeypatch.setattr(k8s, "get_change_provenance", _prov(result))


def test_version_is_dropped_when_the_template_never_changed(monkeypatch):
    _patch(monkeypatch, _TEMPLATE_UNCHANGED)
    f = _findings()
    asyncio.run(_reconcile_version_with_provenance(f))

    assert f.suspected_version is None, "the field still blames a version the cluster cleared"
    assert any("v2.5.0" in e and "payment-flags" in e for e in f.evidence), (
        "dropping the version silently is its own kind of lie; say why"
    )


def test_a_real_template_change_keeps_the_version(monkeypatch):
    _patch(monkeypatch, _TEMPLATE_CHANGED)
    f = _findings()
    asyncio.run(_reconcile_version_with_provenance(f))
    assert f.suspected_version == "v2.5.0"


def test_one_implicated_service_that_did_ship_code_protects_the_version(monkeypatch):
    """Fail-safe direction: a mixed answer keeps the extracted version rather
    than clearing it on the strength of the other service."""
    import app.tools.k8s as k8s

    async def _mixed(service, *_a, **_kw):
        result = _TEMPLATE_UNCHANGED if service == "payment-service" else _TEMPLATE_CHANGED
        return dict(result, service=service)

    monkeypatch.setattr(k8s, "get_change_provenance", _mixed)
    f = _findings(services=["payment-service", "checkout-service"])
    asyncio.run(_reconcile_version_with_provenance(f))
    assert f.suspected_version == "v2.5.0"


def test_a_cluster_that_cannot_answer_leaves_the_version_alone(monkeypatch):
    _patch(monkeypatch, {"found": False, "note": "no ReplicaSets"})
    f = _findings()
    asyncio.run(_reconcile_version_with_provenance(f))
    assert f.suspected_version == "v2.5.0"


def test_a_probe_that_raises_never_sinks_the_extraction(monkeypatch):
    import app.tools.k8s as k8s

    async def _boom(*_a, **_kw):
        raise RuntimeError("kube API down")

    monkeypatch.setattr(k8s, "get_change_provenance", _boom)
    f = _findings()
    asyncio.run(_reconcile_version_with_provenance(f))
    assert f.suspected_version == "v2.5.0"


def test_nothing_to_reconcile_costs_no_cluster_call(monkeypatch):
    import app.tools.k8s as k8s

    async def _never(*_a, **_kw):
        raise AssertionError("provenance must not be probed when no version was named")

    monkeypatch.setattr(k8s, "get_change_provenance", _never)
    asyncio.run(_reconcile_version_with_provenance(_findings(suspected_version=None)))
    asyncio.run(_reconcile_version_with_provenance(_findings(services=[])))


@pytest.mark.asyncio
async def test_extract_findings_reconciles_on_the_way_out(monkeypatch):
    """The reconcile has to sit at the choke point, not at one call site: the
    headless path, its pivots, and the chat path all extract findings."""
    _patch(monkeypatch, _TEMPLATE_UNCHANGED)

    class _LLM:
        async def ainvoke(self, _messages):
            return _findings()

    monkeypatch.setattr(agent_mod, "_findings_llm", _LLM())
    out = await agent_mod.extract_findings([])
    assert out.suspected_version is None
