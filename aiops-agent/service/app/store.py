"""Persistence layer (step 7 後半 7b-0) — durable, atomic store that replaces the
ephemeral JSONL files used by the CE harness and the investigation log.

Why this exists: the agent runs in-cluster as a single-replica Deployment whose
working dir is the pod's ephemeral filesystem. Writing `calibration.jsonl` /
`investigations.jsonl` there means every rollout/restart wipes them — and the
execution plane (step 7) *causes* restarts (rollout_undo, deploy.sh), so the very
system accumulating "earned autonomy" evidence would erase that evidence. The
Learn loop needs labeled runs to persist across incidents, so they move here.

Backend = SQLite on a PersistentVolume (see k8s manifest STORE_PATH=/data). At
replicas=1, SQLite is plenty: ACID gives atomic status transitions and a safe
`label_run` (a targeted UPDATE, not a whole-file rewrite). If the agent ever
scales past one replica, this moves to Postgres (alongside the LangGraph
checkpointer) — the in-memory dedup/checkpointer would have to move with it, so
storage and replica count are decided together.

This module owns the schema and the only SQL. calibration.py / investigations.py
keep their pydantic models + public API and call through here. Tables for
action_requests / audit (7b-1) live here too once those land.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import settings

logger = logging.getLogger("aiops_agent.store")

# Single-process serialization for writes. asyncio runs our sync store calls in
# one thread so this is mostly belt-and-suspenders against the rare threadpool
# caller; combined with WAL + busy_timeout it avoids "database is locked".
_write_lock = threading.Lock()

# What a calibration row's `correct` is a verdict *about*. Defined here because
# this module owns the schema; calibration.py re-exports them.
CULPRIT = "culprit"  # "the blame was right" — the reading the CE math assumes
INCONCLUSIVE = "inconclusive"  # "it appropriately declined to blame anyone"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    ts        TEXT NOT NULL,
    confidence REAL NOT NULL,
    correct   INTEGER,            -- NULL = unlabeled (excluded from CE)
    score     REAL,
    source    TEXT,
    summary   TEXT NOT NULL DEFAULT '',
    hypothesis TEXT NOT NULL DEFAULT '',
    suspected_version TEXT,
    services  TEXT NOT NULL DEFAULT '[]',  -- json array
    error_dimension TEXT,                  -- which part was wrong (root_cause/scope/action/other)
    correction_note TEXT,                  -- free-text human correction
    -- What question `correct` answers. "culprit" = the blame was right, which is
    -- the only reading the calibration math assumes. "inconclusive" = the run
    -- appropriately hedged on a non-incident, a different question entirely.
    -- NULL = unknown provenance; the governance gate treats it as not eligible.
    grading_mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_calibration_run_id ON calibration(run_id);

CREATE TABLE IF NOT EXISTS investigations (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    fp      TEXT NOT NULL,
    ts      TEXT NOT NULL,
    payload TEXT NOT NULL          -- full InvestigationRecord json (append-only)
);
CREATE INDEX IF NOT EXISTS idx_investigations_fp ON investigations(fp);

CREATE TABLE IF NOT EXISTS action_requests (
    request_id TEXT PRIMARY KEY,
    fp         TEXT NOT NULL,
    action     TEXT NOT NULL,
    args       TEXT NOT NULL DEFAULT '{}',   -- json
    autonomy   TEXT NOT NULL,                -- auto/propose
    status     TEXT NOT NULL,                -- lifecycle state machine
    reversible INTEGER NOT NULL DEFAULT 0,
    rollback   TEXT,                         -- json inverse contract (null = none)
    blast_radius TEXT,                       -- json (filled in 7b-2)
    runbook_id TEXT,                         -- source runbook, for precondition revalidation
    params     TEXT NOT NULL DEFAULT '{}',   -- json incident params for revalidation
    idem_key   TEXT NOT NULL DEFAULT '',     -- action|target|fp, for idempotency (7b-3)
    created_ts TEXT NOT NULL,
    expires_ts TEXT NOT NULL,
    actor      TEXT,
    outcome    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ar_status ON action_requests(status);
CREATE INDEX IF NOT EXISTS idx_ar_fp ON action_requests(fp);
CREATE INDEX IF NOT EXISTS idx_ar_idem ON action_requests(idem_key);

-- Execution outcome ledger (7b-3): feeds the breaker's window rate-limit and
-- per-target consecutive-failure count. Append-only.
CREATE TABLE IF NOT EXISTS executions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    scope_key  TEXT NOT NULL,                -- "action|target"
    action     TEXT NOT NULL,
    target     TEXT NOT NULL,
    fp         TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    success    INTEGER NOT NULL              -- 1 success, 0 failure
);
CREATE INDEX IF NOT EXISTS idx_exec_scope ON executions(scope_key);
CREATE INDEX IF NOT EXISTS idx_exec_ts ON executions(ts);

-- Circuit breaker state (7b-3): one row per tripped scope. Open until a human
-- clears it; absence of a row = closed.
CREATE TABLE IF NOT EXISTS breaker (
    scope_key TEXT PRIMARY KEY,              -- "global" or "action|target"
    open      INTEGER NOT NULL DEFAULT 0,
    opened_ts TEXT,
    reason    TEXT
);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- insert-only, never updated
    ts         TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    fp         TEXT NOT NULL DEFAULT '',
    phase      TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT 'system',
    detail     TEXT NOT NULL DEFAULT '{}'          -- json
);
CREATE INDEX IF NOT EXISTS idx_audit_request ON audit(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_fp ON audit(fp);

-- Runbook execution feedback (knowledge-loop §1 閉環三): append-only record of
-- every verify/rollback outcome. Used by runbook_health_report() to surface
-- SOP decay before it silently causes autonomous rollouts to fail.
CREATE TABLE IF NOT EXISTS runbook_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    runbook_id  TEXT NOT NULL,
    step_desc   TEXT NOT NULL DEFAULT '',
    outcome     TEXT NOT NULL,   -- ok / verify_failed / rollback / rollback_failed
    request_id  TEXT NOT NULL DEFAULT '',
    fp          TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '{}'  -- json
);
CREATE INDEX IF NOT EXISTS idx_rb_feedback_runbook ON runbook_feedback(runbook_id);
CREATE INDEX IF NOT EXISTS idx_rb_feedback_ts ON runbook_feedback(ts);

-- Actuation readiness probe history. The whole point of the preflight is that a
-- credential's health is only observable at the moment you use it; a verdict
-- that is only computed on the execution path is therefore observable only when
-- it is already too late. Persisting every probe turns "can we still act" into a
-- series with an age, so the answer exists before an incident asks for it.
CREATE TABLE IF NOT EXISTS actuation_probes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    ok         INTEGER NOT NULL,             -- 1 = reachable, nothing missing/excess
    reachable  INTEGER NOT NULL,             -- 0 = did not authenticate (the 401 case)
    in_cluster INTEGER NOT NULL DEFAULT 0,
    score      REAL,
    namespaces TEXT NOT NULL DEFAULT '',     -- comma-separated
    missing    TEXT NOT NULL DEFAULT '[]',   -- json list
    excess     TEXT NOT NULL DEFAULT '[]',   -- json list
    error      TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'loop'  -- loop / rca / preflight / rollback / api
);
CREATE INDEX IF NOT EXISTS idx_actuation_ts ON actuation_probes(ts);

-- Human verdict on one executed action: did it actually resolve the incident.
-- This is the authoritative AE-SLO numerator and it is deliberately NOT the
-- verify step's opinion. `verify` asks a query the runbook author wrote months
-- ago whether one number came back under a threshold; a person asks whether the
-- incident is over. When those two disagree, the disagreement is the finding —
-- so both are stored, and `agreed` is computed rather than assumed.
CREATE TABLE IF NOT EXISTS action_outcomes (
    request_id  TEXT PRIMARY KEY,             -- one verdict per execution; re-grading overwrites
    ts          TEXT NOT NULL,
    fp          TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',
    resolved    INTEGER NOT NULL,             -- 1 = the incident actually ended
    side_effect INTEGER NOT NULL DEFAULT 0,   -- 1 = it broke something else
    verify_said INTEGER,                      -- what the machine concluded (NULL = never ran)
    drill       INTEGER NOT NULL DEFAULT 0,
    actor       TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_action_outcomes_fp ON action_outcomes(fp);

-- ---- case memory --------------------------------------------------------
-- One row per *incident*, not per run and not per alert instance. The
-- distinction is the whole point: `fp` = sha256(alertname|service|git_version)
-- is right for dedup and for the LangGraph thread_id, and wrong as a memory
-- key, because a redeploy mints a new git_version and therefore a new fp. On
-- the day36 drill snapshot that split one recurring payment incident across six
-- fingerprints — precisely the case where last time's conclusion should have
-- been on the table.
--
-- `case_key` drops the version on purpose. See `case_key()` for the derivation.
CREATE TABLE IF NOT EXISTS cases (
    case_key    TEXT PRIMARY KEY,
    first_ts    TEXT NOT NULL,
    last_ts     TEXT NOT NULL,
    -- The signature, kept human-readable so this table can be grepped. The
    -- authority is still case_key; these are what it was derived from.
    alertname   TEXT,
    service     TEXT,
    symptom     TEXT NOT NULL DEFAULT '',
    occurrences INTEGER NOT NULL DEFAULT 1,

    -- The conclusion. NULL until something that is not the agent itself says so.
    root_cause        TEXT,
    -- human / grader / self / NULL. This column replaces the old
    -- `JOIN calibration WHERE correct=1` as the gate on what may be recalled:
    -- the question is no longer "was this run scored correct" but "who says so".
    -- `self` never qualifies, for the same reason
    -- `governance._SELF_LABEL_SOURCES` exists — saying you were right about
    -- your own work unlocks nothing.
    root_cause_source TEXT,
    confirmed_run_id  TEXT,   -- which run's conclusion was believed (replayable)
    confirmed_ts      TEXT,

    resolution  TEXT,         -- json {"action":..., "args":{...}, "outcome":...}

    -- open / resolved / recurring / false_positive. A false positive is a case
    -- worth remembering ("the last three of these were noise") but must never
    -- be recalled as a prior root cause, so it gets a status rather than a row
    -- in some other table.
    status      TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_cases_service ON cases(service);
CREATE INDEX IF NOT EXISTS idx_cases_status  ON cases(status);

-- The paths that did not work. Conclusions were always stored; the dead ends
-- only ever lived in the transcript, so every run paid for the same empty Tempo
-- query again. Negative evidence is the half that makes recall cheaper.
CREATE TABLE IF NOT EXISTS case_ruled_out (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key  TEXT NOT NULL,
    run_id    TEXT NOT NULL,
    ts        TEXT NOT NULL,
    -- hypothesis / query / action
    kind      TEXT NOT NULL,
    subject   TEXT NOT NULL,
    evidence  TEXT NOT NULL DEFAULT '',
    -- tool_result / grader / human / model. `model` is recorded but not
    -- injected by default: "I ruled that out" with no tool evidence is the same
    -- self-attestation problem one layer down, and feeding it back only makes
    -- the next run stop thinking earlier.
    disproved_by TEXT NOT NULL,
    -- Environments change. "Tempo returned nothing" usually means the 1h
    -- block_retention passed, not that the trace never existed — pinning that
    -- forever would stop the next run from looking where it should.
    still_valid  INTEGER NOT NULL DEFAULT 1,
    expires_ts   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ruled_out_case ON case_ruled_out(case_key, still_valid);
"""

