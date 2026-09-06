#!/usr/bin/env python3
"""Backfill `calibration.drill` on rows written before the column existed.

The column defaults to 0, so every historical rehearsal currently reads as a
live incident — which is the state the gate was just taught not to trust. The
run itself no longer exists to ask, so the fact has to be recovered from what
was written down at the time:

  1. the investigation for that run, whose stored alert labels carry `drill`;
  2. `suspected_version`, which on the day41 rehearsals literally says `-drill`.

Rule 1 is only applied when the match is unambiguous. Before Day38 `run_id` WAS
the fingerprint, so one id covers every run of that alert — a single drill among
them would otherwise smear onto all of its siblings. Those rows are only flagged
when *every* investigation sharing the id says drill, and are reported as
`ambiguous` when the id's runs disagree, because guessing here quietly
manufactures the evidence the gate reads.

Dry-run by default. Writes only with --apply.

    kubectl -n demo exec -i deploy/aiops-agent -- python - < backfill_calibration_drill.py
    kubectl -n demo exec -i deploy/aiops-agent -- python - --apply < backfill_calibration_drill.py
"""

import argparse
import json
import sqlite3
import sys


def is_drill_labels(labels: dict) -> bool:
    return str((labels or {}).get("drill", "")).lower() in ("true", "1", "yes")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/data/aiops.db")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry-run")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # run_id → the drill verdicts of every investigation recorded under it.
    verdicts: dict[str, list[bool]] = {}
    for row in conn.execute("SELECT run_id, fp, payload FROM investigations"):
        try:
            payload = json.loads(row["payload"])
        except Exception:
            continue
        labels = (payload.get("alert") or {}).get("labels") or {}
        for key in (row["run_id"], row["fp"]):
            if key:
                verdicts.setdefault(key, []).append(is_drill_labels(labels))

    flagged, ambiguous = [], []
    for row in conn.execute("SELECT id, run_id, ts, suspected_version, drill FROM calibration"):
        if row["drill"]:
            continue
        version = row["suspected_version"] or ""
        seen = verdicts.get(row["run_id"], [])
        if "drill" in version.lower() or (seen and all(seen)):
            flagged.append((row["id"], row["ts"], version or "-"))
        elif any(seen):
            # The id covers several runs and they disagree — pre-Day38 collision.
            ambiguous.append((row["id"], row["ts"], version or "-"))

    for tag, rows in (("drill", flagged), ("ambiguous (left alone)", ambiguous)):
        print(f"{tag}: {len(rows)}")
        for rid, ts, version in rows:
            print(f"  id={rid} {ts} {version}")

    if args.apply and flagged:
        conn.executemany("UPDATE calibration SET drill=1 WHERE id=?", [(r[0],) for r in flagged])
        conn.commit()
        print(f"applied: {len(flagged)} row(s) marked as rehearsals")
    elif flagged:
        print("dry run — re-run with --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
