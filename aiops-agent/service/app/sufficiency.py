"""Deciding whether to keep investigating, without asking the model.

The headless loop already had a stopping rule: extract `Findings`, and if
`confidence` came back under the threshold, pivot to another hypothesis and go
again. The number driving that decision is one the model writes about its own
work, under three prompt rules it is asked to apply to itself (signal diversity,
refutation attempt, hypothesis convergence). It is the examinee holding the key
to the exam room.

That is not a hypothetical complaint. The calibration harness exists precisely
because the stated confidence and the measured accuracy do not track each other,
and the autonomy gate has been refusing AUTO for weeks on the grounds that the
calibration is unproven. Using the same number to decide when to stop looking is
the same bet placed one step earlier, where nobody is measuring it.

So this module computes the stopping condition from the run's own evidence
instead. Four checks, each of which can be recomputed from a stored run without
a model:

  1. something was actually observed
  2. more than one store said so
  3. the observations speak to more than one causal role
  4. the conclusion cites evidence at all

`confidence` survives, and governance still reads it, because it is what the
calibration curve is built from and throwing it away would break the one loop
that measures this agent against reality. What changes is that it no longer
decides anything on its own.

**Thresholds are two, and two is not arbitrary.** One store agreeing with itself
is not corroboration (two PromQL queries are one source, which is why
`independent_domains` counts stores rather than calls). One causal role is a
symptom without a mechanism, or a mechanism with nothing tying it to what
users saw. Going to three of each was tempting and would have made the gate
stricter, but neither the trace store's retention nor the change feed is
reliably there, so a three-of-three rule would fail on stack conditions rather
than on the quality of the investigation. It would then be quietly relaxed by
whoever hit it at 3am, which is worse than never having set it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .facts import DiagnosticFact, independent_domains, usable_facts

logger = logging.getLogger("aiops_agent.sufficiency")


@dataclass(frozen=True)
class Check:
    """One criterion, its verdict, and the measurement behind it. `detail` is
    written to be pasted into a pivot instruction or read by a person, so it
    names what is missing rather than restating the rule."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Verdict:
    sufficient: bool
    checks: list[Check] = field(default_factory=list)

    @property
    def gaps(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> str:
        marks = ", ".join(f"{c.name}={'ok' if c.passed else 'gap'}" for c in self.checks)
        return f"sufficient={self.sufficient} ({marks})"

    def as_dict(self) -> dict:
        """Persisted with the run so the decision can be re-read later. The
        checks go in whole: a bare boolean would tell a future reader that the
        run stopped, not what it was still missing when it did."""
        return {
            "sufficient": self.sufficient,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }


def _roles(facts: list[DiagnosticFact]) -> set[str]:
    """Causal roles the usable observations speak to. `context` is not a role;
    it is the absence of one, and counting it would let two reference lookups
    satisfy a rule about causal coverage."""
    return {f.role_hint for f in usable_facts(facts) if f.role_hint != "context"}


def evaluate_sufficiency(
    facts: list[DiagnosticFact],
    cited_evidence: list[str] | None = None,
    *,
    min_sources: int = 2,
    min_roles: int = 2,
) -> Verdict:
    """Is there enough here to stop looking? Pure and deterministic."""
    usable = usable_facts(facts)
    domains = sorted(independent_domains(facts))
    roles = sorted(_roles(facts))
    cited = [e for e in (cited_evidence or []) if str(e).strip()]

    checks = [
        Check(
            "observed",
            bool(usable),
            f"{len(usable)}/{len(facts)} tool results were usable as evidence"
            + ("" if usable else "; nothing was measured this run"),
        ),
        Check(
            "independent_sources",
            len(domains) >= min_sources,
            f"{len(domains)} independent source(s) {domains}"
            + ("" if len(domains) >= min_sources else f"; needs {min_sources}"),
        ),
        Check(
            "causal_roles",
            len(roles) >= min_roles,
            f"observations speak to {roles}"
            + ("" if len(roles) >= min_roles else f"; needs {min_roles} distinct roles"),
        ),
        Check(
            "conclusion_cites_evidence",
            bool(cited),
            f"{len(cited)} evidence item(s) cited in the conclusion"
            + ("" if cited else "; the conclusion cites nothing"),
        ),
    ]
    verdict = Verdict(sufficient=all(c.passed for c in checks), checks=checks)
    logger.info("sufficiency: %s", verdict.summary())
    return verdict


# Roles named the way an on-call engineer would ask for them, so the pivot
# instruction says "find what changed" rather than "add a trigger fact".
_ROLE_ASK = {
    "trigger": "what changed (a deploy, a rollout, a config or code diff)",
    "mechanism": "how it is failing (the metric, the wait, the saturated resource)",
    "impact": "what users or callers actually saw (error logs, failed requests)",
}

_SOURCE_ASK = {
    "runtime": "metrics or k8s state",
    "log": "logs",
    "trace": "traces",
    "change": "the deploy/commit history",
}


def pivot_instruction(verdict: Verdict, facts: list[DiagnosticFact]) -> str:
    """Turn the unmet checks into the next instruction.

    The old pivot told the model its confidence was too low and asked it to pick
    a different hypothesis, which is a request to guess again. This one names the
    hole: a run that only ever queried Prometheus is told to go and look at logs
    or traces, and one that never established what changed is told to do that.
    """
    if verdict.sufficient:
        return ""
    lines = ["The evidence for this conclusion is not yet sufficient. What is missing:"]
    for check in verdict.gaps:
        lines.append(f"- {check.name}: {check.detail}")

    have_domains = independent_domains(facts)
    missing_domains = [_SOURCE_ASK[d] for d in _SOURCE_ASK if d not in have_domains]
    have_roles = _roles(facts)
    missing_roles = [_ROLE_ASK[r] for r in _ROLE_ASK if r not in have_roles]

    lines.append("")
    if missing_domains:
        lines.append(
            "Query a store you have not used yet this incident: "
            + "; ".join(missing_domains[:3])
            + "."
        )
    if missing_roles:
        lines.append("Establish " + "; and ".join(missing_roles[:2]) + ".")
    lines.append(
        "Do NOT repeat a query that already came back empty — change the selector, "
        "the window, or the store. When you conclude, cite the concrete values you "
        "read, and if the evidence still is not there, say so plainly instead of "
        "narrowing the claim until it fits."
    )
    return "\n".join(lines)
