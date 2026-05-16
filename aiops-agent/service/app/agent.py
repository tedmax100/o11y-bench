import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator

from langchain_core.messages import SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode, create_react_agent

from .config import settings

logger = logging.getLogger("aiops_agent")
DEBUG_EVENTS = os.getenv("DEBUG_EVENTS", "0") == "1"

SCHEMA_CATALOG = (Path(__file__).parent / "schema_catalog.md").read_text(encoding="utf-8")


def _flatten_content(content) -> str:
    """LangChain message content can be a string OR a list of content blocks
    (Gemini / Anthropic multipart). Always reduce to plain text for the wire."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)

SYSTEM_PROMPT_TEMPLATE = """You are an AIOps assistant helping an on-call SRE investigate
issues using the Grafana stack (Prometheus, Loki, Tempo) via the grafana-mcp tools.

# Time

Current real-world clock: **{now}**. The telemetry datastore holds the last 24h
ending at approximately `now`. Older data does not exist.

**Always express time ranges relative to `now`** (e.g. `now-1h`, `now-3h..now-2h`).
**Do not hardcode calendar dates** — your training-cutoff intuition about "today"
is wrong here. For Loki tools that need RFC3339 (`startRfc3339` / `endRfc3339`),
compute the timestamps from the `{now}` above; never invent a date like
"2024-XX-XX" or "2025-XX-XX".

Loki rejects ranges longer than ~30 days. Keep windows ≤ 6h unless you have a
specific reason; default to 1h.

# Workflow

1. **State a hypothesis** before each query. Name the service and signal you expect
   to see. Update the hypothesis as evidence comes in.
2. **Filter and aggregate at the datasource** — LogQL / PromQL / TraceQL can almost
   always express what you need. Pulling raw logs / spans into your context is the
   last resort, not the first move.
3. **Project only the fields you need**. For Loki, use `| line_format` to keep just
   the fields you'll cite. For Prometheus, `sum by (...)` or `topk(...)`. For Tempo,
   use TraceQL predicates rather than fetching every span.
4. **Recover from tool errors**. If a tool returns an error (e.g. range too long,
   bad LogQL), read the error, fix the parameter, retry. Do not give up after one
   bad call.
5. **Cite the exact query** you ran in your final answer so the user can re-run it.

# Tool routing

Pick the tool by what kind of signal you need. Don't call discovery tools
(`list_*_label_names`, `list_*_label_values`) when the catalog below already tells
you the answer.

| Need | Use |
|------|-----|
| Logs (errors, warnings, request lines, deployment events) | `query_loki_logs` with LogQL |
| Metrics (rates, p95 latency, error ratios, gauge spikes) | `query_prometheus` with PromQL |
| Traces (find root cause service, slow operations) | `query_tempo_traces` with TraceQL |
| Dashboards / datasources discovery | `list_datasources`, `search_dashboards` |

Default ordering for an RCA question:

1. **Metrics first** — narrow the window with `http_requests_total` error rate or
   `service_retry_queue_depth` / `service_cache_refresh_lag_seconds` gauges. This
   gives you a service and a time range cheaply.
2. **Traces next** — `{{ resource.service.name = "<service>" && span:status = error }}`
   confirms the origin service and gives you `trace_id`s to look up.
3. **Logs last** — pivot on `trace_id` or service+level to read the actual error
   message. Always aggregate first (`count_over_time` by error pattern), then drill
   into raw lines only when needed.

# Anti-patterns (don't do these)

- Selectors like `{{app="..."}}` or `{{container="..."}}` — the only Loki labels here
  are `job`, `service`, `level` (see catalog).
- Fetching > 100 raw log lines to "look for errors" — write a LogQL pipeline that
  aggregates by error message or status instead.
- Calling `list_loki_label_values` for `service` — the catalog already lists every
  service.
- Synthesizing an answer without citing a query.

# Schema catalog

{schema_catalog}
"""


def build_system_prompt() -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        now=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        schema_catalog=SCHEMA_CATALOG,
    )


_mcp_client: MultiServerMCPClient | None = None
_agent = None


async def _build_agent():
    global _mcp_client, _agent
    if _agent is not None:
        return _agent

    _mcp_client = MultiServerMCPClient(
        {
            "grafana": {
                "url": settings.mcp_grafana_url,
                "transport": "streamable_http",
            }
        }
    )
    tools = await _mcp_client.get_tools()
    # handle_tool_errors=True turns ToolException into a ToolMessage the LLM can
    # read and recover from, instead of bubbling up and terminating the run.
    tool_node = ToolNode(tools, handle_tool_errors=True)

    llm = ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.google_api_key,
        temperature=0,
    )

    def prompt_fn(state):
        return [SystemMessage(content=build_system_prompt())] + state["messages"]

    _agent = create_react_agent(
        llm,
        tool_node,
        prompt=prompt_fn,
        checkpointer=MemorySaver(),
    )
    return _agent


@asynccontextmanager
async def lifespan(app):
    await _build_agent()
    yield


async def stream_chat(message: str, thread_id: str) -> AsyncIterator[dict]:
    """Yield LangGraph events as dicts. Caller serializes to SSE."""
    agent = await _build_agent()
    config = {"configurable": {"thread_id": thread_id}}

    async for event in agent.astream_events(
        {"messages": [{"role": "user", "content": message}]},
        config=config,
        version="v2",
    ):
        kind = event.get("event")
        name = event.get("name", "")
        data = event.get("data", {})

        if DEBUG_EVENTS:
            logger.warning("event=%s name=%s data_keys=%s", kind, name, list(data.keys()))

        if kind == "on_chat_model_stream":
            chunk = data.get("chunk")
            text = _flatten_content(getattr(chunk, "content", None)) if chunk is not None else ""
            if text:
                yield {"type": "token", "text": text}

        elif kind == "on_tool_start":
            yield {
                "type": "tool_start",
                "tool": name,
                "input": data.get("input"),
            }

        elif kind == "on_tool_end":
            output = data.get("output")
            preview = str(output)[:500] if output is not None else ""
            yield {
                "type": "tool_end",
                "tool": name,
                "output_preview": preview,
            }

        elif kind == "on_chain_end" and name == "LangGraph":
            # Fallback: if streaming tokens didn't fire, emit the final message text
            output = data.get("output", {})
            messages = output.get("messages", []) if isinstance(output, dict) else []
            if messages:
                last = messages[-1]
                raw = getattr(last, "content", None)
                if raw is None and isinstance(last, dict):
                    raw = last.get("content")
                text = _flatten_content(raw)
                if text:
                    yield {"type": "final", "text": text}

    yield {"type": "done"}
