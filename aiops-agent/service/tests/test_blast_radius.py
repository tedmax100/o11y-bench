"""Unit tests for the dry-run + blast-radius gate (7b-2). Policy is pure logic so
it's pinned exactly; the dry-runs are tested against a faked read-only k8s client
to verify the revision-pick / delta math without a cluster."""

from types import SimpleNamespace as NS

import app.blast_radius as br
from app.blast_radius import BlastRadius, dry_run_rollout_undo, dry_run_scale, evaluate_policy


def _ok(**over):
    base = dict(
        action="k8s.rollout_undo",
        target="demo/payment-service",
        namespace="demo",
        target_revision="4",
        affected_pods=3,
        singleton=False,
        available=True,
    )
    base.update(over)
    return BlastRadius(**base)


# ---- policy (pure) ---------------------------------------------------------


def test_policy_within(monkeypatch):
    ok, reason = evaluate_policy(_ok())
    assert ok and "within policy" in reason


def test_policy_unavailable_fails_closed():
    ok, reason = evaluate_policy(
        BlastRadius(
            action="k8s.scale", target="x", namespace="demo", available=False, detail="k8s down"
        )
    )
    assert not ok and "fail-closed" in reason


def test_policy_protected_namespace():
    ok, reason = evaluate_policy(_ok(namespace="kube-system", in_protected_namespace=True))
    assert not ok and "protected" in reason


def test_policy_off_allowlist(monkeypatch):
    monkeypatch.setattr(br.settings, "execution_namespace_allowlist", ["demo"])
    ok, reason = evaluate_policy(_ok(namespace="prod"))
    assert not ok and "allowlist" in reason


def test_policy_singleton_denied(monkeypatch):
    monkeypatch.setattr(br.settings, "deny_singletons", True)
    ok, reason = evaluate_policy(_ok(singleton=True))
    assert not ok and "singleton" in reason


def test_policy_too_many_pods(monkeypatch):
    monkeypatch.setattr(br.settings, "max_blast_pods", 5)
    ok, reason = evaluate_policy(_ok(affected_pods=9))
    assert not ok and "exceeds max" in reason


def test_policy_rollout_undo_needs_previous_revision():
    ok, reason = evaluate_policy(_ok(target_revision=None))
    assert not ok and "previous revision" in reason


# ---- dry-runs (faked read-only client) -------------------------------------


def _fake_apps(deployment="payment-service", replicas=3, current_rev="5", rs_revs=("5", "4", "3")):
    dep = NS(
        spec=NS(replicas=replicas, template=NS(metadata=NS(labels={}))),
        metadata=NS(annotations={"deployment.kubernetes.io/revision": current_rev}),
        status=NS(),
    )

    def _rs(rev):
        return NS(
            metadata=NS(
                annotations={"deployment.kubernetes.io/revision": rev},
                owner_references=[NS(kind="Deployment", name=deployment)],
            )
        )

    rs_list = NS(items=[_rs(r) for r in rs_revs])
    apps = NS(
        read_namespaced_deployment=lambda name, namespace: dep,
        list_namespaced_replica_set=lambda namespace: rs_list,
    )
    return apps


def _wire(monkeypatch, apps):
    from app.tools import k8s

    monkeypatch.setattr(k8s, "_load_client", lambda: (None, apps))


async def test_dry_run_rollout_undo_picks_previous_revision(tmp_path, monkeypatch):
    _wire(monkeypatch, _fake_apps(current_rev="5", rs_revs=("5", "4", "3")))
    out = await dry_run_rollout_undo({"deployment": "payment-service", "namespace": "demo"})
    assert out.available
    assert out.current_revision == "5" and out.target_revision == "4"
    assert out.affected_pods == 3 and out.singleton is False
    assert out.target == "demo/payment-service"


async def test_dry_run_rollout_undo_no_previous_revision(monkeypatch):
    _wire(monkeypatch, _fake_apps(current_rev="1", rs_revs=("1",)))
    out = await dry_run_rollout_undo({"deployment": "payment-service", "namespace": "demo"})
    assert out.target_revision is None and "no previous revision" in " ".join(out.notes)


async def test_dry_run_scale_delta(monkeypatch):
    _wire(monkeypatch, _fake_apps(replicas=2))
    out = await dry_run_scale({"deployment": "payment-service", "namespace": "demo", "replicas": 5})
    assert out.current_replicas == 2 and out.target_replicas == 5 and out.affected_pods == 3


async def test_dry_run_scale_to_zero_noted(monkeypatch):
    _wire(monkeypatch, _fake_apps(replicas=3))
    out = await dry_run_scale({"deployment": "x", "namespace": "demo", "replicas": 0})
    assert out.target_replicas == 0 and any("zero" in n for n in out.notes)


async def test_dry_run_unavailable_when_k8s_down(monkeypatch):
    from app.tools import k8s

    def _boom():
        raise RuntimeError("kubernetes config not available")

    monkeypatch.setattr(k8s, "_load_client", _boom)
    out = await dry_run_rollout_undo({"deployment": "x", "namespace": "demo"})
    assert out.available is False and "not available" in out.detail
