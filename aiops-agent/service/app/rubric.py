"""LLM-as-judge rubric guards for high-stakes agent actions.

Two guards:
  verify_trace_ids(answer, max_retries) — checks every trace ID in an RCA
      answer actually exists in Tempo; on failure, returns a retry prompt
      asking the agent to re-query rather than hallucinate.

  check_k8s_write(action, args, context) — before executing a k8s mutation,
      a grader LLM checks that the action matches stated intent, the
      deployment name looks plausible, and the change is not abnormally risky.
      Returns (ok: bool, reason: str).

Both are best-effort: any exception → pass-through (never block the main path).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

from .config import settings

logger = logging.getLogger("aiops_agent.rubric")

# A trace ID is 128 bits, so 32 hex chars — but Tempo's search API returns them
# with leading zeros stripped, and about one in six real IDs in this stack comes
# back 30 or 31 chars long. A `{32}` pattern silently skips exactly those, which
# means the guard was not checking the IDs it could not see. 24 is well past the
# point where a real ID could be shorter (that needs 32 leading zero bits) and
# still long enough not to collide with ordinary hex-looking words.
_TRACE_ID_RE = re.compile(r"\b([0-9a-f]{24,32})\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Trace ID verification
# ---------------------------------------------------------------------------


async def _tempo_trace_exists(trace_id: str) -> bool:
    """Return True if Tempo has a trace with this ID. Timeout = 3 s."""
    # Tempo answers on both the stripped and the zero-padded form; pad so the
    # ID we check is the canonical 32-char one regardless of how it was cited.
    url = f"{settings.tempo_url.rstrip('/')}/api/traces/{trace_id.rjust(32, '0')}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
        if resp.status_code == 404:
            return False
        data = resp.json()
        # Tempo returns {"batches": [...]} — empty batches = trace not found
        batches = data.get("batches", [])
        return bool(batches)
    except Exception as e:
        logger.debug("tempo existence check for %s failed: %s", trace_id, e)
        return True  # assume valid on network error to avoid blocking


async def verify_trace_ids(answer: str) -> tuple[bool, str]:
    """Check every trace ID embedded in *answer* against Tempo.

    Returns (True, "") when all IDs verify (or there are none).
    Returns (False, retry_prompt) when at least one is missing — the retry
    prompt asks the agent to re-query Tempo rather than fabricate IDs.
    """
    ids = list({m.group(1).lower() for m in _TRACE_ID_RE.finditer(answer)})
    if not ids:
        return True, ""

    missing = []
    for tid in ids:
        if not await _tempo_trace_exists(tid):
            missing.append(tid)

    if not missing:
        return True, ""

    logger.warning("rubric: trace ID hallucination detected — missing in Tempo: %s", missing)
    retry_prompt = (
        f"The trace IDs {missing} you cited do not exist in Tempo. "
        "You MUST NOT invent trace IDs. Call `query_tempo_traces` again to find real traces, "
        "then cite the `traceID` value verbatim from the tool result. "
        "If the query returns zero results, explicitly say no traces were found — "
        "do not substitute a made-up ID."
    )
    return False, retry_prompt


# ---------------------------------------------------------------------------
# K8s write pre-flight rubric
# ---------------------------------------------------------------------------


class _K8sRubricVerdict(BaseModel):
    safe_to_proceed: bool = Field(
        description="True if the action is safe to proceed; false to block."
    )
    reason: str = Field(description="One sentence explaining the verdict.")


_K8S_RUBRIC_SYSTEM = """You are a safety reviewer for Kubernetes remediation actions.
Given a proposed action, its arguments, and a brief context of the incident,
decide whether it is safe to proceed.

BLOCK the action (safe_to_proceed=false) if ANY of the following is true:
- The deployment name or namespace looks obviously wrong (e.g. "default/kube-system",
  wildcard globs, suspiciously generic names like "all" or "*")
- The requested replica count is 0 (could take a service completely down)
- The action is rollout_undo but the context says the problem is NOT a bad deploy
  (e.g. the RCA concluded the issue is a DB overload or infra failure)
- The scale factor is > 10× the current replica count (abnormal amplification)

ALLOW the action (safe_to_proceed=true) if none of the above apply.
Be permissive for legitimate remediations — the circuit breaker and blast-radius
gates have already run. Only block clear safety violations."""


def _k8s_rubric_llm() -> Any:
    return (
        ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            google_api_key=settings.google_api_key,
            temperature=0,
        )
        .with_structured_output(_K8sRubricVerdict)
        .with_config({"run_name": "K8s_Write_Rubric"})
    )


async def check_k8s_write(action: str, args: dict, context: str = "") -> tuple[bool, str]:
    """LLM rubric gate before a k8s mutation executes.

    Returns (True, reason) to allow, (False, reason) to block.
    Any exception → allow (best-effort, never block valid remediations).
    """
    prompt = (
        f"Action: {action}\nArguments: {args}\nIncident context: {context or '(none provided)'}"
    )
    try:
        llm = _k8s_rubric_llm()
        verdict: _K8sRubricVerdict = await llm.ainvoke(
            [SystemMessage(content=_K8S_RUBRIC_SYSTEM), HumanMessage(content=prompt)]
        )
        if not verdict.safe_to_proceed:
            logger.warning(
                "rubric: k8s write BLOCKED — action=%s args=%s reason=%s",
                action,
                args,
                verdict.reason,
            )
        return verdict.safe_to_proceed, verdict.reason
    except Exception as e:
        logger.warning("rubric: k8s write check failed (%s) — allowing", e)
        return True, f"rubric check skipped ({e})"
