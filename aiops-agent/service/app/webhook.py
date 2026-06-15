"""Alert-driven (PUSH) RCA orchestration — doc v3 §4.

Grafana Alerting POSTs a firing alert to /webhook/alert; we fingerprint it,
drop duplicates inside a cooldown window, and kick off a headless RCA per
distinct alert as a background task. The HTTP response returns immediately
(the investigation outlives the request) so Grafana's webhook doesn't time out
waiting for the LLM. Conclusions are distilled into Findings and pushed to a
sink (log always; Grafana annotation if configured).
"""

import asyncio
import hashlib
import logging
import time

import httpx

from .agent import run_headless
from .calibration import record_run
from .config import settings
from .investigations import record_investigation

logger = logging.getLogger("aiops_agent.webhook")

# fingerprint -> last-investigation monotonic timestamp. In-memory, like the
# MemorySaver checkpointer; doc v3 §4.2 notes production would persist this.
_last_run: dict[str, float] = {}

# Hold strong refs to in-flight background investigations so the event loop
# doesn't garbage-collect a task mid-run.
_tasks: set[asyncio.Task] = set()


def fingerprint(labels: dict) -> str:
    """Stable id for an alert instance = alertname + the labels that make it a
    distinct incident. Doubles as the LangGraph thread_id so a later plugin
    follow-up lands in the same investigation thread (doc v3 §4.2 / §4.4)."""
    key = "|".join(
        [
            labels.get("alertname", ""),
            labels.get("service_name", "") or labels.get("service", ""),
            labels.get("git_version", ""),
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _in_cooldown(fp: str) -> bool:
    last = _last_run.get(fp)
    return last is not None and (time.monotonic() - last) < settings.alert_cooldown_seconds


async def _sink_findings(alert: dict, fp: str, result: dict) -> None:
    """Push the conclusion out of the headless run. Always logs; posts a Grafana
    annotation when grafana_url + grafana_token are set (best-effort)."""
    findings = result["findings"]
    logger.info(
        "headless RCA done fp=%s conf=%.2f: %s", fp, findings.confidence, findings.summary
    )

    if not (settings.grafana_url and settings.grafana_token):
        return

    labels = alert.get("labels") or {}
    text = (
        f"[AIOps RCA] {findings.summary} "
        f"(confidence {findings.confidence:.0%}; hypothesis: {findings.hypothesis})"
    )
    tags = ["aiops-rca", f"fp:{fp}"]
    if labels.get("service_name") or labels.get("service"):
        tags.append(f"service:{labels.get('service_name') or labels.get('service')}")
    if findings.suspected_version:
        tags.append(f"version:{findings.suspected_version}")

    body: dict = {"text": text, "tags": tags}
    # Anchor the annotation at the alert's fire time when we have it.
    starts_at = alert.get("startsAt")
    if starts_at:
        try:
            from datetime import datetime

            ms = int(datetime.fromisoformat(starts_at.replace("Z", "+00:00")).timestamp() * 1000)
            body["time"] = ms
        except (ValueError, AttributeError):
            pass

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{settings.grafana_url.rstrip('/')}/api/annotations",
                json=body,
                headers={"Authorization": f"Bearer {settings.grafana_token}"},
            )
            resp.raise_for_status()
    except Exception as e:
        logger.warning("annotation sink failed fp=%s: %s", fp, e)


async def _investigate_and_sink(alert: dict, fp: str) -> None:
    try:
        result = await run_headless(alert, thread_id=fp)
        # Log the run's confidence for CE measurement (correctness is labeled
        # offline). Best-effort inside record_run; fp doubles as the run_id so a
        # later `label <fp>` ties the verdict back to this investigation.
        record_run(result["findings"], run_id=fp)
        for d in result.get("decisions") or []:
            logger.info("governance fp=%s action=%s -> %s (%s)",
                        fp, d.action, d.autonomy.value, d.reason)
        record_investigation(fp, alert, result)
        await _sink_findings(alert, fp, result)
    except Exception as e:
        logger.exception("headless RCA failed fp=%s: %s", fp, e)


async def handle_alert(payload: dict) -> dict:
    """Process a Grafana webhook payload. Returns which alerts were accepted for
    investigation vs skipped (and why) — the investigations run in the
    background, so this returns without waiting for the RCA to finish."""
    alerts = payload.get("alerts") or []
    accepted: list[str] = []
    skipped: list[dict] = []

    for alert in alerts:
        status = alert.get("status")
        if status and status != "firing":
            skipped.append({"reason": "not-firing", "status": status})
            continue

        labels = alert.get("labels") or {}
        fp = fingerprint(labels)
        if _in_cooldown(fp):
            skipped.append({"fingerprint": fp, "reason": "cooldown"})
            continue

        _last_run[fp] = time.monotonic()
        task = asyncio.create_task(_investigate_and_sink(alert, fp))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        accepted.append(fp)

    return {"accepted": accepted, "skipped": skipped}
