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
from pydantic import BaseModel, Field

from .config import settings
from .tools import github_compare, github_get_file, wrap_with_cap

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

# Reply in the user's language

**Hard rule: match the user's language.** If the user wrote in Traditional /
Simplified Chinese, reply in that. If they wrote in English, reply in English.
Mixing — English bullet points under a Chinese question, or vice versa — is
wrong. Detect the language from the most recent user message; do not default
to English.

# Time

Current real-world clock: **{now}**. The telemetry datastore holds the last 24h
ending at approximately `now`. Older data does not exist.

For **time-range arguments** on tool calls (`startTime`, `endTime`, `startRfc3339`,
`endRfc3339`, `start`, `end`): you may write either:

- A literal RFC3339 UTC timestamp computed from `{now}` (e.g. `{now}` minus 1h).
- The shorthand `now`, `now-30s`, `now-15m`, `now-1h`, `now-2d` — the wrapper
  will expand these into RFC3339 for you. Both forms work identically.

**Do not** hardcode calendar dates from your training data ("2024-XX-XX",
"2025-XX-XX"). Use `{now}` or the `now-...` shorthand.

Loki rejects ranges longer than ~30 days. Keep windows ≤ 6h unless you have a
specific reason; default to 1h.

**Do not ask the user for a time range.** If the question doesn't specify one,
silently use the last 1h and state that window in your answer. Asking back is
not allowed — the user is in an incident and wants the check, not a dialogue.

**Do not ask the user to specify tool parameters either** (`stepSeconds`,
`rateInterval`, `limit`, percentile, etc.). Pick a sensible default and run
the query. Sensible defaults: `stepSeconds=60`, `rateInterval="5m"`,
percentile `0.95` if unspecified, `limit=100`. If a tool returns an error
about a missing parameter, do NOT come back to the user — re-issue the call
with the default filled in.

# Query budget & stopping criteria

- **Default budget: 1–2 tool calls per turn.** Start with the single most
  informative query for the question. Only call a second tool if the first
  result genuinely demands a follow-up (e.g. metrics show a spike → pivot to
  logs / traces for that window). Do not "round out the investigation" by
  pre-emptively listing every possibly-relevant metric.
- **Hard ceiling: 4 tool calls per turn.** If you hit 4 without a clear signal,
  stop and report what you checked.
- **Empty results are a valid answer.** Zero rows / empty vector means "no
  signal in this window" — report it as a finding, do not assume the tool is
  broken.
- **Never retry the same query unchanged.** If you retry, change something
  meaningful: the time window, the matcher, the metric, the LogQL pipeline.

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
| Code diff between two deploy versions | `github_compare(repo, base, head)` |
| Read a slice of a file at a specific ref | `github_get_file(repo, path, ref, start, end)` |

Default ordering for an RCA question:

1. **Metrics first** — narrow the window with `http_requests_total` error rate or
   `service_retry_queue_depth` / `service_cache_refresh_lag_seconds` gauges. This
   gives you a service and a time range cheaply.
2. **Traces next** — `{{ resource.service.name = "<service>" && span:status = error }}`
   confirms the origin service and gives you `trace_id`s to look up.
3. **Logs last** — pivot on `trace_id` or service+level to read the actual error
   message. Always aggregate first (`count_over_time` by error pattern), then drill
   into raw lines only when needed.
4. **Deploy correlation** — if a deployment log (`event="deployment"`) sits in or
   just before the incident window, `github_compare` the old→new version on the
   service's repo (see catalog) and look for a suspicious change. Cite the SHA in
   your final answer. Skip this step if there's no deploy event nearby.

# Anti-patterns (don't do these)

- Selectors like `{{app="..."}}` or `{{container="..."}}` — the only Loki labels here
  are `job`, `service`, `level` (see catalog).
- Fetching > 100 raw log lines to "look for errors" — write a LogQL pipeline that
  aggregates by error message or status instead.
- Calling `list_loki_label_values` for `service` — the catalog already lists every
  service.
- **Querying `up{{service_name="..."}}` as a liveness check.** This metric does not
  exist for application services in this stack (see Prometheus section of the
  catalog). It returns empty whether the service is healthy or dead, so it is
  not a signal. Use a rate over a counter the service actually emits.
- Synthesizing an answer without citing the exact queries you ran.

# Answer style

