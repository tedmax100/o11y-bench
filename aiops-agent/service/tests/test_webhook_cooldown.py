"""The gate in front of the RCA: which alerts are suppressed as duplicates.

Day41 (second run): a drill and the real incident it rehearses share a
fingerprint, and the cooldown is ten minutes wide — so the rehearsal ate the
real alert that arrived four minutes later and the investigation never started.
Nothing failed; the alert was simply counted as a duplicate of the practice.
"""

import asyncio

import pytest

import app.webhook as wh


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    wh._last_run.clear()
    yield
    wh._last_run.clear()


@pytest.fixture(autouse=True)
def _no_real_rca(monkeypatch):
    """handle_alert spawns the investigation; this test is about the gate."""

    async def _noop(alert, fp):
        return None

    monkeypatch.setattr(wh, "_investigate_and_sink", _noop)


def _alert(**labels):
    base = {"alertname": "order-cancel-rate-high", "service_name": "order-service"}
    return {"status": "firing", "labels": {**base, **labels}}


async def _post(*alerts):
    return await wh.handle_alert({"alerts": list(alerts)})


async def test_a_drill_does_not_suppress_the_real_alert_that_follows():
    await _post(_alert(drill="true"))
    res = await _post(_alert())
    assert res["accepted"], f"the real alert was suppressed: {res['skipped']}"


async def test_the_real_alert_does_not_suppress_a_drill_either():
    await _post(_alert())
    res = await _post(_alert(drill="true"))
    assert res["accepted"]


async def test_two_real_alerts_still_deduplicate():
    """Separating rehearsals from production does not exempt either from the
    storm suppression that is the whole point of the cooldown."""
    await _post(_alert())
    res = await _post(_alert())
    assert not res["accepted"]
    assert res["skipped"][0]["reason"] == "cooldown"


async def test_two_drills_still_deduplicate():
    await _post(_alert(drill="true"))
    res = await _post(_alert(drill="true"))
    assert not res["accepted"]


async def test_the_fingerprint_itself_is_unchanged_by_the_drill_label():
    """`fp` is also the LangGraph thread id and the key past cases are retrieved
    by. Splitting it would hide a rehearsal's findings from the incident."""
    assert wh.fingerprint(_alert()["labels"]) == wh.fingerprint(_alert(drill="true")["labels"])


def test_cooldown_key_suffixes_only_the_drill_side():
    labels = _alert()["labels"]
    fp = wh.fingerprint(labels)
    assert wh._cooldown_key(fp, labels) == fp
    assert wh._cooldown_key(fp, _alert(drill="true")["labels"]) == f"{fp}|drill"


def test_a_non_firing_alert_is_skipped_before_the_cooldown_is_stamped():
    """A resolved notification must not claim the cooldown slot the next real
    firing needs."""
    resolved = {"status": "resolved", "labels": _alert()["labels"]}
    asyncio.run(_post(resolved))
    assert wh._last_run == {}
