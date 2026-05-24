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
from typing import Any, Callable

from langchain_core.tools import BaseTool, StructuredTool

logger = logging.getLogger("aiops_agent.wrap")

LOKI_CAP_BYTES = 8 * 1024
TEMPO_CAP_BYTES = 8 * 1024
PROM_CAP_BYTES = 16 * 1024

LOKI_TOOL_NAMES = {"query_loki_logs"}
TEMPO_TOOL_NAMES = {"query_tempo_traces"}
PROM_TOOL_NAMES = {"query_prometheus"}

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

    async def _coroutine(**kwargs):
        result = await tool.ainvoke(kwargs)
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
            agg = await tool.ainvoke(new_args)
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

    async def _coroutine(**kwargs):
        result = await tool.ainvoke(kwargs)
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
