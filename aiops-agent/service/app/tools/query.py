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
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, Field

from .. import case_memory
from ..config import settings

logger = logging.getLogger("aiops_agent.query")

# ---- time handling ---------------------------------------------------------

_RELATIVE_RE = re.compile(r"^\s*now\s*(?:-\s*(\d+)\s*([smhd]))?\s*$", re.IGNORECASE)
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_DELTA_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

# "Now" override for the headless alert path. The interactive chat path leaves
# this None and every clock reference resolves to the real wall clock. The
# alert webhook (doc v3 §4.5) sets it to the alert's `startsAt` for the duration
# of one investigation, so BOTH the `now-...` shorthand expansion below AND the
# "Current time" line in the system prompt resolve to the alert's fire time
# instead of when the agent happens to be running. ContextVar (not a module
# global) so concurrent investigations don't clobber each other's clock.
_now_override: ContextVar[datetime | None] = ContextVar("now_override", default=None)


def current_now() -> datetime:
    """The clock the agent should treat as 'now'. Real wall clock unless an
    alert investigation has shifted it to the alert's startsAt."""
    return _now_override.get() or datetime.now(UTC)


@contextmanager
def now_override(dt: datetime | None):
    """Pin `current_now()` to `dt` within this block (None → real clock).
    asyncio copies the context into tasks at creation, so graph nodes spawned
    during `await agent.ainvoke(...)` inside this block inherit the pinned clock."""
    token = _now_override.set(dt)
    try:
        yield
    finally:
        _now_override.reset(token)


def _parse_dt(value: str) -> datetime:
    """Resolve a `now` / `now-Xh|m|s|d` shorthand or an RFC3339 string to an
    aware UTC datetime. Gemini Flash-Lite emits the shorthand constantly even
    when told not to, so we accept both forms."""
    now = current_now()
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
    """First `{...}` stream/span selector, wherever in the expression it sits.
    The post-selector pipeline is what blows the cap, so we re-aggregate on the
    selector alone.

    It used to anchor at the start of the string, which quietly excluded every
    metric-shaped LogQL query — `sum(count_over_time({...} | event="x" [5m]))`
    begins with `sum(`, not with `{`. That mattered far more than the truncation
    path it was written for: the empty-result diagnostics all hang off this
    function, so the one query shape the schema catalog actually demonstrates
    was the one shape that came back as a bare empty result with no note about
    a bad label, a bad field, or an idle window. Measured 2026-08-29 against the
    live stack: a bogus selector and a bogus field both returned `result: []`
    and nothing else, while the stream-shaped versions of the same two mistakes
    each got a full diagnostic.

    The stream selector is always the first `{...}` in a LogQL expression, so
    searching rather than anchoring stays correct for both callers.
    """
    m = re.search(r"(\{[^}]*\})", query)
    return m.group(1) if m else None


# ---- series summarization (feed the LLM a digest, not every datapoint) -----
# A range query returns one value per step (e.g. 61 points over 1h @ 60s). The
# LLM only needs to *read* the result to write the answer — it does not need
# every raw float, and the chart is rendered separately by the plugin re-running
# the same query in Grafana. So we collapse matrix/vector results to a compact
# summary (last/min/max/avg + a short sample only if the series actually moves)
# and round values. This cuts the post-tool LLM call's input tokens sharply
# (the bloat was ~60 high-precision floats per series) without losing the
# numbers an answer or a trend/spike call needs.

_SAMPLE_POINTS = 8  # max points kept when a series varies enough to show a trend
_VARY_THRESHOLD = 0.05  # (max-min)/|max| above this → keep a downsample


def _round_sig(x: Any, sig: int = 4) -> Any:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return x
    if f == 0 or not math.isfinite(f):
        return 0.0 if f == 0 else f
    return round(f, -int(math.floor(math.log10(abs(f)))) + (sig - 1))


