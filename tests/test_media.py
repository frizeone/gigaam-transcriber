"""Тесты медиа-утилит."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from app.config import Settings
from app.errors import UnsupportedMediaError
from app.media import (
    convert_to_wav,
    format_duration_hms,
    format_timestamp,
    get_extension,
    probe_media,
    sanitize_filename,
    sanitize_stem,
    write_fileobj_limited,
)
from tests.conftest import require_ffmpeg, write_wav


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        (r"C:\temp\audio.mp4", "audio.mp4"),
        ("  ..  ", "audio"),
        ("", "audio"),
        ("концерт.mp4", "концерт.mp4"),
        ("CON.mp4", "_CON.mp4"),
        ("a\x00b.mp3", "a_b.mp3"),
        ("файл.оченьдлинноерасширение", "файл"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_stem_and_extension() -> None:
    assert sanitize_stem("../../доклад.mp4") == "доклад"
    assert get_extension("ДОКЛАД.MP4") == ".mp4"
    assert get_extension("без_расширения") == ""


def test_format_duration() -> None:
    assert format_duration_hms(0) == "00:00:00"
    assert format_duration_hms(3725) == "01:02:05"
    assert format_duration_hms(65.5, with_ms=True) == "00:01:05.500"
    assert format_timestamp(59.9) == "00:00:59"


def test_write_fileobj_limited(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    assert write_fileobj_limited(io.BytesIO(b"x" * 100), target, 100) == 100
    assert target.read_bytes() == b"x" * 100
    target2 = tmp_path / "out2.bin"
    with pytest.raises(OverflowError):
        write_fileobj_limited(io.BytesIO(b"x" * 101), target2, 100)
    assert not target2.exists()


def test_probe_wav(settings: Settings, tmp_path: Path) -> None:
    require_ffmpeg()
    path = write_wav(tmp_path / "probe.wav", duration_s=2.5)
    info = probe_media(path, settings)
    assert info.duration_s == pytest.approx(2.5, abs=0.1)
    assert info.audio_codec == "pcm_s16le"
    assert info.sample_rate == 16_000
    assert info.channels == 1
    assert not info.has_video


def test_probe_rejects_non_media(settings: Settings, tmp_path: Path) -> None:
    require_ffmpeg()
    path = tmp_path / "fake.mp4"
    path.write_bytes(b"this is definitely not a video")
    with pytest.raises(UnsupportedMediaError):
        probe_media(path, settings)


def test_convert_stereo_48k_to_mono_16k(settings: Settings, tmp_path: Path) -> None:
    require_ffmpeg()
    source = write_wav(tmp_path / "stereo.wav", duration_s=1.2, sample_rate=48_000, channels=2)
    target = tmp_path / "converted.wav"
    progress: list[float] = []
    convert_to_wav(source, target, settings, duration_s=1.2, on_progress=progress.append)
    with wave.open(str(target), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16_000
        assert wav.getnframes() == pytest.approx(1.2 * 16_000, abs=100)
    assert progress and progress[-1] == pytest.approx(1.2)
