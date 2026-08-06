"""Build the decision-grade Signal context injected into an RCA turn.

Sits on top of the capability snapshot (which says *what metrics/spans/fields
exist*) and adds what ARE calls decision-grade semantics: criticality tier,
journey membership, and the dependency neighbourhood — so the agent reasons
about *which node to blame* with the topology as a first-class input instead of
inferring it from the catalog prose.

Pure and I/O-free (reads the cached static topology + the cached reconcile
snapshot), so it can be called inline on both the headless and chat paths
without adding a network round-trip. When a reconcile (s2) has run, declared
edges not seen in recent traces are marked, observed-but-undeclared edges are
surfaced, and a DQ score is shown — turning the static graph into one the agent
knows is (or isn't) aligned to live telemetry. s3/s4 extend the same block with
signal contracts (authoritative SLIs) and live dependency health.

Every marker here is rationed. A ⚠ costs the reader attention, so it is only
spent where the reconcile had real evidence (the caller was exercised and still
never took the edge), each missing edge is stated once rather than on both
endpoints, and the DQ line explains what its own score does not cover. A block
full of warnings that are mostly sampling artefacts trains the reader — human or
model — to skip all of them.
"""

from __future__ import annotations

from ..config import settings
from .contract import SignalContract, contract_for
from .reconcile import TopologyDrift, get_last_drift
from .topology import get_topology, tier_label

# A caller seen in fewer than this many sampled traces tells us nothing about
# the edges it didn't take: absence of evidence, not evidence of absence.
_MIN_CALLER_EVIDENCE = 5


def _annotate(edge: tuple[str, str], drift: TopologyDrift | None) -> str:
    """Mark a declared edge only when the reconcile had a real chance to see it.

    Marking every unobserved edge floods the block with ⚠ that are mostly
    sampling artefacts, and a warning that is usually wrong gets ignored — the
    ones that matter go with it. So an edge is only called out when its caller
    was actually exercised in the sample; otherwise the honest answer is that we
    have no evidence either way, and that is said once, quietly.
    """
    if not drift or not any((e.caller, e.callee) == edge for e in drift.unobserved_edges):
        return ""
    seen = drift.evidence_for(edge[0])
    if seen >= _MIN_CALLER_EVIDENCE:
        return f" (⚠ not seen in {seen} sampled traces of {edge[0]})"
    return " (not exercised in this sample)"


def _service_block(svc: str, drift: TopologyDrift | None) -> str | None:
    topo = get_topology()
    node = topo.node(svc)
    if node is None:
        return None  # unknown to the topology → nothing decision-grade to add

    lines = [f"### {svc}"]

    crit = f"tier-{node.tier} ({tier_label(node.tier)})"
    if node.journeys:
        parts = []
        for j in node.journeys:
            pos = topo.journey_position(j, svc)
            parts.append(f"{j} ({pos[0]}/{pos[1]})" if pos else j)
        crit += "; journey: " + ", ".join(parts)
    lines.append(f"- criticality: {crit}")

    up = topo.upstream(svc)
    if up:
        # Deliberately unannotated: the same edge is already flagged on the
        # caller's own downstream line, and saying it twice reads as two
        # independent problems.
        rendered = ", ".join(up)
        lines.append(f"- upstream (callers — degrade if this fails): {rendered}")
    else:
        lines.append("- upstream (callers): none (entry point)")

    down = topo.downstream(svc)
    if down:
        rendered = ", ".join(d + _annotate((svc, d), drift) for d in down)
        lines.append(f"- downstream (dependencies — could be blocking this): {rendered}")
    else:
        lines.append(
            "- downstream (dependencies): none (leaf — not blocked by anything downstream)"
        )

    # Observed-but-undeclared edges touching this service: the topology is
    # incomplete here, so the agent shouldn't treat the declared graph as closed.
    if drift:
        extra = [
            f"{e.caller} → {e.callee}"
            for e in drift.undeclared_edges
            if svc in (e.caller, e.callee)
        ]
        if extra:
            lines.append(
                "- ⚠ observed dependencies NOT in the declared topology: " + ", ".join(extra)
            )

    lines.extend(_contract_lines(svc))
    return "\n".join(lines)


def _contract_lines(svc: str) -> list[str]:
    """Authoritative SLI queries + freshness + exclusions for a service (s3)."""
    contract: SignalContract | None = contract_for(svc)
    if contract is None:
        return []
    lines: list[str] = []
    if contract.slis:
        lines.append(
            "- SLI (authoritative — cite these exact queries, don't re-derive; "
            "the capability snapshot is authoritative for what *exists*):"
        )
        for sli in contract.slis:
            unit = f" [{sli.unit}]" if sli.unit else ""
            target = f"  target: {sli.objective}" if sli.objective else ""
            lines.append(f"    {sli.kind}: {sli.promql}{unit}{target}")
        lines.append(
            f"- signal freshness guarantee: ≤{contract.freshness_seconds}s"
            " (older samples are stale)"
        )
    if contract.logs:
        lg = contract.logs
        lines.append(
            "- Logs (authoritative — use THIS selector & event values; do NOT use "
            '`{service=...}` or invent event names like `event="error"`):'
        )
        lines.append(f"    stream selector: {lg.selector}")
        if lg.error_events:
            lines.append(
                '    failure events (filter after the selector with `| event="…"`): '
                + ", ".join(lg.error_events)
            )
        if lg.error_query:
            lines.append(f"    find failures: {lg.error_query}")
        if lg.note:
            lines.append(f"    note: {lg.note}")
    for ex in contract.exclusions:
        lines.append(f"- caveat: {ex}")
    return lines


def _dq_note(drift: TopologyDrift | None) -> str:
    if not drift or not drift.traces_sampled:
        return ""
    score = "n/a" if drift.dq_score is None else f"{drift.dq_score:.0%}"
    note = (
        f"\nTopology data-quality (last reconcile, {drift.traces_sampled} traces): "
        f"declared/observed agreement {score}."
    )
    if drift.dq_score is not None and drift.dq_score < 1.0:
        note += (
            " ⚠ the declared graph is out of date vs live traffic"
            " — trust the trace evidence over it where they disagree."
        )
    # The agreement score only grades observed→declared, so a graph can score
    # 100% while declared edges sit unseen. Saying so here stops the header from
    # contradicting the ⚠ markers a few lines below it.
    elif drift.unobserved_edges:
        note += (
            f" That score only grades edges seen in traffic; {len(drift.unobserved_edges)}"
            " declared edge(s) were not exercised in this sample and are marked below."
        )
    return note


def build_signal_context(services: list[str]) -> str | None:
    """Decision-grade Signal context for the RCA's service(s), or None when the
    topology knows none of them. Capped at 3 to bound prompt size, matching the
    capability snapshot."""
    if not settings.signal_plane_enabled:
        return None
    topo = get_topology()
    drift = get_last_drift()
    blocks = [b for svc in services[:3] if (b := _service_block(svc, drift))]
    if not blocks:
        return None
    return (
        f"## Signal context (topology v{topo.version})\n"
        "Decision-grade dependency + criticality context for the service(s) in "
        "question. Use this to attribute root cause to the right node: a failing "
        "service may be a *symptom* of a failing downstream dependency, not the "
        "cause. Higher-tier services on a user journey matter more when triaging "
        "multiple simultaneous alerts." + _dq_note(drift) + "\n\n" + "\n\n".join(blocks)
    )