def _summarize_series_result(result: Any) -> Any:
    """Collapse a Prometheus/Loki matrix|vector|scalar result for LLM context.
    Non-series shapes (log streams, etc.) pass through unchanged."""
    if not isinstance(result, dict):
        return result
    rt = result.get("resultType")

    if rt == "matrix":
        out = []
        for s in result.get("result", []):
            pairs = s.get("values", []) or []
            vals = []
            for v in pairs:
                try:
                    fv = float(v[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if math.isfinite(fv):
                    vals.append(fv)
            if not vals:
                out.append({"metric": s.get("metric", {}), "points": 0})
                continue
            n = len(vals)
            mn, mx = min(vals), max(vals)
            entry: dict[str, Any] = {
                "metric": s.get("metric", {}),
                "points": n,
                "last": _round_sig(vals[-1]),
                "min": _round_sig(mn),
                "max": _round_sig(mx),
                "avg": _round_sig(sum(vals) / n),
                "window": [pairs[0][0], pairs[-1][0]],
            }
            # Keep a coarse downsample only when the series actually moves, so a
            # spike/trend is still visible; near-constant series stay tiny.
            if n > 2 and mx and abs(mx - mn) / abs(mx) > _VARY_THRESHOLD:
                step = max(n // _SAMPLE_POINTS, 1)
                entry["sample"] = [[pairs[i][0], _round_sig(vals[i])] for i in range(0, n, step)]
            out.append(entry)
        return {
            "resultType": "matrix_summary",
            "result": out,
            "note": (
                "Series summarized to last/min/max/avg (+ sample if it varies); "
                "the ```promql``` panel re-runs the full query for the chart."
            ),
        }

    if rt == "vector":
        out = []
        for s in result.get("result", []):
            val = s.get("value", [None, None])
            out.append(
                {
                    "metric": s.get("metric", {}),
                    "value": _round_sig(val[1] if len(val) > 1 else None),
                }
            )
        return {"resultType": "vector", "result": out}

    if rt == "scalar":
        r = result.get("result")
        v = _round_sig(r[1] if isinstance(r, list) and len(r) > 1 else r)
        return {"resultType": "scalar", "value": v}

    return result


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


# ---- empty results ---------------------------------------------------------
# An empty result is the most dangerous shape a tool can return: HTTP 200, no
# error, and nothing to read. The agent's own transcripts show what it does with
# one — it rewords the query and asks again, because nothing in the response
# says "the name you used doesn't exist here". These helpers spend one cheap
# metadata call to tell it which it is: a name that isn't there, or a window
# where nothing happened. Both fail open; a hint is never worth an exception.

_PROM_METRIC_RE = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?:\{|\[|\)|\s|$)")
_PROMQL_KEYWORDS = frozenset(
    {
        "sum",
        "avg",
        "min",
        "max",
        "count",
        "topk",
        "bottomk",
        "rate",
        "irate",
        "increase",
        "delta",
        "idelta",
        "histogram_quantile",
        "quantile",
        "by",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
        "clamp_min",
        "clamp_max",
        "vector",
        "scalar",
        "absent",
        "label_replace",
        "or",
        "and",
        "unless",
        "le",
        "offset",
        "bool",
        "sum_over_time",
        "avg_over_time",
        "max_over_time",
        "min_over_time",
        "count_over_time",
        "stddev",
        "stdvar",
    }
)


# Grouping clauses and label matchers hold *label* names, and a label name run
# through the metric extractor is always "missing" — there is no series called
# `reason`. A live drill recorded five dead ends of that shape in one run, and
# because they go into the recall block, the next investigation of the same
# incident is told not to spend budget on the labels the answer is written in.
# Strip both regions before looking for metric names.
_PROM_GROUPING_RE = re.compile(
    r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", re.IGNORECASE
)
_PROM_MATCHER_RE = re.compile(r"\{[^}]*\}")


def _metric_names(expr: str) -> set[str]:
    """The metric names an expression actually references."""
    stripped = _PROM_MATCHER_RE.sub("{}", _PROM_GROUPING_RE.sub(" ", expr))
    return {
        m
        for m in _PROM_METRIC_RE.findall(stripped)
        if m not in _PROMQL_KEYWORDS and not m.isdigit()
    }


async def _prom_empty_note(expr: str) -> dict[str, Any] | None:
    """Which metric names in this expression don't exist in Prometheus at all."""
    try:
        data = await _get_json(settings.prometheus_url, "/api/v1/label/__name__/values", {})
        known = set(data.get("data", []) if isinstance(data, dict) else [])
    except ToolException:
        return None
    if not known:
        return None
    used = _metric_names(expr)
    missing = sorted(m for m in used if m not in known)
    if not missing:
        return {
            "note": "The metric names exist, but nothing matched in this window. "
            "Check the label matchers (values are case-sensitive) or widen the range "
            "before assuming the value is zero."
        }
    # A name that does not exist here is a property of the environment, not of
    # the time window, so it is worth remembering against the case. The
    # empty-window branch above deliberately is not: "nothing happened then" must
    # not become "don't look there".
    case_memory.remember_dead_end(
        "query",
        f"PromQL referencing {', '.join(missing)}",
        disproved_by="tool_result",
        evidence="no such metric in this Prometheus",
    )
    return {
        "note": f"No such metric in Prometheus: {', '.join(missing)}.",
        "hint": "Call discover_metrics(service) for the names this service really "
        "emits — rewording this query will return empty again.",
    }


_LOKI_SELECTOR_KEY_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=~|!~|!=|=)")


async def _loki_empty_note(logql: str, start: datetime, end: datetime) -> dict[str, Any] | None:
    """Which selector keys aren't indexable labels — the `{service=...}` trap."""
    selector = _selector(logql)
    if selector is None:
        return None
    try:
        data = await _get_json(
            settings.loki_url,
            "/loki/api/v1/labels",
            {"start": _epoch_ns(start), "end": _epoch_ns(end)},
        )
        labels = set(data.get("data", []) if isinstance(data, dict) else [])
    except ToolException:
        return None
    if not labels:
        return None
    used = set(_LOKI_SELECTOR_KEY_RE.findall(selector))
    unknown = sorted(k for k in used if k not in labels)
    if not unknown:
        # The selector is fine, so the emptiness comes from further down the
        # pipeline — and the most common reason is a filter on a field these
        # services never emit. Saying only "no lines in this window" reads as
        # "nothing happened then", which is the opposite of the truth and is
        # exactly how a run talks itself into a dead end: three of four eval
        # runs on the session-cache incident ended at "logs returned no data",
        # one of them filtering `level="error"` against services that emit no
        # level at all.
        pipeline = await _loki_unknown_pipeline_fields(logql, selector, labels, start, end)
        if pipeline:
            return pipeline
        return {"note": "The stream selector is valid but matched no lines in this window."}
    case_memory.remember_dead_end(
        "query",
        f"LogQL stream selector on {', '.join(unknown)}",
        disproved_by="tool_result",
        evidence="not an indexable stream label in this Loki",
    )
    return {
        "note": f"Not an indexable stream label: {', '.join(unknown)}. "
        f"Indexable labels here: {', '.join(sorted(labels))}.",
        "hint": "Everything else (event, trace_id, business fields) is structured "
        'metadata — filter it AFTER the selector with `| field="..."`. '
        "discover_log_fields(service) lists the fields this service emits.",
    }


# `| json | event="cache.miss"` — a field filter after the selector. Line filters
# (`|=`, `!~` on a bare string) and stage keywords carry no field name and so
# never match this.
_LOKI_PIPELINE_FIELD_RE = re.compile(r"\|\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*(?:=~|!~|!=|=)\s*[\"`]")
# Stages that look like `| name` but name a parser or formatter, not a field.
_LOKI_STAGE_WORDS = {
    "json",
    "logfmt",
    "pattern",
    "regexp",
    "unpack",
    "line_format",
    "label_format",
    "unwrap",
    "decolorize",
    "drop",
    "keep",
    "distinct",
}


async def _loki_unknown_pipeline_fields(
    logql: str, selector: str, labels: set[str], start: datetime, end: datetime
) -> dict[str, Any] | None:
    """Which fields the query filters on after the selector that this stream
    does not actually emit.

    The mirror of the Prometheus metric-name check, and it was missing: that one
    tells you the name does not exist here; this side only ever checked the
    `{...}` keys, so a filter on a nonexistent field came back as a plain empty
    result with a note about the time window.

    Window-scoped, and says so — see the comment on the return.

    Fail-open in every direction — no detected fields, an unreachable Loki, a
    query we cannot parse — because a false "that field does not exist" would
    send a run away from the one query that works.
    """
    used = {
        k
        for k in _LOKI_PIPELINE_FIELD_RE.findall(logql)
        if k not in _LOKI_STAGE_WORDS and k not in labels
    }
    if not used:
        return None
    try:
        data = await _get_json(
            settings.loki_url,
            "/loki/api/v1/detected_fields",
            {"query": selector, "start": _epoch_ns(start), "end": _epoch_ns(end)},
        )
    except ToolException:
        return None
    fields = data.get("fields") if isinstance(data, dict) else None
    if not fields:
        return None
    known = {f.get("label") for f in fields if isinstance(f, dict) and f.get("label")}
    if not known:
        return None
    unknown = sorted(k for k in used if k not in known)
    if not unknown:
        return None
    # Deliberately **not** remembered as a dead end, unlike the metric-name
    # branch above. That one asks Prometheus for every name it knows, which is a
    # statement about the environment. `detected_fields` is scoped to this
    # window and these lines, so on a quiet window it returns only the OTel
    # envelope — measured on the live stack: 15 fields, none of them `event` —
    # and a dead end saying "event is not a field" would outlive the quiet hour
    # and push later runs off the one query that works. The note below is worth
    # saying now; it is not worth remembering.
    return {
        "note": f"Not a field on the lines in this window: {', '.join(unknown)}. "
        f"Present here: {', '.join(sorted(known)) or 'none detected'}.",
        "hint": "The result is empty because of the filter, not because nothing "
        "happened — unless this window holds no lines from this service at all. "
        "discover_log_fields(service) lists what it emits.",
    }


def _is_empty_result(result: Any) -> bool:
    if isinstance(result, dict):
        inner = result.get("result")
        return isinstance(inner, list) and not inner
    return False


# ---- Prometheus ------------------------------------------------------------


class PrometheusArgs(BaseModel):
    expr: str = Field(
        description="PromQL expression. Aggregate at the source "
        "(sum by / topk / histogram_quantile); don't fetch raw series."
    )
    queryType: str = Field(default="range", description="'range' or 'instant'.")
    start: str = Field(default="now-1h", description="RFC3339 or now-shorthand (range only).")
    end: str = Field(default="now", description="RFC3339 or now-shorthand (range only).")
    stepSeconds: int = Field(default=60, description="Range step in seconds.")


async def _query_prometheus(
    expr: str,
    queryType: str = "range",
    start: str = "now-1h",
    end: str = "now",
    stepSeconds: int = 60,
) -> Any:
    if queryType == "instant":
        data = await _get_json(
            settings.prometheus_url,
            "/api/v1/query",
            {"query": expr, "time": _rfc3339(_parse_dt(end))},
        )
    else:
        s, e = _parse_dt(start), _parse_dt(end)
        data = await _get_json(
            settings.prometheus_url,
            "/api/v1/query_range",
            {
                "query": expr,
                "start": _rfc3339(s),
                "end": _rfc3339(e),
                "step": str(max(stepSeconds, 1)),
            },
        )
    if isinstance(data, dict) and data.get("status") == "error":
        raise ToolException(f"Prometheus error: {data.get('error')}")
    result = data.get("data", data) if isinstance(data, dict) else data
    # Digest the series before it reaches the LLM (the chart is rendered from the
    # query, not from this payload). Drops the per-step float dump to last/min/max/avg.
    result = _summarize_series_result(result)
    if _is_empty_result(result):
        note = await _prom_empty_note(expr)
        if note:
            return {**result, **note}
    if _approx_size(result) <= PROM_CAP_BYTES:
        return result
    return {
        "truncated": True,
        "reason": f"Prometheus result > {PROM_CAP_BYTES}B — likely raw per-series.",
        "original_query": expr,
        "hint": (
            "Wrap the query in `sum by (...)` / `topk(...)` or narrow the "
            'matcher (e.g. add `{service_name="..."}`), then re-query.'
        ),
    }


# ---- Loki ------------------------------------------------------------------


class LokiArgs(BaseModel):
    logql: str = Field(
        description="LogQL. Aggregate with count_over_time / sum by; avoid pulling >100 raw lines."
    )
    start: str = Field(default="now-1h", description="RFC3339 or now-shorthand.")
    end: str = Field(default="now", description="RFC3339 or now-shorthand.")
    limit: int = Field(default=100, description="Max log lines (log queries).")
    direction: str = Field(default="backward", description="'backward' or 'forward'.")
    queryType: str = Field(
        default="auto",
        description="'auto' (default — metric aggregations like count_over_time / "
        "rate / sum run as an instant query returning the windowed total; raw log "
        "lines run as a range query), or force 'instant' / 'range'. A metric range "
        "query returns a per-step series you must NOT average into a total.",
    )


# Metric LogQL (returns a number, not log lines): a range version yields one
# windowed value per step, which a model wrongly averages into a "total". For
# these we run an INSTANT query so the windowed total is a single value.
_METRIC_LOGQL_RE = re.compile(
    r"\b(count_over_time|rate|sum_over_time|avg_over_time|bytes_over_time|"
    r"bytes_rate|absent_over_time|quantile_over_time)\b|^\s*(sum|topk|count|avg|min|max)\s*\(",
    re.IGNORECASE,
)


def _is_metric_logql(logql: str) -> bool:
    return bool(_METRIC_LOGQL_RE.search(logql))


# Stages that cannot appear inside `count_over_time(...)`, or that change what a
# line *is* rather than which lines survive. Everything else — line filters
# (`|=`, `!~`), label filters (`| event="x"`), parsers (`| json`) — narrows the
# set of lines and so belongs in the count.
_LOKI_UNCOUNTABLE_STAGES = ("line_format", "label_format", "unwrap")


def _loki_pipeline(logql: str, selector: str) -> str:
    """The part of the query after the stream selector, when it can be counted.

    "" when there is none, or when it contains a stage that has no meaning
    inside `count_over_time` — the caller then falls back to counting the
    selector alone and says so.
    """
    idx = logql.find(selector)
    if idx < 0:
        return ""
    rest = logql[idx + len(selector) :]
    # A metric-shaped query carries its own range and closing parens; taking its
    # tail would produce nonsense, and its result was small enough not to be here.
    rest = rest.split("[")[0].strip().rstrip(")").strip()
    if not rest.startswith("|"):
        return ""
    if any(stage in rest for stage in _LOKI_UNCOUNTABLE_STAGES):
        return ""
    return rest


def _loki_fallback(selector: str, pipeline: str = "") -> str:
    """Count what the caller actually asked about, bucketed.

    The pipeline used to be dropped here, and that made the summary answer a
    different question from the one asked: `{service_name="payment-service"}
    |= "declined"` came back as counts of every event on the service, top bucket
    `payment.authorized`, under a key that said `original_query`. It is labelled
    `truncated`, so it is not a lie — but "I asked for declines and read back a
    number for authorisations" is a mistake nobody would notice.

    `detected_level` rather than `level`: Loki synthesises the former for every
    stream, while the latter is whatever the application happened to name its
    field — here, nothing, so the old grouping bucketed on a label that does not
    exist and the hint told the model to filter by it.
    """
    body = f"{selector} {pipeline}".strip()
    return (
        "topk(20, sum by (service_name, detected_level, event, git_version) "
        f"(count_over_time({body} [5m])))"
    )


def _loki_query_hint(logql: str, exc: ToolException) -> ToolException:
    """Turn a raw Loki 400/parse error into an actionable LogQL hint. The model
    otherwise re-sends the same broken query; a specific correction lets it fix
    in one shot."""
    msg = str(exc)
    if "parse error" not in msg and "returned 400" not in msg and "unexpected" not in msg:
        return exc
    if _selector(logql) is None:
        return ToolException(
            f'{msg}\nHINT: LogQL must START with a stream selector `{{label="..."}}` '
            "before any `|` filter. trace_id / level / event / business fields are "
            "structured metadata — filter them AFTER a selector, e.g. "
            '`{service_name="<svc>"} | trace_id="<id>"`. Indexable selector labels: '
            "service_name, git_repo, git_version, deployment_environment."
        )
    return ToolException(
        f'{msg}\nHINT: check the LogQL pipeline. Log filter: `{{...}} | level="ERROR"`. '
        "Metric/count: `sum(count_over_time({{...}} | <filters> [<window>]))` — the "
        "range goes INSIDE count_over_time, and the whole thing is wrapped in sum(...)."
    )


async def _query_loki_logs(
    logql: str,
    start: str = "now-1h",
    end: str = "now",
    limit: int = 100,
    direction: str = "backward",
    queryType: str = "auto",
) -> Any:
    s, e = _parse_dt(start), _parse_dt(end)
    # Resolve 'auto': metric aggregation → instant (clean windowed total),
    # raw log lines → range. Removes the model's chance to mis-pick range and
    # then average a per-step count series into a wrong total.
    if queryType == "auto":
        queryType = "instant" if _is_metric_logql(logql) else "range"
    # A forced 'instant' on a raw stream selector is not a query Loki can run at
    # all — it 400s with "log queries are not supported as an instant query
    # type". The model reaches for it anyway, because every instruction about
    # windowed totals says the word "instant"; measured on the home-field bench,
    # that mis-pick cost a whole task in 6/6 runs and no prompt wording stopped
    # it.
    #
    # Silently demoting it to a range query is worse than the 400: a raw range
    # query returns at most `limit` lines, so counting them yields a confidently
    # low number (263 against a true 487, measured) instead of a visible error.
    # What the caller wanted is a windowed total, and the shape that gives one is
    # decidable here — so fail with the rewrite instead of guessing.
    if queryType == "instant" and not _is_metric_logql(logql):
        raise ToolException(
            "An instant Loki query needs metric-shaped LogQL; a raw stream selector "
            "can only run as a range query, and counting its (capped) lines is not a "
            "total.\nHINT: wrap the selector to get the windowed total in one value: "
            f"`sum(count_over_time({logql.strip()} [<window>]))` with queryType='instant'. "
            "Use queryType='range' only when you want to read the lines themselves."
        )
    try:
        if queryType == "instant":
            # Single value at `end` — the right shape for a windowed total/count.
            data = await _get_json(
                settings.loki_url,
                "/loki/api/v1/query",
                {"query": logql, "time": _epoch_ns(e), "limit": limit, "direction": direction},
            )
        else:
            data = await _get_json(
                settings.loki_url,
                "/loki/api/v1/query_range",
                {
                    "start": _epoch_ns(s),
                    "end": _epoch_ns(e),  # Loki needs ns
                    "query": logql,
                    "limit": limit,
                    "direction": direction,
                },
            )
    except ToolException as exc:
        raise _loki_query_hint(logql, exc) from exc
    if isinstance(data, dict) and data.get("status") == "error":
        raise _loki_query_hint(logql, ToolException(f"Loki error: {data.get('error')}"))
    result = data.get("data", data) if isinstance(data, dict) else data
    # Loki attaches a ~1.5 KB `stats` block (cache counters, chunk bytes) to every
    # response, empty ones included. It is query telemetry for Grafana, not an
    # answer to anything the agent asked, so it never reaches the model.
    if isinstance(result, dict):
        result = {k: v for k, v in result.items() if k != "stats"}
    # Metric LogQL (count_over_time, sum by ...) returns a matrix/vector — digest
    # it like Prometheus. Log *streams* are left intact (the lines are the answer)
    # and handled by the byte-cap + aggregation fallback below.
    if isinstance(result, dict) and result.get("resultType") in ("matrix", "vector"):
        result = _summarize_series_result(result)
    if _is_empty_result(result):
        note = await _loki_empty_note(logql, s, e)
        if note:
            return {**result, **note}
    if _approx_size(result) <= LOKI_CAP_BYTES:
        return result

    selector = _selector(logql)
    if selector is None:
        return {
            "truncated": True,
            "reason": f"Loki result > {LOKI_CAP_BYTES}B and no `{{...}}` selector to aggregate on.",
            "original_query": logql,
            "hint": "Rewrite with an explicit stream selector, then re-query.",
        }
    pipeline = _loki_pipeline(logql, selector)
    fb = _loki_fallback(selector, pipeline)
    step = max((_epoch_s(e) - _epoch_s(s)) // 100, 1)

    async def _aggregate(query: str):
        agg = await _get_json(
            settings.loki_url,
            "/loki/api/v1/query_range",
            {"start": _epoch_ns(s), "end": _epoch_ns(e), "query": query, "step": step},
        )
        return agg.get("data", agg) if isinstance(agg, dict) else agg

    dropped = ""
    try:
        agg = await _aggregate(fb)
    except ToolException as exc:
        if not pipeline:
            return {
                "truncated": True,
                "original_query": logql,
                "fallback_query": fb,
                "fallback_error": str(exc),
            }
        # The pipeline did not survive being counted. Falling back to the
        # selector alone is still useful, but it answers a wider question than
        # the one asked, so that has to be said rather than left to be inferred
        # from a query string.
        dropped = str(exc)
        fb = _loki_fallback(selector)
        try:
            agg = await _aggregate(fb)
        except ToolException as exc2:
            return {
                "truncated": True,
                "original_query": logql,
                "fallback_query": fb,
                "fallback_error": str(exc2),
            }
    out = {
        "truncated": True,
        "reason": f"Raw Loki output > {LOKI_CAP_BYTES}B; auto-aggregated.",
        "original_query": logql,
        "fallback_query": fb,
        "fallback_aggregation": agg,
        "hint": (
            "Aggregated by (service_name, detected_level, event, git_version), "
            "keeping your filters. Pick a bucket and re-query with it as an extra "
            "filter, or shorten the window."
        ),
    }
    if dropped:
        out["filters_dropped"] = pipeline
        out["hint"] = (
            f"Your filters ({pipeline}) could not be counted, so these numbers are "
            "for the whole stream selector and NOT for what you asked about. "
            f"Loki said: {dropped}. Aggregated by (service_name, detected_level, "
            "event, git_version); shorten the window to see the filtered lines."
        )
    return out


# ---- Tempo -----------------------------------------------------------------


class TempoArgs(BaseModel):
    traceql: str = Field(
        description="TraceQL, e.g. "
        '{ resource.service.name="order-service" && status=error }. '
        "Tempo attrs use dotted names."
    )
    start: str = Field(default="now-1h", description="RFC3339 or now-shorthand.")
    end: str = Field(default="now", description="RFC3339 or now-shorthand.")
    limit: int = Field(default=20, description="Max traces returned.")


# Attribute names that are right in Prometheus/Loki and wrong in Tempo. The
# agent carries one mental model of "the label for a service" across all three
# stores, so it writes the Loki one here and gets a parse error at col 2.
_TEMPO_RENAMES = {
    "service_name": "resource.service.name",
    "service": "resource.service.name",
    "git_version": "resource.service.version",
    "service_version": "resource.service.version",
    "http_route": "span.http.route",
    "http_status_code": "span.http.status_code",
}
_TEMPO_BASE_HINT = (
    'TraceQL predicates go inside braces, e.g. `{ resource.service.name="<svc>" '
    "&& status=error }`, and attribute names are dotted and scoped "
    "(`resource.` for resource attrs, `span.` for span attrs). Read git_version "
    "off the trace's resource.service.version — don't go to Loki for it."
)


def _tempo_query_hint(traceql: str, exc: ToolException) -> ToolException:
    """Turn a Tempo error into the specific edit that fixes this query.

    Tempo is loud (400/500 with a real message) but its messages are written for
    someone who already knows TraceQL: "unexpected IDENTIFIER" doesn't say that
    `service_name` should have been `resource.service.name`, and "binary
    operations must operate on the same type" doesn't say to drop the quotes
    around `error`. Both of those are edits we can name, so we name them —
    otherwise the model spends another call on a rephrase.
    """
    msg = str(exc)
    lines = [f"{msg}\nHINT: {_TEMPO_BASE_HINT}"]

    wrong = [k for k in _TEMPO_RENAMES if re.search(rf"(?<![.\w]){re.escape(k)}\s*=", traceql)]
    if wrong:
        fixes = ", ".join(f"`{k}` -> `{_TEMPO_RENAMES[k]}`" for k in wrong)
        lines.append(f"This query uses the name it has in Prometheus/Loki, not in Tempo: {fixes}.")
    if re.search(r"status\s*=\s*[\'\"]", traceql) or "must operate on the same type" in msg:
        lines.append(
            "`status` is an intrinsic enum, not a string: write `status=error` "
            "(no quotes). Same for `kind=server`."
        )
    return ToolException("\n".join(lines))


async def _query_tempo_traces(
    traceql: str, start: str = "now-1h", end: str = "now", limit: int = 20
) -> Any:
    s, e = _parse_dt(start), _parse_dt(end)
    # Surface the deployed version on each matched span so deploy-correlation
    # questions ("which git_version was this trace running?") can be answered
    # from the search result itself — Tempo's default summary omits it, which
    # otherwise sends the model off to Loki (and it fails). select() is additive.
    q = traceql if "select(" in traceql.lower() else f"{traceql} | select(resource.service.version)"
    try:
        data = await _get_json(
            settings.tempo_url,
            "/api/search",
            # Tempo expects unix seconds for start/end
            {"q": q, "start": _epoch_s(s), "end": _epoch_s(e), "limit": limit},
        )
    except ToolException as exc:
        raise _tempo_query_hint(traceql, exc) from exc
    traces = data.get("traces", []) if isinstance(data, dict) else []
    if _approx_size(traces) <= TEMPO_CAP_BYTES:
        return {"traces": traces, "count": len(traces)}
    # Trace summaries are already compact; if still oversize, return id+service+duration only.
    slim = [
        {
            "traceID": t.get("traceID"),
            "rootServiceName": t.get("rootServiceName"),
            "rootTraceName": t.get("rootTraceName"),
            "durationMs": t.get("durationMs"),
        }
        for t in traces[:limit]
    ]
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
