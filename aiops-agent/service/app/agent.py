import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, AsyncIterator, TypedDict

from langchain_core.messages import SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from .capability import capability_for_services, resolve_services
from .config import settings
from .tools import (
    discover_log_fields_tool,
    discover_metrics_tool,
    discover_span_names_tool,
    github_compare,
    github_get_file,
    query_loki_logs,
    query_prometheus,
    query_tempo_traces,
)

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
issues using the Grafana stack — querying Prometheus, Loki and Tempo directly.

# Reply in the user's language

**Hard rule: match the user's language.** If the user wrote in Traditional /
Simplified Chinese, reply in that. If they wrote in English, reply in English.
Mixing — English bullet points under a Chinese question, or vice versa — is
wrong. Detect the language from the most recent user message; do not default
to English.

# Time

The **current real-world clock is given at the very end of this prompt** (see
*Current time*). The telemetry datastore holds the last 24h ending at
approximately that time. Older data does not exist.

For **time-range arguments** on tool calls (`startTime`, `endTime`, `startRfc3339`,
`endRfc3339`, `start`, `end`): you may write either:

- A literal RFC3339 UTC timestamp computed from the current time (e.g. the
  current time minus 1h).
- The shorthand `now`, `now-30s`, `now-15m`, `now-1h`, `now-2d` — the wrapper
  will expand these into RFC3339 for you. Both forms work identically.

**Do not** hardcode calendar dates from your training data ("2024-XX-XX",
"2025-XX-XX"). Use the current time or the `now-...` shorthand.

Loki rejects ranges longer than ~30 days. Keep windows ≤ 6h unless you have a
specific reason; default to 1h.

**Do not ask the user for a time range.** If the question doesn't specify one,
silently use the last 1h and state that window in your answer. Asking back is
not allowed — the user is in an incident and wants the check, not a dialogue.

**Do not ask the user to specify tool parameters either** (`stepSeconds`,
`rateInterval`, `limit`, percentile, etc.). Pick a sensible default and run
the query. Sensible defaults: `stepSeconds=60`, `rateInterval="5m"`,
percentile `0.95` if unspecified, `limit=100`. **Never use a rate window below
`[5m]`** — metrics export every ~60s, so `rate(...[1m])` returns empty. If a tool returns an error
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

Pick the tool by what kind of signal you need. The catalog below covers the
common cases; when it doesn't name the metric / span / field you need for a
service, use a discovery tool to look it up against the live data instead of
guessing.

| Need | Use |
|------|-----|
| Logs (errors, warnings, request lines, deployment events) | `query_loki_logs` with LogQL |
| Metrics (rates, p95 latency, error ratios, gauge spikes) | `query_prometheus` with PromQL |
| Traces (find root cause service, slow operations) | `query_tempo_traces` with TraceQL |
| Which metrics does a service emit? | `discover_metrics(service)` |
| Which span/operation names does a service have? | `discover_span_names(service)` |
| Which log fields can I filter/group by? | `discover_log_fields(service)` |
| Code diff between two deploy versions | `github_compare(repo, base, head)` |
| Read a slice of a file at a specific ref | `github_get_file(repo, path, ref, start, end)` |

Default ordering for an RCA question:

1. **Metrics first** — narrow the window with an HTTP error-rate or latency
   metric for the service (use the exact metric name from the live capability
   snapshot, e.g. the `*_duration_milliseconds_count` / `*_total` it actually
   emits). This gives you a service and a time range cheaply.
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

- Loki stream selectors other than the indexed ones — the only `{{...}}`-selectable
  labels are `service_name`, `git_repo`, `git_version`, `deployment_environment`.
  `level` / `event` / business fields are structured metadata: filter them with
  `| level="ERROR"` *after* the selector, not inside `{{...}}`.
- Fetching > 100 raw log lines to "look for errors" — write a LogQL pipeline that
  aggregates by error message or status instead.
- Guessing a metric / span / field name when the catalog doesn't list it — call
  the matching `discover_*` tool to get the real names first.
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

## Panels (important)

