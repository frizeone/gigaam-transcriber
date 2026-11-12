"""Интеграционные тесты воркера с FakeTranscriber (полный цикл, без GPU)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from docx import Document

from app.asr.fake import FakeTranscriber
from app.config import Settings
from app.db import JobStore
from app.worker import Worker
from tests.conftest import write_wav


def _make_job(settings: Settings, *, job_id: str = "job", duration: float = 6.0) -> str:
    settings.ensure_directories()
    store = JobStore(settings.db_path)
    store.initialize()
    source = settings.uploads_dir / job_id / "запись.wav"
    write_wav(source, duration_s=duration)
    store.create_job(
        job_id=job_id,
        filename="запись.wav",
        source_path=source,
        model=settings.asr_model,
        timestamps=False,
        duration_s=duration,
    )
    return job_id


def _worker(settings: Settings, store: JobStore, *, empty: bool = False, fail: bool = False) -> Worker:
    def factory(_settings: Settings, model_name: str) -> FakeTranscriber:
        return FakeTranscriber(model_name=model_name, empty=empty, fail=fail)

    return Worker(settings, store=store, transcriber_factory=factory, worker_id="test-worker")


def test_full_cycle_creates_docx(settings: Settings) -> None:
    settings.ensure_directories()
    store = JobStore(settings.db_path)
    store.initialize()
    job_id = _make_job(settings)
    worker = _worker(settings, store)
    try:
        assert worker.run_once() is True
    finally:
        worker.shutdown()
    row = store.get_job(job_id)
    assert row is not None
    assert row["status"] == "done", row
    assert row["progress"] == 100
    assert row["preview"]
    assert row["result_path"] and Path(row["result_path"]).is_file()
    assert row["source_path"] is None
    assert row["rtf"] is not None
    doc = Document(row["result_path"])
    text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    assert "Транскрибация: запись.wav" in text
    assert "тестовая расшифровка" in text.lower()
    assert not (settings.uploads_dir / job_id).exists()


def test_empty_result_is_valid_docx(settings: Settings) -> None:
    settings.ensure_directories()
    store = JobStore(settings.db_path)
    store.initialize()
    job_id = _make_job(settings, job_id="silence")
    worker = _worker(settings, store, empty=True)
    try:
        worker.run_once()
    finally:
        worker.shutdown()
    row = store.get_job(job_id)
    assert row is not None
    assert row["status"] == "done"
    assert row["preview"] == "Речь не обнаружена."
    doc = Document(row["result_path"])
    text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
    assert "Речь не обнаружена." in text


def test_transcriber_failure_marks_failed(settings: Settings) -> None:
    settings.ensure_directories()
    store = JobStore(settings.db_path)
    store.initialize()
    job_id = _make_job(settings, job_id="failure")
    worker = _worker(settings, store, fail=True)
    try:
        worker.run_once()
    finally:
        worker.shutdown()
    row = store.get_job(job_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "asr_failed"
    assert row["result_path"] is None
    assert not (settings.uploads_dir / job_id).exists()


def test_cancel_during_job_deletes(settings: Settings) -> None:
    settings.ensure_directories()
    store = JobStore(settings.db_path)
    store.initialize()
    job_id = _make_job(settings, job_id="cancel")
    worker = _worker(settings, store)
    worker.startup()
    try:
        row = store.claim_next(worker.worker_id)
        assert row is not None
        mode, _ = store.request_cancel_or_delete(job_id)
        assert mode == "canceling"
        worker.process_job(row)
    finally:
        worker.shutdown()
    assert store.get_job(job_id) is None


def test_worker_startup_fails_orphaned_jobs(settings: Settings) -> None:
    settings.ensure_directories()
    store = JobStore(settings.db_path)
    store.initialize()
    job_id = _make_job(settings, job_id="orphan")
    store.claim_next("old-worker")
    worker = _worker(settings, store)
    try:
        worker.startup()
    finally:
        worker.shutdown()
    row = store.get_job(job_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "worker_restarted"


def test_worker_ready_state(settings: Settings) -> None:
    settings.ensure_directories()
    store = JobStore(settings.db_path)
    store.initialize()
    worker = _worker(settings, store)
    try:
        worker.startup()
        state = store.get_runtime_state()
        assert state is not None
        assert state["status"] == "ready"
        assert state["worker_id"] == "test-worker"
        assert store.runtime_state_is_fresh(30)
    finally:
        worker.shutdown()
    state = store.get_runtime_state()
    assert state is not None
    assert state["status"] == "stopped"


def test_expired_results_removed(settings: Settings) -> None:
    settings.ensure_directories()
    store = JobStore(settings.db_path)
    store.initialize()
    job_id = _make_job(settings, job_id="expired")
    result = settings.results_dir / job_id / "old.docx"
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_bytes(b"PK")
    store.update_job(
        job_id,
        status="done",
        result_path=str(result),
        finished_at=(datetime.now(UTC) - timedelta(hours=48)).isoformat(timespec="milliseconds"),
    )
    worker = _worker(settings, store)
    worker._cleanup_expired()
    assert store.get_job(job_id) is None
    assert not result.exists()


def test_stale_worker_state_is_not_fresh(store: JobStore) -> None:
    store.register_worker("dead", version="0.1.0")
    store.update_worker_state(status="ready", heartbeat_at="2000-01-01T00:00:00+00:00")
    assert not store.runtime_state_is_fresh(30)
