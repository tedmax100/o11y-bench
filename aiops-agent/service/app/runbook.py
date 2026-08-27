"""Runbook / SOP layer — Tier 0 (link) + Tier 1 (read-only diagnostics). v3 §5.

This is the bridge that moves the agent from "pure reasoning" toward an execution
plane *without crossing into side effects*. A runbook is a structured Action
Contract: a `trigger` that matches an alert, read-only `diagnostics` that
auto-verify the runbook's preconditions, and `remediation` steps that are
**rendered for a human but never executed here** — Tier 2 auto-remediation is the
action-registry's job (v3 §5.3 / step 7), deliberately not in this module.

Two tiers ship here:
  - **Tier 0 (link)**: match a runbook to a firing alert (by explicit
    `runbook_id` annotation, else by trigger), fill in the incident parameters,
    and render the steps as guidance injected into the headless RCA. Pure, zero
    side effects.
  - **Tier 1 (diagnostics)**: auto-run the `diagnostics` steps — but ONLY through
    the read-only tool map the caller passes in. A step naming a tool that isn't
    in that map (e.g. a remediation action) is structurally skipped, so Tier 1
    can never mutate cluster state. Each step's result + its `expect`/`check` is
    folded into findings so the agent reasons over *confirmed* preconditions.

Read-only enforcement is by construction: this module imports no write API and
dispatches only to the tools the caller hands it (the agent's all-read-only TOOLS).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .config import settings

logger = logging.getLogger("aiops_agent.runbook")

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class DiagnosticCheck(BaseModel):
    """Optional deterministic verdict on a step's result. `expect` (on the step)
    is the human-readable intent; this is the machine-checkable part."""

    nonempty: bool | None = None
    contains: str | None = None
    min_rows: int | None = None
    max_value: float | None = None  # for instant numeric queries (verify step)


class Condition(BaseModel):
    """When a remediation step applies. Every clause set here must hold (AND).

    The clauses read against the Tier 1 diagnostics that already ran, so the
    branch is decided by what the cluster answered, not by what the model wrote.
    `diagnostic` names a diagnostic step's `id`.

    Deliberately not expressive: no or/not, no expressions. A runbook branch a
    person cannot read at 3am is worse than no branch at all, and everything
    harder than this belongs in a second runbook.
    """

    diagnostic: str | None = Field(default=None, description="`id` of a diagnostic step.")
    status: str | list[str] | None = Field(
        default=None, description="Required status of that diagnostic: pass/fail/ran/error/skipped."
    )
    output_contains: str | None = None
    output_not_contains: str | None = None
    param_equals: dict[str, str] = Field(default_factory=dict)


class Step(BaseModel):
    id: str | None = Field(
        default=None, description="Referenced by a remediation step's `when.diagnostic`."
    )
    desc: str
    action: str = Field(description="A read-only tool name, e.g. query_prometheus.")
    args: dict[str, Any] = Field(default_factory=dict)
    expect: str | None = Field(default=None, description="Human-readable precondition.")
    check: DiagnosticCheck | None = None
    # remediation-only: which diagnosis this fix is for. Absent = unconditional.
    when: Condition | None = None
    # remediation-only metadata; informational at this tier (never executed here)
    reversible: bool | None = None
    requires_approval: bool | None = None
    # inverse-operation contract carried into the ActionRequest (step 7 §2.2).
    # {action, args} naming the action that undoes this one. Without it the
    # executor refuses to run the action (no rollback → not executable, 7b-4).
    rollback: dict[str, Any] | None = None
    # post-execution verify spec (7b-4): {action, args, check} — a read-only
    # query run after the settle window; check failure triggers auto-rollback.
    verify: dict[str, Any] | None = None


class Trigger(BaseModel):
    alertname: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class Runbook(BaseModel):
    id: str
    title: str | None = None
    trigger: Trigger = Field(default_factory=Trigger)
    diagnostics: list[Step] = Field(default_factory=list)
    remediation: list[Step] = Field(default_factory=list)


# ---- load / match ----------------------------------------------------------


def load_runbooks(directory: str | Path | None = None) -> list[Runbook]:
    d = Path(directory or settings.runbook_dir)
    if not d.exists():
        return []
    books: list[Runbook] = []
    for fp in sorted(d.glob("*.y*ml")):
        try:
            data = yaml.safe_load(fp.read_text(encoding="utf-8"))
            books.append(Runbook.model_validate(data))
        except Exception as e:  # a bad runbook must not break alert handling
            logger.warning("skipping runbook %s: %s", fp.name, e)
    return books


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm_alertname(name: str | None) -> str:
    """Alert names differ in case and separators across the tools that emit them
    (`PaymentDeclineRateHigh`, `payment-decline-rate-high`, `payment_decline_rate_high`
    are one alert). Compare on the letters and digits alone."""
    return _NON_ALNUM.sub("", (name or "").lower())


# Kept as the in-module name; `store.case_key()` imports the public one so the
# two places that decide "are these the same alert" cannot drift apart (the
# trace-id regex already drifted once, for exactly this reason).
_norm = norm_alertname


def _labels_match(book: Runbook, labels: dict) -> bool:
    """Trigger labels must be a subset of the alert's, and the trigger has to
    constrain something — an empty trigger must not match every alert."""
    if not (book.trigger.alertname or book.trigger.labels):
        return False
    return all((labels or {}).get(k) == v for k, v in book.trigger.labels.items())


def match_runbook(
    labels: dict, annotations: dict, books: list[Runbook] | None = None
) -> Runbook | None:
    """Resolve the runbook for an alert. Explicit `runbook_id` annotation wins
    (precise, v3 §8); otherwise match by trigger — alertname (if the runbook
    specifies one) plus trigger.labels being a subset of the alert's labels."""
    books = load_runbooks() if books is None else books
    rid = (annotations or {}).get("runbook_id")
    if rid:
        for b in books:
            if b.id == rid:
                return b
        logger.warning("alert names runbook_id=%s but no such runbook", rid)

    alertname = (labels or {}).get("alertname")
    for b in books:
        if b.trigger.alertname and b.trigger.alertname != alertname:
            continue
        if _labels_match(b, labels):
            return b

    # Nothing matched exactly. `PaymentDeclineRateHigh` and
    # `payment-decline-rate-high` are the same alert to everyone except this
    # comparison, and a miss here silently costs the whole downstream chain:
    # no diagnostics, no remediation proposal, no action request. Fall back to a
    # normalized comparison and say loudly that it happened, so the mismatch gets
    # fixed at the source instead of being papered over forever.
    for b in books:
        if not b.trigger.alertname or _norm(b.trigger.alertname) != _norm(alertname):
            continue
        if _labels_match(b, labels):
            logger.warning(
                "runbook %s matched alertname %r only after normalization (trigger says %r) "
                "— align the alert rule or the runbook trigger",
                b.id,
                alertname,
                b.trigger.alertname,
            )
            return b

    near = [
        b.id
        for b in books
        if b.trigger.alertname and _norm(b.trigger.alertname) == _norm(alertname)
    ]
    if near:
        logger.warning(
            "no runbook for alertname=%r; %s has the same name but its trigger labels "
            "do not match the alert's",
            alertname,
            near,
        )
    return None


