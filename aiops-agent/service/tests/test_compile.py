"""Unit tests for the ownership-refactor compiler: per-service signal fragments
→ aggregated topology + contracts. Includes a regression guard that the
committed generated files stay in sync with the fragments."""

from app.signals.compile import (
    Dependency,
    ServiceSignal,
    _journey_chains,
    _topo_sort,
    compile_signals,
    load_fragments,
)
from app.signals.contract import get_contracts
from app.signals.topology import Edge, ServiceNode, get_topology


# ---- topo-sort / journey derivation ---------------------------------------

def test_topo_sort_deterministic_alpha_tiebreak():
    # gw fans out to order+payment, order→payment → linear chain, ties alpha.
    nodes = {"webapp", "api-gateway", "order-service", "payment-service"}
    edges = [("webapp", "api-gateway"), ("api-gateway", "order-service"),
             ("api-gateway", "payment-service"), ("order-service", "payment-service")]
    assert _topo_sort(nodes, edges) == [
        "webapp", "api-gateway", "order-service", "payment-service"]


def test_journey_chain_derived_from_edges():
    nodes = [ServiceNode(name=n, journeys=["checkout"]) for n in
             ("webapp", "api-gateway", "order-service", "payment-service")]
    edges = [Edge(caller="webapp", callee="api-gateway"),
             Edge(caller="api-gateway", callee="order-service"),
             Edge(caller="api-gateway", callee="payment-service"),
             Edge(caller="order-service", callee="payment-service")]
    chains = _journey_chains(nodes, edges)
    assert chains["checkout"] == ["webapp", "api-gateway", "order-service", "payment-service"]


# ---- compile a small fragment set -----------------------------------------

def test_compile_builds_nodes_edges_contracts():
    frags = [
        ServiceSignal(service="a", tier=1, journeys=["j"],
                      depends_on=[Dependency(callee="b", attribution="q")]),
        ServiceSignal(service="b", tier=2, journeys=["j"]),
    ]
    topo, contracts = compile_signals(frags)
    assert {n.name for n in topo.nodes} == {"a", "b"}
    assert topo.attribution_for("a", "b") == "q"
    assert topo.tier_of("b") == 2
    assert topo.journeys["j"] == ["a", "b"]
    assert contracts.for_service("a") is not None


# ---- regression guard: fragments stay in sync with committed aggregates ----

def test_shipped_fragments_compile_to_committed_files():
    frags = load_fragments()
    assert {f.service for f in frags} == {
        "webapp", "api-gateway", "order-service", "payment-service", "user-service"}
    new_t, new_c = compile_signals(frags)

    get_topology.cache_clear()
    get_contracts.cache_clear()
    cur_t, cur_c = get_topology(), get_contracts()

    # topology: nodes (tier/journeys/git_version), edges (+attribution), journeys
    assert {n.name: (n.tier, tuple(n.journeys), n.git_version) for n in new_t.nodes} \
        == {n.name: (n.tier, tuple(n.journeys), n.git_version) for n in cur_t.nodes}
    assert {(e.caller, e.callee): e.attribution for e in new_t.edges} \
        == {(e.caller, e.callee): e.attribution for e in cur_t.edges}
    assert new_t.journeys == cur_t.journeys

    # contracts: SLIs, freshness, decisions, exclusions, logs
    def digest(cs):
        return {c.service: (
            tuple((s.kind, s.promql, s.objective, s.unit) for s in c.slis),
            c.freshness_seconds, tuple(c.supported_decisions), tuple(c.exclusions),
            None if c.logs is None else (c.logs.selector, tuple(c.logs.error_events),
                                         c.logs.error_query, c.logs.note),
        ) for c in cs.contracts}
    assert digest(new_c) == digest(cur_c)
