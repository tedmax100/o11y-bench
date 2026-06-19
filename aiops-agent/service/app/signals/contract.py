"""Per-service signal contracts — the authoritative way to read a service's
health (signal-plane-design s3).

A contract declares, per service, which metric is the error/latency/throughput
SLI and the *correct* PromQL for it (right aggregation, right unit), plus a
freshness guarantee, the decisions the signal supports, and exclusion conditions
(when not to trust it). Injected into the RCA so the agent cites a declared,
correct query instead of re-deriving one each run and hitting the recurring bugs
(histogram-seconds-in-ms-buckets, count-vs-rate, dividing without clamp).

Like topology, this is a declared artifact that can drift: the capability
snapshot stays authoritative for which metrics *exist*; the contract is
authoritative for *how to aggregate* them. `validate_against_live()` checks the
referenced metric base-names still appear in live telemetry. Loading is
fail-open — a missing/broken file just means no contract injection.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ..config import settings

logger = logging.getLogger("aiops_agent.signals.contract")

# Metric family base-names referenced in an SLI's PromQL, for live validation:
# an identifier, minus the _bucket/_sum/_count/_total suffixes and any {labels}.
_METRIC_RE = re.compile(r"\b([a-z_][a-z0-9_]*_(?:total|seconds|bucket|sum|count))\b")


class SLI(BaseModel):
    kind: str                      # error | latency | throughput | saturation
    promql: str                    # authoritative query (correct aggregation/unit)
    objective: str = ""            # declared target, e.g. "p95 < 0.2s"
    unit: str = ""                 # ratio | s | rps | …


class LogSignal(BaseModel):
    """Authoritative LogQL for a service — the correct stream selector and the
    real `event=` values that mark failures. Declared because agents reliably get
    these wrong (use `{service=...}` instead of `{service_name=...}`, invent
    `event="error"` / `event="order_failed"` that don't exist)."""
    selector: str                  # e.g. {service_name="payment-service"}
    error_events: list[str] = Field(default_factory=list)  # real event= values for failures
    error_query: str = ""          # representative authoritative LogQL to surface failures
    note: str = ""


class SignalContract(BaseModel):
    service: str
    freshness_seconds: int = 60    # samples older than this → treat as stale
    slis: list[SLI] = Field(default_factory=list)
    supported_decisions: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    logs: LogSignal | None = None  # authoritative log selector + failure events

    def metric_basenames(self) -> set[str]:
        """Metric families referenced by this contract's SLIs (suffix-stripped),
        for diffing against live telemetry."""
        out: set[str] = set()
        for sli in self.slis:
            for m in _METRIC_RE.findall(sli.promql):
                for suffix in ("_bucket", "_sum", "_count"):
                    if m.endswith(suffix):
                        m = m[: -len(suffix)]
                        break
                out.add(m)
        return out


class ContractSet(BaseModel):
    version: str = "0"
    contracts: list[SignalContract] = Field(default_factory=list)

    def for_service(self, service: str) -> SignalContract | None:
        return next((c for c in self.contracts if c.service == service), None)


def _contracts_path() -> Path:
    if settings.signal_contracts_path:
        return Path(settings.signal_contracts_path)
    return Path(__file__).parent / "contracts.yaml"


@lru_cache(maxsize=1)
def get_contracts() -> ContractSet:
    """Load + cache the signal contracts. Fail-open: any error → empty set."""
    path = _contracts_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cs = ContractSet.model_validate(data)
        logger.info("loaded signal contracts v%s: %d services", cs.version, len(cs.contracts))
        return cs
    except Exception as e:
        logger.warning("signal contracts load failed (%s); contract injection disabled: %s", path, e)
        return ContractSet()


def contract_for(service: str) -> SignalContract | None:
    return get_contracts().for_service(service)


def validate_against_live(contract: SignalContract, live_metric_names: list[str]) -> list[str]:
    """Pure check: SLI metric base-names this contract references that don't
    appear in the live metric set (contract drift)."""
    live = set(live_metric_names)
    return [
        f"{contract.service}: SLI references '{m}' not present in live metrics"
        for m in sorted(contract.metric_basenames())
        if m not in live
    ]


def validate_against_weaver(contract: SignalContract, weaver_metric_names: set[str]) -> list[str]:
    """Pure check: SLI metric base-names this contract references that the Weaver
    semconv registry does NOT declare (contract drifting from the schema source
    of truth). See app/signals/weaver.py for the registry name extraction."""
    return [
        f"{contract.service}: SLI references '{m}' not declared in the Weaver registry"
        for m in sorted(contract.metric_basenames())
        if m not in weaver_metric_names
    ]


# ---- CLI: validate contracts against live telemetry ------------------------

if __name__ == "__main__":  # pragma: no cover
    import asyncio

    from ..tools.discovery import discover_metrics

    async def _main() -> None:
        cs = get_contracts()
        print(f"signal contracts v{cs.version}: {len(cs.contracts)} services")
        any_drift = False
        for c in cs.contracts:
            if not c.slis:
                print(f"  {c.service}: no SLIs (relies on auto HTTP / Loki)")
                continue
            live = await discover_metrics(c.service)
            names = [m["name"] for m in live.get("metrics", [])]
            warns = validate_against_live(c, names)
            if warns:
                any_drift = True
                for w in warns:
                    print(f"  ⚠ {w}")
            else:
                print(f"  ✓ {c.service}: {len(c.slis)} SLIs aligned with live metrics")
        if not any_drift:
            print("all contract SLIs reference live metrics")

    asyncio.run(_main())
