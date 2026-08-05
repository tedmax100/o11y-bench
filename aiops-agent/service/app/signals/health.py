"""Live dependency-health evaluation — blame propagation (signal-plane-design s4).

The single biggest source of confident-wrong RCA is blaming the service that
*shows* the symptom instead of the downstream dependency that *causes* it (order
errors because payment is failing). s1-s3 gave us the graph + the authoritative
SLI per service; s4 spends that: for the service under investigation, run each
neighbour's error SLI live and tell the agent which way the blame flows.

The rule: if a **downstream dependency** is unhealthy, the service under
investigation is probably a *symptom* — investigate the dependency first. If all
dependencies are healthy, the fault is likely local.

This is the one Signal Plane piece that does live I/O, so (unlike the sync
context block) it runs *before* the agent loop like the runbook diagnostics —
read-only, off the agent's tool budget, and best-effort (any query failure
degrades to "unavailable", never breaks the run). Queries honour the pinned
incident clock when run inside a `now_override` block.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel

from ..config import settings
from ..tools.query import _get_json, _parse_dt, _rfc3339, current_now
from .contract import SLI, contract_for
from .topology import get_topology

logger = logging.getLogger("aiops_agent.signals.health")


class NeighborHealth(BaseModel):
    service: str
    relation: str  # "self" (under investigation) | "downstream" (dep) | "upstream" (caller)
    metric: str  # "error" | "throughput"
    value: float | None
    unit: str
    objective: str = ""  # the SLI's declared target, e.g. "declined_rate < 1%"
    verdict: str  # healthy | unhealthy | unknown | unavailable | unjudgeable


class ImpactEdge(BaseModel):
    """s4.2: how much a caller's own failures attributed to an unhealthy callee
    have RISEN vs a baseline window — the difference between 'topologically
    adjacent' and 'materially impacted'."""

    primary: str
    dependency: str
    current: float | None
    baseline: float | None
    delta: float | None
    verdict: str  # rising | flat | unavailable


def _health_sli(svc: str) -> SLI | None:
    """The SLI to judge a neighbour by: its error ratio if it has one, else
    throughput as a liveness proxy."""
    c = contract_for(svc)
    if c is None:
        return None
    err = next((s for s in c.slis if s.kind == "error"), None)
    if err is not None:
        return err
    return next((s for s in c.slis if s.kind == "throughput"), None)


async def _instant_scalar(expr: str, at: str = "now") -> float | None:
    """Run an instant PromQL query at time `at` (a now-shorthand like "now-1h",
    honouring any pinned incident clock) and reduce its vector to one scalar
    (sum of series values). None on error or empty result."""
    data = await _get_json(
        settings.prometheus_url,
        "/api/v1/query",
        {"query": expr, "time": _rfc3339(_parse_dt(at))},
    )
    if not isinstance(data, dict) or data.get("status") == "error":
        return None
    result = (data.get("data") or {}).get("result") or []
    total = 0.0
    seen = False
    for series in result:
        val = (series.get("value") or [None, None])[1]
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN (e.g. histogram_quantile with no samples)
            continue
        total += f
        seen = True
    return total if seen else None


async def _evaluate(svc: str, relation: str) -> NeighborHealth | None:
    sli = _health_sli(svc)
    if sli is None:
        # No SLI declared for this service — that is a gap in the contract, not
        # a clean bill of health. Say so on its own line instead of dropping the
        # service, so no downstream sentence can read the silence as "healthy".
        return NeighborHealth(
            service=svc,
            relation=relation,
            metric="none",
            value=None,
            unit="",
            verdict="unjudgeable",
        )
    try:
        value = await _instant_scalar(sli.promql)
    except Exception as e:
        logger.warning("dependency health: query for %s failed: %s", svc, e)
        return NeighborHealth(
            service=svc,
            relation=relation,
            metric=sli.kind,
            value=None,
            unit=sli.unit,
            objective=sli.objective,
            verdict="unavailable",
        )
    if value is None:
        verdict = "unavailable"
    elif sli.kind == "error":
        verdict = "unhealthy" if value > settings.signal_health_error_threshold else "healthy"
    else:
        # throughput: can't call it unhealthy from rate alone (0 may be no
        # traffic, not an outage) — report it as a liveness-only signal.
        verdict = "unknown"
    return NeighborHealth(
        service=svc,
        relation=relation,
        metric=sli.kind,
        value=value,
        unit=sli.unit,
        objective=sli.objective,
        verdict=verdict,
    )


def _fmt(h: NeighborHealth) -> str:
    label = "this service" if h.relation == "self" else h.relation
    if h.verdict == "unjudgeable":
        return (
            f"- {label} {h.service}: no error SLI declared — CANNOT be judged from metrics "
            "(a missing declaration, not a healthy verdict; judge it from its logs)"
        )
    if h.value is None:
        val = "n/a"
    elif h.unit == "ratio":
        val = f"{h.value:.1%}"
    else:
        val = f"{h.value:.2g} {h.unit}".strip()
    if h.verdict == "unhealthy":
        obj = f" (breaches objective {h.objective})" if h.objective else ""
        tail = f" — UNHEALTHY{obj}"
    else:
        tail = {
            "healthy": " — healthy",
            "unknown": " (liveness only; no error SLI)",
            "unavailable": " — unavailable",
        }.get(h.verdict, "")
    return f"- {label} {h.service}: {h.metric} {val}{tail}"


