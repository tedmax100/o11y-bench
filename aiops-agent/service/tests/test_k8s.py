"""Unit tests for the read-only k8s signal tools.

The live k3d cluster is healthy, so the failure-path parsing (OOMKilled,
CrashLoopBackOff, stuck rollout, event filtering) can't be exercised against it.
These tests feed hand-built objects that mimic the kubernetes client's model
shape into the module's cached API handles, so no cluster is required.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

import app.tools.k8s as k8s


@pytest.fixture(autouse=True)
def _reset_client(monkeypatch):
    """Each test injects its own fake API; reset the module cache around it."""
    monkeypatch.setattr(k8s, "_core_api", None)
    monkeypatch.setattr(k8s, "_apps_api", None)
    monkeypatch.setattr(k8s, "_load_error", None)
    yield


def _install(monkeypatch, core=None, apps=None):
    """Make `_load_client()` return our fakes without touching real kube config."""
    monkeypatch.setattr(k8s, "_load_client", lambda: (core, apps))


def _pod(name, *, phase="Running", statuses=None, git_version="v1", node="n1"):
    return NS(
        metadata=NS(name=name, labels={"git_version": git_version} if git_version else {}),
        spec=NS(node_name=node),
        status=NS(
            phase=phase,
            container_statuses=statuses or [],
            start_time=datetime.now(UTC) - timedelta(minutes=10),
        ),
    )


def _cstatus(*, ready=True, restarts=0, waiting=None, last_term=None):
    state = NS(waiting=NS(reason=waiting) if waiting else None, terminated=None, running=None)
    last_state = NS(
        terminated=NS(reason=last_term[0], exit_code=last_term[1]) if last_term else None,
        waiting=None,
        running=None,
    )
    return NS(ready=ready, restart_count=restarts, state=state, last_state=last_state)


# ---- pod status ------------------------------------------------------------


async def test_pod_status_parses_oom_and_crashloop(monkeypatch):
    pod = _pod(
        "payment-service-abc-123",
        statuses=[
            _cstatus(
                ready=False, restarts=7, waiting="CrashLoopBackOff", last_term=("OOMKilled", 137)
            )
        ],
        git_version="v2.5.0",
    )
    core = NS(list_namespaced_pod=lambda **kw: NS(items=[pod]))
    _install(monkeypatch, core=core)

    out = await k8s.get_pod_status("payment-service")
    assert out["pod_count"] == 1
    p = out["pods"][0]
    assert p["restarts"] == 7
    assert p["ready"] == "0/1"
    assert p["waiting_reasons"] == ["CrashLoopBackOff"]
    assert p["last_terminated"] == ["OOMKilled(exit 137)"]
    assert p["git_version"] == "v2.5.0"


async def test_pod_status_label_selector_uses_config(monkeypatch):
    seen = {}
    core = NS(list_namespaced_pod=lambda **kw: seen.update(kw) or NS(items=[]))
    _install(monkeypatch, core=core)
    monkeypatch.setattr(k8s.settings, "k8s_label_key", "app")
    monkeypatch.setattr(k8s.settings, "k8s_namespace", "demo")

    await k8s.get_pod_status("order-service")
    assert seen["label_selector"] == "app=order-service"
    assert seen["namespace"] == "demo"


# ---- events ----------------------------------------------------------------


def _event(*, name, kind="Pod", etype="Warning", reason="BackOff", msg="m", count=1, mins_ago=1):
    ts = datetime.now(UTC) - timedelta(minutes=mins_ago)
    return NS(
        involved_object=NS(name=name, kind=kind),
        type=etype,
        reason=reason,
        message=msg,
        count=count,
        last_timestamp=ts,
        event_time=None,
        metadata=NS(creation_timestamp=ts),
    )


async def test_events_filter_and_sort(monkeypatch):
    events = [
        _event(name="payment-service-abc-1", reason="OOMKilling", mins_ago=2),
        _event(
            name="payment-service-abc-2", etype="Normal", reason="Pulled", mins_ago=1
        ),  # routine → dropped
        _event(
            name="payment-service", kind="Deployment", reason="ProgressDeadlineExceeded", mins_ago=5
        ),
        _event(name="order-service-xyz", reason="BackOff", mins_ago=1),  # other service → dropped
        _event(
            name="payment-service-abc-3", etype="Normal", reason="Killing", mins_ago=3
        ),  # Normal but interesting → kept
    ]
    core = NS(list_namespaced_event=lambda **kw: NS(items=events))
    _install(monkeypatch, core=core)

    out = await k8s.get_k8s_events("payment-service")
    reasons = [e["reason"] for e in out["events"]]
    assert reasons == ["OOMKilling", "Killing", "ProgressDeadlineExceeded"]  # newest first
    assert all("order-service" not in e["object"] for e in out["events"])
    assert "Pulled" not in reasons


async def test_events_limit(monkeypatch):
    events = [
        _event(name=f"payment-service-{i}", reason="BackOff", mins_ago=i) for i in range(1, 10)
    ]
    core = NS(list_namespaced_event=lambda **kw: NS(items=events))
    _install(monkeypatch, core=core)
    out = await k8s.get_k8s_events("payment-service", limit=3)
    assert len(out["events"]) == 3
    assert out["event_count"] == 9


# ---- deployment status -----------------------------------------------------


async def test_deployment_status_healthy(monkeypatch):
    dep = NS(
        metadata=NS(annotations={"deployment.kubernetes.io/revision": "5"}),
        spec=NS(replicas=3, template=NS(metadata=NS(labels={"git_version": "v2.5.0"}))),
        status=NS(
            ready_replicas=3,
            available_replicas=3,
            updated_replicas=3,
            unavailable_replicas=None,
            conditions=[
                NS(type="Available", status="True", reason="MinimumReplicasAvailable", message="ok")
            ],
        ),
    )
    apps = NS(read_namespaced_deployment=lambda **kw: dep)
    _install(monkeypatch, apps=apps)

    out = await k8s.get_deployment_status("payment-service")
    assert out["found"] is True
    assert out["revision"] == "5"
    assert out["available_replicas"] == 3
    assert out["unavailable_replicas"] == 0  # None coalesced to 0
    assert out["git_version"] == "v2.5.0"


async def test_deployment_status_404(monkeypatch):
    # An exception carrying .status = 404, like the client's ApiException.
    err = Exception("not found")
    err.status = 404
    apps = NS(read_namespaced_deployment=lambda **kw: (_ for _ in ()).throw(err))
    _install(monkeypatch, apps=apps)

    out = await k8s.get_deployment_status("ghost")
    assert out["found"] is False
    assert "ghost" in out["note"]


# ---- unavailable degradation ----------------------------------------------


async def test_unavailable_when_config_missing(monkeypatch):
    def _boom():
        raise RuntimeError("kubernetes config not available (ConfigException)")

    monkeypatch.setattr(k8s, "_load_client", _boom)

    out = await k8s.get_pod_status("payment-service")
    assert out["unavailable"] is True
    assert "config not available" in out["detail"]