# Additive migrations for columns added after initial schema creation.
# Each ALTER is wrapped in a no-op try so re-running on an up-to-date db is safe.
_MIGRATIONS = [
    "ALTER TABLE calibration ADD COLUMN error_dimension TEXT",
    # Drill executions are real executions — they mutate the cluster and they
    # belong in the ledger — but a rehearsal and an incident must not be averaged
    # into one ratio. That mistake already cost this system a latency SLO whose
    # samples were mostly one replayed alert.
    "ALTER TABLE executions ADD COLUMN drill INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE calibration ADD COLUMN correction_note TEXT",
    "ALTER TABLE calibration ADD COLUMN grading_mode TEXT",
    # Case memory (additive; existing readers keep working untouched).
    # `run_id` is per invocation. `investigations` used `fp` for this, so N runs
    # of one alert shared one key and a single verdict on the latest row joined
    # onto all of them — on the day36 snapshot that handed five recalled
    # "incidents" a human's approval, three of which concluded "false alarm".
    # eval/harness.py already worked around this with a nonce-bearing run_id;
    # this puts the fix in the schema instead of in one caller.
    "ALTER TABLE investigations ADD COLUMN run_id TEXT",
    "ALTER TABLE investigations ADD COLUMN case_key TEXT",
    "ALTER TABLE calibration ADD COLUMN case_key TEXT",
    # The alert instance this run belonged to. calibration.run_id used to *be*
    # the fingerprint, which is why a verdict could not name the run it judged.
    # Now run_id identifies the run and `fp` keeps the grouping the labelers
    # address by, so "the latest run of this alert" is a lookup someone wrote
    # down rather than a collision nobody noticed.
    "ALTER TABLE calibration ADD COLUMN fp TEXT",
    # Which run produced this proposal — so the executor's own verification
    # labels that run, not every run that ever shared the fingerprint.
    "ALTER TABLE action_requests ADD COLUMN run_id TEXT",
    # Why a human approved or (much more usefully) declined this proposal. The
    # rejection was already durable; the *reason* was not, so the next run had
    # no way to know it had been told no, and proposing the same thing again was
    # not a bug in the model, it was the only thing the record allowed.
    "ALTER TABLE action_requests ADD COLUMN decision_note TEXT",
    # Which incident the proposal belongs to, pinned when it is made. Resolving
    # it later from run_id works only once the investigation row lands, and that
    # row is written at the *end* of the run — a proposal decided on before then
    # (or belonging to a run that died) had nowhere to file the rejection.
    "ALTER TABLE action_requests ADD COLUMN case_key TEXT",
]

# Indexes on migration-added columns. Kept out of _SCHEMA because that script
# runs before the ALTERs on an older store, where the columns do not exist yet.
# Deliberately NOT unique on run_id: backfilled rows all carry their old fp, and
# the colliding ones are exactly the history we are keeping. Uniqueness is a
# property of the writer (nonce), not something to enforce retroactively.
_POST_MIGRATION_SCHEMA = (
    "CREATE INDEX IF NOT EXISTS idx_inv_run_id ON investigations(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_inv_case   ON investigations(case_key)",
    "CREATE INDEX IF NOT EXISTS idx_cal_case   ON calibration(case_key)",
    "CREATE INDEX IF NOT EXISTS idx_cal_fp     ON calibration(fp)",
)


def _resolve(path: str | Path | None) -> Path:
    return Path(path) if path is not None else Path(settings.store_path)