The Grafana plugin **auto-renders** fenced query blocks in your answer as live
panels. This is the main way the user sees the data — your prose explains, the
panel shows. Three fence types render:

- ```` ```promql ```` → live time-series chart (Prometheus).
- ```` ```logql ```` → live **logs panel** showing the actual log lines (Loki).
- ```` ```traceql ```` → live **traces table** of matching traces (Tempo).

Rules:

- **Whenever your answer is backed by a signal, include the underlying query
  in the matching fence** so the user sees the panel: a metric value → a
  ```` ```promql ```` block; a logs answer ("recent errors", "last N lines",
  "no ERROR logs") → a ```` ```logql ```` block; a traces answer ("slow/error
  traces", "which traces") → a ```` ```traceql ```` block. One block per panel.
- **For a "show me the logs / traces" request, the panel IS the answer.** Don't
  reply with only prose and a suggested query — emit the ```` ```logql ````
  / ```` ```traceql ```` block that actually returns what they asked for (e.g.
  the last N lines), so the panel renders it.
- **If the user asks for a specific count** ("近10筆 log", "3 筆 trace"), put
  that number on the fence info line so the panel shows exactly that many:
  ```` ```logql 10 ```` or ```` ```traceql 3 ````. The number is the panel's
  line/row limit. Omit it for a default view (logs 100, traces 20).
- **Keep it focused** — 1 panel for a simple check, up to 3 for an
  investigation. Don't dump every query you ran; pick the ones that carry the story.
- **No `Queries run:` heading.** Just put the fenced blocks at the end of your
  prose. The panel speaks for itself.

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

# Current time

The current real-world clock is **{now}** (UTC). Compute all relative time
ranges (the `now-...` shorthand, "past 1h", etc.) from this value.

NOTE: everything above this line is identical on every call — only this
timestamp changes — so the model provider can serve the prefix from its context
cache. Keep volatile values out of the prompt body above.
"""


def build_system_prompt() -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        now=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        schema_catalog=SCHEMA_CATALOG,
    )


# The output contract the model must keep on *every* turn. Pulled out so the
# post-tool prompts can restate it cheaply without re-sending the full ~4k
# system prompt (template + schema catalog).
_OUTPUT_CONTRACT = """Output contract:
- Reply in the user's language (the language of their question).
- Lead with the answer; cite concrete numbers with units (e.g. "p95 ≈ 48 ms").
- Back each signal with the matching fenced block so the panel renders:
  ```promql``` (metric), ```logql``` (logs), ```traceql``` (traces). 1 block per panel.
- Don't ask the user for time ranges or tool parameters — use sensible defaults
  (1h window, stepSeconds=60, rateInterval="5m", p95, limit=100)."""

# ReAct continuation prompt. After the first agent step the model already has
# the question, the live capability snapshot and the tool results in the
# conversation, so we DON'T re-send the full system prompt (template + catalog,
# ~4k tokens) on every loop — only this short reminder. This is the single
# biggest input-token saving: the post-tool agent call drops from ~7k to ~3k.
CONTINUE_PROMPT = (
    "Continue the investigation. You've already run a tool this turn — read the "
    "latest tool result in the conversation and either run ONE more focused query "
    "(only if the result clearly demands it and budget remains) or answer now.\n\n"
    + _OUTPUT_CONTRACT
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

# Mode (only when in_scope)

Also classify HOW the request should be served:

- `lookup` — a single, self-contained "show me / what is" request that one query
  answers: a metric value or chart ("p95 latency of order-service", "error rate
  now"), "show me the last N error logs", "recent traces for X". One signal, no
  reasoning across multiple queries needed.
- `investigate` — needs reasoning across signals or drill-down: "why is X slow",
  "what changed", root-cause, deploy correlation, anomaly hunting, anything
  comparing/pivoting (metrics→logs→traces) or likely to need follow-up queries.

When unsure, prefer `investigate` (it can always answer a simple question too;
the reverse is not true)."""

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
    mode: str = Field(
        default="investigate",
        description="'lookup' for a single-query show-me request, else 'investigate'.",
    )


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


