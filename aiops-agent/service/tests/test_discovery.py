"""What `discover_metrics` says back is what the model believes about the store.

The regression pinned here: the tool summarised each metric's "extra" labels and
dropped the resource ones as uninteresting, so its answer to "which labels can I
use for this service?" never contained `service_name` — and the model, with no
evidence either way, grouped by the conventional `service`, which exists in no
series and makes Prometheus return one unlabelled total instead of an error.
"""

from __future__ import annotations

import pytest

import app.tools.discovery as d

_SERIES = {
    "data": [
        {
            "__name__": "http_server_duration_milliseconds_count",
            "service_name": "payment-service",
            "service_namespace": "demo",
            "deployment_environment": "demo",
            "git_version": "v2.5.0",
            "job": "demo/payment-service",
            "http_method": "POST",
            "http_status_code": "200",
            "http_target": "/charge",  # noise: high cardinality
            "net_host_port": "8000",  # noise
        },
        {
            "__name__": "payment_charges_total",
            "service_name": "payment-service",
            "service_namespace": "demo",
            "git_version": "v2.5.0",
            "status": "declined",
            "reason": "new_validator",
        },
        {
            "__name__": "otel_sdk_exporter_span_exported",  # SDK internals
            "service_name": "payment-service",
        },
        {"__name__": "target_info", "service_name": "payment-service"},
    ]
}


@pytest.fixture
def discovered(monkeypatch):
    async def mock_get_json(base, path, params):
        return _SERIES

    monkeypatch.setattr(d, "_get_json", mock_get_json)
    return d


@pytest.mark.asyncio
async def test_identity_labels_are_reported(discovered):
    out = await discovered.discover_metrics("payment-service")
    assert "service_name" in out["identity_labels"]
    assert "git_version" in out["identity_labels"]


@pytest.mark.asyncio
async def test_identity_labels_are_not_repeated_on_every_metric(discovered):
    """Reported once, not per metric — the token cost is why they were stripped
    in the first place, and that reason is still valid."""
    out = await discovered.discover_metrics("payment-service")
    for m in out["metrics"]:
        assert "service_name" not in m["labels"]


@pytest.mark.asyncio
async def test_per_metric_labels_still_carry_the_real_dimensions(discovered):
    out = await discovered.discover_metrics("payment-service")
    charges = next(m for m in out["metrics"] if m["name"] == "payment_charges_total")
    assert charges["labels"] == ["reason", "status"]


@pytest.mark.asyncio
async def test_noise_and_sdk_internals_stay_out(discovered):
    out = await discovered.discover_metrics("payment-service")
    names = [m["name"] for m in out["metrics"]]
    assert "otel_sdk_exporter_span_exported" not in names
    assert "target_info" not in names
    http = next(m for m in out["metrics"] if m["name"].startswith("http_server"))
    assert "http_target" not in http["labels"]  # high cardinality
    assert "net_host_port" not in http["labels"]


@pytest.mark.asyncio
async def test_the_metric_name_itself_is_never_an_identity_label(discovered):
    out = await discovered.discover_metrics("payment-service")
    assert "__name__" not in out["identity_labels"]
