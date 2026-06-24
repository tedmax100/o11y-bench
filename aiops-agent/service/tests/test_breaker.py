"""Unit tests for the circuit breaker (7b-3): consecutive-failure trip, manual
reset-only, and the global window rate limit. All store-backed, so state is what
a real (restart-surviving) breaker would see."""

import app.breaker as bk


def _db(monkeypatch, tmp_path):
    p = tmp_path / "aiops.db"
    monkeypatch.setattr(bk.settings, "store_path", str(p))
    monkeypatch.setattr(bk.settings, "breaker_enabled", True)
    return p


def test_closed_by_default(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    ok, reason = bk.check("k8s.rollout_undo", "demo/x", path=p)
    assert ok and reason == "closed"


def test_trips_after_consecutive_failures(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(bk.settings, "breaker_fail_threshold", 2)
    monkeypatch.setattr(bk.settings, "breaker_max_actions_per_window", 100)  # isolate the trip
    bk.record_outcome("k8s.rollout_undo", "demo/x", success=False, path=p)
    assert bk.check("k8s.rollout_undo", "demo/x", path=p)[0] is True  # 1 fail < threshold
    bk.record_outcome("k8s.rollout_undo", "demo/x", success=False, path=p)
    ok, reason = bk.check("k8s.rollout_undo", "demo/x", path=p)
    assert ok is False and "breaker open" in reason


def test_success_resets_failure_streak(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(bk.settings, "breaker_fail_threshold", 2)
    monkeypatch.setattr(bk.settings, "breaker_max_actions_per_window", 100)
    bk.record_outcome("k8s.rollout_undo", "demo/x", success=False, path=p)
    bk.record_outcome("k8s.rollout_undo", "demo/x", success=True, path=p)  # streak cleared
    bk.record_outcome("k8s.rollout_undo", "demo/x", success=False, path=p)  # streak = 1
    assert bk.check("k8s.rollout_undo", "demo/x", path=p)[0] is True


def test_open_stays_open_until_reset(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(bk.settings, "breaker_fail_threshold", 1)
    monkeypatch.setattr(bk.settings, "breaker_max_actions_per_window", 100)
    bk.record_outcome("k8s.rollout_undo", "demo/x", success=False, path=p)  # trips
    assert bk.check("k8s.rollout_undo", "demo/x", path=p)[0] is False
    # a later success does NOT auto-close it
    bk.record_outcome("k8s.rollout_undo", "demo/x", success=True, path=p)
    assert bk.check("k8s.rollout_undo", "demo/x", path=p)[0] is False
    # only a manual reset closes it
    assert bk.reset(bk.scope_key("k8s.rollout_undo", "demo/x"), path=p) == 1
    assert bk.check("k8s.rollout_undo", "demo/x", path=p)[0] is True


def test_global_window_rate_limit(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(bk.settings, "breaker_fail_threshold", 100)  # isolate the rate limit
    monkeypatch.setattr(bk.settings, "breaker_max_actions_per_window", 3)
    # three executions across different targets still count toward the global limit
    for t in ("demo/a", "demo/b", "demo/c"):
        bk.record_outcome("k8s.rollout_undo", t, success=True, path=p)
    ok, reason = bk.check("k8s.rollout_undo", "demo/d", path=p)
    assert ok is False and "rate limit" in reason


def test_reset_all_and_snapshot(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(bk.settings, "breaker_fail_threshold", 1)
    monkeypatch.setattr(bk.settings, "breaker_max_actions_per_window", 100)
    bk.record_outcome("k8s.rollout_undo", "demo/x", success=False, path=p)
    bk.record_outcome("k8s.scale", "demo/y", success=False, path=p)
    assert len(bk.snapshot(path=p)) == 2
    assert bk.reset(None, path=p) == 2  # reset all
    assert bk.snapshot(path=p) == []


def test_disabled_breaker_always_allows(tmp_path, monkeypatch):
    p = _db(monkeypatch, tmp_path)
    monkeypatch.setattr(bk.settings, "breaker_fail_threshold", 1)
    monkeypatch.setattr(bk.settings, "breaker_enabled", False)
    bk.record_outcome("k8s.rollout_undo", "demo/x", success=False, path=p)  # would trip
    ok, reason = bk.check("k8s.rollout_undo", "demo/x", path=p)
    assert ok and "disabled" in reason
