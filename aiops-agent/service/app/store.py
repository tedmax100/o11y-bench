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

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import settings

logger = logging.getLogger("aiops_agent.store")

# Single-process serialization for writes. asyncio runs our sync store calls in
# one thread so this is mostly belt-and-suspenders against the rare threadpool
# caller; combined with WAL + busy_timeout it avoids "database is locked".
_write_lock = threading.Lock()

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
    correction_note TEXT                   -- free-text human correction
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
"""

# Additive migrations for columns added after initial schema creation.
# Each ALTER is wrapped in a no-op try so re-running on an up-to-date db is safe.
_MIGRATIONS = [
    "ALTER TABLE calibration ADD COLUMN error_dimension TEXT",
    "ALTER TABLE calibration ADD COLUMN correction_note TEXT",
]


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
        yield conn
        conn.commit()
    finally:
        conn.close()


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
    path: str | Path | None = None,
) -> None:
    """Append a pending calibration record (correct=NULL until labeled)."""
    with _write_lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO calibration "
            "(run_id, ts, confidence, summary, hypothesis, suspected_version, services) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, ts, confidence, summary, hypothesis, suspected_version, json.dumps(services)),
        )


def cal_label(
    run_id: str,
    correct: bool,
    *,
    score: float | None,
    source: str,
    error_dimension: str | None = None,
    correction_note: str | None = None,
    path: str | Path | None = None,
) -> bool:
    """Atomically set the verdict on the *most recent* record for run_id. One
    UPDATE — no whole-file rewrite, no read-modify-write race. Returns True if a
    row matched."""
    with _write_lock, _connect(path) as conn:
        cur = conn.execute(
            "UPDATE calibration SET correct=?, score=?, source=?, "
            "error_dimension=?, correction_note=? "
            "WHERE id = (SELECT id FROM calibration WHERE run_id=? "
            "            ORDER BY id DESC LIMIT 1)",
            (1 if correct else 0, score, source, error_dimension, correction_note, run_id),
        )
        return cur.rowcount > 0


def cal_count_by_source(
    *, exclude_sources: tuple[str, ...] = (), path: str | Path | None = None
) -> int:
    """Count labeled calibration records, optionally excluding specific sources.
    Used by governance to count human/grader labels without remediation self-labels."""
    placeholders = ",".join("?" * len(exclude_sources))
    with _connect(path) as conn:
        if exclude_sources:
            return conn.execute(
                f"SELECT COUNT(*) FROM calibration WHERE correct IS NOT NULL "
                f"AND (source IS NULL OR source NOT IN ({placeholders}))",
                list(exclude_sources),
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM calibration WHERE correct IS NOT NULL"
        ).fetchone()[0]


def cal_load(path: str | Path | None = None) -> list[dict[str, Any]]:
    """All calibration records in insert order, as dicts (services parsed,
    correct mapped back to bool/None)."""
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT run_id, ts, confidence, correct, score, source, summary, "
            "hypothesis, suspected_version, services FROM calibration ORDER BY id"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["correct"] = None if d["correct"] is None else bool(d["correct"])
        d["services"] = json.loads(d["services"] or "[]")
        out.append(d)
    return out


# ---- investigations -------------------------------------------------------


def inv_insert(fp: str, ts: str, payload_json: str, path: str | Path | None = None) -> None:
    with _write_lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO investigations (fp, ts, payload) VALUES (?,?,?)",
            (fp, ts, payload_json),
        )


def inv_load(path: str | Path | None = None) -> list[str]:
    """Investigation payloads (json strings) in insert order = chronological, so
    callers can keep 'latest per fp wins'."""
    with _connect(path) as conn:
        rows = conn.execute("SELECT payload FROM investigations ORDER BY id").fetchall()
    return [r["payload"] for r in rows]


def inv_query_similar(
    service: str,
    alertname: str,
    limit: int = 5,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return up to `limit` past investigations for the same service+alertname
    that were labeled correct=True (joined with calibration by fp=run_id).
    Most-recent first. Returns parsed payload dicts."""
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT i.payload FROM investigations i
            JOIN calibration c ON c.run_id = i.fp
            WHERE json_extract(i.payload, '$.service') = ?
              AND json_extract(i.payload, '$.alertname') = ?
              AND c.correct = 1
            ORDER BY i.id DESC LIMIT ?
            """,
            (service, alertname, limit),
        ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except Exception:
            pass
    return out


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
    idem_key: str, exclude_request_id: str, path: str | Path | None = None
) -> str | None:
    """Idempotency probe: the request_id of *another* request with the same
    idem_key that already ran or is running, else None. Empty idem_key never
    matches (no target to dedup on)."""
    if not idem_key:
        return None
    placeholders = ",".join("?" for _ in _RAN_STATUSES)
    with _connect(path) as conn:
        r = conn.execute(
            f"SELECT request_id FROM action_requests "
            f"WHERE idem_key=? AND request_id<>? AND status IN ({placeholders}) "
            f"ORDER BY created_ts LIMIT 1",
            (idem_key, exclude_request_id, *_RAN_STATUSES),
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
    path: str | Path | None = None,
) -> None:
    with _write_lock, _connect(path) as conn:
        conn.execute(
            "INSERT INTO executions (ts, scope_key, action, target, fp, request_id, success) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, scope_key, action, target, fp, request_id, 1 if success else 0),
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


def rb_feedback_health_report(
    days: int = 30, path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Return a list of runbooks that show decay signals over the past `days` days.

    Decay signals (per design doc §1 閉環三):
    - verify_failed rate > 30% in the window
    - any rollback_failed (immediate flag)
    Returns one dict per runbook that tripped at least one signal, sorted by
    severity (rollback_failed first, then by verify_failed rate desc)."""
    from datetime import UTC, datetime, timedelta

    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect(path) as conn:
        rows = conn.execute(
            """
            SELECT
                runbook_id,
                COUNT(*) AS total,
                SUM(CASE WHEN outcome = 'verify_failed' THEN 1 ELSE 0 END) AS verify_failed,
                SUM(CASE WHEN outcome = 'rollback_failed' THEN 1 ELSE 0 END) AS rollback_failed,
                SUM(CASE WHEN outcome = 'rollback' THEN 1 ELSE 0 END) AS rollback,
                SUM(CASE WHEN outcome = 'ok' THEN 1 ELSE 0 END) AS ok
            FROM runbook_feedback
            WHERE ts >= ?
            GROUP BY runbook_id
            """,
            (cutoff,),
        ).fetchall()

    results = []
    for r in rows:
        total = r["total"]
        vf_rate = r["verify_failed"] / total if total else 0.0
        has_rb_failed = r["rollback_failed"] > 0
        # Only surface runbooks with a decay signal
        if vf_rate > 0.30 or has_rb_failed:
            signals = []
            if has_rb_failed:
                n = r["rollback_failed"]
                signals.append(f"rollback_failed x{n} — suspend auto-execution")
            if vf_rate > 0.30:
                vf, tot = r["verify_failed"], total
                signals.append(f"verify_failed {vf_rate:.0%} ({vf}/{tot}) — needs-review")
            results.append(
                {
                    "runbook_id": r["runbook_id"],
                    "total_executions": total,
                    "verify_failed": r["verify_failed"],
                    "verify_failed_rate": round(vf_rate, 3),
                    "rollback_failed": r["rollback_failed"],
                    "rollback": r["rollback"],
                    "ok": r["ok"],
                    "decay_signals": signals,
                }
            )

    # rollback_failed first (critical), then by verify_failed_rate desc
    results.sort(key=lambda x: (-x["rollback_failed"], -x["verify_failed_rate"]))
    return results
    migrate_legacy_jsonl(path)
