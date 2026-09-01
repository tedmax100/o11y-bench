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
# Tier 0/1 runbooks are data the runbook layer loads at runtime (settings.runbook_dir
# defaults to ./runbooks relative to this WORKDIR). Without this they'd be absent
# in-cluster and no alert would ever match a runbook.
COPY runbooks /app/runbooks

RUN uv sync --frozen --no-dev 2>/dev/null \
    || uv sync --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

# Stable HTTP semantic conventions: `http.server.request.duration` /
# `http.client.request.duration` in seconds, with the spec's explicit bucket
# boundaries. The default (old) mode emits the pre-stable names in
# milliseconds against the SDK's default buckets (0, 5, 10, 25, ...) — the
# source of the constant p95 = 4.75 we chased for days, which measured the
# bucket layout rather than any latency. `http` = new names only; `http/dup`
# emits both, which is what a migration with live dashboards would use.
# The Deployment sets this too; keeping it here means a plain `docker run`
# of this image reports the same names as the cluster does.
ENV OTEL_SEMCONV_STABILITY_OPT_IN=http

EXPOSE 8000

CMD ["opentelemetry-instrument", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
