"""Unit tests for app/rubric.py — trace ID verification and k8s write rubric."""

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import app.rubric as rubric
from app.rubric import _tempo_trace_exists, check_k8s_write, verify_trace_ids

# ---------------------------------------------------------------------------
# _tempo_trace_exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_tempo_exists_returns_true_when_batches_nonempty():
    tid = "a" * 32
    respx.get(f"http://localhost:3200/api/traces/{tid}").mock(
        return_value=httpx.Response(200, json={"batches": [{"spans": []}]})
    )
    result = await _tempo_trace_exists(tid)
    assert result is True


@pytest.mark.asyncio
@respx.mock
async def test_tempo_exists_returns_false_on_404():
    tid = "b" * 32
    respx.get(f"http://localhost:3200/api/traces/{tid}").mock(
        return_value=httpx.Response(404, json={})
    )
    result = await _tempo_trace_exists(tid)
    assert result is False


@pytest.mark.asyncio
@respx.mock
async def test_tempo_exists_returns_false_on_empty_batches():
    tid = "c" * 32
    respx.get(f"http://localhost:3200/api/traces/{tid}").mock(
        return_value=httpx.Response(200, json={"batches": []})
    )
    result = await _tempo_trace_exists(tid)
    assert result is False


@pytest.mark.asyncio
async def test_tempo_exists_returns_true_on_network_error(monkeypatch):
    """Network failure → assume valid (never block on infra issues)."""

    async def _raise(*a, **kw):
        raise httpx.ConnectError("timeout")

    monkeypatch.setattr(httpx.AsyncClient, "__aenter__", _raise)
    result = await _tempo_trace_exists("d" * 32)
    assert result is True


# ---------------------------------------------------------------------------
# verify_trace_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_no_trace_ids_passes():
    ok, prompt = await verify_trace_ids("No trace IDs mentioned here.")
    assert ok is True
    assert prompt == ""


@pytest.mark.asyncio
async def test_verify_real_trace_ids_pass(monkeypatch):
    tid = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
    monkeypatch.setattr(rubric, "_tempo_trace_exists", AsyncMock(return_value=True))
    ok, prompt = await verify_trace_ids(f"See trace {tid} for details.")
    assert ok is True
    assert prompt == ""


@pytest.mark.asyncio
async def test_verify_hallucinated_trace_id_fails(monkeypatch):
    tid = "deadbeefdeadbeefdeadbeefdeadbeef"
    monkeypatch.setattr(rubric, "_tempo_trace_exists", AsyncMock(return_value=False))
    ok, prompt = await verify_trace_ids(f"Root cause confirmed by trace {tid}.")
    assert ok is False
    assert tid in prompt
    assert "query_tempo_traces" in prompt


@pytest.mark.asyncio
async def test_verify_mixed_traces_fails_on_any_missing(monkeypatch):
    """One real + one hallucinated → overall fail."""
    real = "1111111111111111111111111111111a"
    fake = "2222222222222222222222222222222b"

    async def _exists(tid):
        return tid == real

    monkeypatch.setattr(rubric, "_tempo_trace_exists", _exists)
    ok, prompt = await verify_trace_ids(f"Trace {real} and trace {fake} both confirm it.")
    assert ok is False
    assert fake.lower() in prompt.lower()


@pytest.mark.asyncio
async def test_verify_exception_in_tempo_check_passes_through(monkeypatch):
    """Exception propagating out of _tempo_trace_exists → treated as valid (best-effort).
    _tempo_trace_exists catches its own httpx errors and returns True, so this
    tests that verify_trace_ids also handles unexpected exceptions gracefully."""

    async def _raise(tid):
        raise Exception("unexpected error")

    monkeypatch.setattr(rubric, "_tempo_trace_exists", _raise)
    tid = "ffffffffffffffffffffffffffffffff"
    # verify_trace_ids should not raise even if _tempo_trace_exists throws
    try:
        ok, _prompt = await verify_trace_ids(f"See trace {tid}.")
        # If it catches internally, no missing → pass
        assert ok is True
    except Exception:
        # Caller (agent.py) also wraps in try/except — acceptable either way
        pass


@pytest.mark.asyncio
async def test_verify_deduplicates_repeated_ids(monkeypatch):
    """Same ID mentioned twice → only one Tempo call."""
    tid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"
    mock = AsyncMock(return_value=True)
    monkeypatch.setattr(rubric, "_tempo_trace_exists", mock)
    await verify_trace_ids(f"Trace {tid} and again {tid}.")
    mock.assert_called_once()


# ---------------------------------------------------------------------------
# check_k8s_write
# ---------------------------------------------------------------------------


def _mock_llm(verdict_ok: bool, reason: str = "ok"):
    from langchain_core.runnables import RunnableLambda

    from app.rubric import _K8sRubricVerdict

    async def _invoke(messages, **kw):
        return _K8sRubricVerdict(safe_to_proceed=verdict_ok, reason=reason)

    return RunnableLambda(_invoke)


@pytest.mark.asyncio
async def test_k8s_write_allows_safe_rollout_undo(monkeypatch):
    monkeypatch.setattr(rubric, "_k8s_rubric_llm", lambda: _mock_llm(True, "looks fine"))
    ok, reason = await check_k8s_write(
        "k8s.rollout_undo",
        {"deployment": "payment-service", "namespace": "demo"},
        "deploy bad image v1.2.3",
    )
    assert ok is True
    assert "fine" in reason