@contextmanager
def _connect(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open the store, ensure the schema, yield a row-dict connection, commit on
    clean exit. Schema creation is idempotent so a read on a fresh path just
    materializes an empty db."""
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_SCHEMA)
        for migration in _MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists
        for stmt in _POST_MIGRATION_SCHEMA:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column missing on a store too old to have been migrated
        yield conn
        conn.commit()
    finally:
        conn.close()


_COUNTED_TABLES = (
    "calibration",
    "investigations",
    "action_requests",
    "executions",
    "audit",
    "runbook_feedback",
    "cases",
    "case_ruled_out",
)


def describe(path: str | Path | None = None) -> dict[str, Any]:
    """Which physical store is this, and what is in it.

    Exists because the most expensive class of bug in this system was never a
    wrong query — it was a right query against the wrong file. Two deployments
    of the same code, same schema, same filename, different mount: one had 35
    calibration rows and 0 investigations, the other 15 and 15, and nothing on
    any screen said which one you were reading. An absolute path plus row counts
    costs one cheap query and makes that answerable without kubectl.
    """
    p = _resolve(path)
    out: dict[str, Any] = {"path": str(p.resolve()) if p.exists() else str(p.absolute())}
    out["exists"] = p.exists()
    tables: dict[str, int | None] = {}
    # This function must not go anywhere near `_connect`. `_connect` creates the
    # file and runs the schema + migrations on open, which is right for every
    # writer and wrong for the one caller whose entire job is asking "which file
    # am I looking at". Both halves of that bit us: describing a missing path
    # manufactured another empty store, and describing an *older* store silently
    # migrated it — which is how a read-only probe added a column to the very
    # snapshot that was being kept as evidence that the column was absent.
    if not out["exists"]:
        out["tables"] = dict.fromkeys(_COUNTED_TABLES, None)
        return out
    try:
        # mode=ro is enforced by SQLite itself, so this cannot create, migrate,
        # or write no matter what the rest of this module does later.
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            for t in _COUNTED_TABLES:
                try:
                    tables[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except sqlite3.Error:
                    tables[t] = None  # table absent on this store — say so, don't guess 0
        finally:
            conn.close()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    out["tables"] = tables
    return out


# ---- calibration ----------------------------------------------------------


def cal_insert(
    *,
    run_id: str,
    ts: str,
    confidence: float,
    summary: str,
    hypothesis: str,
    suspected_version: str | None,
    services: list[str],
    grading_mode: str | None = None,
    case_key: str | None = None,
    fp: str | None = None,
    path: str | Path | None = None,
) -> None:
    """Append a pending calibration record (correct=NULL until labeled).

    `grading_mode` records what `correct` will mean for this row — see the column
    comment in the schema. Production runs leave it None until something judges
    them; the eval harness knows its fixture's mode and says so."""
    with _write_lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO calibration "
            "(run_id, ts, confidence, summary, hypothesis, suspected_version, services, "
            "grading_mode, case_key, fp) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                ts,
                confidence,
                summary,
                hypothesis,
                suspected_version,
                json.dumps(services),
                grading_mode,
                case_key,
                fp,
            ),
        )


def cal_label(
    run_id: str,
    correct: bool,
    *,
    score: float | None,
    source: str,
    error_dimension: str | None = None,
    correction_note: str | None = None,
    grading_mode: str | None = None,
    path: str | Path | None = None,
) -> bool:
    """Atomically set the verdict on the *most recent* record for run_id. One
    UPDATE — no whole-file rewrite, no read-modify-write race. Returns True if a
    row matched.

    `grading_mode` is the question this verdict answers; whoever judges knows it,
    the run itself does not. None leaves whatever the row already had, so a
    labeler that has no opinion can't erase one."""
    with _write_lock, _connect(path) as conn:
        cur = conn.execute(
            "UPDATE calibration SET correct=?, score=?, source=?, "
            "error_dimension=?, correction_note=?, grading_mode=COALESCE(?, grading_mode) "
            "WHERE id = (SELECT id FROM calibration WHERE run_id=? "
            "            ORDER BY id DESC LIMIT 1)",
            (
                1 if correct else 0,
                score,
                source,
                error_dimension,
                correction_note,
                grading_mode,
                run_id,
            ),
        )
        return cur.rowcount > 0


def cal_resolve_run_id(ident: str, path: str | Path | None = None) -> str | None:
    """Turn whatever a labeler is holding into the run_id of one specific run.

    `ident` is a run_id when the caller has one and a fingerprint when it does
    not — the plugin's label endpoint and the executor's self-verification both
    only ever had the fingerprint. Exact run_id wins; otherwise this resolves to
    the *latest* run of that alert instance and says so by returning a different
    string than it was given. Returns None when neither matches.

    The resolution used to happen by accident, inside `cal_label`'s
    "ORDER BY id DESC LIMIT 1" — same behaviour, except nothing anywhere said a
    choice was being made, and the losing runs stayed unlabeled forever.
    """
    with _connect(path) as conn:
        row = conn.execute("SELECT 1 FROM calibration WHERE run_id=? LIMIT 1", (ident,)).fetchone()
        if row:
            return ident
        row = conn.execute(
            "SELECT run_id FROM calibration WHERE fp=? ORDER BY id DESC LIMIT 1", (ident,)
        ).fetchone()
    return row["run_id"] if row else None


def cal_latest(run_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    """The most recent calibration row for a run_id — the same row `cal_label`
    updates, so a caller can read back what it just labeled without guessing
    which of the colliding rows it hit."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM calibration WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def cal_count_by_source(
    *,
    exclude_sources: tuple[str, ...] = (),
    modes: tuple[str, ...] | None = None,
    path: str | Path | None = None,
) -> int:
    """Count labeled calibration records, optionally excluding specific sources
    and restricting to specific `grading_mode`s. Used by governance to count
    human/grader labels without remediation self-labels.

    `modes` must match whatever the caller feeds `compute_calibration` — a floor
    counted over a wider set than the curve is computed over is not a floor.
    NULL grading_mode never matches a mode filter (fail-closed on unknowns)."""
    where = ["correct IS NOT NULL"]
    params: list[Any] = []
    if exclude_sources:
        placeholders = ",".join("?" * len(exclude_sources))
        where.append(f"(source IS NULL OR source NOT IN ({placeholders}))")
        params.extend(exclude_sources)
    if modes is not None:
        placeholders = ",".join("?" * len(modes))
        where.append(f"grading_mode IN ({placeholders})")
        params.extend(modes)
    with _connect(path) as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM calibration WHERE {' AND '.join(where)}", params
        ).fetchone()[0]


def cal_load(path: str | Path | None = None) -> list[dict[str, Any]]:
    """All calibration records in insert order, as dicts (services parsed,
    correct mapped back to bool/None)."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT run_id, ts, confidence, correct, score, source, summary, "
            "hypothesis, suspected_version, services, grading_mode FROM calibration ORDER BY id"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["correct"] = None if d["correct"] is None else bool(d["correct"])
        d["services"] = json.loads(d["services"] or "[]")
        out.append(d)
    return out


# ---- investigations -------------------------------------------------------


def inv_insert(
    fp: str,
    ts: str,
    payload_json: str,
    path: str | Path | None = None,
    *,
    run_id: str | None = None,
    case_key: str | None = None,
) -> None:
    """Append one investigation. `run_id` identifies this invocation, `case_key`
    the incident it belongs to; both are optional so the legacy callers and the
    backfilled history stay readable, but the live path always passes them."""
    with _write_lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO investigations (fp, ts, payload, run_id, case_key) VALUES (?,?,?,?,?)",
            (fp, ts, payload_json, run_id, case_key),
        )


