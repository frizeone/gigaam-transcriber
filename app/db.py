"""SQLite-хранилище задач и состояния воркера.

Один файл БД лежит на volume data и доступен процессам web и worker.
Используется WAL: читатели не блокируют писателя, запись сериализуется
короткими транзакциями. Этого достаточно для одной GPU-очереди.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ACTIVE_STATUSES = ("extracting", "transcribing", "rendering")
TERMINAL_STATUSES = ("done", "failed", "canceled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    filename TEXT NOT NULL,
    source_path TEXT,
    wav_path TEXT,
    result_path TEXT,
    result_filename TEXT,
    preview TEXT,
    model TEXT NOT NULL,
    timestamps INTEGER NOT NULL DEFAULT 0,
    duration_s REAL,
    progress REAL NOT NULL DEFAULT 0,
    processed_seconds REAL NOT NULL DEFAULT 0,
    stage_message TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    transcribing_started_at TEXT,
    processing_seconds REAL,
    rtf REAL,
    peak_vram_mb REAL,
    worker_id TEXT,
    heartbeat_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    delete_after_finish INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_finished ON jobs(finished_at);

CREATE TABLE IF NOT EXISTS runtime_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    worker_id TEXT,
    started_at TEXT,
    heartbeat_at TEXT,
    status TEXT NOT NULL DEFAULT 'starting',
    asr_model TEXT,
    device TEXT,
    cuda_available INTEGER NOT NULL DEFAULT 0,
    providers TEXT,
    message TEXT,
    loaded_models TEXT,
    updated_at TEXT NOT NULL
);
"""

_JOB_COLUMNS = {
    "status",
    "filename",
    "source_path",
    "wav_path",
    "result_path",
    "result_filename",
    "preview",
    "model",
    "timestamps",
    "duration_s",
    "progress",
    "processed_seconds",
    "stage_message",
    "error_code",
    "error_message",
    "started_at",
    "finished_at",
    "transcribing_started_at",
    "processing_seconds",
    "rtf",
    "peak_vram_mb",
    "worker_id",
    "heartbeat_at",
    "cancel_requested",
    "delete_after_finish",
}

_RUNTIME_COLUMNS = {
    "worker_id",
    "started_at",
    "heartbeat_at",
    "status",
    "asr_model",
    "device",
    "cuda_available",
    "providers",
    "message",
    "loaded_models",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="milliseconds")


def iso_before(seconds: float = 0, *, hours: float = 0) -> str:
    return (utcnow() - timedelta(seconds=seconds, hours=hours)).isoformat(timespec="milliseconds")


