"""Environment fit — does the knowledge we inject belong to the stores we query?

The Signal Plane hands the agent a catalog it did not derive: contract SLIs with
authoritative PromQL, log selectors with the right key, a declared topology. All
of it is knowledge *about one environment*, and none of it carries a check that
the environment on the other end of `settings.prometheus_url` is that one.

Measured once against a twin stack that renames everything and changes nothing
else, the same catalog scored 1.0 at home and 0.0 on the twin — same services,
same traffic, same incident. Without this check the agent runs the 0.0 case
exactly as confidently as the 1.0 case, because every query comes back empty or
wrong rather than refused.

Three questions, one per store, all read-only:

  metrics  do the contracts' SLI metric base-names exist in Prometheus
  logs     is each log selector's key indexable AND does its value exist — the
           key alone is not enough, since Loki keeps `service_name` and fills it
           with `unknown_service` when the resource attribute is missing
  traces   are the declared services present in Tempo's resource.service.name

`fit_verdict()` reduces that to the shape governance already consumes for
calibration and topology (`proven_good` + note), so a catalog that belongs to
another environment narrows autonomy through the same door as a stale topology.
Conservative by construction: never computed, stale, or a store that did not
answer all land as "unproven" — never as "fits".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..config import settings
from ..tools.discovery import _epoch_ns, _parse_dt
from ..tools.query import _get_json
from .contract import get_contracts
from .topology import get_topology

logger = logging.getLogger("aiops_agent.signals.envfit")

_ROLLUP_SUFFIXES = ("_bucket", "_sum", "_count")


@dataclass
class EnvFit:
    """How much of the injected knowledge resolves against the live stores."""

    checked: int = 0
    resolved: int = 0
    by_store: dict[str, tuple[int, int]] = field(default_factory=dict)  # store -> (hit, total)
    unresolved: list[str] = field(default_factory=list)
    computed_ts: float = 0.0
    complete: bool = True  # False when a store did not answer

    @property
    def score(self) -> float | None:
        return round(self.resolved / self.checked, 4) if self.checked else None


_last: EnvFit | None = None


def get_last_fit() -> EnvFit | None:
    """The cached fit, or None when nobody has measured this environment yet."""
    return _last


async def _live_metric_names() -> set[str]:
    data = await _get_json(settings.prometheus_url, "/api/v1/label/__name__/values", {})
    names = set(data.get("data", []) if isinstance(data, dict) else [])
    # A contract references the histogram family; Prometheus lists its rollups.
    for n in list(names):
        for suf in _ROLLUP_SUFFIXES:
            if n.endswith(suf):
                names.add(n[: -len(suf)])
    return names


async def _loki_label_values(key: str) -> set[str]:
    start, end = _parse_dt("now-1h"), _parse_dt("now")
    data = await _get_json(
        settings.loki_url,
        f"/loki/api/v1/label/{key}/values",
        {"start": _epoch_ns(start), "end": _epoch_ns(end)},
    )
    return set(data.get("data", []) if isinstance(data, dict) else [])


async def _loki_indexable() -> set[str]:
    start, end = _parse_dt("now-1h"), _parse_dt("now")
    data = await _get_json(
        settings.loki_url,
        "/loki/api/v1/labels",
        {"start": _epoch_ns(start), "end": _epoch_ns(end)},
    )
    return set(data.get("data", []) if isinstance(data, dict) else [])


async def _tempo_service_names() -> set[str]:
    data = await _get_json(
        settings.tempo_url, "/api/v2/search/tag/resource.service.name/values", {}
    )
    vals = data.get("tagValues", []) if isinstance(data, dict) else []
    return {v.get("value") for v in vals if isinstance(v, dict) and v.get("value")}


async def compute_env_fit() -> EnvFit:
    """Measure the catalog against the three stores and cache the result."""
    global _last
    fit = EnvFit(computed_ts=time.time())

    try:
        live_metrics = await _live_metric_names()
    except Exception as e:  # any failure to reach a store is "no evidence"
        logger.info("env fit: Prometheus did not answer: %s", e)
        live_metrics, fit.complete = None, False
    if live_metrics is not None:
        hit = total = 0
        for c in get_contracts().contracts:
            for base in sorted(c.metric_basenames()):
                total += 1
                if base in live_metrics:
                    hit += 1
                else:
                    fit.unresolved.append(f"metric {base} ({c.service})")
        fit.by_store["metrics"] = (hit, total)

    try:
        indexable = await _loki_indexable()
        hit = total = 0
        cache: dict[str, set[str]] = {}
        for c in get_contracts().contracts:
            if not c.logs or not c.logs.selector:
                continue
            key, _, raw = c.logs.selector.strip("{}").partition("=")
            key, want = key.strip(), raw.strip().strip('"')
            total += 1
            if key not in indexable:
                fit.unresolved.append(f"log label {key} not indexable ({c.service})")
                continue
            if key not in cache:
                cache[key] = await _loki_label_values(key)
            if want in cache[key]:
                hit += 1
            else:
                fit.unresolved.append(f"log selector {key}={want} matches nothing")
        fit.by_store["logs"] = (hit, total)
    except Exception as e:
        logger.info("env fit: Loki did not answer: %s", e)
        fit.complete = False

    try:
        live_services = await _tempo_service_names()
        hit = total = 0
        for svc in sorted(get_topology().names()):
            total += 1
            if svc in live_services:
                hit += 1
            else:
                fit.unresolved.append(f"service {svc} unknown to Tempo")
        fit.by_store["traces"] = (hit, total)
    except Exception as e:
        logger.info("env fit: Tempo did not answer: %s", e)
        fit.complete = False

    fit.resolved = sum(h for h, _ in fit.by_store.values())
    fit.checked = sum(t for _, t in fit.by_store.values())
    _last = fit
    logger.info(
        "env fit %s (%d/%d resolved, complete=%s)",
        fit.score,
        fit.resolved,
        fit.checked,
        fit.complete,
    )
    return fit


def fit_verdict() -> dict:
    """{proven_good, score, note} — the shape governance already reads."""
    fit = get_last_fit()
    if fit is None:
        return {
            "proven_good": False,
            "score": None,
            "note": "injected knowledge never checked against these stores; env fit unproven",
        }
    if not fit.complete or not fit.checked:
        return {
            "proven_good": False,
            "score": fit.score,
            "note": "some stores did not answer; env fit unproven",
        }
    age = int(time.time() - fit.computed_ts)
    if age > settings.dq_max_env_fit_age_seconds:
        return {
            "proven_good": False,
            "score": fit.score,
            "note": (
                f"env fit measured {age}s ago (> {settings.dq_max_env_fit_age_seconds}s); stale"
            ),
        }
    if fit.score is not None and fit.score < settings.dq_min_env_fit:
        head = fit.unresolved[0] if fit.unresolved else ""
        return {
            "proven_good": False,
            "score": fit.score,
            "note": (
                f"only {fit.resolved}/{fit.checked} of the injected knowledge resolves against "
                f"these stores ({head}); the catalog may belong to another environment"
            ),
        }
    return {
        "proven_good": True,
        "score": fit.score,
        "note": f"injected knowledge resolves here ({fit.resolved}/{fit.checked})",
    }


if __name__ == "__main__":  # pragma: no cover - dev CLI
    import asyncio
    import json

    async def _main() -> None:
        fit = await compute_env_fit()
        for store, (hit, total) in fit.by_store.items():
            print(f"  {store:<8} {hit}/{total}")
        for u in fit.unresolved[:10]:
            print(f"      ✗ {u}")
        print(json.dumps(fit_verdict(), ensure_ascii=False))

    asyncio.run(_main())
