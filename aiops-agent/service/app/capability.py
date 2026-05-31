"""Per-service capability index.

Turns the raw discovery tools into a cached, proactively-injected inventory.
When a user message names a service, we fetch (or serve from cache) that
service's real metrics / span names / log fields and inject a compact snapshot
into the turn's context — so the agent writes correct queries without first
spending a discovery tool call, and without trusting the (drift-prone) static
catalog for inventory.

Service detection here is the cheap exact/substring pass (English names and
their short form). Fuzzy / Chinese-alias resolution is the resolver's job
(Phase D); on a miss we simply inject nothing and let the agent fall back to
the catalog + on-demand discover_* tools.
"""

from __future__ import annotations

import asyncio
import logging
import time

from langchain_core.messages import SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

from .config import settings
from .tools.discovery import (
    discover_log_fields,
    discover_metrics,
    discover_span_names,
    list_service_names,
)

logger = logging.getLogger("aiops_agent.capability")

_CAP_TTL = 600  # per-service inventory changes slowly; 10 min is plenty
_SVC_TTL = 300

_cap_cache: dict[str, tuple[float, dict]] = {}
_svc_cache: tuple[float, list[str]] | None = None


async def _service_names() -> list[str]:
    global _svc_cache
    now = time.time()
    if _svc_cache and now - _svc_cache[0] < _SVC_TTL:
        return _svc_cache[1]
    try:
        names = await list_service_names()
    except Exception as e:  # discovery datastore hiccup — don't break the turn
        logger.warning("list_service_names failed: %s", e)
        return _svc_cache[1] if _svc_cache else []
    _svc_cache = (now, names)
    return names


async def get_service_capability(service: str) -> dict:
    """Fetch (or cache-serve) a service's live inventory."""
    now = time.time()
    hit = _cap_cache.get(service)
    if hit and now - hit[0] < _CAP_TTL:
        return hit[1]

    metrics, spans, fields = await asyncio.gather(
        discover_metrics(service),
        discover_span_names(service),
        discover_log_fields(service),
        return_exceptions=True,
    )
    cap = {
        "service": service,
        "metrics": metrics.get("metrics", []) if isinstance(metrics, dict) else [],
        "span_names": spans.get("span_names", []) if isinstance(spans, dict) else [],
        "fields": fields.get("fields", []) if isinstance(fields, dict) else [],
    }
    for label, val in (("metrics", metrics), ("spans", spans), ("fields", fields)):
        if isinstance(val, Exception):
            logger.warning("discover %s for %s failed: %s", label, service, val)
    _cap_cache[service] = (now, cap)
    return cap


async def detect_services(message: str) -> list[str]:
    """Exact / short-form match of live service names in the message. The short
    form is only used when it's >=4 chars, so a 3-letter token like `api`
    doesn't spuriously match every message that mentions an API."""
    names = await _service_names()
    msg = message.lower()
    found = []
    for n in names:
        short = n.split("-")[0].lower()  # order-service -> order
        if n.lower() in msg or (len(short) >= 4 and short in msg):
            found.append(n)
    return found


# ---- LLM fallback resolver -------------------------------------------------
# Exact match handles English names cheaply (no LLM call). When it misses, the
# user likely used a Chinese alias / business term ("訂單服務", "金流"), so we
# ask the LLM to map the phrase to a canonical service_name from the live set.

_CONF_THRESHOLD = 0.6


class _Match(BaseModel):
    service: str = Field(description="canonical service_name, exactly as in the provided list")
    confidence: float = Field(description="0.0-1.0 how sure this is the service the user means")


class _Resolution(BaseModel):
    reasoning: str = Field(default="")
    matches: list[_Match] = Field(default_factory=list)


_resolver_llm = (
    ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.google_api_key,
        temperature=0,
    )
    .with_structured_output(_Resolution)
    .with_config({"run_name": "AIOps_Service_Resolver"})
)

_RESOLVER_PROMPT = """Map the user's mention of a service to canonical service_name(s).

The live services are: {names}.

The user may write English, Traditional/Simplified Chinese, business aliases or
typos. Typical mappings: 訂單/購物車/order → order-service; 付款/金流/結帳/charge
→ payment-service; 使用者/用戶/會員/auth → user-service; 閘道/gateway/路由 →
api-gateway; 網站/前端/edge/入口 → webapp.

Only return services from the list above. If the message is NOT about a specific
service (e.g. "which service is slowest?", "any errors anywhere?"), return an
empty matches list. Give each match a confidence 0-1."""


async def _llm_resolve(message: str, names: list[str]) -> list[tuple[str, float]]:
    res = await _resolver_llm.ainvoke(
        [
            SystemMessage(content=_RESOLVER_PROMPT.format(names=", ".join(names))),
            {"role": "user", "content": message},
        ]
    )
    valid = set(names)
    return [(m.service, m.confidence) for m in res.matches if m.service in valid]


async def resolve_services(message: str) -> dict:
    """Resolve the message to service(s). Returns:
    {method, services: confidently-resolved, candidates: ambiguous/low-conf}.
    `services` drives capability injection now; `candidates` is what the
    interactive disambiguation menu (Phase D-2) will offer."""
    exact = await detect_services(message)
    if exact:
        return {"method": "exact", "services": exact, "candidates": []}

    names = await _service_names()
    if not names:
        return {"method": "none", "services": [], "candidates": []}
    try:
        matches = await _llm_resolve(message, names)
    except Exception as e:
        logger.warning("llm service resolve failed: %s", e)
        return {"method": "none", "services": [], "candidates": []}

    confident = [s for s, c in matches if c >= _CONF_THRESHOLD]
    low = [s for s, c in matches if c < _CONF_THRESHOLD]
    if len(confident) == 1:
        return {"method": "llm", "services": confident, "candidates": []}
    if len(confident) > 1:
        return {"method": "llm-multi", "services": confident, "candidates": confident}
    if low:
        return {"method": "llm-low", "services": [], "candidates": low}
    return {"method": "none", "services": [], "candidates": []}


def format_capability(cap: dict) -> str:
    svc = cap["service"]
    lines = [f"### {svc} (live inventory)"]

    metrics = cap.get("metrics") or []
    if metrics:
        parts = []
        for m in metrics:
            labels = ",".join(m.get("labels") or [])
            label_str = "{" + labels + "}" if labels else ""
            parts.append(f"{m['name']}{label_str} [{m['type']}]")
        lines.append("- metrics: " + "; ".join(parts))

    spans = cap.get("span_names") or []
    if spans:
        lines.append("- span names: " + ", ".join(spans))

    fields = cap.get("fields") or []
    if fields:
        names = ", ".join(f.get("field") for f in fields if f.get("field"))
        lines.append("- log fields: " + names)

    return "\n".join(lines)


async def capability_for_services(services: list[str]) -> str | None:
    """Build the snapshot to inject for an explicit, validated service list.
    Unknown names are dropped; returns None when nothing valid remains."""
    names = set(await _service_names())
    valid = [s for s in services if s in names][:3]
    if not valid:
        return None
    caps = await asyncio.gather(*(get_service_capability(s) for s in valid))
    blocks = [format_capability(c) for c in caps]
    return (
        "## Live capability snapshot\n"
        "Real inventory for the service(s) in the question, read from the "
        "datastores just now. **Trust this over the schema catalog** for which "
        "metrics / spans / log fields exist; use exact names from here in your "
        "queries.\n\n" + "\n\n".join(blocks)
    )