class JobStore:
    """Тонкая синхронная обёртка над SQLite."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', '1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )

    # ── Задачи ──────────────────────────────────────────────────────────────

    def create_job(
        self,
        *,
        job_id: str,
        filename: str,
        source_path: Path,
        model: str,
        timestamps: bool,
        duration_s: float,
    ) -> dict[str, Any]:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, status, filename, source_path, model, timestamps, duration_s,
                    progress, processed_seconds, stage_message, created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                """,
                (
                    job_id,
                    filename,
                    str(source_path),
                    model,
                    int(timestamps),
                    duration_s,
                    "В очереди",
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row is not None
        return dict(row)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_jobs(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
        return int(row["n"]) if row else 0

    def count_queued(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status='queued'").fetchone()
        return int(row["n"]) if row else 0

    def queue_position(self, job_id: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] != "queued":
                return None
            older = conn.execute(
                """
                SELECT COUNT(*) AS n FROM jobs
                WHERE status='queued' AND (created_at < ? OR (created_at = ? AND id < ?))
                """,
                (row["created_at"], row["created_at"], row["id"]),
            ).fetchone()
        return int(older["n"]) + 1 if older else 1

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        unknown = set(fields) - _JOB_COLUMNS
        if unknown:
            raise ValueError(f"Неизвестные поля jobs: {sorted(unknown)}")
        if not fields:
            return self.get_job(job_id)
        fields["updated_at"] = utcnow_iso()
        assignments = ", ".join(f"{name}=?" for name in fields)
        values = [*(fields.values()), job_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", values)  # noqa: S608 - whitelist
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def set_progress(
        self,
        job_id: str,
        *,
        progress: float,
        processed_seconds: float,
        stage_message: str | None = None,
        heartbeat: bool = True,
    ) -> None:
        fields: dict[str, Any] = {
            "progress": max(0.0, min(100.0, progress)),
            "processed_seconds": max(0.0, processed_seconds),
        }
        if stage_message is not None:
            fields["stage_message"] = stage_message
        if heartbeat:
            fields["heartbeat_at"] = utcnow_iso()
        self.update_job(job_id, **fields)

    def claim_next(self, worker_id: str) -> dict[str, Any] | None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status='queued' AND cancel_requested=0
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    """
                    UPDATE jobs SET status='extracting', worker_id=?, started_at=?,
                        heartbeat_at=?, updated_at=?, stage_message='Извлечение аудио'
                    WHERE id=?
                    """,
                    (worker_id, now, now, now, row["id"]),
                )
                claimed = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return dict(claimed) if claimed else None

    def set_failed(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        stage_message: str = "Ошибка",
    ) -> None:
        now = utcnow_iso()
        self.update_job(
            job_id,
            status="failed",
            error_code=code,
            error_message=message,
            stage_message=stage_message,
            finished_at=now,
            heartbeat_at=now,
            progress=0,
        )

    def set_canceled(self, job_id: str) -> None:
        now = utcnow_iso()
        self.update_job(
            job_id,
            status="canceled",
            stage_message="Отменено",
            finished_at=now,
            heartbeat_at=now,
            cancel_requested=1,
        )

    def request_cancel_or_delete(self, job_id: str) -> tuple[str, dict[str, Any] | None]:
        """Вернуть ('deleted'|'canceling'|'absent', строка)."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return "absent", None
                if row["status"] in ACTIVE_STATUSES:
                    now = utcnow_iso()
                    conn.execute(
                        """
                        UPDATE jobs SET cancel_requested=1, delete_after_finish=1,
                            stage_message='Отмена…', updated_at=?, heartbeat_at=?
                        WHERE id=?
                        """,
                        (now, now, job_id),
                    )
                    conn.execute("COMMIT")
                    return "canceling", dict(row)
                conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                conn.execute("COMMIT")
                return "deleted", dict(row)
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def delete_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return dict(row)

    def clear_paths(self, job_id: str) -> None:
        self.update_job(
            job_id,
            source_path=None,
            wav_path=None,
            result_path=None,
            result_filename=None,
        )

    def list_expired(self, ttl_hours: int) -> list[dict[str, Any]]:
        cutoff = iso_before(hours=ttl_hours)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('done', 'failed', 'canceled')
                  AND COALESCE(finished_at, updated_at) < ?
                ORDER BY finished_at ASC
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ── Восстановление после падений ────────────────────────────────────────

    def fail_active_jobs_of_other_workers(self, worker_id: str) -> int:
        now = utcnow_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status='failed', error_code='worker_restarted',
                    error_message='Обработка прервана перезапуском воркера. Запустите задачу повторно.',
                    stage_message='Прервано перезапуском', finished_at=?, updated_at=?, heartbeat_at=?
                WHERE status IN ('extracting', 'transcribing', 'rendering')
                  AND (worker_id IS NULL OR worker_id != ?)
                """,
                (now, now, now, worker_id),
            )
        return int(cursor.rowcount)

    def fail_all_active_jobs(self, code: str, message: str) -> int:
        now = utcnow_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status='failed', error_code=?, error_message=?,
                    stage_message='Прервано', finished_at=?, updated_at=?, heartbeat_at=?
                WHERE status IN ('extracting', 'transcribing', 'rendering')
                """,
                (code, message, now, now, now),
            )
        return int(cursor.rowcount)

    # ── Состояние воркера ───────────────────────────────────────────────────

    def register_worker(self, worker_id: str, *, version: str) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_state (
                    id, worker_id, started_at, heartbeat_at, status, message, updated_at
                ) VALUES (1, ?, ?, ?, 'loading', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    worker_id=excluded.worker_id,
                    started_at=excluded.started_at,
                    heartbeat_at=excluded.heartbeat_at,
                    status='loading',
                    message=excluded.message,
                    updated_at=excluded.updated_at
                """,
                (worker_id, now, now, f"Воркер запускается (v{version})", now),
            )

    def update_worker_state(self, **fields: Any) -> None:
        unknown = set(fields) - _RUNTIME_COLUMNS
        if unknown:
            raise ValueError(f"Неизвестные поля runtime_state: {sorted(unknown)}")
        fields["updated_at"] = utcnow_iso()
        assignments = ", ".join(f"{name}=?" for name in fields)
        values = list(fields.values())
        with self._connect() as conn:
            conn.execute(f"UPDATE runtime_state SET {assignments} WHERE id=1", values)  # noqa: S608

    def heartbeat(self, worker_id: str, job_id: str | None = None) -> None:
        now = utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE runtime_state SET heartbeat_at=?, updated_at=? WHERE id=1 AND worker_id=?",
                (now, now, worker_id),
            )
            if job_id:
                conn.execute(
                    "UPDATE jobs SET heartbeat_at=?, updated_at=? WHERE id=? AND worker_id=?",
                    (now, now, job_id, worker_id),
                )

    def get_runtime_state(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runtime_state WHERE id=1").fetchone()
        return dict(row) if row else None

    def runtime_state_is_fresh(self, stale_seconds: int) -> bool:
        state = self.get_runtime_state()
        if not state or not state.get("heartbeat_at"):
            return False
        try:
            heartbeat = datetime.fromisoformat(state["heartbeat_at"])
        except ValueError:
            return False
        return (utcnow() - heartbeat).total_seconds() <= stale_seconds

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        counts["total"] = sum(counts.values())
        return counts
