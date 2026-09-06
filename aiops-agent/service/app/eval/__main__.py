"""CLI: python -m app.eval run [options]

Drives the real headless RCA path over the fixtures and prints a pass@k +
regression report. Requires the observability stack reachable and GOOGLE_API_KEY
set — same prerequisites as a production headless run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import stack as stackmod
from .harness import (
    DEFAULT_BASELINE,
    DEFAULT_FIXTURE_RECORD,
    DEFAULT_FIXTURES,
    DEFAULT_STORE,
    format_ab_report,
    format_report,
    library_overlap,
    load_baseline,
    load_fixtures,
    recall_arm,
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
        "--fixture-record",
        type=Path,
        default=DEFAULT_FIXTURE_RECORD,
        help="committed JSONL the autonomy gate reads; new labels are appended for review",
    )
    pr.add_argument(
        "--no-record",
        action="store_true",
        help="do not touch the fixture record (experiments that should not vouch for anything)",
    )
    pr.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_STORE,
        help="calibration store path (default: a dedicated eval.db; pass aiops.db to feed prod CE)",
    )
    pr.add_argument(
        "-n",
        "--seeds",
        type=int,
        default=1,
        help=(
            "repeated calls per fixture within one pass (default 1). NOT a sampling "
            "knob: the seed sets a thread id and a record id and never reaches the "
            "model call, which runs at temperature 0 — across every run recorded, "
            "26 of 27 multi-seed groups returned an identical verdict. Use --repeat."
        ),
    )
    pr.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "passes over the whole suite (default 1). Under --stack the container "
            "stays up, so passes differ only in what the model does with a byte-"
            "identical prompt — measured at 0% spread across five fixtures, which is "
            "what temperature 0 means. The swings this was built to chase happen "
            "between invocations, where a new scenario time moves every timestamp in "
            "the prompt; use this to confirm a result is stable, not to average one out."
        ),
    )
    pr.add_argument("--only", default=None, help="run only the fixture with this id")
    pr.add_argument(
        "--recall",
        choices=("on", "off", "both"),
        default="on",
        help="past-case recall arm: on (default, the runtime setting), off (the "
        "control), or both (run the suite twice and print the delta)",
    )
    pr.add_argument(
        "--save-baseline",
        action="store_true",
        help="overwrite the baseline with this run's correct rates",
    )
    # --- reproducible-environment (Path A) ---
    pr.add_argument(
        "--stack",
        action="store_true",
        help="boot the prebuilt o11y-stack image (deterministic incident) and run "
        "against it; pins every fixture clock to the stack's scenario time",
    )
    pr.add_argument("--image", default=stackmod.DEFAULT_IMAGE, help="stack image for --stack")
    pr.add_argument(
        "--scenario-time",
        default=None,
        help="O11Y_SCENARIO_TIME_ISO for the baked data (default: now); only with --stack",
    )
    pr.add_argument(
        "--boot-timeout", type=float, default=180.0, help="seconds to wait for stack data"
    )
    pr.add_argument(
        "--keep-stack", action="store_true", help="leave the container running after the run"
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

    scenario_time = None
    booted = False
    try:
        if args.stack:
            scenario_time = args.scenario_time or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"booting {args.image} (scenario time {scenario_time})…")
            stackmod.boot(scenario_time, image=args.image)
            booted = True
            if not stackmod.wait_ready(scenario_time, timeout=args.boot_timeout):
                print("  stack did not produce queryable incident data in time")
                return 1
            print("  stack ready; running against fixed data")

        def _suite(recall: bool):
            with recall_arm(recall):
                return asyncio.run(
                    run_suite(
                        fixtures,
                        seeds=args.seeds,
                        store_path=args.store,
                        scenario_time=scenario_time,
                        repeats=args.repeat,
                    )
                )

        if args.recall == "both":
            # Control first: the recall arm writes nothing to the case library
            # (the harness opens no case scope), but running the control second
            # would still put it after N more incidents of wall-clock drift.
            off_summaries = _suite(False)
            summaries = _suite(True)
        else:
            off_summaries = None
            summaries = _suite(args.recall == "on")
    finally:
        if booted and not args.keep_stack:
            stackmod.teardown()

    if off_summaries is not None:
        print(format_ab_report(summaries, off_summaries, library_overlap(fixtures)))
        print()

    baseline = load_baseline(args.baseline)
    diff = regression_diff(summaries, baseline)
    print(format_report(summaries, diff, store_path=args.store))

    if args.save_baseline:
        save_baseline(args.baseline, summaries)
        print(f"  baseline saved to {args.baseline}")

    # Copy the labels this run earned into the committed record the autonomy
    # gate reads. Appending to a tracked file rather than writing the cluster
    # directly is the point: the evidence that can unlock AUTO arrives as a diff
    # a person merges. Nothing is committed here — that decision stays with the
    # human who is about to read it.
    if not args.no_record:
        from ..calibration import load_records
        from .record import append as append_record

        added = append_record(load_records(args.store), args.fixture_record)
        if added:
            print(f"  {added} label(s) appended to {args.fixture_record} — review the diff")
        else:
            print(f"  no new labels for {args.fixture_record}")

    # Exit non-zero on a regression so CI can gate on it.
    regressed = any(base is not None and cur < base for _, base, cur in diff)
    return 1 if regressed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