# ---- fast path (lookup mode) -----------------------------------------------
# A "show me this metric/logs/traces" question doesn't need the ReAct tool loop:
# the agent's real job is translating the natural-language question into the
# right query, and the plugin renders the panel by re-running that query in
# Grafana. So for `lookup` mode we do ONE LLM call that emits the fenced query
# block(s) as the answer — no tool execution, no second interpretation call.
# Investigations (reasoning across signals, drill-down) still take the full
# graph below. Misclassified lookups self-correct: the follow-up chips a user
# clicks are reclassified next turn and escalate to `investigate`.
FAST_PATH_PROMPT = """You are an AIOps assistant. The user wants to SEE a single
signal (a metric, some logs, or traces). Do NOT call tools and do NOT investigate
— just translate their question into the correct query and let the panel show it.

Reply in the user's language. Output: one short sentence naming what the panel
shows, then the matching fenced block (the plugin renders it live):

- metric → ```promql``` (p95/p99 latency → `histogram_quantile(0.95, sum by (le) (rate(<metric>_bucket[5m])))`; rates/QPS → `sum(rate(<counter>_total[5m]))`)
  **Rate windows MUST be ≥ 5m.** Metrics are exported every ~60s, so `rate(...[1m])`
  has too few samples and returns EMPTY. Always use `[5m]` (never `[1m]`/`[30s]`).
- logs   → ```logql```  (only `service_name`/`git_repo`/`git_version`/`deployment_environment` go inside `{...}`; filter `level`/`event`/business fields AFTER with `| level="ERROR"`)
- traces → ```traceql``` (attrs are dotted: `{ resource.service.name = "X" && status = error }`)

Rules:
- **Always scope the query to the service the user named** — add the label
  selector `{service_name="<svc>"}` (Prometheus/Loki) or
  `resource.service.name="<svc>"` (Tempo). The `http_server_*` metrics are
  shared across all services; without the label the panel shows everything.
- Use the EXACT metric / span / field names from the live capability snapshot
  if one is provided; don't guess.
- **Do NOT state specific numbers** ("p95 ≈ 48 ms") — you have not measured them;
  the panel shows the actual values. Describe what it shows, not invented values.
- If the user asked for a specific count ("近10筆 log"), put it on the fence:
  ```logql 10``` / ```traceql 3```.
- Default window 1h; don't ask the user for parameters."""

_fast_llm = ChatGoogleGenerativeAI(
    model=settings.gemini_model,
    google_api_key=settings.google_api_key,
    temperature=0,
).with_config({"run_name": "AIOps_FastPath"})


async def stream_fast_path(message: str, snapshot: str | None) -> AsyncIterator[str]:
    """One LLM call: NL question -> fenced query block(s). Yields text chunks."""
    msgs: list = [SystemMessage(content=FAST_PATH_PROMPT)]
    if snapshot:
        msgs.append(SystemMessage(content=snapshot))
    msgs.append({"role": "user", "content": message})
    async for chunk in _fast_llm.astream(msgs):
        text = _flatten_content(getattr(chunk, "content", None))
        if text:
            yield text


# v3: query tools talk to the Prometheus/Loki/Tempo native HTTP APIs directly
# (the agent runs in-cluster and reaches them over internal DNS). The byte-cap +
# schema-aware aggregation fallback that used to wrap the mcp-grafana tools now
# lives inside these tools. See tools/query.py.
TOOLS = [
    query_prometheus,
    query_loki_logs,
    query_tempo_traces,
    discover_metrics_tool,
    discover_span_names_tool,
    discover_log_fields_tool,
    github_compare,
    github_get_file,
]


class RcaState(TypedDict):
    """State for the RCA graph. `tool_calls_used` is reset to 0 on each turn's
    input (overwrite reducer), so the budget is per-turn, not per-thread, even
    though `messages` accumulates across the thread (add_messages reducer)."""

    messages: Annotated[list, add_messages]
    tool_calls_used: int
    budget: int


def _last_tool_calls(messages: list) -> list:
    last = messages[-1] if messages else None
    return list(getattr(last, "tool_calls", None) or [])


