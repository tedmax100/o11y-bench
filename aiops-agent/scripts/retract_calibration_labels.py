#!/usr/bin/env python3
"""Retract calibration verdicts that judged a run against a premise later shown
to be wrong.

A label is evidence, and evidence that turns out to rest on a mistaken
correction keeps being counted forever — the reliability curve has no notion of
"we were wrong about being wrong". The demo record has exactly one such case: a
run concluded the v2.5.0 validator regression (the seeded fault), a human marked
it wrong with the note "根因是 DB 連線問題", and the three obedient
re-investigations that followed the correction were each marked wrong in turn.
Four of the six rows in the decision band come from that one chain.

This retracts the verdict and leaves everything else: the row, its confidence,
its summary, and the note that produced it all stay, so the mistake remains
readable. Only `correct` goes back to NULL — the run returns to the pool of
things nobody has judged, which is what was actually true.

Dry-run by default. Writes only with --apply.

    kubectl -n demo exec -i deploy/aiops-agent -- python - --run-id RUN \
        < retract_calibration_labels.py
"""

import argparse
import sqlite3
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/data/aiops.db")
    ap.add_argument("--run-id", action="append", default=[], help="repeatable")
    ap.add_argument("--id", action="append", type=int, default=[], help="calibration row id")
    ap.add_argument("--reason", default="", help="recorded in the audit log")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    if not args.run_id and not args.id:
        ap.error("nothing to retract: pass --run-id and/or --id")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    where, params = [], []
    if args.run_id:
        where.append(f"run_id IN ({','.join('?' * len(args.run_id))})")
        params += args.run_id
    if args.id:
        where.append(f"id IN ({','.join('?' * len(args.id))})")
        params += [str(i) for i in args.id]
    clause = f"({' OR '.join(where)}) AND correct IS NOT NULL"

    rows = conn.execute(
        f"SELECT id, run_id, ts, confidence, correct, correction_note FROM calibration "
        f"WHERE {clause} ORDER BY id",
        params,
    ).fetchall()
    print(f"labeled rows matched: {len(rows)}")
    for r in rows:
        note = (r["correction_note"] or "")[:60]
        print(f"  id={r['id']} {r['ts']} conf={r['confidence']} correct={r['correct']} {note}")
    if not rows:
        return 0

    if not args.apply:
        print("dry run — re-run with --apply to write")
        return 0

    conn.execute(
        f"UPDATE calibration SET correct=NULL, score=NULL, source=NULL WHERE {clause}", params
    )
    conn.commit()
    print(f"retracted: {len(rows)} verdict(s); rows and notes kept")

    # Best-effort: the agent's own audit log if this is running inside the pod.
    try:
        sys.path.insert(0, "/app")
        from app import audit

        audit.record(
            "calibration_retract",
            "ok",
            actor="operator",
            detail={"ids": [r["id"] for r in rows], "reason": args.reason},
        )
        print("audit recorded")
    except Exception as e:
        print(f"audit not recorded ({type(e).__name__}: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
