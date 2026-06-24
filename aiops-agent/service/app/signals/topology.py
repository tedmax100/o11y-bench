"""Service topology as a first-class, versioned Signal Plane artifact.

Loads `topology.yaml` (the declarative service graph: criticality tier, journey
membership, caller→callee edges) into a queryable `Topology`, replacing the
prose dependency graph the agent used to have to read out of schema_catalog.md.

The query API (`upstream`/`downstream`/`journey_of`/`tier_of`/`impacted_by`) is
what `context.py` turns into the decision-grade Signal context injected per RCA.
Loading is fail-open: a missing/broken file yields an empty Topology (logged),
so the run falls back to the catalog rather than crashing.

`validate_against_live()` is a pure helper that diffs the declared node set
against the live service_name set — wired into a CLI / startup check, not the
per-run hot path (keeping context building I/O-free). s2 takes this further by
reconciling *edges* against the live Tempo call graph.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ..config import settings

logger = logging.getLogger("aiops_agent.signals.topology")

_TIER_LABEL = {1: "revenue/edge-critical", 2: "important", 3: "best-effort"}


def tier_label(tier: int) -> str:
    return _TIER_LABEL.get(tier, "unclassified")


class ServiceNode(BaseModel):
    name: str
    role: str = ""
    tier: int = 3
    journeys: list[str] = Field(default_factory=list)
    owner: str = ""
    repo: str = "tedmax100/o11y-bench"
    git_version: str = ""


class Edge(BaseModel):
    caller: str
    callee: str
    # s4.2: PromQL measuring the caller's OWN failures attributed to this callee
    # (e.g. order's cancelled/errored orders with reason=payment*). Lets s4 prove
    # the caller is materially impacted by an unhealthy callee, not just adjacent.
    attribution: str = ""


class Topology(BaseModel):
    version: str = "0"
    nodes: list[ServiceNode] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    journeys: dict[str, list[str]] = Field(default_factory=dict)

    # ---- queries (replace "LLM reads the prose graph") --------------------

    def node(self, name: str) -> ServiceNode | None:
        return next((n for n in self.nodes if n.name == name), None)

    def names(self) -> list[str]:
        return [n.name for n in self.nodes]

    def upstream(self, svc: str) -> list[str]:
        """Direct callers — the services that break when `svc` breaks."""
        return sorted({e.caller for e in self.edges if e.callee == svc})

    def downstream(self, svc: str) -> list[str]:
        """Direct dependencies — what `svc` could be blocked by."""
        return sorted({e.callee for e in self.edges if e.caller == svc})

    def attribution_for(self, caller: str, callee: str) -> str | None:
        """The PromQL measuring `caller`'s failures attributed to `callee`, if
        the edge declares one (s4.2). None when the edge or field is absent."""
        e = next((e for e in self.edges if e.caller == caller and e.callee == callee), None)
        return e.attribution if e and e.attribution else None

    def impacted_by(self, svc: str) -> list[str]:
        """Transitive callers: the full set of services degraded if `svc` fails.
        Used by s4 blame propagation; here it powers the blast-path query."""
        seen: set[str] = set()
        frontier = [svc]
        while frontier:
            cur = frontier.pop()
            for caller in self.upstream(cur):
                if caller not in seen:
                    seen.add(caller)
                    frontier.append(caller)
        return sorted(seen)

    def tier_of(self, svc: str) -> int | None:
        n = self.node(svc)
        return n.tier if n else None

    def journey_of(self, svc: str) -> list[str]:
        n = self.node(svc)
        return list(n.journeys) if n else []

    def journey_position(self, journey: str, svc: str) -> tuple[int, int] | None:
        """1-based (position, length) of `svc` in a journey chain, or None."""
        chain = self.journeys.get(journey)
        if not chain or svc not in chain:
            return None
        return chain.index(svc) + 1, len(chain)


def _topology_path() -> Path:
    """Config override wins; otherwise the topology.yaml shipped beside this
    module (so it resolves regardless of the process CWD)."""
    if settings.topology_path:
        return Path(settings.topology_path)
    return Path(__file__).parent / "topology.yaml"


@lru_cache(maxsize=1)
def get_topology() -> Topology:
    """Load + cache the declared topology. Fail-open: any error → empty graph."""
    path = _topology_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        topo = Topology.model_validate(data)
        logger.info(
            "loaded topology v%s: %d nodes, %d edges",
            topo.version, len(topo.nodes), len(topo.edges),
        )
        return topo
    except Exception as e:  # missing file / bad yaml / schema mismatch
        logger.warning("topology load failed (%s); signal context disabled: %s", path, e)
        return Topology()


def validate_against_live(topo: Topology, live_names: list[str]) -> list[str]:
    """Pure existence check: declared nodes missing from the live service set,
    and live services not declared. Returned as human-readable warnings; this is
    the s1 'live alignment' surface (edges are reconciled later in s2)."""
    declared = set(topo.names())
    live = set(live_names)
    warnings = []
    for n in sorted(declared - live):
        warnings.append(f"declared service '{n}' not present in live telemetry")
    for n in sorted(live - declared):
        warnings.append(f"live service '{n}' is not declared in topology")
    return warnings


# ---- CLI: validate the artifact against live telemetry ---------------------

if __name__ == "__main__":  # pragma: no cover
    import asyncio
    import sys

    from ..tools.discovery import list_service_names

    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    topo = get_topology()
    if cmd == "validate":
        live = asyncio.run(list_service_names())
        warns = validate_against_live(topo, live)
        if not warns:
            print(f"topology v{topo.version} aligns with {len(live)} live services")
        else:
            print(f"topology v{topo.version} drift vs live:")
            for w in warns:
                print(f"  - {w}")
    else:
        print(f"topology v{topo.version}: {topo.names()}")
        for n in topo.nodes:
            print(f"  {n.name} tier-{n.tier} journeys={n.journeys} "
                  f"upstream={topo.upstream(n.name)} downstream={topo.downstream(n.name)}")
