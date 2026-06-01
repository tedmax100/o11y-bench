# aiops-agent service

LangGraph + Gemini 3.1 Flash Lite agent. Streams a ReAct loop via SSE; tools come
from `grafana-mcp` (streamable-http transport).

## Setup

```bash
cd aiops-agent/service
uv sync
cp .env.example .env
# edit .env: set GOOGLE_API_KEY, point MCP_GRAFANA_URL at the sidecar
```

## Run

Assumes the o11y-bench sidecar (Grafana + mcp-grafana) is up on :8080.

```bash
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Try it from CLI

```bash
curl -N -X POST http://localhost:8000/chat \
  -H "content-type: application/json" \
  -d '{"message": "list available prometheus datasources"}'
```

The response is SSE. Events:

| event | payload |
|-------|---------|
| `thread` | `{thread_id}` — reuse to continue the same conversation |
| `status` | `{phase, label}` — progress phase (`understanding` / `locating` / `thinking` / `analyzing` / `wrapping_up`) so the UI can show what the agent is doing |
| `token` | `{text}` — streaming LLM token |
| `tool_start` | `{tool, input}` |
| `tool_end` | `{tool, output_preview}` |
| `clarify` | `{prompt, options}` — ambiguous service; UI shows a picker, resend with `service_hint` |
| `final` | `{text}` — full answer, emitted only if token streaming didn't fire |
| `done` | end of turn |
