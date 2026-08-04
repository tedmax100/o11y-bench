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

import json
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


def alignment_report(path: Path | None = None) -> dict:
    """Check every contract SLI against the registry and return a shippable
    verdict.

    The registry is a repo artifact, not something the agent image carries, so
    this runs at dev/CI time and its result is written to `schema_alignment.json`
    for `dq.py` to read. Crucially the "registry unreadable" case is reported as
    `checked: 0` rather than as violations: an empty name set would otherwise
    make *every* SLI look undeclared, turning a missing file into six findings.

    Deterministic on purpose (no timestamp): the artifact is committed, so CI can
    regenerate it and fail on a diff. Git already records when it last changed.
    """
    from .contract import get_contracts, validate_against_weaver

    declared = weaver_prom_metric_names(path)
    if not declared:
        return {
            "checked": 0,
            "declared_metrics": 0,
            "undeclared": [],
            "note": "weaver registry not readable; schema alignment unproven",
        }
    contracts = get_contracts().contracts
    undeclared = [w for c in contracts for w in validate_against_weaver(c, declared)]
    return {
        "checked": len(contracts),
        "declared_metrics": len(declared),
        "undeclared": undeclared,
        "note": (
            f"{len(contracts)} contracts checked against {len(declared)} registry metrics"
            if not undeclared
            else f"{len(undeclared)} SLI reference(s) not declared in the registry"
        ),
    }


def alignment_path() -> Path:
    return Path(__file__).parent / "schema_alignment.json"


if __name__ == "__main__":  # pragma: no cover
    report = alignment_report()
    alignment_path().write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if not report["checked"]:
        print(f"⚠ {report['note']}")
        print(f"  wrote {alignment_path().name} recording that there is no evidence")
        raise SystemExit(0)
    print(
        f"weaver registry declares {report['declared_metrics']} Prom metrics; "
        f"checked {report['checked']} contracts"
    )
    for w in report["undeclared"]:
        print(f"  ⚠ {w}")
    if not report["undeclared"]:
        print("✓ all contract SLIs reference metrics declared in the Weaver registry")
    print(f"  wrote {alignment_path().name}")
    raise SystemExit(1 if report["undeclared"] else 0)
