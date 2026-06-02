"""Trace Explorer backend.

Powers the plugin's LLM-aware Trace Explorer page. Talks to Tempo's native HTTP
API (the agent already reaches Tempo over internal DNS) and turns a raw trace
into a normalized node tree the frontend can render:

- gen_ai spans (from `opentelemetry-instrumentation-langchain`) become rich
  `llm` / `tool` nodes carrying the actual prompt/completion messages, tool
  args/results, token usage and a computed cost.
- everything else (FastAPI/httpx server-client spans, demo-service business
  spans) becomes generic `http` / `business` nodes with selected attributes.

It also provides a one-shot "AI analysis" summary and a lightweight
"chat about this trace" assistant — the trace JSON is the only context, so this
is deliberately NOT the RCA agent (no intent gate, no live-query tools).

Tempo API quirks (probed live): `/api/search` takes `start`/`end` in unix
seconds + a TraceQL `q`; `/api/traces/{id}` returns OTLP-JSON
(`batches[].scopeSpans[].spans[]`) where attribute values are tagged unions
(`stringValue` / `intValue` (a string) / `boolValue` / `doubleValue` /
`arrayValue.values[]`).
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from .agent import _flatten_content
from .config import settings
from .tools.query import _epoch_s, _get_json, _parse_dt

logger = logging.getLogger("aiops_agent.traces")

# ---- cost model ------------------------------------------------------------
# USD per 1M tokens, (input, output). Cached input is billed at 0.25x input.
# A small, intentionally-overridable map; unknown model -> cost is None.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gemini-3.1-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
}
_CACHE_DISCOUNT = 0.25


def _cost(model: str | None, in_tok: int, out_tok: int, cache_tok: int) -> float | None:
    price = MODEL_PRICES.get(model or "")
    if not price:
        return None
    in_price, out_price = price
    fresh_in = max(in_tok - cache_tok, 0)
    return round(
        (fresh_in * in_price + cache_tok * in_price * _CACHE_DISCOUNT + out_tok * out_price)
        / 1_000_000,
        6,
    )


# ---- OTLP-JSON helpers -----------------------------------------------------


def _otlp_val(v: dict) -> Any:
    """Decode a single OTLP-JSON attribute value (a tagged union)."""
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "boolValue" in v:
        return v["boolValue"]
    if "doubleValue" in v:
        return v["doubleValue"]
    if "arrayValue" in v:
        return [_otlp_val(x) for x in v["arrayValue"].get("values", [])]
    return next(iter(v.values()), None)


def _flatten(attrs: list[dict]) -> dict[str, Any]:
    return {a["key"]: _otlp_val(a.get("value", {})) for a in attrs or []}


def _maybe_json(s: Any) -> Any:
    """gen_ai message/tool payloads arrive as JSON strings; parse when possible."""
    if not isinstance(s, str):
        return s
    t = s.strip()
    if t and t[0] in "[{":
        try:
            return json.loads(t)
        except ValueError:
            return s
    return s


def _truncate(obj: Any, limit: int = 600) -> Any:
    """Shrink a payload for the LLM-context (compact) shape."""
    if isinstance(obj, str):
        return obj if len(obj) <= limit else obj[:limit] + "…"
    if isinstance(obj, list):
        return [_truncate(x, limit) for x in obj[:20]]
    if isinstance(obj, dict):
        return {k: _truncate(v, limit) for k, v in obj.items()}
    return obj


# ---- list ------------------------------------------------------------------


async def list_traces(
    service: str | None = None,
    q: str | None = None,
    start: str = "now-1h",
    end: str = "now",
    limit: int = 30,
) -> dict:
    """Search Tempo and return compact trace summaries for the picker."""
    # Health/readiness probes fire every few seconds and are sub-millisecond
    # single-span traces. They are so frequent they fill Tempo's search `limit`
    # before any real trace surfaces, so a Python post-filter doesn't help —
    # exclude them at the Tempo level via the trace:duration intrinsic. Probes
    # are <2ms; real LLM turns (~seconds) and business traces (root ≥ ~8ms) pass
    # the 5ms floor. A caller-supplied `q` is used verbatim (full control).
    if q:
        traceql = q
    elif service:
        traceql = f'{{ resource.service.name = "{service}" && trace:duration > 5ms }}'
    else:
        traceql = "{ trace:duration > 5ms }"
    s, e = _parse_dt(start), _parse_dt(end)
    data = await _get_json(
        settings.tempo_url,
        "/api/search",
        {"q": traceql, "start": _epoch_s(s), "end": _epoch_s(e), "limit": limit},
    )
    traces = data.get("traces", []) if isinstance(data, dict) else []
    out = [
        {
            "traceID": t.get("traceID"),
            "rootServiceName": t.get("rootServiceName"),
            "rootTraceName": t.get("rootTraceName"),
            "durationMs": t.get("durationMs"),
            "startTimeUnixNano": t.get("startTimeUnixNano"),
        }
        for t in traces
    ]
    return {"traces": out, "count": len(out)}


# ---- normalize -------------------------------------------------------------


def _node_kind(name: str, attrs: dict) -> str:
    if name.startswith("ChatGoogleGenerativeAI") or attrs.get("gen_ai.operation.name") == "chat":
        return "llm"
    if name.startswith("execute_tool") or "gen_ai.tool.name" in attrs:
        return "tool"
    if any(k.startswith("http.") for k in attrs) or name.startswith(("GET ", "POST ", "PUT ", "DELETE ")):
        return "http"
    return "business"


def _node_label(name: str, attrs: dict) -> str:
    """The 'agent/role' grouping label (left-pane GROUP by Agent)."""
    run = attrs.get("run_name") or attrs.get("traceloop.entity.name")
    if run:
        return str(run)
    node = attrs.get("langgraph_node")
    if node:
        return str(node)
    return name


def _payload(kind: str, attrs: dict, *, compact: bool) -> dict:
    p: dict[str, Any] = {}
    if kind == "llm":
        p["input_messages"] = _maybe_json(attrs.get("gen_ai.input.messages"))
        p["output_messages"] = _maybe_json(attrs.get("gen_ai.output.messages"))
        p["system_instructions"] = _maybe_json(attrs.get("gen_ai.system_instructions"))
        fr = attrs.get("gen_ai.response.finish_reasons")
        p["finish_reasons"] = fr if isinstance(fr, list) else ([fr] if fr else [])
    elif kind == "tool":
        p["tool_name"] = attrs.get("gen_ai.tool.name")
        p["arguments"] = _maybe_json(attrs.get("gen_ai.tool.call.arguments"))
        p["result"] = _maybe_json(attrs.get("gen_ai.tool.call.result"))
    else:
        # http / business: keep a few low-noise, high-signal attributes.
        keep = (
            "http.method", "http.route", "http.target", "http.status_code",
            "http.request.method", "http.response.status_code", "url.path",
            "status", "app.outcome", "app.fail_reason", "git_version",
        )
        p["attributes"] = {k: attrs[k] for k in keep if k in attrs}
    if compact:
        # Drop the giant tool-definitions blob (never in payload) and shrink text.
        p = _truncate(p)
    return p


def _normalize_trace(raw: dict, *, compact: bool = False) -> dict:
    """Turn Tempo OTLP-JSON into a node tree + rollups."""
    nodes: dict[str, dict] = {}
    order: list[str] = []
    for batch in raw.get("batches", []):
        res = _flatten(batch.get("resource", {}).get("attributes", []))
        for ss in batch.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                attrs = _flatten(sp.get("attributes", []))
                name = sp.get("name", "")
                kind = _node_kind(name, attrs)
                start_ns = int(sp.get("startTimeUnixNano", 0) or 0)
                end_ns = int(sp.get("endTimeUnixNano", 0) or 0)
                in_tok = int(attrs.get("gen_ai.usage.input_tokens", 0) or 0)
                out_tok = int(attrs.get("gen_ai.usage.output_tokens", 0) or 0)
                cache_tok = int(attrs.get("gen_ai.usage.cache_read.input_tokens", 0) or 0)
                model = attrs.get("gen_ai.request.model") or attrs.get("gen_ai.response.model")
                status = sp.get("status") or {}
                is_error = status.get("code") == "STATUS_CODE_ERROR" or attrs.get("error") is True
                sid = sp.get("spanId")
                nodes[sid] = {
                    "span_id": sid,
                    "parent_id": sp.get("parentSpanId") or None,
                    "name": name,
                    "kind": kind,
                    "label": _node_label(name, attrs),
                    "service": res.get("service.name"),
                    "model": model,
                    "provider": attrs.get("gen_ai.provider.name"),
                    "operation": attrs.get("gen_ai.operation.name"),
                    "duration_ms": round((end_ns - start_ns) / 1e6, 2) if end_ns and start_ns else None,
                    "start_ns": start_ns,
                    "input_tokens": in_tok or None,
                    "output_tokens": out_tok or None,
                    "cache_read_tokens": cache_tok or None,
                    "cost": _cost(model, in_tok, out_tok, cache_tok) if kind == "llm" else None,
                    "error": bool(is_error),
                    "payload": _payload(kind, attrs, compact=compact),
                    "children": [],
                }
                order.append(sid)

    # Link children; roots = spans whose parent isn't in this trace.
    roots: list[dict] = []
    for sid in order:
        n = nodes[sid]
        parent = nodes.get(n["parent_id"])
        (parent["children"] if parent else roots).append(n)

    # Sort children by start time so the tree reads top-to-bottom in call order.
    for n in nodes.values():
        n["children"].sort(key=lambda c: c["start_ns"])
    roots.sort(key=lambda c: c["start_ns"])

    llm_nodes = [n for n in nodes.values() if n["kind"] == "llm"]
    total_in = sum(n["input_tokens"] or 0 for n in llm_nodes)
    total_out = sum(n["output_tokens"] or 0 for n in llm_nodes)
    total_cache = sum(n["cache_read_tokens"] or 0 for n in llm_nodes)
    costs = [n["cost"] for n in llm_nodes if n["cost"] is not None]
    rollup = {
        "span_count": len(nodes),
        "llm_calls": len(llm_nodes),
        "tool_calls": sum(1 for n in nodes.values() if n["kind"] == "tool"),
        "error_count": sum(1 for n in nodes.values() if n["error"]),
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_tokens": total_cache,
        "total_tokens": total_in + total_out,
        "cost": round(sum(costs), 6) if costs else None,
        "models": sorted({n["model"] for n in llm_nodes if n["model"]}),
    }
    return {"roots": roots, "rollup": rollup}


async def get_trace(trace_id: str, *, compact: bool = False) -> dict:
    raw = await _get_json(settings.tempo_url, f"/api/traces/{trace_id}", {})
    tree = _normalize_trace(raw, compact=compact)
    tree["traceID"] = trace_id
    return tree


# ---- AI analysis -----------------------------------------------------------

_ANALYSIS_PROMPT = """You analyse a single distributed/LLM trace and give the
on-call engineer ONE short, punchy verdict (max ~25 words). Focus on what stands
out: retry loops or many generations, errors, the dominant token/cost or latency
contributor. State a number when it matters. No preamble, no markdown."""


def _analysis_llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model, google_api_key=settings.google_api_key, temperature=0
    ).with_config({"run_name": "Trace_Analysis"})


async def analyze_trace(trace_id: str) -> str:
    tree = await get_trace(trace_id, compact=True)
    res = await _analysis_llm().ainvoke(
        [
            SystemMessage(content=_ANALYSIS_PROMPT),
            HumanMessage(content="Trace (normalized JSON):\n" + json.dumps(tree, ensure_ascii=False)),
        ]
    )
    # gemini-3.x returns multipart content (text + thinking blocks); flatten to text.
    return _flatten_content(res.content).strip()


# ---- chat about this trace -------------------------------------------------

_TRACE_CHAT_PROMPT = """You help an engineer understand ONE trace they are
looking at. The full normalized trace (spans, LLM calls with prompts/completions,
tool calls with args/results, token usage, cost, errors) is given below as JSON —
it is your ONLY source of truth. Answer strictly from it; if something isn't in
the trace, say so. Be concise, cite concrete span names / numbers / tokens / cost.
Reply in the user's language."""