# ---- parameter substitution ------------------------------------------------


def incident_params(labels: dict, annotations: dict) -> dict[str, str]:
    """The substitution context for `{...}` placeholders in runbook steps."""
    params = {k: str(v) for k, v in (labels or {}).items()}
    params.update({k: str(v) for k, v in (annotations or {}).items()})
    # common alias so runbooks can write {service_name} regardless of which label carried it
    if "service_name" not in params and params.get("service"):
        params["service_name"] = params["service"]
    return params


def _subst(value: Any, params: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: params.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _subst(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, params) for v in value]
    return value


def _unresolved(value: Any) -> list[str]:
    return _PLACEHOLDER.findall(json.dumps(value, default=str))


# ---- Tier 0: render --------------------------------------------------------


def render_runbook(rb: Runbook, params: dict[str, str]) -> str:
    """Markdown guidance with incident parameters filled in. Remediation steps
    are shown but flagged as human-approval-only (not executed at this tier)."""
    lines = [f"## Runbook: {rb.id}" + (f" — {rb.title}" if rb.title else "")]
    if rb.diagnostics:
        lines.append("\n**Diagnostics (read-only — auto-verifiable preconditions):**")
        for i, s in enumerate(rb.diagnostics, 1):
            args = _subst(s.args, params)
            lines.append(
                f"{i}. {s.desc} — `{s.action}({json.dumps(args, separators=(',', ':'))})`"
                + (f"  _expect: {s.expect}_" if s.expect else "")
            )
    if rb.remediation:
        lines.append("\n**Remediation (requires human approval — NOT auto-executed):**")
        for i, s in enumerate(rb.remediation, 1):
            flags = []
            if s.reversible is not None:
                flags.append("reversible" if s.reversible else "IRREVERSIBLE")
            if s.requires_approval:
                flags.append("approval required")
            if s.when is not None:
                # Rendered before the diagnostics have been folded in, so this
                # says "there is a branch here", not which way it went.
                flags.append("conditional — only for the matching diagnosis")
            tag = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"{i}. {s.desc} — `{s.action}`{tag}")
    return "\n".join(lines)


