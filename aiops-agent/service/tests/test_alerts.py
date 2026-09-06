"""Unit tests for the design-alert capability (step 6). Pure transform + parse +
fail-closed provisioning gate; the one HTTP write is mocked so no Grafana needed."""

import pytest
from pydantic import ValidationError

import app.alerts as alerts_mod
from app.alerts import (
    AlertProvisioningDisabled,
    AlertSpec,
    build_alert_rule,
    parse_alert_blocks,
    provision_alert,
)


def _spec(**kw) -> AlertSpec:
    base = dict(title="t", expr="sum(rate(x_total[5m]))", threshold=0.05)
    base.update(kw)
    return AlertSpec(**base)


# ---- AlertSpec validation --------------------------------------------------


def test_spec_defaults():
    s = _spec()
    assert s.comparison == "gt" and s.for_duration == "5m" and s.severity == "warning"
    assert s.datasource_uid == "prometheus" and s.interval_seconds == 60


def test_spec_rejects_bad_comparison():
    with pytest.raises(ValidationError):
        _spec(comparison="between")


@pytest.mark.parametrize("bad", ["5", "5 min", "1d", "soon", ""])
def test_spec_rejects_bad_for_duration(bad):
    with pytest.raises(ValidationError):
        _spec(for_duration=bad)


@pytest.mark.parametrize("ok", ["30s", "5m", "1h"])
def test_spec_accepts_go_durations(ok):
    assert _spec(for_duration=ok).for_duration == ok


def test_spec_rejects_tiny_interval():
    with pytest.raises(ValidationError):
        _spec(interval_seconds=1)


# ---- build_alert_rule (pure transform; pin the payload shape) --------------


def test_build_rule_three_stage_pipeline():
    payload = build_alert_rule(_spec(title="my rule"))
    assert payload["title"] == "my rule"
    assert payload["condition"] == "C"  # threshold stage is the alerting condition
    refs = [d["refId"] for d in payload["data"]]
    assert refs == ["A", "B", "C"]


def test_build_rule_embeds_expr_as_instant_query():
    expr = "sum(rate(payment_charges_total[5m]))"
    a = build_alert_rule(_spec(expr=expr))["data"][0]
    assert a["model"]["expr"] == expr
    assert a["model"]["instant"] is True and a["model"]["range"] is False


def test_build_rule_threshold_and_comparison():
    c = build_alert_rule(_spec(threshold=0.2, comparison="lt"))["data"][2]
    cond = c["model"]["conditions"][0]["evaluator"]
    assert cond["type"] == "lt" and cond["params"] == [0.2]


def test_build_rule_carries_labels_and_annotations():
    payload = build_alert_rule(
        _spec(
            severity="critical",
            service_name="payment-service",
            summary="decline rate high",
            for_duration="10m",
        )
    )
    assert payload["labels"] == {"severity": "critical", "service_name": "payment-service"}
    assert payload["annotations"]["summary"] == "decline rate high"
    assert payload["for"] == "10m"


def test_build_rule_summary_falls_back_to_title():
    payload = build_alert_rule(_spec(title="only title", summary=""))
    assert payload["annotations"]["summary"] == "only title"


def test_build_rule_omits_service_label_when_absent():
    payload = build_alert_rule(_spec(service_name=None))
    assert payload["labels"] == {"severity": "warning"}


# ---- parse_alert_blocks (mirror of the plugin's splitQueryBlocks) ----------


def test_parse_single_block():
    text = """Here's an alert:

```alert
{"title": "x", "expr": "sum(rate(y[5m]))", "threshold": 0.1}
```
"""
    specs = parse_alert_blocks(text)
    assert len(specs) == 1 and specs[0].title == "x" and specs[0].threshold == 0.1


def test_parse_multiple_blocks():
    text = (
        '```alert\n{"title": "a", "expr": "e1", "threshold": 1}\n```\n'
        '```alert\n{"title": "b", "expr": "e2", "threshold": 2}\n```'
    )
    assert [s.title for s in parse_alert_blocks(text)] == ["a", "b"]


def test_parse_skips_malformed_block():
    text = (
        "```alert\nnot json\n```\n"  # bad json
        '```alert\n{"title": "ok", "expr": "e", "threshold": 1}\n```'  # good
    )
    specs = parse_alert_blocks(text)
    assert [s.title for s in specs] == ["ok"]


