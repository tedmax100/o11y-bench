"""Unit tests for the executor.
7b-1: kill switch → REFUSED.
7b-2: pre-execution read-only gates → ABORTED when preconditions flip or blast radius over policy.
7b-3: idempotency + circuit breaker → ABORTED.
7b-4: real impl wired; verify (step 5) + auto-rollback (step 6) tested here."""

import app.action_requests as arq
import app.breaker as bk
import app.execution as ex
import app.store as store
from app.action_requests import Status, approve, create_from_decision, get
from app.blast_radius import BlastRadius
from app.execution import run
from app.governance import Autonomy, Decision


def _db(monkeypatch, tmp_path):
    p = tmp_path / "aiops.db"
    monkeypatch.setattr(arq.settings, "store_path", str(p))
    monkeypatch.setattr(arq.settings, "action_requests_enabled", True)
    return p


def _decision():
    return Decision(
        action="k8s.rollout_undo",
        autonomy=Autonomy.PROPOSE,
        requires_human=True,
        confidence=0.9,
        reason="r",
        calibration_note="c",
        reversible=True,
        requires_approval=True,
    )


def _wire_ok_dry_run(monkeypatch, **over):
    """Make the blast-radius gate pass with an in-policy footprint, so a run can
    reach the (kill-switched) execute step."""
    base = dict(
        action="k8s.rollout_undo",
        target="demo/x",
        namespace="demo",
        current_revision="5",
        target_revision="4",
        current_replicas=3,
        target_replicas=3,
        affected_pods=3,
        singleton=False,
        available=True,
    )
    base.update(over)

    async def _dr(args):
        return BlastRadius(**base)

    from app.actions import registry

    monkeypatch.setattr(registry.get("k8s.rollout_undo"), "dry_run", _dr)


