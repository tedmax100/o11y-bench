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
    audit.record(
        "execute",
        "refuse",
        request_id="r1",
        detail={"reason": "kill switch off", "action": "k8s.rollout_undo"},
        path=p,
    )
    row = audit.history(request_id="r1", path=p)[0]
    assert row["detail"]["reason"] == "kill switch off"
    assert row["verdict"] == "refuse"


# ---- the link back to the reasoning ----------------------------------------


def test_current_trace_id_is_none_when_nothing_is_recording():
    """Outside `opentelemetry-instrument` there is no trace, and that must be a
    None rather than an exception — every probe script in this series runs that
    way."""
    assert audit.current_trace_id() is None


def test_record_attaches_the_trace_id(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(audit, "current_trace_id", lambda: "f" * 32)
    audit.record("execute", "ok", request_id="r1", fp="fp1", path=p)
    entries = audit.history(request_id="r1", path=p)
    assert entries and entries[0]["detail"]["trace_id"] == "f" * 32


def test_record_without_a_trace_still_records(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(audit, "current_trace_id", lambda: None)
    audit.record("execute", "ok", request_id="r2", fp="fp2", path=p)
    entries = audit.history(request_id="r2", path=p)
    assert entries and "trace_id" not in entries[0]["detail"]
