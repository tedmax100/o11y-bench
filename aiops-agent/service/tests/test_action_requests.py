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


# ---- the proposal carries its own size -------------------------------------


def test_create_from_decision_stores_the_footprint(tmp_path, monkeypatch):
    """A suggestion whose size is only known after you approve it is a surprise,
    not a suggestion — so the dry-run result is stored with the proposal."""
    p = _db(monkeypatch, tmp_path)
    footprint = {
        "action": "k8s.rollout_undo",
        "target": "demo/payment-service",
        "namespace": "demo",
        "affected_pods": 2,
        "policy_ok": True,
        "policy_reason": "within policy (affected 2 pod(s), ns demo)",
    }
    req = create_from_decision(
        "fp1",
        _decision(Autonomy.PROPOSE),
        args={"namespace": "demo", "deployment": "payment-service"},
        blast_radius=footprint,
        path=p,
    )
    assert req is not None
    stored = get(req.request_id, path=p)
    assert stored.blast_radius["affected_pods"] == 2
    assert stored.blast_radius["policy_ok"] is True


def test_create_from_decision_without_a_footprint_still_proposes(tmp_path, monkeypatch):
    """Best-effort: a footprint we could not compute must not cost the proposal."""
    p = _db(monkeypatch, tmp_path)
    req = create_from_decision("fp2", _decision(Autonomy.PROPOSE), args={}, path=p)
    assert req is not None
    assert get(req.request_id, path=p).blast_radius is None


def test_a_drill_and_a_real_incident_do_not_share_an_idempotency_key(tmp_path, monkeypatch):
    """The rehearsal must not spend the real incident's one allowed action.

    Day41: a drill flipped the flag, and the real execution ten minutes later
    aborted as a duplicate of it — which also cost the only chance the case had
    to record a resolution, since drills deliberately write none."""
    p = _db(monkeypatch, tmp_path)
    args = {"namespace": "demo", "configmap": "user-flags", "flag": "user_session_cache_disabled"}
    drill = create_from_decision("fp1", _decision(), args=args, params={"drill": "True"}, path=p)
    real = create_from_decision("fp1", _decision(), args=args, params={"drill": "False"}, path=p)
    assert drill.idem_key != real.idem_key
    assert drill.idem_key == real.idem_key + "|drill"


def test_production_idempotency_keys_keep_their_historical_shape(tmp_path, monkeypatch):
    """Real requests must hash exactly as they did before the drill suffix
    existed, or every key already in the ledger stops matching itself."""
    p = _db(monkeypatch, tmp_path)
    for params in ({}, None, {"drill": "no"}):
        req = create_from_decision(
            "fp1", _decision(), args={"deployment": "payment-service"}, params=params, path=p
        )
        assert req.idem_key == "k8s.rollout_undo|demo/payment-service|fp1"


def test_two_drills_on_one_incident_still_deduplicate(tmp_path, monkeypatch):
    """Separating drills from production is not the same as exempting them: an
    alert storm during a rehearsal is still a storm."""
    p = _db(monkeypatch, tmp_path)
    args = {"deployment": "payment-service"}
    a = create_from_decision("fp1", _decision(), args=args, params={"drill": "1"}, path=p)
    b = create_from_decision("fp1", _decision(), args=args, params={"drill": "yes"}, path=p)
    assert a.idem_key == b.idem_key


def test_is_drill_reads_the_alert_label_the_way_the_ledger_does(tmp_path, monkeypatch):
    assert arq.is_drill({"drill": "True"}) is True
    assert arq.is_drill({"drill": "1"}) is True
    assert arq.is_drill({"drill": "yes"}) is True
    assert arq.is_drill({"drill": "false"}) is False
    assert arq.is_drill({}) is False
    assert arq.is_drill(None) is False
