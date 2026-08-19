"""The write side of case memory: who may turn a run into precedent, and what a
run remembers about the paths that failed.

The read side is pinned in test_store.py. What is pinned here is the policy —
these are the tests that fail if someone makes the agent's own verdict on its
own work count."""

from typing import ClassVar

import app.action_requests as action_requests
import app.agent as agent
import app.calibration as calibration
import app.case_memory as case_memory
import app.eval.harness as harness
import app.governance as governance
import app.investigations as investigations
import app.store as store


def _cfg(monkeypatch, tmp_path):
    p = tmp_path / "aiops.db"
    monkeypatch.setattr(store.settings, "store_path", str(p))
    monkeypatch.setattr(case_memory.settings, "case_memory_enabled", True)
    return p


def _scope(fp="fp1", alertname="PaymentDeclineRateHigh", service="payment-service"):
    return case_memory.case_scope(fp=fp, alertname=alertname, service=service)


def test_scope_is_none_without_a_service(monkeypatch, tmp_path):
    """A key built from the alertname alone would collide every unrelated
    firing of that name into one incident."""
    _cfg(monkeypatch, tmp_path)
    with case_memory.case_scope(fp="fp", alertname="a", service=None) as sc:
        assert sc is None
        assert case_memory.current_scope() is None


def test_scope_is_none_when_disabled(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path)
    monkeypatch.setattr(case_memory.settings, "case_memory_enabled", False)
    with _scope() as sc:
        assert sc is None


def test_scope_key_survives_a_redeploy(monkeypatch, tmp_path):
    """Two runs of the same incident on different image tags — different fp,
    same case."""
    _cfg(monkeypatch, tmp_path)
    with _scope(fp="fp-v250") as a, _scope(fp="fp-v251") as b:
        assert a.case_key == b.case_key
        assert a.run_id != b.run_id


