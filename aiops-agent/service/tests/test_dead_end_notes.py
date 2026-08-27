"""Empty is not the same as 'nothing happened' (day36).

Three of four eval runs on the session-cache incident ended at "logs returned
no data"; one of them was filtering `level="error"` against services that emit
no level at all, and the only guidance it got back was a note about the time
window. The Prometheus side has told you the name does not exist — and
remembered it — since day one; this is that check on the Loki side, plus the
GitHub call that cannot resolve a ref in this environment.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.tools import github, query


@pytest.mark.asyncio
async def test_a_filter_on_a_field_these_logs_do_not_emit_says_so():
    fields = {"fields": [{"label": "event"}, {"label": "git_version"}]}
    with (
        patch.object(query, "_get_json", new=AsyncMock(return_value=fields)),
        patch.object(query.case_memory, "remember_dead_end", return_value=True) as remember,
    ):
        out = await query._loki_unknown_pipeline_fields(
            '{service_name="user-service"} | json | level="error"',
            '{service_name="user-service"}',
            {"service_name"},
            query._parse_dt("now-1h"),
            query._parse_dt("now"),
        )
    assert out is not None
    assert "Not a field on the lines in this window: level" in out["note"]
    assert "because of the filter, not because nothing happened" in out["hint"]
    # NOT remembered: detected_fields is scoped to this window, so on a quiet
    # hour it returns only the OTel envelope, and a dead end saying "event is
    # not a field" would outlive the quiet hour.
    remember.assert_not_called()


@pytest.mark.asyncio
async def test_a_filter_on_a_field_that_exists_is_left_alone():
    fields = {"fields": [{"label": "event"}]}
    with patch.object(query, "_get_json", new=AsyncMock(return_value=fields)):
        out = await query._loki_unknown_pipeline_fields(
            '{service_name="user-service"} | json | event="cache.miss"',
            '{service_name="user-service"}',
            {"service_name"},
            query._parse_dt("now-1h"),
            query._parse_dt("now"),
        )
    assert out is None


@pytest.mark.asyncio
async def test_parser_stages_and_line_filters_are_not_fields():
    """`| json` and `|= "boom"` name no field; flagging them would send a run
    away from a query that works."""
    with patch.object(query, "_get_json", new=AsyncMock(return_value={"fields": []})) as get:
        out = await query._loki_unknown_pipeline_fields(
            '{service_name="u"} |= "boom" | json | line_format "{{.msg}}"',
            '{service_name="u"}',
            {"service_name"},
            query._parse_dt("now-1h"),
            query._parse_dt("now"),
        )
    assert out is None
    get.assert_not_called()  # nothing to ask about


@pytest.mark.asyncio
async def test_no_detected_fields_fails_open():
    """A false 'that field does not exist' would push a run off the one query
    that works, so silence beats a guess."""
    with patch.object(query, "_get_json", new=AsyncMock(return_value={"fields": []})):
        out = await query._loki_unknown_pipeline_fields(
            '{service_name="u"} | json | level="error"',
            '{service_name="u"}',
            {"service_name"},
            query._parse_dt("now-1h"),
            query._parse_dt("now"),
        )
    assert out is None


@pytest.mark.asyncio
async def test_a_selector_key_is_not_reported_twice():
    """The `{...}` keys are the other branch's job."""
    with patch.object(query, "_get_json", new=AsyncMock(return_value={"fields": [{"label": "e"}]})):
        out = await query._loki_unknown_pipeline_fields(
            '{service_name="u"} | service_name="u"',
            '{service_name="u"}',
            {"service_name"},
            query._parse_dt("now-1h"),
            query._parse_dt("now"),
        )
    assert out is None


@pytest.mark.asyncio
async def test_github_compare_404_is_remembered_and_points_elsewhere():
    class _R:
        status_code = 404
        text = ""

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _R()

    with (
        patch.object(github.httpx, "AsyncClient", lambda **k: _Client()),
        patch.object(github.case_memory, "remember_dead_end", return_value=True) as remember,
    ):
        out = await github.github_compare.ainvoke(
            {"repo": "demo-services/user", "base": "v1.3.0", "head": "v1.3.0"}
        )
    assert "repo or refs not found" in out["error"]
    assert "k8s_change_provenance" in out["hint"]
    assert remember.call_args.args[0] == "query"
    assert "github_compare on demo-services/user" in remember.call_args.args[1]
