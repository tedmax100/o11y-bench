"""Grade only the runs whose answer is recoverable, and say why for each one.

A scripted grade is defensible exactly when the fault was injected on purpose:
we chose the root cause, so we know what the right answer was. That is the same
reason `gameday.py` is allowed to grade its own drill. Everything else in the
backlog is left alone — a verdict nobody can reconstruct is worse than a NULL.

The truth table below is not an opinion about each run. It is what was done to
the cluster, and each entry names the check that recovers it:

  drill scenario a   the bad flag file shipped IN the pod template, plus a
                     version bump. The previous ReplicaSet really is a good
                     state, so blaming the deploy is right.
  drill scenario b   the bad flag shipped in the ConfigMap that the unchanged
                     template mounts. Blaming the deploy is wrong: `rollout
                     undo` restores a template that was never the problem.
  session-cache      the cause is `user_session_cache_disabled` on user-service's
                     ConfigMap. Blaming an order-service version is wrong.
  2026-08-26 run     same shape as scenario b: the ConfigMap flag was flipped and
                     payment restarted. Verified from the cluster: every
                     ReplicaSet from revision 68 on runs the same image
                     (`demo-services/payment:dev`) and the same FEATURE_FLAGS_PATH,
                     so `git_version` is a label, not a build, and rolling back
                     cannot clear a flag-driven decline.

Labels are written by calling `label_run()` in the pod, NOT through
`POST /investigations/{fp}/label`: that endpoint kicks off a re-investigation
whenever `correct=False`, and a chain of those is what contaminated the
calibration pool the last time. A grader recording an old verdict has no
business spending tokens.

    P=aiops-agent/scripts/grade_known_answers.py
    kubectl -n demo exec -i deploy/aiops-agent -- python - < $P            # dry run
    kubectl -n demo exec -i deploy/aiops-agent -- sh -c 'APPLY=1 python -' < $P
"""

from __future__ import annotations

import json
import os
import urllib.request

AGENT = "http://localhost:8000"

# fp -> (correct, grading_mode, error_dimension, why)
TRUTH: dict[str, tuple[bool, str, str | None, str]] = {
    "19ff926125a8a1f9": (
        False,
        "culprit",
        "root_cause",
        "blamed a code regression in v2.5.0, but the fault was a ConfigMap flag flip; "
        "revisions 68-71 all run the same image, so the recommended rollout undo "
        "would not have cleared the declines",
    ),
    "616f93beb7137419": (
        True,
        "culprit",
        None,
        "drill scenario a (bad deploy): blaming the drill version is the right answer",
    ),
    "2eb483fa52c0c7a2": (
        True,
        "culprit",
        None,
        "drill scenario a (bad deploy): blaming the drill version is the right answer",
    ),
    "0c429fc1c5308ed6": (
        False,
        "culprit",
        "root_cause",
        "drill scenario b (bad config): the template was never the problem, "
        "so a code regression in the drill version is the wrong culprit",
    ),
    "253f7dcabc655ae5": (
        False,
        "culprit",
        "root_cause",
        "drill scenario b (bad config): the template was never the problem, "
        "so a code regression in the drill version is the wrong culprit",
    ),
    "dc26306bbddba8e4": (
        False,
        "culprit",
        "root_cause",
        "drill scenario b (bad config): the template was never the problem, "
        "so a code regression in the drill version is the wrong culprit",
    ),
    "2b0a13c99c8f670a": (
        False,
        "culprit",
        "root_cause",
        "session-cache drill: the cause is the user_session_cache_disabled flag on "
        "user-service, not a code regression in order-service v3.1.2",
    ),
}

