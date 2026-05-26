"""Schema-aware truncation for MCP query tools.

When a Loki / Tempo query returns too many bytes, we don't head-N truncate
(the LLM can't reconstruct a distribution from a random prefix). Instead we
re-issue the same query as a `sum by (...) count_over_time(...)` aggregation
and return that — `event`, `git_version`, `service_name`, `level` are all
low-cardinality enough to bin on, and the LLM gets a usable top-K it can
drill into with a narrower filter on the next call.

The schema this assumes is documented in `schema_catalog.md`. Without those
labels in place this wrapper would still cap, but its `hint` would be
useless.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from langchain_core.tools import BaseTool, StructuredTool

logger = logging.getLogger("aiops_agent.wrap")

# Argument names across mcp-grafana's query tools that expect a timestamp.
# We normalise *all* of these to RFC3339 (UTC, Z-suffixed) before forwarding.
# Both Prom and Loki accept RFC3339 in their respective HTTP APIs, so a single
# canonical form is safe across tools.
_TIME_ARG_KEYS = frozenset({
    "startTime", "endTime", "startRfc3339", "endRfc3339", "start", "end",
})

_RELATIVE_RE = re.compile(r"^\s*now\s*(?:-\s*(\d+)\s*([smhd]))?\s*$", re.IGNORECASE)
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _normalize_time(value: Any) -> Any:
    """Coerce a `now` / `now-Xh|m|s|d` string to RFC3339. Pass through anything
    that already looks like a timestamp or isn't a string. Gemini Flash-Lite
    keeps emitting `"now-1h"` even when the system prompt tells it not to —
    this catches it before mcp-grafana rejects the call."""
    if not isinstance(value, str):
        return value
    if _RFC3339_RE.match(value):
        return value
    m = _RELATIVE_RE.match(value)
    if not m:
        return value
    num, unit = m.group(1), m.group(2)
    now = datetime.now(UTC)
    if num is None:
        ts = now
    else:
        delta_units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
        ts = now - timedelta(**{delta_units[unit.lower()]: int(num)})
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_time_args(kwargs: dict) -> dict:
    out = dict(kwargs)
    for k in list(out):
        if k in _TIME_ARG_KEYS:
            new = _normalize_time(out[k])
            if new != out[k]:
                logger.info("normalized time arg %s: %r -> %r", k, out[k], new)
                out[k] = new
    return out


def _force_datasource_uid(tool_name: str, kwargs: dict) -> dict:
    """Overwrite `datasourceUid` with the canonical value for this tool's
    backing datasource. Gemini Flash-Lite sometimes invents UIDs."""
    canonical = _CANONICAL_DS_UID.get(tool_name)
    if canonical is None:
        return kwargs
    out = dict(kwargs)
    if out.get("datasourceUid") != canonical:
        logger.info(
            "rewrote datasourceUid for %s: %r -> %r",
            tool_name, out.get("datasourceUid"), canonical,
        )
        out["datasourceUid"] = canonical
    return out


def _fill_prom_defaults(kwargs: dict) -> dict:
    """mcp-grafana's `query_prometheus` rejects range queries that omit
    `stepSeconds`. Gemini Flash-Lite forgets it ~once per 3 calls, and when
    rejected it asks the user to specify a step instead of just picking one.
    Inject a sane default so the request goes through."""
    out = dict(kwargs)
    qt = out.get("queryType", "range")
    if qt == "range" and out.get("stepSeconds") in (None, 0):
        out["stepSeconds"] = 60
        logger.info("filled default stepSeconds=60 on query_prometheus call")
    return out

LOKI_CAP_BYTES = 8 * 1024
TEMPO_CAP_BYTES = 8 * 1024
PROM_CAP_BYTES = 16 * 1024

LOKI_TOOL_NAMES = {"query_loki_logs"}
TEMPO_TOOL_NAMES = {"query_tempo_traces"}
PROM_TOOL_NAMES = {"query_prometheus", "query_prometheus_histogram"}

# Canonical datasource UIDs as provisioned in demo-services/k8s/14-grafana.yaml.
# Gemini Flash-Lite sometimes hallucinates random-looking UIDs (e.g. "6o_aK6nZk")
# which mcp-grafana rejects with an opaque error and the LLM doesn't recover.
# Tool name uniquely picks the datasource, so just force-overwrite.
_CANONICAL_DS_UID = {
    "query_loki_logs": "loki",
    "query_tempo_traces": "tempo",
    "query_prometheus": "prometheus",
    "query_prometheus_histogram": "prometheus",
}

# MCP-grafana uses `logql`/`traceql`/`expr`. Older or alternative servers
# sometimes use `query`. We accept both so the wrapper survives an upstream
# rename without code change.
_LOGQL_KEYS = ("logql", "query")
_TRACEQL_KEYS = ("traceql", "query")
_PROMQL_KEYS = ("expr", "query")


def _approx_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (str, bytes)):
        return len(value)
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _get_arg(args: dict, keys: tuple[str, ...]) -> tuple[str | None, str | None]:
    for k in keys:
        if k in args and isinstance(args[k], str):
            return k, args[k]
    return None, None


def _extract_logql_selector(logql: str) -> str | None:
    """Return the first `{...}` stream selector from a LogQL query, or None
    if the query doesn't start with one. We ignore the rest of the pipeline
    deliberately — the post-selector filters are what blew the cap."""
    m = re.match(r"\s*(\{[^}]*\})", logql)
    return m.group(1) if m else None


def _extract_traceql_selector(traceql: str) -> str | None:
    """TraceQL selectors are `{ ... }` blocks. Grab the first one."""
    m = re.match(r"\s*(\{[^}]*\})", traceql)
    return m.group(1) if m else None


def _build_loki_fallback(selector: str) -> str:
    return (
        "topk(20, "
        "sum by (service_name, level, event, git_version) "
        f"(count_over_time({selector} [5m])))"
    )


def _build_tempo_fallback(selector: str) -> str:
    # TraceQL aggregation: count spans grouped by service + status.
    return (
        f"{selector} "
        "| count() by (resource.service.name, status, span.http.status_code)"
    )


def _make_wrapper(
    tool: BaseTool,
    *,
    cap_bytes: int,
    query_keys: tuple[str, ...],
    build_fallback: Callable[[str], str],
    extract_selector: Callable[[str], str | None],
    flavor: str,
) -> BaseTool:
    """Wrap `tool` so its output is capped and oversize results auto-aggregate.

    `flavor` is just a string used in messages / hints (e.g. "Loki")."""

    # Call the underlying tool's coroutine directly instead of `tool.ainvoke`.
    # Going through ainvoke would emit a second on_tool_start/on_tool_end pair
    # nested inside our wrapper's events, which the SSE stream surfaces as a
    # duplicate tool card in the UI.
    inner = tool.coroutine
    if inner is None:
        raise RuntimeError(f"wrap_with_cap: {tool.name} has no coroutine to wrap")

    async def _coroutine(**kwargs):
        kwargs = _normalize_time_args(kwargs)
        kwargs = _force_datasource_uid(tool.name, kwargs)
        result = await inner(**kwargs)
        size = _approx_size(result)
        if size <= cap_bytes:
            return result

        key, query = _get_arg(kwargs, query_keys)
        if key is None or query is None:
            return {
                "truncated": True,
                "reason": f"{flavor} output {size}B > cap {cap_bytes}B",
                "hint": (
                    f"No recognized query key in args (looked for {list(query_keys)}). "
                    "Re-issue a narrower query manually."
                ),
            }

        selector = extract_selector(query)
        if selector is None:
            return {
                "truncated": True,
                "reason": f"{flavor} output {size}B > cap {cap_bytes}B; selector unparseable",
                "original_query": query,
                "hint": (
                    f"{flavor} query did not start with a `{{label=...}}` stream "
                    "selector, so the wrapper can't auto-aggregate. Rewrite the "
                    "query with an explicit stream selector first."
                ),
            }

        fallback_query = build_fallback(selector)
        new_args = dict(kwargs)
        new_args[key] = fallback_query

        try:
            agg = await inner(**new_args)
        except Exception as exc:
            logger.warning("%s fallback aggregation failed: %s", flavor, exc)
            return {
                "truncated": True,
                "reason": f"{flavor} output {size}B > cap; fallback query also failed",
                "original_query": query,
                "fallback_query": fallback_query,
                "fallback_error": str(exc),
            }

        return {
            "truncated": True,
            "reason": f"{flavor} output {size}B > cap {cap_bytes}B",
            "original_query": query,
            "fallback_query": fallback_query,
            "fallback_aggregation": agg,
            "hint": (
                f"Raw {flavor} output too large. Auto-aggregated by "
                "(service_name, level, event, git_version) for Loki / "
                "(service, status, http.status_code) for Tempo. Pick a "
                "specific bucket and re-query with that as an extra filter, "
                "or shorten the time window."
            ),
        }

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=_coroutine,
    )


def _make_prom_wrapper(tool: BaseTool) -> BaseTool:
    """Prometheus results are usually small (already aggregated). If they
    aren't, the LLM wrote `http_requests_total` without a `sum by (...)`.
    Tell it to fix the query rather than silently re-aggregating —
    re-aggregating raw timeseries from Prom is more work than just asking."""

    inner = tool.coroutine
    if inner is None:
        raise RuntimeError(f"wrap_with_cap: {tool.name} has no coroutine to wrap")

    async def _coroutine(**kwargs):
        kwargs = _normalize_time_args(kwargs)
        kwargs = _force_datasource_uid(tool.name, kwargs)
        kwargs = _fill_prom_defaults(kwargs)
        result = await inner(**kwargs)
        size = _approx_size(result)
        if size <= PROM_CAP_BYTES:
            return result

        _, query = _get_arg(kwargs, _PROMQL_KEYS)
        return {
            "truncated": True,
            "reason": f"Prometheus output {size}B > cap {PROM_CAP_BYTES}B",
            "original_query": query,
            "hint": (
                "PromQL result is too large — usually this means the query "
                "returned raw per-instance series. Wrap it in `sum by (...)` "
                "or `topk(...)`, or narrow the matcher (e.g. add "
                "`{service_name=\"...\"}`), then re-query."
            ),
        }

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=_coroutine,
    )


def wrap_with_cap(tool: BaseTool) -> BaseTool:
    """Return a wrapped tool if `tool` is one of the query tools we cap;
    otherwise return the tool unchanged."""
    if tool.name in LOKI_TOOL_NAMES:
        return _make_wrapper(
            tool,
            cap_bytes=LOKI_CAP_BYTES,
            query_keys=_LOGQL_KEYS,
            build_fallback=_build_loki_fallback,
            extract_selector=_extract_logql_selector,
            flavor="Loki",
        )
    if tool.name in TEMPO_TOOL_NAMES:
        return _make_wrapper(
            tool,
            cap_bytes=TEMPO_CAP_BYTES,
            query_keys=_TRACEQL_KEYS,
            build_fallback=_build_tempo_fallback,
            extract_selector=_extract_traceql_selector,
            flavor="Tempo",
        )
    if tool.name in PROM_TOOL_NAMES:
        return _make_prom_wrapper(tool)
    return tool
