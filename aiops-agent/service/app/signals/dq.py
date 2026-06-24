"""Data-Quality SLO — turns the topology reconcile (s2) into a governance signal
(signal-plane-design s5; ARE flagship SLO #2 after calibration error).

ARE reads its flagship SLOs *together*: high autonomy on low data-quality is
unsafe (acting confidently on a wrong map). So just as the governance gate
withholds AUTO when calibration is poor/unproven, it should withhold AUTO when
the Signal Plane isn't decision-grade — the declared topology has drifted from
the live call graph, the last reconcile is stale, or no reconcile has happened.

`dq_verdict()` reduces the cached reconcile drift to the same shape governance
already consumes for calibration — a dict with `proven_good` + a note — so
governance treats DQ exactly like calibration without importing the signal layer.
Conservative by construction: no reconcile evidence → not proven-good → AUTO
withheld (autonomy is earned).
"""

from __future__ import annotations

import time

from ..config import settings
from .reconcile import get_last_drift


def dq_verdict() -> dict:
    """{proven_good, score, note} for governance. proven-good requires a recent
    reconcile with no observed-but-undeclared edges and agreement ≥ floor."""
    drift = get_last_drift()
    if drift is None or not drift.traces_sampled:
        return {
            "proven_good": False,
            "score": None,
            "note": "topology not reconciled against live traces; DQ unproven",
        }

    age = int(time.time() - drift.computed_ts)
    if age > settings.dq_max_reconcile_age_seconds:
        return {
            "proven_good": False,
            "score": drift.dq_score,
            "note": (
                f"last reconcile {age}s old (> {settings.dq_max_reconcile_age_seconds}s); DQ stale"
            ),
        }
    if drift.undeclared_edges:
        n = len(drift.undeclared_edges)
        return {
            "proven_good": False,
            "score": drift.dq_score,
            "note": f"{n} observed-but-undeclared edge(s) (topology drift); DQ degraded",
        }
    if drift.dq_score is not None and drift.dq_score < settings.dq_min_score:
        return {
            "proven_good": False,
            "score": drift.dq_score,
            "note": (
                f"declared/observed agreement {drift.dq_score}"
                f" < {settings.dq_min_score}; DQ degraded"
            ),
        }
    return {
        "proven_good": True,
        "score": drift.dq_score,
        "note": f"topology aligned to live traffic (agreement {drift.dq_score}, "
        f"{drift.traces_sampled} traces, reconciled {age}s ago)",
    }
