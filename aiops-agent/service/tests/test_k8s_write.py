"""Unit tests for app/tools/k8s_write.py — rollout_undo and scale mutations.
All kubernetes API calls are mocked; no cluster needed."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import app.tools.k8s_write as kw


def _make_rs(rev: int, images: list[str], name: str | None = None) -> SimpleNamespace:
    """Build a minimal ReplicaSet mock with the revision annotation."""
    containers = [SimpleNamespace(image=img) for img in images]
    template_spec = SimpleNamespace(containers=containers)
    template = SimpleNamespace(spec=template_spec)
    spec = SimpleNamespace(template=template)
    meta = SimpleNamespace(
        annotations={"deployment.kubernetes.io/revision": str(rev)},
        name=name or f"dep-{rev}",
    )
    return SimpleNamespace(metadata=meta, spec=spec)


def _make_deployment(rev: int, labels: dict | None = None) -> SimpleNamespace:
    sel = SimpleNamespace(match_labels=labels or {"app": "payment"})
    spec = SimpleNamespace(selector=sel, replicas=3, template=SimpleNamespace())
    meta = SimpleNamespace(
        annotations={"deployment.kubernetes.io/revision": str(rev)},
        name="payment",
    )
    return SimpleNamespace(metadata=meta, spec=spec)


# ---- helpers ---------------------------------------------------------------


def _patch_read_client(dep, rs_list_items):
    """Return a patch that makes k8s._load_client return fakes."""
    apps_r = MagicMock()
    apps_r.read_namespaced_deployment.return_value = dep
    apps_r.list_namespaced_replica_set.return_value = SimpleNamespace(items=rs_list_items)
    # _load_client returns (core_v1, apps_v1)
    return patch("app.tools.k8s._load_client", return_value=(MagicMock(), apps_r))


def _patch_write_api(write_api=None):
    api = write_api or MagicMock()
    return patch.object(kw, "_load_write_api", return_value=api), api


# ---- impl_rollout_undo -----------------------------------------------------


def _fake_template():
    return {
        "metadata": {"annotations": {"deployment.kubernetes.io/revision": "2"}},
        "spec": {"containers": [{"image": "payment:v1.2.0"}]},
    }


@pytest.mark.asyncio
async def test_rollout_undo_basic(monkeypatch):
    dep = _make_deployment(rev=3)
    prev_rs = _make_rs(rev=2, images=["payment:v1.2.0"])
    curr_rs = _make_rs(rev=3, images=["payment:v1.3.0"])

    write_api = MagicMock()

    mock_api_client = MagicMock()
    mock_api_client.sanitize_for_serialization.return_value = _fake_template()

    with _patch_read_client(dep, [curr_rs, prev_rs]):
        with patch.object(kw, "_load_write_api", return_value=write_api):
            with patch("kubernetes.client.ApiClient", return_value=mock_api_client):
                result = await kw.impl_rollout_undo({"deployment": "payment", "namespace": "demo"})

    assert result["action"] == "rollout_undo"
    assert result["rolled_back_to_revision"] == 2
    assert result["images"] == ["payment:v1.2.0"]
    write_api.patch_namespaced_deployment.assert_called_once()
    call_kwargs = write_api.patch_namespaced_deployment.call_args
    patch_body = call_kwargs[1].get("body") or call_kwargs[0][2]
    assert "spec" in patch_body
    # revision annotation should be stripped from the patch template
    tmpl_ann = patch_body["spec"]["template"]["metadata"]["annotations"]
    assert "deployment.kubernetes.io/revision" not in tmpl_ann


@pytest.mark.asyncio
async def test_rollout_undo_no_previous_rs_raises(monkeypatch):
    dep = _make_deployment(rev=1)  # rev 1 has no previous
    curr_rs = _make_rs(rev=1, images=["payment:v1.0.0"])

    with _patch_read_client(dep, [curr_rs]):
        with patch.object(kw, "_load_write_api", return_value=MagicMock()):
            with pytest.raises(RuntimeError, match="no previous ReplicaSet"):
                await kw.impl_rollout_undo({"deployment": "payment", "namespace": "demo"})


@pytest.mark.asyncio
async def test_rollout_undo_uses_default_namespace(monkeypatch):
    dep = _make_deployment(rev=2)
    prev_rs = _make_rs(rev=1, images=["img:v1"])
    curr_rs = _make_rs(rev=2, images=["img:v2"])

    write_api = MagicMock()
    mock_api_client = MagicMock()
    mock_api_client.sanitize_for_serialization.return_value = {
        "metadata": {"annotations": {}},
        "spec": {},
    }

    with _patch_read_client(dep, [curr_rs, prev_rs]):
        with patch.object(kw, "_load_write_api", return_value=write_api):
            with patch("kubernetes.client.ApiClient", return_value=mock_api_client):
                monkeypatch.setattr(kw.settings, "k8s_namespace", "default-ns")
                result = await kw.impl_rollout_undo({"deployment": "payment"})

    assert result["namespace"] == "default-ns"


# ---- impl_scale ------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_patches_replicas():
    dep = _make_deployment(rev=1)
    dep.spec.replicas = 3
    write_api = MagicMock()

    with _patch_read_client(dep, []):
        with patch.object(kw, "_load_write_api", return_value=write_api):
            result = await kw.impl_scale(
                {"deployment": "payment", "namespace": "demo", "replicas": "1"}
            )

    assert result["action"] == "scale"
    assert result["previous_replicas"] == 3
    assert result["new_replicas"] == 1
    write_api.patch_namespaced_deployment.assert_called_once()
    _, call_kw = write_api.patch_namespaced_deployment.call_args
    body = call_kw.get("body") or write_api.patch_namespaced_deployment.call_args[0][2]
    assert body["spec"]["replicas"] == 1


@pytest.mark.asyncio
async def test_scale_replicas_coerced_from_string():
    dep = _make_deployment(rev=1)
    dep.spec.replicas = 2
    write_api = MagicMock()

    with _patch_read_client(dep, []):
        with patch.object(kw, "_load_write_api", return_value=write_api):
            result = await kw.impl_scale(
                {"deployment": "svc", "namespace": "demo", "replicas": "5"}
            )

    assert result["new_replicas"] == 5


@pytest.mark.asyncio
async def test_scale_uses_default_namespace(monkeypatch):
    dep = _make_deployment(rev=1)
    dep.spec.replicas = 2
    write_api = MagicMock()

    with _patch_read_client(dep, []):
        with patch.object(kw, "_load_write_api", return_value=write_api):
            monkeypatch.setattr(kw.settings, "k8s_namespace", "prod")
            result = await kw.impl_scale({"deployment": "x", "replicas": 3})

    assert result["namespace"] == "prod"


# ---- _load_write_api caching -----------------------------------------------


def test_load_write_api_error_is_cached(tmp_path):
    """Once the write client fails to load, subsequent calls raise with the cached message."""
    # Reset module-level cache
    kw._write_api = None
    kw._write_error = None

    with patch("pathlib.Path.exists", return_value=False):
        with patch("kubernetes.config.load_incluster_config", side_effect=Exception("not in k8s")):
            with patch(
                "kubernetes.config.load_kube_config", side_effect=Exception("no kubeconfig")
            ):
                with pytest.raises(RuntimeError, match="k8s write client unavailable"):
                    kw._load_write_api()

    # Second call should hit the cached error
    with pytest.raises(RuntimeError, match="k8s write client unavailable"):
        kw._load_write_api()

    # Cleanup
    kw._write_api = None
    kw._write_error = None


def test_write_client_sends_a_bearer_prefixed_header(monkeypatch, tmp_path):
    """The header the client actually puts on the wire must say `Bearer <jwt>`.

    Regression for the failure this whole execution plane was blocked on: the
    prefix was keyed on "authorization" while the generated client only reads it
    under "BearerToken", so a perfectly valid token went out bare and came back
    401 — indistinguishable, from the outside, from an expired credential.
    """
    import app.tools.k8s_write as kw

    token_file = tmp_path / "token"
    token_file.write_text("fake.jwt.value\n")
    monkeypatch.setattr(kw, "_WRITE_TOKEN_PATH", str(token_file))
    monkeypatch.setattr(kw, "_CLUSTER_CA_PATH", str(tmp_path / "ca.crt"))
    monkeypatch.setattr(kw, "in_cluster_write_creds", lambda: True)

    _apps, authz = kw._build_write_clients()
    cfg = authz.api_client.configuration
    assert cfg.auth_settings()["BearerToken"]["value"] == "Bearer fake.jwt.value"