# Runs that carry their own `run_id` and never reached the Todo queue, because
# nothing wrote an investigation row for them. The fp-keyed table above cannot
# reach these: they all share one fingerprint that already has its verdict, and
# `cal_label` updates the latest row per run_id — which is the whole reason runs
# stopped being identified by fingerprint. One verdict per run, addressed by run.
#
# Same discipline as TRUTH: each entry is what was done to the cluster, not an
# opinion about the answer. The order-service incident is the confirmed case
# `ffa6ab9638c72564` — `user_session_cache_disabled` was set true on
# user-service, so every auth check fell through to the slow session store. The
# payment ones are the `payment-flags` ConfigMap flip, template unchanged.
#
# The rule applied to the version field is the one already used above for
# `2b0a13c99c8f670a`: naming the right cause and *also* pinning it on a version
# is wrong, because a rollback to that version's predecessor clears nothing. It
# is the same failure whether the version sits in the field or in the sentence.
BY_RUN: dict[str, tuple[bool, str, str | None, str]] = {
    "2b0a13c99c8f670a-20260820T162600-ff9a6d": (
        False,
        "inconclusive",
        "root_cause",
        "called the alert a false positive on a non-existent metric, at the "
        "incident's own start time; the session-cache incident was real",
    ),
    "2b0a13c99c8f670a-20260820T163500-564d0a": (
        True,
        "culprit",
        None,
        "named user-service session store timeouts and cache misses, and pinned "
        "it on no version — the right cause and the right kind of cause",
    ),
    "2b0a13c99c8f670a-20260820T164501-fac7f1": (
        False,
        "culprit",
        "root_cause",
        "blamed business-logic outcomes and stated the user-service session "
        "store was NOT timing out, which is the opposite of the cause",
    ),
    "2b0a13c99c8f670a-20260822T151536-98241e": (
        False,
        "culprit",
        "root_cause",
        "right cause, wrong kind of cause: pinned the session-cache flag on "
        "user-service v1.3.0, and rolling that back clears nothing",
    ),
    "2b0a13c99c8f670a-20260822T152640-6a767f": (
        False,
        "culprit",
        "root_cause",
        "right cause, pinned on order-service v3.1.2 — the exact version this "
        "incident's truth note says was not it",
    ),
    "2b0a13c99c8f670a-20260822T154008-8478b4": (
        False,
        "culprit",
        "root_cause",
        "right cause, pinned on order-service v3.1.2 — same as the run before it",
    ),
    "2c386e950a3fc8f5-20260828T165907-93c37d": (
        False,
        "culprit",
        "root_cause",
        "said configuration regression in the payment-flags ConfigMap and still "
        "carried suspected_version v2.5.0; the prose and the field named "
        "different causes and only one of them was right",
    ),
    "d99732674e74e0eb-20260829T104550-46ffbc": (
        True,
        "culprit",
        None,
        "named the payment-flags ConfigMap and the new_validator_odd_cents rule, "
        "with no version in the field or the prose",
    ),
}

# Named so the report says why they are being skipped rather than silently
# dropping them.
UNVERIFIABLE = {
    "1539e7b9b01d65bb": "June run; the metrics that would settle 'gateway timeouts vs "
    "new_validator' are long past Prometheus retention",
    "a93580b3-2757-4c2e-b48f-565d14183e72": "a chat question about request rates, not an "
    "incident; the culprit ruler does not measure it",
    "fb490a75-dcf2-48c7-ab0d-0c071294aba7": "'everything is normal' — an inconclusive-mode "
    "verdict, and whether it was right needs that day's data, which is gone",
    "4a42857b-9af9-4280-b687-2d9499d1d98f": "refused to investigate with no incident given; "
    "inconclusive mode, and confidence 0.0 would blow up the culprit math",
}


def http(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{AGENT}{path}", data=data, method=method, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    apply = os.environ.get("APPLY") == "1"

    todo = http("GET", "/todo")["investigations_to_label"]["items"]
    pending = {x["fp"]: x for x in todo}

    print(f"{'fp':<38} {'conf':>5}  verdict")
    print("-" * 100)
    graded = 0
    for fp, (correct, mode, dim, why) in TRUTH.items():
        x = pending.get(fp)
        if x is None:
            print(f"{fp:<38} {'-':>5}  already labeled or gone; skipped")
            continue
        verdict = "correct" if correct else "WRONG"
        print(f"{fp:<38} {x['confidence']:>5}  {verdict:<8} {why}")
        graded += 1
        if apply:
            from app.calibration import label_run

            ok = label_run(
                fp,
                correct,
                source="grader-truth",
                grading_mode=mode,
                error_dimension=dim,
                correction_note=why,
            )
            print(f"{'':<38} {'':>5}  -> {'labeled' if ok else 'NO RECORD'}")

    # The run-keyed half. No Todo lookup: these rows have no investigation to be
    # pending on, and the store is the only thing that knows they exist.
    from app import store as _store

    for run_id, (correct, mode, dim, why) in BY_RUN.items():
        row = _store.cal_latest(run_id)
        if row is None:
            print(f"{run_id:<38} {'-':>5}  no calibration row; skipped")
            continue
        if row.get("correct") is not None:
            print(f"{run_id:<38} {'-':>5}  already labeled; skipped")
            continue
        verdict = "correct" if correct else "WRONG"
        print(f"{run_id:<38} {row['confidence']:>5}  {verdict:<8} {why}")
        graded += 1
        if apply:
            from app.calibration import label_run

            ok = label_run(
                run_id,
                correct,
                source="grader-truth",
                grading_mode=mode,
                error_dimension=dim,
                correction_note=why,
            )
            print(f"{'':<38} {'':>5}  -> {'labeled' if ok else 'NO RECORD'}")

    print()
    for fp, why in UNVERIFIABLE.items():
        if fp in pending:
            print(f"left for a human: {fp}\n    {why}")

    print(f"\n{graded} gradable, {len(UNVERIFIABLE)} left for a human.")
    if not apply:
        print("dry run — nothing written. Re-run with APPLY=1.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