def inv_load(path: str | Path | None = None) -> list[str]:
    """Investigation payloads (json strings) in insert order = chronological, so
    callers can keep 'latest per fp wins'."""
    with _connect(path) as conn:
        rows = conn.execute("SELECT payload FROM investigations ORDER BY id").fetchall()
    return [r["payload"] for r in rows]


def inv_query_similar(
    service: str,
    alertname: str | None = None,
    limit: int = 5,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return up to `limit` past investigations for this service that were
    labeled correct=True (joined with calibration by fp=run_id), most-recent
    first. `alertname` narrows to the same alert; a chat question has no
    alertname, so leaving it out matches any past investigation of the service.
    Returns parsed payload dicts.

    Only `culprit`-graded rows qualify. On an `inconclusive` row, correct=True
    means "it rightly blamed nobody" — retrieving that as a solved past incident
    would feed the agent a non-incident as precedent, which is the opposite of
    what this context is for. Rows with no recorded mode are excluded too: this
    output goes into a prompt, so unknown provenance fails closed."""
    where = "json_extract(i.payload, '$.service') = ?"
    params: list[Any] = [service]
    if alertname:
        where += " AND json_extract(i.payload, '$.alertname') = ?"
        params.append(alertname)
    params.extend([CULPRIT, limit])
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT i.payload FROM investigations i
            JOIN calibration c ON c.run_id = i.fp
            WHERE {where} AND c.correct = 1 AND c.grading_mode = ?
            ORDER BY i.id DESC LIMIT ?
            """,
            params,
        ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except Exception:
            pass
    return out


# ---- case memory ----------------------------------------------------------

# Sources whose word is enough to write a root cause into `cases`. These are the
# `source` strings the labelers actually write: "ui" = a person in the plugin,
# "manual" = a person at the CLI, "eval"/"eval-harness" = the o11y-bench grader.
# "human"/"grader" are accepted as generic aliases for callers that have no
# narrower name.
#
# An allowlist rather than governance's denylist (`_SELF_LABEL_SOURCES`) on
# purpose: the two fail in opposite directions, and this output goes into a
# prompt. A new label source that nobody thought about should be *ignored* here,
# not trusted — the failure mode of the denylist is that a future self-attesting
# source is trusted by default, which is exactly the thing this table exists to
# prevent.
TRUSTED_ROOT_CAUSE_SOURCES = ("ui", "manual", "eval", "eval-harness", "human", "grader")

# Which ruled-out entries are worth putting back in a prompt. `model` is stored
# and excluded; see the column comment.
TRUSTED_DISPROOF_SOURCES = ("tool_result", "grader", "human")

_CASE_RECALL_STATUSES = ("resolved", "recurring")
# The status that must never come back as precedent. Expressed as a denylist
# because recall now covers cases that only know how they were fixed, and those
# sit at `open` until somebody labels the diagnosis — an allowlist of statuses
# would have silently excluded exactly the rows this was opened up for.
_CASE_NEVER_RECALL_STATUSES = ("false_positive",)


def case_key(alertname: str | None, service: str | None, symptom: str = "") -> str:
    """Signature of an *incident*: which alert, on which service, with which
    symptom — and deliberately not which version.

    `webhook.fingerprint()` includes git_version, which is correct for its jobs
    (a redeploy is a new alert instance worth re-investigating) and wrong for
    memory (a redeploy is not a new incident). Both keys therefore exist and
    neither is derived from the other.

    `symptom` is a placeholder for the chat path, which arrives with no
    alertname. It is empty everywhere today; the parameter exists so adding it
    later is not a key migration.
    """
    from .runbook import norm_alertname

    key = "|".join([norm_alertname(alertname), (service or "").strip().lower(), symptom])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def new_run_id(fp: str) -> str:
    """Identity for one invocation. `fp` stays in it so a row can still be traced
    back to its alert instance by eye."""
    return f"{fp}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


def case_upsert(
    *,
    key: str,
    ts: str,
    alertname: str | None,
    service: str | None,
    symptom: str = "",
    path: str | Path | None = None,
) -> None:
    """Record that this incident was investigated (again). Never touches
    `root_cause` or `status`: observing a case is not concluding one."""
    with _write_lock, _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO cases (case_key, first_ts, last_ts, alertname, service, symptom,
                               occurrences)
            VALUES (?,?,?,?,?,?,1)
            ON CONFLICT(case_key) DO UPDATE SET
                last_ts     = excluded.last_ts,
                occurrences = cases.occurrences + 1,
                status      = CASE WHEN cases.status = 'resolved' THEN 'recurring'
                                   ELSE cases.status END
            """,
            (key, ts, ts, alertname, service, symptom),
        )


def case_confirm(
    key: str,
    *,
    root_cause: str,
    source: str,
    run_id: str,
    ts: str,
    resolution: dict | None = None,
    path: str | Path | None = None,
) -> bool:
    """Promote one run's conclusion to the case's root cause.

    Returns False without writing when `source` is not trusted — the caller is
    not expected to know the policy, and a silent no-op here is safer than a
    self-attested root cause becoming precedent. Returns False too when the case
    row does not exist yet; confirming something never observed is a bug, not a
    state to invent.
    """
    if source not in TRUSTED_ROOT_CAUSE_SOURCES:
        logger.info("case_confirm ignored for %s: untrusted source %r", key, source)
        return False
    with _write_lock, _connect(path) as conn:
        cur = conn.execute(
            """
            UPDATE cases SET root_cause=?, root_cause_source=?, confirmed_run_id=?,
                             confirmed_ts=?, resolution=COALESCE(?, resolution),
                             status='resolved'
            WHERE case_key=?
            """,
            (
                root_cause,
                source,
                run_id,
                ts,
                json.dumps(resolution) if resolution is not None else None,
                key,
            ),
        )
        return cur.rowcount > 0


def case_set_resolution(key: str, *, resolution: dict, path: str | Path | None = None) -> bool:
    """Record what actually fixed this incident, without touching the diagnosis.

    Deliberately not part of `case_confirm`: that promotes a *conclusion* and
    demands a trusted labeler, while this records an *event* that the executor
    observed. The column has existed since the case table was written and the
    recall block has been rendering `resolved by:` from it the whole time —
    nothing ever wrote it, so that line has never once appeared.

    The newest verified fix wins. An incident that recurs and is fixed a
    different way the second time should recall the second way.
    """
    with _write_lock, _connect(path) as conn:
        cur = conn.execute(
            "UPDATE cases SET resolution=? WHERE case_key=?",
            (json.dumps(resolution), key),
        )
        return cur.rowcount > 0


def case_set_status(key: str, status: str, path: str | Path | None = None) -> bool:
    """Set the case's status directly. The caller that needs this is the
    `inconclusive` grading path: "it rightly blamed nobody" is a verdict about
    the case, not a root cause for it."""
    with _write_lock, _connect(path) as conn:
        cur = conn.execute("UPDATE cases SET status=? WHERE case_key=?", (status, key))
        return cur.rowcount > 0


def _case_row(row: sqlite3.Row) -> dict[str, Any]:
    """One `cases` row as a dict, with `resolution` decoded.

    Shared because it was not: `case_query_similar` decoded the JSON and
    `case_get` handed back the raw string, so which one you called decided
    whether `row["resolution"]["action"]` worked or raised.
    """
    d = dict(row)
    if d.get("resolution"):
        try:
            d["resolution"] = json.loads(d["resolution"])
        except Exception:
            pass
    return d


def case_get(key: str, path: str | Path | None = None) -> dict[str, Any] | None:
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM cases WHERE case_key=?", (key,)).fetchone()
    return _case_row(row) if row else None


def ruled_out_insert(
    *,
    key: str,
    run_id: str,
    ts: str,
    kind: str,
    subject: str,
    disproved_by: str,
    evidence: str = "",
    expires_ts: str | None = None,
    path: str | Path | None = None,
) -> None:
    """Record a path that did not work. Cheap and append-only: the value is in
    having many of them, so nothing here validates or dedupes."""
    with _write_lock, _connect(path) as conn:
        conn.execute(
            """
            INSERT INTO case_ruled_out
                (case_key, run_id, ts, kind, subject, evidence, disproved_by, expires_ts)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (key, run_id, ts, kind, subject, evidence, disproved_by, expires_ts),
        )


