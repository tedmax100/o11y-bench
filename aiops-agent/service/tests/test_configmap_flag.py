"""Unit tests for the feature-flag remediation action (k8s.configmap_flag_set):
the write impl, its blast-radius dry-run, and the runbook that reaches for it.
All kubernetes calls are mocked; no cluster needed."""

import json
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

import app.tools.k8s_write as kw
from app.actions import registry
from app.blast_radius import dry_run_configmap_flag_set


def _cm(data: dict) -> NS:
    return NS(data=data, metadata=NS(name="user-flags"))


def _flags(**kw_) -> dict:
    base = {"user_session_cache_disabled": False, "unrelated_flag": True}
    base.update(kw_)
    return {"flags.json": json.dumps(base)}


def _patch_clients(cm, deployments=()):
    core_r = MagicMock()
    core_r.read_namespaced_config_map.return_value = cm
    apps_r = MagicMock()
    apps_r.list_namespaced_deployment.return_value = NS(items=list(deployments))
    return patch("app.tools.k8s._load_client", return_value=(core_r, apps_r))


def _deployment(name: str, *, replicas=2, volume_cm=None, envfrom_cm=None, envref_cm=None) -> NS:
    volumes = [NS(config_map=NS(name=volume_cm))] if volume_cm else []
    env_from = [NS(config_map_ref=NS(name=envfrom_cm))] if envfrom_cm else []
    env = [NS(value_from=NS(config_map_key_ref=NS(name=envref_cm)))] if envref_cm else []
    container = NS(env_from=env_from, env=env)
    pod_spec = NS(volumes=volumes, containers=[container], init_containers=[])
    return NS(
        metadata=NS(name=name),
        spec=NS(replicas=replicas, template=NS(spec=pod_spec)),
    )


# ---- the write impl --------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_set_patches_only_the_named_flag():
    cm = _cm(_flags())
    core_w = MagicMock()
    with _patch_clients(cm), patch.object(kw, "_load_write_core_api", return_value=core_w):
        out = await kw.impl_configmap_flag_set(
            {
                "configmap": "user-flags",
                "namespace": "demo",
                "flag": "user_session_cache_disabled",
                "value": True,
            }
        )

    assert out["previous_value"] is False and out["new_value"] is True
    body = core_w.patch_namespaced_config_map.call_args.kwargs["body"]
    written = json.loads(body["data"]["flags.json"])
    assert written["user_session_cache_disabled"] is True
    # The other flag on the same document survives the patch — a strategic merge
    # replaces the whole string, so this is the read-modify-write being checked.
    assert written["unrelated_flag"] is True


@pytest.mark.asyncio
async def test_flag_set_refuses_to_invent_a_flag():
    cm = _cm(_flags())
    core_w = MagicMock()
    with _patch_clients(cm), patch.object(kw, "_load_write_core_api", return_value=core_w):
        with pytest.raises(RuntimeError, match="no flag 'typo_flag'"):
            await kw.impl_configmap_flag_set(
                {"configmap": "user-flags", "flag": "typo_flag", "value": True}
            )
    core_w.patch_namespaced_config_map.assert_not_called()


@pytest.mark.asyncio
async def test_flag_set_refuses_non_json_key():
    cm = _cm({"flags.json": "not json at all"})
    core_w = MagicMock()
    with _patch_clients(cm), patch.object(kw, "_load_write_core_api", return_value=core_w):
        with pytest.raises(RuntimeError, match="not JSON"):
            await kw.impl_configmap_flag_set(
                {"configmap": "user-flags", "flag": "x", "value": True}
            )
    core_w.patch_namespaced_config_map.assert_not_called()


# ---- the dry-run -----------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_counts_every_reader():
    deps = [
        _deployment("user-service", replicas=2, volume_cm="user-flags"),
        _deployment("api-gateway", replicas=3, envfrom_cm="user-flags"),
        _deployment("order-service", replicas=4, volume_cm="other-map"),
    ]
    with _patch_clients(_cm(_flags()), deps):
        br = await dry_run_configmap_flag_set(
            {
                "configmap": "user-flags",
                "namespace": "demo",
                "flag": "user_session_cache_disabled",
                "value": True,
            }
        )
    assert br.available and br.affected_pods == 5  # 2 + 3, not order-service's 4
    assert br.detail == "api-gateway, user-service"
    assert any("2 workloads" in n for n in br.notes)
    assert br.current_revision == "user_session_cache_disabled=False"
    assert br.target_revision == "user_session_cache_disabled=True"


