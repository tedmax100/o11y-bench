"""Calibration-error (CE) measurement harness — ARE gap-analysis §4.2 step 2.

ARE names CE ("confidently wrong") the most important health metric: a stated
confidence must track the actual success rate, and if it drifts the agent should
narrow its autonomy. Nothing measured it yet — the headless path *emits*
`Findings.confidence` but never compares it to reality. This module closes that
loop, and is the prerequisite for any Tier 2 confidence threshold (v3 §8).

Two-phase by necessity: at run time we have the confidence but NOT the verdict
(no grader/human has judged the RCA yet). So:

  1. **online** (webhook path) — `record_run()` appends a *pending* record
     (confidence + a digest of the findings), best-effort, never breaking the run.
  2. **offline** — `label_run()` fills in `correct` once a verdict exists. The
     verdict source is pluggable: thresholded o11y-bench grading score
     (`score_to_correct`) or a lightweight ground-truth match (`grade_against_truth`).

The calibration math (`compute_calibration`) is pure and deterministic — it's
the part with real logic, so it's what the unit tests pin down. Records live in
the durable SQLite store (`app.store`), so `label_run` is an atomic UPDATE rather
than a whole-file rewrite, and the data survives the pod restarts the execution
plane triggers. See store.py for why JSONL was retired (7b-0).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import store
from .config import settings

logger = logging.getLogger("aiops_agent.calibration")


class CalibrationRecord(BaseModel):
    """One headless RCA run + (eventually) its correctness verdict."""

    run_id: str = Field(description="Alert fingerprint / thread_id of the run.")
    ts: str = Field(description="RFC3339 time the run was recorded.")
    confidence: float = Field(description="The run's stated confidence, 0.0-1.0.")
    # Filled in offline. None = not yet judged (excluded from CE until labeled).
    correct: bool | None = None
    score: float | None = Field(default=None, description="Raw grading score, if from o11y-bench.")
    source: str | None = Field(
        default=None, description="How `correct` was decided (truth/grader)."
    )
    # Digest for after-the-fact labeling — enough to judge without re-running.
    summary: str = ""
    hypothesis: str = ""
    suspected_version: str | None = None
    services: list[str] = Field(default_factory=list)
    # Filled in when correct=False: which part was wrong + human-supplied note.
    error_dimension: str | None = None  # root_cause | scope | action | other
    correction_note: str | None = None


# ---- store (durable SQLite via app.store; `path` = db path) -----------------

def load_records(path: Path | None = None) -> list[CalibrationRecord]:
    out: list[CalibrationRecord] = []
    for d in store.cal_load(path):
        try:
            out.append(CalibrationRecord.model_validate(d))
        except Exception as e:  # one bad row must not sink the whole report
            logger.warning("skipping malformed calibration row: %s", e)
    return out


def record_run(findings: Any, run_id: str, path: Path | None = None) -> CalibrationRecord | None:
    """Append a pending record for a finished headless run. Best-effort: returns
    None and logs on any failure, never raises into the run."""
    if not settings.calibration_enabled:
        return None
    try:
        rec = CalibrationRecord(
            run_id=run_id,
            ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            confidence=float(getattr(findings, "confidence", 0.0) or 0.0),
            summary=getattr(findings, "summary", "") or "",
            hypothesis=getattr(findings, "hypothesis", "") or "",
            suspected_version=getattr(findings, "suspected_version", None),
            services=list(getattr(findings, "services", []) or []),
        )
        store.cal_insert(
            run_id=rec.run_id, ts=rec.ts, confidence=rec.confidence,
            summary=rec.summary, hypothesis=rec.hypothesis,
            suspected_version=rec.suspected_version, services=rec.services, path=path,
        )
        return rec
    except Exception as e:
        logger.warning("record_run failed for %s: %s", run_id, e)
        return None


def label_run(
    run_id: str,
    correct: bool,
    *,
    score: float | None = None,
    source: str = "manual",
    error_dimension: str | None = None,
    correction_note: str | None = None,
    path: Path | None = None,
) -> bool:
    """Fill in the verdict for the most recent record matching run_id (atomic
    UPDATE in the store). Returns True if a record was updated."""
    ok = store.cal_label(
        run_id, correct, score=score, source=source,
        error_dimension=error_dimension, correction_note=correction_note,
        path=path,
    )
    if not ok:
        logger.warning("label_run: no record for run_id=%s", run_id)
    return ok


# ---- correctness sources (pluggable) ---------------------------------------

def score_to_correct(score: float, threshold: float | None = None) -> bool:
    """o11y-bench grading produces a 0-1 score; a run counts as correct when it
    clears the threshold. Keeps the CE harness decoupled from the grader's
    internals — we record the number it already produces."""
    thr = settings.calibration_correct_threshold if threshold is None else threshold
    return score >= thr


def grade_against_truth(findings: Any, truth: dict) -> bool:
    """Lightweight ground-truth match for the demo incident: the RCA is 'correct'
    when it names the right service AND, if a culprit version is known, fingers it.
    `truth` = {"service": "payment-service", "version": "v2.5.0"} (version optional)."""
    want_svc = (truth.get("service") or "").lower()
    if want_svc:
        got = {s.lower() for s in (getattr(findings, "services", []) or [])}
        # also accept the service appearing in the summary, since the model
        # sometimes states it in prose rather than the structured field.
        in_summary = want_svc in (getattr(findings, "summary", "") or "").lower()
        if want_svc not in got and not in_summary:
            return False
    want_ver = truth.get("version")
    if want_ver:
        got_ver = (getattr(findings, "suspected_version", None) or "")
        if want_ver.lower() not in got_ver.lower():
            return False
    return True


# ---- calibration math (pure) -----------------------------------------------

def compute_calibration(records: list[CalibrationRecord], n_bins: int = 10) -> dict[str, Any]:
    """Expected/Maximum Calibration Error + Brier score + reliability bins over
    the *labeled* records. Equal-width bins on [0,1] by stated confidence.

    ECE = Σ_b (n_b/N)·|acc_b − conf_b|   (lower is better; 0 = perfectly calibrated)
    MCE = max_b |acc_b − conf_b|         (worst single bin)
    Brier = mean((conf − correct)²)      (proper score; lower is better)
    """
    labeled = [r for r in records if r.correct is not None]
    n = len(labeled)
    if n == 0:
        return {"count": 0, "labeled": 0, "ece": None, "mce": None, "brier": None, "bins": []}

    # Bin index: confidence c → min(floor(c·n_bins), n_bins-1) so c==1.0 lands in
    # the last bin rather than overflowing.
    bins: list[dict[str, Any]] = []
    ece = 0.0
    mce = 0.0
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        members = [
            r for r in labeled
            if (min(int(r.confidence * n_bins), n_bins - 1) == i)
        ]
        if not members:
            bins.append({"lo": lo, "hi": hi, "count": 0,
                         "avg_confidence": None, "accuracy": None, "gap": None})
            continue
        avg_conf = sum(r.confidence for r in members) / len(members)
        acc = sum(1 for r in members if r.correct) / len(members)
        gap = abs(acc - avg_conf)
        ece += (len(members) / n) * gap
        mce = max(mce, gap)
        bins.append({
            "lo": lo, "hi": hi, "count": len(members),
            "avg_confidence": round(avg_conf, 4),
            "accuracy": round(acc, 4),
            "gap": round(gap, 4),
        })

    brier = sum((r.confidence - (1.0 if r.correct else 0.0)) ** 2 for r in labeled) / n
    overall_acc = sum(1 for r in labeled if r.correct) / n
    overall_conf = sum(r.confidence for r in labeled) / n
    return {
        "count": len(records),
        "labeled": n,
        "ece": round(ece, 4),
        "mce": round(mce, 4),
        "brier": round(brier, 4),
        "overall_accuracy": round(overall_acc, 4),
        "overall_confidence": round(overall_conf, 4),
        # >0 ⇒ overconfident (says more than it delivers); <0 ⇒ underconfident.
        "overconfidence": round(overall_conf - overall_acc, 4),
        "bins": bins,
    }


def format_report(calib: dict[str, Any]) -> str:
    if not calib.get("labeled"):
        return (f"No labeled runs yet ({calib.get('count', 0)} recorded, "
                "0 with a correctness verdict). Label runs with "
                "`python -m app.calibration label <run_id> --correct/--wrong`.")
    lines = [
        f"Calibration over {calib['labeled']} labeled run(s) "
        f"(of {calib['count']} recorded):",
        f"  ECE   = {calib['ece']}   (expected calibration error; 0 = perfect)",
        f"  MCE   = {calib['mce']}   (worst bin)",
        f"  Brier = {calib['brier']}",
        f"  accuracy {calib['overall_accuracy']} vs confidence {calib['overall_confidence']} "
        f"→ overconfidence {calib['overconfidence']:+}",
        "  reliability:",
        "    bin            n   conf    acc    gap",
    ]
    for b in calib["bins"]:
        if not b["count"]:
            continue
        lines.append(
            f"    [{b['lo']:.1f},{b['hi']:.1f})  {b['count']:>3}  "
            f"{b['avg_confidence']:.3f}  {b['accuracy']:.3f}  {b['gap']:.3f}"
        )
    return "\n".join(lines)


# ---- CLI -------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="app.calibration", description="CE harness")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report", help="print the calibration report")
    pl = sub.add_parser("label", help="set a run's correctness verdict")
    pl.add_argument("run_id")
    g = pl.add_mutually_exclusive_group(required=True)
    g.add_argument("--correct", action="store_true")
    g.add_argument("--wrong", action="store_true")
    pl.add_argument("--score", type=float, default=None)
    pl.add_argument("--source", default="manual")

    args = parser.parse_args(argv)
    if args.cmd == "report":
        print(format_report(compute_calibration(load_records())))
        return 0
    if args.cmd == "label":
        ok = label_run(args.run_id, correct=args.correct, score=args.score, source=args.source)
        print("updated" if ok else f"no record for run_id={args.run_id}")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
