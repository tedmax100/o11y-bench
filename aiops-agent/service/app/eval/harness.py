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
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from .. import store
from ..agent import run_headless
from ..calibration import grade_against_truth
from ..config import settings
from ..investigations import record_investigation
from .process import CheckResult, ProcessSpec, grade_process

_HERE = Path(__file__).parent
DEFAULT_FIXTURES = _HERE / "fixtures.yaml"
DEFAULT_BASELINE = _HERE / "baseline.json"
DEFAULT_STORE = _HERE / "eval.db"  # separate from prod aiops.db unless overridden
# The committed record the autonomy gate reads. eval.db is this harness's
# working store and is gitignored; the gate must not depend on a file that only
# exists on the machine that ran the build. See record.py.
DEFAULT_FIXTURE_RECORD = _HERE / "fixture_record.jsonl"


# ---- fixtures ---------------------------------------------------------------


class Fixture(BaseModel):
    """One incident the agent should be able to root-cause.

    Two grading modes via `expect`:
      - "culprit" (default): the agent must finger `truth` (service + optional
        version). Graded by `grade_against_truth`.
      - "inconclusive": there is no real incident, so a good agent must NOT
        confidently blame anyone — correct iff confidence ≤ `max_confidence`,
        it named no service in `forbid_services`, and it pinned nothing on a
        version in `forbid_versions`. This is the negative test that
        catches "confidently wrong on a non-incident" regressions, which a suite
        of only positive fixtures cannot.
    """

    id: str
    alert: dict[str, Any]
    truth: dict[str, Any] = Field(default_factory=dict)
    expect: Literal["culprit", "inconclusive"] = "culprit"
    max_confidence: float = 0.6  # inconclusive only: appropriately-hedged ceiling
    # Read in BOTH modes. Inconclusive fixtures use it to catch a confidently
    # blamed bystander; culprit fixtures to catch an answer that names the right
    # service and drags its victims along with it.
    forbid_services: list[str] = Field(default_factory=list)
    # …and never pins it on a version it was primed to suspect. The service
    # check cannot catch this on a fixture whose alert names the *right*
    # service: an agent that inherits a past case's culprit blames the correct
    # service for the wrong reason, and the service check waves it through.
    # Read in BOTH modes: inconclusive fixtures use it to catch an inherited
    # culprit, culprit fixtures to catch the config-vs-deploy confusion (right
    # service, wrong kind of cause).
    forbid_versions: list[str] = Field(default_factory=list)
    # Process expectations graded from the transcript (see process.py). A
    # fixture can assert on *how* the answer was reached even when the verdict
    # itself is right — a correct culprit reached by rephrasing a query into an
    # empty result is a regression waiting to happen.
    process: ProcessSpec = Field(default_factory=ProcessSpec)

    def resolved_alert(self, now_iso: str | None = None) -> dict[str, Any]:
        """Copy of the alert with a relative `startsAt` resolved to a UTC time.

        `now` and `now-<N>h` / `now-<N>m` are both accepted. `now_iso` pins them
        to a scenario clock (the provisioned stack's data-end); omitted, they
        fall back to wall-clock now (a live incident you triggered).

        The offset form exists because a stack can bake more than one incident,
        and two incidents that are both live at data-end are indistinguishable
        from one alert's point of view — every window contains both. An absolute
        timestamp cannot be written down here either: the data-end moves with
        `O11Y_SCENARIO_TIME_ISO`. So the fixture says how far back its incident
        sits, and the clock stays the stack's to decide.
        """
        alert = copy.deepcopy(self.alert)
        starts = alert.get("startsAt")
        if not isinstance(starts, str):
            return alert
        m = _RELATIVE_START.fullmatch(starts.strip().lower())
        if m is None:
            return alert
        base = (
            datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            if now_iso
            else datetime.now(UTC)
        )
        amount, unit = m.group(1), m.group(2)
        if amount:
            delta = timedelta(hours=int(amount)) if unit == "h" else timedelta(minutes=int(amount))
            base -= delta
        alert["startsAt"] = base.strftime("%Y-%m-%dT%H:%M:%SZ")
        return alert