def ruled_out_invalidate(key: str, kind: str | None = None, path: str | Path | None = None) -> int:
    """Stop recalling these dead ends — the environment they were true in is
    gone. Returns how many rows were retired."""
    sql = "UPDATE case_ruled_out SET still_valid=0 WHERE case_key=? AND still_valid=1"
    params: list[Any] = [key]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    with _write_lock, _connect(path) as conn:
        return conn.execute(sql, params).rowcount


def case_query_similar(
    service: str,
    alertname: str | None = None,
    limit: int = 5,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Past incidents on this service worth putting in front of the model.

    Structured filter first, ranking second, and **one row per case** — the
    predecessor (`inv_query_similar`) joined investigations to calibration on
    `fp`, so a single verdict fanned out over every run that shared the
    fingerprint and the top-5 came back as five near-copies of one incident,
    several of which had concluded there was no incident.

    No embeddings at this size. Structured matching can be explained to whoever
    is on call ("because the last three of these on payment-service"); a
    similarity score cannot.

    Recall requires a root cause from a trusted source and a status that means
    something was actually wrong. `false_positive` cases are kept but never
    returned here: they are useful context about the alert, not precedent about
    a cause.
    """
    # A case is worth recalling if it knows *something*: why it happened, or
    # what made it stop. Requiring a root cause meant an incident somebody had
    # actually fixed stayed invisible until a second person got round to
    # labelling the diagnosis — and "the last three of these were fixed by
    # rolling back" is the more actionable half of the two.
    where = "service = ? AND (root_cause IS NOT NULL OR resolution IS NOT NULL)"
    params: list[Any] = [service]
    if alertname:
        where += " AND alertname = ?"
        params.append(alertname)
    # An incident nobody has seen in months is history, not a prior. The row
    # stays; it just stops being offered as what is probably happening now.
    where += " AND last_ts >= ?"
    params.append(
        (datetime.now(UTC) - timedelta(days=settings.case_max_age_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    )
    where += (
        f" AND (root_cause IS NULL OR root_cause_source IN "
        f"({','.join('?' * len(TRUSTED_ROOT_CAUSE_SOURCES))}))"
        f" AND status NOT IN ({','.join('?' * len(_CASE_NEVER_RECALL_STATUSES))})"
    )
    params.extend(TRUSTED_ROOT_CAUSE_SOURCES)
    params.extend(_CASE_NEVER_RECALL_STATUSES)
    params.append(limit)
    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM cases WHERE {where} ORDER BY occurrences DESC, last_ts DESC LIMIT ?",
            params,
        ).fetchall()
    return [_case_row(r) for r in rows]


def case_forget(key: str, path: str | Path | None = None) -> dict[str, int]:
    """Retract what this case claims to know, without deleting the case.

    Three things go, because they are all things this case tells the next run:
    the root cause stops being offered as a prior, the recorded fix stops being
    offered as what works, and every dead end on it is retired. The fix is
    included even though it was observed rather than claimed — "rolling back
    cleared it" is exactly the kind of statement a rebuilt environment
    invalidates, and this is the button for saying the ground moved. Somebody is saying
    the ground under this record has moved — an environment rebuilt, a policy
    changed, a diagnosis that turned out to be wrong later — and the honest
    response is to stop asserting all of it, not to argue about which half is
    still true.

    `occurrences`, `first_ts` and the rows themselves survive. That the incident
    happened is not in dispute.
    """
    with _write_lock, _connect(path) as conn:
        cleared = conn.execute(
            """
            UPDATE cases SET root_cause=NULL, root_cause_source=NULL,
                             confirmed_run_id=NULL, confirmed_ts=NULL,
                             resolution=NULL, status='open'
            WHERE case_key=?
            """,
            (key,),
        ).rowcount
        retired = conn.execute(
            "UPDATE case_ruled_out SET still_valid=0 WHERE case_key=? AND still_valid=1",
            (key,),
        ).rowcount
    return {"cases": cleared, "dead_ends": retired}


def case_key_for_run(run_id: str, path: str | Path | None = None) -> str | None:
    """Which incident a given run was about.

    Needed by the write paths that only ever hold a run id and are nowhere near
    an open case scope — the human deciding on a proposal, minutes or hours
    after the investigation ended. Investigations first because that row is
    written at the end of every run; calibration only exists once someone (or
    the executor) has had an opinion about it.
    """
    if not run_id:
        return None
    with _connect(path) as conn:
        for sql in (
            "SELECT case_key FROM investigations WHERE run_id=? AND case_key IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
            "SELECT case_key FROM calibration WHERE run_id=? AND case_key IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
        ):
            row = conn.execute(sql, (run_id,)).fetchone()
            if row and row["case_key"]:
                return row["case_key"]
    return None


def case_ruled_out_for(
    case_keys: list[str],
    limit: int = 10,
    now_ts: str | None = None,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Still-valid dead ends for these cases, most recent first.

    Ranked separately from `case_query_similar` on purpose: a root cause is
    interesting because it keeps happening, a dead end because it was disproved
    recently — the environment it was disproved in is more likely to still be
    the current one.
    """
    if not case_keys:
        return []
    now_dt = datetime.now(UTC)
    now = now_ts or now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Everything here goes stale, including the entries with no expiry set.
    # `expires_ts` was for the dead ends known to be short-lived when they were
    # written (a trace outside the retention window); the ones a person wrote —
    # "we don't roll back during business hours" — carry no expiry at all and
    # would otherwise be recalled forever, long after whoever said it moved on.
    cutoff = (now_dt - timedelta(days=settings.case_dead_end_max_age_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    ph = ",".join("?" * len(case_keys))
    src = ",".join("?" * len(TRUSTED_DISPROOF_SOURCES))
    params: list[Any] = [*case_keys, *TRUSTED_DISPROOF_SOURCES, now, cutoff, limit]
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM case_ruled_out
            WHERE case_key IN ({ph}) AND still_valid=1 AND disproved_by IN ({src})
              AND (expires_ts IS NULL OR expires_ts > ?)
              AND ts >= ?
            ORDER BY id DESC LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def ruled_out_find(
    case_key: str,
    *,
    kind: str,
    subject: str,
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """The newest live dead end matching one exact subject, or None.

    Shares `case_ruled_out_for`'s freshness rules by calling it: same expiry,
    same age cutoff, same trusted sources. Filtering here on `subject` in SQL
    instead would have been one query and two sets of rules.
    """
    for row in case_ruled_out_for([case_key], limit=200, path=path):
        if row["kind"] == kind and row["subject"] == subject:
            return row
    return None


def backfill_cases(path: str | Path | None = None) -> dict[str, int]:
    """One-time: give existing investigations a run_id and a case_key, and
    materialize the `cases` rows they imply.

    Two deliberate omissions.

    `run_id` is backfilled to `fp`, which reproduces the old collisions rather
    than inventing identities that were never recorded. New rows carry a nonce;
    history stays honest about not having one.

    **No root cause is backfilled.** The old `correct=1` labels cannot be
    attributed to a specific run — that ambiguity is the bug this table exists
    to fix — so promoting them would freeze the wrong prior into the new schema.
    The case library starts empty and earns its rows.
    """
    counts = {"run_id": 0, "case_key": 0, "cases": 0}
    with _write_lock, _connect(path) as conn:
        counts["run_id"] = conn.execute(
            "UPDATE investigations SET run_id = fp WHERE run_id IS NULL"
        ).rowcount
        rows = conn.execute(
            "SELECT id, fp, ts, payload FROM investigations WHERE case_key IS NULL ORDER BY id"
        ).fetchall()
        seen: dict[str, tuple[str, str, str | None, str | None]] = {}
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except Exception:
                continue  # malformed row: leave case_key NULL rather than guess
            alertname = payload.get("alertname")
            service = payload.get("service")
            key = case_key(alertname, service)
            conn.execute("UPDATE investigations SET case_key=? WHERE id=?", (key, r["id"]))
            counts["case_key"] += 1
            ts = r["ts"] or payload.get("ts") or ""
            if key in seen:
                first, _last, an, sv = seen[key]
                seen[key] = (min(first, ts) if first else ts, ts, an, sv)
            else:
                seen[key] = (ts, ts, alertname, service)
        for key, (first, last, alertname, service) in seen.items():
            occurrences = conn.execute(
                "SELECT COUNT(*) FROM investigations WHERE case_key=?", (key,)
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO cases (case_key, first_ts, last_ts, alertname, service, occurrences)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(case_key) DO UPDATE SET
                    last_ts     = MAX(cases.last_ts, excluded.last_ts),
                    occurrences = excluded.occurrences
                """,
                (key, first, last, alertname, service, occurrences),
            )
            counts["cases"] += 1
    return counts


# ---- action_requests (lifecycle state machine; 7b-1) ----------------------

_AR_COLS = (
    "request_id",
    "fp",
    "action",
    "args",
    "autonomy",
    "status",
    "reversible",
    "rollback",
    "blast_radius",
    "runbook_id",
    "params",
    "idem_key",
    "run_id",
    "case_key",
    "created_ts",
    "expires_ts",
    "actor",
    "outcome",
)

# Statuses meaning the action actually ran or is running — the set idempotency
# treats as "already acted on" (REFUSED/ABORTED/etc. don't count: nothing ran).
_RAN_STATUSES = (
    "executing",
    "succeeded",
    "failed",
    "verify_failed",
    "rolling_back",
    "rolled_back",
    "rollback_failed",
)


def _ar_row_to_dict(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["reversible"] = bool(d["reversible"])
    d["args"] = json.loads(d["args"] or "{}")
    d["rollback"] = json.loads(d["rollback"]) if d["rollback"] else None
    d["blast_radius"] = json.loads(d["blast_radius"]) if d["blast_radius"] else None
    d["params"] = json.loads(d["params"] or "{}")
    return d


def ar_insert(rec: dict, path: str | Path | None = None) -> None:
    row = {
        **rec,
        "reversible": 1 if rec.get("reversible") else 0,
        "args": json.dumps(rec.get("args") or {}),
        "rollback": json.dumps(rec["rollback"]) if rec.get("rollback") else None,
        "blast_radius": json.dumps(rec["blast_radius"]) if rec.get("blast_radius") else None,
        "params": json.dumps(rec.get("params") or {}),
    }
    with _write_lock, _connect(path) as conn:
        conn.execute(
            f"INSERT INTO action_requests ({','.join(_AR_COLS)}) "
            f"VALUES ({','.join('?' for _ in _AR_COLS)})",
            tuple(row.get(c) for c in _AR_COLS),
        )


def ar_update(
    request_id: str,
    *,
    blast_radius: dict | None = None,
    outcome: str | None = None,
    path: str | Path | None = None,
) -> None:
    """Attach computed fields (e.g. blast radius) without changing status — the
    dry-run gate records its result while the request stays in `executing`."""
    sets, params = [], []
    if blast_radius is not None:
        sets.append("blast_radius=?")
        params.append(json.dumps(blast_radius))
    if outcome is not None:
        sets.append("outcome=?")
        params.append(outcome)
    if not sets:
        return
    params.append(request_id)
    with _write_lock, _connect(path) as conn:
        conn.execute(f"UPDATE action_requests SET {','.join(sets)} WHERE request_id=?", params)


def ar_find_ran(
    idem_key: str,
    exclude_request_id: str,
    path: str | Path | None = None,
    window_seconds: int | None = None,
) -> str | None:
    """Idempotency probe: the request_id of *another* request with the same
    idem_key that already ran or is running **within the window**, else None.
    Empty idem_key never matches (no target to dedup on).

    The window is the whole point. `idem_key` is `action|target|fp`, and `fp` is
    deliberately stable across recurrences (it is also the investigation's
    thread_id), so an unbounded lookup does not mean "don't act twice on this
    incident" — it means "never act on this kind of incident again, for the life
    of the database". A drill hit exactly that: a rollback was refused as a
    duplicate of an execution eight days earlier, on an alert that had long since
    resolved and re-fired.

    What idempotency is actually defending against is an alert storm double-acting
    on one target, and that happens in minutes. Anything older than the window is
    a different occurrence of the same recurring problem, which is precisely what
    the runbook exists to fix again.
    """
    if not idem_key:
        return None
    if window_seconds is None:
        window_seconds = settings.idempotency_window_seconds
    placeholders = ",".join("?" for _ in _RAN_STATUSES)
    since = (datetime.now(UTC) - timedelta(seconds=window_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect(path) as conn:
        r = conn.execute(
            f"SELECT request_id FROM action_requests "
            f"WHERE idem_key=? AND request_id<>? AND created_ts>=? "
            f"AND status IN ({placeholders}) "
            f"ORDER BY created_ts LIMIT 1",
            (idem_key, exclude_request_id, since, *_RAN_STATUSES),
        ).fetchone()
    return r["request_id"] if r else None


# ---- circuit breaker + execution ledger (7b-3) ----------------------------


def exec_record(
    *,
    ts: str,
    scope_key: str,
    action: str,
    target: str,
    fp: str,
    request_id: str,
    success: bool,
    drill: bool = False,
    path: str | Path | None = None,
) -> None:
    with _write_lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO executions "
            "(ts, scope_key, action, target, fp, request_id, success, drill) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, scope_key, action, target, fp, request_id, 1 if success else 0, int(drill)),
        )


def exec_window_count(since_ts: str, path: str | Path | None = None) -> int:
    """How many executions happened at/after since_ts (global rate-limit input).
    ts is ISO-8601 Z, lexically sortable, so a string compare is correct."""
    with _connect(path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM executions WHERE ts >= ?", (since_ts,)
        ).fetchone()[0]


def exec_consecutive_failures(scope_key: str, path: str | Path | None = None) -> int:
    """Failures since the last success (or ever) for a scope — the trip input."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT success FROM executions WHERE scope_key=? ORDER BY id DESC", (scope_key,)
        ).fetchall()
    n = 0
    for r in rows:
        if r["success"]:
            break
        n += 1
    return n


def breaker_get(scope_key: str, path: str | Path | None = None) -> dict[str, Any] | None:
    with _connect(path) as conn:
        r = conn.execute("SELECT * FROM breaker WHERE scope_key=?", (scope_key,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["open"] = bool(d["open"])
    return d


def breaker_set_open(
    scope_key: str, opened_ts: str, reason: str, path: str | Path | None = None
) -> None:
    with _write_lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO breaker (scope_key, open, opened_ts, reason) VALUES (?,1,?,?) "
            "ON CONFLICT(scope_key) DO UPDATE SET open=1, opened_ts=excluded.opened_ts, "
            "reason=excluded.reason",
            (scope_key, opened_ts, reason),
        )


def breaker_clear(scope_key: str | None = None, path: str | Path | None = None) -> int:
    """Reset a tripped scope (or all if scope_key is None). Returns rows cleared."""
    with _write_lock, _connect(path) as conn:
        if scope_key:
            cur = conn.execute("DELETE FROM breaker WHERE scope_key=?", (scope_key,))
        else:
            cur = conn.execute("DELETE FROM breaker")
        return cur.rowcount


def breaker_all(path: str | Path | None = None) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM breaker ORDER BY scope_key").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["open"] = bool(d["open"])
        out.append(d)
    return out


def ar_get(request_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    with _connect(path) as conn:
        r = conn.execute(
            "SELECT * FROM action_requests WHERE request_id=?", (request_id,)
        ).fetchone()
    return _ar_row_to_dict(r) if r else None


def ar_list(
    *, status: str | None = None, limit: int = 50, path: str | Path | None = None
) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM action_requests WHERE status=? ORDER BY created_ts DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM action_requests ORDER BY created_ts DESC LIMIT ?", (limit,)
            ).fetchall()
    return [_ar_row_to_dict(r) for r in rows]


def ar_transition(
    request_id: str,
    expect_status: str,
    new_status: str,
    *,
    actor: str | None = None,
    outcome: str | None = None,
    decision_note: str | None = None,
    blast_radius: dict | None = None,
    path: str | Path | None = None,
) -> bool:
    """Atomic compare-and-set on status: only flips when the row is *currently* in
    `expect_status`. This is what makes double-approve / approve-racing-AUTO safe —
    the second writer sees the status already moved and its UPDATE matches 0 rows."""
    sets = ["status=?"]
    params: list[Any] = [new_status]
    if actor is not None:
        sets.append("actor=?")
        params.append(actor)
    if outcome is not None:
        sets.append("outcome=?")
        params.append(outcome)
    if decision_note is not None:
        sets.append("decision_note=?")
        params.append(decision_note)
    if blast_radius is not None:
        sets.append("blast_radius=?")
        params.append(json.dumps(blast_radius))
    params += [request_id, expect_status]
    with _write_lock, _connect(path) as conn:
        cur = conn.execute(
            f"UPDATE action_requests SET {','.join(sets)} WHERE request_id=? AND status=?",
            params,
        )
        return cur.rowcount > 0


# ---- audit (insert-only) --------------------------------------------------


def audit_insert(rec: dict, path: str | Path | None = None) -> None:
    with _write_lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO audit (ts, request_id, fp, phase, verdict, actor, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                rec["ts"],
                rec.get("request_id", ""),
                rec.get("fp", ""),
                rec["phase"],
                rec["verdict"],
                rec.get("actor", "system"),
                json.dumps(rec.get("detail") or {}),
            ),
        )


def audit_list(
    *,
    request_id: str | None = None,
    fp: str | None = None,
    limit: int = 200,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    where, params = [], []
    if request_id is not None:
        where.append("request_id=?")
        params.append(request_id)
    if fp is not None:
        where.append("fp=?")
        params.append(fp)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT ts, request_id, fp, phase, verdict, actor, detail "
            f"FROM audit{clause} ORDER BY id LIMIT ?",
            params,
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"] or "{}")
        out.append(d)
    return out


# ---- one-time legacy JSONL migration --------------------------------------


def _import_jsonl(p: Path) -> list[str]:
    if not p.exists():
        return []
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def migrate_legacy_jsonl(path: str | Path | None = None) -> dict[str, int]:
    """Best-effort one-time import of the old ephemeral JSONL files into the db.
    Only runs per-table when that table is empty, so it's idempotent and never
    duplicates. Returns {table: rows_imported}."""
    imported = {"calibration": 0, "investigations": 0}
    try:
        with _connect(path) as conn:
            cal_empty = conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0] == 0
            inv_empty = conn.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 0

            if cal_empty:
                for line in _import_jsonl(Path(settings.calibration_log_path)):
                    try:
                        d = json.loads(line)
                        conn.execute(
                            "INSERT INTO calibration (run_id, ts, confidence, correct, "
                            "score, source, summary, hypothesis, suspected_version, services) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (
                                d.get("run_id", ""),
                                d.get("ts", ""),
                                float(d.get("confidence", 0.0)),
                                None if d.get("correct") is None else (1 if d["correct"] else 0),
                                d.get("score"),
                                d.get("source"),
                                d.get("summary", ""),
                                d.get("hypothesis", ""),
                                d.get("suspected_version"),
                                json.dumps(d.get("services", [])),
                            ),
                        )
                        imported["calibration"] += 1
                    except Exception as e:
                        logger.warning("skip bad legacy calibration line: %s", e)

            if inv_empty:
                for line in _import_jsonl(Path(settings.investigations_log_path)):
                    try:
                        d = json.loads(line)
                        conn.execute(
                            "INSERT INTO investigations (fp, ts, payload) VALUES (?,?,?)",
                            (d.get("fp", ""), d.get("ts", ""), line),
                        )
                        imported["investigations"] += 1
                    except Exception as e:
                        logger.warning("skip bad legacy investigation line: %s", e)
            conn.commit()
    except Exception as e:  # migration must never block startup
        logger.warning("legacy JSONL migration skipped: %s", e)
    if imported["calibration"] or imported["investigations"]:
        logger.info("migrated legacy JSONL into store: %s", imported)
    return imported


def init(path: str | Path | None = None) -> None:
    """Materialize the schema + run the one-time legacy import. Called at startup."""
    with _connect(path):
        pass


# ---- runbook feedback (knowledge-loop §1 閉環三) ---------------------------


def rb_feedback_insert(
    *,
    runbook_id: str,
    outcome: str,
    step_desc: str = "",
    request_id: str = "",
    fp: str = "",
    detail: dict | None = None,
    path: str | Path | None = None,
) -> None:
    """Append one execution outcome for a runbook step (ok / verify_failed /
    rollback / rollback_failed). Append-only; never updated."""
    from datetime import UTC, datetime

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _write_lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO runbook_feedback "
            "(ts, runbook_id, step_desc, outcome, request_id, fp, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, runbook_id, step_desc, outcome, request_id, fp, json.dumps(detail or {})),
        )


# What a runbook's execution record says about the runbook. One definition,
# because the report a person reads and the gate that withholds autonomy have to
# agree — two thresholds drifting apart is how a dashboard ends up saying a
# procedure is fine while the governance plane treats it as suspended.
RB_HEALTHY = "healthy"
RB_NEEDS_REVIEW = "needs_review"
RB_SUSPENDED = "suspended"
RB_NO_RECORD = "insufficient_data"


def _rb_verdict(counts: dict[str, int]) -> dict[str, Any]:
    """Turn one runbook's outcome counts into a status, a rate and a sentence.

    `rollback_failed` is its own status at any sample size: the number that
    matters there is not a rate, it is that the escape hatch was tried and did
    not work. A verify-failure rate needs enough executions to be a claim —
    one failed run out of one is 100% and means nothing.
    """
    total = counts.get("total", 0)
    vf = counts.get("verify_failed", 0)
    rbf = counts.get("rollback_failed", 0)
    rate = vf / total if total else 0.0
    out = {**counts, "verify_failed_rate": round(rate, 3)}
    if rbf:
        return {
            **out,
            "status": RB_SUSPENDED,
            "note": f"rollback_failed x{rbf} — this runbook's undo did not work",
        }
    if total < settings.runbook_health_min_runs:
        return {
            **out,
            "status": RB_NO_RECORD,
            "note": f"{total} recorded execution(s) — too few to rate",
        }
    if rate > settings.runbook_health_verify_failed_rate:
        return {
            **out,
            "status": RB_NEEDS_REVIEW,
            "note": f"verify_failed {rate:.0%} ({vf}/{total}) — the symptom survived the fix",
        }
    return {**out, "status": RB_HEALTHY, "note": f"{counts.get('ok', 0)}/{total} verified clean"}


def rb_health(
    runbook_id: str, days: int | None = None, path: str | Path | None = None
) -> dict[str, Any]:
    """One runbook's track record over the window. Always returns a verdict —
    a runbook nobody has ever executed is `insufficient_data`, not an error."""
    from datetime import UTC, datetime, timedelta

    window = days if days is not None else settings.runbook_health_window_days
    cutoff = (datetime.now(UTC) - timedelta(days=window)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect(path) as conn:
        row = conn.execute(
            f"""
            SELECT {_RB_COUNT_COLS}
            FROM runbook_feedback WHERE runbook_id = ? AND ts >= ?
            """,
            (runbook_id, cutoff),
        ).fetchone()
    counts = {
        k: (row[k] or 0) for k in ("total", "verify_failed", "rollback_failed", "rollback", "ok")
    }
    return {"runbook_id": runbook_id, **_rb_verdict(counts)}


_RB_COUNT_COLS = """
    COUNT(*) AS total,
    SUM(CASE WHEN outcome = 'verify_failed' THEN 1 ELSE 0 END) AS verify_failed,
    SUM(CASE WHEN outcome = 'rollback_failed' THEN 1 ELSE 0 END) AS rollback_failed,
    SUM(CASE WHEN outcome = 'rollback' THEN 1 ELSE 0 END) AS rollback,
    SUM(CASE WHEN outcome = 'ok' THEN 1 ELSE 0 END) AS ok
"""


def rb_feedback_health_report(
    days: int = 30, path: str | Path | None = None
) -> list[dict[str, Any]]:
    """The runbooks whose record says something is wrong, worst first.

    Shares `_rb_verdict` with the gate that acts on this, so the page a person
    reads and the decision the agent makes cannot disagree. One behaviour
    changed when they were merged: a runbook with two executions and one failure
    used to be reported at "50%", and is now held back as too few to rate.
    """
    from datetime import UTC, datetime, timedelta

    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT runbook_id, {_RB_COUNT_COLS}
            FROM runbook_feedback
            WHERE ts >= ?
            GROUP BY runbook_id
            """,
            (cutoff,),
        ).fetchall()

    results = []
    for r in rows:
        counts = {
            k: (r[k] or 0) for k in ("total", "verify_failed", "rollback_failed", "rollback", "ok")
        }
        v = _rb_verdict(counts)
        if v["status"] not in (RB_SUSPENDED, RB_NEEDS_REVIEW):
            continue
        results.append(
            {
                "runbook_id": r["runbook_id"],
                "total_executions": counts["total"],
                "verify_failed": counts["verify_failed"],
                "verify_failed_rate": v["verify_failed_rate"],
                "rollback_failed": counts["rollback_failed"],
                "rollback": counts["rollback"],
                "ok": counts["ok"],
                "status": v["status"],
                "decay_signals": [v["note"]],
            }
        )

    # rollback_failed first (critical), then by verify_failed_rate desc
    results.sort(key=lambda x: (-x["rollback_failed"], -x["verify_failed_rate"]))
    return results


# ---- actuation readiness probe history ------------------------------------


def actuation_probe_insert(
    *,
    ok: bool,
    reachable: bool,
    in_cluster: bool,
    score: float | None,
    namespaces: list[str],
    missing: list[str],
    excess: list[str],
    error: str = "",
    source: str = "loop",
    path: str | Path | None = None,
) -> None:
    """Append one readiness probe. Best-effort by contract: the caller must never
    lose a probe result to a storage failure, so this swallows nothing but is
    always called inside a try by the prober."""
    from datetime import UTC, datetime

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _write_lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO actuation_probes "
            "(ts, ok, reachable, in_cluster, score, namespaces, missing, excess, error, source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                ts,
                int(ok),
                int(reachable),
                int(in_cluster),
                score,
                ",".join(namespaces),
                json.dumps(missing),
                json.dumps(excess),
                error,
                source,
            ),
        )


def actuation_probe_recent(limit: int = 50, path: str | Path | None = None) -> list[dict]:
    """Most recent probes, newest first."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT * FROM actuation_probes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---- action outcome grading (AE-SLO numerator) -----------------------------


def action_outcome_put(
    *,
    request_id: str,
    resolved: bool,
    actor: str,
    fp: str = "",
    action: str = "",
    side_effect: bool = False,
    verify_said: bool | None = None,
    drill: bool = False,
    note: str = "",
    path: str | Path | None = None,
) -> None:
    """Record (or correct) the human verdict on one executed action."""
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _write_lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO action_outcomes "
            "(request_id, ts, fp, action, resolved, side_effect, verify_said, drill, actor, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(request_id) DO UPDATE SET "
            "ts=excluded.ts, resolved=excluded.resolved, side_effect=excluded.side_effect, "
            "verify_said=excluded.verify_said, actor=excluded.actor, note=excluded.note",
            (
                request_id,
                ts,
                fp,
                action,
                int(resolved),
                int(side_effect),
                None if verify_said is None else int(verify_said),
                int(drill),
                actor,
                note,
            ),
        )


def action_outcomes(path: str | Path | None = None) -> list[dict]:
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM action_outcomes ORDER BY ts DESC").fetchall()
    return [dict(r) for r in rows]


def ae_slo(min_n: int = 5, path: str | Path | None = None) -> dict:
    """Action Effectiveness: of the actions we executed, how many actually ended
    the incident — graded by a person, counted separately for drills.

    Two rules this function exists to enforce, because both were broken by hand
    before:

    1. **No percentage under `min_n`.** `0/1` printed as `0.0%` reads like a
       measured failure rate; it is one anecdote. Below the floor the ratio is
       omitted entirely rather than rendered small.
    2. **Drills never join the incident ratio.** They are reported beside it, so
       a rehearsal cannot flatter (or ruin) the number that describes production.
    """
    rows = action_outcomes(path)

    def summarize(subset: list[dict]) -> dict:
        n = len(subset)
        effective = sum(1 for r in subset if r["resolved"] and not r["side_effect"])
        out = {"n": n, "effective": effective, "raw": f"{effective}/{n}"}
        out["rate"] = round(effective / n, 3) if n >= min_n else None
        if n < min_n:
            out["note"] = f"n={n} below the reporting floor of {min_n}; ratio withheld"
        return out

    graded = [r for r in rows if r["verify_said"] is not None]
    disagreements = [r for r in graded if bool(r["verify_said"]) != bool(r["resolved"])]
    return {
        "incidents": summarize([r for r in rows if not r["drill"]]),
        "drills": summarize([r for r in rows if r["drill"]]),
        "verify_agreement": {
            "graded": len(graded),
            "disagreed": len(disagreements),
            "note": (
                "no executed action has been graded yet, so the machine check has "
                "nothing to be compared against"
                if not graded
                else "verify and the on-call reached the same verdict every time"
                if not disagreements
                else "the machine check and the person disagreed at least once — "
                "read those rows before trusting either signal alone"
            ),
        },
    }
