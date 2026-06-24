"""Design-alert capability (ARE gap-analysis §4.2 step 6 / v3 §6).

This is the agent's **first side-effecting + human-in-the-loop** capability — the
"Governance warm-up". It deliberately stays on the safe side of the design rule
(§4.3): the read-only reasoning core never mutates anything. Instead the agent
*proposes* an alert rule as an ```alert``` fenced block; the plugin renders it as
a card with a "create alert" button, and **only a human click** POSTs the spec
back to /alerts/provision, which writes it to Grafana.

So the human-in-the-loop gate is structural here, just like the propose-only
remediation gate (governance.py): nothing leaves prose unless a person acts. And
unlike `actions_enabled` (autonomous *mutation*, default off), alert creation is
human-confirmed AND reversible (you can delete the rule), so it's gated by
fail-closed credentials + an operator-flippable switch rather than locked off.

Layering mirrors the rest of the service: a pure `build_alert_rule` transform
(unit-testable payload shape, no I/O) under an async `provision_alert` that does
the one HTTP write, and a `parse_alert_blocks` that extracts the agent's proposal
the same way the plugin's splitQueryBlocks does.
"""

from __future__ import annotations

import json
import logging
import re

import httpx
from pydantic import BaseModel, Field, field_validator

from .config import settings

logger = logging.getLogger("aiops_agent.alerts")


class AlertProvisioningDisabled(RuntimeError):
    """Raised when provisioning is attempted without Grafana credentials or with
    the operator switch off — fail-closed, like the webhook secret gate."""


class AlertSpec(BaseModel):
    """The contract for one proposed alert rule. Kept small and o11y-bench
    flavoured: a single PromQL query reduced to a scalar and compared to a
    threshold. Everything the plugin card needs to show + everything
    build_alert_rule needs to emit a Grafana rule."""

    title: str
    expr: str  # PromQL — evaluated as an instant query, reduced, then thresholded
    threshold: float
    # How the reduced value is compared to the threshold to fire.
    comparison: str = "gt"  # "gt" | "lt"
    for_duration: str = "5m"  # how long the condition must hold before firing
    severity: str = "warning"
    summary: str = ""
    service_name: str | None = None
    datasource_uid: str = "prometheus"
    folder_uid: str = "aiops"
    interval_seconds: int = Field(default=60, ge=10)

    @field_validator("comparison")
    @classmethod
    def _check_comparison(cls, v: str) -> str:
        if v not in ("gt", "lt"):
            raise ValueError("comparison must be 'gt' or 'lt'")
        return v

    @field_validator("for_duration")
    @classmethod
    def _check_for(cls, v: str) -> str:
        # Grafana wants a Go duration ("5m", "30s", "1h"); reject free text early
        # so a bad proposal fails here, not deep in the Grafana API.
        if not re.fullmatch(r"\d+(s|m|h)", v):
            raise ValueError("for_duration must look like '30s' / '5m' / '1h'")
        return v


# Grafana maps our friendly comparison onto its evaluator type.
_EVALUATOR = {"gt": "gt", "lt": "lt"}


def build_alert_rule(spec: AlertSpec) -> dict:
    """Pure transform: AlertSpec → Grafana managed-alert-rule provisioning payload
    (POST /api/v1/provisioning/alert-rules). No I/O, so the payload shape is
    pinned by unit tests.

    The rule is the standard three-stage pipeline Grafana's UI builds:
      A — instant PromQL query against the datasource
      B — reduce A to a single number (last value)
      C — threshold on B; this is the alerting `condition`.
    """
    labels = {"severity": spec.severity}
    if spec.service_name:
        labels["service_name"] = spec.service_name

    annotations = {"summary": spec.summary or spec.title}

    data = [
        {
            "refId": "A",
            "relativeTimeRange": {"from": 600, "to": 0},
            "datasourceUid": spec.datasource_uid,
            "model": {
                "refId": "A",
                "expr": spec.expr,
                "instant": True,
                "range": False,
                "datasource": {"type": "prometheus", "uid": spec.datasource_uid},
            },
        },
        {
            "refId": "B",
            "datasourceUid": "__expr__",
            "model": {
                "refId": "B",
                "type": "reduce",
                "reducer": "last",
                "expression": "A",
                "datasource": {"type": "__expr__", "uid": "__expr__"},
            },
        },
        {
            "refId": "C",
            "datasourceUid": "__expr__",
            "model": {
                "refId": "C",
                "type": "threshold",
                "expression": "B",
                "datasource": {"type": "__expr__", "uid": "__expr__"},
                "conditions": [
                    {
                        "evaluator": {
                            "type": _EVALUATOR[spec.comparison],
                            "params": [spec.threshold],
                        }
                    }
                ],
            },
        },
    ]

    return {
        "title": spec.title,
        "ruleGroup": "aiops-proposed",
        "folderUID": spec.folder_uid,
        "condition": "C",
        "for": spec.for_duration,
        "orgID": 1,
        "noDataState": "OK",
        "execErrState": "Error",
        "labels": labels,
        "annotations": annotations,
        "data": data,
    }


async def provision_alert(spec: AlertSpec) -> dict:
    """Write the proposed rule to Grafana. The single side-effecting call in this
    capability; reached only from a human button click (see /alerts/provision).
    Fail-closed: no Grafana credentials or operator switch off → refuse, like the
    webhook secret gate, rather than silently no-op."""
    if not settings.alert_provisioning_enabled:
        raise AlertProvisioningDisabled(
            "alert provisioning disabled (alert_provisioning_enabled=False)"
        )
    if not (settings.grafana_url and settings.grafana_token):
        raise AlertProvisioningDisabled(
            "alert provisioning disabled (no grafana_url/grafana_token configured)")

    payload = build_alert_rule(spec)
    logger.warning("provisioning alert rule title=%r service=%s", spec.title, spec.service_name)
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{settings.grafana_url.rstrip('/')}/api/v1/provisioning/alert-rules",
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.grafana_token}",
                # The rule is meant to be editable in the UI afterwards, not a
                # file-managed object the operator can't touch.
                "X-Disable-Provenance": "true",
            },
        )
        resp.raise_for_status()
        body = resp.json()
    return {"uid": body.get("uid"), "title": body.get("title", spec.title)}


# group 1: optional info line, group 2: body (JSON AlertSpec)
_ALERT_BLOCK_RE = re.compile(r"```alert([^\n]*)\n?([\s\S]*?)```", re.MULTILINE)


def parse_alert_blocks(text: str) -> list[AlertSpec]:
    """Extract ```alert``` proposals from an agent answer (mirrors the plugin's
    splitQueryBlocks). Each block body is a JSON object matching AlertSpec.
    Malformed blocks are skipped — a bad proposal must not break the turn."""
    if not text:
        return []
    out: list[AlertSpec] = []
    for _info, body in _ALERT_BLOCK_RE.findall(text):
        try:
            out.append(AlertSpec.model_validate(json.loads(body)))
        except Exception as e:
            logger.warning("skipping malformed ```alert``` block: %s", e)
    return out
