"""Signal Plane — decision-grade telemetry (signal-plane-design).

Turns the Signal semantics that used to live as prose in `schema_catalog.md`
(topology / criticality / journey) into first-class, versioned, queryable
artifacts injected into the RCA as decision-grade context. Read-only enrichment
sitting *upstream* of the read-only reasoning core — it never mutates anything
and is fail-open: if it can't build context, the run continues on the catalog +
discover_* tools as before.

s1 ships the declarative topology artifact + context injection. s2 reconciles it
against the live Tempo call graph (drift / DQ), s3 adds per-service signal
contracts, s4 adds dependency-health blame propagation.
"""
