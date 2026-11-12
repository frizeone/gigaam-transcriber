"""Общие фикстуры и генераторы тестовых файлов."""

from __future__ import annotations

import math
import shutil
import struct
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import JobStore
from app.main import create_app


def write_wav(
    path: Path,
    *,
    duration_s: float = 3.0,
    sample_rate: int = 16_000,
    channels: int = 1,
    frequency: float = 440.0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(duration_s * sample_rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        data = bytearray()
        for i in range(frames):
            value = int(8000 * math.sin(2 * math.pi * frequency * i / sample_rate))
            for _ in range(channels):
                data.extend(struct.pack("<h", value))
        wav.writeframes(bytes(data))
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        hf_home=tmp_path / "models",
        asr_backend="fake",
        asr_device="cpu",
        asr_quant="int8",
        auth_token="",
        upload_rate_limit_per_min=100,
        log_level="warning",
    )


@pytest.fixture
def store(settings: Settings) -> JobStore:
    job_store = JobStore(settings.db_path)
    job_store.initialize()
    return job_store


@pytest.fixture
def app(settings: Settings):
    application = create_app(settings)
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_wav(tmp_path: Path) -> Path:
    return write_wav(tmp_path / "sample.wav", duration_s=3.0)


@pytest.fixture
def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def require_ffmpeg() -> None:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe не установлены")
