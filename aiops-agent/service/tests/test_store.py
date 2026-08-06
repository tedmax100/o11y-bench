"""Unit tests for the persistence layer (7b-0). The point of moving off JSONL is
durability + atomic updates, so that's what's pinned: data survives a fresh
connection (= pod restart), label is a targeted UPDATE on the latest row, and the
one-time legacy migration is idempotent."""

import json

import app.store as store


def _cfg(monkeypatch, tmp_path, name="aiops.db"):
    p = tmp_path / name
    monkeypatch.setattr(store.settings, "store_path", str(p))
    return p


def test_calibration_insert_load_label(tmp_path, monkeypatch):
    p = _cfg(monkeypatch, tmp_path)
    store.cal_insert(
        run_id="fp",
        ts="t1",
        confidence=0.4,
        summary="old",
        hypothesis="h",
        suspected_version=None,
        services=[],
    )
    store.cal_insert(
        run_id="fp",
        ts="t2",
        confidence=0.9,
        summary="new",
        hypothesis="h",
        suspected_version="v2.5.0",
        services=["payment"],
    )

    rows = store.cal_load(p)
    assert len(rows) == 2
    assert rows[0]["correct"] is None and rows[1]["services"] == ["payment"]

    # label hits the most recent row for run_id, atomically
    assert store.cal_label("fp", True, score=0.9, source="grader", path=p) is True
    rows = store.cal_load(p)
    assert rows[1]["correct"] is True and rows[1]["score"] == 0.9
    assert rows[0]["correct"] is None
    assert store.cal_label("nope", True, score=None, source="x", path=p) is False


def test_survives_reconnection(tmp_path, monkeypatch):
    # A new connection == a process restart: the file-backed db must still have it.
    p = _cfg(monkeypatch, tmp_path)
    store.inv_insert("fp-1", "t", json.dumps({"fp": "fp-1", "v": 1}), p)
    # nothing held open; load opens a brand new connection
    payloads = store.inv_load(p)
    assert len(payloads) == 1 and json.loads(payloads[0])["fp"] == "fp-1"


def test_load_on_fresh_path_is_empty(tmp_path, monkeypatch):
    p = _cfg(monkeypatch, tmp_path, "fresh.db")
    assert store.cal_load(p) == []
    assert store.inv_load(p) == []


def test_legacy_migration_idempotent(tmp_path, monkeypatch):
    # Seed legacy JSONL files, then migrate twice — second run is a no-op because
    # the tables are no longer empty.
    p = _cfg(monkeypatch, tmp_path)
    calj = tmp_path / "calibration.jsonl"
    invj = tmp_path / "investigations.jsonl"
    calj.write_text(
        json.dumps(
            {"run_id": "fp", "ts": "t", "confidence": 0.7, "correct": True, "services": ["payment"]}
        )
        + "\n"
    )
    invj.write_text(json.dumps({"fp": "fp", "ts": "t", "summary": "s"}) + "\n")
    monkeypatch.setattr(store.settings, "calibration_log_path", str(calj))
    monkeypatch.setattr(store.settings, "investigations_log_path", str(invj))

    first = store.migrate_legacy_jsonl(p)
    assert first == {"calibration": 1, "investigations": 1}
    second = store.migrate_legacy_jsonl(p)
    assert second == {"calibration": 0, "investigations": 0}  # tables non-empty → skip

    rows = store.cal_load(p)
    assert len(rows) == 1 and rows[0]["correct"] is True and rows[0]["services"] == ["payment"]
    assert len(store.inv_load(p)) == 1


# ---- past incidents for a chat question ------------------------------------


def _seed_investigation(path, fp, service, alertname, summary, correct=True):
    import json

    store.init(path)
    payload = json.dumps(
        {
            "fp": fp,
            "service": service,
            "alertname": alertname,
            "summary": summary,
            "confidence": 0.8,
        }
    )
    store.inv_insert(fp, "2026-08-06T00:00:00Z", payload, path)
    store.cal_insert(
        run_id=fp,
        ts="2026-08-06T00:00:00Z",
        confidence=0.8,
        summary=summary,
        hypothesis="h",
        suspected_version=None,
        services=[service],
        path=path,
    )
    store.cal_label(fp, correct, score=1.0 if correct else 0.0, source="test", path=path)


def test_inv_query_similar_without_an_alertname_matches_any_investigation(tmp_path):
    """A chat question has no alertname; matching on the service alone is still
    the right thing, because that is what a colleague would remember."""
    path = tmp_path / "aiops.db"
    _seed_investigation(path, "fp1", "payment-service", "payment-decline-rate-high", "bad deploy")
    assert store.inv_query_similar("payment-service", path=path)
    assert store.inv_query_similar("payment-service", "payment-decline-rate-high", path=path)
    assert not store.inv_query_similar("payment-service", "some-other-alert", path=path)


def test_inv_query_similar_still_ignores_runs_that_were_wrong(tmp_path):
    path = tmp_path / "aiops.db"
    _seed_investigation(path, "fp2", "order-service", "x", "guessed", correct=False)
    assert not store.inv_query_similar("order-service", path=path)