@pytest.mark.asyncio
async def test_dry_run_notes_a_no_op_flip():
    deps = [_deployment("user-service", volume_cm="user-flags")]
    with _patch_clients(_cm(_flags(user_session_cache_disabled=True)), deps):
        br = await dry_run_configmap_flag_set(
            {"configmap": "user-flags", "flag": "user_session_cache_disabled", "value": True}
        )
    assert any("would change nothing" in n for n in br.notes)


@pytest.mark.asyncio
async def test_dry_run_fails_closed_without_a_cluster():
    with patch("app.tools.k8s._load_client", side_effect=RuntimeError("k8s not wired")):
        br = await dry_run_configmap_flag_set(
            {"configmap": "user-flags", "flag": "f", "value": True}
        )
    assert not br.available  # policy refuses on this


@pytest.mark.asyncio
async def test_dry_run_requires_a_flag_name():
    br = await dry_run_configmap_flag_set({"configmap": "user-flags", "value": True})
    assert not br.available and "required" in br.detail


# ---- registration + the runbook that uses it -------------------------------


def test_action_is_registered_reversible_and_approval_gated():
    spec = registry.get("k8s.configmap_flag_set")
    assert spec is not None
    assert spec.reversible and spec.requires_approval
    assert spec.impl is not None and spec.dry_run is not None


def test_session_cache_runbook_matches_the_order_service_alert():
    from app import runbook

    rb = runbook.match_runbook(
        {"alertname": "order-cancel-rate-high", "service_name": "order-service"}, {}
    )
    assert rb is not None and rb.id == "session-cache-timeout"

    step = rb.remediation[0]
    assert step.action == "k8s.configmap_flag_set"
    assert step.requires_approval and step.reversible
    # The undo puts the flag back rather than doing something merely similar.
    assert step.rollback["action"] == "k8s.configmap_flag_set"
    # The flag's healthy state is False: `user_session_cache_disabled` is TRUE
    # during the incident. Pinned here because a name like that invites the
    # opposite reading, and the opposite reading re-fires the incident.
    assert step.args["value"] is False
    assert step.rollback["args"]["value"] is True
    # Verify reads the upstream signal, not the alert's own metric: the pager
    # going quiet is a different claim from the auth checks recovering.
    assert "user_auth_checks_total" in step.verify["args"]["expr"]


def test_session_cache_runbook_crosses_the_hop_in_diagnostics():
    from app import runbook

    rb = next(r for r in runbook.load_runbooks() if r.id == "session-cache-timeout")
    exprs = " ".join(str(d.args) for d in rb.diagnostics)
    assert "orders_total" in exprs  # the alerting service
    assert "user_auth_checks_total" in exprs  # one hop upstream — the point of it


# ---- the two read-only endpoints the game day preflights on ----------------


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_actions_endpoint_reports_contract_and_kill_switch():
    body = _client().get("/actions").json()
    by_name = {a["name"]: a for a in body["actions"]}
    flag = by_name["k8s.configmap_flag_set"]
    assert flag["executable"] and flag["has_dry_run"]
    assert flag["requires_approval"] and flag["reversible"]
    # Knowing the action and being allowed to run it are separate facts, and a
    # preflight that conflates them cannot tell a stale image from a closed
    # kill switch.
    assert "actions_enabled" in body


def test_cases_context_endpoint_returns_the_rendered_recall_block(monkeypatch):
    import app.agent as agent_mod

    monkeypatch.setattr(
        agent_mod,
        "_past_incident_context",
        lambda service, alertname=None: f"## {service}/{alertname}",
    )
    body = _client().get("/cases/context?service=order-service&alertname=order-cancel").json()
    assert body["context"] == "## order-service/order-cancel"