async def _evaluate_impact(primary: str, dependency: str, attribution: str) -> ImpactEdge:
    """s4.2: the caller's failures attributed to `dependency`, now vs a baseline
    `offset` ago. A delta over the floor = materially impacted (real symptom);
    ~0 = unhealthy dependency but the caller isn't actually feeling it."""
    try:
        cur = await _instant_scalar(attribution)
        base = await _instant_scalar(
            attribution, at=f"now-{settings.signal_health_baseline_offset}"
        )
    except Exception as e:
        logger.warning("impact query for %s→%s failed: %s", primary, dependency, e)
        cur = None
        base = None
    if cur is None:
        return ImpactEdge(
            primary=primary,
            dependency=dependency,
            current=None,
            baseline=base,
            delta=None,
            verdict="unavailable",
        )
    base = base or 0.0
    delta = cur - base
    verdict = "rising" if delta > settings.signal_health_impact_min_delta else "flat"
    return ImpactEdge(
        primary=primary,
        dependency=dependency,
        current=cur,
        baseline=base,
        delta=delta,
        verdict=verdict,
    )


def _fmt_impact(im: ImpactEdge) -> str:
    if im.verdict == "unavailable":
        return (
            f"- impact of {im.dependency} on {im.primary}: unavailable "
            "(no attribution metric reading)"
        )
    tail = (
        " — RISING (materially impacted)"
        if im.verdict == "rising"
        else " — flat (no material rise; baseline-level)"
    )
    return (
        f"- impact of {im.dependency} on {im.primary}: failures attributed to it "
        f"{im.current:.3g}/s (baseline {im.baseline:.3g}/s, Δ{im.delta:+.3g}/s){tail}"
    )


