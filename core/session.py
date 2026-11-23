"""SQLite-backed session store for the SDLC pipeline.

Stores pipeline runs, stage artifacts, audit log, and kanban tasks.
Uses WAL mode with NFS fallback for safe concurrent access across threads.

Schema is pipeline-specific: runs, stage_artifacts, audit_log, kanban_tasks.
FTS5 virtual table on artifact content enables full-text search across stages.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3

_WAL_INCOMPAT_MARKERS = (
    "locking protocol",
    "not authorized",
    "disk i/o error",
)

_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          TEXT PRIMARY KEY,
    idea            TEXT NOT NULL,
    config_json     TEXT,
    status          TEXT DEFAULT 'running',
    current_stage   TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    tenant_id       TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS stage_artifacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    stage           TEXT NOT NULL,
    artifact_json   TEXT NOT NULL,
    status          TEXT DEFAULT 'pending',
    created_at      TEXT,
    approved_at     TEXT,
    approved_by     TEXT,
    UNIQUE(run_id, stage)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT,
    stage       TEXT,
    decision    TEXT,
    actor       TEXT,
    rationale   TEXT,
    timestamp   TEXT
);

CREATE TABLE IF NOT EXISTS kanban_tasks (
    task_id         TEXT PRIMARY KEY,
    run_id          TEXT,
    title           TEXT,
    assignee        TEXT,
    status          TEXT DEFAULT 'ready',
    priority        INTEGER DEFAULT 0,
    depends_on      TEXT,
    metadata_json   TEXT,
    comments_json   TEXT DEFAULT '[]',
    created_at      TEXT,
    started_at      TEXT,
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS kanban_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT,
    run_id      TEXT,
    event_type  TEXT,
    payload     TEXT,
    created_at  TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS artifact_fts USING fts5(
    run_id,
    stage,
    content,
    content='stage_artifacts',
    content_rowid='id'
);

CREATE TABLE IF NOT EXISTS token_usage (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    stage            TEXT NOT NULL,
    role             TEXT NOT NULL,
    tokens_in        INTEGER DEFAULT 0,
    tokens_out       INTEGER DEFAULT 0,
    duration_seconds REAL DEFAULT 0.0,
    model            TEXT DEFAULT '',
    recorded_at      TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retry(fn, max_attempts: int = 5, base_delay: float = 0.05):
    """Retry SQLite BUSY/LOCKED errors with jittered exponential backoff.

    Uses jittered exponential backoff to avoid thundering-herd on the lock.
    """
    for attempt in range(max_attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                if attempt < max_attempts - 1:
                    delay = base_delay * (2**attempt) * (0.5 + random.random())
                    time.sleep(delay)
                    continue
            raise
    raise RuntimeError("unreachable")


class PipelineDB:
    """Thread-safe SQLite store for the SDLC pipeline.

    One instance per process; gateway processes may share it across platform
    threads via WAL mode with connection-per-thread isolation.
    """

    def __init__(self, db_path: Optional[Path] = None, tenant_id: str = "default"):
        self._path = db_path or (Path.home() / ".sdlc-factory" / "pipeline.db")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.tenant_id = tenant_id
        self._init_db()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if any(m in str(exc).lower() for m in _WAL_INCOMPAT_MARKERS):
                    logger.warning("WAL unavailable (%s), falling back to DELETE journal", exc)
                else:
                    raise
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._conn()
        conn.executescript(_CREATE_SCHEMA)
        row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schema_meta VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
        else:
            existing = int(row["value"])
            if existing < SCHEMA_VERSION:
                self._migrate(conn, existing)
                conn.execute(
                    "UPDATE schema_meta SET value=? WHERE key='version'",
                    (str(SCHEMA_VERSION),),
                )
                conn.commit()
        # Create the tenant index after all migrations have run so the column exists
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_tenant ON pipeline_runs (tenant_id)"
        )
        conn.commit()

    def _migrate(self, conn: sqlite3.Connection, from_version: int) -> None:
        if from_version < 2:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS token_usage (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id           TEXT NOT NULL,
                    stage            TEXT NOT NULL,
                    role             TEXT NOT NULL,
                    tokens_in        INTEGER DEFAULT 0,
                    tokens_out       INTEGER DEFAULT 0,
                    duration_seconds REAL DEFAULT 0.0,
                    model            TEXT DEFAULT '',
                    recorded_at      TEXT
                );
            """)
        if from_version < 3:
            conn.executescript("""
                ALTER TABLE pipeline_runs ADD COLUMN tenant_id TEXT DEFAULT 'default';
                CREATE INDEX IF NOT EXISTS idx_pipeline_runs_tenant ON pipeline_runs (tenant_id);
            """)
        logger.info("Schema migrated from v%d to v%d", from_version, SCHEMA_VERSION)

    # ------------------------------------------------------------------
    # Pipeline runs
    # ------------------------------------------------------------------

    def create_run(self, run_id: str, idea: str, config: dict) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT OR IGNORE INTO pipeline_runs "
                "(run_id, idea, config_json, status, started_at, tenant_id) VALUES (?,?,?,?,?,?)",
                (run_id, idea, json.dumps(config), "running", _now_iso(), self.tenant_id),
            )
            self._conn().commit()

    def update_run_status(self, run_id: str, status: str, stage: Optional[str] = None) -> None:
        with self._write_lock:
            conn = self._conn()
            completed = _now_iso() if status in ("completed", "failed") else None
            conn.execute(
                "UPDATE pipeline_runs SET status=?, current_stage=?, completed_at=? "
                "WHERE run_id=? AND tenant_id=?",
                (status, stage, completed, run_id, self.tenant_id),
            )
            conn.commit()

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = (
            self._conn()
            .execute(
                "SELECT * FROM pipeline_runs WHERE run_id=? AND tenant_id=?",
                (run_id, self.tenant_id),
            )
            .fetchone()
        )
        return dict(row) if row else None

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = (
            self._conn()
            .execute(
                "SELECT * FROM pipeline_runs WHERE tenant_id=? ORDER BY started_at DESC LIMIT ?",
                (self.tenant_id, limit),
            )
            .fetchall()
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Stage artifacts
    # ------------------------------------------------------------------

    def save_artifact(self, run_id: str, stage: str, artifact: Any) -> None:
        payload = json.dumps(artifact, ensure_ascii=False)
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO stage_artifacts "
                "(run_id, stage, artifact_json, status, created_at) VALUES (?,?,?,?,?)",
                (run_id, stage, payload, "pending", _now_iso()),
            )
            conn.commit()
        logger.info("Artifact saved: run=%s stage=%s", run_id, stage)

    def get_artifact(self, run_id: str, stage: str) -> Optional[Any]:
        row = (
            self._conn()
            .execute(
                "SELECT artifact_json FROM stage_artifacts WHERE run_id=? AND stage=?",
                (run_id, stage),
            )
            .fetchone()
        )
        if row is None:
            return None
        return json.loads(row["artifact_json"])

    def approve_artifact(self, run_id: str, stage: str, approved_by: str) -> None:
        with self._write_lock:
            self._conn().execute(
                "UPDATE stage_artifacts SET status='approved', approved_at=?, approved_by=? "
                "WHERE run_id=? AND stage=?",
                (_now_iso(), approved_by, run_id, stage),
            )
            self._conn().commit()

    def get_last_completed_stage(self, run_id: str) -> Optional[str]:
        row = (
            self._conn()
            .execute(
                "SELECT stage FROM stage_artifacts WHERE run_id=? AND status='approved' "
                "ORDER BY id DESC LIMIT 1",
                (run_id,),
            )
            .fetchone()
        )
        return row["stage"] if row else None

    def clear_stages_after(self, run_id: str, stage_name: str, stage_order: List[str]) -> List[str]:
        """Delete all stage artifacts that come after stage_name in stage_order.

        Used by the AMENDED flow: when a stage is amended, downstream stages
        must be invalidated so the pipeline re-runs them on resume.
        Returns the list of cleared stage names.
        """
        try:
            idx = stage_order.index(stage_name)
        except ValueError:
            return []
        downstream = stage_order[idx + 1 :]
        if not downstream:
            return []
        with self._write_lock:
            conn = self._conn()
            for stage in downstream:
                conn.execute(
                    "DELETE FROM stage_artifacts WHERE run_id=? AND stage=?",
                    (run_id, stage),
                )
            conn.commit()
        logger.info(
            "Cleared %d downstream stages after %s for run %s: %s",
            len(downstream),
            stage_name,
            run_id,
            downstream,
        )
        return downstream

    def update_artifact(self, run_id: str, stage: str, artifact: Any) -> None:
        """Replace an existing stage artifact (used by AMENDED approval flow)."""
        payload = json.dumps(artifact, ensure_ascii=False)
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "UPDATE stage_artifacts SET artifact_json=?, status='pending', "
                "approved_at=NULL, approved_by=NULL WHERE run_id=? AND stage=?",
                (payload, run_id, stage),
            )
            conn.commit()
        logger.info("Artifact updated (amended): run=%s stage=%s", run_id, stage)

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def log_decision(
        self,
        run_id: str,
        stage: str,
        decision: str,
        actor: str,
        rationale: str = "",
    ) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO audit_log (run_id, stage, decision, actor, rationale, timestamp) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, stage, decision, actor, rationale, _now_iso()),
            )
            self._conn().commit()

    def get_audit_log(self, run_id: str) -> List[Dict[str, Any]]:
        rows = (
            self._conn()
            .execute("SELECT * FROM audit_log WHERE run_id=? ORDER BY id ASC", (run_id,))
            .fetchall()
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Kanban tasks
    # ------------------------------------------------------------------

    def create_task(
        self,
        task_id: str,
        run_id: str,
        title: str,
        assignee: str,
        metadata: Optional[dict] = None,
        depends_on: Optional[List[str]] = None,
        priority: int = 0,
    ) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT OR IGNORE INTO kanban_tasks "
                "(task_id, run_id, title, assignee, status, priority, "
                "depends_on, metadata_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    run_id,
                    title,
                    assignee,
                    "ready",
                    priority,
                    json.dumps(depends_on or []),
                    json.dumps(metadata or {}),
                    _now_iso(),
                ),
            )
            self._conn().commit()

    def update_task_status(
        self,
        task_id: str,
        status: str,
        metadata: Optional[dict] = None,
    ) -> None:
        with self._write_lock:
            conn = self._conn()
            now = _now_iso()
            if status == "in_progress":
                conn.execute(
                    "UPDATE kanban_tasks SET status=?, started_at=? WHERE task_id=?",
                    (status, now, task_id),
                )
            elif status == "done":
                meta_update = ""
                params: list = [status, now]
                if metadata is not None:
                    meta_update = ", metadata_json=?"
                    params.append(json.dumps(metadata))
                params.append(task_id)
                conn.execute(
                    f"UPDATE kanban_tasks SET status=?, completed_at=?{meta_update} "
                    f"WHERE task_id=?",
                    params,
                )
            else:
                conn.execute(
                    "UPDATE kanban_tasks SET status=? WHERE task_id=?",
                    (status, task_id),
                )
            conn.commit()

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = (
            self._conn()
            .execute("SELECT * FROM kanban_tasks WHERE task_id=?", (task_id,))
            .fetchone()
        )
        if not row:
            return None
        d = dict(row)
        d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
        d["depends_on"] = json.loads(d.pop("depends_on") or "[]")
        d["comments"] = json.loads(d.pop("comments_json") or "[]")
        return d

    def list_tasks(self, run_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = (
                self._conn()
                .execute(
                    "SELECT * FROM kanban_tasks WHERE run_id=? AND status=? ORDER BY priority DESC",
                    (run_id, status),
                )
                .fetchall()
            )
        else:
            rows = (
                self._conn()
                .execute(
                    "SELECT * FROM kanban_tasks WHERE run_id=? ORDER BY priority DESC",
                    (run_id,),
                )
                .fetchall()
            )
        result = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
            d["depends_on"] = json.loads(d.pop("depends_on") or "[]")
            d["comments"] = json.loads(d.pop("comments_json") or "[]")
            result.append(d)
        return result

    def add_task_comment(self, task_id: str, comment: str, author: str = "agent") -> None:
        with self._write_lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT comments_json FROM kanban_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row:
                return
            comments = json.loads(row["comments_json"] or "[]")
            comments.append({"author": author, "text": comment, "ts": _now_iso()})
            conn.execute(
                "UPDATE kanban_tasks SET comments_json=? WHERE task_id=?",
                (json.dumps(comments), task_id),
            )
            conn.commit()

    def are_dependencies_done(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if not task:
            return False
        deps = task.get("depends_on") or []
        if not deps:
            return True
        for dep_id in deps:
            dep = self.get_task(dep_id)
            if not dep or dep["status"] != "done":
                return False
        return True

    # ------------------------------------------------------------------
    # Token / cost tracking
    # ------------------------------------------------------------------

    def record_token_usage(
        self,
        run_id: str,
        stage: str,
        role: str,
        tokens_in: int,
        tokens_out: int,
        duration_seconds: float = 0.0,
        model: str = "",
    ) -> None:
        with self._write_lock:
            self._conn().execute(
                "INSERT INTO token_usage "
                "(run_id, stage, role, tokens_in, tokens_out, duration_seconds, model, recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_id, stage, role, tokens_in, tokens_out, duration_seconds, model, _now_iso()),
            )
            self._conn().commit()

    def get_token_summary(self, run_id: str) -> List[Dict[str, Any]]:
        """Return per-role token usage rows, ordered by stage then role."""
        rows = (
            self._conn()
            .execute(
                "SELECT stage, role, model, "
                "SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out, "
                "SUM(duration_seconds) AS duration_seconds "
                "FROM token_usage WHERE run_id=? "
                "GROUP BY stage, role, model ORDER BY recorded_at ASC",
                (run_id,),
            )
            .fetchall()
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # FTS search
    # ------------------------------------------------------------------

    def search_artifacts(self, query: str, run_id: Optional[str] = None) -> List[Dict]:
        if run_id:
            rows = (
                self._conn()
                .execute(
                    "SELECT sa.run_id, sa.stage, sa.artifact_json "
                    "FROM artifact_fts fts JOIN stage_artifacts sa ON sa.rowid = fts.rowid "
                    "WHERE artifact_fts MATCH ? AND sa.run_id=?",
                    (query, run_id),
                )
                .fetchall()
            )
        else:
            rows = (
                self._conn()
                .execute(
                    "SELECT sa.run_id, sa.stage, sa.artifact_json "
                    "FROM artifact_fts fts JOIN stage_artifacts sa ON sa.rowid = fts.rowid "
                    "WHERE artifact_fts MATCH ?",
                    (query,),
                )
                .fetchall()
            )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Module-level default instance
# ---------------------------------------------------------------------------

_default_db: Optional[PipelineDB] = None
_default_db_lock = threading.Lock()


def get_default_db(
    db_path: Optional[Path] = None,
    tenant_id: str = "default",
) -> PipelineDB:
    global _default_db
    with _default_db_lock:
        if _default_db is None:
            _default_db = PipelineDB(db_path, tenant_id=tenant_id)
        elif tenant_id != "default" and _default_db.tenant_id != tenant_id:
            # Different tenant requested — return a new scoped instance (not cached).
            return PipelineDB(db_path or _default_db._path, tenant_id=tenant_id)
    return _default_db
