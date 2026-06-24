"""Direct native-API discovery tools.

Answers "what does service X actually have" by querying the live datastores
instead of trusting the static schema_catalog. Feeds the per-service capability
index (capability.py) and the service resolver, and is also exposed to the
agent so it can look up inventory the catalog doesn't cover.

Probed API shapes (see the aiops-native-api-behaviors memory):
- Prometheus `/api/v1/series?match[]={service_name="X"}` → every series (hence
  metric names + label keys). `/api/v1/metadata` is empty under OTel
  remote-write, so metric *type* is inferred from the name suffix.
- Tempo `/api/v2/search/tag/name/values?q={resource.service.name="X"}` → span
  names scoped to the service (start/end in unix seconds).
- Loki `/loki/api/v1/detected_fields?query={service_name="X"}` → structured
  metadata / detected field keys with cardinality (start/end in nanoseconds).
- Loki `/loki/api/v1/label/service_name/values` → the live service set.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..config import settings
from .query import _epoch_ns, _epoch_s, _get_json, _parse_dt

logger = logging.getLogger("aiops_agent.discovery")

# Resource/identity labels present on every series — not interesting as the
# "extra" dimensions of a metric.
_COMMON_LABELS = frozenset(
    {
        "__name__",
        "service_name",
        "service_namespace",
        "git_repo",
        "git_version",
        "service_version",
        "deployment_environment",
        "job",
        "instance",
    }
)
# High-cardinality auto-instrumentation / SDK labels that are noise in a summary.
_NOISE_LABELS = frozenset(
    {
        "http_host",
        "http_server_name",
        "http_target",
        "http_url",
        "http_scheme",
        "http_flavor",
        "net_host_name",
        "net_host_port",
        "net_peer_ip",
        "net_peer_port",
        "le",
        "telemetry_auto_version",
        "telemetry_sdk_language",
        "telemetry_sdk_name",
        "telemetry_sdk_version",
    }
)
# Loki detected fields that are SDK/telemetry plumbing or duplicates of the
# standard trace_id/span_id, not business signal.
_NOISE_LOG_FIELDS = frozenset(
    {
        "telemetry_sdk_language",
        "telemetry_sdk_name",
        "telemetry_sdk_version",
        "telemetry_auto_version",
        "severity_number",
        "severity_text",
        "detected_level",
        "observed_timestamp",
        "otelServiceName",
        "otelTraceID",
        "otelSpanID",
        "otelTraceSampled",
        "service_version",
        "schema_url",
        "scope_name",
    }
)


def _metric_family(name: str) -> tuple[str, str]:
    """(base_name, type) — collapse histogram/summary families to one entry and
    infer type from the OTel/Prom naming convention."""
    for suffix in ("_bucket", "_sum", "_count"):
        if name.endswith(suffix):
            return name[: -len(suffix)], "histogram"
    if name.endswith("_total"):
        return name, "counter"
    return name, "gauge"


# ---- Prometheus metrics ----------------------------------------------------


async def discover_metrics(service: str, lookback: str = "now-1h") -> dict[str, Any]:
    start, end = _parse_dt(lookback), _parse_dt("now")
    data = await _get_json(
        settings.prometheus_url,
        "/api/v1/series",
        {
            "match[]": f'{{service_name="{service}"}}',
            "start": _epoch_s(start),
            "end": _epoch_s(end),
        },
    )
    series = data.get("data", []) if isinstance(data, dict) else []
    families: dict[tuple[str, str], set[str]] = {}
    for s in series:
        name = s.get("__name__")
        if not name or name.startswith("otel_sdk_") or name == "target_info":
            continue  # SDK internals, not application signal
        key = _metric_family(name)
        labels = {k for k in s if k not in _COMMON_LABELS and k not in _NOISE_LABELS}
        families.setdefault(key, set()).update(labels)
    metrics = [
        {"name": base, "type": mtype, "labels": sorted(labels)}
        for (base, mtype), labels in sorted(families.items())
    ]
    return {"service": service, "metric_count": len(metrics), "metrics": metrics}


# ---- Tempo span names ------------------------------------------------------


def _is_useful_span(name: str) -> bool:
    # Drop the auto httpx client child spans and bare HTTP-verb spans.
    if name.endswith((" http send", " http receive")):
        return False
    if name.strip() in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
        return False
    return True


async def discover_span_names(service: str, lookback: str = "now-1h") -> dict[str, Any]:
    start, end = _parse_dt(lookback), _parse_dt("now")
    data = await _get_json(
        settings.tempo_url,
        "/api/v2/search/tag/name/values",
        {
            "q": f'{{resource.service.name="{service}"}}',
            "start": _epoch_s(start),
            "end": _epoch_s(end),
        },
    )
    raw = data.get("tagValues", []) if isinstance(data, dict) else []
    names = sorted({tv.get("value") for tv in raw if isinstance(tv, dict) and tv.get("value")})
    useful = [n for n in names if _is_useful_span(n)]
    return {"service": service, "span_names": useful}


# ---- Loki log fields -------------------------------------------------------


async def discover_log_fields(service: str, lookback: str = "now-1h") -> dict[str, Any]:
    start, end = _parse_dt(lookback), _parse_dt("now")
    data = await _get_json(
        settings.loki_url,
        "/loki/api/v1/detected_fields",
        {
            "query": f'{{service_name="{service}"}}',
            "start": _epoch_ns(start),
            "end": _epoch_ns(end),
        },
    )
    fields = data.get("fields", []) if isinstance(data, dict) else []
    out = [
        {"field": f.get("label"), "type": f.get("type"), "cardinality": f.get("cardinality")}
        for f in fields
        if f.get("label") and f.get("label") not in _NOISE_LOG_FIELDS
    ]
    out.sort(key=lambda f: f.get("cardinality") or 0, reverse=True)
    return {"service": service, "fields": out}


# ---- live service set (used by the resolver) -------------------------------


async def list_service_names(lookback: str = "now-6h") -> list[str]:
    """The set of service_name values actually present in Loki right now."""
    start, end = _parse_dt(lookback), _parse_dt("now")
    data = await _get_json(
        settings.loki_url,
        "/loki/api/v1/label/service_name/values",
        {"start": _epoch_ns(start), "end": _epoch_ns(end)},
    )
    return sorted(data.get("data", []) if isinstance(data, dict) else [])


# ---- agent-facing tools ----------------------------------------------------


class ServiceArg(BaseModel):
    service: str = Field(description="Exact service_name, e.g. order-service.")


discover_metrics_tool = StructuredTool(
    name="discover_metrics",
    description="List the Prometheus metrics a service actually emits (name, type, "
    "label keys). Use when you're unsure which metric to query for a service.",
    args_schema=ServiceArg,
    coroutine=discover_metrics,
)

discover_span_names_tool = StructuredTool(
    name="discover_span_names",
    description="List the Tempo span/operation names a service produces. Use to find "
    "the right operation to filter traces on.",
    args_schema=ServiceArg,
    coroutine=discover_span_names,
)

discover_log_fields_tool = StructuredTool(
    name="discover_log_fields",
    description="List the Loki structured-metadata/log fields a service emits (with "
    "cardinality). Use to find which fields you can filter or group logs by.",
    args_schema=ServiceArg,
    coroutine=discover_log_fields,
)
