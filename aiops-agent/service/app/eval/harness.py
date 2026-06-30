"""The eval harness: load fixtures → run the real agent N times each → grade →
report + regress + feed calibration.

The unit under test is `app.agent.run_headless` — the same code the alert
webhook drives in production (playbook, capability snapshot, discover-before-
query, hypothesis loop, governance). Nothing here mocks the agent; it needs the
live observability stack reachable (PROMETHEUS_URL / LOKI_URL / TEMPO_URL) and a
GOOGLE_API_KEY, exactly like a real headless run.

Grading reuses `calibration.grade_against_truth` (service + optional version
match) so the harness stays decoupled from how correctness is judged. Each run is
also inserted+labeled into the calibration store, so a harness pass produces the
dense, unbiased CE data that production alone never gathers.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from .. import store
from ..agent import run_headless
from ..calibration import grade_against_truth

_HERE = Path(__file__).parent
DEFAULT_FIXTURES = _HERE / "fixtures.yaml"
DEFAULT_BASELINE = _HERE / "baseline.json"
DEFAULT_STORE = _HERE / "eval.db"  # separate from prod aiops.db unless overridden


# ---- fixtures ---------------------------------------------------------------


class Fixture(BaseModel):
    """One incident the agent should be able to root-cause.

    Two grading modes via `expect`:
      - "culprit" (default): the agent must finger `truth` (service + optional
        version). Graded by `grade_against_truth`.
      - "inconclusive": there is no real incident, so a good agent must NOT
        confidently blame anyone — correct iff confidence ≤ `max_confidence` and
        it named no service in `forbid_services`. This is the negative test that
        catches "confidently wrong on a non-incident" regressions, which a suite
        of only positive fixtures cannot.
    """

    id: str
    alert: dict[str, Any]
    truth: dict[str, Any] = Field(default_factory=dict)
    expect: Literal["culprit", "inconclusive"] = "culprit"
    # inconclusive-mode knobs (ignored for culprit fixtures):
    max_confidence: float = 0.6  # appropriately-hedged ceiling
    forbid_services: list[str] = Field(default_factory=list)

    def resolved_alert(self) -> dict[str, Any]:
        """Copy of the alert with `startsAt: now` resolved to the current UTC
        time — convenient for a live demo incident you just triggered."""
        alert = copy.deepcopy(self.alert)
        starts = alert.get("startsAt")
        if isinstance(starts, str) and starts.strip().lower() == "now":
            alert["startsAt"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return alert


def load_fixtures(path: Path) -> list[Fixture]:
    data = yaml.safe_load(path.read_text()) or []
    return [Fixture.model_validate(item) for item in data]


# ---- per-run result ---------------------------------------------------------


@dataclass
class RunResult:
    fixture_id: str
    seed: int
    correct: bool
    service_hit: bool
    version_hit: bool | None  # None when the fixture states no version
    confidence: float
    services: list[str]
    suspected_version: str | None
    summary: str
    error: str | None = None


def _dimension_hits(findings: Any, truth: dict[str, Any]) -> tuple[bool, bool | None]:
    """Per-dimension breakdown mirroring grade_against_truth, for the report."""
    want_svc = (truth.get("service") or "").lower()
    got_svc = {s.lower() for s in (getattr(findings, "services", []) or [])}
    in_summary = want_svc in (getattr(findings, "summary", "") or "").lower()
    service_hit = (not want_svc) or (want_svc in got_svc) or in_summary

    want_ver = truth.get("version")
    if not want_ver:
        return service_hit, None
    got_ver = getattr(findings, "suspected_version", None) or ""
    return service_hit, want_ver.lower() in got_ver.lower()


def grade_run(findings: Any, fixture: Fixture) -> tuple[bool, bool, bool | None]:
    """Grade one run → (correct, service_hit, version_hit). version_hit is None
    when the fixture doesn't grade a version (or is inconclusive-mode)."""
    if fixture.expect == "inconclusive":
        conf = float(getattr(findings, "confidence", 0.0) or 0.0)
        blamed = {s.lower() for s in (getattr(findings, "services", []) or [])}
        named_forbidden = any(s.lower() in blamed for s in fixture.forbid_services)
        correct = conf <= fixture.max_confidence and not named_forbidden
        # service_hit mirrors correctness (the "did it hedge" signal); no version.
        return correct, correct, None

    correct = grade_against_truth(findings, fixture.truth)
    service_hit, version_hit = _dimension_hits(findings, fixture.truth)
    return correct, service_hit, version_hit


async def run_one(
    fixture: Fixture, seed: int, *, run_nonce: str, store_path: Path
) -> RunResult:
    """Run the real agent once and grade it. Failures are captured, not raised,
    so one bad run never sinks the batch."""
    # Unique thread_id so MemorySaver never shares state across seeds or runs.
    thread_id = f"eval-{fixture.id}-s{seed}-{run_nonce}"
    try:
        result = await run_headless(fixture.resolved_alert(), thread_id=thread_id)
    except Exception as e:  # the harness must survive any agent error
        return RunResult(
            fixture_id=fixture.id,
            seed=seed,
            correct=False,
            service_hit=False,
            version_hit=None,
            confidence=0.0,
            services=[],
            suspected_version=None,
            summary="",
            error=f"{type(e).__name__}: {e}",
        )

    findings = result["findings"]
    correct, service_hit, version_hit = grade_run(findings, fixture)
    conf = float(getattr(findings, "confidence", 0.0) or 0.0)

    # Feed calibration: insert a pending record then label it. Direct store calls
    # (not calibration.record_run) so this works regardless of the runtime
    # calibration_enabled flag, and writes to the eval store by default.
    # run_id carries run_nonce so it is unique across harness invocations —
    # otherwise repeated (fixture, seed) rows collide and cal_label's
    # "most recent by run_id" UPDATE can attach a verdict to the wrong physical
    # row, corrupting the (confidence, correct) pairing calibration reads.
    run_id = f"eval-{fixture.id}-seed{seed}-{run_nonce}"
    try:
        store.cal_insert(
            run_id=run_id,
            ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            confidence=conf,
            summary=getattr(findings, "summary", "") or "",
            hypothesis=getattr(findings, "hypothesis", "") or "",
            suspected_version=getattr(findings, "suspected_version", None),
            services=list(getattr(findings, "services", []) or []),
            path=store_path,
        )
        store.cal_label(
            run_id,
            correct,
            score=1.0 if correct else 0.0,
            source="eval-harness",
            path=store_path,
        )
    except Exception:  # calibration feedback is best-effort
        pass

    return RunResult(
        fixture_id=fixture.id,
        seed=seed,
        correct=correct,
        service_hit=service_hit,
        version_hit=version_hit,
        confidence=conf,
        services=list(getattr(findings, "services", []) or []),
        suspected_version=getattr(findings, "suspected_version", None),
        summary=getattr(findings, "summary", "") or "",
    )


