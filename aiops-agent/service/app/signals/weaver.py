"""Cross-check signal contracts against the OTel Weaver semconv registry — the
schema single source of truth (signal-plane-design backlog).

The Weaver registry (`demo-services/weaver/registry`) declares each metric in
idiomatic dotted form (`app.payment.charges.count`) and records the actual
emitted Prometheus name in a `note:` ("Current code metric: `payment_charges_total`").
This module extracts those Prom names so a contract's SLI metric references can
be validated to only name metrics the registry declares — catching drift between
the contract (decision/SLO layer) and the registry (schema layer).

DEV/CI-time guard, NOT a runtime dependency: the registry is not shipped in the
agent image, so this is invoked from a CLI/test against the repo, never on the
RCA hot path. Fail-open: if the registry isn't readable, returns an empty set
(callers then simply skip the check).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from ..config import settings

logger = logging.getLogger("aiops_agent.signals.weaver")

# The Prom name lives in each metric's note: Current code metric: `name`.
_PROM_NAME_RE = re.compile(r"Current code metric:\s*`([a-z_][a-z0-9_]*)`")


def _registry_path() -> Path:
    if settings.weaver_registry_path:
        return Path(settings.weaver_registry_path)
    # Repo-root-relative (dev/CI). app/signals/weaver.py → parents[4] = repo root.
    return Path(__file__).parents[4] / "demo-services/weaver/registry/model/metrics.yaml"


def weaver_prom_metric_names(path: Path | None = None) -> set[str]:
    """The Prometheus metric base-names the Weaver registry declares (read from
    each metric group's `note`). Empty set when the registry isn't available."""
    p = path or _registry_path()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("weaver registry not readable (%s): %s", p, e)
        return set()
    names: set[str] = set()
    for g in data.get("groups", []):
        if g.get("type") != "metric":
            continue
        m = _PROM_NAME_RE.search(g.get("note") or "")
        if m:
            names.add(m.group(1))
    return names


if __name__ == "__main__":  # pragma: no cover
    from .contract import get_contracts, validate_against_weaver

    weaver = weaver_prom_metric_names()
    if not weaver:
        print("weaver registry not found — skipping alignment check")
        raise SystemExit(0)
    print(f"weaver registry declares {len(weaver)} Prom metrics: {sorted(weaver)}")
    any_drift = False
    for c in get_contracts().contracts:
        warns = validate_against_weaver(c, weaver)
        for w in warns:
            any_drift = True
            print(f"  ⚠ {w}")
    if not any_drift:
        print("✓ all contract SLIs reference metrics declared in the Weaver registry")
