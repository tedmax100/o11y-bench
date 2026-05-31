"""Direct native-API query tools for Prometheus / Loki / Tempo.

Replaces the mcp-grafana query tools (and the wrap.py shim around them). The
agent runs inside the demo k3d cluster and reaches each datasource over
internal DNS, so we talk to the raw HTTP APIs instead of proxying through
Grafana. The schema-aware byte-cap + aggregation fallback that used to live in
wrap.py is ported here, minus the mcp-only patches (datasourceUid forcing,
stepSeconds repair) which no longer apply.

Native-API quirks this accounts for (probed against the live stack):
- Prometheus range/instant under `/api/v1/query_range` & `/api/v1/query`.
- Loki label/query APIs require `start`/`end` in **nanoseconds** or they
  silently return empty — we always pass them.
- Tempo search (`/api/search`) takes `start`/`end` in **unix seconds** and a
  TraceQL string in `q`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

from ..config import settings

logger = logging.getLogger("aiops_agent.query")

# ---- time handling ---------------------------------------------------------

_RELATIVE_RE = re.compile(r"^\s*now\s*(?:-\s*(\d+)\s*([smhd]))?\s*$", re.IGNORECASE)
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_DELTA_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def _parse_dt(value: str) -> datetime:
    """Resolve a `now` / `now-Xh|m|s|d` shorthand or an RFC3339 string to an
    aware UTC datetime. Gemini Flash-Lite emits the shorthand constantly even
    when told not to, so we accept both forms."""
    now = datetime.now(UTC)
    m = _RELATIVE_RE.match(value)
    if m:
        num, unit = m.group(1), m.group(2)
        if num is None:
            return now
        return now - timedelta(**{_DELTA_UNITS[unit.lower()]: int(num)})
    if _RFC3339_RE.match(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    raise ToolException(
        f"Unrecognized time value {value!r}. Use RFC3339 (2026-05-31T12:00:00Z) "
        "or the shorthand now / now-15m / now-1h / now-2d."
    )


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_s(dt: datetime) -> int:
    return int(dt.timestamp())


def _epoch_ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


# ---- byte cap + aggregation fallback --------------------------------------

LOKI_CAP_BYTES = 8 * 1024
TEMPO_CAP_BYTES = 8 * 1024
PROM_CAP_BYTES = 16 * 1024


def _approx_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (str, bytes)):
        return len(value)
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _selector(query: str) -> str | None:
    """First `{...}` stream/span selector. The post-selector pipeline is what
    blows the cap, so we re-aggregate on the selector alone."""
    m = re.match(r"\s*(\{[^}]*\})", query)
    return m.group(1) if m else None


# ---- HTTP helpers ----------------------------------------------------------

_TIMEOUT = httpx.Timeout(30.0)


async def _get_json(base: str, path: str, params: dict) -> Any:
    url = f"{base.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise ToolException(f"request to {url} failed: {exc}") from exc
    # Prom/Loki wrap errors in a 4xx body with {"status":"error","error":...}.
    if resp.status_code >= 400:
        detail = resp.text[:500]
        raise ToolException(f"{url} returned {resp.status_code}: {detail}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ToolException(f"{url} returned non-JSON: {resp.text[:300]}") from exc


# ---- Prometheus ------------------------------------------------------------

class PrometheusArgs(BaseModel):
    expr: str = Field(description="PromQL expression. Aggregate at the source "
                       "(sum by / topk / histogram_quantile); don't fetch raw series.")
    queryType: str = Field(default="range", description="'range' or 'instant'.")
    start: str = Field(default="now-1h", description="RFC3339 or now-shorthand (range only).")
    end: str = Field(default="now", description="RFC3339 or now-shorthand (range only).")
    stepSeconds: int = Field(default=60, description="Range step in seconds.")


async def _query_prometheus(expr: str, queryType: str = "range", start: str = "now-1h",
                            end: str = "now", stepSeconds: int = 60) -> Any:
    if queryType == "instant":
        data = await _get_json(settings.prometheus_url, "/api/v1/query",
                               {"query": expr, "time": _rfc3339(_parse_dt(end))})
    else:
        s, e = _parse_dt(start), _parse_dt(end)
        data = await _get_json(settings.prometheus_url, "/api/v1/query_range",
                               {"query": expr, "start": _rfc3339(s), "end": _rfc3339(e),
                                "step": str(max(stepSeconds, 1))})
    if isinstance(data, dict) and data.get("status") == "error":
        raise ToolException(f"Prometheus error: {data.get('error')}")
    result = data.get("data", data) if isinstance(data, dict) else data
    if _approx_size(result) <= PROM_CAP_BYTES:
        return result
    return {
        "truncated": True,
        "reason": f"Prometheus result > {PROM_CAP_BYTES}B — likely raw per-series.",
        "original_query": expr,
        "hint": ("Wrap the query in `sum by (...)` / `topk(...)` or narrow the "
                 "matcher (e.g. add `{service_name=\"...\"}`), then re-query."),
    }


# ---- Loki ------------------------------------------------------------------

class LokiArgs(BaseModel):
    logql: str = Field(description="LogQL. Aggregate with count_over_time / sum by; "
                       "avoid pulling >100 raw lines.")
    start: str = Field(default="now-1h", description="RFC3339 or now-shorthand.")
    end: str = Field(default="now", description="RFC3339 or now-shorthand.")
    limit: int = Field(default=100, description="Max log lines (log queries).")
    direction: str = Field(default="backward", description="'backward' or 'forward'.")


def _loki_fallback(selector: str) -> str:
    return ("topk(20, sum by (service_name, level, event, git_version) "
            f"(count_over_time({selector} [5m])))")


async def _query_loki_logs(logql: str, start: str = "now-1h", end: str = "now",
                          limit: int = 100, direction: str = "backward") -> Any:
    s, e = _parse_dt(start), _parse_dt(end)
    base_params = {"start": _epoch_ns(s), "end": _epoch_ns(e)}  # Loki needs ns
    data = await _get_json(settings.loki_url, "/loki/api/v1/query_range",
                           {**base_params, "query": logql, "limit": limit,
                            "direction": direction})
    if isinstance(data, dict) and data.get("status") == "error":
        raise ToolException(f"Loki error: {data.get('error')}")
    result = data.get("data", data) if isinstance(data, dict) else data
    if _approx_size(result) <= LOKI_CAP_BYTES:
        return result

    selector = _selector(logql)
    if selector is None:
        return {"truncated": True,
                "reason": f"Loki result > {LOKI_CAP_BYTES}B and no `{{...}}` selector to aggregate on.",
                "original_query": logql,
                "hint": "Rewrite with an explicit stream selector, then re-query."}
    fb = _loki_fallback(selector)
    step = max((_epoch_s(e) - _epoch_s(s)) // 100, 1)
    try:
        agg = await _get_json(settings.loki_url, "/loki/api/v1/query_range",
                              {**base_params, "query": fb, "step": step})
        agg = agg.get("data", agg) if isinstance(agg, dict) else agg
    except ToolException as exc:
        return {"truncated": True, "original_query": logql, "fallback_query": fb,
                "fallback_error": str(exc)}
    return {
        "truncated": True,
        "reason": f"Raw Loki output > {LOKI_CAP_BYTES}B; auto-aggregated.",
        "original_query": logql,
        "fallback_query": fb,
        "fallback_aggregation": agg,
        "hint": ("Aggregated by (service_name, level, event, git_version). Pick a "
                 "bucket and re-query with it as an extra filter, or shorten the window."),
    }


# ---- Tempo -----------------------------------------------------------------

class TempoArgs(BaseModel):
    traceql: str = Field(description="TraceQL, e.g. "
                         "{ resource.service.name=\"order-service\" && status=error }. "
                         "Tempo attrs use dotted names.")
    start: str = Field(default="now-1h", description="RFC3339 or now-shorthand.")
    end: str = Field(default="now", description="RFC3339 or now-shorthand.")
    limit: int = Field(default=20, description="Max traces returned.")


async def _query_tempo_traces(traceql: str, start: str = "now-1h", end: str = "now",
                             limit: int = 20) -> Any:
    s, e = _parse_dt(start), _parse_dt(end)
    data = await _get_json(settings.tempo_url, "/api/search",
                           {"q": traceql, "start": _epoch_s(s), "end": _epoch_s(e),  # Tempo: unix seconds
                            "limit": limit})
    traces = data.get("traces", []) if isinstance(data, dict) else []
    if _approx_size(traces) <= TEMPO_CAP_BYTES:
        return {"traces": traces, "count": len(traces)}
    # Trace summaries are already compact; if still oversize, return id+service+duration only.
    slim = [{"traceID": t.get("traceID"), "rootServiceName": t.get("rootServiceName"),
             "rootTraceName": t.get("rootTraceName"), "durationMs": t.get("durationMs")}
            for t in traces[:limit]]
    return {
        "truncated": True,
        "reason": f"Tempo result > {TEMPO_CAP_BYTES}B; returning slim summaries.",
        "traces": slim,
        "hint": "Tighten the TraceQL (add status=error / duration> / a route) or lower limit.",
    }


# ---- tool objects ----------------------------------------------------------

query_prometheus = StructuredTool(
    name="query_prometheus",
    description="Run a PromQL query against Prometheus (range or instant). Use for "
                "rates, error ratios, p95/p99 latency (histogram_quantile), gauges.",
    args_schema=PrometheusArgs,
    coroutine=_query_prometheus,
)

query_loki_logs = StructuredTool(
    name="query_loki_logs",
    description="Run a LogQL query against Loki. Use for error/warn lines, BizEvent "
                "counts, deployment events. Aggregate with count_over_time / sum by.",
    args_schema=LokiArgs,
    coroutine=_query_loki_logs,
)

query_tempo_traces = StructuredTool(
    name="query_tempo_traces",
    description="Search traces in Tempo with TraceQL. Use to find the origin service "
                "of an error or slow operations. Resource/span attrs use dotted names.",
    args_schema=TempoArgs,
    coroutine=_query_tempo_traces,
)
