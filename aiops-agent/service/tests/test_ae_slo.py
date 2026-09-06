"""AE-SLO grading: the human verdict on an executed action, and the two counting
rules that were previously enforced by remembering to enforce them."""

import app.store as store


def _put(p, rid, **kw):
    base = dict(request_id=rid, resolved=True, actor="oncall", verify_said=True, path=p)
    base.update(kw)
    store.action_outcome_put(**base)


def test_no_percentage_below_the_reporting_floor(tmp_path):
    """`0/1` rendered as `0.0%` reads like a measured failure rate. It is one
    anecdote, and it already got carried into a report that way once."""
    p = tmp_path / "s.db"
    _put(p, "r1", resolved=False, verify_said=False)
    slo = store.ae_slo(min_n=5, path=p)
    assert slo["incidents"]["raw"] == "0/1"
    assert slo["incidents"]["rate"] is None
    assert "below the reporting floor" in slo["incidents"]["note"]


def test_rate_appears_once_there_is_enough_evidence(tmp_path):
    p = tmp_path / "s.db"
    for i in range(4):
        _put(p, f"ok{i}")
    _put(p, "bad", resolved=False, verify_said=False)
    slo = store.ae_slo(min_n=5, path=p)
    assert slo["incidents"]["rate"] == 0.8


def test_drills_never_join_the_incident_ratio(tmp_path):
    """A rehearsal must not flatter the number that describes production."""
    p = tmp_path / "s.db"
    for i in range(5):
        _put(p, f"drill{i}", drill=True)
    _put(p, "real", resolved=False, verify_said=False)
    slo = store.ae_slo(min_n=5, path=p)
    assert slo["drills"]["raw"] == "5/5"
    assert slo["incidents"]["raw"] == "0/1"
    assert slo["incidents"]["rate"] is None  # still one anecdote


def test_a_side_effect_makes_an_action_ineffective(tmp_path):
    """ "It fixed the symptom and broke something else" is not a success — the
    SLO asks about effectiveness, not about whether the command returned 200."""
    p = tmp_path / "s.db"
    _put(p, "r1", resolved=True, side_effect=True)
    assert store.ae_slo(min_n=1, path=p)["incidents"]["rate"] == 0.0


def test_machine_and_human_disagreement_is_surfaced(tmp_path):
    """When verify says pass and the on-call says the incident is still open,
    that gap is the finding — it must not be averaged away."""
    p = tmp_path / "s.db"
    _put(p, "r1", resolved=False, verify_said=True)
    _put(p, "r2", resolved=True, verify_said=True)
    agreement = store.ae_slo(path=p)["verify_agreement"]
    assert agreement["graded"] == 2 and agreement["disagreed"] == 1
    assert "disagreed at least once" in agreement["note"]


def test_regrading_overwrites_rather_than_double_counts(tmp_path):
    p = tmp_path / "s.db"
    _put(p, "r1", resolved=True)
    _put(p, "r1", resolved=False, actor="oncall-2")
    rows = store.action_outcomes(path=p)
    assert len(rows) == 1 and rows[0]["resolved"] == 0 and rows[0]["actor"] == "oncall-2"


def test_drill_executions_are_marked_in_the_ledger(tmp_path):
    p = tmp_path / "s.db"
    store.exec_record(
        ts="2026-08-16T05:00:00Z",
        scope_key="k8s.rollout_undo|demo/payment-service",
        action="k8s.rollout_undo",
        target="demo/payment-service",
        fp="fp1",
        request_id="r1",
        success=True,
        drill=True,
        path=p,
    )
    with store._connect(p) as conn:
        assert conn.execute("SELECT drill FROM executions").fetchone()["drill"] == 1


def test_empty_state_does_not_claim_a_disagreement(tmp_path):
    """An SLO with no data must say it has no data, not report a failure."""
    note = store.ae_slo(path=tmp_path / "s.db")["verify_agreement"]["note"]
    assert "nothing to be compared against" in note


