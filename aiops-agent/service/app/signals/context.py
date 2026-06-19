"""Build the decision-grade Signal context injected into an RCA turn.

Sits on top of the capability snapshot (which says *what metrics/spans/fields
exist*) and adds what ARE calls decision-grade semantics: criticality tier,
journey membership, and the dependency neighbourhood — so the agent reasons
about *which node to blame* with the topology as a first-class input instead of
inferring it from the catalog prose.

Pure and I/O-free (reads the cached static topology), so it can be called inline
on both the headless and chat paths without adding a network round-trip. s3/s4
extend the same block with signal contracts (authoritative SLIs) and live
dependency health.
"""

from __future__ import annotations

from ..config import settings
from .topology import get_topology, tier_label


def _service_block(svc: str) -> str | None:
    topo = get_topology()
    node = topo.node(svc)
    if node is None:
        return None  # unknown to the topology → nothing decision-grade to add

    lines = [f"### {svc}"]

    crit = f"tier-{node.tier} ({tier_label(node.tier)})"
    journeys = node.journeys or []
    if journeys:
        parts = []
        for j in journeys:
            pos = topo.journey_position(j, svc)
            parts.append(f"{j} ({pos[0]}/{pos[1]})" if pos else j)
        crit += "; journey: " + ", ".join(parts)
    lines.append(f"- criticality: {crit}")

    up = topo.upstream(svc)
    lines.append(
        f"- upstream (callers — degrade if this fails): {', '.join(up)}"
        if up else "- upstream (callers): none (entry point)"
    )

    down = topo.downstream(svc)
    lines.append(
        f"- downstream (dependencies — could be blocking this): {', '.join(down)}"
        if down else "- downstream (dependencies): none (leaf — not blocked by anything downstream)"
    )

    return "\n".join(lines)


def build_signal_context(services: list[str]) -> str | None:
    """Decision-grade Signal context for the RCA's service(s), or None when the
    topology knows none of them. Capped at 3 to bound prompt size, matching the
    capability snapshot."""
    if not settings.signal_plane_enabled:
        return None
    topo = get_topology()
    blocks = [b for svc in services[:3] if (b := _service_block(svc))]
    if not blocks:
        return None
    return (
        f"## Signal context (topology v{topo.version})\n"
        "Decision-grade dependency + criticality context for the service(s) in "
        "question. Use this to attribute root cause to the right node: a failing "
        "service may be a *symptom* of a failing downstream dependency, not the "
        "cause. Higher-tier services on a user journey matter more when triaging "
        "multiple simultaneous alerts.\n\n" + "\n\n".join(blocks)
    )
