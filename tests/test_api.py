"""Контракты HTTP API и коды ошибок."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import JobStore
from app.main import create_app
from app.media import write_wav_silence
from tests.conftest import write_wav


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "hf_home": tmp_path / "models",
        "asr_backend": "fake",
        "asr_device": "cpu",
        "asr_quant": "int8",
        "auth_token": "",
        "upload_rate_limit_per_min": 100,
        "log_level": "warning",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def make_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _insert_queued(settings: Settings, *, job_id: str = "job-1") -> None:
    settings.ensure_directories()
    store = JobStore(settings.db_path)
    store.initialize()
    source = settings.uploads_dir / job_id / "file.wav"
    write_wav_silence(source, 1.0)
    store.create_job(
        job_id=job_id,
        filename="file.wav",
        source_path=source,
        model=settings.asr_model,
        timestamps=False,
        duration_s=1.0,
    )


def test_health_and_limits(client: TestClient) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in health.headers["content-security-policy"]
    assert health.headers["cache-control"] == "no-store"
    limits = client.get("/api/limits")
    assert limits.status_code == 200
    payload = limits.json()
    assert payload["max_upload_mb"] == 2048
    assert "gigaam-v3-e2e-rnnt" in payload["models"]
    assert ".mp4" in payload["allowed_extensions"]
    assert payload["auth_required"] is False


def test_ready_without_worker(client: TestClient) -> None:
    response = client.get("/api/ready")
    assert response.status_code == 503
    payload = response.json()
    assert payload["ready"] is False
    assert payload["status"] == "starting"
    assert payload["error"]["code"] == "not_ready"


def test_ready_with_worker_state(app, client: TestClient) -> None:
    store: JobStore = app.state.store
    store.register_worker("worker-1", version="0.1.0")
    store.update_worker_state(
        status="ready",
        asr_model="gigaam-v3-e2e-rnnt",
        device="cuda",
        cuda_available=1,
        providers="CUDAExecutionProvider,CPUExecutionProvider",
        loaded_models='["gigaam-v3-e2e-rnnt"]',
        message="Модель загружена",
    )
    response = client.get("/api/ready")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert payload["device"] == "cuda"
    assert payload["cuda_available"] is True
    assert payload["error"] is None


def test_upload_get_list_and_download_conflict(client: TestClient, sample_wav: Path) -> None:
    data = {"timestamps": "true", "model": "gigaam-v3-e2e-rnnt"}
    with sample_wav.open("rb") as handle:
        response = client.post(
            "/api/jobs",
            files={"file": ("Тест запись.wav", handle, "audio/wav")},
            data=data,
        )
    assert response.status_code == 201, response.text
    payload = response.json()
    job_id = payload["job_id"]
    assert payload["location"] == f"/api/jobs/{job_id}"

    job_response = client.get(f"/api/jobs/{job_id}")
    assert job_response.status_code == 200
    job = job_response.json()
    assert job["status"] == "queued"
    assert job["filename"] == "Тест запись.wav"
    assert job["duration_s"] == pytest.approx(3.0, abs=0.1)
    assert job["queue_position"] == 1
    assert job["timestamps"] is True
    assert job["events_url"] == f"/api/jobs/{job_id}/events"

    listing = client.get("/api/jobs").json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == job_id

    conflict = client.get(f"/api/jobs/{job_id}/result.docx")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"


def test_upload_rejects_unsupported_extension(client: TestClient) -> None:
    response = client.post("/api/jobs", files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media"


def test_upload_rejects_fake_mp4(client: TestClient) -> None:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe не установлены")
    response = client.post("/api/jobs", files={"file": ("movie.mp4", b"not a video", "video/mp4")})
    assert response.status_code == 415
    message = response.json()["error"]["message"]
    assert "аудиодорожка" in message or "не похож" in message


def test_upload_empty_file(client: TestClient) -> None:
    response = client.post("/api/jobs", files={"file": ("empty.wav", b"", "audio/wav")})
    assert response.status_code == 415


def test_file_too_large(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_upload_mb=1)
    with make_client(settings) as client:
        response = client.post(
            "/api/jobs",
            files={"file": ("big.wav", b"x" * (1024 * 1024 + 10), "audio/wav")},
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


def test_duration_limit(tmp_path: Path) -> None:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe не установлены")
    settings = make_settings(tmp_path, max_duration_min=1)
    wav = write_wav(tmp_path / "long.wav", duration_s=61.0)
    with make_client(settings) as client, wav.open("rb") as handle:
        response = client.post("/api/jobs", files={"file": ("long.wav", handle, "audio/wav")})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "duration_exceeded"


def test_invalid_model(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        files={"file": ("a.wav", b"123", "audio/wav")},
        data={"model": "whisper-large"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_model"


def test_queue_full(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, queue_max=1)
    _insert_queued(settings)
    with make_client(settings) as client:
        response = client.post("/api/jobs", files={"file": ("a.wav", b"123", "audio/wav")})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"


def test_upload_rate_limit(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, upload_rate_limit_per_min=1)
    with make_client(settings) as client:
        first = client.post("/api/jobs", files={"file": ("a.exe", b"1", "application/octet-stream")})
        second = client.post("/api/jobs", files={"file": ("b.exe", b"1", "application/octet-stream")})
    assert first.status_code == 415
    assert second.status_code == 429
    assert "Retry-After" in second.headers


def test_auth_token(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, auth_token="secret-token")
    with make_client(settings) as client:
        unauthorized = client.get("/api/jobs")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "unauthorized"
        assert unauthorized.headers["www-authenticate"] == "Bearer"
        header_ok = client.get("/api/jobs", headers={"X-Auth-Token": "secret-token"})
        assert header_ok.status_code == 200
        bearer_ok = client.get("/api/jobs", headers={"Authorization": "Bearer secret-token"})
        assert bearer_ok.status_code == 200
        query_ok = client.get("/api/jobs?token=secret-token")
        assert query_ok.status_code == 200
        health = client.get("/api/health")
        assert health.status_code == 200
        limits = client.get("/api/limits")
        assert limits.status_code == 200


def test_delete_queued_removes_files(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    _insert_queued(settings, job_id="delete-me")
    source = settings.uploads_dir / "delete-me" / "file.wav"
    assert source.exists()
    with make_client(settings) as client:
        response = client.delete("/api/jobs/delete-me")
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"
        assert client.get("/api/jobs/delete-me").status_code == 404
    assert not source.exists()


def test_delete_missing(client: TestClient) -> None:
    response = client.delete("/api/jobs/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_validation_error_format(client: TestClient) -> None:
    response = client.get("/api/jobs?limit=0")
    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["message"] == "Проверьте параметры запроса."


def test_sse_terminal_job(app, client: TestClient, tmp_path: Path) -> None:
    store: JobStore = app.state.store
    settings: Settings = app.state.settings
    result_dir = settings.results_dir / "sse-job"
    result_dir.mkdir(parents=True, exist_ok=True)
    result = result_dir / "file__2026-01-02_03-04.docx"
    result.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    source = settings.uploads_dir / "sse-job" / "file.wav"
    write_wav_silence(source, 1.0)
    store.create_job(
        job_id="sse-job",
        filename="file.wav",
        source_path=source,
        model=settings.asr_model,
        timestamps=False,
        duration_s=1.0,
    )
    store.update_job(
        "sse-job",
        status="done",
        progress=100.0,
        preview="Готовый текст",
        result_path=str(result),
        result_filename=result.name,
    )
    with client.stream("GET", "/api/jobs/sse-job/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = []
        for line in response.iter_lines():
            lines.append(line)
            if line.startswith("event: done"):
                break
    body = "\n".join(lines)
    assert "event: status" in body
    assert "event: progress" in body
    assert "event: done" in body


def test_openapi_and_docs(client: TestClient) -> None:
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    paths = openapi.json()["paths"]
    for path in ("/api/jobs", "/api/jobs/{job_id}", "/api/jobs/{job_id}/events", "/api/health", "/api/ready"):
        assert path in paths
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "/swagger/swagger-ui-bundle.js" in docs.text
    assert "/swagger/swagger-ui.css" in docs.text
    assert client.get("/swagger/swagger-ui-bundle.js").status_code == 200
    assert client.get("/swagger/swagger-ui.css").status_code == 200
    assert client.get("/").status_code == 200
    assert "GigaAM Transcriber" in client.get("/").text
