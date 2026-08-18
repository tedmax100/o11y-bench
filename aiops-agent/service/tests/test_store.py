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


# ---- case memory ----------------------------------------------------------


def _inv(store_path, fp, alertname, service, git_version="", summary=""):
    payload = json.dumps(
        {
            "fp": fp,
            "ts": "2026-08-16T05:00:00Z",
            "alertname": alertname,
            "service": service,
            "git_version": git_version,
            "summary": summary,
        }
    )
    store.inv_insert(fp, "2026-08-16T05:00:00Z", payload, store_path)


def test_case_key_ignores_version_and_alertname_spelling(tmp_path, monkeypatch):
    """The whole reason this key exists: a redeploy is a new alert instance but
    not a new incident, and the same alert is spelled three ways."""
    a = store.case_key("PaymentDeclineRateHigh", "payment-service")
    b = store.case_key("payment_decline_rate_high", "payment-service")
    assert a == b
    # version is not an input at all — no parameter to pass it through
    assert a != store.case_key("payment-decline-rate-high", "order-service")


def test_new_run_id_is_unique_per_call(tmp_path, monkeypatch):
    ids = {store.new_run_id("fp") for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("fp-") for i in ids)


def test_backfill_collapses_versioned_fingerprints_into_one_case(tmp_path, monkeypatch):
    """Reproduces the day36 drill shape: one incident investigated across five
    image tags, which `fp` split into five unrelated fingerprints."""
    p = _cfg(monkeypatch, tmp_path)
    for i in range(5):
        _inv(p, f"fp{i}", "payment-decline-rate-high", "payment-service", f"v2.5.1-drill-{i}")
    counts = store.backfill_cases(p)
    assert counts == {"run_id": 5, "case_key": 5, "cases": 1}
    key = store.case_key("payment-decline-rate-high", "payment-service")
    assert store.case_get(key, p)["occurrences"] == 5
    # backfill records the incident, never its cause
    assert store.case_get(key, p)["root_cause"] is None


def test_backfill_is_idempotent(tmp_path, monkeypatch):
    p = _cfg(monkeypatch, tmp_path)
    _inv(p, "fp0", "a", "svc")
    store.backfill_cases(p)
    assert store.backfill_cases(p) == {"run_id": 0, "case_key": 0, "cases": 0}
    assert store.case_get(store.case_key("a", "svc"), p)["occurrences"] == 1


def test_case_confirm_rejects_self_attestation(tmp_path, monkeypatch):
    p = _cfg(monkeypatch, tmp_path)
    key = store.case_key("a", "svc")
    store.case_upsert(key=key, ts="t", alertname="a", service="svc", path=p)
    assert (
        store.case_confirm(key, root_cause="it was me", source="self", run_id="r1", ts="t", path=p)
        is False
    )
    assert store.case_get(key, p)["root_cause"] is None
    assert store.case_confirm(
        key, root_cause="new_validator", source="human", run_id="r1", ts="t", path=p
    )
    assert store.case_get(key, p)["root_cause"] == "new_validator"


def test_case_confirm_on_unknown_case_writes_nothing(tmp_path, monkeypatch):
    p = _cfg(monkeypatch, tmp_path)
    assert (
        store.case_confirm("nope", root_cause="x", source="human", run_id="r", ts="t", path=p)
        is False
    )
    assert store.case_get("nope", p) is None


def test_recall_returns_one_row_per_case_not_per_run(tmp_path, monkeypatch):
    """The predecessor joined on `fp` and returned five near-copies of one
    incident. One confirmed case must yield exactly one recalled row."""
    p = _cfg(monkeypatch, tmp_path)
    key = store.case_key("a", "svc")
    for i in range(9):
        _inv(p, f"fp{i}", "a", "svc", f"v{i}")
    store.backfill_cases(p)
    store.case_confirm(
        key,
        root_cause="new_validator rejects odd cents",
        source="grader",
        run_id="fp0",
        ts="t",
        resolution={"action": "k8s.rollout_undo"},
        path=p,
    )
    rows = store.case_query_similar("svc", path=p)
    assert len(rows) == 1
    assert rows[0]["occurrences"] == 9
    assert rows[0]["resolution"] == {"action": "k8s.rollout_undo"}


def test_false_positive_case_is_kept_but_never_recalled(tmp_path, monkeypatch):
    p = _cfg(monkeypatch, tmp_path)
    key = store.case_key("a", "svc")
    store.case_upsert(key=key, ts="t", alertname="a", service="svc", path=p)
    store.case_confirm(key, root_cause="noise", source="human", run_id="r", ts="t", path=p)
    store.case_set_status(key, "false_positive", path=p)
    assert store.case_get(key, p)["status"] == "false_positive"
    assert store.case_query_similar("svc", path=p) == []


def test_recurring_after_reoccurrence_stays_recallable(tmp_path, monkeypatch):
    p = _cfg(monkeypatch, tmp_path)
    key = store.case_key("a", "svc")
    store.case_upsert(key=key, ts="t1", alertname="a", service="svc", path=p)
    store.case_confirm(key, root_cause="rc", source="human", run_id="r", ts="t1", path=p)
    store.case_upsert(key=key, ts="t2", alertname="a", service="svc", path=p)
    assert store.case_get(key, p)["status"] == "recurring"
    assert len(store.case_query_similar("svc", path=p)) == 1


def test_ruled_out_excludes_model_claims_and_expired_rows(tmp_path, monkeypatch):
    p = _cfg(monkeypatch, tmp_path)
    key = store.case_key("a", "svc")
    common = {"key": key, "run_id": "r", "ts": "t", "path": p}
    store.ruled_out_insert(
        kind="query", subject="tempo service_name=", disproved_by="tool_result", **common
    )
    store.ruled_out_insert(
        kind="hypothesis", subject="db saturation", disproved_by="model", **common
    )
    store.ruled_out_insert(
        kind="query",
        subject="traces older than 1h",
        disproved_by="tool_result",
        expires_ts="2026-08-16T07:00:00Z",
        **common,
    )
    rows = store.case_ruled_out_for([key], now_ts="2026-08-16T08:00:00Z", path=p)
    assert [r["subject"] for r in rows] == ["tempo service_name="]


def test_ruled_out_invalidate_by_kind(tmp_path, monkeypatch):
    p = _cfg(monkeypatch, tmp_path)
    key = store.case_key("a", "svc")
    for kind in ("query", "query", "hypothesis"):
        store.ruled_out_insert(
            key=key, run_id="r", ts="t", kind=kind, subject=kind, disproved_by="human", path=p
        )
    assert store.ruled_out_invalidate(key, kind="query", path=p) == 2
    rows = store.case_ruled_out_for([key], now_ts="t", path=p)
    assert [r["kind"] for r in rows] == ["hypothesis"]


def test_case_tables_survive_a_reconnect(tmp_path, monkeypatch):
    p = _cfg(monkeypatch, tmp_path)
    key = store.case_key("a", "svc")
    store.case_upsert(key=key, ts="t", alertname="a", service="svc", path=p)
    with sqlite3.connect(str(p)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1
