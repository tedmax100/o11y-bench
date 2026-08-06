"""Reconcile the declared topology against the live Tempo call graph.

s1 ships a *declared* graph (topology.yaml). ARE (ch2.4 / ch4.3) demands the
topology be a living artefact continuously aligned to telemetry, not a wiki page
that drifts. s2 closes that gap: sample recent traces, derive the *observed*
caller→callee edges from the parent→child service boundary, and diff them
against what we declared.

How an edge is observed: with httpx/FastAPI auto-instrumentation, service A
calling service B produces A's CLIENT span as the parent of B's SERVER span, so
`service.name` changes exactly at the call boundary. A span whose service differs
from its parent's service therefore *is* an observed edge (parent_svc → svc).

This is read-only and runs off the per-RCA hot path (a CLI / periodic job). It
caches the latest drift snapshot in memory; `context.py` reads that cache
synchronously to annotate the injected Signal context with ⚠ drift markers and a
DQ score — the first Data-Quality SLO data point (ARE ch3.6), without adding a
network round-trip to the reasoning path.
"""

from __future__ import annotations

import asyncio
import logging
import time

from pydantic import BaseModel, Field

from ..config import settings
from ..tools.query import _epoch_s, _get_json, _parse_dt
from .topology import Edge, Topology, get_topology

logger = logging.getLogger("aiops_agent.signals.reconcile")


class TopologyDrift(BaseModel):
    """Diff between the declared graph and what the live traces show."""

    topology_version: str = "0"
    traces_sampled: int = 0
    declared_count: int = 0
    observed_count: int = 0
    undeclared_edges: list[Edge] = Field(default_factory=list)  # observed, not declared (drift)
    unobserved_edges: list[Edge] = Field(default_factory=list)  # declared, never seen (stale)
    # DQ score: of the edges actually flowing in telemetry, the share our
    # declared graph accounts for. 1.0 = the declared graph captures everything
    # observed; <1.0 = real edges we never declared (the dangerous direction).
    # None when no traffic was observed (can't judge).
    dq_score: float | None = None
    # How many sampled traces each service appeared in. An edge A→B that we
    # never observed only means something if A was *active* in the sample: a
    # caller nobody exercised is silence, not evidence of a dead edge.
    caller_samples: dict[str, int] = Field(default_factory=dict)
    computed_ts: float = 0.0

    def evidence_for(self, caller: str) -> int:
        return self.caller_samples.get(caller, 0)


def _otlp_service(resource: dict) -> str | None:
    """Pull service.name out of an OTLP-JSON resource attribute list."""
    for a in resource.get("attributes", []):
        if a.get("key") == "service.name":
            return (a.get("value") or {}).get("stringValue")
    return None


def edges_from_trace(raw: dict) -> set[tuple[str, str]]:
    """Observed caller→callee edges in one trace's OTLP-JSON: a span whose
    service differs from its parent's service marks a call boundary."""
    svc_of: dict[str, str | None] = {}
    parent_of: dict[str, str | None] = {}
    for batch in raw.get("batches", []):
        service = _otlp_service(batch.get("resource", {}))
        for ss in batch.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                sid = sp.get("spanId")
                if not sid:
                    continue
                svc_of[sid] = service
                parent_of[sid] = sp.get("parentSpanId") or None
    edges: set[tuple[str, str]] = set()
    for sid, svc in svc_of.items():
        psvc = svc_of.get(parent_of.get(sid))
        if psvc and svc and psvc != svc:
            edges.add((psvc, svc))
    return edges


def services_from_trace(raw: dict) -> set[str]:
    """Every service that appears anywhere in one trace. Used to tell 'this
    caller ran and never made the call' apart from 'this caller never ran'."""
    seen: set[str] = set()
    for batch in raw.get("batches", []):
        service = _otlp_service(batch.get("resource", {}))
        if service and any(ss.get("spans") for ss in batch.get("scopeSpans", [])):
            seen.add(service)
    return seen