# ---- Tier 1: read-only diagnostics runner ----------------------------------


class DiagnosticResult(BaseModel):
    id: str | None = None
    desc: str
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: str  # "pass" | "fail" | "ran" | "skipped" | "error"
    expect: str | None = None
    detail: str = ""
    output_preview: str = ""
    # The untruncated result, for `when` clauses to read. `output_preview` is
    # what a human sees; matching a branch condition against a 500-char cut of
    # someone else's JSON is how a branch silently stops firing.
    output_text: str = ""


def _evaluate_check(check: DiagnosticCheck | None, output: Any) -> tuple[str, str]:
    """(status, detail). 'ran' when there's no deterministic check to apply."""
    if check is None:
        return "ran", ""
    text = output if isinstance(output, str) else json.dumps(output, default=str)
    rows = (
        output
        if isinstance(output, list)
        else (
            output.get("data")
            if isinstance(output, dict) and isinstance(output.get("data"), list)
            else None
        )
    )
    if check.contains is not None:
        ok = check.contains in text
        return ("pass" if ok else "fail"), f"contains {check.contains!r}: {ok}"
    if check.min_rows is not None:
        n = len(rows) if rows is not None else 0
        return ("pass" if n >= check.min_rows else "fail"), f"rows={n} (need ≥{check.min_rows})"
    if check.nonempty is not None:
        empty = (not rows) if rows is not None else (text.strip() in ("", "{}", "[]"))
        ok = (not empty) if check.nonempty else empty
        return ("pass" if ok else "fail"), f"nonempty={not empty}"
    return "ran", ""


async def run_diagnostics(
    rb: Runbook, params: dict[str, str], tool_map: dict[str, Any]
) -> list[DiagnosticResult]:
    """Execute the read-only diagnostics. `tool_map` is {tool_name: BaseTool};
    a step whose `action` isn't in it is skipped (this is how a remediation
    action can never run here). Steps with unresolved `{placeholders}` after
    substitution are skipped too — we don't fire a half-filled query."""
    results: list[DiagnosticResult] = []
    for s in rb.diagnostics:
        args = _subst(s.args, params)
        if s.action not in tool_map:
            results.append(
                DiagnosticResult(
                    id=s.id,
                    desc=s.desc,
                    action=s.action,
                    args=args,
                    status="skipped",
                    expect=s.expect,
                    detail="action is not a read-only tool (remediation is not run at Tier 1)",
                )
            )
            continue
        missing = _unresolved(args)
        if missing:
            results.append(
                DiagnosticResult(
                    id=s.id,
                    desc=s.desc,
                    action=s.action,
                    args=args,
                    status="skipped",
                    expect=s.expect,
                    detail=f"unresolved parameters: {', '.join(sorted(set(missing)))}",
                )
            )
            continue
        try:
            out = await tool_map[s.action].ainvoke(args)
        except Exception as e:
            results.append(
                DiagnosticResult(
                    id=s.id,
                    desc=s.desc,
                    action=s.action,
                    args=args,
                    status="error",
                    expect=s.expect,
                    detail=f"{type(e).__name__}: {e}",
                )
            )
            continue
        status, detail = _evaluate_check(s.check, out)
        text = out if isinstance(out, str) else json.dumps(out, default=str)
        results.append(
            DiagnosticResult(
                id=s.id,
                desc=s.desc,
                action=s.action,
                args=args,
                status=status,
                expect=s.expect,
                detail=detail,
                output_preview=text[:500],
                output_text=text,
            )
        )
    return results


def format_diagnostics(rb: Runbook, results: list[DiagnosticResult]) -> str:
    """Inject-ready summary of what the diagnostics confirmed/refuted."""
    lines = [
        f"## Runbook diagnostics auto-run: {rb.id}",
        "These read-only checks ran before your investigation. Treat 'pass' as "
        "a confirmed precondition and build on it; don't re-run them.",
    ]
    for i, r in enumerate(results, 1):
        head = f"{i}. [{r.status.upper()}] {r.desc}"
        if r.expect:
            head += f" (expect: {r.expect})"
        lines.append(head)
        if r.detail:
            lines.append(f"   - {r.detail}")
        if r.output_preview:
            lines.append(f"   - result: {r.output_preview}")
    return "\n".join(lines)