Speak naturally — like a colleague answering across the desk, not a report.
Prose, not bullet lists, for short answers. Lead with the answer, give the
numbers that matter, skip the "I checked the metrics and logs..." preamble.
Remember: reply in the language the user asked in (see the "Reply in the
user's language" rule at the top).

**Always cite concrete numbers with units** (e.g. "p95 ≈ 48 ms", "request rate
~1 req/s", "0 ERROR logs"). Vague claims like "回應速度正常" without a number
are not useful.

## Charts (important)

The Grafana plugin **auto-renders** any fenced ```` ```promql ```` block in
your answer as a live time-series panel. This is the main way the user sees
metric data — your prose explains, the chart shows.

Rules:

- **If your answer cites a metric value, you MUST include the underlying
  PromQL in a ```` ```promql ```` block.** One block per chart. The user
  reads the prose and watches the chart side by side.
- **Keep charts focused** — 1 chart for a simple status check, up to 3 for
  an investigation. Do not dump every query you ran. Pick the ones that
  carry the story.
- **No `Queries run:` heading.** Just put the ```` ```promql ```` blocks at
  the end of your prose. The chart speaks for itself.
- **Logs-only answers don't need charts** — e.g. "no ERROR logs in last 1h"
  can just be a sentence with the LogQL in a ```` ```logql ```` block
  (plugin does not render LogQL as a chart, but the code is still useful).

Format example (casual metric question):
```
p95 在過去一小時大概 48 ms，蠻穩的。

` ` `promql
histogram_quantile(0.95, sum by (le) (rate(http_server_duration_milliseconds_bucket{{service_name="order-service"}}[5m])))
` ` `
```

Format example (incident with multiple signals):
```
payment-service 在 14:05 後 decline 率從 0% 跳到 18%，全集中在 v2.5.0、
reason 是 `new_validator_odd_cents`。看起來跟新部署的 validator 有關。

` ` `promql
sum by (git_version, reason) (rate(payment_charges_total{{status="declined"}}[5m]))
` ` `
```

(Real output uses triple backticks, not spaced.)

# Schema catalog

{schema_catalog}
"""


def build_system_prompt() -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        now=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        schema_catalog=SCHEMA_CATALOG,
    )


INTENT_SYSTEM_PROMPT = """You are an intent gate for an AIOps / observability assistant.

The assistant ONLY helps an on-call SRE investigate production issues using the
Grafana stack (Prometheus metrics, Loki logs, Tempo traces) and correlate them
with GitHub deploys. In-scope intents include:

- Asking about service health, error rates, latency (p95/p99), throughput.
- Investigating an incident / outage / alert / anomaly.
- Reading or aggregating logs, metrics, or traces.
- Root-cause analysis, deploy correlation, "what changed", "why is X slow".
- Questions about the telemetry data, dashboards, or the observability stack itself.

OUT of scope (must be rejected):

- General chit-chat, jokes, opinions, role-play.
- Coding help unrelated to investigating this system, general knowledge questions.
- Anything that has nothing to do with operating/observing this system.

Judge ONLY the latest user message (use prior context only to disambiguate
follow-ups like "and the logs?"). Set in_scope=true only if it is an AIOps /
observability request.
"""

# Fixed refusal text. Deliberately NOT generated by the LLM: the classifier only
# returns a bool, so a prompt-injected user message cannot turn this gate into a
# "user input -> LLM -> rendered output" echo channel.
REFUSAL_TEXT = (
    "我只能協助可觀測性與事件調查（metrics、logs、traces、根因分析）。"
    "請問你想查哪個服務或哪個指標？\n"
    "(I can only help with observability and incident investigation — "
    "metrics, logs, traces. Which service or signal would you like to look into?)"
)


class IntentResult(BaseModel):
    """Structured output for the AIOps intent gate."""

    reasoning: str = Field(default="", description="Brief reasoning for the decision.")
    in_scope: bool = Field(..., description="True if the message is an AIOps/observability request.")


_intent_llm = ChatGoogleGenerativeAI(
    model=settings.gemini_model,
    google_api_key=settings.google_api_key,
    temperature=0,
)
_intent_classifier = _intent_llm.with_structured_output(IntentResult).with_config(
    {"run_name": "AIOps_Intent_Gate"}
)


async def classify_intent(message: str) -> IntentResult:
    """Classify whether a user message is an in-scope AIOps/observability request."""
    return await _intent_classifier.ainvoke(
        [
            SystemMessage(content=INTENT_SYSTEM_PROMPT),
            {"role": "user", "content": message},
        ]
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
    mcp_tools = await _mcp_client.get_tools()
    # v2: wrap query_loki_logs / query_tempo_traces / query_prometheus so a
    # raw output that blows the byte cap is replaced with a schema-aware
    # aggregation (sum by service_name/level/event/git_version) instead of
    # being head-N truncated. See tools/wrap.py.
    wrapped_mcp = [wrap_with_cap(t) for t in mcp_tools]
    tools = wrapped_mcp + [github_compare, github_get_file]
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

    # Intent gate: reject anything outside the AIOps / observability scope before
    # spending any MCP tool calls or LLM turns on it.
    # fail-closed: if the classifier errors we refuse rather than let an
    # unclassified message reach the tools. An attacker who can force the
    # classify call to fail must not thereby bypass the gate.
    try:
        intent = await classify_intent(message)
    except Exception as e:
        logger.warning("Intent gate failed, refusing (fail-closed): %s", e)
        intent = IntentResult(in_scope=False)

    if not intent.in_scope:
        yield {"type": "token", "text": REFUSAL_TEXT}
        yield {"type": "done"}
        return

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
