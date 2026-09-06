"""The fixture record: the evidence the regression gate reads, in version control.

The gate used to read `app/eval/eval.db`, which is gitignored. It reached the
image only because `COPY app /app/app` picked up whatever happened to be on the
machine that ran the build — so the verdict was a function of whose laptop built
it, and on CI or a fresh clone there was no record at all. Fail-closed, but not
reproducible, which is the thing this whole system keeps arguing about.

So the record moved to a committed JSONL and `eval.db` went back to being what
it always was: the harness's working store, for reports and regression diffs.
Two consequences worth stating, because both are the point rather than a side
effect:

- The evidence enters version control the way any other change does — a person
  looks at a diff and merges it. "Who may write the evidence that unlocks
  autonomy" becomes "who may merge", instead of "who can write one file in the
  cluster".
- The image carries its own report card. This version of the agent, and the
  labels earned by this version — which is exactly what the gate is asking
  about, and why the record is not fetched at runtime from somewhere newer.

Only the fields the curve is computed from are kept. `summary`/`hypothesis` are
the run's prose and would make the diff unreadable for no gain; `run_id` stays
because it carries the fixture name, which is what makes a diff mean anything to
a person reading it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..calibration import CalibrationRecord

logger = logging.getLogger("aiops_agent.eval.record")

FIELDS = ("run_id", "ts", "confidence", "correct", "source", "grading_mode")


def load(path: str | Path) -> list[CalibrationRecord]:
    """Every labeled row in the record. A malformed line is skipped rather than
    sinking the file — the same treatment `calibration.load_records` gives a bad
    database row, and for the same reason: one corrupt line must not silently
    turn the gate's evidence into "no record"."""
    out: list[CalibrationRecord] = []
    p = Path(path)
    if not p.exists():
        return out
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(CalibrationRecord.model_validate(json.loads(line)))
        except Exception as e:
            logger.warning("skipping malformed fixture record line %d: %s", i, e)
    return out


def append(records: list[CalibrationRecord], path: str | Path) -> int:
    """Add labeled runs to the record, skipping run_ids already present.

    Appends rather than rewrites so the file reads as what it is: a log of what
    this agent has been graded on, in the order it happened. Returns how many
    lines were added, so a caller can tell a person to go look at the diff.
    """
    p = Path(path)
    known = {r.run_id for r in load(p)}
    new = [r for r in records if r.correct is not None and r.run_id not in known]
    if not new:
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for r in new:
            fh.write(json.dumps({f: getattr(r, f) for f in FIELDS}, sort_keys=True) + "\n")
    return len(new)
