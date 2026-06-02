# Agent service image, deployed into the demo-services k3d cluster so it can
# reach Prometheus/Loki/Tempo via internal DNS (prometheus.demo.svc:9090 ...).
# Mirrors the demo-services Dockerfiles: uv base, --frozen with a re-resolve
# fallback. The observer is now observed too — launched under
# `opentelemetry-instrument` so FastAPI/httpx/logging + the LangChain
# instrumentor (GenAI spans/metrics for every Gemini call) ship via OTLP.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base

WORKDIR /app

COPY pyproject.toml uv.lock* /app/
COPY app /app/app

RUN uv sync --frozen --no-dev 2>/dev/null \
    || uv sync --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

CMD ["opentelemetry-instrument", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