# ---- aggregation ------------------------------------------------------------


@dataclass
class FixtureSummary:
    fixture_id: str
    n: int
    correct_rate: float  # fraction of runs grade_against_truth == True
    any_correct: bool  # pass@k in the "at least one of k" sense
    service_rate: float
    version_rate: float | None  # None when fixture has no version truth
    mean_confidence: float
    errors: int
    runs: list[RunResult] = field(default_factory=list)


def summarize(fixture_id: str, runs: list[RunResult]) -> FixtureSummary:
    n = len(runs)
    correct = sum(1 for r in runs if r.correct)
    svc = sum(1 for r in runs if r.service_hit)
    ver_runs = [r for r in runs if r.version_hit is not None]
    ver_rate = (
        sum(1 for r in ver_runs if r.version_hit) / len(ver_runs) if ver_runs else None
    )
    return FixtureSummary(
        fixture_id=fixture_id,
        n=n,
        correct_rate=correct / n if n else 0.0,
        any_correct=correct > 0,
        service_rate=svc / n if n else 0.0,
        version_rate=ver_rate,
        mean_confidence=sum(r.confidence for r in runs) / n if n else 0.0,
        errors=sum(1 for r in runs if r.error),
        runs=runs,
    )


async def run_suite(
    fixtures: list[Fixture], *, seeds: int, store_path: Path
) -> list[FixtureSummary]:
    run_nonce = str(int(time.time()))
    summaries: list[FixtureSummary] = []
    for fixture in fixtures:
        runs: list[RunResult] = []
        for seed in range(seeds):
            runs.append(
                await run_one(fixture, seed, run_nonce=run_nonce, store_path=store_path)
            )
        summaries.append(summarize(fixture.id, runs))
    return summaries


# ---- baseline / regression --------------------------------------------------


def load_baseline(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    data: dict[str, float] = json.loads(path.read_text())
    return data


def save_baseline(path: Path, summaries: list[FixtureSummary]) -> None:
    path.write_text(
        json.dumps({s.fixture_id: round(s.correct_rate, 4) for s in summaries}, indent=2)
    )


def regression_diff(
    summaries: list[FixtureSummary], baseline: dict[str, float], *, tol: float = 1e-9
) -> list[tuple[str, float | None, float]]:
    """(fixture_id, baseline_rate_or_None, current_rate) for fixtures whose
    correct_rate moved beyond `tol` — negative delta is a regression."""
    out: list[tuple[str, float | None, float]] = []
    for s in summaries:
        base = baseline.get(s.fixture_id)
        if base is None or abs(s.correct_rate - base) > tol:
            out.append((s.fixture_id, base, s.correct_rate))
    return out


# ---- report -----------------------------------------------------------------


def format_report(
    summaries: list[FixtureSummary],
    diff: list[tuple[str, float | None, float]],
    *,
    store_path: Path,
) -> str:
    lines: list[str] = []
    n_total = sum(s.n for s in summaries)
    overall = (
        sum(s.correct_rate * s.n for s in summaries) / n_total if n_total else 0.0
    )
    lines.append(
        f"aiops-agent eval — {len(summaries)} fixture(s), {n_total} run(s), "
        f"overall correct {overall:.0%}"
    )
    lines.append("")
    header = f"  {'fixture':<28} {'correct':>9}  {'service':>8}  {'version':>8}  {'conf':>5}  err"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for s in summaries:
        ver = "  n/a" if s.version_rate is None else f"{s.version_rate:>7.0%}"
        lines.append(
            f"  {s.fixture_id:<28} "
            f"{s.correct_rate:>6.0%} ({sum(1 for r in s.runs if r.correct)}/{s.n}) "
            f"{s.service_rate:>7.0%}  {ver}  {s.mean_confidence:>5.2f}  {s.errors:>3}"
        )

    if diff:
        lines.append("")
        lines.append("  regression vs baseline:")
        for fid, base, cur in diff:
            if base is None:
                lines.append(f"    + {fid}: new fixture (correct {cur:.0%})")
            else:
                arrow = "▼" if cur < base else "▲"
                lines.append(f"    {arrow} {fid}: {base:.0%} → {cur:.0%}")
    else:
        lines.append("")
        lines.append("  no change vs baseline.")

    # surface the first error so a broken stack is obvious, not silent zeros.
    first_err = next(
        (r for s in summaries for r in s.runs if r.error), None
    )
    if first_err is not None:
        lines.append("")
        lines.append(f"  ! first error ({first_err.fixture_id}): {first_err.error}")

    lines.append("")
    lines.append(f"  calibration labels written to {store_path}")
    return "\n".join(lines)
