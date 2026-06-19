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
from ..tools.query import _get_json, _parse_dt, _rfc3339
from .contract import SLI, contract_for
from .topology import get_topology

logger = logging.getLogger("aiops_agent.signals.health")


class NeighborHealth(BaseModel):
    service: str
    relation: str          # "self" (under investigation) | "downstream" (dep) | "upstream" (caller)
    metric: str            # "error" | "throughput"
    value: float | None
    unit: str
    objective: str = ""    # the SLI's declared target, e.g. "declined_rate < 1%"
    verdict: str           # healthy | unhealthy | unknown | unavailable


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


async def _instant_scalar(expr: str) -> float | None:
    """Run an instant PromQL query and reduce its vector to one scalar (sum of
    series values). None on error or empty result."""
    data = await _get_json(
        settings.prometheus_url, "/api/v1/query",
        {"query": expr, "time": _rfc3339(_parse_dt("now"))},
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
        return None
    try:
        value = await _instant_scalar(sli.promql)
    except Exception as e:
        logger.warning("dependency health: query for %s failed: %s", svc, e)
        return NeighborHealth(service=svc, relation=relation, metric=sli.kind,
                              value=None, unit=sli.unit, objective=sli.objective,
                              verdict="unavailable")
    if value is None:
        verdict = "unavailable"
    elif sli.kind == "error":
        verdict = "unhealthy" if value > settings.signal_health_error_threshold else "healthy"
    else:
        # throughput: can't call it unhealthy from rate alone (0 may be no
        # traffic, not an outage) — report it as a liveness-only signal.
        verdict = "unknown"
    return NeighborHealth(service=svc, relation=relation, metric=sli.kind,
                          value=value, unit=sli.unit, objective=sli.objective, verdict=verdict)


def _fmt(h: NeighborHealth) -> str:
    if h.value is None:
        val = "n/a"
    elif h.unit == "ratio":
        val = f"{h.value:.1%}"
    else:
        val = f"{h.value:.2g} {h.unit}".strip()
    label = "this service" if h.relation == "self" else h.relation
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


async def evaluate_dependency_health(services: list[str]) -> str | None:
    """For the service(s) under investigation, evaluate each neighbour's health
    live and return a context block that tells the agent which way blame flows.
    None when there's nothing to evaluate (no topology/contract neighbours)."""
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
    bad_deps = [h.service for h in evaluated if h.relation == "downstream" and h.verdict == "unhealthy"]
    had_deps = any(h.relation == "downstream" for h in evaluated)

    if bad_self and bad_deps:
        verdict = (
            f"→ {', '.join(bad_self)} is breaching its own error SLO AND a downstream "
            f"dependency ({', '.join(bad_deps)}) is unhealthy — likely a cascading "
            "failure. Determine whether the breach is caused by the unhealthy dependency."
        )
    elif bad_self:
        deps_note = (
            "its downstream dependencies are healthy" if had_deps
            else "it has no downstream dependencies to inherit a fault from"
        )
        verdict = (
            f"→ {', '.join(bad_self)} is itself breaching its error SLO and {deps_note} — "
            "it is the LIKELY ROOT CAUSE, not a symptom. Do NOT dismiss this as normal; "
            "correlate with git_version (sum by git_version,reason) to find which deploy "
            "introduced it."
        )
    elif bad_deps:
        verdict = (
            "→ A downstream dependency is unhealthy ("
            + ", ".join(bad_deps)
            + "). The service(s) under investigation are likely showing a SYMPTOM, "
            "not the root cause — investigate the unhealthy dependency first."
        )
    elif had_deps:
        verdict = (
            "→ Neither the service(s) under investigation nor their downstream "
            "dependencies show an unhealthy SLI right now."
        )
    else:
        verdict = (
            "→ No unhealthy SLI right now; the service(s) under investigation are a "
            "leaf with no downstream dependencies."
        )

    primary_label = ", ".join(sorted(primaries))
    return (
        f"## Dependency health (live) — {primary_label}\n"
        "Each service's SLI, read just now, to attribute root cause to the right "
        "node:\n" + "\n".join(lines) + "\n" + verdict
    )