async def evaluate_dependency_health(services: list[str]) -> str | None:
    """For the service(s) under investigation, evaluate each neighbour's health
    live and return a context block that tells the agent which way blame flows.
    None only when the service isn't in the topology at all — a service the walk
    reaches but cannot judge still gets a block saying exactly that, because
    silence here reads as "healthy" to whoever consumes it next."""
    if not (settings.signal_plane_enabled and settings.signal_dependency_health_enabled):
        return None
    topo = get_topology()
    primaries = {s for s in services if topo.node(s)}
    if not primaries:
        return None

    # Collect each primary's neighbours (excluding the primaries themselves),
    # tagged by relation, deduped, capped to bound cost.
    downstream: set[str] = set()
    upstream: set[str] = set()
    for svc in primaries:
        downstream |= set(topo.downstream(svc))
        upstream |= set(topo.upstream(svc))
    downstream -= primaries
    upstream -= primaries - downstream  # a node can be both; prefer downstream

    neighbour_targets: list[tuple[str, str]] = (
        [(s, "downstream") for s in sorted(downstream)]
        + [(s, "upstream") for s in sorted(upstream - downstream)]
    )[: settings.signal_health_max_neighbors]

    # Evaluate the service(s) under investigation themselves AND their
    # neighbours. The self-verdict is what stops the agent dismissing the
    # service it's asked about as healthy when its own error SLI is breaching.
    self_targets = [(s, "self") for s in sorted(primaries)]
    results = await asyncio.gather(
        *(_evaluate(s, rel) for s, rel in self_targets + neighbour_targets)
    )
    evaluated = [h for h in results if h is not None]
    if not evaluated:
        return None

    # self first, then downstream, then upstream — order the agent should read.
    order = {"self": 0, "downstream": 1, "upstream": 2}
    evaluated.sort(key=lambda h: (order.get(h.relation, 9), h.service))
    lines = [_fmt(h) for h in evaluated]

    bad_self = [h.service for h in evaluated if h.relation == "self" and h.verdict == "unhealthy"]
    bad_deps = [
        h.service for h in evaluated if h.relation == "downstream" and h.verdict == "unhealthy"
    ]
    had_deps = any(h.relation == "downstream" for h in evaluated)

    # Services the walk reached but could not judge (no error SLI declared, or the
    # query came back empty). Every verdict below has to qualify itself with these
    # — otherwise "no unhealthy SLI" gets read as "everything here is fine", which
    # is a claim about services we never actually measured.
    _blind = ("unjudgeable", "unavailable", "unknown")
    blind_self = [h.service for h in evaluated if h.relation == "self" and h.verdict in _blind]
    blind_deps = [
        h.service for h in evaluated if h.relation == "downstream" and h.verdict in _blind
    ]
    blind_self_note = (
        f" NOTE: {', '.join(blind_self)} has no error SLI of its own, so this verdict says "
        "nothing about it — judge it from its logs before ruling it out."
        if blind_self
        else ""
    )
    blind_deps_note = (
        f" NOTE: {len(blind_deps)} downstream dependency/dependencies ({', '.join(blind_deps)}) "
        "could NOT be judged (no error SLI), so a fault inherited from them is not ruled out."
        if blind_deps
        else ""
    )

    # s4.2: for each unhealthy downstream a primary declares an attribution edge
    # to, measure whether the primary's failures attributed to it actually ROSE
    # (vs baseline) — the difference between adjacent and materially impacted.
    impacts: list[ImpactEdge] = []
    for p in sorted(primaries):
        for dep in bad_deps:
            attr = topo.attribution_for(p, dep)
            if attr:
                impacts.append(await _evaluate_impact(p, dep, attr))
    lines += [_fmt_impact(im) for im in impacts]
    rising = [im for im in impacts if im.verdict == "rising"]
    flat = [im for im in impacts if im.verdict == "flat"]

    if bad_self and bad_deps:
        rise_note = (
            f" Its failures attributed to {', '.join(sorted({im.dependency for im in rising}))} "
            "ROSE vs baseline — the breach is inherited from that dependency."
            if rising
            else ""
        )
        verdict = (
            f"→ {', '.join(bad_self)} is breaching its own error SLO AND a downstream "
            f"dependency ({', '.join(bad_deps)}) is unhealthy — likely a cascading "
            "failure."
            + (rise_note or " Determine whether the breach is caused by the unhealthy dependency.")
        )
    elif bad_self:
        if not had_deps:
            deps_note = "it has no downstream dependencies to inherit a fault from"
        elif blind_deps:
            deps_note = "none of its judgeable downstream dependencies is unhealthy"
        else:
            deps_note = "its downstream dependencies are healthy"
        verdict = (
            f"→ {', '.join(bad_self)} is itself breaching its error SLO and {deps_note} — "
            "it is the LIKELY ROOT CAUSE, not a symptom. Do NOT dismiss this as normal; "
            "correlate with git_version (sum by git_version,reason) to find which deploy "
            "introduced it."
        )
    elif bad_deps and rising:
        # s4.2: attribution metric confirms the primary IS feeling the dependency.
        svcs = ", ".join(sorted({im.primary for im in rising}))
        deps = ", ".join(sorted({im.dependency for im in rising}))
        verdict = (
            f"→ Confirmed: {svcs} IS materially impacted by {deps} — its own failures "
            "attributed to that dependency ROSE vs baseline (see impact line). It is a "
            f"genuine SYMPTOM; fix {deps} to restore it."
        )
    elif bad_deps and flat:
        # s4.2: dependency unhealthy but the primary's attributed-failure rate did
        # NOT rise → topologically adjacent, not materially impacted. Directly
        # resolves the Q2 over-claim instead of just asking the agent to confirm.
        deps = ", ".join(sorted({im.dependency for im in flat}))
        svcs = ", ".join(sorted({im.primary for im in flat}))
        verdict = (
            f"→ {deps} is unhealthy, but {svcs}'s own failures attributed to it did NOT "
            "rise vs baseline (Δ≈0, see impact line) — it is NOT materially impacted by "
            f"this incident, only topologically adjacent. Fix {deps} as its own problem; "
            f"do not report {svcs} as a symptom of it."
        )
    elif bad_deps:
        # Downstream unhealthy, no attribution metric declared to measure impact.
        # Fall back to s4.1: self is healthy, so tell the agent to confirm before
        # claiming symptom rather than assert it from topology.
        self_state = (
            "the service(s) under investigation could NOT be judged from metrics"
            if blind_self
            else "the service(s) under investigation show HEALTHY SLIs themselves"
        )
        verdict = (
            "→ A downstream dependency is unhealthy ("
            + ", ".join(bad_deps)
            + f"), but {self_state}. "
            "An unhealthy downstream does NOT by itself mean they are impacted — before "
            "calling them a symptom, CONFIRM they actually see failures attributed to that "
            "dependency (e.g. their own upstream-error / cancelled-by-dependency count, "
            "not just their overall error SLI). Fix the unhealthy dependency regardless; "
            "it is the primary problem to investigate." + blind_self_note
        )
    elif had_deps:
        verdict = "→ No unhealthy SLI among the services this walk could judge." + blind_self_note
    else:
        verdict = (
            "→ No unhealthy SLI right now; the service(s) under investigation are a "
            "leaf with no downstream dependencies." + blind_self_note
        )

    verdict += blind_deps_note

    primary_label = ", ".join(sorted(primaries))
    # State the clock these readings were taken against. Inside an alert
    # investigation `current_now()` is pinned to the alert's startsAt, so "just
    # now" would be wrong by however old the alert is — and a reader (human or
    # model) has no other way to tell which window these numbers came from.
    read_at = current_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"## Dependency health (live) — {primary_label}\n"
        f"Each service's SLI, read at {read_at} (the incident clock for this "
        "investigation, not necessarily wall-clock now), to attribute root cause "
        "to the right node:\n" + "\n".join(lines) + "\n" + verdict
    )
