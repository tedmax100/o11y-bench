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


class Step(BaseModel):
    desc: str
    action: str = Field(description="A read-only tool name, e.g. query_prometheus.")
    args: dict[str, Any] = Field(default_factory=dict)
    expect: str | None = Field(default=None, description="Human-readable precondition.")
    check: DiagnosticCheck | None = None
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
            tag = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"{i}. {s.desc} — `{s.action}`{tag}")
    return "\n".join(lines)


# ---- Tier 1: read-only diagnostics runner ----------------------------------


class DiagnosticResult(BaseModel):
    desc: str
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: str  # "pass" | "fail" | "ran" | "skipped" | "error"
    expect: str | None = None
    detail: str = ""
    output_preview: str = ""


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
        preview = (out if isinstance(out, str) else json.dumps(out, default=str))[:500]
        results.append(
            DiagnosticResult(
                desc=s.desc,
                action=s.action,
                args=args,
                status=status,
                expect=s.expect,
                detail=detail,
                output_preview=preview,
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
