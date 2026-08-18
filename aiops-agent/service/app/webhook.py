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
from .case_memory import case_scope
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
    uncertainty = result.get("uncertainty")
    logger.info("headless RCA done fp=%s conf=%.2f: %s", fp, findings.confidence, findings.summary)
    if uncertainty:
        logger.warning(
            "headless RCA uncertain fp=%s: missing_signals=%s recommended=%s",
            fp,
            uncertainty.missing_signals,
            uncertainty.recommended_human_action,
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
    labels = alert.get("labels") or {}
    # Opened around the whole run, not just the recording: the dead ends worth
    # remembering are discovered inside the tools, and asyncio copies the
    # context into the tasks the graph spawns.
    with case_scope(
        fp=fp,
        alertname=labels.get("alertname"),
        service=labels.get("service_name") or labels.get("service"),
    ) as scope:
        await _run_and_sink(alert, fp, scope)


async def _run_and_sink(alert: dict, fp: str, scope) -> None:
    try:
        result = await run_headless(alert, thread_id=fp)
        # Log the run's confidence for CE measurement (correctness is labeled
        # offline). The row is keyed by the run, and carries `fp` so a labeler
        # holding only the fingerprint can still resolve to it — the fingerprint
        # used to *be* the run_id, which is why one verdict silently covered
        # every run of an alert and the rest stayed unlabeled forever.
        record_run(
            result["findings"],
            run_id=scope.run_id if scope else fp,
            case_key=scope.case_key if scope else None,
            fp=fp,
        )
        for d in result.get("decisions") or []:
            logger.info(
                "governance fp=%s action=%s -> %s (%s)", fp, d.action, d.autonomy.value, d.reason
            )
        record_investigation(fp, alert, result)
        await _sink_findings(alert, fp, result)
    except Exception as e:
        logger.exception("headless RCA failed fp=%s: %s", fp, e)


async def reinvestigate(
    fp: str,
    alert: dict,
    error_dimension: str | None,
    correction_note: str | None,
) -> None:
    """Re-run RCA for an alert that was labeled Wrong, injecting the human
    correction as context so the agent knows what to reconsider."""
    correction_lines = [
        "The previous RCA for this alert was marked INCORRECT by a human reviewer.",
    ]
    if error_dimension:
        labels = {
            "root_cause": "The root cause identification was wrong.",
            "scope": "The affected service/scope was wrong.",
            "action": "The proposed remediation action was wrong.",
            "other": "The assessment was wrong (see note).",
        }
        correction_lines.append(f"Error dimension: {labels.get(error_dimension, error_dimension)}")
    if correction_note:
        correction_lines.append(f"Human note: {correction_note}")
    correction_lines.append(
        "Please re-examine the signals from scratch and revise your conclusion. "
        "Do NOT repeat the previous answer."
    )
    hint = "\n".join(correction_lines)

    # Inject the correction as a new user turn in the SAME thread so the agent
    # sees its prior reasoning and knows exactly what to fix.
    alert_with_hint = dict(alert)
    alert_with_hint["_correction_hint"] = hint
    await _investigate_and_sink(alert_with_hint, fp)


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