async def test_approved_execution_is_refused_by_kill_switch(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(arq.settings, "actions_enabled", False)  # the gate
    _wire_ok_dry_run(monkeypatch)
    req = create_from_decision("fp1", _decision(), args={"deployment": "x"}, path=p)
    approve(req.request_id, actor="alice", path=p)

    res = await run(req.request_id, path=p)
    assert res["status"] == Status.REFUSED.value
    assert "disabled" in res["outcome"]
    final = get(req.request_id, p)
    assert final.status == Status.REFUSED.value
    assert final.blast_radius and final.blast_radius["target_revision"] == "4"  # dry-run stored


async def test_blast_radius_over_policy_aborts(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(arq.settings, "max_blast_pods", 5)
    _wire_ok_dry_run(monkeypatch, affected_pods=50)  # way over the ceiling
    req = create_from_decision("fp1", _decision(), args={"deployment": "x"}, path=p)
    approve(req.request_id, actor="alice", path=p)

    res = await run(req.request_id, path=p)
    assert res["status"] == Status.ABORTED.value
    assert get(req.request_id, p).status == Status.ABORTED.value


async def test_dry_run_unavailable_fails_closed(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    _wire_ok_dry_run(monkeypatch, available=False, detail="k8s down")
    req = create_from_decision("fp1", _decision(), args={"deployment": "x"}, path=p)
    approve(req.request_id, actor="alice", path=p)

    res = await run(req.request_id, path=p)
    assert res["status"] == Status.ABORTED.value  # can't verify footprint → refuse


async def test_precondition_flip_aborts(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    _wire_ok_dry_run(monkeypatch)
    # A runbook with one diagnostic; revalidation finds it now FAILS.
    from app.runbook import DiagnosticResult, Runbook, Step

    rb = Runbook(id="rb1", diagnostics=[Step(desc="version still bad", action="query_prometheus")])
    monkeypatch.setattr(ex, "load_runbooks", lambda: [rb])
    monkeypatch.setattr(ex, "_read_only_tools", lambda: {})

    async def _diag(rb_, params, tool_map):
        return [
            DiagnosticResult(desc="version still bad", action="query_prometheus", status="fail")
        ]

    monkeypatch.setattr(ex, "run_diagnostics", _diag)

    req = create_from_decision(
        "fp1", _decision(), args={"deployment": "x"}, runbook_id="rb1", params={}, path=p
    )
    approve(req.request_id, actor="alice", path=p)
    res = await run(req.request_id, path=p)
    assert res["status"] == Status.ABORTED.value
    assert "precondition" in res["outcome"]


async def test_precondition_pass_then_refused(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(arq.settings, "actions_enabled", False)
    _wire_ok_dry_run(monkeypatch)
    from app.runbook import DiagnosticResult, Runbook, Step

    rb = Runbook(id="rb1", diagnostics=[Step(desc="check", action="query_prometheus")])
    monkeypatch.setattr(ex, "load_runbooks", lambda: [rb])
    monkeypatch.setattr(ex, "_read_only_tools", lambda: {})

    async def _diag(rb_, params, tool_map):
        return [DiagnosticResult(desc="check", action="query_prometheus", status="pass")]

    monkeypatch.setattr(ex, "run_diagnostics", _diag)

    req = create_from_decision("fp1", _decision(), runbook_id="rb1", params={}, path=p)
    approve(req.request_id, actor="alice", path=p)
    res = await run(req.request_id, path=p)
    assert res["status"] == Status.REFUSED.value  # gates passed; kill switch stops it


async def test_idempotent_duplicate_aborts(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    _wire_ok_dry_run(monkeypatch)
    # req1 reaches a ran/running state for this (action, target, fp)
    req1 = create_from_decision("fp1", _decision(), args={"deployment": "x"}, path=p)
    approve(req1.request_id, actor="alice", path=p)
    store.ar_transition(req1.request_id, "approved", "executing", path=p)
    # req2 is the same incident + same target → same idem_key
    req2 = create_from_decision("fp1", _decision(), args={"deployment": "x"}, path=p)
    assert req2.idem_key == req1.idem_key
    approve(req2.request_id, actor="bob", path=p)

    res = await run(req2.request_id, path=p)
    assert res["status"] == Status.ABORTED.value
    assert "idempotent" in res["outcome"]


async def test_open_breaker_aborts(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    _wire_ok_dry_run(monkeypatch)
    monkeypatch.setattr(bk.settings, "breaker_fail_threshold", 2)
    monkeypatch.setattr(bk.settings, "breaker_max_actions_per_window", 100)
    # trip the breaker for demo/x (the target of args={"deployment":"x"})
    bk.record_outcome("k8s.rollout_undo", "demo/x", success=False, path=p)
    bk.record_outcome("k8s.rollout_undo", "demo/x", success=False, path=p)

    req = create_from_decision("fp1", _decision(), args={"deployment": "x"}, path=p)
    approve(req.request_id, actor="alice", path=p)
    res = await run(req.request_id, path=p)
    assert res["status"] == Status.ABORTED.value
    assert "circuit breaker" in res["outcome"]


async def test_unapproved_request_is_not_executed(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    req = create_from_decision("fp1", _decision(), path=p)  # still PROPOSED
    res = await run(req.request_id, path=p)
    assert res["status"] == Status.PROPOSED.value
    assert "not in approved state" in res["outcome"]
    assert get(req.request_id, p).status == Status.PROPOSED.value  # untouched


async def test_double_execute_claims_once(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(arq.settings, "actions_enabled", False)
    _wire_ok_dry_run(monkeypatch)
    req = create_from_decision("fp1", _decision(), path=p)
    approve(req.request_id, actor="alice", path=p)

    first = await run(req.request_id, path=p)
    assert first["status"] == Status.REFUSED.value
    second = await run(req.request_id, path=p)  # already terminal → can't re-claim
    assert second["status"] == Status.REFUSED.value
    assert "not in approved state" in second["outcome"]


async def test_missing_request(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    assert (await run("nope", path=p))["status"] == "not_found"


# ---------------------------------------------------------------------------
# 7b-4: verify (step 5) + auto-rollback (step 6)
# ---------------------------------------------------------------------------


def _wire_impl(monkeypatch, *, impl_ok: bool, rollback_ok: bool = True):
    """Monkeypatch actions_enabled + impl + verify settle window to 0."""
    from app.actions import registry

    async def _impl(args):
        if not impl_ok:
            raise RuntimeError("fake impl error")
        return {
            "action": "rollout_undo",
            "deployment": args.get("deployment"),
            "rolled_back_to_revision": 4,
        }

    async def _rb_impl(args):
        if not rollback_ok:
            raise RuntimeError("fake rollback error")
        return {"action": "rollout_undo", "rolled_back_to_revision": 5}

    monkeypatch.setattr(registry.get("k8s.rollout_undo"), "impl", _impl)
    monkeypatch.setattr(
        registry.get("k8s.rollout_undo"),
        "dry_run",
        (lambda: None).__class__(  # reuse _wire_ok_dry_run helper below
            *[], **{}
        ),
    )
    # patch the rollback action impl (same action for rollout_undo)
    # rollback_rollout_undo === impl_rollout_undo; give it a separate fake
    monkeypatch.setattr(
        __import__("app.tools.k8s_write", fromlist=["rollback_rollout_undo"]),
        "rollback_rollout_undo",
        _rb_impl,
    )
    # Re-wire the registry's rollback callable
    import app.tools.k8s_write as kw

    kw.rollback_rollout_undo = _rb_impl


def _setup_verify_test(monkeypatch, tmp_path, *, verify_pass: bool, rollback_ok: bool = True):
    """Common setup for verify tests: enabled kill switch, faked impl, faked verify tool."""
    import app.execution as ex_mod
    from app.actions import registry

    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(arq.settings, "actions_enabled", True)
    monkeypatch.setattr(arq.settings, "verify_delay_seconds", 0)
    _wire_ok_dry_run(monkeypatch)

    # fake impl (always succeeds at the execute step)
    async def _impl(args):
        return {"action": "rollout_undo", "rolled_back_to_revision": 4}

    monkeypatch.setattr(registry.get("k8s.rollout_undo"), "impl", _impl)

    # fake verify tool
    verify_out = (
        {"resultType": "vector", "result": [{"metric": {}, "value": 0.001}]}
        if verify_pass
        else {"resultType": "vector", "result": [{"metric": {}, "value": 0.5}]}
    )

    class _FakeTool:
        name = "query_prometheus"

        async def ainvoke(self, args):
            return verify_out

    monkeypatch.setattr(ex_mod, "_read_only_tools", lambda: {"query_prometheus": _FakeTool()})

    # fake rollback impl
    async def _rb(args):
        if not rollback_ok:
            raise RuntimeError("rollback error")
        return {"rolled_back": True}

    # patch the registry lookup for rollback (same action name k8s.rollout_undo)
    # _auto_rollback calls spec.impl on the rollback contract action; patch the impl
    # on the registry spec to return the rollback fake on second call won't work cleanly
    # so we patch _auto_rollback directly for rollback_ok=False case
    if not rollback_ok:

        async def _fail_rb(req, path):
            import app.audit as audit

            audit.record(
                "rollback",
                "fail",
                request_id=req.request_id,
                fp=req.fp,
                detail={"error": "fake rollback error"},
                path=path,
            )
            return False

        monkeypatch.setattr(ex_mod, "_auto_rollback", _fail_rb)

    # runbook with verify spec
    from app.runbook import Runbook, Step

    rb = Runbook(
        id="rb1",
        remediation=[
            Step(
                desc="rollback",
                action="k8s.rollout_undo",
                rollback={
                    "action": "k8s.rollout_undo",
                    "args": {"deployment": "x", "namespace": "demo"},
                },
                verify={
                    "action": "query_prometheus",
                    "args": {"expr": "sum(rate(foo[2m]))", "queryType": "instant"},
                    "check": {"max_value": 0.01},
                },
            )
        ],
    )
    monkeypatch.setattr(ex_mod, "load_runbooks", lambda: [rb])

    return p


async def test_verify_pass_reaches_succeeded(tmp_path, monkeypatch):
    """Execute succeeds + verify passes → SUCCEEDED."""
    p = _setup_verify_test(monkeypatch, tmp_path, verify_pass=True)
    req = create_from_decision(
        "fp1", _decision(), runbook_id="rb1", args={"deployment": "x", "namespace": "demo"}, path=p
    )
    approve(req.request_id, actor="alice", path=p)
    res = await run(req.request_id, path=p)
    assert res["status"] == Status.SUCCEEDED.value
    assert get(req.request_id, p).status == Status.SUCCEEDED.value


async def test_verify_fail_triggers_rollback(tmp_path, monkeypatch):
    """Verify fails → auto-rollback succeeds → ROLLED_BACK."""
    p = _setup_verify_test(monkeypatch, tmp_path, verify_pass=False, rollback_ok=True)
    # wire rollback via the registry impl (same action → same impl = our fake)
    from app.actions import registry

    async def _rb(args):
        return {"rolled_back": True}

    monkeypatch.setattr(registry.get("k8s.rollout_undo"), "impl", _rb)

    req = create_from_decision(
        "fp1",
        _decision(),
        runbook_id="rb1",
        args={"deployment": "x", "namespace": "demo"},
        rollback={"action": "k8s.rollout_undo", "args": {"deployment": "x", "namespace": "demo"}},
        path=p,
    )
    approve(req.request_id, actor="alice", path=p)
    res = await run(req.request_id, path=p)
    assert res["status"] == Status.ROLLED_BACK.value
    assert get(req.request_id, p).status == Status.ROLLED_BACK.value


async def test_verify_fail_rollback_fail(tmp_path, monkeypatch):
    """Verify fails + rollback also fails → ROLLBACK_FAILED."""
    p = _setup_verify_test(monkeypatch, tmp_path, verify_pass=False, rollback_ok=False)
    req = create_from_decision(
        "fp1",
        _decision(),
        runbook_id="rb1",
        args={"deployment": "x", "namespace": "demo"},
        rollback={"action": "k8s.rollout_undo", "args": {"deployment": "x", "namespace": "demo"}},
        path=p,
    )
    approve(req.request_id, actor="alice", path=p)
    res = await run(req.request_id, path=p)
    assert res["status"] == Status.ROLLBACK_FAILED.value
    assert get(req.request_id, p).status == Status.ROLLBACK_FAILED.value
