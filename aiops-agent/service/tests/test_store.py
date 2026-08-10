"""Unit tests for the persistence layer (7b-0). The point of moving off JSONL is
durability + atomic updates, so that's what's pinned: data survives a fresh
connection (= pod restart), label is a targeted UPDATE on the latest row, and the
one-time legacy migration is idempotent."""

import json
import sqlite3

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


def _seed_investigation(
    path, fp, service, alertname, summary, correct=True, grading_mode=store.CULPRIT
):
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
        grading_mode=grading_mode,
        path=path,
    )
    if correct is not None:
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


def test_inv_query_similar_ignores_a_correctly_hedged_non_incident(tmp_path):
    """`correct=1` on an inconclusive run means "it rightly blamed nobody".
    Serving that as precedent would hand the agent a non-incident as a solved
    case — the exact opposite of what the context is for."""
    path = tmp_path / "aiops.db"
    _seed_investigation(
        path,
        "fp3",
        "user-service",
        "x",
        "nothing wrong here",
        correct=True,
        grading_mode=store.INCONCLUSIVE,
    )
    assert not store.inv_query_similar("user-service", path=path)


def test_inv_query_similar_ignores_rows_of_unknown_grading_mode(tmp_path):
    """This output goes into a prompt; unknown provenance fails closed."""
    path = tmp_path / "aiops.db"
    _seed_investigation(
        path, "fp4", "api-gateway", "x", "who knows", correct=True, grading_mode=None
    )
    assert not store.inv_query_similar("api-gateway", path=path)


def test_inv_query_similar_needs_both_tables(tmp_path):
    """A calibration row with no investigation row retrieves nothing, however
    well it was graded — the library is a JOIN, and the two tables have
    different writers."""
    path = tmp_path / "aiops.db"
    store.cal_insert(
        run_id="lonely",
        ts="2026-08-06T00:00:00Z",
        confidence=0.9,
        summary="s",
        hypothesis="h",
        suspected_version=None,
        services=["payment-service"],
        grading_mode=store.CULPRIT,
        path=path,
    )
    store.cal_label("lonely", True, score=1.0, source="eval-harness", path=path)
    assert not store.inv_query_similar("payment-service", path=path)


# ---- store identity ---------------------------------------------------------


def _insert(path, run_id, confidence):
    store.cal_insert(
        run_id=run_id,
        ts="2026-01-01T00:00:00Z",
        confidence=confidence,
        summary="",
        hypothesis="",
        suspected_version=None,
        services=[],
        path=path,
    )


def test_describe_names_the_physical_store(tmp_path, monkeypatch):
    """The most expensive bug class here was never a wrong query — it was a right
    query against the wrong file. Two stores, same code, same schema, same
    filename, different mount, and nothing on any screen said which one you were
    reading. `describe` makes that answerable without shelling into the pod."""
    p = _cfg(monkeypatch, tmp_path)
    _insert(p, "r1", 0.9)

    d = store.describe(p)

    assert d["path"].endswith(str(p.name)) and d["exists"]
    assert d["tables"]["calibration"] == 1
    assert d["tables"]["investigations"] == 0


def test_describe_distinguishes_two_same_named_stores(tmp_path, monkeypatch):
    """The actual production shape: `aiops.db` on a dev box and `aiops.db` on a
    pod's volume, disagreeing for weeks."""
    _cfg(monkeypatch, tmp_path)
    dev = tmp_path / "dev" / "aiops.db"
    cluster = tmp_path / "cluster" / "aiops.db"
    _insert(dev, "r1", 0.9)
    for i in range(3):
        _insert(cluster, f"c{i}", 0.5)

    a, b = store.describe(dev), store.describe(cluster)

    assert a["path"] != b["path"]
    assert (a["tables"]["calibration"], b["tables"]["calibration"]) == (1, 3)


def test_describe_on_a_fresh_path_is_not_an_error(tmp_path, monkeypatch):
    """A path with no store reports unknown, not zero. "There is no file here"
    and "there is a file here and it is empty" are different answers, and the
    whole point of this function is telling stores apart."""
    _cfg(monkeypatch, tmp_path)
    d = store.describe(tmp_path / "nope" / "aiops.db")
    assert d["exists"] is False and d["tables"]["calibration"] is None
    assert "error" not in d


def test_describe_does_not_create_the_store_it_describes(tmp_path, monkeypatch):
    """The probe built to count these files must not add to them. `_connect`
    creates + migrates on open, which is right for every writer and wrong for
    the one caller whose whole job is asking which file it is looking at."""
    _cfg(monkeypatch, tmp_path)
    missing = tmp_path / "ghost" / "aiops.db"

    d = store.describe(missing)

    assert d["exists"] is False
    assert not missing.exists()
    assert d["tables"]["calibration"] is None  # unknown, not 0


def test_describe_never_writes_to_the_store_it_reads(tmp_path, monkeypatch):
    """`_connect` migrates on open. A probe that migrates the file it is only
    supposed to identify will silently alter older stores — which is exactly how
    a read-only run added a column to a snapshot being kept as evidence that the
    column was missing."""
    _cfg(monkeypatch, tmp_path)
    old = tmp_path / "old.db"
    conn = sqlite3.connect(str(old))
    conn.execute("CREATE TABLE calibration (id INTEGER PRIMARY KEY, confidence REAL)")
    conn.commit()
    conn.close()
    before = old.read_bytes()

    d = store.describe(old)

    assert d["tables"]["calibration"] == 0
    assert d["tables"]["audit"] is None  # table genuinely absent here
    assert old.read_bytes() == before  # byte-identical: nothing was migrated
