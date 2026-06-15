"""Unit tests for the Tier 0/1 runbook layer. Matching, substitution and the
read-only diagnostics runner are pure/mockable, so no live infra is needed."""

import app.runbook as rb
from app.runbook import (
    DiagnosticCheck,
    Runbook,
    Step,
    Trigger,
    _evaluate_check,
    incident_params,
    match_runbook,
    render_runbook,
    run_diagnostics,
)


def _book(**kw):
    base = dict(id="payment-bad-deploy", trigger=Trigger(
        alertname="payment-decline-rate-high", labels={"service_name": "payment-service"}))
    base.update(kw)
    return Runbook(**base)


class _FakeTool:
    """Mimics a langchain BaseTool: has .ainvoke(args) -> result."""
    def __init__(self, name, result=None, raises=None):
        self.name = name
        self._result = result
        self._raises = raises

    async def ainvoke(self, args):
        if self._raises:
            raise self._raises
        return self._result


# ---- load ------------------------------------------------------------------

def test_load_real_demo_runbook(tmp_path):
    # the shipped runbook must parse
    books = rb.load_runbooks("runbooks")
    ids = {b.id for b in books}
    assert "payment-bad-deploy" in ids
    pb = next(b for b in books if b.id == "payment-bad-deploy")
    assert pb.diagnostics and pb.remediation
    # remediation step must be flagged approval-required (never auto-run)
    assert pb.remediation[0].requires_approval is True


def test_load_skips_malformed(tmp_path):
    (tmp_path / "good.yaml").write_text("id: ok\n")
    (tmp_path / "bad.yaml").write_text("id: [unclosed\n")
    books = rb.load_runbooks(tmp_path)
    assert {b.id for b in books} == {"ok"}


# ---- match -----------------------------------------------------------------

def test_match_by_explicit_runbook_id():
    books = [_book(id="other", trigger=Trigger()), _book()]
    m = match_runbook({}, {"runbook_id": "payment-bad-deploy"}, books)
    assert m and m.id == "payment-bad-deploy"


def test_match_by_trigger_labels_and_alertname():
    books = [_book()]
    m = match_runbook(
        {"alertname": "payment-decline-rate-high", "service_name": "payment-service"}, {}, books)
    assert m and m.id == "payment-bad-deploy"


def test_no_match_wrong_service():
    books = [_book()]
    assert match_runbook(
        {"alertname": "payment-decline-rate-high", "service_name": "order-service"}, {}, books) is None


def test_empty_trigger_never_matches_everything():
    books = [Runbook(id="catch-all", trigger=Trigger())]
    assert match_runbook({"alertname": "anything"}, {}, books) is None


# ---- params + substitution -------------------------------------------------

def test_incident_params_aliases_service():
    p = incident_params({"service": "payment-service", "git_version": "v2.5.0"}, {"sev": "warn"})
    assert p["service_name"] == "payment-service" and p["git_version"] == "v2.5.0" and p["sev"] == "warn"


def test_render_fills_params_and_flags_remediation():
    book = _book(
        title="t",
        diagnostics=[Step(desc="check", action="k8s_deployment_status",
                          args={"service": "{service_name}"}, expect="healthy")],
        remediation=[Step(desc="rollback", action="k8s.rollout_undo",
                          reversible=True, requires_approval=True)],
    )
    out = render_runbook(book, {"service_name": "payment-service"})
    assert "payment-service" in out
    assert "approval required" in out and "NOT auto-executed" in out


# ---- check evaluation ------------------------------------------------------

def test_evaluate_check_variants():
    assert _evaluate_check(None, {"x": 1})[0] == "ran"
    assert _evaluate_check(DiagnosticCheck(contains="v2.5.0"), {"v": "v2.5.0"})[0] == "pass"
    assert _evaluate_check(DiagnosticCheck(contains="zzz"), {"v": "v2.5.0"})[0] == "fail"
    assert _evaluate_check(DiagnosticCheck(min_rows=2), {"data": [1, 2, 3]})[0] == "pass"
    assert _evaluate_check(DiagnosticCheck(min_rows=5), {"data": [1]})[0] == "fail"
    assert _evaluate_check(DiagnosticCheck(nonempty=True), {"data": []})[0] == "fail"
    assert _evaluate_check(DiagnosticCheck(nonempty=True), {"data": [1]})[0] == "pass"


# ---- diagnostics runner ----------------------------------------------------

async def test_run_diagnostics_pass_and_check():
    book = _book(diagnostics=[
        Step(desc="version concentration", action="query_prometheus",
             args={"expr": "sum by (git_version) (...)"},
             check=DiagnosticCheck(nonempty=True)),
    ])
    tools = {"query_prometheus": _FakeTool("query_prometheus", result={"data": [{"v": 1}]})}
    res = await run_diagnostics(book, {}, tools)
    assert len(res) == 1 and res[0].status == "pass"
    assert res[0].output_preview


async def test_run_diagnostics_skips_non_readonly_action():
    # a remediation-style action not in the read-only map must be skipped, never run
    book = _book(diagnostics=[Step(desc="rollback?!", action="k8s.rollout_undo", args={})])
    res = await run_diagnostics(book, {}, {"query_prometheus": _FakeTool("query_prometheus")})
    assert res[0].status == "skipped" and "remediation" in res[0].detail


async def test_run_diagnostics_skips_unresolved_params():
    book = _book(diagnostics=[
        Step(desc="diff", action="github_compare",
             args={"repo": "r", "base": "{prev_version}", "head": "{git_version}"}),
    ])
    tools = {"github_compare": _FakeTool("github_compare", result="ok")}
    res = await run_diagnostics(book, {"git_version": "v2.5.0"}, tools)  # prev_version missing
    assert res[0].status == "skipped" and "prev_version" in res[0].detail


async def test_run_diagnostics_records_tool_error():
    book = _book(diagnostics=[Step(desc="x", action="query_prometheus", args={})])
    tools = {"query_prometheus": _FakeTool("query_prometheus", raises=RuntimeError("boom"))}
    res = await run_diagnostics(book, {}, tools)
    assert res[0].status == "error" and "boom" in res[0].detail