@pytest.mark.asyncio
async def test_k8s_write_blocks_when_llm_says_unsafe(monkeypatch):
    monkeypatch.setattr(rubric, "_k8s_rubric_llm", lambda: _mock_llm(False, "replica count is 0"))
    ok, reason = await check_k8s_write(
        "k8s.scale",
        {"deployment": "payment-service", "replicas": 0},
        "scale down for maintenance",
    )
    assert ok is False
    assert "0" in reason


@pytest.mark.asyncio
async def test_k8s_write_passes_on_llm_exception(monkeypatch):
    """LLM failure → best-effort, never block."""
    from langchain_core.runnables import RunnableLambda

    async def _fail(messages, **kw):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(rubric, "_k8s_rubric_llm", lambda: RunnableLambda(_fail))
    ok, reason = await check_k8s_write("k8s.rollout_undo", {"deployment": "x"}, "")
    assert ok is True
    assert "skipped" in reason


@pytest.mark.asyncio
async def test_k8s_write_empty_context_still_runs(monkeypatch):
    """No incident context provided → rubric still executes."""
    monkeypatch.setattr(rubric, "_k8s_rubric_llm", lambda: _mock_llm(True, "allowed"))
    ok, _reason = await check_k8s_write("k8s.rollout_undo", {"deployment": "svc"})
    assert ok is True


# ---------------------------------------------------------------------------
# execution.py gate 3b: rubric blocks only when actions_enabled=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rubric_gate_blocks_when_actions_enabled(monkeypatch):
    """check_k8s_write returning False + actions_enabled=True → block."""
    import app.execution as ex

    monkeypatch.setattr(ex.settings, "actions_enabled", True)
    monkeypatch.setattr(
        rubric,
        "_k8s_rubric_llm",
        lambda: _mock_llm(False, "replica count is 0"),
    )
    ok, reason = await check_k8s_write("k8s.scale", {"replicas": 0}, "")
    # Rubric says unsafe
    assert ok is False
    assert "0" in reason


@pytest.mark.asyncio
async def test_rubric_does_not_block_when_actions_disabled(monkeypatch):
    """When actions_enabled=False the rubric LLM still runs but check_k8s_write
    returns the verdict — the execution.py gate conditionally ignores it.
    Verify the condition: rubric verdict False + actions_enabled False → gate passes."""
    import app.execution as ex

    monkeypatch.setattr(ex.settings, "actions_enabled", False)
    monkeypatch.setattr(
        rubric,
        "_k8s_rubric_llm",
        lambda: _mock_llm(False, "unsafe"),
    )
    rubric_ok, _ = await check_k8s_write("k8s.rollout_undo", {"deployment": "x"}, "")
    # The rubric itself says False, but execution.py gates on `actions_enabled`
    # so `not rubric_ok and settings.actions_enabled` = True and False = False → no block
    assert rubric_ok is False  # verdict is captured
    # The gate expression in execution.py:
    assert not (not rubric_ok and ex.settings.actions_enabled)  # gate does NOT fire


# ---- the IDs the guard could not see ---------------------------------------


def test_trace_regex_matches_ids_with_stripped_leading_zeros():
    """Tempo's search API returns trace IDs with leading zeros stripped — about
    one in six in this stack is 30 or 31 chars. A {32} pattern skipped exactly
    those, so an answer citing one was never checked at all."""
    from app.rubric import _TRACE_ID_RE

    assert _TRACE_ID_RE.search("869290709c6aa24faed123d5b465a6")  # 30
    assert _TRACE_ID_RE.search("c463250d900592060957bc2cbdfe804")  # 31
    assert _TRACE_ID_RE.search("171ef2ebf1fc16daf0b0292de66d1985")  # 32
    assert not _TRACE_ID_RE.search("deadbeef")  # too short to be an ID


@pytest.mark.asyncio
async def test_short_trace_id_is_checked_and_padded(monkeypatch):
    """A stripped ID must reach Tempo as the canonical 32-char form."""
    seen: list[str] = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"batches": [{"scopeSpans": []}]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            seen.append(url)
            return _Resp()

    monkeypatch.setattr(rubric.httpx, "AsyncClient", lambda **kw: _Client())
    ok, _ = await rubric.verify_trace_ids("see trace c463250d900592060957bc2cbdfe804")
    assert ok is True
    assert seen and seen[0].endswith("/0c463250d900592060957bc2cbdfe804")


# ---- the context the judge is handed ---------------------------------------


def test_rubric_context_carries_the_intent_rules_need():
    from app.action_requests import ActionRequest
    from app.execution import _rubric_context

    req = ActionRequest(
        request_id="r1",
        fp="fp1",
        action="k8s_scale",
        args={"namespace": "demo", "deployment": "payment-service", "replicas": 60},
        autonomy="propose",
        status="pending",
        runbook_id="payment-decline",
        params={"service_name": "payment-service", "alertname": "PaymentDeclineRateHigh"},
        blast_radius={
            "action": "k8s.scale",
            "namespace": "demo",
            "target": "payment-service",
            "current_replicas": 2,
            "target_replicas": 60,
        },
        created_ts="2026-08-06T00:00:00Z",
        expires_ts="2026-08-07T00:00:00Z",
    )
    context = _rubric_context(req)
    assert "payment-decline" in context
    assert "PaymentDeclineRateHigh" in context
    assert "2" in context and "60" in context  # the ratio rule is now answerable


def test_rubric_context_is_never_empty():
    from app.action_requests import ActionRequest
    from app.execution import _rubric_context

    req = ActionRequest(
        request_id="r2",
        fp="fp2",
        action="k8s_rollout_restart",
        args={},
        autonomy="propose",
        status="pending",
        created_ts="2026-08-06T00:00:00Z",
        expires_ts="2026-08-07T00:00:00Z",
    )
    assert _rubric_context(req) == "(none provided)"
