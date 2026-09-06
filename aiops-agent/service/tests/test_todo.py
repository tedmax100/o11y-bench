"""The one view of what is waiting on a person.

Three of this system's four goals stall on human work — labelling a run, deciding
on a proposal, saying what caused an incident — and each stalled for the same
reason: no entry point. What is pinned here is that the view counts the whole
backlog (not just the page it returns), that an unanswered proposal is reported
as unanswered rather than quietly dropped, and that a broken gate reading cannot
take the queues down with it."""

import app.action_requests as arq
import app.governance as governance
import app.store as store
from app.action_requests import create_from_decision
from app.governance import Autonomy, Decision


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    p = tmp_path / "aiops.db"
    for mod in (store, arq, governance):
        monkeypatch.setattr(mod.settings, "store_path", str(p))
    monkeypatch.setattr(arq.settings, "action_requests_enabled", True)
    return TestClient(app), p


def _decision(action="k8s.rollout_undo"):
    return Decision(
        action=action,
        autonomy=Autonomy.PROPOSE,
        requires_human=True,
        confidence=0.9,
        reason="r",
        calibration_note="c",
        reversible=True,
        requires_approval=True,
    )


def test_counts_the_backlog_not_the_page(monkeypatch, tmp_path):
    c, _ = _client(monkeypatch, tmp_path)
    for i in range(3):
        store.case_upsert(
            key=f"k{i}", ts=f"2026-08-2{i}T00:00:00Z", alertname="a", service="payment-service"
        )
    body = c.get("/todo", params={"limit": 1}).json()["cases_to_label"]
    assert body["count"] == 3 and len(body["items"]) == 1


def test_unanswered_proposals_are_reported_not_dropped(monkeypatch, tmp_path):
    """An expired proposal is a fact about the process — somebody was asked and
    never answered — so it is counted separately rather than disappearing."""
    c, p = _client(monkeypatch, tmp_path)
    live = create_from_decision("fp1", _decision(), args={}, path=p)
    stale = create_from_decision("fp2", _decision(), args={}, path=p)
    store.ar_transition(stale.request_id, "proposed", "expired", path=p)
    body = c.get("/todo").json()["requests_to_decide"]
    assert [r["request_id"] for r in body["items"]] == [live.request_id]
    assert body["count"] == 1 and body["expired_unattended"] == 1


def test_labeled_cases_leave_the_queue(monkeypatch, tmp_path):
    c, _ = _client(monkeypatch, tmp_path)
    store.case_upsert(key="k1", ts="2026-08-20T00:00:00Z", alertname="a", service="svc")
    assert c.get("/todo").json()["cases_to_label"]["count"] == 1
    store.case_confirm("k1", root_cause="rc", source="human", run_id="r", ts="2026-08-20T01:00:00Z")
    assert c.get("/todo").json()["cases_to_label"]["count"] == 0


def test_a_broken_gate_reading_does_not_take_the_queues_down(monkeypatch, tmp_path):
    c, _ = _client(monkeypatch, tmp_path)
    store.case_upsert(key="k1", ts="2026-08-20T00:00:00Z", alertname="a", service="svc")

    def boom(path=None):
        raise RuntimeError("no cluster")

    monkeypatch.setattr(governance, "autonomy_status", boom)
    body = c.get("/todo").json()
    assert body["cases_to_label"]["count"] == 1
    assert "no cluster" in body["autonomy"]["error"]


def test_autonomy_reports_the_distance_left(monkeypatch, tmp_path):
    """The point of the section: not "denied", but how far from granted."""
    c, _ = _client(monkeypatch, tmp_path)
    auto = c.get("/todo").json()["autonomy"]
    assert auto["granted"] is False
    assert {g["gate"] for g in auto["gates"]} == {
        "calibration",
        "data_quality",
        "actuation",
        "fixture_record",
    }
    assert auto["blockers"], "an empty store cannot have earned autonomy"
    cal = auto["calibration"]
    assert cal["labeled"] == 0
    assert cal["labeled_required"] == governance.settings.governance_min_labeled_runs
    # Every blocker carries the sentence that says why, so the UI never has to
    # invent one.
    assert all(g["note"] for g in auto["blockers"])


def test_an_executed_action_nobody_graded_is_on_the_list(monkeypatch, tmp_path):
    """The AE-SLO divided by the graded rows, so an execution nobody judged was
    indistinguishable from one that never happened — including here, the one
    place that is supposed to show what a person still owes."""
    c, p = _client(monkeypatch, tmp_path)
    ran = create_from_decision("fp1", _decision(), args={}, path=p)
    store.ar_transition(ran.request_id, "proposed", "succeeded", path=p)
    judged = create_from_decision("fp2", _decision(), args={}, path=p)
    store.ar_transition(judged.request_id, "proposed", "succeeded", path=p)
    store.action_outcome_put(request_id=judged.request_id, resolved=True, actor="oncall", path=p)

    body = c.get("/todo").json()["actions_to_grade"]
    assert body["count"] == 1
    assert [r["request_id"] for r in body["items"]] == [ran.request_id]


def test_a_proposal_nobody_ran_is_not_something_to_grade(monkeypatch, tmp_path):
    """It belongs in `requests_to_decide`. Grading it would put a verdict in the
    ledger for something that never touched the cluster."""
    c, p = _client(monkeypatch, tmp_path)
    create_from_decision("fp1", _decision(), args={}, path=p)
    body = c.get("/todo").json()
    assert body["actions_to_grade"]["count"] == 0
    assert body["requests_to_decide"]["count"] == 1
