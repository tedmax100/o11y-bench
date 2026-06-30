"""CLI: python -m app.eval run [options]

Drives the real headless RCA path over the fixtures and prints a pass@k +
regression report. Requires the observability stack reachable and GOOGLE_API_KEY
set — same prerequisites as a production headless run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .harness import (
    DEFAULT_BASELINE,
    DEFAULT_FIXTURES,
    DEFAULT_STORE,
    format_report,
    load_baseline,
    load_fixtures,
    regression_diff,
    run_suite,
    save_baseline,
)

_SUBCOMMANDS = {"run"}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.eval", description="aiops-agent eval harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="run the fixtures and print the report")
    pr.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    pr.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    pr.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_STORE,
        help="calibration store path (default: a dedicated eval.db; pass aiops.db to feed prod CE)",
    )
    pr.add_argument("-n", "--seeds", type=int, default=3, help="runs per fixture (default 3)")
    pr.add_argument("--only", default=None, help="run only the fixture with this id")
    pr.add_argument(
        "--save-baseline",
        action="store_true",
        help="overwrite the baseline with this run's correct rates",
    )

    # `run` is the default subcommand: `python -m app.eval -n 5` works the same
    # as `python -m app.eval run -n 5`. Only inject it when the first token isn't
    # already a subcommand and isn't a top-level help request.
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or (raw[0] not in _SUBCOMMANDS and raw[0] not in ("-h", "--help")):
        raw = ["run", *raw]

    args = parser.parse_args(raw)
    if args.cmd != "run":
        return 2

    fixtures = load_fixtures(args.fixtures)
    if args.only:
        fixtures = [f for f in fixtures if f.id == args.only]
    if not fixtures:
        print("no fixtures to run")
        return 1

    summaries = asyncio.run(run_suite(fixtures, seeds=args.seeds, store_path=args.store))
    baseline = load_baseline(args.baseline)
    diff = regression_diff(summaries, baseline)
    print(format_report(summaries, diff, store_path=args.store))

    if args.save_baseline:
        save_baseline(args.baseline, summaries)
        print(f"  baseline saved to {args.baseline}")

    # Exit non-zero on a regression so CI can gate on it.
    regressed = any(base is not None and cur < base for _, base, cur in diff)
    return 1 if regressed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