# `now`, or `now-6h` / `now-90m`.
_RELATIVE_START = re.compile(r"now(?:\s*-\s*(\d+)\s*([hm]))?")


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
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def process_ok(self) -> bool:
        return all(c.passed for c in self.checks)


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


def blames_forbidden_version(findings: Any, forbid: list[str]) -> str:
    """The forbidden version this answer pins the cause on, or "" if none.

    Reads the prose as well as the field, and that is the whole point. Measured
    over 20 live passes on the ConfigMap incident: `suspected_version` came back
    None 20/20 — the provenance reconcile cleared it every time — while 20/20
    summaries still opened with "Code regression in payment-service v2.5.0".
    A judge that reads only the field scores that a clean sweep, and the person
    on call reads the sentence.

    So the failure did not go away when the field was fixed; it moved. Checking
    one surface is what let it move without the number noticing.
    """
    surfaces = [
        getattr(findings, "suspected_version", None) or "",
        getattr(findings, "summary", "") or "",
        getattr(findings, "hypothesis", "") or "",
    ]
    text = " ".join(surfaces).lower()
    for v in forbid:
        if v and v.lower() in text:
            return v
    return ""


def grade_run(findings: Any, fixture: Fixture) -> tuple[bool, bool, bool | None]:
    """Grade one run → (correct, service_hit, version_hit). version_hit is None
    when the fixture doesn't grade a version (or is inconclusive-mode)."""
    if fixture.expect == "inconclusive":
        conf = float(getattr(findings, "confidence", 0.0) or 0.0)
        blamed = {s.lower() for s in (getattr(findings, "services", []) or [])}
        named_forbidden = any(s.lower() in blamed for s in fixture.forbid_services)
        version_forbidden = bool(blames_forbidden_version(findings, fixture.forbid_versions))
        correct = conf <= fixture.max_confidence and not named_forbidden and not version_forbidden
        # service_hit mirrors correctness (the "did it hedge" signal); no version.
        return correct, correct, None

    correct = grade_against_truth(findings, fixture.truth)
    service_hit, version_hit = _dimension_hits(findings, fixture.truth)
    # `forbid_versions` used to be read only in inconclusive mode, which quietly
    # made it dead config on every culprit fixture that declared it. It matters
    # most exactly there: a config-type incident has a right service AND a
    # version that must not be blamed, and grading only the service waves
    # through the failure this whole path was built for — the alert carries a
    # `git_version` label, the fault is in a ConfigMap the pod template mounts,
    # and the answer pins it on the version anyway. See
    # `blames_forbidden_version` for why it reads the prose and not just the
    # field: fixing the field moved the failure, it did not remove it.
    if blames_forbidden_version(findings, fixture.forbid_versions):
        correct = False
    # The same hole `forbid_versions` had, in the other field, found by writing
    # the first fixture that needs it: a culprit-mode fixture could declare
    # `forbid_services` and have it silently ignored. It is not decoration on
    # this kind of incident — for a retry storm the whole question is whether
    # the answer blames the services whose error counts moved, and naming
    # api-gateway *while also* blaming its victims is not a right answer with a
    # cosmetic flaw. Only `services` is read, not the prose: unlike a version
    # string, a service name legitimately appears in a correct explanation of
    # what broke downstream, so substring-matching prose would fail the best
    # answers. See [[aiops-provenance-is-a-step]] for why the blunt version of
    # that check was kept over a cleverer one.
    blamed = {s.lower() for s in (getattr(findings, "services", []) or [])}
    if any(s.lower() in blamed for s in fixture.forbid_services):
        correct = False
    return correct, service_hit, version_hit


