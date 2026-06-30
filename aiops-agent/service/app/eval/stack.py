"""Reproducible eval environment: boot the o11y-bench prebuilt stack image,
which self-generates a deterministic incident on startup.

This is Path A of the reproducibility design: correctness comes from FIXED data,
not from mocking. The `demo-services-o11y-stack` image bundles Prometheus / Loki
/ Tempo plus the telemetry generator; on boot it bakes 24h of history ending at
`O11Y_SCENARIO_TIME_ISO`, with the payment-service v2.4.1→v2.5.0 decline incident
at end−3h. We publish its native ports to the agent's defaults
(localhost:9090/3100/3200), so `run_headless` hits it unchanged, and pin every
fixture's clock to the same scenario time — so every run queries the same data
while the agent stays free to issue whatever (real) queries it likes per seed.

The image has no Kubernetes API, so the agent's k8s tools degrade to
"unavailable" — infra-causal incidents (OOMKilled/CrashLoop) are out of scope for
this stack; metrics/logs/traces incidents are fully covered.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request

DEFAULT_IMAGE = "demo-services-o11y-stack:latest"
CONTAINER_NAME = "aiops-eval-stack"
# host:container — the host side matches the agent's default *_URL settings, so
# no settings override is needed. 8080 is the stack's own readiness gateway.
PORTS = {9090: 9090, 3100: 3100, 3200: 3200, 8080: 8080}


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True)


def teardown(name: str = CONTAINER_NAME) -> None:
    """Remove the eval stack container (force, ignore if absent)."""
    _run(["docker", "rm", "-f", name])


def boot(scenario_time: str, *, image: str = DEFAULT_IMAGE, name: str = CONTAINER_NAME) -> str:
    """Start the stack container detached. Clears any stale container of the same
    name first. Returns the container id; raises on a docker error (e.g. a host
    port already in use)."""
    teardown(name)
    publish: list[str] = []
    for host, container in PORTS.items():
        publish += ["-p", f"{host}:{container}"]
    res = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            *publish,
            "-e",
            f"O11Y_SCENARIO_TIME_ISO={scenario_time}",
            image,
        ]
    )
    if res.returncode != 0:
        raise RuntimeError(f"docker run failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def wait_ready(*, timeout: float = 180.0, poll: float = 3.0) -> bool:
    """Block until the incident data is actually queryable, not just until the
    container is up — poll Prometheus for the payment counter the generator
    emits. Returns False on timeout."""
    query = "http://localhost:9090/api/v1/query?query=payment_charges_total"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(query, timeout=3) as resp:  # fixed localhost URL
                data = json.load(resp)
            if data.get("status") == "success" and data.get("data", {}).get("result"):
                return True
        except Exception:
            pass  # container still starting / generator still loading
        time.sleep(poll)
    return False
