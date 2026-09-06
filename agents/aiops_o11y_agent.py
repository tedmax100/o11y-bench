"""Harbor agent that runs *our* aiops-agent (LangGraph + Gemini, native
Prometheus/Loki/Tempo APIs) against o11y-bench tasks, instead of the
generic MCP-tool agents in `o11y_agent.py` / `langchain_o11y_agent.py`.

Why a separate agent class rather than reusing LangChainO11yBenchAgent: that
one talks to the task's `mcp-grafana` server. Our agent has its own tool
layer (app/tools/query.py) that hits Prometheus/Loki/Tempo's native HTTP APIs
directly — the whole point of the v3 rewrite documented in
`aiops-agent/service/app/agent.py`. Running it here is how we find out
whether the RCA loop we've been building generalizes past our own demo stack
(see the "away-field" experiments under
otel-aiops-agent/ironman-2026/day33/) using o11y-bench's own 63-task suite
and grader instead of a hand-rolled nine-question bench.

Scope: this only exercises the query/RCA half of the agent (prometheus_query,
loki_query, tempo_query, investigation categories — the tool set below has no
Grafana-dashboard tools, so `dashboarding` / `grafana_api` tasks are not
answerable here; filter them out with `--task-name`).

The service's dependency footprint (langgraph, langchain-google-genai,
opentelemetry, kubernetes client — see aiops-agent/service/pyproject.toml) is
installed *inside* the task container via `uv sync`, mirroring how
LangChainO11yBenchAgent runs its own runner in-container. This is different
from (and does not conflict with) the host-side approach used for the
separate away-field self-telemetry smoke test — that one avoided the
container to keep the agent's own OTel signal clean; here we don't run
`opentelemetry-instrument` at all, so there's no self-telemetry to pollute.
"""

import os
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path

from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .o11y_agent import O11yBenchAgent

_SERVICE_DIR = Path(__file__).resolve().parents[1] / "aiops-agent" / "service"
_RUNNER_SCRIPT = Path(__file__).with_name("aiops_agent_runner.py")

# Files/dirs from aiops-agent/service worth shipping into the container. Not
# the whole tree: `.venv` (224M, rebuilt in-container by `uv sync` anyway),
# `.env` (holds the *local* GOOGLE_API_KEY — the container gets its own via
# Harbor's env, never the file), caches, and the dev SQLite/eval artifacts
# must never go along for the ride.
_INCLUDE = ("app", "pyproject.toml", "uv.lock")

# Tools this environment cannot answer through: no Kubernetes cluster (see
# app/tools/k8s.py — k8s_enabled=false makes these report `unavailable`
# without ever touching a kubeconfig) and no GitHub repo behind these tasks
# (github_compare/get_file would just 404 against made-up repo names and burn
# tool-call budget). Dropped from TOOLS in the runner, not just disabled, so
# the model doesn't waste a call reaching for them.
DROPPED_TOOLS = (
    "k8s_pod_status",
    "k8s_events",
    "k8s_deployment_status",
    "k8s_change_provenance",
    "github_compare",
    "github_get_file",
)


def _stage_service_dir() -> Path:
    """Copy the subset of aiops-agent/service this run needs into a scratch
    dir, so upload_dir doesn't ship .venv/.env/dev artifacts into the task
    container."""
    staged = Path(tempfile.mkdtemp(prefix="aiops-o11y-bench-stage-"))
    for name in _INCLUDE:
        src = _SERVICE_DIR / name
        dst = staged / name
        if src.is_dir():
            ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "eval.db", "baseline.json")
            shutil.copytree(src, dst, ignore=ignore)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return staged


class AiopsO11yBenchAgent(O11yBenchAgent):
    """Runs aiops-agent's own RCA graph (native Prom/Loki/Tempo tools, no MCP)
    against an o11y-bench task, in-container via `uv run --project`."""

    @staticmethod
    def name() -> str:
        return "aiops-agent"

    def version(self) -> str:
        return "1.0.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        staged = _stage_service_dir()
        try:
            await environment.exec(command="mkdir -p /app/aiops-agent")
            await environment.upload_dir(source_dir=staged, target_dir="/app/aiops-agent")
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        await environment.upload_file(
            source_path=_RUNNER_SCRIPT,
            target_path="/app/aiops-agent/o11y_bench_runner.py",
        )
        # `uv sync` here (setup) rather than lazily in run() so the ~15s dep
        # resolve/install doesn't eat into the agent's own timeout budget.
        result = await environment.exec(
            command="cd /app/aiops-agent && uv sync --frozen",
            timeout_sec=300,
        )
        if result.return_code != 0:
            self.logger.error(f"uv sync failed: {result.stdout}\n{result.stderr}")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        instruction_path = self.logs_dir / "instruction.txt"
        instruction_path.write_text(instruction)
        await environment.upload_file(
            source_path=instruction_path,
            target_path="/app/instruction.txt",
        )

        # Fresh SQLite store + LangGraph thread per trial: aiops-agent's case
        # memory (`_inject_past_incidents`) is meant to close the loop across
        # real incidents on ONE stack over time, not across independent
        # benchmark trials — reusing a store here would make trial N an
        # open-book exam on trial N-1's answer (see [[aiops-awayfield-third-arm]]).
        trial_id = uuid.uuid4().hex[:12]

        env: dict[str, str] = {
            "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
            "STORE_PATH": f"/tmp/aiops-store-{trial_id}.db",
            "AIOPS_THREAD_ID": trial_id,
            # No Kubernetes cluster and no GitHub repo behind this stack (see
            # DROPPED_TOOLS above) — k8s_enabled makes the four k8s_* tools
            # short-circuit cleanly instead of reading our home k3d cluster.
            "K8S_ENABLED": "false",
        }
        self._copy_env(env, "GOOGLE_API_KEY", "GEMINI_API_KEY")
        # The task's docker-compose overlay already injects these against the
        # per-task o11y-stack sidecar (native HTTP APIs, no mcp-grafana) — see
        # tasks/*/environment/docker-compose.yaml.
        self._copy_env(env, "PROMETHEUS_URL", "LOKI_URL", "TEMPO_URL")
        env.update(self._extra_env)

        command = (
            "set -o pipefail; mkdir -p /logs/agent/command-0; "
            "cd /app/aiops-agent && "
            "uv run --project . o11y_bench_runner.py 2>&1 | "
            'tee "/logs/agent/command-0/stdout.txt"'
        )
        self.logger.info(f"Running aiops-agent runner (trial {trial_id})")
        result = await environment.exec(command=f"bash -lc {shlex.quote(command)}", env=env)

        try:
            await environment.download_file(
                source_path="/logs/agent/trajectory.json",
                target_path=self.logs_dir / "trajectory.json",
            )
        except Exception as e:
            self.logger.warning(f"Could not download trajectory.json: {e}")

        context.metadata = {
            **(context.metadata or {}),
            "agent": self.name(),
            "trial_id": trial_id,
        }

        if result.return_code != 0:
            from harbor.agents.installed.base import NonZeroAgentExitCodeError

            raise NonZeroAgentExitCodeError(f"Agent exited with code {result.return_code}")

    def _copy_env(self, env: dict[str, str], *keys: str) -> None:
        for key in keys:
            val = os.environ.get(key)
            if val:
                env[key] = val