async def telemetry_preflight(alert: dict[str, Any]) -> str:
    """ "" if the stores have something to reason about in this alert's window,
    else why they do not.

    Grading a run against empty stores measures the environment, not the agent,
    and it does not fail in a way anybody notices: the run dutifully queries,
    gets nothing, and the process checks book it as "came back empty, retried
    without discovering" — a model defect, in a window where discovery would
    also have found nothing. That is the same shape as Tempo's retention, which
    this file already warns about in prose while still running the fixture.

    Measured 2026-08-29: an unattended 20-pass run landed in a 45-minute gap
    with no ingestion at all, and 14 of its 17 process failures came from it.

    Prometheus is the probe, on a bare label matcher rather than a metric name,
    so this stays true of any environment: `count({service_name="x"})` is "did
    anything at all write series for this service in that window". Fail-open —
    an unreachable store is not evidence that the window was empty, and refusing
    to run on it would trade a false model failure for a false environment one.
    """
    from ..config import settings as _settings
    from ..tools.query import _epoch_s, _get_json

    labels = alert.get("labels") or {}
    service = labels.get("service_name") or labels.get("service")
    starts = _parse_starts_at_safe(alert.get("startsAt"))
    if not service or starts is None:
        return ""
    try:
        series = await _get_json(
            _settings.prometheus_url,
            "/api/v1/query_range",
            {
                "query": f'count({{service_name="{service}"}})',
                "start": _epoch_s(starts - timedelta(minutes=30)),
                "end": _epoch_s(starts + timedelta(minutes=5)),
                "step": "300",
            },
        )
    except Exception:
        return ""
    result = ((series or {}).get("data") or {}).get("result") or []
    if any(float(v[1]) > 0 for r in result for v in (r.get("values") or [])):
        return ""
    return (
        f"no telemetry for {service} in the 30 minutes before "
        f"{starts:%Y-%m-%dT%H:%M:%SZ} — the stores were idle, so this run would "
        "grade the environment, not the agent"
    )


