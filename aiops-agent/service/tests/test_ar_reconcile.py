"""Unit tests for background reconciliation of the action-request state machine.

The rule under test is narrow and deliberate: reconciliation may make the record
honest, and may not do anything else. Every test here is really asking "did it
stay inside that line" — especially the one that asserts no rollback is
attempted for an abandoned execution.
"""

import sqlite3

import app.action_requests as arq
from app.action_requests import Status, get, reconcile, reject
from app.audit import history
from app.governance import Autonomy, Decision
from app.store import ar_transition


def _db(monkeypatch, tmp_path):
    p = tmp_path / "aiops.db"
    monkeypatch.setattr(arq.settings, "store_path", str(p))
    monkeypatch.setattr(arq.settings, "action_requests_enabled", True)
    return p


def _decision(autonomy=Autonomy.PROPOSE):
    return Decision(
        action="k8s.rollout_undo",
        autonomy=autonomy,
        requires_human=True,
        confidence=0.9,
        reason="r",
        calibration_note="c",
        reversible=True,
        requires_approval=True,
    )


def _propose(p, fp="fp1"):
    return arq.create_from_decision(fp, _decision(), args={"deployment": "d"}, path=p)


def _backdate(p, request_id, *, created="2020-01-01T00:00:00Z", expires="2020-01-01T00:15:00Z"):
    """Push a row into the past. Times are stored as strings, so writing older
    ones produces exactly the state the passage of time would have produced —
    no clock mocking, and the reconciler is exercised on real rows."""
    conn = sqlite3.connect(str(p))
    try:
        conn.execute(
            "UPDATE action_requests SET created_ts=?, expires_ts=? WHERE request_id=?",
            (created, expires, request_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---- proposed → expired ----------------------------------------------------


def test_stale_proposal_expires_without_anyone_knocking(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    req = _propose(p)
    _backdate(p, req.request_id)

    out = reconcile(path=p)

    assert out["expired"] == [req.request_id]
    assert get(req.request_id, p).status == Status.EXPIRED.value


def test_fresh_proposal_is_left_alone(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    req = _propose(p)

    out = reconcile(path=p)

    assert out["expired"] == []
    assert get(req.request_id, p).status == Status.PROPOSED.value


def test_expiry_is_attributed_to_the_reconciler(tmp_path, monkeypatch):
    """A status that changed with nobody watching still needs an actor, or the
    reconciler becomes the second invisible hand in a system built to have none."""
    p = _db(monkeypatch, tmp_path)
    req = _propose(p)
    _backdate(p, req.request_id)

    reconcile(path=p)

    events = [e for e in history(request_id=req.request_id, path=p) if e["phase"] == "expired"]
    assert events and events[0]["actor"] == "reconciler"


# ---- executing → failed (and nothing more) ---------------------------------


def test_abandoned_execution_is_written_off(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    req = _propose(p)
    ar_transition(req.request_id, Status.PROPOSED.value, Status.APPROVED.value, path=p)
    ar_transition(req.request_id, Status.APPROVED.value, Status.EXECUTING.value, path=p)
    _backdate(p, req.request_id)

    out = reconcile(path=p)

    assert out["abandoned"] == [req.request_id]
    row = get(req.request_id, p)
    assert row.status == Status.FAILED.value
    # The outcome must say the write is of unknown status, not imply it failed.
    assert "unknown" in row.outcome


def test_abandoned_execution_does_not_roll_back(tmp_path, monkeypatch):
    """The whole safety argument: we do not know whether the change landed, so
    guessing can turn 'maybe nothing happened' into 'definitely something did'."""
    p = _db(monkeypatch, tmp_path)
    req = _propose(p)
    ar_transition(req.request_id, Status.PROPOSED.value, Status.APPROVED.value, path=p)
    ar_transition(req.request_id, Status.APPROVED.value, Status.EXECUTING.value, path=p)
    _backdate(p, req.request_id)

    reconcile(path=p)

    events = history(request_id=req.request_id, path=p)
    assert not [e for e in events if e["phase"] == "rollback"]
    abandoned = [e for e in events if e["phase"] == "abandoned"]
    assert abandoned and abandoned[0]["detail"]["rollback_attempted"] is False


def test_recent_execution_is_not_declared_dead(tmp_path, monkeypatch):
    """A live execute→settle→verify run must survive a reconciliation pass."""
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(arq.settings, "executing_timeout_seconds", 600)
    req = _propose(p)
    ar_transition(req.request_id, Status.PROPOSED.value, Status.APPROVED.value, path=p)
    ar_transition(req.request_id, Status.APPROVED.value, Status.EXECUTING.value, path=p)

    out = reconcile(path=p)

    assert out["abandoned"] == []
    assert get(req.request_id, p).status == Status.EXECUTING.value


def test_terminal_rows_are_untouched(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    req = _propose(p)
    ar_transition(req.request_id, Status.PROPOSED.value, Status.REJECTED.value, path=p)
    _backdate(p, req.request_id)

    out = reconcile(path=p)

    assert out["expired"] == [] and out["abandoned"] == []
    assert get(req.request_id, p).status == Status.REJECTED.value


# ---- reject now tells the same story as approve -----------------------------


def test_rejecting_a_stale_proposal_expires_it_instead(tmp_path, monkeypatch):
    """Two equally lapsed proposals must not end up with different histories —
    one `expired` with a reason, the other `rejected` with a person's name on it."""
    p = _db(monkeypatch, tmp_path)
    req = _propose(p)
    _backdate(p, req.request_id)

    assert reject(req.request_id, "nathan", p) is None
    assert get(req.request_id, p).status == Status.EXPIRED.value