def _build_graph():
    """Explicit StateGraph replacing create_react_agent. Same agent↔tools ReAct
    loop, but with a *hard* tool-call budget: once `tool_calls_used` hits
    `budget` the graph routes to `force_answer` (LLM with no tools bound) so a
    headless run can't loop forever. See doc/aiops-agent-design-v3.md §4.3."""
    # handle_tool_errors=True turns ToolException into a ToolMessage the LLM can
    # read and recover from, instead of bubbling up and terminating the run.
    tool_node = ToolNode(TOOLS, handle_tool_errors=True)

    llm = ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.google_api_key,
        temperature=0,
    )
    llm_with_tools = llm.bind_tools(TOOLS)

    async def agent_node(state: RcaState):
        # First agent step: full system prompt (instructions + schema catalog).
        # Subsequent steps (a tool already ran this turn): only the short
        # continuation reminder — the snapshot + tool results are already in
        # `messages`, so re-sending the ~4k prompt every loop is pure waste.
        sys = build_system_prompt() if state["tool_calls_used"] == 0 else CONTINUE_PROMPT
        msgs = [SystemMessage(content=sys)] + state["messages"]
        return {"messages": [await llm_with_tools.ainvoke(msgs)]}

    async def tools_node(state: RcaState):
        # Count the calls this AIMessage requested *before* ToolNode runs them,
        # then fold that into the running total so the budget is enforced.
        n = len(_last_tool_calls(state["messages"]))
        out = await tool_node.ainvoke(state)
        return {"messages": out["messages"], "tool_calls_used": state["tool_calls_used"] + n}

    async def force_answer_node(state: RcaState):
        # Budget exhausted: answer with what we have. No tools bound, so the
        # model must produce text. Streams as on_chat_model_stream like any answer.
        # Budget is only ever exhausted *after* tools ran, so the snapshot +
        # tool results are already in `messages` — no need to re-send the full
        # system prompt here either; the short answer contract is enough.
        nudge = SystemMessage(
            content=(
                "You have used your tool-call budget for this turn. Do NOT call "
                "any more tools. Answer now with what you found so far, and state "
                "which checks you ran.\n\n" + _OUTPUT_CONTRACT
            )
        )
        msgs = state["messages"] + [nudge]
        return {"messages": [await llm.ainvoke(msgs)]}

    def route_after_agent(state: RcaState) -> str:
        if not _last_tool_calls(state["messages"]):
            return END  # model answered without (more) tools
        if state["tool_calls_used"] >= state["budget"]:
            return "force_answer"
        return "tools"

    graph = StateGraph(RcaState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("force_answer", force_answer_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent", route_after_agent, {"tools": "tools", "force_answer": "force_answer", END: END}
    )
    graph.add_edge("tools", "agent")
    graph.add_edge("force_answer", END)
    return graph.compile(checkpointer=MemorySaver())


_agent = None


async def _build_agent():
    global _agent
    if _agent is None:
        _agent = _build_graph()
    return _agent


# ---- structured Findings (seed for the push/webhook entrypoint) -------------
# A headless alert-driven run has no human reading the prose; the downstream
# runbook layer needs a machine-readable verdict. This model + helper are the
# seed for that (doc/aiops-agent-design-v3.md §4.3). NOT called on the chat hot
# path — chat returns prose only — so it adds no per-turn latency today. The
# webhook step (next) will call extract_findings() once at the end of a run.


class Findings(BaseModel):
    """Machine-readable conclusion of an RCA run."""

    summary: str = Field(description="One-line conclusion of the investigation.")
    hypothesis: str = Field(description="The leading root-cause hypothesis.")
    confidence: float = Field(description="0.0-1.0 confidence in the hypothesis.")
    evidence: list[str] = Field(
        default_factory=list, description="Concrete queries / values that support the conclusion."
    )
    services: list[str] = Field(
        default_factory=list, description="Service(s) implicated."
    )
    suspected_version: str | None = Field(
        default=None, description="git_version suspected of introducing the issue, if any."
    )


_FINDINGS_PROMPT = """Extract a structured RCA conclusion from the investigation
transcript below. Use ONLY what the transcript actually established — do not
invent evidence. If the run was inconclusive, say so in `summary` and give a low
`confidence`. `evidence` should quote the concrete queries/values that were run."""

_findings_llm = (
    ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.google_api_key,
        temperature=0,
    )
    .with_structured_output(Findings)
    .with_config({"run_name": "AIOps_Findings_Extractor"})
)


