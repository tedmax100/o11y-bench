"""Unit tests for the action-request lifecycle (7b-1). Pins the state machine:
which governance decisions create a request, the initial status, and the atomic
transitions (approve/reject/expire) that make double-approval impossible."""

import app.action_requests as arq
from app.action_requests import Status, approve, create_from_decision, get, reject
from app.governance import Autonomy, Decision


def _db(monkeypatch, tmp_path):
    p = tmp_path / "aiops.db"
    monkeypatch.setattr(arq.settings, "store_path", str(p))
    monkeypatch.setattr(arq.settings, "action_requests_enabled", True)
    return p


def _decision(
    autonomy=Autonomy.PROPOSE, action="k8s.rollout_undo", reversible=True, requires_approval=True
):
    return Decision(
        action=action,
        autonomy=autonomy,
        requires_human=(autonomy is not Autonomy.AUTO),
        confidence=0.9,
        reason="r",
        calibration_note="c",
        reversible=reversible,
        requires_approval=requires_approval,
    )


def test_propose_creates_proposed_request(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    req = create_from_decision(
        "fp1",
        _decision(Autonomy.PROPOSE),
        args={"deployment": "payment-service"},
        rollback={"action": "k8s.rollout_undo"},
        path=p,
    )
    assert req is not None
    assert req.status == Status.PROPOSED.value
    assert req.actor is None
    assert req.args["deployment"] == "payment-service"
    assert req.rollback == {"action": "k8s.rollout_undo"}


def test_escalate_creates_nothing(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    assert create_from_decision("fp1", _decision(Autonomy.ESCALATE), path=p) is None


def test_auto_is_approved_only_when_kill_switch_on(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(arq.settings, "actions_enabled", False)
    r1 = create_from_decision("fp1", _decision(Autonomy.AUTO), path=p)
    assert r1.status == Status.PROPOSED.value  # AUTO-but-disabled degrades to propose

    monkeypatch.setattr(arq.settings, "actions_enabled", True)
    r2 = create_from_decision("fp2", _decision(Autonomy.AUTO), path=p)
    assert r2.status == Status.APPROVED.value and r2.actor == "system"


def test_approve_then_double_approve_is_rejected(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    req = create_from_decision("fp1", _decision(Autonomy.PROPOSE), path=p)
    approved = approve(req.request_id, actor="alice", path=p)
    assert approved.status == Status.APPROVED.value and approved.actor == "alice"
    # second approval finds it no longer proposed → atomic CAS matches 0 rows
    assert approve(req.request_id, actor="bob", path=p) is None
    assert get(req.request_id, p).actor == "alice"  # first writer wins


def test_reject(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    req = create_from_decision("fp1", _decision(Autonomy.PROPOSE), path=p)
    rejected = reject(req.request_id, actor="alice", path=p)
    assert rejected.status == Status.REJECTED.value
    # can't approve a rejected request
    assert approve(req.request_id, actor="bob", path=p) is None


def test_approve_after_ttl_expires(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(arq.settings, "approval_ttl_seconds", -10)  # already past
    req = create_from_decision("fp1", _decision(Autonomy.PROPOSE), path=p)
    assert approve(req.request_id, actor="alice", path=p) is None
    assert get(req.request_id, p).status == Status.EXPIRED.value


def test_approve_missing_request(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    assert approve("does-not-exist", actor="alice", path=p) is None
