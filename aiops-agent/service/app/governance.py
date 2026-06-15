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

from enum import Enum

from pydantic import BaseModel, Field

from .actions import ActionSpec, registry
from .config import settings


class Autonomy(str, Enum):
    AUTO = "auto"        # policy permits autonomous execution (still subject to the kill switch)
    PROPOSE = "propose"  # surface to a human to confirm
    ESCALATE = "escalate"  # hand back to a human; do not even pre-fill an action


class Decision(BaseModel):
    action: str
    autonomy: Autonomy
    requires_human: bool
    confidence: float
    reason: str
    calibration_note: str
    reversible: bool
    requires_approval: bool


def _calibration_verdict(calib: dict) -> tuple[bool, str]:
    """(proven_good, note). Good = enough labeled runs and overconfidence within
    tolerance. Unproven (too few labels) is treated as NOT good — autonomy must
    be earned with evidence."""
    labeled = calib.get("labeled") or 0
    if labeled < settings.governance_min_labeled_runs:
        return False, (f"calibration unproven ({labeled} labeled run(s) < "
                       f"{settings.governance_min_labeled_runs}); autonomy withheld")
    overconf = calib.get("overconfidence")
    if overconf is None:
        return False, "calibration unavailable; autonomy withheld"
    if overconf > settings.governance_max_overconfidence:
        return False, (f"overconfident by {overconf:+} > "
                       f"{settings.governance_max_overconfidence}; autonomy narrowed")
    return True, f"calibration ok (overconfidence {overconf:+}, {labeled} runs)"


def decide(action: ActionSpec, confidence: float, calib: dict) -> Decision:
    """Policy verdict for one proposed action given the run confidence and the
    current calibration state."""
    good, cal_note = _calibration_verdict(calib)

    def mk(level: Autonomy, reason: str) -> Decision:
        return Decision(
            action=action.name, autonomy=level,
            requires_human=(level is not Autonomy.AUTO),
            confidence=confidence, reason=reason, calibration_note=cal_note,
            reversible=action.reversible, requires_approval=action.requires_approval)

    # Hard safety rules first — independent of confidence/calibration.
    if not action.reversible:
        return mk(Autonomy.ESCALATE, "action is irreversible — never autonomous")

    if confidence < settings.governance_conf_low:
        return mk(Autonomy.ESCALATE, f"confidence {confidence} below low threshold "
                                     f"{settings.governance_conf_low}")
    if confidence < settings.governance_conf_high:
        return mk(Autonomy.PROPOSE, f"confidence {confidence} in the propose band")

    # confidence >= high
    if action.requires_approval:
        return mk(Autonomy.PROPOSE, "high confidence but action is approval-gated")
    if not good:
        return mk(Autonomy.PROPOSE, "high confidence but calibration not proven-good")
    return mk(Autonomy.AUTO, "high confidence, reversible, calibration proven-good")


def propose_remediations(remediation_actions: list[str], confidence: float, calib: dict) -> list[Decision]:
    """Map a runbook's remediation step action names to registered actions and run
    each through the gate. Unregistered names are skipped (only the typed,
    whitelisted vocabulary is eligible)."""
    out: list[Decision] = []
    for name in remediation_actions:
        spec = registry.get(name)
        if spec is None:
            continue
        out.append(decide(spec, confidence, calib))
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
        lines.append(f"  - {d.reason}; {d.calibration_note}")
    return "\n".join(lines)
