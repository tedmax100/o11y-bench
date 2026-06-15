"""Unit tests for the headless-investigation store."""

from types import SimpleNamespace as NS

import app.investigations as inv
from app.governance import Autonomy, Decision


def _findings():
    return NS(summary="v2.5.0 validator declines odd cents", hypothesis="bad deploy",
              confidence=0.95, suspected_version="v2.5.0", services=["payment-service"])


def _decision():
    return Decision(action="k8s.rollout_undo", autonomy=Autonomy.PROPOSE, requires_human=True,
                    confidence=0.95, reason="approval-gated", calibration_note="ok",
                    reversible=True, requires_approval=True)


def _alert():
    return {"labels": {"alertname": "payment-decline-rate-high",
                       "service_name": "payment-service", "git_version": "v2.5.0"}}


def _result():
    return {"findings": _findings(), "decisions": [_decision()], "answer": "long answer " * 500}


def test_record_and_list(tmp_path, monkeypatch):
    p = tmp_path / "inv.jsonl"
    monkeypatch.setattr(inv.settings, "investigations_log_path", str(p))
    monkeypatch.setattr(inv.settings, "investigations_enabled", True)

    inv.record_investigation("fp-1", _alert(), _result())
    rows = inv.list_investigations(path=p)
    assert len(rows) == 1
    r = rows[0]
    assert r["fp"] == "fp-1"
    assert r["service"] == "payment-service"
    assert r["confidence"] == 0.95
    assert r["decisions"][0]["action"] == "k8s.rollout_undo"
    assert r["decisions"][0]["autonomy"] == "propose"
    assert len(r["answer"]) <= 2000  # truncated
    assert r["correct"] is None      # not labeled yet


def test_latest_per_fingerprint(tmp_path, monkeypatch):
    p = tmp_path / "inv.jsonl"
    monkeypatch.setattr(inv.settings, "investigations_log_path", str(p))
    monkeypatch.setattr(inv.settings, "investigations_enabled", True)

    inv.record_investigation("fp-1", _alert(), _result())
    r2 = _result(); r2["findings"].confidence = 0.30
    inv.record_investigation("fp-1", _alert(), r2)  # same fp, newer
    rows = inv.list_investigations(path=p)
    assert len(rows) == 1 and rows[0]["confidence"] == 0.30  # latest wins


def test_disabled_is_noop(tmp_path, monkeypatch):
    p = tmp_path / "inv.jsonl"
    monkeypatch.setattr(inv.settings, "investigations_log_path", str(p))
    monkeypatch.setattr(inv.settings, "investigations_enabled", False)
    inv.record_investigation("fp-x", _alert(), _result())
    assert not p.exists()


def test_correctness_merged_from_calibration(tmp_path, monkeypatch):
    invp = tmp_path / "inv.jsonl"
    calp = tmp_path / "cal.jsonl"
    monkeypatch.setattr(inv.settings, "investigations_log_path", str(invp))
    monkeypatch.setattr(inv.settings, "investigations_enabled", True)
    # calibration store keyed by run_id == fp
    import app.calibration as cal
    monkeypatch.setattr(cal.settings, "calibration_log_path", str(calp))
    monkeypatch.setattr(cal.settings, "calibration_enabled", True)

    inv.record_investigation("fp-1", _alert(), _result())
    cal.record_run(_findings(), run_id="fp-1")
    cal.label_run("fp-1", correct=True)

    rows = inv.list_investigations(path=invp)
    assert rows[0]["correct"] is True
