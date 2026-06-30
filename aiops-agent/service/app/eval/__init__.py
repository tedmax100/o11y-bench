"""Offline regression / benchmark harness for the aiops-agent itself.

Unlike o11y-bench (which benchmarks a *generic* model+MCP scaffold), this drives
the real production RCA path — `app.agent.run_headless` — over a fixed set of
incident fixtures, multiple times each, and grades the structured `Findings`
against ground truth. It produces a pass@k report, a regression diff against a
saved baseline, and feeds dense correctness labels back into the calibration
(CE) store so ECE/Brier stop depending on whatever production webhooks happened
to fire.

Run it: `python -m app.eval run` (see `app/eval/__main__.py`).
"""
