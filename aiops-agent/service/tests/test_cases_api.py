"""The read endpoints for case memory.

Until these existed the only way to see what the system had learned was to exec
into the pod and open the SQLite file, so what is pinned here is mostly that the
API does *not* inherit recall's filters: browsing must show the unlabelled and
the untrusted cases, because those are the ones a human is there to act on."""

import app.store as store


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(store.settings, "store_path", str(tmp_path / "aiops.db"))
    return TestClient(app)


def _seed(key="k1", ts="2026-08-20T00:00:00Z", service="payment-service", alertname="Decline"):
    store.case_upsert(key=key, ts=ts, alertname=alertname, service=service)
    return key


def test_list_returns_cases_and_total(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed("k1")
    _seed("k2", ts="2026-08-21T00:00:00Z")
    body = c.get("/cases").json()
    assert body["total"] == 2
    assert [x["case_key"] for x in body["cases"]] == ["k2", "k1"]


def test_list_filters_to_the_ones_waiting_on_a_human(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed("k-todo")
    _seed("k-done")
    store.case_confirm(
        "k-done", root_cause="rc", source="human", run_id="r", ts="2026-08-20T01:00:00Z"
    )
    body = c.get("/cases", params={"unlabeled": True}).json()
    assert [x["case_key"] for x in body["cases"]] == ["k-todo"]


def test_detail_reports_whether_a_case_is_actually_recalled(monkeypatch, tmp_path):
    """A case can hold a root cause and still never reach a prompt. The detail
    view has to say which, or it describes memory the agent does not have."""
    c = _client(monkeypatch, tmp_path)
    _seed("k1")
    assert c.get("/cases/k1").json()["recallable"] is False
    store.case_confirm("k1", root_cause="rc", source="human", run_id="r", ts="2026-08-20T01:00:00Z")
    body = c.get("/cases/k1").json()
    assert body["recallable"] is True
    assert body["case"]["root_cause"] == "rc"


def test_a_false_positive_is_listed_but_never_recallable(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed("k1")
    store.case_confirm("k1", root_cause="rc", source="human", run_id="r", ts="2026-08-20T01:00:00Z")
    store.case_set_status("k1", "false_positive")
    body = c.get("/cases/k1").json()
    assert body["recallable"] is False
    assert c.get("/cases").json()["total"] == 1


def test_detail_carries_runs_and_dead_ends(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    key = _seed("k1")
    store.inv_insert("fp1", "t1", "{}", run_id="r1", case_key=key)
    store.ruled_out_insert(
        key=key, run_id="r1", ts="t1", kind="query", subject="tempo", disproved_by="tool_result"
    )
    body = c.get("/cases/k1").json()
    assert [r["run_id"] for r in body["runs"]] == ["r1"]
    assert [d["subject"] for d in body["dead_ends"]] == ["tempo"]


def test_context_route_still_wins_over_the_key_route(monkeypatch, tmp_path):
    """`/cases/context` is declared first on purpose; a case whose key happened
    to be 'context' must not shadow it."""
    c = _client(monkeypatch, tmp_path)
    body = c.get("/cases/context", params={"service": "payment-service"}).json()
    assert "context" in body


def test_unknown_case_is_404(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    assert c.get("/cases/nope").status_code == 404


def test_a_human_can_give_a_case_its_root_cause(monkeypatch, tmp_path):
    """The queue of incidents with no cause was unworkable because nothing but
    the grading paths could write one."""
    c = _client(monkeypatch, tmp_path)
    _seed("k1")
    body = c.post(
        "/cases/k1/root-cause",
        json={"root_cause": "new_validator rejects odd cents", "run_id": "r1"},
    ).json()
    assert body["root_cause"] == "new_validator rejects odd cents"
    assert body["root_cause_source"] == "human"
    assert body["status"] == "resolved"
    # And it is precedent now, not just a stored string.
    assert c.get("/cases/k1").json()["recallable"] is True


def test_the_caller_cannot_name_its_own_source(monkeypatch, tmp_path):
    """`source` is fixed to human by the endpoint. Letting the body set it is
    how an agent's own verdict would end up vouching for itself."""
    c = _client(monkeypatch, tmp_path)
    _seed("k1")
    c.post("/cases/k1/root-cause", json={"root_cause": "rc", "source": "self"})
    assert store.case_get("k1")["root_cause_source"] == "human"


def test_an_empty_root_cause_is_rejected(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed("k1")
    assert c.post("/cases/k1/root-cause", json={"root_cause": "   "}).status_code == 400
    assert store.case_get("k1")["root_cause"] is None


def test_root_cause_on_an_unknown_case_is_404(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    assert c.post("/cases/nope/root-cause", json={"root_cause": "rc"}).status_code == 404
