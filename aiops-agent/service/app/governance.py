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
  - confidence ≥ high AND reversible AND not approval-gated AND calibration
        is proven-good                → AUTO
        calibration unproven/poor     → downgraded to PROPOSE

"Calibration proven-good" = enough labeled runs exist AND measured
overconfidence is within tolerance. With no calibration evidence the gate refuses
AUTO by default — in uncertainty it degrades to a human, which ARE names the
highest sign of maturity.

This module returns a *decision*; it never executes. Execution is the registry's
job and is separately kill-switched (actions.py). The two gates are independent
on purpose: "should we" vs "can we".
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from .actions import ActionSpec, registry
from .config import settings


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
    return True, f"calibration ok (overconfidence {overconf:+}, {labeled} runs)"


def decide(
    action: ActionSpec, confidence: float, calib: dict, dq: dict | None = None, *, path=None
) -> Decision:
    """Policy verdict for one proposed action given the run confidence, the
    current calibration state, and (optionally) the data-quality verdict. When a
    DQ verdict is supplied, AUTO additionally requires it to be proven-good —
    autonomy is withheld on a signal model that has drifted or gone stale."""
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

        human_labeled = store.cal_count_by_source(exclude_sources=_SELF_LABEL_SOURCES, path=path)
    good, cal_note = _calibration_verdict(calib, human_labeled=human_labeled)
    dq_note = dq.get("note", "") if dq else "DQ not evaluated"

    def mk(level: Autonomy, reason: str) -> Decision:
        return Decision(
            action=action.name,
            autonomy=level,
            requires_human=(level is not Autonomy.AUTO),
            confidence=confidence,
            reason=reason,
            calibration_note=cal_note,
            dq_note=dq_note,
            reversible=action.reversible,
            requires_approval=action.requires_approval,
        )

    # Hard safety rules first — independent of confidence/calibration.
    if not action.reversible:
        return mk(Autonomy.ESCALATE, "action is irreversible — never autonomous")

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
    return mk(Autonomy.AUTO, "high confidence, reversible, calibration + data-quality proven-good")


def propose_remediations(
    remediation_actions: list[str], confidence: float, calib: dict, dq: dict | None = None
) -> list[Decision]:
    """Map a runbook's remediation step action names to registered actions and run
    each through the gate. Unregistered names are skipped (only the typed,
    whitelisted vocabulary is eligible)."""
    out: list[Decision] = []
    for name in remediation_actions:
        spec = registry.get(name)
        if spec is None:
            continue
        out.append(decide(spec, confidence, calib, dq))
    return out


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
        lines.append(f"  - {d.reason}; {d.calibration_note}; DQ: {d.dq_note}")
    return "\n".join(lines)