def _ran(p, rid, status="succeeded", drill=False, action="k8s.rollout_undo"):
    """An action that really touched the cluster, with no verdict on it yet."""
    store.ar_insert(
        {
            "request_id": rid,
            "fp": "fp1",
            "action": action,
            "args": {"namespace": "demo", "deployment": "payment-service"},
            "autonomy": "propose",
            "status": status,
            "reversible": True,
            "rollback": None,
            "blast_radius": None,
            "runbook_id": "rb1",
            "params": {"drill": "true"} if drill else {},
            "idem_key": f"k|{rid}",
            "created_ts": "2026-08-30T05:00:00Z",
            "expires_ts": "2026-08-30T05:30:00Z",
            "actor": "operator",
            "outcome": "",
        },
        path=p,
    )


def test_ungraded_executions_have_an_address(tmp_path):
    """Six of nine executed actions had no verdict and no list they appeared on,
    so they read as absent rather than as open questions."""
    p = tmp_path / "s.db"
    _ran(p, "r1")
    _ran(p, "r2", status="rolled_back")
    _ran(p, "graded")
    _put(p, "graded", resolved=True)
    pending = {r["request_id"] for r in store.ungraded_actions(path=p)}
    assert pending == {"r1", "r2"}


def test_a_refusal_is_not_a_grading_backlog_item(tmp_path):
    """Nothing ran, so "did the fix work" has no referent — an expired proposal
    must not appear as something an on-call forgot to grade."""
    p = tmp_path / "s.db"
    _ran(p, "never", status="expired")
    _ran(p, "also-never", status="proposed")
    assert store.ungraded_actions(path=p) == []


def test_the_ratio_reports_how_much_of_what_ran_it_covers(tmp_path):
    """The bug this exists for: 3/3 graded effective, out of 9 executed, printed
    as a clean ratio with the other six nowhere on the page."""
    p = tmp_path / "s.db"
    for i in range(5):
        _ran(p, f"ok{i}")
        _put(p, f"ok{i}")
    for i in range(4):
        _ran(p, f"quiet{i}")
    inc = store.ae_slo(min_n=5, path=p)["incidents"]
    assert inc["rate"] == 1.0
    assert inc["coverage"] == {"ran": 9, "graded": 5, "ungraded": 4, "fraction": 0.556}
    assert "4 of 9 executed actions have no verdict" in inc["note"]


def test_an_ungraded_drill_does_not_dent_incident_coverage(tmp_path):
    """Coverage splits the same way the ratio does, or a forgotten rehearsal
    would read as a forgotten production execution."""
    p = tmp_path / "s.db"
    _ran(p, "real")
    _put(p, "real")
    _ran(p, "reh", drill=True)
    slo = store.ae_slo(path=p)
    assert slo["incidents"]["coverage"]["ungraded"] == 0
    assert slo["drills"]["coverage"] == {"ran": 1, "graded": 0, "ungraded": 1, "fraction": 0.0}


def test_expired_proposals_are_counted_outside_the_slo(tmp_path):
    """`expired` means nobody pressed anything before the TTL. That is a fact
    about the operators, so it is reported — but never inside the ratio that
    describes remediation."""
    p = tmp_path / "s.db"
    _ran(p, "done")
    for i in range(3):
        _ran(p, f"gone{i}", status="expired")
    _ran(p, "waiting", status="proposed")
    d = store.proposal_disposition(path=p)
    assert d["expired"] == 3 and d["open"] == 1
    assert d["expiry_rate"] == 0.75  # 3 of 4 decided, the open one is not decided yet
    assert "expired unanswered" in d["note"]
    assert store.ae_slo(path=p)["incidents"]["coverage"]["ran"] == 1


def test_disposition_on_an_empty_ledger_claims_nothing(tmp_path):
    d = store.proposal_disposition(path=tmp_path / "s.db")
    assert d["expiry_rate"] is None and "no proposal has reached" in d["note"]