def test_observe_counts_attention_not_knowledge(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    with _scope() as sc:
        case_memory.observe(sc, path=p)
        case_memory.observe(sc, path=p)
    row = store.case_get(sc.case_key, p)
    assert row["occurrences"] == 2
    assert row["root_cause"] is None and row["status"] == "open"


def test_dead_end_outside_a_scope_is_a_noop(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    assert case_memory.remember_dead_end("query", "x", disproved_by="tool_result", path=p) is False


def test_dead_end_recorded_against_the_case_in_scope(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    with _scope() as sc:
        assert case_memory.remember_dead_end(
            "query",
            "PromQL referencing http_requests_total",
            disproved_by="tool_result",
            evidence="no such metric",
            path=p,
        )
    rows = store.case_ruled_out_for([sc.case_key], path=p)
    assert len(rows) == 1
    assert rows[0]["run_id"] == sc.run_id


def test_dead_end_ttl_stops_being_recalled(monkeypatch, tmp_path):
    """'Tempo had nothing' is a statement about the retention window."""
    p = _cfg(monkeypatch, tmp_path)
    with _scope() as sc:
        case_memory.remember_dead_end(
            "query", "trace lookup", disproved_by="tool_result", ttl_seconds=1, path=p
        )
    assert store.case_ruled_out_for([sc.case_key], now_ts="2000-01-01T00:00:00Z", path=p)
    assert store.case_ruled_out_for([sc.case_key], now_ts="2099-01-01T00:00:00Z", path=p) == []


def _labeled_run(
    p,
    *,
    source,
    grading_mode,
    correct=True,
    summary="new_validator rejects odd cents",
    correction_note=None,
):
    """One recorded run, then a verdict on it, through the real entry points."""
    with _scope() as sc:
        case_memory.observe(sc, path=p)

        class F:
            confidence = 0.7
            hypothesis = "code regression"
            suspected_version = "v2.5.0"
            services: ClassVar[list[str]] = ["payment-service"]

        F.summary = summary
        calibration.record_run(F, run_id="fp1", path=p, case_key=sc.case_key)
    calibration.label_run(
        "fp1",
        correct=correct,
        source=source,
        grading_mode=grading_mode,
        correction_note=correction_note,
        path=p,
    )
    return sc.case_key


def test_self_verification_never_becomes_precedent(monkeypatch, tmp_path):
    """`execution.py` labels its own remediation as verified. That must move a
    calibration row and nothing else."""
    p = _cfg(monkeypatch, tmp_path)
    key = _labeled_run(p, source="remediation-verified", grading_mode=store.CULPRIT)
    assert store.case_get(key, p)["root_cause"] is None
    assert store.case_query_similar("payment-service", path=p) == []


def test_human_label_confirms_the_case(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    key = _labeled_run(p, source="ui", grading_mode=store.CULPRIT)
    row = store.case_get(key, p)
    assert row["root_cause"] == "new_validator rejects odd cents"
    assert row["root_cause_source"] == "ui"
    assert len(store.case_query_similar("payment-service", path=p)) == 1


def test_grader_label_confirms_the_case(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    key = _labeled_run(p, source="eval-harness", grading_mode=store.CULPRIT)
    assert store.case_get(key, p)["root_cause_source"] == "eval-harness"


def test_correct_hedge_is_a_false_positive_not_a_root_cause(monkeypatch, tmp_path):
    """'It rightly blamed nobody' is a fact about the alert, not a solved case."""
    p = _cfg(monkeypatch, tmp_path)
    key = _labeled_run(p, source="ui", grading_mode=store.INCONCLUSIVE)
    row = store.case_get(key, p)
    assert row["status"] == "false_positive"
    assert row["root_cause"] is None
    assert store.case_query_similar("payment-service", path=p) == []


def test_wrong_label_leaves_the_root_cause_alone(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    key = _labeled_run(p, source="ui", grading_mode=store.CULPRIT, correct=False)
    assert store.case_get(key, p)["root_cause"] is None


def test_wrong_label_becomes_a_disproof(monkeypatch, tmp_path):
    """Being told the answer was wrong is not knowing the answer, but it is
    knowing one answer this incident does not have."""
    p = _cfg(monkeypatch, tmp_path)
    key = _labeled_run(
        p,
        source="ui",
        grading_mode=store.CULPRIT,
        correct=False,
        correction_note="the flag was already off, this was the ConfigMap",
    )
    (row,) = store.case_ruled_out_for([key], path=p)
    assert row["kind"] == "hypothesis"
    assert row["subject"] == "new_validator rejects odd cents"
    assert row["evidence"] == "the flag was already off, this was the ConfigMap"
    assert row["disproved_by"] == "human"


def test_a_disproof_needs_a_hypothesis_to_refute(monkeypatch, tmp_path):
    """A wrong `inconclusive` run declined to blame anyone. There is nothing on
    the table to rule out, and the correction note is the answer — recording it
    under "already ruled out" would be worse than recording nothing."""
    p = _cfg(monkeypatch, tmp_path)
    key = _labeled_run(
        p,
        source="ui",
        grading_mode=store.INCONCLUSIVE,
        correct=False,
        correction_note="payment v2.5.0, same as last time",
    )
    assert store.case_ruled_out_for([key], path=p) == []


def test_self_verification_may_not_disprove_either(monkeypatch, tmp_path):
    """The executor grading its own remediation is not evidence in either
    direction."""
    p = _cfg(monkeypatch, tmp_path)
    key = _labeled_run(p, source="remediation-failed", grading_mode=store.CULPRIT, correct=False)
    assert store.case_ruled_out_for([key], path=p) == []


def test_disproof_from_an_unknown_source_is_ignored(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    key = _labeled_run(p, source="some-new-bot", grading_mode=store.CULPRIT, correct=False)
    assert store.case_ruled_out_for([key], path=p) == []


def test_unknown_grading_mode_fails_closed(monkeypatch, tmp_path):
    """Whatever this row's `correct` was answering, nobody recorded it — and the
    text would go straight into a prompt."""
    p = _cfg(monkeypatch, tmp_path)
    key = _labeled_run(p, source="ui", grading_mode=None)
    assert store.case_get(key, p)["root_cause"] is None


# ---- what actually reaches the prompt --------------------------------------


def _confirmed_case(p, *, alertname="PaymentDeclineRateHigh", occurrences=1):
    key = store.case_key(alertname, "payment-service")
    for _ in range(occurrences):
        store.case_upsert(
            key=key,
            ts="2026-08-16T05:00:00Z",
            alertname=alertname,
            service="payment-service",
            path=p,
        )
    store.case_confirm(
        key,
        root_cause="new_validator rejects odd cents",
        source="ui",
        run_id="r1",
        ts="2026-08-16T05:00:00Z",
        resolution={"action": "k8s.rollout_undo"},
        path=p,
    )
    return key


def test_recall_block_is_empty_without_a_confirmed_case(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    store.case_upsert(
        key=store.case_key("a", "payment-service"),
        ts="t",
        alertname="a",
        service="payment-service",
        path=p,
    )
    assert agent._past_incident_context("payment-service") == ""


def test_a_human_disproof_does_not_reach_the_prompt(monkeypatch, tmp_path):
    """It used to, and that was measured as harmful: naming the refuted branch
    made three seeds out of three take it, at lower confidence than the arm that
    never saw it. The verdict is still recorded and still used — after the
    answer, in `refutation.py`, not before it."""
    p = _cfg(monkeypatch, tmp_path)
    key = _labeled_run(
        p,
        source="ui",
        grading_mode=store.CULPRIT,
        correct=False,
        correction_note="latency was flat on that version",
    )
    (row,) = store.case_ruled_out_for([key], path=p)
    assert row["kind"] == "hypothesis"

    block = agent._past_incident_context("payment-service", "PaymentDeclineRateHigh")
    assert "new_validator rejects odd cents" not in block


def test_the_dead_ends_a_machine_can_enforce_still_reach_the_prompt(monkeypatch, tmp_path):
    """The distinction the move rests on: a query that cannot work here is a
    fact about the environment, not a suggestion about the cause."""
    p = _cfg(monkeypatch, tmp_path)
    key = store.case_key("PaymentDeclineRateHigh", "payment-service")
    store.case_upsert(
        key=key,
        ts="2026-08-16T05:00:00Z",
        alertname="PaymentDeclineRateHigh",
        service="payment-service",
        path=p,
    )
    store.ruled_out_insert(
        key=key,
        run_id="r1",
        ts=store.datetime.now(store.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        kind="query",
        subject="LogQL stream selector on service",
        evidence="not an indexable stream label in this Loki",
        disproved_by="tool_result",
        path=p,
    )
    block = agent._past_incident_context("payment-service", "PaymentDeclineRateHigh")
    assert "LogQL stream selector on service" in block


def test_a_disproof_is_scoped_to_its_own_incident(monkeypatch, tmp_path):
    """Same service, different alert. The dead end belongs to the incident, not
    to everything that ever fired on payment-service."""
    p = _cfg(monkeypatch, tmp_path)
    _labeled_run(p, source="ui", grading_mode=store.CULPRIT, correct=False)
    assert agent._past_incident_context("payment-service", "PaymentChargeLatencyHigh") == ""


def test_recall_block_carries_cause_and_dead_ends(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    key = _confirmed_case(p, occurrences=6)
    store.ruled_out_insert(
        key=key,
        run_id="r1",
        ts="2026-08-16T05:00:00Z",
        kind="query",
        subject="LogQL stream selector on service",
        evidence="not an indexable stream label in this Loki",
        disproved_by="tool_result",
        path=p,
    )
    block = agent._past_incident_context("payment-service")
    assert block.startswith(agent.PAST_CASES_HEADING)
    assert "×6" in block and "confirmed by ui" in block
    assert "new_validator rejects odd cents" in block
    assert "resolved by: k8s.rollout_undo" in block
    assert "Already ruled out here" in block
    assert "LogQL stream selector on service" in block


def test_recall_block_omits_dead_ends_the_model_only_claimed(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    key = _confirmed_case(p)
    store.ruled_out_insert(
        key=key,
        run_id="r1",
        ts="t",
        kind="hypothesis",
        subject="downstream DB saturation",
        disproved_by="model",
        path=p,
    )
    block = agent._past_incident_context("payment-service")
    assert "Already ruled out" not in block
    assert "downstream DB" not in block


def test_recall_arms_differ(monkeypatch, tmp_path):
    """The control arm reads the old JOIN, which this store cannot satisfy —
    so the A/B has a real zero on one side rather than the same text twice."""
    p = _cfg(monkeypatch, tmp_path)
    _confirmed_case(p)
    assert agent._past_incident_context("payment-service") != ""
    monkeypatch.setattr(agent.settings, "case_recall_enabled", False)
    assert agent._past_incident_context("payment-service") == ""


# ---- one verdict, one run --------------------------------------------------


def test_label_by_fingerprint_resolves_to_the_latest_run(monkeypatch, tmp_path):
    """The plugin's endpoint only ever has a fingerprint. It must still land on
    exactly one run — and the resolution is now something the code does on
    purpose rather than a side effect of ORDER BY id DESC."""
    p = _cfg(monkeypatch, tmp_path)
    for run_id in ("fp1-run-a", "fp1-run-b"):
        store.cal_insert(
            run_id=run_id,
            ts="t",
            confidence=0.7,
            summary=run_id,
            hypothesis="h",
            suspected_version=None,
            services=[],
            grading_mode=store.CULPRIT,
            fp="fp1",
        )
    assert store.cal_resolve_run_id("fp1", p) == "fp1-run-b"
    assert store.cal_resolve_run_id("fp1-run-a", p) == "fp1-run-a"
    assert store.cal_resolve_run_id("nothing", p) is None

    calibration.label_run("fp1", correct=True, source="ui", path=p)
    labeled = {r["run_id"]: r["correct"] for r in store.cal_load(p) if r["correct"] is not None}
    assert labeled == {"fp1-run-b": 1}


def test_a_verdict_no_longer_covers_every_run_of_the_alert(monkeypatch, tmp_path):
    """The day36 shape: nine runs of one alert, one human verdict. Exactly one
    investigation may show it."""
    p = _cfg(monkeypatch, tmp_path)
    for i in range(9):
        run_id = f"fp1-run-{i}"
        _inv_row(p, fp="fp1", run_id=run_id, summary=f"conclusion {i}")
        store.cal_insert(
            run_id=run_id,
            ts=f"2026-08-16T05:0{i}:00Z",
            confidence=0.7,
            summary=f"conclusion {i}",
            hypothesis="h",
            suspected_version=None,
            services=[],
            grading_mode=store.CULPRIT,
            fp="fp1",
        )
    calibration.label_run("fp1", correct=True, source="ui", path=p)
    rows = investigations.list_investigations(path=p)
    # list keeps the latest row per fingerprint, and that row is the labeled run
    assert [r["correct"] for r in rows] == [True]
    assert rows[0]["run_id"] == "fp1-run-8"


def _inv_row(p, *, fp, run_id, summary, case_key=None):
    rec = investigations.InvestigationRecord(
        fp=fp,
        run_id=run_id,
        ts=f"2026-08-16T05:00:0{run_id[-1]}Z",
        alertname="PaymentDeclineRateHigh",
        service="payment-service",
        summary=summary,
    )
    store.inv_insert(fp, rec.ts, rec.model_dump_json(), p, run_id=run_id, case_key=case_key)


def test_action_request_remembers_the_run_that_proposed_it(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    with _scope() as sc:
        req = action_requests.create_from_decision(
            "fp1",
            governance.Decision(
                action="k8s.rollout_undo",
                autonomy=governance.Autonomy.PROPOSE,
                reason="test",
                requires_human=True,
                reversible=True,
                confidence=0.7,
                calibration_note="",
                requires_approval=True,
            ),
            path=p,
        )
    assert req.run_id == sc.run_id
    assert store.ar_get(req.request_id, p)["run_id"] == sc.run_id


def _proposal(p, sc, *, args=None):
    return action_requests.create_from_decision(
        "fp1",
        governance.Decision(
            action="k8s.rollout_undo",
            autonomy=governance.Autonomy.PROPOSE,
            reason="test",
            requires_human=True,
            reversible=True,
            confidence=0.7,
            calibration_note="",
            requires_approval=True,
        ),
        args=args or {"namespace": "demo", "deployment": "payment-service"},
        path=p,
    )


def test_rejection_reason_becomes_a_dead_end_on_the_case(monkeypatch, tmp_path):
    """The whole point of asking for a reason: it has to reach the next run."""
    p = _cfg(monkeypatch, tmp_path)
    with _scope() as sc:
        case_memory.observe(sc, path=p)
        _inv_row(p, fp="fp1", run_id=sc.run_id, summary="decline spike", case_key=sc.case_key)
        req = _proposal(p, sc)
    action_requests.reject(req.request_id, actor="nathan", reason="we roll forward here", path=p)

    assert store.ar_get(req.request_id, p)["decision_note"] == "we roll forward here"
    (row,) = store.case_ruled_out_for([sc.case_key], path=p)
    assert row["kind"] == "action"
    assert row["subject"] == "k8s.rollout_undo on demo/payment-service"
    assert row["evidence"] == "we roll forward here"
    assert row["disproved_by"] == "human"


def test_rejection_without_a_reason_is_still_remembered(monkeypatch, tmp_path):
    """Requiring a justification before the system remembers anything is how the
    column ends up empty on every row."""
    p = _cfg(monkeypatch, tmp_path)
    with _scope() as sc:
        case_memory.observe(sc, path=p)
        _inv_row(p, fp="fp1", run_id=sc.run_id, summary="decline spike", case_key=sc.case_key)
        req = _proposal(p, sc)
    action_requests.reject(req.request_id, actor="nathan", path=p)
    (row,) = store.case_ruled_out_for([sc.case_key], path=p)
    assert "nathan" in row["evidence"]


def test_approval_leaves_no_dead_end(monkeypatch, tmp_path):
    """Only 'no' is evidence. A yes is what the proposal already said."""
    p = _cfg(monkeypatch, tmp_path)
    with _scope() as sc:
        case_memory.observe(sc, path=p)
        _inv_row(p, fp="fp1", run_id=sc.run_id, summary="decline spike", case_key=sc.case_key)
        req = _proposal(p, sc)
    action_requests.approve(req.request_id, actor="nathan", path=p)
    assert store.case_ruled_out_for([sc.case_key], path=p) == []


def test_rejection_lands_before_the_run_has_written_itself_down(monkeypatch, tmp_path):
    """The investigation row is written when the run *ends*. A decision can
    arrive at any time, so the proposal carries its own case key rather than
    resolving one from a row that may not exist yet."""
    p = _cfg(monkeypatch, tmp_path)
    with _scope() as sc:
        req = _proposal(p, sc)  # no investigation row, no calibration row
    assert store.case_key_for_run(sc.run_id, path=p) is None
    action_requests.reject(
        req.request_id, actor="nathan", reason="not during business hours", path=p
    )
    (row,) = store.case_ruled_out_for([sc.case_key], path=p)
    assert row["evidence"] == "not during business hours"


def test_a_rejection_with_no_traceable_run_is_dropped(monkeypatch, tmp_path):
    """A proposal made outside a case scope has no incident to hang the
    rejection on. Better a lost note than one filed under the wrong case."""
    p = _cfg(monkeypatch, tmp_path)
    req = _proposal(p, None)
    assert action_requests.reject(req.request_id, actor="nathan", reason="no", path=p) is not None
    assert (
        store.case_ruled_out_for(
            [store.case_key("PaymentDeclineRateHigh", "payment-service")], path=p
        )
        == []
    )


# ---- the A/B is only an A/B if the library has not seen the fixture ---------


def _fixture(alertname="PaymentDeclineRateHigh", service="payment-service"):
    return harness.Fixture(
        id=f"{service}-{alertname}",
        alert={
            "labels": {"alertname": alertname, "service_name": service},
            "annotations": {},
            "startsAt": "now",
        },
        truth={"service": service},
    )


def test_recall_arm_restores_the_previous_setting(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path)
    monkeypatch.setattr(harness.settings, "case_recall_enabled", True)
    with harness.recall_arm(False):
        assert harness.settings.case_recall_enabled is False
    assert harness.settings.case_recall_enabled is True


def test_clean_ab_when_the_library_is_empty(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path)
    assert harness.library_overlap([_fixture()]) == []


def test_open_book_detected_when_the_library_answers_the_fixture(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    _confirmed_case(p)
    assert harness.library_overlap([_fixture()]) == [("payment-service-PaymentDeclineRateHigh", 1)]


def test_a_disproof_alone_makes_the_fixture_open_book(monkeypatch, tmp_path):
    """A human's "not that version" narrows the search as much as a root cause
    does. A run that gets it is not running the same experiment as one that
    doesn't, so the report has to say so."""
    p = _cfg(monkeypatch, tmp_path)
    _labeled_run(p, source="ui", grading_mode=store.CULPRIT, correct=False)
    assert store.case_query_similar("payment-service", path=p) == []
    assert harness.library_overlap([_fixture()]) == [("payment-service-PaymentDeclineRateHigh", 1)]


def test_overlap_reads_the_store_the_agent_reads(monkeypatch, tmp_path):
    """The eval store and the runtime store are different files. Recall takes no
    path argument, so it resolves through settings.store_path — checking the
    eval store here would call a dirty experiment clean."""
    p = _cfg(monkeypatch, tmp_path)
    _confirmed_case(p)
    eval_store = tmp_path / "eval.db"
    store.init(eval_store)
    assert store.case_query_similar("payment-service", path=eval_store) == []
    assert harness.library_overlap([_fixture()]) != []


def test_ab_report_leads_with_the_open_book_warning(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path)
    on = [harness.summarize("f1", [])]
    off = [harness.summarize("f1", [])]
    clean = harness.format_ab_report(on, off, [])
    dirty = harness.format_ab_report(on, off, [("f1", 2)])
    assert clean.startswith("clean A/B")
    assert dirty.startswith("OPEN BOOK")
    assert "f1: 2 case(s) recalled" in dirty


# ---- forgetting -------------------------------------------------------------


def test_a_dead_end_ages_out_of_recall(monkeypatch, tmp_path):
    """The explicit TTL covered the ones known to be short-lived when written.
    Everything a person writes has no TTL at all and would otherwise be recalled
    forever."""
    p = _cfg(monkeypatch, tmp_path)
    key = store.case_key("PaymentDeclineRateHigh", "payment-service")
    store.case_upsert(
        key=key,
        ts="2026-08-16T05:00:00Z",
        alertname="PaymentDeclineRateHigh",
        service="payment-service",
        path=p,
    )
    store.ruled_out_insert(
        key=key,
        run_id="r1",
        ts="2026-01-01T00:00:00Z",
        kind="action",
        subject="k8s.rollout_undo on demo/payment-service",
        evidence="not during business hours",
        disproved_by="human",
        path=p,
    )
    assert store.case_ruled_out_for([key], path=p) == []
    monkeypatch.setattr(store.settings, "case_dead_end_max_age_days", 3650)
    assert len(store.case_ruled_out_for([key], path=p)) == 1


def test_a_case_nobody_has_seen_in_months_stops_being_a_prior(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    _confirmed_case(p)
    assert len(store.case_query_similar("payment-service", path=p)) == 1
    monkeypatch.setattr(store.settings, "case_max_age_days", 1)
    assert store.case_query_similar("payment-service", path=p) == []


def test_forgetting_retracts_the_claims_and_keeps_the_history(monkeypatch, tmp_path):
    """Somebody says the ground moved. What is dropped is what the case
    asserts — not the fact that it happened."""
    p = _cfg(monkeypatch, tmp_path)
    key = _confirmed_case(p, occurrences=6)
    store.ruled_out_insert(
        key=key,
        run_id="r1",
        ts=store.datetime.now(store.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        kind="query",
        subject="LogQL stream selector on service",
        evidence="",
        disproved_by="tool_result",
        path=p,
    )
    assert store.case_query_similar("payment-service", path=p)
    assert store.case_ruled_out_for([key], path=p)

    assert store.case_forget(key, path=p) == {"cases": 1, "dead_ends": 1}

    assert store.case_query_similar("payment-service", path=p) == []
    assert store.case_ruled_out_for([key], path=p) == []
    row = store.case_get(key, p)
    assert row["occurrences"] == 6
    assert row["status"] == "open"


def test_forgetting_an_unknown_case_reports_nothing_changed(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    assert store.case_forget("deadbeef", path=p) == {"cases": 0, "dead_ends": 0}


# ---- a refusal that binds the next run --------------------------------------


def _reject_once(p, sc, *, reason="not during business hours"):
    req = _proposal(p, sc)
    action_requests.reject(req.request_id, actor="nathan", reason=reason, path=p)


def test_a_declined_action_is_not_proposed_again(monkeypatch, tmp_path):
    """At PROPOSE it would make someone type the same refusal twice, and the
    second refusal says less than the first — it is about our persistence."""
    p = _cfg(monkeypatch, tmp_path)
    monkeypatch.setattr(governance.settings, "governance_min_human_labeled_runs", 0)
    with _scope() as sc:
        _reject_once(p, sc)
        hit = case_memory.prior_rejection(sc.case_key, "k8s.rollout_undo", "demo/payment-service")
    assert hit is not None

    d = governance.decide(
        governance.registry.get("k8s.rollout_undo"), 0.95, {"labeled": 100}, rejected=hit
    )
    assert d.autonomy is governance.Autonomy.ESCALATE
    assert "not during business hours" in d.reason


def test_a_refusal_is_about_the_target_it_named(monkeypatch, tmp_path):
    """ "Don't restart payment" is not a statement about restarting anything
    else."""
    p = _cfg(monkeypatch, tmp_path)
    with _scope() as sc:
        _reject_once(p, sc)
        assert (
            case_memory.prior_rejection(sc.case_key, "k8s.rollout_undo", "demo/order-service")
            is None
        )
        assert (
            case_memory.prior_rejection(sc.case_key, "k8s.restart", "demo/payment-service") is None
        )


def test_a_refusal_binds_only_its_own_incident(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    with _scope() as sc:
        _reject_once(p, sc)
    other = store.case_key("PaymentChargeLatencyHigh", "payment-service")
    assert case_memory.prior_rejection(other, "k8s.rollout_undo", "demo/payment-service") is None


def test_a_refusal_stops_binding_when_it_stops_being_recalled(monkeypatch, tmp_path):
    """The gate and the prompt read through the same freshness rules. A rule
    enforced but no longer mentioned is a rule nobody can explain."""
    p = _cfg(monkeypatch, tmp_path)
    with _scope() as sc:
        _reject_once(p, sc)
    # A window that has already closed — the refusal was written seconds ago, so
    # anything short of that keeps it inside a same-second boundary.
    monkeypatch.setattr(store.settings, "case_dead_end_max_age_days", -1)
    assert (
        case_memory.prior_rejection(sc.case_key, "k8s.rollout_undo", "demo/payment-service") is None
    )


def test_forgetting_a_case_releases_its_refusals(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    with _scope() as sc:
        _reject_once(p, sc)
    store.case_forget(sc.case_key, path=p)
    assert (
        case_memory.prior_rejection(sc.case_key, "k8s.rollout_undo", "demo/payment-service") is None
    )
