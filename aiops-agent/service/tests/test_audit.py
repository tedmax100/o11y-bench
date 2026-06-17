"""Unit tests for the append-only audit log (7b-1)."""

import app.audit as audit


def _db(monkeypatch, tmp_path):
    p = tmp_path / "aiops.db"
    monkeypatch.setattr(audit.store.settings, "store_path", str(p))
    return p


def test_record_and_scope(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    audit.record("proposed", "ok", request_id="r1", fp="fpA", actor="system", path=p)
    audit.record("approved", "ok", request_id="r1", fp="fpA", actor="alice", path=p)
    audit.record("proposed", "ok", request_id="r2", fp="fpB", path=p)

    all_rows = audit.history(path=p)
    assert len(all_rows) == 3
    # chronological (insert order)
    assert [r["phase"] for r in all_rows] == ["proposed", "approved", "proposed"]

    by_req = audit.history(request_id="r1", path=p)
    assert len(by_req) == 2 and {r["actor"] for r in by_req} == {"system", "alice"}

    by_fp = audit.history(fp="fpB", path=p)
    assert len(by_fp) == 1 and by_fp[0]["request_id"] == "r2"


def test_detail_roundtrips_as_dict(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    audit.record("execute", "refuse", request_id="r1",
                 detail={"reason": "kill switch off", "action": "k8s.rollout_undo"}, path=p)
    row = audit.history(request_id="r1", path=p)[0]
    assert row["detail"]["reason"] == "kill switch off"
    assert row["verdict"] == "refuse"
