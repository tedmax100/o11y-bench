"""Reproducible eval environment: boot the o11y-bench prebuilt stack image,
which self-generates a deterministic incident on startup.

This is Path A of the reproducibility design: correctness comes from FIXED data,
not from mocking. The `demo-services-o11y-stack` image bundles Prometheus / Loki
/ Tempo plus the telemetry generator; on boot it bakes 24h of history ending at
`O11Y_SCENARIO_TIME_ISO`, with the payment-service v2.4.1→v2.5.0 decline incident
at end−3h, plus a session-cache incident on order-service/user-service bounded
to end−7h..end−5h. We publish its native ports to the agent's defaults
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
import urllib.parse
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


# One counter per baked incident. Waiting on the payment counter alone was
# enough while there was one incident; with two, an image built before the
# second generator lands would come up "ready" and the session-cache fixture
# would then fail as if the agent had got it wrong.
_READY_QUERIES = ("payment_charges_total", "user_auth_checks_total")


def wait_ready(
    scenario_time: str | None = None, *, timeout: float = 180.0, poll: float = 3.0
) -> bool:
    """Block until the incident data is actually queryable, not just until the
    container is up — poll Prometheus for a counter from each baked incident.
    Returns False on timeout.

    `scenario_time` must be the one the stack was booted with. An instant query
    only sees a sample inside Prometheus's 5m lookback, and the baked data ends
    at the scenario time — so asking at wall-clock now returns an empty result
    for every clock except one that happens to be roughly now. That made the
    check pass only while the scenario time was not being varied, which is the
    one condition under which it is not worth checking.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if all(_has_series(metric, scenario_time) for metric in _READY_QUERIES):
                return True
        except Exception:
            pass  # container still starting / generator still loading
        time.sleep(poll)
    return False


def _has_series(metric: str, scenario_time: str | None = None) -> bool:
    url = f"http://localhost:9090/api/v1/query?query={metric}"
    if scenario_time:
        url += f"&time={urllib.parse.quote(scenario_time)}"
    with urllib.request.urlopen(url, timeout=3) as resp:  # fixed localhost URL
        data = json.load(resp)
    return data.get("status") == "success" and bool(data.get("data", {}).get("result"))
