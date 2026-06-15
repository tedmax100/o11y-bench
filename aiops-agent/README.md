# AIOps Agent (MVP)

Chat-based RCA assistant for the Grafana stack.
LangGraph + Gemini agent ←→ Grafana app plugin ←→ user.

```plaintext
┌──────────────────┐  POST /chat   ┌─────────────────────┐  MCP    ┌────────────────┐
│ Grafana plugin   │ ────SSE────▶ │ FastAPI + LangGraph │ ──────▶ │ mcp-grafana    │
│ (Chat UI)        │              │ (Gemini Flash Lite) │         │ Loki/Tempo/    │
└──────────────────┘              └─────────────────────┘         │ Prometheus     │
                                                                  └────────────────┘
```

## Layout

```
aiops-agent/
  plugin/            # Grafana app plugin (npm, React, @grafana/ui)
  service/           # Python agent service (uv, FastAPI, LangGraph)
  docker-compose.yaml # Sidecar (Grafana + mcp-grafana + data sources) with plugin mount
```

## Quick start

完整啟動步驟、健康檢查、troubleshooting 都在 **[RUNBOOK.md](./RUNBOOK.md)**。
TL;DR — 三個 terminal：

```bash
# Terminal 1: sidecar
cd aiops-agent && docker compose up --build

# Terminal 2: agent service
cd aiops-agent/service && uv run uvicorn app.main:app --reload --port 8000

# Terminal 3: plugin watch build
cd aiops-agent/plugin && npm run dev
```

三個都 ready 後開 <http://localhost:3000> → Apps → AIOps → Chat。

## Notes

- Plugin is unsigned; `GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS` is set in compose.
- Agent service runs on the host so hot-reload works. The plugin calls it at
  `http://localhost:8000` — CORS in `service/app/main.py` allows `localhost:3000`.
- See [../doc/aiops-agent-design.md](../doc/aiops-agent-design.md) for the design rationale.
- See [../doc/aiops-agent-mvp-notes.md](../doc/aiops-agent-mvp-notes.md) for build notes.
- See [../doc/agents/aiops-agent-ARE-gap-analysis.md](../doc/agents/aiops-agent-ARE-gap-analysis.md)
  for how this agent maps onto the Agentic Reliability Engineering (ARE) philosophy
  — what it satisfies (Signal/Reasoning planes, hallucination defense, bounded
  autonomy) and what's missing (Act/Governance/Learn, CE calibration).
