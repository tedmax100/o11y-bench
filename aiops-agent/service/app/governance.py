"""Governance plane — the runtime policy that decides, per proposed action,
whether the agent may act autonomously, must ask a human, or must escalate.
(ARE §三 Governance plane / §四.2 calibration; v3 §5.2 approval gate.)

ARE's central safety idea is here: autonomy is earned and *revocable*. A high
stated confidence is necessary but not sufficient — the agent's history must show
that its confidence has actually tracked reality (low calibration error). So this
gate reads both the run's confidence AND the measured calibration (from the CE
harness) and **narrows autonomy when calibration is poor or unproven**:

  - irreversible action            → ESCALATE (never autonomous, full stop)
  - requires_approval              → at most PROPOSE (never AUTO)
  - confidence < low               → ESCALATE
  - low ≤ confidence < high        → PROPOSE (human confirms)
  - runbook's own rollback failed  → ESCALATE (its undo is known broken)
  - confidence ≥ high AND reversible AND not approval-gated AND calibration
        is proven-good                → AUTO
        calibration unproven/poor     → downgraded to PROPOSE
        runbook's record unproven     → downgraded to PROPOSE

"Calibration proven-good" = enough labeled runs exist AND the reliability curve
holds up under four separate readings: mean overconfidence within tolerance, the
worst adequately-populated bin within tolerance, enough labeled runs in the
confidence band where AUTO is actually granted, and enough accuracy in that band.
The mean alone is deliberately not sufficient — it is a signed average, and an
underconfident half plus an overconfident half sum to a number that looks
perfect while the agent is wrong in both directions. With no calibration
evidence the gate refuses AUTO by default — in uncertainty it degrades to a
human, which ARE names the highest sign of maturity.

This module returns a *decision*; it never executes. Execution is the registry's
job and is separately kill-switched (actions.py). The two gates are independent
on purpose: "should we" vs "can we".
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .actions import ActionSpec, registry
from .calibration import bin_evidence
from .config import settings

logger = logging.getLogger("aiops_agent.governance")


class Autonomy(StrEnum):
    AUTO = "auto"  # policy permits autonomous execution (still subject to the kill switch)
    PROPOSE = "propose"  # surface to a human to confirm
    ESCALATE = "escalate"  # hand back to a human; do not even pre-fill an action


class Decision(BaseModel):
    action: str
    autonomy: Autonomy
    requires_human: bool
    confidence: float
    reason: str
    calibration_note: str
    dq_note: str = ""
    act_note: str = ""
    rb_note: str = ""
    ev_note: str = ""
    reversible: bool
    requires_approval: bool


_SELF_LABEL_SOURCES = ("remediation-verified", "remediation-failed")


def _calibration_verdict(calib: dict, *, human_labeled: int | None = None) -> tuple[bool, str]:
    """(proven_good, note). Good = enough labeled runs (with at least
    governance_min_human_labeled_runs from human/grader, not self-produced)
    AND overconfidence within tolerance. Autonomy is earned with external evidence;
    self-produced remediation labels alone cannot unlock AUTO (§6.2 constraint 1).

    `human_labeled` is the pre-fetched count of non-self-label records. When None
    the check is skipped — callers that don't care about the separation (tests,
    propose-only paths) omit it; callers that must enforce it (AUTO evaluation)
    pass it explicitly."""
    labeled = calib.get("labeled") or 0
    if labeled < settings.governance_min_labeled_runs:
        return False, (
            f"calibration unproven ({labeled} labeled run(s) < "
            f"{settings.governance_min_labeled_runs}); autonomy withheld"
        )

    # Require the minimum human/grader label count when the caller supplies it.
    if human_labeled is not None and human_labeled < settings.governance_min_human_labeled_runs:
        return False, (
            f"insufficient human/grader labels ({human_labeled} < "
            f"{settings.governance_min_human_labeled_runs}); "
            "self-produced labels cannot unlock AUTO"
        )
    overconf = calib.get("overconfidence")
    if overconf is None:
        return False, "calibration unavailable; autonomy withheld"
    if overconf > settings.governance_max_overconfidence:
        return False, (
            f"overconfident by {overconf:+} > "
            f"{settings.governance_max_overconfidence}; autonomy narrowed"
        )

    # The mean cleared. That is necessary and nowhere near sufficient: it is a
    # signed average, so an underconfident half and an overconfident half sum to
    # a passing number while the agent is wrong in both directions. The bins
    # were always computed and never read — read them.
    ev = bin_evidence(
        calib,
        min_bin_count=settings.governance_min_bin_count,
        band_lo=settings.governance_conf_high,
    )
    if not ev["available"]:
        return False, "no reliability curve to read; autonomy withheld"

    skipped = (
        f", {ev['thin_bins']} bin(s)/{ev['thin_runs']} run(s) too thin to count"
        if ev["thin_bins"]
        else ""
    )

    # The band check comes first: it asks about the exact region where AUTO is
    # granted, so failing it makes the rest of the curve irrelevant.
    if ev["band_n"] < settings.governance_min_bin_count:
        return False, (
            f"only {ev['band_n']} labeled run(s) at confidence ≥ "
            f"{settings.governance_conf_high} (need "
            f"{settings.governance_min_bin_count}); no evidence in the band where "
            f"AUTO is granted{skipped}"
        )
    if ev["band_accuracy"] < settings.governance_min_band_accuracy:
        return False, (
            f"accuracy {ev['band_accuracy']} at confidence ≥ "
            f"{settings.governance_conf_high} (n={ev['band_n']}) < "
            f"{settings.governance_min_band_accuracy}; autonomy withheld in the "
            f"band it would be exercised in{skipped}"
        )
    if ev["max_gap"] is not None and ev["max_gap"] > settings.governance_max_bin_gap:
        return False, (
            f"worst bin {ev['max_gap_bin']} is off by {ev['max_gap']} > "
            f"{settings.governance_max_bin_gap} (mean overconfidence {overconf:+} "
            f"hid it); autonomy narrowed{skipped}"
        )
    return True, (
        f"calibration ok (overconfidence {overconf:+}, worst bin {ev['max_gap']}, "
        f"accuracy {ev['band_accuracy']} on {ev['band_n']} run(s) in the decision "
        f"band, {labeled} labeled){skipped}"
    )


def _within_fixture_window(records: list) -> tuple[list, int | None]:
    """(records inside the freshness window, age in days of the newest label).

    Returns an empty list when the newest label is already outside the window,
    so the caller fails closed rather than computing a curve over nothing.
    """
    max_age = settings.governance_fixture_max_age_days
    now = datetime.now(UTC)
    ages: list[tuple[int, Any]] = []
    for r in records:
        if r.correct is None or not r.ts:
            continue
        try:
            ts = datetime.strptime(r.ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:  # an unparseable stamp is not evidence of freshness
            continue
        ages.append(((now - ts).days, r))
    if not ages:
        return [], None
    newest = min(a for a, _ in ages)
    if newest > max_age:
        return [], newest
    return [r for a, r in ages if a <= max_age], newest


def regression_verdict(path=None) -> dict:
    """The gate's view of the fixture record, in the same `proven_good` shape as
    the DQ, actuation and runbook-health verdicts.

    This is deliberately *not* merged into the production calibration curve. The
    two answer different questions with evidence of different worth:

    - production labels: is this agent right about live incidents, judged by a
      person who read the transcript;
    - the harness: has it regressed on questions whose answers we already know,
      judged mechanically against a truth file on a baked stack. That record is
      a committed file, not the harness's working database — see
      app/eval/record.py for why the difference matters.

    Merging them would let dozens of grader labels vouch for a write to a live
    cluster, and the clock probe already measured what that vouching is worth —
    one fixture went 100% to 0% between two boots with untouched code, so a
    fixture pass is evidence about the fixture. Kept apart, the harness answers
    the one question it is good at, and AUTO requires both to clear the same
    bar: the standard is identical, the bodies of evidence are not.

    A missing or unreadable store is "no record", which earns no autonomy —
    the same position taken on a runbook nobody has run. So is a record that has
    gone stale: labels age out of the window, and a store whose newest label
    falls outside it stops counting entirely rather than quietly vouching for
    code it never ran against.
    """
    if not settings.governance_regression_gate_enabled:
        return {"proven_good": True, "note": "regression gate disabled"}
    record_path = path or settings.fixture_record_path
    if not record_path or not Path(record_path).exists():
        return {
            "proven_good": False,
            "note": "no fixture record to read; autonomy withheld",
        }
    try:
        from .calibration import compute_calibration
        from .eval.record import load as load_fixture_record

        modes = tuple(settings.governance_calibration_modes)
        records = load_fixture_record(record_path)
        fresh, newest_age = _within_fixture_window(records)
        if not fresh:
            age_note = (
                f"newest label {newest_age}d old (> {settings.governance_fixture_max_age_days}d)"
                if newest_age is not None
                else "no labels with a readable timestamp"
            )
            return {
                "proven_good": False,
                "note": f"fixtures: {age_note}; the record is about older code",
                "labeled": 0,
                "newest_age_days": newest_age,
            }
        calib = compute_calibration(fresh, modes=modes)
        # Count the floor over the same rows the curve is computed over — in
        # window, same modes — for the reason cal_count_by_source states: a
        # floor counted on a wider set than the curve is not a floor.
        graded = sum(
            1
            for r in fresh
            if r.correct is not None
            and r.source not in _SELF_LABEL_SOURCES
            and (r.grading_mode in modes if modes else True)
        )
        good, note = _calibration_verdict(calib, human_labeled=graded)
        return {
            "proven_good": good,
            "note": f"fixtures: {note} (newest label {newest_age}d ago)",
            "labeled": calib.get("labeled") or 0,
            "overconfidence": calib.get("overconfidence"),
            "newest_age_days": newest_age,
        }
    except Exception as e:  # a gate that crashes must not read as a pass
        logger.warning("regression verdict unavailable: %s", e)
        return {"proven_good": False, "note": f"fixture record unreadable: {e}"}


def decide(
    action: ActionSpec,
    confidence: float,
    calib: dict,
    dq: dict | None = None,
    act: dict | None = None,
    rb: dict | None = None,
    rejected: dict | None = None,
    ev: dict | None = None,
    *,
    path=None,
) -> Decision:
    """Policy verdict for one proposed action given the run confidence, the
    current calibration state, and (optionally) the data-quality, actuation and
    runbook-health verdicts. When supplied, AUTO additionally requires each to be
    proven-good — autonomy is withheld on a signal model that has drifted or gone
    stale, on a write credential that can no longer be shown to work, and on a
    procedure whose own record says it does not fix this.

    The five gates answer five different questions and none substitutes for
    another: *should we* (calibration), *is the map real* (DQ), *can we still
    act* (actuation), *does this procedure still work* (runbook health), *has it
    regressed on what we already know the answer to* (the fixture record).

    The last two are both "a record", and they are still not the same question:
    runbook health is about one procedure, the fixture record is about the
    agent's judgement. Nor is the fixture record a substitute for the
    calibration curve — see `regression_verdict` for why they are counted
    separately."""
    # For AUTO evaluation enforce the human-label minimum (§6.2 constraint 1).
    # Fetch lazily — only when confidence is high enough to reach the AUTO gate.
    # `path=None` means use settings.store_path; tests that don't wire a store
    # should set governance_min_human_labeled_runs=0 to bypass the DB hit.
    human_labeled: int | None = None
    if (
        confidence >= settings.governance_conf_high
        and settings.governance_min_human_labeled_runs > 0
    ):
        from . import store

        # Count over the same rows the curve is computed over — a floor counted
        # on a wider set than the curve is not a floor.
        human_labeled = store.cal_count_by_source(
            exclude_sources=_SELF_LABEL_SOURCES,
            modes=tuple(settings.governance_calibration_modes),
            exclude_drills=True,
            path=path,
        )
    good, cal_note = _calibration_verdict(calib, human_labeled=human_labeled)
    dq_note = dq.get("note", "") if dq else "DQ not evaluated"
    act_note = act.get("note", "") if act else "actuation readiness not evaluated"
    rb_note = rb.get("note", "") if rb else "runbook health not evaluated"
    ev_note = ev.get("note", "") if ev else "fixture record not evaluated"

    def mk(level: Autonomy, reason: str) -> Decision:
        return Decision(
            action=action.name,
            autonomy=level,
            requires_human=(level is not Autonomy.AUTO),
            confidence=confidence,
            reason=reason,
            calibration_note=cal_note,
            dq_note=dq_note,
            act_note=act_note,
            rb_note=rb_note,
            ev_note=ev_note,
            reversible=action.reversible,
            requires_approval=action.requires_approval,
        )

    # Hard safety rules first — independent of confidence/calibration.
    if not action.reversible:
        return mk(Autonomy.ESCALATE, "action is irreversible — never autonomous")

    # A runbook whose rollback has failed is not a confidence problem. The
    # reason a reversible action is allowed at all is that it can be undone, and
    # this one's record says the undo was tried and did not work — so it stops
    # being reversible in the only sense that matters and goes to a person.
    if rb is not None and rb.get("status") == "suspended":
        return mk(Autonomy.ESCALATE, f"runbook suspended — {rb.get('note', '')}")

    # A person already declined this exact action on this incident. Proposing it
    # again is not a decision the agent gets to make twice: at PROPOSE it makes
    # someone type the same refusal a second time, and the second refusal
    # carries less information than the first — it is about our persistence, not
    # about the action. ESCALATE creates no proposal, and the reason carries
    # their words so the escalation is not a mystery.
    if rejected:
        why = (rejected.get("evidence") or "").strip()
        return mk(
            Autonomy.ESCALATE,
            f"a person declined this on {(rejected.get('ts') or '')[:10]}"
            + (f": {why}" if why else " without giving a reason"),
        )

    if confidence < settings.governance_conf_low:
        return mk(
            Autonomy.ESCALATE,
            f"confidence {confidence} below low threshold {settings.governance_conf_low}",
        )
    if confidence < settings.governance_conf_high:
        return mk(Autonomy.PROPOSE, f"confidence {confidence} in the propose band")

    # confidence >= high — AUTO must clear every earned-autonomy gate.
    if action.requires_approval:
        return mk(Autonomy.PROPOSE, "high confidence but action is approval-gated")
    if not good:
        return mk(Autonomy.PROPOSE, "high confidence but calibration not proven-good")
    if dq is not None and not dq.get("proven_good"):
        return mk(Autonomy.PROPOSE, "high confidence but data-quality (DQ) not proven-good")
    if act is not None and not act.get("proven_good"):
        return mk(Autonomy.PROPOSE, "high confidence but actuation readiness not proven-good")
    if rb is not None and not rb.get("proven_good"):
        return mk(Autonomy.PROPOSE, f"high confidence but {rb.get('note', 'runbook unproven')}")
    if ev is not None and not ev.get("proven_good"):
        return mk(
            Autonomy.PROPOSE, f"high confidence but {ev.get('note', 'fixture record unproven')}"
        )
    return mk(
        Autonomy.AUTO,
        "high confidence, reversible, calibration + data-quality + actuation + "
        "runbook health + fixture record proven-good",
    )


def autonomy_status(path=None) -> dict:
    """Where AUTO stands right now, gate by gate, with the distance left to go.

    `decide()` answers this for one action at the moment it is proposed, which
    is the only moment it matters and the worst moment to find out. The
    operational question — "what would have to change for this to act on its
    own" — had no answer short of reading the code and querying SQLite by hand,
    and a bar nobody can see is a bar nobody works towards.

    Same functions as the live gate, not a second implementation: whatever this
    reports is what the next high-confidence proposal will be told. Runbook
    health is absent on purpose — it is a verdict about one procedure, so there
    is no global value to report; it is evaluated per runbook at decision time.

    `blockers` is the list a person can act on. Empty does not mean the agent
    will act: the kill switch (`actions_enabled`) is a separate question, and it
    is reported alongside rather than folded in, because "policy would allow it"
    and "this deployment permits it" fail in ways that look identical from
    outside.
    """
    from . import store
    from .calibration import bin_evidence, compute_calibration, load_records, production_records
    from .signals.actuation import actuation_verdict
    from .signals.dq import dq_verdict

    # Drills out, on both halves. The curve and the floor must be computed over
    # the same rows or the floor is not a floor.
    calib = compute_calibration(
        production_records(load_records(path)), modes=tuple(settings.governance_calibration_modes)
    )
    human_labeled = store.cal_count_by_source(
        exclude_sources=_SELF_LABEL_SOURCES,
        modes=tuple(settings.governance_calibration_modes),
        exclude_drills=True,
        path=path,
    )
    cal_good, cal_note = _calibration_verdict(calib, human_labeled=human_labeled)
    ev = bin_evidence(
        calib,
        min_bin_count=settings.governance_min_bin_count,
        band_lo=settings.governance_conf_high,
    )

    gates = [
        {"gate": "calibration", "proven_good": cal_good, "note": cal_note},
        {"gate": "data_quality", **_verdict_fields(dq_verdict())},
        {"gate": "actuation", **_verdict_fields(actuation_verdict())},
        {"gate": "fixture_record", **_verdict_fields(regression_verdict(path))},
    ]
    return {
        "granted": all(g["proven_good"] for g in gates),
        "actions_enabled": settings.actions_enabled,
        "gates": gates,
        "blockers": [g for g in gates if not g["proven_good"]],
        # The numbers behind the calibration gate, so a UI can render "17 of 20"
        # rather than re-parsing the sentence.
        "calibration": {
            "labeled": calib.get("labeled") or 0,
            "labeled_required": settings.governance_min_labeled_runs,
            "human_labeled": human_labeled,
            "human_labeled_required": settings.governance_min_human_labeled_runs,
            "band_lo": settings.governance_conf_high,
            "band_n": ev.get("band_n"),
            "band_n_required": settings.governance_min_bin_count,
            "band_accuracy": ev.get("band_accuracy"),
            "band_accuracy_required": settings.governance_min_band_accuracy,
            "overconfidence": calib.get("overconfidence"),
            "overconfidence_max": settings.governance_max_overconfidence,
            "worst_bin_gap": ev.get("max_gap"),
            "worst_bin_gap_max": settings.governance_max_bin_gap,
        },
    }


def _verdict_fields(v: dict) -> dict:
    return {"proven_good": bool(v.get("proven_good")), "note": v.get("note", "")}


def propose_remediations(
    remediation_actions: list[str],
    confidence: float,
    calib: dict,
    dq: dict | None = None,
    act: dict | None = None,
    rb: dict | None = None,
    rejected: dict[str, dict] | None = None,
    ev: dict | None = None,
) -> list[Decision]:
    """Map a runbook's remediation step action names to registered actions and run
    each through the gate. Unregistered names are skipped (only the typed,
    whitelisted vocabulary is eligible).

    `rejected` maps an action name to the refusal a person already wrote on this
    incident, if any."""
    out: list[Decision] = []
    for name in remediation_actions:
        spec = registry.get(name)
        if spec is None:
            continue
        out.append(decide(spec, confidence, calib, dq, act, rb, (rejected or {}).get(name), ev))
    return out


def runbook_health_verdict(runbook_id: str, path=None) -> dict:
    """The gate's view of a runbook's record: a `proven_good` verdict in the
    same shape as the DQ and actuation ones, so `decide` treats all four alike.

    A runbook nobody has executed enough times is *not* proven good. That reads
    harsh for a freshly written procedure, and it is the same position this
    system takes everywhere else: autonomy is earned against a record, and a
    procedure with no record has not earned any. The cost of being wrong here is
    a proposal a human reads.
    """
    from . import store

    try:
        h = store.rb_health(runbook_id, path=path)
    except Exception as e:  # a missing record must not sink the gate
        logger.warning("runbook health lookup failed for %s: %s", runbook_id, e)
        return {"proven_good": False, "status": "unknown", "note": "runbook health unavailable"}
    return {
        "proven_good": h["status"] == store.RB_HEALTHY,
        "status": h["status"],
        "note": h["note"],
    }


def format_decisions(decisions: list[Decision]) -> str:
    if not decisions:
        return ""
    enabled = settings.actions_enabled
    lines = ["## Remediation governance decisions"]
    for d in decisions:
        verb = {
            Autonomy.AUTO: "AUTO (policy permits autonomous execution)"
            + ("" if enabled else " — but execution kill-switch is OFF, so PROPOSE"),
            Autonomy.PROPOSE: "PROPOSE (needs human confirmation)",
            Autonomy.ESCALATE: "ESCALATE (hand to human)",
        }[d.autonomy]
        lines.append(f"- `{d.action}` → {verb}")
        lines.append(f"  - {d.reason}; {d.calibration_note}; DQ: {d.dq_note}; act: {d.act_note}")
    return "\n".join(lines)
