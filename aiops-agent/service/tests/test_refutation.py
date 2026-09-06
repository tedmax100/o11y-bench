"""An answer that repeats what a person crossed out.

Day39's A/B put the refuted hypothesis in the prompt and measured the opposite
of the intended effect: three seeds out of three answered the ruled-out
hypothesis, at lower confidence than the arm that never saw it. These tests pin
the replacement — the same information, checked after the answer instead of
suggested before it — starting with the exact pair of strings that failed."""

import app.agent as agent
import app.case_memory as case_memory
import app.refutation as refutation
import app.store as store

# Verbatim from ab-intervention-20260819.txt.
REFUTED = "a code regression in order-service itself"
ANSWER = "Code regression in order-service v1.8.2 causing auth failures."
OTHER_ANSWER = (
    "user-service auth checks are timing out against the session store, and "
    "order-service cancels the orders downstream of them."
)


def _row(subject, kind="hypothesis", disproved_by="human", evidence=""):
    return {"subject": subject, "kind": kind, "disproved_by": disproved_by, "evidence": evidence}


def test_the_answer_that_started_this_is_caught():
    assert refutation.find_repeat(ANSWER, [_row(REFUTED)]) is not None


def test_a_different_cause_passes():
    """The check must not simply reject anything that mentions order-service —
    the alert fires there, so every answer will."""
    assert refutation.find_repeat(OTHER_ANSWER, [_row(REFUTED)]) is None


def test_connective_words_do_not_carry_a_match():
    """A hypothesis made only of filler discriminates nothing, and scores zero
    rather than matching everything."""
    filler = "caused by an issue in the code"
    assert refutation.significant_words(filler) == set()
    assert refutation.overlap(filler, "the root cause is a code issue") == 0.0
    assert refutation.find_repeat("anything at all", [_row(filler)]) is None


def test_only_hypotheses_are_checked():
    """A ruled-out *query* being mentioned in an answer is not a wrong answer."""
    rows = [_row("LogQL stream selector on service", kind="query")]
    assert refutation.find_repeat("I tried the LogQL stream selector on service", rows) is None


def test_the_best_match_wins():
    rows = [_row("payment-service declines"), _row(REFUTED)]
    hit = refutation.find_repeat(ANSWER, rows)
    assert hit["subject"] == REFUTED


def test_the_retry_prompt_does_not_hand_over_the_answer():
    """Nobody in this loop knows the cause. A correction that invents one is a
    leak wearing a helpful voice."""
    text = refutation.retry_prompt(_row(REFUTED, evidence="nothing shipped that window"))
    assert REFUTED in text
    assert "a person" in text
    assert "nothing shipped that window" in text
    assert "user-service" not in text


# ---- the wiring -------------------------------------------------------------


def _cfg(monkeypatch, tmp_path):
    p = tmp_path / "aiops.db"
    monkeypatch.setattr(store.settings, "store_path", str(p))
    monkeypatch.setattr(case_memory.settings, "case_memory_enabled", True)
    return p


def _seed(p, subject, kind="hypothesis"):
    with case_memory.case_scope(
        fp="fp1", alertname="OrderAuthFailureRateHigh", service="order-service"
    ) as sc:
        case_memory.observe(sc, path=p)
        store.ruled_out_insert(
            key=sc.case_key,
            run_id="manual",
            ts=store.datetime.now(store.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            kind=kind,
            subject=subject,
            evidence="",
            disproved_by="human",
            path=p,
        )
        return sc


def test_check_is_a_noop_outside_a_scope(monkeypatch, tmp_path):
    _cfg(monkeypatch, tmp_path)
    assert agent._refutation_check(ANSWER) == (True, "")


def test_check_sends_the_answer_back_inside_a_scope(monkeypatch, tmp_path):
    p = _cfg(monkeypatch, tmp_path)
    with case_memory.case_scope(
        fp="fp1", alertname="OrderAuthFailureRateHigh", service="order-service"
    ) as sc:
        case_memory.observe(sc, path=p)
        store.ruled_out_insert(
            key=sc.case_key,
            run_id="manual",
            ts=store.datetime.now(store.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            kind="hypothesis",
            subject=REFUTED,
            evidence="",
            disproved_by="human",
            path=p,
        )
        ok, prompt = agent._refutation_check(ANSWER)
        assert ok is False
        assert REFUTED in prompt

        ok2, _ = agent._refutation_check(OTHER_ANSWER)
        assert ok2 is True


def test_a_refuted_hypothesis_is_no_longer_put_in_front_of_the_model(monkeypatch, tmp_path):
    """The whole point of the move. What the model sees must not name the
    branch — that is what made it take it."""
    p = _cfg(monkeypatch, tmp_path)
    sc = _seed(p, REFUTED)
    block = agent._past_incident_context("order-service", "OrderAuthFailureRateHigh")
    assert REFUTED not in block
    assert block == ""  # nothing else to say about this case yet

    # …while the half a machine can enforce is still shown.
    _seed(p, "LogQL stream selector on service", kind="query")
    block2 = agent._past_incident_context("order-service", "OrderAuthFailureRateHigh")
    assert "LogQL stream selector" in block2
    assert REFUTED not in block2
    assert sc.case_key


def test_the_check_is_off_in_the_control_arm(monkeypatch, tmp_path):
    """`case_recall_enabled` selects whether a run uses the case library at all,
    and this check is now the library's main way of reaching an answer. Without
    the gate an A/B compares two identical arms."""
    p = _cfg(monkeypatch, tmp_path)
    with case_memory.case_scope(
        fp="fp1", alertname="OrderAuthFailureRateHigh", service="order-service"
    ) as sc:
        case_memory.observe(sc, path=p)
        store.ruled_out_insert(
            key=sc.case_key,
            run_id="manual",
            ts=store.datetime.now(store.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            kind="hypothesis",
            subject=REFUTED,
            evidence="",
            disproved_by="human",
            path=p,
        )
        monkeypatch.setattr(agent.settings, "case_recall_enabled", True)
        assert agent._refutation_check(ANSWER)[0] is False
        monkeypatch.setattr(agent.settings, "case_recall_enabled", False)
        assert agent._refutation_check(ANSWER) == (True, "")