def test_parse_skips_block_failing_validation():
    # valid json but comparison is invalid → AlertSpec validation rejects it
    text = '```alert\n{"title": "x", "expr": "e", "threshold": 1, "comparison": "nope"}\n```'
    assert parse_alert_blocks(text) == []


def test_parse_accepts_a_json_fence_that_validates():
    """The model gets the JSON right far more often than the fence tag. A
    proposal rendered as a code block instead of a button is one the user has to
    hand-carry into Grafana, so the receiver is the lenient side."""
    text = '```json\n{"title": "x", "expr": "sum(rate(y[5m]))", "threshold": 0.1}\n```'
    specs = parse_alert_blocks(text)
    assert len(specs) == 1 and specs[0].title == "x"


def test_parse_ignores_json_that_is_not_an_alert():
    text = '```json\n{"some": "other payload"}\n```'
    assert parse_alert_blocks(text) == []


def test_parse_empty_and_no_blocks():
    assert parse_alert_blocks("") == []
    assert parse_alert_blocks("just prose, no fences") == []


# ---- provision_alert: fail-closed gate -------------------------------------


async def test_provision_refused_when_switch_off(monkeypatch):
    monkeypatch.setattr(alerts_mod.settings, "alert_provisioning_enabled", False)
    monkeypatch.setattr(alerts_mod.settings, "grafana_url", "http://g")
    monkeypatch.setattr(alerts_mod.settings, "grafana_token", "tok")
    with pytest.raises(AlertProvisioningDisabled):
        await provision_alert(_spec())


async def test_provision_refused_without_credentials(monkeypatch):
    monkeypatch.setattr(alerts_mod.settings, "alert_provisioning_enabled", True)
    monkeypatch.setattr(alerts_mod.settings, "grafana_url", "")
    monkeypatch.setattr(alerts_mod.settings, "grafana_token", "")
    with pytest.raises(AlertProvisioningDisabled):
        await provision_alert(_spec())


async def test_provision_posts_to_grafana(monkeypatch):
    monkeypatch.setattr(alerts_mod.settings, "alert_provisioning_enabled", True)
    monkeypatch.setattr(alerts_mod.settings, "grafana_url", "http://grafana:3000/")
    monkeypatch.setattr(alerts_mod.settings, "grafana_token", "tok")

    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"uid": "abc123", "title": "t"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers):
            captured["folder_get"] = url
            return _Resp()  # 200 → the folder is already there

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Resp()

    monkeypatch.setattr(alerts_mod.httpx, "AsyncClient", lambda **kw: _Client())

    result = await provision_alert(_spec(title="t"))
    assert result == {"uid": "abc123", "title": "t"}
    assert captured["folder_get"].endswith("/api/folders/aiops")
    # rstrip('/') on the base url, hits the provisioning endpoint with bearer auth
    assert captured["url"] == "http://grafana:3000/api/v1/provisioning/alert-rules"
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["json"]["condition"] == "C"


@pytest.mark.asyncio
async def test_provision_creates_the_folder_when_it_is_missing(monkeypatch):
    """Grafana refuses a rule whose folder is missing, and the person who clicked
    the button never chose that folder — so the folder is ours to create."""
    monkeypatch.setattr(alerts_mod.settings, "alert_provisioning_enabled", True)
    monkeypatch.setattr(alerts_mod.settings, "grafana_url", "http://grafana:3000/")
    monkeypatch.setattr(alerts_mod.settings, "grafana_token", "tok")

    posts: list[str] = []

    class _Resp:
        def __init__(self, status_code=200, body=None):
            self.status_code = status_code
            self._body = body or {"uid": "abc123", "title": "t"}

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers):
            return _Resp(status_code=404, body={})

        async def post(self, url, json, headers):
            posts.append(url)
            return _Resp()

    monkeypatch.setattr(alerts_mod.httpx, "AsyncClient", lambda **kw: _Client())

    await provision_alert(_spec(title="t"))
    assert posts[0].endswith("/api/folders")  # folder first
    assert posts[1].endswith("/api/v1/provisioning/alert-rules")