async def extract_findings(messages: list) -> Findings:
    """Distill a finished run's messages into a structured Findings. Used by the
    headless (alert webhook) path; not wired into chat."""
    return await _findings_llm.ainvoke([SystemMessage(content=_FINDINGS_PROMPT)] + messages)


# ---- follow-up suggestions (the "Follow-up" chips under each answer) --------
# After an answer, propose 2-3 concrete next questions an SRE would click to go
# deeper. Emitted as a `suggestions` SSE event; the plugin renders them as chips.


class FollowUps(BaseModel):
    suggestions: list[str] = Field(
        default_factory=list,
        description="2-3 short follow-up questions the user might ask next.",
    )


_FOLLOWUP_PROMPT = """Given an on-call SRE's question and the assistant's answer,
propose 2-3 SHORT follow-up questions the SRE would naturally click to go deeper.

Rules:
- Each is a concrete next investigative step grounded in THIS answer: drill into
  the errors, compare the suspected versions' diff, check a dependent/upstream
  service, widen or shift the time window, or pivot signal (metric→logs→traces).
- Phrase each as the user would type it, in the SAME language as the question.
- Keep each under ~12 words. No numbering, no preamble.
- Don't restate what was already answered, and stay within observability/RCA."""

_followup_llm = (
    ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.google_api_key,
        temperature=0.3,
    )
    .with_structured_output(FollowUps)
    .with_config({"run_name": "AIOps_FollowUp_Suggester"})
)


async def suggest_followups(user_message: str, answer: str) -> list[str]:
    """Propose next-step questions from the Q/A pair. Cheap: only the user
    question + final answer are sent, not the full tool transcript."""
    res = await _followup_llm.ainvoke(
        [
            SystemMessage(content=_FOLLOWUP_PROMPT),
            {"role": "user", "content": f"Question:\n{user_message}\n\nAnswer:\n{answer}"},
        ]
    )
    return [s.strip() for s in res.suggestions if s.strip()][:3]


@asynccontextmanager
async def lifespan(app):
    await _build_agent()
    yield


CLARIFY_PROMPT = "你是指哪一個服務？(Which service do you mean?)"

# Progress phases surfaced to the UI as `status` events so the user can see the
# agent is still working and where it is. `phase` is the machine key (for icons /
# i18n later); `label` is what renders today.
STATUS_LABELS = {
    "understanding": "理解問題中…",
    "locating": "鎖定相關服務中…",
    "thinking": "思考中…",
    "analyzing": "分析查詢結果中…",
    "wrapping_up": "已達查詢上限，整理結論中…",
}


def _status(phase: str) -> dict:
    return {"type": "status", "phase": phase, "label": STATUS_LABELS.get(phase, phase)}


async def _followups_and_done(message: str, answer_parts: list[str]) -> AsyncIterator[dict]:
    """Shared turn tail: suggest follow-up chips from the Q/A pair, then `done`.
    Used by both the fast path and the full-graph path. Best-effort — a
    suggestion failure never breaks the turn."""
    answer = "".join(answer_parts).strip()
    if answer:
        try:
            items = await suggest_followups(message, answer)
            if items:
                yield {"type": "suggestions", "items": items}
        except Exception as e:
            logger.warning("follow-up suggestion failed: %s", e)
    yield {"type": "done"}


