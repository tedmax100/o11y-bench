"""Runs inside the o11y-bench task container, in aiops-agent's own `uv`
project (`uv run --project /app/aiops-agent o11y_bench_runner.py`) — so
`import app...` resolves to aiops-agent/service/app, not this repo's
`o11y_bench` package.

Reuses `app.agent.stream_chat`, the same entry point the plugin and the
day25 away-field bench (`otel-aiops-agent/ironman-2026/day25/rerun_bench.py`)
already drive — not a new integration path.

Two adjustments before that call, mirroring day25's `run_today(governance=False)`:

- **Neutral schema catalog.** The shipped `schema_catalog.md` describes our
  own demo stack (payment-service, http_server_duration_milliseconds, ...).
  Handing it to the model on an o11y-bench task is the "wrong" arm from the
  day33 away-field experiment — it scored *worse* than no catalog at all,
  because the model trusted names that don't exist here instead of running
  discover_* first.
- **Drop the k8s_*/github_* tools.** No Kubernetes cluster and no GitHub repo
  sit behind an o11y-bench task; leaving them bound just spends tool-call
  budget on calls that can only ever return "unavailable" or 404.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

NEUTRAL_CATALOG = """# Telemetry Schema Catalog

No environment-specific inventory is provided. Discover metric names, log
fields and span names with the discover_* tools before querying, and read
label values off the results rather than assuming them.
"""

DROPPED_TOOLS = {
    "k8s_pod_status",
    "k8s_events",
    "k8s_deployment_status",
    "k8s_change_provenance",
    "github_compare",
    "github_get_file",
}


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(p for p in parts if p)
    return str(content) if content else ""


def _build_trajectory(
    messages: list[BaseMessage],
    calls: list,
    *,
    task_prompt: str,
    system_prompt: str,
    model_name: str,
) -> dict[str, Any]:
    """ATIF-ish trajectory: one step per AIMessage, its tool calls, and the
    matched results — built from `app.eval.process.extract_calls`'s already-
    paired (call, result) view rather than re-walking raw ToolMessages."""
    steps: list[dict[str, Any]] = [
        {
            "step_id": 1,
            "timestamp": datetime.now(UTC).isoformat(),
            "source": "system",
            "message": system_prompt,
        },
        {
            "step_id": 2,
            "timestamp": datetime.now(UTC).isoformat(),
            "source": "user",
            "message": task_prompt,
        },
    ]
    step_id = 2
    call_iter = iter(calls)

    def flush(ai: AIMessage) -> None:
        nonlocal step_id
        n = len(getattr(ai, "tool_calls", None) or [])
        this_calls = [next(call_iter, None) for _ in range(n)]
        this_calls = [c for c in this_calls if c is not None]
        step_id += 1
        # `_parse_atif_steps` in the grading library does
        # `step.get("tool_calls", [])` / iterates `observation["results"]`
        # unconditionally — a key present with value `None` breaks that (it's
        # not a missing key, so the default never kicks in). Omit rather than
        # null these out.
        step: dict[str, Any] = {
            "step_id": step_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "source": "agent",
            "message": _message_text(ai.content) or "(tool use)",
        }
        if this_calls:
            step["tool_calls"] = [
                {"function_name": c.name, "arguments": c.args} for c in this_calls
            ]
            step["observation"] = {
                "results": [{"content": c.result, "kind": c.kind} for c in this_calls]
            }
        steps.append(step)

    for m in messages:
        if isinstance(m, AIMessage):
            flush(m)

    return {
        "schema_version": "ATIF-v1.7",
        "session_id": str(uuid.uuid4()),
        "trajectory_id": str(uuid.uuid4()),
        "agent": {
            "name": "aiops-agent",
            "version": "1.0.0",
            "model_name": model_name,
            "tool_definitions": [{"name": n} for n in sorted({c.name for c in calls})],
        },
        "steps": steps,
        "final_metrics": {
            "total_steps": len(steps),
            "extra": {"total_tool_calls": len(calls)},
        },
    }


async def main() -> None:
    import app.agent as agent_mod
    from app.eval.process import extract_calls

    agent_mod.SCHEMA_CATALOG = NEUTRAL_CATALOG
    agent_mod._inject_signal_context = lambda *a, **k: None
    agent_mod.TOOLS = [t for t in agent_mod.TOOLS if t.name not in DROPPED_TOOLS]

    question = Path("/app/instruction.txt").read_text().strip()
    thread_id = os.environ.get("AIOPS_THREAD_ID") or uuid.uuid4().hex[:12]

    answer_parts: list[str] = []
    async for event in agent_mod.stream_chat(question, thread_id=thread_id):
        if event.get("type") == "token":
            answer_parts.append(event["text"])
    answer = "".join(answer_parts)

    graph = await agent_mod._build_agent()
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    messages = state.values.get("messages", [])
    calls = extract_calls(messages)

    trajectory = _build_trajectory(
        messages,
        calls,
        task_prompt=question,
        system_prompt=agent_mod.build_system_prompt(),
        model_name=agent_mod.settings.gemini_model,
    )

    agent_dir = Path("/logs/agent")
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "trajectory.json").write_text(json.dumps(trajectory, indent=2))

    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
