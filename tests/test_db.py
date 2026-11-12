"""Тесты SQLite-хранилища."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.db import JobStore
from app.media import write_wav_silence


def _create(store: JobStore, *, job_id: str, tmp_path: Path, filename: str = "a.wav") -> dict:
    source = tmp_path / f"{job_id}.wav"
    write_wav_silence(source, 0.1)
    return store.create_job(
        job_id=job_id,
        filename=filename,
        source_path=source,
        model="gigaam-v3-e2e-rnnt",
        timestamps=False,
        duration_s=1.0,
    )


def test_queue_order_and_position(store: JobStore, tmp_path: Path) -> None:
    first = _create(store, job_id="first", tmp_path=tmp_path)
    _create(store, job_id="second", tmp_path=tmp_path)
    assert store.queue_position(first["id"]) == 1
    assert store.count_queued() == 2
    claimed = store.claim_next("worker-1")
    assert claimed is not None
    assert claimed["id"] == "first"
    assert claimed["status"] == "extracting"
    assert claimed["worker_id"] == "worker-1"
    assert store.queue_position("second") == 1
    assert store.count_queued() == 1
    again = store.claim_next("worker-1")
    assert again is not None and again["id"] == "second"
    assert store.claim_next("worker-1") is None


def test_cancel_queued_deletes(store: JobStore, tmp_path: Path) -> None:
    job = _create(store, job_id="cancel-me", tmp_path=tmp_path)
    mode, row = store.request_cancel_or_delete(job["id"])
    assert mode == "deleted"
    assert row is not None
    assert store.get_job(job["id"]) is None


def test_cancel_active_marks_flags(store: JobStore, tmp_path: Path) -> None:
    job = _create(store, job_id="active", tmp_path=tmp_path)
    store.claim_next("worker-1")
    mode, row = store.request_cancel_or_delete(job["id"])
    assert mode == "canceling"
    assert row is not None
    current = store.get_job(job["id"])
    assert current is not None
    assert current["cancel_requested"] == 1
    assert current["delete_after_finish"] == 1


def test_other_worker_jobs_fail(store: JobStore, tmp_path: Path) -> None:
    job = _create(store, job_id="old", tmp_path=tmp_path)
    store.claim_next("old-worker")
    assert store.fail_active_jobs_of_other_workers("new-worker") == 1
    current = store.get_job(job["id"])
    assert current is not None
    assert current["status"] == "failed"
    assert current["error_code"] == "worker_restarted"


def test_runtime_state_heartbeat(store: JobStore) -> None:
    store.register_worker("w1", version="0.1.0")
    state = store.get_runtime_state()
    assert state is not None
    assert state["status"] == "loading"
    assert state["worker_id"] == "w1"
    assert store.runtime_state_is_fresh(60)
    store.update_worker_state(status="ready", asr_model="gigaam-v3-e2e-rnnt", device="cpu")
    state = store.get_runtime_state()
    assert state is not None
    assert state["status"] == "ready"


def test_expired_jobs(store: JobStore, tmp_path: Path) -> None:
    job = _create(store, job_id="expired", tmp_path=tmp_path)
    old = (datetime.now(UTC) - timedelta(hours=48)).isoformat(timespec="milliseconds")
    store.update_job(job["id"], status="done", finished_at=old)
    expired = store.list_expired(24)
    assert [row["id"] for row in expired] == ["expired"]
    store.delete_job(job["id"])
    assert store.get_job(job["id"]) is None


def test_update_rejects_unknown_field(store: JobStore, tmp_path: Path) -> None:
    job = _create(store, job_id="fields", tmp_path=tmp_path)
    try:
        store.update_job(job["id"], evil="x")
    except ValueError as exc:
        assert "evil" in str(exc)
    else:
        raise AssertionError("ожидалось ValueError")