def diff_edges(
    topo: Topology,
    observed: set[tuple[str, str]],
    traces_sampled: int,
    caller_samples: dict[str, int] | None = None,
) -> TopologyDrift:
    """Pure diff of declared vs observed edges → drift + DQ score."""
    declared = {(e.caller, e.callee) for e in topo.edges}
    undeclared = observed - declared
    unobserved = declared - observed
    dq = len(declared & observed) / len(observed) if observed else None
    return TopologyDrift(
        topology_version=topo.version,
        traces_sampled=traces_sampled,
        declared_count=len(declared),
        observed_count=len(observed),
        undeclared_edges=[Edge(caller=c, callee=k) for c, k in sorted(undeclared)],
        unobserved_edges=[Edge(caller=c, callee=k) for c, k in sorted(unobserved)],
        dq_score=round(dq, 3) if dq is not None else None,
        caller_samples=dict(caller_samples or {}),
        computed_ts=time.time(),
    )


# ---- latest-drift cache (read synchronously by context.py) -----------------

_last_drift: TopologyDrift | None = None


def get_last_drift() -> TopologyDrift | None:
    """The most recent reconcile result, or None if reconcile hasn't run."""
    return _last_drift


def set_last_drift(drift: TopologyDrift) -> None:
    global _last_drift
    _last_drift = drift


# ---- live observation (network; off the RCA hot path) ----------------------


async def _search_trace_ids(lookback: str, limit: int) -> list[str]:
    s, e = _parse_dt(lookback), _parse_dt("now")
    data = await _get_json(
        settings.tempo_url,
        "/api/search",
        # Skip sub-ms health probes (see tempo-probe-noise-filter); they carry no
        # cross-service edges and would burn the search limit.
        {"q": "{ trace:duration > 5ms }", "start": _epoch_s(s), "end": _epoch_s(e), "limit": limit},
    )
    traces = data.get("traces", []) if isinstance(data, dict) else []
    return [t.get("traceID") for t in traces if t.get("traceID")]


async def observe_edges(
    lookback: str = "now-1h", max_traces: int = 50
) -> tuple[set[tuple[str, str]], int, dict[str, int]]:
    """Sample recent traces; union their observed edges and count how many of
    those traces each service took part in."""
    trace_ids = await _search_trace_ids(lookback, max_traces)
    observed: set[tuple[str, str]] = set()
    caller_samples: dict[str, int] = {}
    sampled = 0
    for tid in trace_ids:
        try:
            raw = await _get_json(settings.tempo_url, f"/api/traces/{tid}", {})
        except Exception as e:  # one bad trace shouldn't sink the reconcile
            logger.warning("reconcile: fetch trace %s failed: %s", tid, e)
            continue
        observed |= edges_from_trace(raw)
        for svc in services_from_trace(raw):
            caller_samples[svc] = caller_samples.get(svc, 0) + 1
        sampled += 1
    return observed, sampled, caller_samples


async def reconcile(lookback: str = "now-1h", max_traces: int = 50) -> TopologyDrift:
    """Observe live edges, diff against the declared topology, cache + return."""
    topo = get_topology()
    observed, sampled, caller_samples = await observe_edges(lookback, max_traces)
    drift = diff_edges(topo, observed, sampled, caller_samples)
    set_last_drift(drift)
    logger.info(
        "reconcile: %d traces, dq=%s, %d undeclared, %d unobserved",
        sampled,
        drift.dq_score,
        len(drift.undeclared_edges),
        len(drift.unobserved_edges),
    )
    return drift


if __name__ == "__main__":  # pragma: no cover
    drift = asyncio.run(reconcile())
    print(f"topology v{drift.topology_version} reconciled against {drift.traces_sampled} traces")
    print(
        f"  declared={drift.declared_count} observed={drift.observed_count}"
        f" dq_score={drift.dq_score}"
    )
    if drift.undeclared_edges:
        print("  ⚠ observed but NOT declared (drift):")
        for e in drift.undeclared_edges:
            print(f"      {e.caller} → {e.callee}")
    if drift.unobserved_edges:
        print("  declared but not observed (stale or low traffic):")
        for e in drift.unobserved_edges:
            print(f"      {e.caller} → {e.callee}")
    if not drift.undeclared_edges and not drift.unobserved_edges:
        print("  ✓ declared graph matches the live call graph")