# ---- branch: pick the remediation that matches the diagnosis ----------------


class RemediationChoice(BaseModel):
    """One remediation step with the branch's verdict on it."""

    step: Step
    applicable: bool
    reason: str = ""


def _match_condition(
    cond: Condition, by_id: dict[str, DiagnosticResult] | None, params: dict[str, str]
) -> tuple[bool, str]:
    """(applicable, reason). Undetermined counts as applicable — see
    `select_remediation` for why fail-open is the right default here."""
    for k, v in (cond.param_equals or {}).items():
        if str(params.get(k, "")) != str(v):
            return False, f"{k}={params.get(k)!r}, this step is for {k}={v!r}"

    if cond.diagnostic is None:
        return True, ""
    if by_id is None:
        return True, "diagnostics did not run"
    r = by_id.get(cond.diagnostic)
    if r is None:
        # An authoring bug in the runbook, not a verdict about the incident.
        logger.warning("runbook condition names unknown diagnostic id %r", cond.diagnostic)
        return True, f"diagnostic {cond.diagnostic!r} not found (condition not evaluated)"

    if cond.status is not None:
        want = [cond.status] if isinstance(cond.status, str) else list(cond.status)
        if r.status not in want:
            return False, f"{cond.diagnostic} is {r.status}, needs {'/'.join(want)}"
    if r.status in ("skipped", "error") and (cond.output_contains or cond.output_not_contains):
        # There is no output to read, so the text clauses have no verdict.
        return True, f"{cond.diagnostic} {r.status}, text condition not evaluated"
    if cond.output_contains is not None and cond.output_contains not in r.output_text:
        return False, f"{cond.diagnostic} does not say {cond.output_contains!r}"
    if cond.output_not_contains is not None and cond.output_not_contains in r.output_text:
        return False, f"{cond.diagnostic} says {cond.output_not_contains!r}"
    return True, ""


def select_remediation(
    rb: Runbook, results: list[DiagnosticResult] | None, params: dict[str, str]
) -> list[RemediationChoice]:
    """Choose the remediation steps whose `when` matches what the diagnostics found.

    Until now a runbook listed one fix per alert, and the alert is exactly the
    thing that cannot tell the shapes apart: the same decline-rate page fires
    whether a bad image shipped or a mounted ConfigMap was flipped, and
    `rollout undo` only helps in the first case. The provenance check
    (`inapplicable_by_provenance`) caught that afterwards, by striking the wrong
    action off a list it should never have been on for this incident. This picks
    the branch at the front instead, from the diagnostics that already ran.

    Fail-open by construction: a step is dropped only when a condition is
    *decidedly* false. No diagnostics, an unknown id, a step that errored — all
    keep the step, because the cost of a wrong drop is that the on-call is never
    shown the fix, and the cost of a wrong keep is one more line the gate still
    has to clear.

    A branch clause is not a precondition. `execution.py` aborts an approved
    action when any diagnostic `check` fails, so a diagnostic that exists to
    *sort* incidents should carry no `check` (status "ran") and be branched on
    with `output_contains`; a `check` that fails on purpose in the other branch
    would abort the fix that is right for this one.
    """
    by_id = None
    if results is not None:
        by_id = {r.id: r for r in results if r.id}
    out: list[RemediationChoice] = []
    for s in rb.remediation:
        if s.when is None:
            out.append(RemediationChoice(step=s, applicable=True))
            continue
        ok, reason = _match_condition(s.when, by_id, params)
        out.append(RemediationChoice(step=s, applicable=ok, reason=reason))
    return out


def format_remediation_choices(choices: list[RemediationChoice]) -> str:
    """The branch, written out. The steps that were *not* chosen stay visible
    with the reason: an on-call reading "we didn't roll back because the last
    rollouts changed nothing" learns the shape of the incident, where a silently
    shortened list teaches nothing."""
    if not any(c.reason for c in choices):
        return ""
    lines = ["## Runbook remediation branch"]
    for c in choices:
        mark = "APPLIES" if c.applicable else "NOT FOR THIS INCIDENT"
        line = f"- [{mark}] {c.step.desc} — `{c.step.action}`"
        if c.reason:
            line += f" ({c.reason})"
        lines.append(line)
    return "\n".join(lines)