def _parse_starts_at_safe(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


async def run_one(
    fixture: Fixture,
    seed: int,
    *,
    run_nonce: str,
    store_path: Path,
    scenario_time: str | None = None,
) -> RunResult:
    """Run the real agent once and grade it. Failures are captured, not raised,
    so one bad run never sinks the batch."""
    # Unique thread_id so MemorySaver never shares state across seeds or runs.
    thread_id = f"eval-{fixture.id}-s{seed}-{run_nonce}"
    alert = fixture.resolved_alert(scenario_time)
    idle = await telemetry_preflight(alert)
    if idle:
        # Counted as an error, not as a wrong answer. `err` in the report is the
        # column that says "do not read the score above this line".
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
            error=idle,
        )
    try:
        result = await run_headless(alert, thread_id=thread_id)
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
    checks = grade_process(
        fixture.process, result.get("messages") or [], result.get("answer") or "", conf
    )
    # A run is only correct if it got there honestly: the verdict AND the
    # process. Keeping them one flag is deliberate — a suite that reports them
    # separately invites reading the number you like.
    correct = correct and all(c.passed for c in checks)

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
            # What `correct` will mean for this row: "culprit" fixtures grade the
            # blame, "inconclusive" ones grade the hedge. The governance gate
            # only computes its curve over the former.
            grading_mode=fixture.expect,
            path=store_path,
        )
        store.cal_label(
            run_id,
            correct,
            score=1.0 if correct else 0.0,
            source="eval-harness",
            grading_mode=fixture.expect,
            path=store_path,
        )
        # Also record the investigation, under the *same* id. The past-incident
        # library is a JOIN of these two tables on run_id=fp, so a harness that
        # writes only calibration can never make it non-empty however many runs
        # it grades. `run_id` (not thread_id) is deliberate: they differ by two
        # characters ("seed0" vs "s0"), and a JOIN across that gap fails silently.
        record_investigation(
            run_id,
            fixture.resolved_alert(scenario_time),
            {"answer": result.get("answer") or "", "findings": findings, "decisions": []},
            path=store_path,
            source="eval",
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
        checks=checks,
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
    # One entry per pass over the suite. The sampling unit that matters: seeds
    # inside a pass agreed on the verdict 26 of 27 recorded times, because the
    # seed never reaches the model call and the model runs at temperature 0.
    # What moved a fixture from 3/3 to 0/3 was one pass versus the next.
    pass_rates: list[float] = field(default_factory=list)

    @property
    def spread(self) -> float | None:
        """Widest gap between passes, or None with fewer than two."""
        if len(self.pass_rates) < 2:
            return None
        return max(self.pass_rates) - min(self.pass_rates)


def summarize(fixture_id: str, runs: list[RunResult]) -> FixtureSummary:
    n = len(runs)
    correct = sum(1 for r in runs if r.correct)
    svc = sum(1 for r in runs if r.service_hit)
    ver_runs = [r for r in runs if r.version_hit is not None]
    ver_rate = sum(1 for r in ver_runs if r.version_hit) / len(ver_runs) if ver_runs else None
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


def merge_passes(passes: list[list[FixtureSummary]]) -> list[FixtureSummary]:
    """Fold repeated passes over the same suite into one summary per fixture.

    The per-pass rates are kept rather than averaged away. A mean of 0.5 built
    from 1.0 and 0.0 is a different situation from one built from 0.5 and 0.5,
    and only the first tells you the number you are about to compare against is
    not stable enough to compare.
    """
    if not passes:
        return []
    by_fixture: dict[str, list[FixtureSummary]] = {}
    for one_pass in passes:
        for summary in one_pass:
            by_fixture.setdefault(summary.fixture_id, []).append(summary)

    merged: list[FixtureSummary] = []
    for fixture_id, parts in by_fixture.items():
        runs = [r for part in parts for r in part.runs]
        combined = summarize(fixture_id, runs)
        combined.pass_rates = [part.correct_rate for part in parts]
        merged.append(combined)
    # Keep the fixture order of the first pass.
    order = {s.fixture_id: i for i, s in enumerate(passes[0])}
    merged.sort(key=lambda s: order.get(s.fixture_id, len(order)))
    return merged


async def run_suite(
    fixtures: list[Fixture],
    *,
    seeds: int,
    store_path: Path,
    scenario_time: str | None = None,
    repeats: int = 1,
) -> list[FixtureSummary]:
    """Run every fixture, `repeats` times over the whole suite.

    A pass is the sampling unit. Seeds inside a pass are the same request issued
    again — the seed sets a thread id and a record id and never reaches the model
    call, and the model is built at temperature 0 — so they come back correlated
    and cost the same as real samples. Repeats re-ask the question.

    Under `--stack` the container stays up across passes, so every pass queries
    identical data and the spread that comes out is the model's, not the
    generator's. Measured 2026-08-19 over five fixtures: that spread is 0% on
    all of them, which is what temperature 0 is supposed to mean and rules the
    model out as the source of the swings this was built to chase. Those
    happened between *invocations*, where a fresh container moves every absolute
    timestamp in the prompt. Varying the scenario time on purpose is the
    experiment this does not yet run.
    """
    passes: list[list[FixtureSummary]] = []
    for _ in range(max(1, repeats)):
        run_nonce = str(int(time.time()))
        summaries: list[FixtureSummary] = []
        for fixture in fixtures:
            runs: list[RunResult] = []
            for seed in range(seeds):
                runs.append(
                    await run_one(
                        fixture,
                        seed,
                        run_nonce=run_nonce,
                        store_path=store_path,
                        scenario_time=scenario_time,
                    )
                )
            summaries.append(summarize(fixture.id, runs))
        passes.append(summaries)
    return merge_passes(passes)


# ---- recall A/B -------------------------------------------------------------
# Day38 swapped what past-incident recall reads. Measuring whether that helped
# needs two arms of the same suite — and, more importantly, needs to know
# whether the library has already seen the fixture, because a library that
# contains the answer turns the "with recall" arm into an open-book exam and its
# score into a measurement of retrieval, not reasoning.


@contextmanager
def recall_arm(enabled: bool) -> Iterator[None]:
    """Run this block with case recall forced on or off."""
    previous = settings.case_recall_enabled
    settings.case_recall_enabled = enabled
    try:
        yield
    finally:
        settings.case_recall_enabled = previous


def library_overlap(fixtures: list[Fixture]) -> list[tuple[str, int]]:
    """Which fixtures the case library can already answer, and with how many
    cases. Empty list = a clean A/B.

    Reads the *production* store, not the eval store: `_past_incident_context`
    takes no path, so recall during an eval run resolves through
    `settings.store_path` like every other runtime read. Checking the eval store
    here would report a clean experiment while the agent reads a dirty one.
    """
    out: list[tuple[str, int]] = []
    for fx in fixtures:
        labels = fx.alert.get("labels") or {}
        service = labels.get("service_name") or labels.get("service")
        if not service:
            continue
        alertname = labels.get("alertname")
        try:
            hits = store.case_query_similar(service=service, alertname=alertname, limit=5)
            # Dead ends count as overlap too. A human disproof ("not the payment
            # version") narrows the search as effectively as a root cause does,
            # so a fixture carrying one is no longer unseen — counting only
            # confirmed causes would report a clean A/B on a primed run.
            keys = {c["case_key"] for c in hits}
            if alertname:
                keys.add(store.case_key(alertname, service))
            hits_n = len(hits) + len(store.case_ruled_out_for(sorted(keys), limit=8))
        except Exception:
            continue
        if hits_n:
            out.append((fx.id, hits_n))
    return out


def format_ab_report(
    on: list[FixtureSummary],
    off: list[FixtureSummary],
    overlap: list[tuple[str, int]],
) -> str:
    """Side by side, with the open-book warning first because it decides whether
    the numbers below it mean anything."""
    lines: list[str] = []
    if overlap:
        lines.append("OPEN BOOK — the case library already answers these fixtures:")
        for fixture_id, n in overlap:
            lines.append(f"  {fixture_id}: {n} case(s) recalled")
        lines.append("  The recall arm is retrieving an answer it was told. Whatever the delta")
        lines.append("  below is, it is not evidence that recall helps an unseen incident.")
        lines.append("")
    else:
        lines.append("clean A/B: the case library answers none of these fixtures\n")

    by_id = {s.fixture_id: s for s in off}
    lines.append(f"{'fixture':<34} {'recall off':>11} {'recall on':>11} {'delta':>8}")
    for s in on:
        base = by_id.get(s.fixture_id)
        if base is None:
            continue
        delta = s.correct_rate - base.correct_rate
        lines.append(
            f"{s.fixture_id:<34} {base.correct_rate:>10.0%} {s.correct_rate:>11.0%} {delta:>+8.0%}"
        )
    spreads = [s.spread for s in [*on, *off] if s.spread is not None]
    lines.append("")
    lines.append(
        "A delta here is a difference between two small samples of a non-deterministic model."
    )
    if spreads:
        lines.append(
            f"The same arms varied by up to {max(spreads):.0%} between passes of identical "
            "code; a delta under that is not a result."
        )
    else:
        lines.append(
            "Both arms ran a single pass, so nothing here measures how much they move on "
            "their own. Add --repeat before reading the delta."
        )
    lines.append(
        "Day27 measured the same code scoring 2.5-3.5 across three runs; read the transcripts"
    )
    lines.append(
        "before reading the delta."
        + (f"  (passes/arm: {len(on[0].pass_rates) or 1})" if on else "")
    )
    return "\n".join(lines)


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
    overall = sum(s.correct_rate * s.n for s in summaries) / n_total if n_total else 0.0
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

    spreads = [(s.fixture_id, s.pass_rates, s.spread) for s in summaries if s.spread is not None]
    if spreads:
        lines.append("")
        lines.append("  between passes (the sampling unit — seeds inside a pass are correlated):")
        worst = max(sp for _, _, sp in spreads)
        for fid, rates, sp in spreads:
            shown = " ".join(f"{r:.0%}" for r in rates)
            lines.append(f"    {fid:<38} {shown:<24} spread {sp:>4.0%}")
        lines.append(
            f"    Widest spread this run: {worst:.0%}. A difference smaller than that is "
            "not a result."
        )

    failed = [
        (s.fixture_id, r.seed, c)
        for s in summaries
        for r in s.runs
        for c in r.checks
        if not c.passed
    ]
    if failed:
        lines.append("")
        lines.append("  failed process checks (the answer may still read fine):")
        for fid, seed, c in failed:
            lines.append(f"    x {fid} seed{seed} — {c.name}: {c.detail}")

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
    first_err = next((r for s in summaries for r in s.runs if r.error), None)
    if first_err is not None:
        lines.append("")
        lines.append(f"  ! first error ({first_err.fixture_id}): {first_err.error}")

    lines.append("")
    lines.append(f"  calibration labels written to {store_path}")
    return "\n".join(lines)