def _trace_chat_llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model, google_api_key=settings.google_api_key, temperature=0
    ).with_config({"run_name": "Trace_Chat"})


async def stream_trace_chat(
    trace_id: str, message: str, history: list[dict] | None = None
) -> AsyncIterator[dict]:
    """SSE generator for the side chat. Emits the same event shapes the plugin's
    SSE reader already understands (`token` / `final` / `done`)."""
    try:
        tree = await get_trace(trace_id, compact=True)
    except Exception as e:  # trace gone / Tempo hiccup — tell the user, don't 500
        logger.warning("trace fetch for chat failed: %s", e)
        yield {"type": "token", "text": f"無法載入這條 trace（{trace_id}）：{e}"}
        yield {"type": "done"}
        return

    msgs: list = [
        SystemMessage(content=_TRACE_CHAT_PROMPT),
        SystemMessage(content="Trace JSON:\n" + json.dumps(tree, ensure_ascii=False)),
    ]
    for h in history or []:
        role, text = h.get("role"), h.get("text", "")
        msgs.append(AIMessage(content=text) if role == "assistant" else HumanMessage(content=text))
    msgs.append(HumanMessage(content=message))

    streamed = False
    try:
        async for chunk in _trace_chat_llm().astream(msgs):
            text = chunk.content if isinstance(chunk.content, str) else "".join(
                b.get("text", "") for b in chunk.content if isinstance(b, dict)
            )
            if text:
                streamed = True
                yield {"type": "token", "text": text}
    except Exception as e:
        logger.warning("trace chat stream failed: %s", e)
        if not streamed:
            yield {"type": "token", "text": f"分析失敗：{e}"}
    yield {"type": "done"}