async def stream_chat(
    message: str, thread_id: str, service_hint: str | None = None
) -> AsyncIterator[dict]:
    """Yield LangGraph events as dicts. Caller serializes to SSE.

    `service_hint` is set when the user picked a service from the clarify menu —
    we skip resolution and inject that service's capability directly."""
    agent = await _build_agent()
    config = {"configurable": {"thread_id": thread_id}}

    # Intent gate: reject anything outside the AIOps / observability scope before
    # spending any MCP tool calls or LLM turns on it.
    # fail-closed: if the classifier errors we refuse rather than let an
    # unclassified message reach the tools. An attacker who can force the
    # classify call to fail must not thereby bypass the gate.
    yield _status("understanding")
    try:
        intent = await classify_intent(message)
    except Exception as e:
        logger.warning("Intent gate failed, refusing (fail-closed): %s", e)
        intent = IntentResult(in_scope=False)

    if not intent.in_scope:
        yield {"type": "token", "text": REFUSAL_TEXT}
        yield {"type": "done"}
        return

    # Resolve which service(s) the question is about, then inject their live
    # capability snapshot (Phase C/D). Order:
    #  - service_hint set (user picked from the clarify menu) → use it directly.
    #  - else resolve; ambiguous candidates → emit a `clarify` menu and stop
    #    this turn (Phase D-2); confident match → inject; nothing → no injection.
    turn_messages: list = []
    snapshot: str | None = None
    yield _status("locating")
    try:
        if service_hint:
            snapshot = await capability_for_services([service_hint])
        else:
            resolution = await resolve_services(message)
            if resolution["candidates"]:
                yield {
                    "type": "clarify",
                    "prompt": CLARIFY_PROMPT,
                    "options": resolution["candidates"],
                }
                yield {"type": "done"}
                return
            if resolution["services"]:
                snapshot = await capability_for_services(resolution["services"])
    except Exception as e:
        logger.warning("capability/resolve failed, continuing without it: %s", e)

    if snapshot:
        turn_messages.append(SystemMessage(content=snapshot))
    turn_messages.append({"role": "user", "content": message})

    # Accumulate the answer text so we can suggest follow-ups from it afterward.
    answer_parts: list[str] = []

    # Fast path: a single-query "show me" request. Translate it to the query and
    # let the panel render it — one LLM call, no tool loop, no interpretation
    # call. On any failure we fall through to the full graph below.
    if intent.mode == "lookup":
        yield _status("thinking")
        try:
            async for text in stream_fast_path(message, snapshot):
                answer_parts.append(text)
                yield {"type": "token", "text": text}
            async for ev in _followups_and_done(message, answer_parts):
                yield ev
            return
        except Exception as e:
            logger.warning("fast path failed, falling back to full graph: %s", e)
            answer_parts.clear()

    # Tracks whether a tool has run yet, so the agent node's "thinking" status
    # reads as "analyzing results" once we're past the first query.
    tool_ran = False

    async for event in agent.astream_events(
        # tool_calls_used resets to 0 each turn (overwrite reducer); messages
        # append to the thread history (add_messages reducer).
        {"messages": turn_messages, "tool_calls_used": 0, "budget": settings.tool_call_budget},
        config=config,
        version="v2",
    ):
        kind = event.get("event")
        name = event.get("name", "")
        data = event.get("data", {})

        if DEBUG_EVENTS:
            logger.warning("event=%s name=%s data_keys=%s", kind, name, list(data.keys()))

        # Node-entry status. `agent` / `force_answer` enter via on_chain_start
        # with name == the node (route_after_agent also fires with name
        # "route_after_agent", which we skip). `tools` is covered by tool_start.
        if kind == "on_chain_start" and name in ("agent", "force_answer"):
            if name == "force_answer":
                yield _status("wrapping_up")
            else:
                yield _status("analyzing" if tool_ran else "thinking")

        elif kind == "on_chat_model_stream":
            chunk = data.get("chunk")
            text = _flatten_content(getattr(chunk, "content", None)) if chunk is not None else ""
            if text:
                answer_parts.append(text)
                yield {"type": "token", "text": text}

        elif kind == "on_tool_start":
            tool_ran = True
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
                    if not answer_parts:
                        answer_parts.append(text)
                    yield {"type": "final", "text": text}

    async for ev in _followups_and_done(message, answer_parts):
        yield ev
